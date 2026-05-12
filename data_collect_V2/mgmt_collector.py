#!/usr/bin/env python3
"""
Management Node Collector (mgmt_collector)
============================================
在管理节点上运行，采集 K8s 集群元数据：Node 列表 + Pod 列表。

运行方式：
  python3 mgmt_collector.py --kubeconfig ~/.kube/config -o ./data/

输出文件：
  - nodes_info.json  集群所有节点的 K8s 元数据
  - pods_info.json   集群所有 Pod 的 K8s 元数据（按节点过滤使用）

依赖：
  - Python 3.6+
  - kubectl（需在 PATH 中，有权限访问集群）
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta


# ── kubectl helper ──

def run_kubectl(args, kubeconfig=""):
    """运行 kubectl 命令，返回 stdout 字符串。"""
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += args
    cmd_str = " ".join(cmd)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  [ERROR] kubectl 命令失败，返回码={result.returncode}", file=sys.stderr)
            print(f"         命令: {cmd_str}", file=sys.stderr)
            print(f"         stderr: {result.stderr.strip()[:500]}", file=sys.stderr)
            return ""
        return result.stdout
    except FileNotFoundError:
        print(f"  [ERROR] kubectl 未安装或不在 PATH 中", file=sys.stderr)
        print(f"         命令: {cmd_str}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"  [ERROR] kubectl 命令超时 (60s)", file=sys.stderr)
        print(f"         命令: {cmd_str}", file=sys.stderr)
        return ""


# ── K8s memory string parser ──

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
    try:
        return int(mem_str)
    except ValueError:
        pass
    try:
        return int(float(mem_str))
    except ValueError:
        return 0


# ── Collectors ──

def collect_nodes(kubeconfig):
    """采集集群所有节点的 K8s 元数据。"""
    print("[1/2] 采集节点列表 ...")
    raw = run_kubectl(["get", "nodes", "-o", "json"], kubeconfig)
    if not raw:
        print("  [ERROR] 无法获取节点列表", file=sys.stderr)
        sys.exit(1)

    k8s_nodes = json.loads(raw).get("items", [])
    print(f"      发现 {len(k8s_nodes)} 个节点")

    nodes = []
    for n in k8s_nodes:
        meta = n["metadata"]
        status = n.get("status", {})
        addresses = {a["type"]: a["address"] for a in status.get("addresses", [])}

        # 提取 conditions
        conditions = {}
        for c in status.get("conditions", []):
            conditions[c["type"]] = c["status"]

        # 提取 capacity / allocatable
        capacity = n.get("status", {}).get("capacity", {})
        allocatable = n.get("status", {}).get("allocatable", {})

        nodes.append({
            "name": meta["name"],
            "ip": addresses.get("InternalIP", addresses.get("ExternalIP", "unknown")),
            "capacity_memory_bytes": parse_k8s_memory(capacity.get("memory", "0")),
            "allocatable_memory_bytes": parse_k8s_memory(allocatable.get("memory", "0")),
            "labels": meta.get("labels", {}),
            "conditions": conditions,
        })

    return nodes


def collect_pods(kubeconfig):
    """采集集群所有 Pod 的元数据。"""
    print("[2/2] 采集 Pod 列表 ...")
    raw = run_kubectl(["get", "pods", "--all-namespaces", "-o", "json"], kubeconfig)
    if not raw:
        print("  [ERROR] 无法获取 Pod 列表", file=sys.stderr)
        sys.exit(1)

    k8s_pods = json.loads(raw).get("items", [])
    print(f"      发现 {len(k8s_pods)} 个 Pod")

    pods = []
    for p in k8s_pods:
        meta = p["metadata"]
        spec = p.get("spec", {})

        containers_meta = {}
        for c in spec.get("containers", []):
            req = c.get("resources", {}).get("requests", {}).get("memory", "0")
            lim = c.get("resources", {}).get("limits", {}).get("memory", "0")
            containers_meta[c["name"]] = {
                "requests_memory_bytes": parse_k8s_memory(req),
                "limits_memory_bytes": parse_k8s_memory(lim),
            }

        pods.append({
            "name": meta["name"],
            "namespace": meta["namespace"],
            "uid": meta["uid"],
            "node_name": spec.get("nodeName", "unknown"),
            "containers": containers_meta,
            "total_requests_memory_bytes": sum(
                c["requests_memory_bytes"] for c in containers_meta.values()
            ),
            "total_limits_memory_bytes": sum(
                c["limits_memory_bytes"] for c in containers_meta.values()
            ),
        })

    return pods


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="Management Node Collector — 采集 K8s 集群元数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--kubeconfig", default=os.environ.get("KUBECONFIG", ""),
        help="kubeconfig 文件路径（默认读取 KUBECONFIG 环境变量）",
    )
    parser.add_argument(
        "-o", "--output-dir", default=".",
        help="输出目录（默认 .）",
    )
    args = parser.parse_args()

    tz = datetime.now(timezone(timedelta(hours=8))).astimezone().tzinfo
    collection_time = datetime.now(tz).isoformat()

    print("╔══════════════════════════════════════════════╗")
    print("║  Mgmt Collector v2.0                          ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    if args.kubeconfig:
        print(f"  [*] kubeconfig: {args.kubeconfig}")
    else:
        print(f"  [*] kubeconfig: 使用 kubectl 默认查找路径")

    # 采集
    nodes = collect_nodes(args.kubeconfig)
    pods = collect_pods(args.kubeconfig)

    # 写入 nodes_info.json
    os.makedirs(args.output_dir, exist_ok=True)
    nodes_file = os.path.join(args.output_dir, "nodes_info.json")
    nodes_output = {
        "collection_metadata": {
            "collector": "mgmt_collector",
            "version": "2.0.0",
            "collection_time": collection_time,
            "total_nodes": len(nodes),
        },
        "nodes": nodes,
    }
    with open(nodes_file, "w", encoding="utf-8") as f:
        json.dump(nodes_output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  [OK] nodes_info.json → {nodes_file}")

    # 写入 pods_info.json
    pods_file = os.path.join(args.output_dir, "pods_info.json")
    pods_output = {
        "collection_metadata": {
            "collector": "mgmt_collector",
            "version": "2.0.0",
            "collection_time": collection_time,
            "total_pods": len(pods),
        },
        "pods": pods,
    }
    with open(pods_file, "w", encoding="utf-8") as f:
        json.dump(pods_output, f, ensure_ascii=False, indent=2, default=str)
    print(f"  [OK] pods_info.json → {pods_file}")

    # 按节点统计
    node_pod_counts = {}
    for p in pods:
        n = p["node_name"]
        node_pod_counts[n] = node_pod_counts.get(n, 0) + 1

    print()
    print("══════════════════════════════════════════════")
    print("  采集完成!")
    print(f"  节点:   {len(nodes)}")
    print(f"  Pods:   {len(pods)}")
    print(f"  按节点分布:")
    for n, c in sorted(node_pod_counts.items()):
        print(f"    {n}: {c} Pods")
    print(f"  输出目录: {os.path.abspath(args.output_dir)}")
    print("══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
