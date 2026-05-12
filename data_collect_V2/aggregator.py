#!/usr/bin/env python3
"""
Data Aggregator (aggregator)
==============================
将管理面元数据 + 各节点内存数据合并为最终 memory_data.json。

运行方式：
  python3 aggregator.py \
    --nodes-info nodes_info.json \
    --pods-info pods_info.json \
    --ne-id ne-001 --ne-name "NE-Core-01" --ne-type "Core Network Element" \
    --raw-files raw_k8s-worker-01.json raw_k8s-worker-02.json \
    -o memory_data.json

输入：
  - nodes_info.json  （mgmt_collector 输出）
  - pods_info.json   （mgmt_collector 输出）
  - raw_*.json       （node_collector 输出，每节点一份）

输出：
  - memory_data.json （与 V1 前端完全兼容）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from glob import glob


# ── ID Generator ──

def make_id_counter():
    counters = {}
    def counter(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"
    return counter

next_id = make_id_counter()


# ── Meminfo Transform ──

def compute_node_metrics(meminfo):
    """从 /proc/meminfo 计算节点级内存指标（KB）。"""
    def kb(key):
        val = meminfo.get(key, 0)
        if isinstance(val, str):
            try:
                return int(val.split()[0])
            except (ValueError, IndexError):
                return 0
        return int(val) if val else 0

    total = kb("MemTotal")
    available = kb("MemAvailable")
    hot = kb("Active") or (kb("Active(anon)") + kb("Active(file)"))
    cold = kb("Inactive") or (kb("Inactive(anon)") + kb("Inactive(file)"))

    return {
        "mem_total_kb": total,
        "mem_available_kb": available,
        "mem_used_kb": total - available,
        "mem_hot_kb": hot,
        "mem_cold_kb": cold,
    }


# ── original_info Builders ──

def build_ne_original_info(meta, nodes):
    return "\n".join([
        "# NE Collection Summary",
        f"aggregator_version: 2.0.0",
        f"collection_time: {meta.get('collection_time', 'unknown')}",
        f"target: {meta.get('ne_name', '?')} ({meta.get('ne_type', '?')})",
        f"total_nodes: {len(nodes)}",
    ])


def build_node_original_info(meminfo_raw, k8s_node_info):
    lines = [f"# /proc/meminfo\n{meminfo_raw}" if meminfo_raw else "# /proc/meminfo (no data)"]
    if k8s_node_info:
        lines.append(f"# K8s capacity: {k8s_node_info.get('capacity_memory_bytes', '?')} bytes")
        lines.append(f"# K8s allocatable: {k8s_node_info.get('allocatable_memory_bytes', '?')} bytes")
    return "\n".join(lines)


def build_pod_original_info(cgroup_stat_raw, requests, limits):
    lines = [cgroup_stat_raw.strip()] if cgroup_stat_raw else ["# cgroup memory.stat (no data)"]
    lines.append(f"# requests_memory_bytes: {requests}")
    lines.append(f"# limits_memory_bytes: {limits}")
    return "\n".join(lines)


def build_container_original_info(cgroup_stat_raw, requests, limits):
    lines = [cgroup_stat_raw.strip()] if cgroup_stat_raw else ["# container cgroup memory.stat (no data)"]
    lines.append(f"# requests_memory_bytes: {requests}")
    lines.append(f"# limits_memory_bytes: {limits}")
    return "\n".join(lines)


def build_process_original_info(raw_status, raw_smaps):
    parts = []
    if raw_status:
        parts.append(raw_status.strip())
    if raw_smaps:
        parts.append(raw_smaps.strip())
    return "\n".join(parts) if parts else "# /proc/pid/status (no data)"


# ── Transform Pipeline ──

def transform_process(raw_proc):
    return {
        "id": next_id("proc"),
        "pid": raw_proc.get("pid", 0),
        "name": raw_proc.get("name", "unknown"),
        "vm_rss_kb": raw_proc.get("vm_rss_kb", 0),
        "vm_hwm_kb": raw_proc.get("vm_hwm_kb", 0),
        "rss_anon_kb": raw_proc.get("rss_anon_kb", 0),
        "rss_file_kb": raw_proc.get("rss_file_kb", 0),
        "rss_shmem_kb": raw_proc.get("rss_shmem_kb", 0),
        "vm_pss_kb": raw_proc.get("vm_pss_kb", 0),
        "vm_uss_kb": raw_proc.get("vm_uss_kb", 0),
        "vm_swap_kb": raw_proc.get("vm_swap_kb", 0),
        "original_info": build_process_original_info(
            raw_proc.get("raw_status", ""),
            raw_proc.get("raw_smaps", ""),
        ),
    }


def transform_container(raw_ctr):
    return {
        "id": next_id("ctr"),
        "name": raw_ctr.get("name", "unknown"),
        "requests_memory_bytes": raw_ctr.get("requests_memory_bytes", 0),
        "limits_memory_bytes": raw_ctr.get("limits_memory_bytes", 0),
        "rss_bytes": raw_ctr.get("rss_bytes", 0),
        "usage_bytes": raw_ctr.get("usage_bytes", 0),
        "active_anon_bytes": raw_ctr.get("active_anon_bytes", 0),
        "inactive_anon_bytes": raw_ctr.get("inactive_anon_bytes", 0),
        "active_file_bytes": raw_ctr.get("active_file_bytes", 0),
        "inactive_file_bytes": raw_ctr.get("inactive_file_bytes", 0),
        "original_info": build_container_original_info(
            raw_ctr.get("cgroup_memory_stat_raw", ""),
            raw_ctr.get("requests_memory_bytes", 0),
            raw_ctr.get("limits_memory_bytes", 0),
        ),
        "processes": [transform_process(p) for p in raw_ctr.get("processes", [])],
    }


def transform_pod(raw_pod):
    return {
        "id": next_id("pod"),
        "name": raw_pod.get("name", "unknown"),
        "namespace": raw_pod.get("namespace", "unknown"),
        "requests_memory_bytes": raw_pod.get("requests_memory_bytes", 0),
        "limits_memory_bytes": raw_pod.get("limits_memory_bytes", 0),
        "working_set_bytes": raw_pod.get("working_set_bytes", 0),
        "rss_bytes": raw_pod.get("rss_bytes", 0),
        "active_anon_bytes": raw_pod.get("active_anon_bytes", 0),
        "inactive_anon_bytes": raw_pod.get("inactive_anon_bytes", 0),
        "active_file_bytes": raw_pod.get("active_file_bytes", 0),
        "inactive_file_bytes": raw_pod.get("inactive_file_bytes", 0),
        "original_info": build_pod_original_info(
            raw_pod.get("cgroup_memory_stat_raw", ""),
            raw_pod.get("requests_memory_bytes", 0),
            raw_pod.get("limits_memory_bytes", 0),
        ),
        "containers": [transform_container(c) for c in raw_pod.get("containers", [])],
    }


def transform_node(raw_doc, k8s_node_info):
    node_data = raw_doc.get("node", {})
    meta = raw_doc.get("collection_metadata", {})
    meminfo = node_data.get("meminfo", {})

    metrics = compute_node_metrics(meminfo)

    return {
        "id": next_id("node"),
        "name": meta.get("node_name", node_data.get("name", "unknown")),
        "ip": node_data.get("ip", meta.get("node_ip", "unknown")),
        "mem_total_kb": metrics["mem_total_kb"],
        "mem_available_kb": metrics["mem_available_kb"],
        "mem_used_kb": metrics["mem_used_kb"],
        "mem_hot_kb": metrics["mem_hot_kb"],
        "mem_cold_kb": metrics["mem_cold_kb"],
        "original_info": build_node_original_info(
            node_data.get("meminfo_raw", ""),
            k8s_node_info,
        ),
        "pods": [transform_pod(p) for p in raw_doc.get("pods", [])],
    }


# ── Main ──

def load_json(path):
    """加载 JSON 文件。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"  [ERROR] 无法读取 {path}: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Data Aggregator — 汇总管理面元数据 + 节点内存数据 → memory_data.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nodes-info", required=True,
                        help="nodes_info.json 路径（mgmt_collector 输出）")
    parser.add_argument("--pods-info", required=True,
                        help="pods_info.json 路径（mgmt_collector 输出）")
    parser.add_argument("--ne-id", required=True,
                        help="网元 ID，如 ne-001")
    parser.add_argument("--ne-name", required=True,
                        help="网元名称，如 NE-Core-01")
    parser.add_argument("--ne-type", default="Network Element",
                        help="网元类型")
    parser.add_argument("-o", "--output", default="memory_data.json",
                        help="输出文件（默认 memory_data.json）")
    parser.add_argument("--raw-files", nargs="+", required=True,
                        help="各节点 raw_*.json 文件列表（支持 glob）")
    args = parser.parse_args()

    # 展开 glob
    raw_paths = []
    for pattern in args.raw_files:
        matches = glob(pattern)
        raw_paths.extend(matches if matches else [pattern])

    print("╔══════════════════════════════════════════════╗")
    print("║  Data Aggregator v2.0                         ║")
    print("╚══════════════════════════════════════════════╝")
    print()
    print(f"  NE:       {args.ne_id} ({args.ne_name})")
    print(f"  类型:     {args.ne_type}")
    print(f"  待处理:   {len(raw_paths)} 个节点数据文件")
    print()

    # ── Load metadata ──
    print("[1/4] 加载管理面元数据 ...")
    nodes_info_doc = load_json(args.nodes_info)
    pods_info_doc = load_json(args.pods_info)
    if not nodes_info_doc or not pods_info_doc:
        print("[ERROR] 管理面元数据加载失败", file=sys.stderr)
        sys.exit(1)

    # K8s node info index
    k8s_nodes = {n["name"]: n for n in nodes_info_doc.get("nodes", [])}
    print(f"      节点元数据: {len(k8s_nodes)} 个")

    # ── Load raw node data ──
    print("[2/4] 加载节点内存数据 ...")
    raw_docs = []
    for p in raw_paths:
        doc = load_json(p)
        if doc:
            raw_docs.append(doc)
            node_name = doc.get("collection_metadata", {}).get("node_name", p)
            print(f"      [OK] {node_name} ({len(doc.get('pods',[]))} Pods)")
    if not raw_docs:
        print("[ERROR] 没有有效的节点数据文件", file=sys.stderr)
        sys.exit(1)

    # ── Transform ──
    print("[3/4] 加工数据 ...")
    nodes = []
    for doc in raw_docs:
        node_name = doc.get("collection_metadata", {}).get("node_name",
                      doc.get("node", {}).get("name", "unknown"))
        k8s_info = k8s_nodes.get(node_name)
        if not k8s_info:
            print(f"  [WARN] 节点 {node_name} 在 nodes_info 中无匹配", file=sys.stderr)
        nodes.append(transform_node(doc, k8s_info))

    collection_meta = raw_docs[0].get("collection_metadata", {}) if raw_docs else {}
    ne = {
        "id": next_id("ne"),
        "name": args.ne_name,
        "type": args.ne_type,
        "original_info": build_ne_original_info({
            "collection_time": collection_meta.get("collection_time", ""),
            "ne_name": args.ne_name,
            "ne_type": args.ne_type,
        }, nodes),
        "nodes": nodes,
    }

    # ── Output ──
    print("[4/4] 生成 memory_data.json ...")
    tz = datetime.now(timezone(timedelta(hours=8))).astimezone().tzinfo
    output = {
        "_metadata": {
            "aggregator_version": "2.0.0",
            "processed_at": datetime.now(tz).isoformat(),
            "source_files": len(raw_docs),
            "network_elements": 1,
            "total_nodes": len(nodes),
            "total_pods": sum(len(n["pods"]) for n in nodes),
            "total_containers": sum(len(p["containers"]) for n in nodes for p in n["pods"]),
            "total_processes": sum(
                len(c["processes"]) for n in nodes for p in n["pods"] for c in p["containers"]
            ),
        },
        "network_elements": [ne],
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print()
    print("══════════════════════════════════════════════")
    print("  汇总完成!")
    print(f"  网元:   {output['_metadata']['network_elements']}")
    print(f"  节点:   {output['_metadata']['total_nodes']}")
    print(f"  Pods:   {output['_metadata']['total_pods']}")
    print(f"  容器:   {output['_metadata']['total_containers']}")
    print(f"  进程:   {output['_metadata']['total_processes']}")
    print(f"  输出:   {args.output}")
    print("══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
