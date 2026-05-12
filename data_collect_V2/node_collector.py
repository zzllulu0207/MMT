#!/usr/bin/env python3
"""
Data Node Collector (node_collector)
======================================
在每个 K8s 数据节点上运行，采集本节点内存数据。

运行方式：
  sudo python3 node_collector.py --node-name k8s-worker-01 \
      --pods-info pods_info.json -o raw_k8s-worker-01.json

输入：
  - pods_info.json（由 mgmt_collector.py 生成，包含集群所有 Pod 的 K8s 元数据）

采集内容：
  - /proc/meminfo → 节点级内存统计
  - /sys/fs/cgroup/memory/kubepods/**/memory.stat → Pod/容器 cgroup 内存
  - /proc/<pid>/status, /proc/<pid>/smaps_rollup → 进程级内存

依赖：
  - Python 3.6+
  - root 权限（读取 /proc 和 /sys/fs/cgroup）
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone, timedelta


# ── Constants ──

CGROUP_MEMORY_ROOT = "/sys/fs/cgroup/memory"
PROC_ROOT = "/proc"
KUBEPODS_CGROUP = os.path.join(CGROUP_MEMORY_ROOT, "kubepods")


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


def get_local_ip():
    """获取节点主 IP。"""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        print(f"  [*] 节点 IP (hostname={hostname}): {ip}")
        return ip
    except Exception as e:
        print(f"  [WARN] gethostbyname 失败: {e}", file=sys.stderr)

    cmd = ["ip", "route", "get", "1.1.1.1"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            m = re.search(r"src\s+([\d.]+)", result.stdout)
            if m:
                ip = m.group(1)
                print(f"  [*] 节点 IP (路由表): {ip}")
                return ip
    except Exception:
        pass

    print(f"  [WARN] 无法获取节点 IP，使用 'unknown'", file=sys.stderr)
    return "unknown"


# ── Load pods_info ──

def load_pods_for_node(pods_info_path, node_name):
    """从 pods_info.json 加载指定节点的 Pod 元数据。"""
    print(f"  [*] 加载 Pod 元数据: {pods_info_path}")
    print(f"      过滤节点: {node_name}")
    try:
        with open(pods_info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"  [ERROR] 无法读取 pods_info 文件: {e}", file=sys.stderr)
        return {}

    all_pods = data.get("pods", [])
    # 按 uid 索引，只保留属于本节点的
    pod_map = {}
    for p in all_pods:
        if p.get("node_name") == node_name:
            pod_map[p["uid"]] = p

    print(f"      匹配到 {len(pod_map)} 个 Pod（总共 {len(all_pods)} 个）")
    return pod_map


# ── Collection Functions ──

def collect_meminfo():
    """采集 /proc/meminfo。"""
    print("  [*] 采集 /proc/meminfo ...")
    meminfo = parse_kv_file(os.path.join(PROC_ROOT, "meminfo"))
    original_lines = []
    try:
        with open(os.path.join(PROC_ROOT, "meminfo"), "r") as f:
            original_lines = [line.rstrip() for line in f]
    except IOError:
        pass
    return {"parsed": meminfo, "raw": "\n".join(original_lines)}


def find_container_pids(container_cgroup_path):
    """从 cgroup.procs 获取容器内所有进程 PID。"""
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
        "vm_uss_kb": 0,
        "vm_swap_kb": get_kb("VmSwap"),
        "raw_status": status_raw.strip(),
        "raw_smaps": smaps_raw.strip(),
    }


def collect_container(container_cgroup_path, container_name, requests_bytes, limits_bytes):
    """采集单个容器的 cgroup 和进程数据。"""
    print(f"    [*] 容器: {container_name} @ {container_cgroup_path}")

    memstat_path = os.path.join(container_cgroup_path, "memory.stat")
    memstat_raw = ""
    memstat = {}
    try:
        with open(memstat_path, "r") as f:
            memstat_raw = f.read().strip()
        memstat = parse_kv_pairs(memstat_raw)
    except (IOError, PermissionError) as e:
        print(f"      [WARN] 无法读取 memory.stat: {e}", file=sys.stderr)

    usage_bytes = 0
    try:
        with open(os.path.join(container_cgroup_path, "memory.usage_in_bytes"), "r") as f:
            usage_bytes = int(f.read().strip())
    except (IOError, PermissionError):
        pass

    rss = memstat.get("total_rss", memstat.get("rss", 0))

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
        "active_anon_bytes": memstat.get("active_anon", 0),
        "inactive_anon_bytes": memstat.get("inactive_anon", 0),
        "active_file_bytes": memstat.get("active_file", 0),
        "inactive_file_bytes": memstat.get("inactive_file", 0),
        "cgroup_memory_stat_raw": memstat_raw,
        "processes": processes,
    }


def discover_pods_via_cgroup(pod_meta_map):
    """通过 cgroup 发现 Pod 并匹配 K8s 元数据。"""
    pods = []
    if not os.path.isdir(KUBEPODS_CGROUP):
        print(f"  [WARN] cgroup 根目录不存在: {KUBEPODS_CGROUP}", file=sys.stderr)
        return pods

    for entry in sorted(os.listdir(KUBEPODS_CGROUP)):
        entry_path = os.path.join(KUBEPODS_CGROUP, entry)
        if not os.path.isdir(entry_path):
            continue

        pod_dirs = []
        if entry.startswith("pod"):
            pod_dirs.append(entry_path)
        elif entry in ("burstable", "guaranteed", "besteffort"):
            for sub_entry in sorted(os.listdir(entry_path)):
                sub_path = os.path.join(entry_path, sub_entry)
                if os.path.isdir(sub_path) and sub_entry.startswith("pod"):
                    pod_dirs.append(sub_path)

        for pod_dir in pod_dirs:
            pod_data = collect_pod_from_cgroup(pod_dir, pod_meta_map)
            if pod_data:
                pods.append(pod_data)

    return pods


def collect_pod_from_cgroup(pod_cgroup_path, pod_meta_map):
    """从 pod cgroup 目录采集数据并与 K8s 元数据匹配。"""
    cgroup_basename = os.path.basename(pod_cgroup_path)
    pod_uid = cgroup_basename.replace("pod", "")

    # 匹配 K8s 元数据
    meta = pod_meta_map.get(pod_uid)
    if not meta:
        # 尝试前缀模糊匹配
        for uid, m in pod_meta_map.items():
            if pod_uid.startswith(uid[:8]) or uid.startswith(pod_uid[:8]):
                meta = m
                break

    if meta:
        pod_name = meta["name"]
        pod_namespace = meta["namespace"]
        containers_meta = meta.get("containers", {})
        requests_bytes = meta.get("total_requests_memory_bytes", 0)
        limits_bytes = meta.get("total_limits_memory_bytes", 0)
    else:
        pod_name = f"unknown-{cgroup_basename[:12]}"
        pod_namespace = "unknown"
        containers_meta = {}
        requests_bytes = 0
        limits_bytes = 0

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

    working_set = 0
    try:
        with open(os.path.join(pod_cgroup_path, "memory.usage_in_bytes"), "r") as f:
            working_set = int(f.read().strip())
    except (IOError, PermissionError):
        pass

    rss = memstat.get("total_rss", memstat.get("rss", 0))

    # 发现容器 cgroup 目录
    container_dirs = []
    for entry in sorted(os.listdir(pod_cgroup_path)):
        entry_path = os.path.join(pod_cgroup_path, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "memory.stat")):
            container_dirs.append(entry_path)

    # 采集容器（附带领 K8s 配额）
    containers = []
    for cg_path in container_dirs:
        cg_name = os.path.basename(cg_path)
        container_name = cg_name
        req_bytes = 0
        lim_bytes = 0

        for cname, cmeta in containers_meta.items():
            if cname in cg_name or cg_name in cname:
                container_name = cname
                req_bytes = cmeta["requests_memory_bytes"]
                lim_bytes = cmeta["limits_memory_bytes"]
                break

        ctr_data = collect_container(cg_path, container_name, req_bytes, lim_bytes)
        containers.append(ctr_data)

    return {
        "uid": pod_uid,
        "name": pod_name,
        "namespace": pod_namespace,
        "requests_memory_bytes": requests_bytes,
        "limits_memory_bytes": limits_bytes,
        "working_set_bytes": working_set,
        "rss_bytes": rss,
        "active_anon_bytes": memstat.get("active_anon", 0),
        "inactive_anon_bytes": memstat.get("inactive_anon", 0),
        "active_file_bytes": memstat.get("active_file", 0),
        "inactive_file_bytes": memstat.get("inactive_file", 0),
        "cgroup_memory_stat_raw": memstat_raw,
        "containers": containers,
    }


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="Data Node Collector — 采集本节点内存数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--node-name", required=True,
                        help="K8s 节点名（必填）")
    parser.add_argument("--pods-info", required=True,
                        help="pods_info.json 文件路径（由 mgmt_collector 生成）")
    parser.add_argument("-o", "--output", default=None,
                        help="输出文件（默认 raw_<node_name>.json）")
    args = parser.parse_args()

    node_name = args.node_name
    tz = datetime.now(timezone(timedelta(hours=8))).astimezone().tzinfo
    collection_time = datetime.now(tz).isoformat()

    print("╔══════════════════════════════════════════════╗")
    print("║  Node Collector v2.0                          ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Node:   {node_name}".ljust(49) + "║")
    print(f"║  Time:   {collection_time}".ljust(49) + "║")
    print("╚══════════════════════════════════════════════╝")
    print()

    # ── Step 1: 加载本节点 Pod 元数据 ──
    print("[1/4] 加载 Pod 元数据 ...")
    pod_meta_map = load_pods_for_node(args.pods_info, node_name)

    # ── Step 2: 采集节点级内存 ──
    print("[2/4] 采集节点内存 ...")
    meminfo_data = collect_meminfo()
    node_ip = get_local_ip()

    # ── Step 3: 发现并采集 Pod ──
    print("[3/4] 发现并采集 Pod ...")
    pods = discover_pods_via_cgroup(pod_meta_map)
    print(f"      cgroup 中发现 {len(pods)} 个 Pod")

    # ── Step 4: 输出 ──
    print("[4/4] 生成输出文件 ...")
    raw_data = {
        "collection_metadata": {
            "collector": "node_collector",
            "version": "2.0.0",
            "collection_time": collection_time,
            "node_name": node_name,
            "node_ip": node_ip,
        },
        "node": {
            "name": node_name,
            "ip": node_ip,
            "meminfo": meminfo_data["parsed"],
            "meminfo_raw": meminfo_data["raw"],
        },
        "pods": pods,
    }

    output_file = args.output or f"raw_{node_name}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2, default=str)

    total_containers = sum(len(p.get("containers", [])) for p in pods)
    total_processes = sum(
        len(c.get("processes", [])) for p in pods for c in p.get("containers", [])
    )

    print()
    print("══════════════════════════════════════════════")
    print("  采集完成!")
    print(f"  节点:   {node_name} ({node_ip})")
    print(f"  Pods:   {len(pods)}")
    print(f"  容器:   {total_containers}")
    print(f"  进程:   {total_processes}")
    print(f"  输出:   {output_file}")
    print("══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
