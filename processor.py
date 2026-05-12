#!/usr/bin/env python3
"""
K8s Memory Data Processor
==========================
将 collector.py 采集的原始 JSON 文件加工为 memory_data.json。

运行方式：
  # 处理单个节点采集文件
  python3 processor.py -i raw_ne-001.json -o memory_data.json

  # 处理多个节点采集文件（自动按 ne_id 分组）
  python3 processor.py -i raw_node1.json raw_node2.json raw_node3.json -o memory_data.json

  # 从目录批量读取
  python3 processor.py -i raw_data/*.json -o memory_data.json

加工内容：
  - 按 ne_id 分组节点 → 网元
  - 从 /proc/meminfo 计算 mem_used / mem_hot / mem_cold
  - 为每个层级分配顺序 ID
  - 为每个层级构造 original_info（保存原始采集文本）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from glob import glob


# ── ID Generators ──

def make_id_counter():
    """返回一个闭包计数器，每次调用返回递增的格式化 ID。"""
    counters = {}

    def counter(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"

    return counter


next_id = make_id_counter()


# ── Meminfo Transform ──

def transform_node_meminfo(meminfo):
    """将 /proc/meminfo 解析 dict 转换为节点级内存指标（单位 KB）。"""
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
    used = total - available

    # hot = active pages; cold = inactive pages
    active_anon = kb("Active(anon)")
    inactive_anon = kb("Inactive(anon)")
    active_file = kb("Active(file)")
    inactive_file = kb("Inactive(file)")

    # 优先使用 Active/Inactive 总计，其次用 anon+file 之和
    hot = kb("Active") or (active_anon + active_file)
    cold = kb("Inactive") or (inactive_anon + inactive_file)

    return {
        "mem_total_kb": total,
        "mem_available_kb": available,
        "mem_used_kb": used,
        "mem_hot_kb": hot,
        "mem_cold_kb": cold,
    }


# ── original_info Builders ──

def build_ne_original_info(meta, nodes):
    """构造 NE 级别的 original_info。"""
    lines = [
        f"# NE Collection Summary",
        f"collector_version: {meta.get('collector_version', 'unknown')}",
        f"collection_time: {meta.get('collection_time', 'unknown')}",
        f"target: {meta.get('ne_name', '?')} ({meta.get('ne_type', '?')})",
        f"ne_id: {meta.get('ne_id', '?')}",
        f"total_nodes: {len(nodes)}",
    ]
    return "\n".join(lines)


def build_node_original_info(meminfo_raw):
    """构造 Node 级别的 original_info（meminfo 原文）。"""
    if meminfo_raw:
        return f"# /proc/meminfo\n{meminfo_raw}"
    return "# /proc/meminfo (no data)"


def build_pod_original_info(cgroup_stat_raw, requests, limits):
    """构造 Pod 级别的 original_info。"""
    lines = [cgroup_stat_raw.strip()] if cgroup_stat_raw else ["# cgroup memory.stat (no data)"]
    lines.append(f"# requests_memory_bytes: {requests}")
    lines.append(f"# limits_memory_bytes: {limits}")
    return "\n".join(lines)


def build_container_original_info(cgroup_stat_raw, requests, limits):
    """构造 Container 级别的 original_info。"""
    lines = [cgroup_stat_raw.strip()] if cgroup_stat_raw else ["# container cgroup memory.stat (no data)"]
    lines.append(f"# requests_memory_bytes: {requests}")
    lines.append(f"# limits_memory_bytes: {limits}")
    return "\n".join(lines)


def build_process_original_info(raw_status, raw_smaps):
    """构造 Process 级别的 original_info。"""
    parts = []
    if raw_status:
        parts.append(raw_status.strip())
    if raw_smaps:
        parts.append(raw_smaps.strip())
    return "\n".join(parts) if parts else "# /proc/pid/status (no data)"


# ── Transform Pipeline ──

def transform_process(raw_proc):
    """将 collector 的进程原始数据转换为目标格式。"""
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
            raw_proc.get("raw_smaps", "")
        ),
    }


def transform_container(raw_ctr):
    """将 collector 的容器原始数据转换为目标格式。"""
    processes = [transform_process(p) for p in raw_ctr.get("processes", [])]

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
        "processes": processes,
    }


def transform_pod(raw_pod):
    """将 collector 的 Pod 原始数据转换为目标格式。"""
    containers = [transform_container(c) for c in raw_pod.get("containers", [])]

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
        "containers": containers,
    }


def transform_node(raw_doc):
    """将 collector 的单个节点原始文档转换为目标 node 格式。"""
    node_data = raw_doc.get("node", {})
    meminfo = node_data.get("meminfo", {})
    meminfo_raw = node_data.get("meminfo_raw", "")

    node_metrics = transform_node_meminfo(meminfo)
    pods = [transform_pod(p) for p in raw_doc.get("pods", [])]

    return {
        "id": next_id("node"),
        "name": node_data.get("hostname", raw_doc.get("collection_metadata", {}).get("hostname", "unknown")),
        "ip": node_data.get("ip", "unknown"),
        "mem_total_kb": node_metrics["mem_total_kb"],
        "mem_available_kb": node_metrics["mem_available_kb"],
        "mem_used_kb": node_metrics["mem_used_kb"],
        "mem_hot_kb": node_metrics["mem_hot_kb"],
        "mem_cold_kb": node_metrics["mem_cold_kb"],
        "original_info": build_node_original_info(meminfo_raw),
        "pods": pods,
    }


# ── Aggregation ──

def aggregate_by_ne(raw_docs):
    """将原始文档按 ne_id 分组，构造 network_elements 列表。"""
    ne_groups = {}

    for doc in raw_docs:
        meta = doc.get("collection_metadata", {})
        ne_id = meta.get("ne_id", "unknown")
        ne_name = meta.get("ne_name", ne_id)
        ne_type = meta.get("ne_type", "Network Element")

        if ne_id not in ne_groups:
            ne_groups[ne_id] = {
                "name": ne_name,
                "type": ne_type,
                "meta": meta,
                "docs": [],
            }
        # 保留每个 NE 第一条记录的元数据
        if ne_groups[ne_id]["meta"] == meta:
            pass
        ne_groups[ne_id]["docs"].append(doc)

    network_elements = []
    for ne_id, group in ne_groups.items():
        nodes = [transform_node(doc) for doc in group["docs"]]

        ne_entry = {
            "id": next_id("ne"),
            "name": group["name"],
            "type": group["type"],
            "original_info": build_ne_original_info(group["meta"], nodes),
            "nodes": nodes,
        }
        network_elements.append(ne_entry)

    return network_elements


# ── Main ──

def load_raw_files(paths):
    """加载所有原始 JSON 文件，返回文档列表。"""
    docs = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            docs.append(doc)
            print(f"  [OK] 加载: {path}")
        except (IOError, json.JSONDecodeError) as e:
            print(f"  [WARN] 跳过 {path}: {e}", file=sys.stderr)
    return docs


def main():
    parser = argparse.ArgumentParser(
        description="K8s Memory Data Processor — 将 collector 原始数据加工为 memory_data.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-i", "--input", nargs="+", required=True,
        help="原始 JSON 文件路径（支持 glob 通配符）",
    )
    parser.add_argument(
        "-o", "--output", default="memory_data.json",
        help="输出文件路径（默认: memory_data.json）",
    )
    args = parser.parse_args()

    # 展开 glob 通配符（Windows 兼容）
    raw_paths = []
    for pattern in args.input:
        matches = glob(pattern)
        if matches:
            raw_paths.extend(matches)
        else:
            raw_paths.append(pattern)

    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  K8s Memory Processor v1.0                    ║")
    print(f"╚══════════════════════════════════════════════╝")
    print()

    # 加载
    print(f"[1/3] 加载原始数据文件 ({len(raw_paths)} 个) ...")
    raw_docs = load_raw_files(raw_paths)
    if not raw_docs:
        print("[ERROR] 没有有效的输入文件", file=sys.stderr)
        sys.exit(1)

    # 加工
    print(f"[2/3] 加工数据 ...")
    network_elements = aggregate_by_ne(raw_docs)

    # 输出
    print(f"[3/3] 生成 memory_data.json ...")
    output = {"network_elements": network_elements}

    tz = datetime.now(timezone(timedelta(hours=8))).astimezone().tzinfo
    output["_metadata"] = {
        "processor_version": "1.0.0",
        "processed_at": datetime.now(tz).isoformat(),
        "source_files": len(raw_docs),
        "network_elements": len(network_elements),
        "total_nodes": sum(len(ne["nodes"]) for ne in network_elements),
        "total_pods": sum(len(n["pods"]) for ne in network_elements for n in ne["nodes"]),
        "total_containers": sum(
            len(p["containers"])
            for ne in network_elements for n in ne["nodes"] for p in n["pods"]
        ),
        "total_processes": sum(
            len(c["processes"])
            for ne in network_elements for n in ne["nodes"]
            for p in n["pods"] for c in p["containers"]
        ),
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print()
    print(f"══════════════════════════════════════════════")
    print(f"  处理完成!")
    print(f"  网元:   {output['_metadata']['network_elements']}")
    print(f"  节点:   {output['_metadata']['total_nodes']}")
    print(f"  Pods:   {output['_metadata']['total_pods']}")
    print(f"  容器:   {output['_metadata']['total_containers']}")
    print(f"  进程:   {output['_metadata']['total_processes']}")
    print(f"  输出:   {args.output}")
    print(f"══════════════════════════════════════════════")


if __name__ == "__main__":
    main()
