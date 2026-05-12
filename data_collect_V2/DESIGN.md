# Data Collect V2 — 设计文档

## 1. 设计目标

将 V1 的单脚本 collector.py 拆分为三个独立脚本，职责分离，适应不同部署场景：

| 脚本 | 运行位置 | 权限要求 | 职责 |
|------|----------|----------|------|
| `mgmt_collector.py` | 管理节点（有 kubectl 权限的任意机器） | 无 | 采集 K8s 集群元数据：Node 列表 + Pod 列表 |
| `node_collector.py` | 每个数据节点 | root | 采集本节点内存数据：meminfo + cgroup + 进程 |
| `aggregator.py` | 管理节点或汇总机 | 无 | 将管理面元数据 + 各节点内存数据合并为最终 JSON |

## 2. 架构与数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│ Step 1: mgmt_collector.py (管理节点，执行一次)                         │
│                                                                     │
│  kubectl get nodes -o json  ──┬──>  nodes_info.json                 │
│                               │     (集群所有节点列表)                 │
│  kubectl get pods             │                                     │
│    --all-namespaces -o json ──┴──>  pods_info.json                  │
│                                     (集群所有 Pod 元数据)              │
└─────────────────────────────────────────────────────────────────────┘
        │                              │
        │ nodes_info.json              │ pods_info.json
        ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 2: node_collector.py (每个数据节点执行一次，可并行)                 │
│                                                                     │
│  输入: --node-name <name> --pods-info pods_info.json                │
│                                                                     │
│  /proc/meminfo                 ──┬──>  node_mem                     │
│  /sys/fs/cgroup/memory/kubepods ──┤    pod_cgroup_stats             │
│  /proc/<pid>/status             ──┤    process_mem                  │
│  /proc/<pid>/smaps_rollup       ──┤                                 │
│  pods_info.json (按 node 过滤)   ──┘    K8s metadata match          │
│                                                                     │
│  输出: raw_<node_name>.json (单节点完整内存数据)                       │
└─────────────────────────────────────────────────────────────────────┘
        │                              │
        │ nodes_info.json              │ raw_*.json (每节点一份)
        ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 3: aggregator.py (管理节点，执行一次)                             │
│                                                                     │
│  输入:                                                              │
│    --nodes-info nodes_info.json                                     │
│    --pods-info pods_info.json                                       │
│    --raw-files raw_node1.json raw_node2.json ...                    │
│    --ne-id <id> --ne-name <name> --ne-type <type>                   │
│                                                                     │
│  处理:                                                              │
│    1. 按 NE 分组（支持单 NE / 多 NE 配置）                             │
│    2. 为每层分配顺序 ID                                               │
│    3. 计算 mem_used / mem_hot / mem_cold                            │
│    4. 构造 original_info                                             │
│                                                                     │
│  输出: memory_data.json                                             │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. 文件规格

### 3.1 nodes_info.json（mgmt_collector 输出）

```json
{
  "collection_metadata": {
    "collector": "mgmt_collector",
    "version": "2.0.0",
    "collection_time": "2026-05-12T10:00:00+08:00",
    "total_nodes": 3
  },
  "nodes": [
    {
      "name": "k8s-worker-01",
      "ip": "192.168.1.10",
      "capacity_memory_bytes": 67108864000,
      "allocatable_memory_bytes": 65000000000,
      "labels": { "node-role.kubernetes.io/worker": "" },
      "conditions": { "Ready": "True", "MemoryPressure": "False" }
    }
  ]
}
```

### 3.2 pods_info.json（mgmt_collector 输出）

```json
{
  "collection_metadata": {
    "collector": "mgmt_collector",
    "version": "2.0.0",
    "collection_time": "2026-05-12T10:00:05+08:00",
    "total_pods": 45
  },
  "pods": [
    {
      "name": "nginx-proxy-7d4b9c",
      "namespace": "prod",
      "uid": "a1b2c3d4-...",
      "node_name": "k8s-worker-01",
      "containers": [
        {
          "name": "nginx",
          "requests_memory_bytes": 67108864,
          "limits_memory_bytes": 134217728
        }
      ],
      "total_requests_memory_bytes": 134217728,
      "total_limits_memory_bytes": 268435456
    }
  ]
}
```

### 3.3 raw_<node_name>.json（node_collector 输出）

```json
{
  "collection_metadata": {
    "collector": "node_collector",
    "version": "2.0.0",
    "collection_time": "2026-05-12T10:01:00+08:00",
    "node_name": "k8s-worker-01",
    "node_ip": "192.168.1.10"
  },
  "node": {
    "name": "k8s-worker-01",
    "ip": "192.168.1.10",
    "meminfo": { "MemTotal": 65536000, "MemAvailable": 22020096, ... },
    "meminfo_raw": "MemTotal: ..."
  },
  "pods": [
    {
      "uid": "a1b2c3d4-...",
      "name": "nginx-proxy-7d4b9c",
      "namespace": "prod",
      "requests_memory_bytes": 134217728,
      "limits_memory_bytes": 268435456,
      "working_set_bytes": 198967296,
      "rss_bytes": 183500800,
      "active_anon_bytes": 104857600,
      "inactive_anon_bytes": 47185920,
      "active_file_bytes": 62914560,
      "inactive_file_bytes": 31457280,
      "cgroup_memory_stat_raw": "cache 8388608\nrss 183500800\n...",
      "containers": [
        {
          "name": "nginx",
          "requests_memory_bytes": 67108864,
          "limits_memory_bytes": 134217728,
          "rss_bytes": 104857600,
          "usage_bytes": 120259584,
          "active_anon_bytes": 52428800,
          "inactive_anon_bytes": 20971520,
          "active_file_bytes": 31457280,
          "inactive_file_bytes": 15728640,
          "cgroup_memory_stat_raw": "cache 4194304\nrss 104857600\n...",
          "processes": [
            {
              "pid": 1234,
              "name": "nginx: master",
              "vm_rss_kb": 51200,
              "vm_hwm_kb": 56320,
              "rss_anon_kb": 30720,
              "rss_file_kb": 18432,
              "rss_shmem_kb": 2048,
              "vm_pss_kb": 42496,
              "vm_uss_kb": 40960,
              "vm_swap_kb": 0,
              "raw_status": "Name: nginx\nPid: 1234\n...",
              "raw_smaps": "Rss: 51200 kB\n..."
            }
          ]
        }
      ]
    }
  ]
}
```

### 3.4 memory_data.json（aggregator 输出）

与 V1 完全一致的格式，确保前端 `memory_report.html` 无需任何修改即可使用。

## 4. 脚本参数设计

### 4.1 mgmt_collector.py

```
usage: mgmt_collector.py [-h] [--kubeconfig KUBECONFIG] [-o OUTPUT_DIR]

options:
  --kubeconfig     kubeconfig 路径（默认从环境变量读取）
  -o, --output-dir 输出目录（默认 .）
```

输出文件:
- `<output_dir>/nodes_info.json`
- `<output_dir>/pods_info.json`

### 4.2 node_collector.py

```
usage: node_collector.py [-h] --node-name NODE_NAME --pods-info PODS_INFO
                         [-o OUTPUT]

options:
  --node-name       K8s 节点名（必填）
  --pods-info       pods_info.json 路径（由 mgmt_collector 生成）
  -o, --output      输出文件路径（默认 raw_<node_name>.json）
```

### 4.3 aggregator.py

```
usage: aggregator.py [-h] --nodes-info NODES_INFO --pods-info PODS_INFO
                     --ne-id NE_ID --ne-name NE_NAME
                     [--ne-type NE_TYPE] [-o OUTPUT]
                     --raw-files RAW_FILES [RAW_FILES ...]

options:
  --nodes-info      nodes_info.json 路径
  --pods-info       pods_info.json 路径
  --ne-id           网元 ID
  --ne-name         网元名称
  --ne-type         网元类型（默认 "Network Element"）
  -o, --output      输出文件（默认 memory_data.json）
  --raw-files       各节点 raw_*.json 文件列表（支持 glob）
```

## 5. 部署流程

```bash
# === Step 1: 管理节点 ===
python3 mgmt_collector.py --kubeconfig ~/.kube/config -o ./data/
# 输出: ./data/nodes_info.json, ./data/pods_info.json

# === Step 2: 每个数据节点（可并行） ===
# 先将 pods_info.json 分发到各节点
scp ./data/pods_info.json k8s-worker-01:/tmp/

# SSH 到节点执行
ssh k8s-worker-01 "sudo python3 node_collector.py \
  --node-name k8s-worker-01 \
  --pods-info /tmp/pods_info.json \
  -o /tmp/raw_k8s-worker-01.json"

# 回收采集结果
scp k8s-worker-01:/tmp/raw_k8s-worker-01.json ./data/

# === Step 3: 汇总 ===
python3 aggregator.py \
  --nodes-info ./data/nodes_info.json \
  --pods-info ./data/pods_info.json \
  --ne-id ne-001 \
  --ne-name "NE-Core-01" \
  --ne-type "Core Network Element" \
  --raw-files ./data/raw_*.json \
  -o memory_data.json
```

## 6. 与 V1 的关键差异

| 项目 | V1 (collector.py + processor.py) | V2 |
|------|------|-----|
| 脚本数量 | 2 | 3 |
| kubectl 调用 | collector 内嵌 | 独立 mgmt_collector |
| kubeconfig 管理 | collector 需处理 sudo 场景 | mgmt_collector 不需要 root |
| nodes_info 来源 | 从 raw JSON 的 metadata 推断 | 独立 kubectl 查询 |
| pods 元数据 | kubectl field-selector 按节点查询 | 全量查询后按节点过滤 |
| 并行度 | 串行 | mgmt 1次 + node N次（可并行） |
| 节点名检测 | 复杂自动检测逻辑 | 外部显式传入 --node-name |
| NE 分组 | processor 按 ne_id 自动分组 | aggregator 按 --ne-id 明确分组 |

## 7. 错误处理

- mgmt_collector: kubectl 不可用 → exit(1)，打印诊断
- node_collector: 无 root 权限 → WARN，跳过需 root 的文件，继续采集
- node_collector: pods_info.json 不可用 → WARN，K8s 元数据留空（unknown）
- aggregator: raw 文件缺失某节点 → WARN，跳过该节点
- aggregator: nodes_info 与 raw_files 不匹配 → WARN，输出统计差异
