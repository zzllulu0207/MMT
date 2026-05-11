#!/usr/bin/env python3
"""
K8s Node Memory Data Collector
================================
在 K8s 节点上采集原始内存数据，输出 raw_data.json 供 processor.py 加工。

运行方式：
  # 直接在节点上运行（推荐）
  sudo python3 collector.py --ne-id ne-001 --ne-name "NE-Core-01" --ne-type "Core Network Element"

  # 指定输出文件
  sudo python3 collector.py --ne-id ne-001 --ne-name "NE-Core-01" --output /tmp/raw_ne001.json

  # DaemonSet 模式（通过 hostPath 挂载 /proc 和 /sys/fs/cgroup）
  python3 collector.py --ne-id ne-001 --ne-name "NE-Core-01" --inside-pod

采集内容：
  - /proc/meminfo → 节点级内存统计
  - /sys/fs/cgroup/memory/kubepods/**/memory.stat → Pod/容器 cgroup 内存
  - /proc/<pid>/status, /proc/<pid>/smaps_rollup → 进程级内存
  - kubectl API → K8s 元数据 (requests/limits/namespace)

依赖：
  - Python 3.6+
  - kubectl (需在 PATH 中，且有权限访问该节点的 pods)
  - 运行在目标节点上（需访问 /proc, /sys/fs/cgroup）
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ── Constants ──

CGROUP_MEMORY_ROOT = "/sys/fs/cgroup/memory"
PROC_ROOT = "/proc"
KUBEPODS_CGROUP = os.path.join(CGROUP_MEMORY_ROOT, "kubepods")

# --- 由 main() 设置 ---
KUBECONFIG_PATH = ""  # kubectl --kubeconfig 参数路径
KUBECTL_BIN = "kubectl"


# ── Helpers ──

def parse_kv_file(filepath):
    """解析 key: value 格式文件，返回 dict。"""
    result = {}
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                # 尝试转为数字
                try:
                    result[key] = int(val.split()[0]) if val.split() else val
                except ValueError:
                    result[key] = val
    except (IOError, PermissionError) as e:
        print(f"  [WARN] 无法读取 {filepath}: {e}", file=sys.stderr)
    return result


def parse_kv_pairs(text):
    """解析 key value 格式文本（如 memory.stat），返回 dict。"""
    result = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                result[parts[0]] = parts[1]
        elif len(parts) == 1:
            result[parts[0]] = None
    return result


def run_kubectl(args):
    """运行 kubectl 命令，返回 stdout 字符串。"""
    cmd = [KUBECTL_BIN]
    if KUBECONFIG_PATH:
        cmd += ["--kubeconfig", KUBECONFIG_PATH]
    cmd += args
    cmd_str = " ".join(cmd)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30
        )
        stdout_short = result.stdout.strip()[:200] if result.stdout else "(empty)"
        if result.returncode != 0:
            print(f"  [WARN] 命令失败，返回码={result.returncode}", file=sys.stderr)
            print(f"        命令: {cmd_str}", file=sys.stderr)
            print(f"        stdout: {stdout_short}", file=sys.stderr)
            print(f"        stderr: {result.stderr.strip()[:500]}", file=sys.stderr)
            return ""
        return result.stdout
    except FileNotFoundError:
        print(f"  [ERROR] kubectl 未安装或不在 PATH 中", file=sys.stderr)
        print(f"         命令: {cmd_str}", file=sys.stderr)
        return ""
    except subprocess.TimeoutExpired:
        print(f"  [WARN] 命令超时 (30s)", file=sys.stderr)
        print(f"        命令: {cmd_str}", file=sys.stderr)
        return ""


def get_local_ip():
    """获取节点主 IP。"""
    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception:
        pass
    # fallback: 从路由表获取
    try:
        result = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5
        )
        m = re.search(r"src\s+([\d.]+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


# ── Collection Functions ──

def collect_meminfo():
    """采集 /proc/meminfo。"""
    print("  [*] 采集 /proc/meminfo ...")
    meminfo = parse_kv_file(os.path.join(PROC_ROOT, "meminfo"))
    # 过滤出关键字段并规范化 key（去掉括号等）
    original_lines = []
    try:
        with open(os.path.join(PROC_ROOT, "meminfo"), "r") as f:
            original_lines = [line.rstrip() for line in f]
    except IOError:
        pass
    return {"parsed": meminfo, "raw": "\n".join(original_lines)}


def find_container_pids(container_cgroup_path):
    """从 cgroup.procs 文件中获取容器内所有进程 PID。"""
    procs_file = os.path.join(container_cgroup_path, "cgroup.procs")
    pids = []
    try:
        with open(procs_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    pids.append(int(line))
    except (IOError, PermissionError):
        pass
    return pids


def collect_process_info(pid):
    """采集 /proc/<pid>/status 和 /proc/<pid>/smaps_rollup。"""
    status_path = os.path.join(PROC_ROOT, str(pid), "status")
    smaps_path = os.path.join(PROC_ROOT, str(pid), "smaps_rollup")

    status_raw = ""
    status_parsed = {}
    smaps_raw = ""
    smaps_parsed = {}

    try:
        with open(status_path, "r") as f:
            status_raw = f.read()
        status_parsed = parse_kv_file(status_path)
    except (IOError, PermissionError):
        return None

    try:
        with open(smaps_path, "r") as f:
            smaps_raw = f.read()
        smaps_parsed = parse_kv_file(smaps_path)
    except (IOError, PermissionError):
        pass

    # 提取进程名
    proc_name = status_parsed.get("Name", "unknown")
    if isinstance(proc_name, str) and proc_name.startswith("("):
        proc_name = proc_name.strip("()")

    # 提取关键内存指标 (单位 KB)
    def get_kb(key):
        val = status_parsed.get(key, 0)
        if isinstance(val, str):
            parts = val.split()
            try:
                return int(parts[0])
            except (ValueError, IndexError):
                return 0
        return int(val) if val else 0

    return {
        "pid": pid,
        "name": str(status_parsed.get("Name", "unknown")),
        "vm_rss_kb": get_kb("VmRSS"),
        "vm_hwm_kb": get_kb("VmHWM"),
        "rss_anon_kb": get_kb("RssAnon"),
        "rss_file_kb": get_kb("RssFile"),
        "rss_shmem_kb": get_kb("RssShmem"),
        "vm_pss_kb": int(smaps_parsed.get("Pss", 0)) if smaps_parsed else get_kb("VmRSS"),
        "vm_uss_kb": 0,  # 需要从 smaps 详细计算
        "vm_swap_kb": get_kb("VmSwap"),
        "raw_status": status_raw.strip(),
        "raw_smaps": smaps_raw.strip(),
    }


def collect_container(container_cgroup_path, container_name, requests_bytes, limits_bytes):
    """采集单个容器的 cgroup 和进程数据。"""
    print(f"    [*] 容器: {container_name} @ {container_cgroup_path}")

    # 读 memory.stat
    memstat_path = os.path.join(container_cgroup_path, "memory.stat")
    memstat_raw = ""
    memstat = {}
    try:
        with open(memstat_path, "r") as f:
            memstat_raw = f.read().strip()
        memstat = parse_kv_pairs(memstat_raw)
    except (IOError, PermissionError) as e:
        print(f"      [WARN] 无法读取 memory.stat: {e}", file=sys.stderr)

    # 读 memory.usage_in_bytes
    usage_bytes = 0
    try:
        with open(os.path.join(container_cgroup_path, "memory.usage_in_bytes"), "r") as f:
            usage_bytes = int(f.read().strip())
    except (IOError, PermissionError):
        pass

    # RSS
    rss = memstat.get("total_rss", memstat.get("rss", 0))

    # active / inactive
    active_anon = memstat.get("active_anon", 0)
    inactive_anon = memstat.get("inactive_anon", 0)
    active_file = memstat.get("active_file", 0)
    inactive_file = memstat.get("inactive_file", 0)

    # 进程
    processes = []
    pids = find_container_pids(container_cgroup_path)
    for pid in pids:
        proc_info = collect_process_info(pid)
        if proc_info:
            processes.append(proc_info)

    return {
        "name": container_name,
        "requests_memory_bytes": requests_bytes,
        "limits_memory_bytes": limits_bytes,
        "rss_bytes": rss,
        "usage_bytes": usage_bytes,
        "active_anon_bytes": active_anon,
        "inactive_anon_bytes": inactive_anon,
        "active_file_bytes": active_file,
        "inactive_file_bytes": inactive_file,
        "cgroup_memory_stat_raw": memstat_raw,
        "processes": processes,
    }


def discover_pods_via_cgroup():
    """通过遍历 /sys/fs/cgroup/memory/kubepods 发现 Pod。"""
    pods = []
    if not os.path.isdir(KUBEPODS_CGROUP):
        print(f"  [WARN] cgroup 根目录不存在: {KUBEPODS_CGROUP}", file=sys.stderr)
        return pods

    for entry in sorted(os.listdir(KUBEPODS_CGROUP)):
        entry_path = os.path.join(KUBEPODS_CGROUP, entry)
        if not os.path.isdir(entry_path):
            continue

        # Pod cgroup 目录格式: pod<uid> 或 burstable/pod<uid> / guaranteed/pod<uid>
        pod_dirs = []
        if entry.startswith("pod"):
            pod_dirs.append(entry_path)
        elif entry in ("burstable", "guaranteed", "besteffort"):
            # QoS 分类目录
            for sub_entry in sorted(os.listdir(entry_path)):
                sub_path = os.path.join(entry_path, sub_entry)
                if os.path.isdir(sub_path) and sub_entry.startswith("pod"):
                    pod_dirs.append(sub_path)

        for pod_dir in pod_dirs:
            pod_info = collect_pod_from_cgroup(pod_dir)
            if pod_info:
                pods.append(pod_info)

    return pods


def collect_pod_from_cgroup(pod_cgroup_path):
    """从 pod cgroup 目录采集数据。"""
    # 读 memory.stat
    memstat_path = os.path.join(pod_cgroup_path, "memory.stat")
    memstat_raw = ""
    memstat = {}
    try:
        with open(memstat_path, "r") as f:
            memstat_raw = f.read().strip()
        memstat = parse_kv_pairs(memstat_raw)
    except (IOError, PermissionError):
        pass

    # 读 memory.usage_in_bytes (working set)
    working_set = 0
    try:
        with open(os.path.join(pod_cgroup_path, "memory.usage_in_bytes"), "r") as f:
            working_set = int(f.read().strip())
    except (IOError, PermissionError):
        pass

    # RSS
    rss = memstat.get("total_rss", memstat.get("rss", 0))

    # active / inactive
    active_anon = memstat.get("active_anon", 0)
    inactive_anon = memstat.get("inactive_anon", 0)
    active_file = memstat.get("active_file", 0)
    inactive_file = memstat.get("inactive_file", 0)

    # Pod 名和 namespace 将从 kubectl 补充
    return {
        "cgroup_path": pod_cgroup_path,
        "working_set_bytes": working_set,
        "rss_bytes": rss,
        "active_anon_bytes": active_anon,
        "inactive_anon_bytes": inactive_anon,
        "active_file_bytes": active_file,
        "inactive_file_bytes": inactive_file,
        "cgroup_memory_stat_raw": memstat_raw,
        "containers_cgroup_dirs": discover_container_cgroups(pod_cgroup_path),
    }


def discover_container_cgroups(pod_cgroup_path):
    """发现 pod cgroup 下的容器 cgroup 目录。"""
    containers = []
    for entry in sorted(os.listdir(pod_cgroup_path)):
        entry_path = os.path.join(pod_cgroup_path, entry)
        if not os.path.isdir(entry_path):
            continue
        # 容器 cgroup 有 memory.stat 文件
        if os.path.isfile(os.path.join(entry_path, "memory.stat")):
            containers.append(entry_path)
    return containers


def enrich_pods_with_k8s_metadata(pods, node_name):
    """通过 kubectl 获取 Pod 的 K8s 元数据。"""
    print(f"  [*] 获取 K8s Pod 元数据 (kubectl) ...")
    print(f"      节点名: {node_name}")

    # 获取该节点上所有 pod 的 JSON
    kubectl_out = run_kubectl([
        "get", "pods",
        "--all-namespaces",
        "--field-selector", f"spec.nodeName={node_name}",
        "-o", "json"
    ])

    if not kubectl_out:
        print(f"  [WARN] 无法通过 kubectl 获取 pod 列表", file=sys.stderr)
        print(f"         field-selector: spec.nodeName={node_name}", file=sys.stderr)
        print(f"         请检查节点名是否正确，或通过 --node-name 参数手动指定", file=sys.stderr)
        # 尝试列出所有节点名以辅助排查
        nodes_list = run_kubectl(["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"])
        if nodes_list.strip():
            print(f"         集群中可用的节点名: {nodes_list.strip()}", file=sys.stderr)
        return pods

    try:
        k8s_pods = json.loads(kubectl_out).get("items", [])
    except json.JSONDecodeError:
        print("  [WARN] kubectl 输出解析失败", file=sys.stderr)
        return pods

    # 构建 uid → k8s_metadata 映射
    uid_map = {}
    for kp in k8s_pods:
        uid = kp["metadata"]["uid"]
        all_containers = kp.get("spec", {}).get("containers", [])
        containers_meta = {}
        for c in all_containers:
            req = c.get("resources", {}).get("requests", {}).get("memory", "0")
            lim = c.get("resources", {}).get("limits", {}).get("memory", "0")
            containers_meta[c["name"]] = {
                "requests_memory_bytes": parse_k8s_memory(req),
                "limits_memory_bytes": parse_k8s_memory(lim),
            }
        # 计算 pod 总 requests/limits
        total_req = sum(c["requests_memory_bytes"] for c in containers_meta.values())
        total_lim = sum(c["limits_memory_bytes"] for c in containers_meta.values())

        uid_map[uid] = {
            "name": kp["metadata"]["name"],
            "namespace": kp["metadata"]["namespace"],
            "uid": uid,
            "requests_memory_bytes": total_req,
            "limits_memory_bytes": total_lim,
            "containers": containers_meta,
        }

    # 将 k8s 元数据匹配到 cgroup pods（通过 pod UID）
    for pod in pods:
        cgroup_basename = os.path.basename(pod["cgroup_path"])
        # cgroup 目录名格式: pod<uid>
        pod_uid = cgroup_basename.replace("pod", "")
        if pod_uid in uid_map:
            meta = uid_map[pod_uid]
            pod["name"] = meta["name"]
            pod["namespace"] = meta["namespace"]
            pod["uid"] = meta["uid"]
            pod["requests_memory_bytes"] = meta["requests_memory_bytes"]
            pod["limits_memory_bytes"] = meta["limits_memory_bytes"]
            pod["containers_meta"] = meta["containers"]
        else:
            # 尝试部分匹配
            matched = False
            for uid, meta in uid_map.items():
                if pod_uid.startswith(uid[:8]) or uid.startswith(pod_uid[:8]):
                    pod["name"] = meta["name"]
                    pod["namespace"] = meta["namespace"]
                    pod["uid"] = meta["uid"]
                    pod["requests_memory_bytes"] = meta["requests_memory_bytes"]
                    pod["limits_memory_bytes"] = meta["limits_memory_bytes"]
                    pod["containers_meta"] = meta["containers"]
                    matched = True
                    break
            if not matched:
                pod["name"] = f"unknown-{cgroup_basename[:12]}"
                pod["namespace"] = "unknown"
                pod["uid"] = pod_uid
                pod["requests_memory_bytes"] = 0
                pod["limits_memory_bytes"] = 0
                pod["containers_meta"] = {}

    return pods


def enrich_containers_from_cgroup(pod_data, node_name):
    """为每个 Pod 的容器采集详细数据。"""
    enriched_containers = []

    for cgroup_path in pod_data.get("containers_cgroup_dirs", []):
        cgroup_name = os.path.basename(cgroup_path)

        # 匹配容器名称
        container_name = cgroup_name
        req_bytes = 0
        lim_bytes = 0

        containers_meta = pod_data.get("containers_meta", {})
        for cname, cmeta in containers_meta.items():
            # cgroup 目录名通常包含容器名
            if cname in cgroup_name or cgroup_name in cname:
                container_name = cname
                req_bytes = cmeta["requests_memory_bytes"]
                lim_bytes = cmeta["limits_memory_bytes"]
                break

        ctr_data = collect_container(
            cgroup_path, container_name, req_bytes, lim_bytes
        )
        enriched_containers.append(ctr_data)

    return enriched_containers


def parse_k8s_memory(mem_str):
    """解析 K8s 内存字符串 (如 '128Mi', '1Gi') 为 bytes。"""
    if not mem_str or mem_str == "0":
        return 0
    mem_str = str(mem_str).strip()
    units = {
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
        "Ti": 1024 ** 4, "Pi": 1024 ** 5,
        "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3,
        "k": 1000,
    }
    for suffix, multiplier in units.items():
        if mem_str.endswith(suffix):
            try:
                return int(float(mem_str[:-len(suffix)]) * multiplier)
            except ValueError:
                return 0
    # 纯数字（单位 bytes）
    try:
        return int(mem_str)
    except ValueError:
        pass
    # 带 'e' 后缀（如 1e9）
    try:
        return int(float(mem_str))
    except ValueError:
        return 0


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="K8s Node Memory Data Collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ne-id", required=True, help="网元 ID，如 ne-001")
    parser.add_argument("--ne-name", required=True, help="网元名称，如 NE-Core-01")
    parser.add_argument("--ne-type", default="Network Element", help="网元类型")
    parser.add_argument("--output", "-o", default=None, help="输出文件路径（默认: raw_<ne-id>.json）")
    parser.add_argument("--inside-pod", action="store_true", help="DaemonSet/pod 内运行模式")
    parser.add_argument("--node-name", default=os.environ.get("NODE_NAME", ""),
                        help="K8s 节点名（默认读取 NODE_NAME 环境变量）")
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", ""),
                        help="kubeconfig 文件路径（sudo 场景必需，默认读取 KUBECONFIG 环境变量）")
    args = parser.parse_args()

    if not args.node_name:
        print("[ERROR] 请通过 --node-name 指定 K8s 节点名，或设置 NODE_NAME 环境变量", file=sys.stderr)
        sys.exit(1)
    node_name = args.node_name

    # --- kubectl 配置 ---
    global KUBECONFIG_PATH, KUBECTL_BIN
    if args.kubeconfig:
        KUBECONFIG_PATH = args.kubeconfig
    elif os.geteuid() == 0 and "SUDO_USER" in os.environ:
        # sudo 场景：尝试找到原始用户的 kubeconfig
        sudo_user = os.environ["SUDO_USER"]
        sudo_home = os.path.expanduser(f"~{sudo_user}")
        guessed = os.path.join(sudo_home, ".kube", "config")
        if os.path.isfile(guessed):
            KUBECONFIG_PATH = guessed
            print(f"  [*] 自动检测 kubeconfig (SUDO_USER): {KUBECONFIG_PATH}")

    if KUBECONFIG_PATH:
        print(f"  [*] 使用 kubeconfig: {KUBECONFIG_PATH}")
    else:
        print(f"  [*] KUBECONFIG 未指定，依赖 kubectl 默认查找路径")
    tz = datetime.now(timezone(timedelta(hours=8))).astimezone().tzinfo
    collection_time = datetime.now(tz).isoformat()

    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  K8s Memory Collector v1.0                    ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║  NE:     {args.ne_id} ({args.ne_name})".ljust(49) + "║")
    print(f"║  Node:   {node_name}".ljust(49) + "║")
    print(f"║  Time:   {collection_time}".ljust(49) + "║")
    print(f"╚══════════════════════════════════════════════╝")
    print()

    # ── Step 1: Node-level data ──
    print("[1/5] 采集节点级数据 ...")
    meminfo_data = collect_meminfo()
    node_ip = get_local_ip()

    # ── Step 2: Discover Pods via cgroup ──
    print("[2/5] 发现 Pod cgroup ...")
    pods = discover_pods_via_cgroup()
    print(f"      发现 {len(pods)} 个 Pod")

    # ── Step 3: Enrich with K8s metadata ──
    print("[3/5] 获取 K8s 元数据 ...")
    pods = enrich_pods_with_k8s_metadata(pods, node_name)

    # ── Step 4: Collect container & process data ──
    print("[4/5] 采集容器和进程数据 ...")
    for pod in pods:
        print(f"  [*] Pod: {pod.get('name', 'unknown')} (ns={pod.get('namespace', '?')})")
        pod["containers"] = enrich_containers_from_cgroup(pod, node_name)
        # 清理临时字段
        pod.pop("containers_cgroup_dirs", None)
        pod.pop("containers_meta", None)

    # ── Step 5: Build output ──
    print("[5/5] 生成输出文件 ...")
    raw_data = {
        "collection_metadata": {
            "collector_version": "1.0.0",
            "collection_time": collection_time,
            "hostname": node_name,
            "node_ip": node_ip,
            "ne_id": args.ne_id,
            "ne_name": args.ne_name,
            "ne_type": args.ne_type,
        },
        "node": {
            "ip": node_ip,
            "hostname": node_name,
            "meminfo": meminfo_data["parsed"],
            "meminfo_raw": meminfo_data["raw"],
        },
        "pods": pods,
    }

    output_file = args.output or f"raw_{args.ne_id}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2, default=str)

    total_containers = sum(len(p.get("containers", [])) for p in pods)
    total_processes = sum(
        len(c.get("processes", []))
        for p in pods for c in p.get("containers", [])
    )

    print()
    print(f"══════════════════════════════════════════════")
    print(f"  采集完成!")
    print(f"  节点:   {node_name} ({node_ip})")
    print(f"  Pods:   {len(pods)}")
    print(f"  容器:   {total_containers}")
    print(f"  进程:   {total_processes}")
    print(f"  输出:   {output_file}")
    print(f"══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
