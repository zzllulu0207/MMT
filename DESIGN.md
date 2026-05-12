# MemInsight — K8s 节点内存分析工具 设计文档

## 1. 系统概述

MemInsight 是一套面向 K8s 集群的内存分析工具链，提供从**数据采集 → 加工 → 可视化**的完整链路。系统由三个核心组件构成：

| 组件 | 文件 | 运行环境 | 职责 |
|------|------|----------|------|
| **Collector** | `collector.py` | K8s 节点（需 root） | 采集节点 /proc、cgroup 原始内存数据和 K8s 元数据 |
| **Processor** | `processor.py` | 任意 Python 环境 | 将多节点采集数据聚合为统一的结构化 JSON |
| **Report** | `memory_report.html` | 浏览器（本地文件或 HTTP 服务） | 交互式内存分析报告，支持中英双语 |

```
┌──────────────┐    raw_*.json    ┌──────────────┐   memory_data.json   ┌──────────────────┐
│  collector.py │  ──────────────> │ processor.py │  ──────────────────> │ memory_report.html│
│  (节点采集)    │                 │  (聚合加工)   │                      │  (可视化分析)      │
└──────────────┘                  └──────────────┘                      └──────────────────┘
       │                                                                        │
       ▼                                                                        ▼
  /proc/meminfo                                                         memory_flame.svg
  /sys/fs/cgroup/memory/kubepods                                        (外部 SVG 火焰图)
  /proc/<pid>/status
  kubectl API / pods_info.json
```

---

## 2. 数据模型

### 2.1 输出 JSON 结构（memory_data.json）

```json
{
  "_metadata": {                          // processor 加工元信息
    "processor_version": "1.0.0",
    "processed_at": "2026-05-12T...",
    "source_files": 3,
    "network_elements": 2,
    "total_nodes": 4,
    "total_pods": 8,
    "total_containers": 12,
    "total_processes": 24
  },
  "network_elements": [{                  // 网元列表（顶层）
    "id": "ne-001",                      // 全局顺序 ID
    "name": "NE-Core-01",                // 网元名称
    "type": "Core Network Element",      // 网元类型
    "original_info": "# NE Collection...", // 原始采集信息（每层均有）
    "nodes": [{                          // 节点列表（L1）
      "id": "node-001",
      "name": "worker-node-01",
      "ip": "192.168.1.10",
      "mem_total_kb": 65536000,          // 总物理内存
      "mem_available_kb": 22020096,      // 可用内存（MemAvailable）
      "mem_used_kb": 43515904,           // 已用 = total - available
      "mem_hot_kb": 28311552,            // 热内存 = Active
      "mem_cold_kb": 15204352,           // 冷内存 = Inactive
      "original_info": "# /proc/meminfo...",
      "pods": [{                         // Pod 列表（L2）
        "id": "pod-001",
        "name": "nginx-proxy-7d4b9c",
        "namespace": "prod",
        "requests_memory_bytes": 134217728,   // K8s resource requests
        "limits_memory_bytes": 268435456,     // K8s resource limits
        "working_set_bytes": 198967296,       // cgroup working set
        "rss_bytes": 183500800,               // cgroup RSS
        "active_anon_bytes": ...,             // 活跃匿名页
        "inactive_anon_bytes": ...,           // 非活跃匿名页
        "active_file_bytes": ...,             // 活跃文件页
        "inactive_file_bytes": ...,           // 非活跃文件页
        "original_info": "# cgroup memory.stat...",
        "containers": [{                 // 容器列表（L3）
          "id": "ctr-001",
          "name": "nginx",
          "requests_memory_bytes": 67108864,
          "limits_memory_bytes": 134217728,
          "rss_bytes": 104857600,
          "usage_bytes": 120259584,
          "active_anon_bytes": ...,
          "inactive_anon_bytes": ...,
          "active_file_bytes": ...,
          "inactive_file_bytes": ...,
          "original_info": "# container cgroup...",
          "processes": [{               // 进程列表（L4）
            "id": "proc-001",
            "pid": 1234,
            "name": "nginx: master",
            "vm_rss_kb": 51200,         // /proc/pid/status VmRSS
            "vm_hwm_kb": 56320,         // /proc/pid/status VmHWM
            "rss_anon_kb": 30720,       // 匿名页 RSS
            "rss_file_kb": 18432,       // 文件页 RSS
            "rss_shmem_kb": 2048,       // 共享内存 RSS
            "vm_pss_kb": 42496,         // PSS (smaps_rollup)
            "vm_uss_kb": 40960,         // USS
            "vm_swap_kb": 0,            // Swap 使用量
            "original_info": "# /proc/pid/status..."
          }]
        }]
      }]
    }]
  }]
}
```

### 2.2 ID 分配规则

Processor 使用全局递增计数器为所有实体分配顺序 ID：

| 实体 | 前缀 | 示例 |
|------|------|------|
| Network Element | `ne-` | `ne-001`, `ne-002` |
| Node | `node-` | `node-001`, `node-002` |
| Pod | `pod-` | `pod-001` |
| Container | `ctr-` | `ctr-001`, `ctr-002` |
| Process | `proc-` | `proc-001` |

### 2.3 original_info 机制

每一层级都保留 `original_info` 字段，存储采集到的原始文本内容。这确保了：
- **可追溯性**：分析异常值时可以回看原始系统输出
- **去耦合**：前端不依赖原始文件系统，所有信息封装在 JSON 中
- **可读性**：HTML 中以 scrollable textarea 呈现，用户可直接查看原始数据

---

## 3. Collector 设计

### 3.1 架构

```
collector.py
├── 常量定义 (CGROUP_MEMORY_ROOT, PROC_ROOT, KUBEPODS_CGROUP)
├── 辅助解析器
│   ├── parse_kv_file()       — 解析 key: value 文件 (/proc/meminfo, /proc/pid/status)
│   ├── parse_kv_pairs()      — 解析 key value 文件 (cgroup memory.stat)
│   ├── parse_k8s_memory()    — 解析 K8s 内存量字符串 (128Mi → bytes)
│   ├── run_kubectl()         — kubectl 子进程调用（带错误诊断）
│   └── get_local_ip()        — 获取节点 IP
├── 采集函数
│   ├── collect_meminfo()     — /proc/meminfo → {parsed, raw}
│   ├── discover_pods_via_cgroup()   — 遍历 cgroup 发现 Pod
│   ├── collect_pod_from_cgroup()    — 单个 Pod cgroup 统计
│   ├── discover_container_cgroups() — 发现容器 cgroup 子目录
│   ├── collect_container()   — 容器 cgroup + 进程数据
│   ├── find_container_pids() — 读取 cgroup.procs 获取 PID 列表
│   ├── collect_process_info() — /proc/pid/status + smaps_rollup
│   ├── enrich_pods_with_k8s_metadata() — K8s 元数据匹配
│   └── enrich_containers_from_cgroup() — 容器数据采集
└── main() — 5 步采集流程
```

### 3.2 5 步采集流程

```
[1/5] 采集节点级数据    — /proc/meminfo + IP 检测
[2/5] 发现 Pod cgroup   — 遍历 /sys/fs/cgroup/memory/kubepods
[3/5] 获取 K8s 元数据    — kubectl API（或外部 pods_info.json）
[4/5] 采集容器和进程数据  — cgroup memory.stat + /proc/pid/status
[5/5] 生成输出文件       — raw_<ne-id>.json
```

### 3.3 cgroup 路径遍历逻辑

```
/sys/fs/cgroup/memory/kubepods/
├── pod<uid1>/              ← 直接 Pod 目录
├── pod<uid2>/
├── burstable/
│   ├── pod<uid3>/          ← QoS 分类下的 Pod
│   └── pod<uid4>/
├── guaranteed/
│   └── pod<uid5>/
└── besteffort/
    └── pod<uid6>/
```

- 一级条目以 `pod` 开头 → 直接作为 Pod 目录
- 一级条目为 `burstable` / `guaranteed` / `besteffort` → 进入子目录查找 `pod*` 目录

### 3.4 cgroup UID 与 K8s UID 匹配

Collector 从 cgroup 目录名提取 Pod UID（`pod<uid>` → `<uid>`），与 kubectl API 返回的 K8s UID 做匹配：

1. **精确匹配**：`pod_uid == k8s_uid`
2. **前缀匹配（回退）**：`pod_uid[:8] == k8s_uid[:8]` 或反向（兼容 cgroup v1 短 UID）
3. **未匹配**：标记为 `unknown-<cgroup_dir_prefix>`，namespace 设为 `unknown`

### 3.5 进程内存采集

对每个容器，读取 `cgroup.procs` 获取所有 PID，然后逐个采集：

| 数据源 | 提取字段 | 用途 |
|--------|----------|------|
| `/proc/<pid>/status` | VmRSS, VmHWM, RssAnon, RssFile, RssShmem, VmSwap | 进程内存基础指标 |
| `/proc/<pid>/smaps_rollup` | Pss | 比例内存（精确值） |
| `/proc/<pid>/status` (raw) | 完整文本 | original_info |
| `/proc/<pid>/smaps_rollup` (raw) | 完整文本 | original_info |

### 3.6 kubectl 调用策略

Collector 对 kubectl 的依赖设计为**可绕过**：

```
优先级1: --pods-info 参数 → 从外部 JSON 文件读取（绕过 kubectl）
优先级2: kubectl API → 通过 run_kubectl() 调用
```

**kubectl fallback 路径下的 kubeconfig 检测**：
```
优先级1: --kubeconfig 参数
优先级2: KUBECONFIG 环境变量
优先级3: sudo 场景 → 从 SUDO_USER 推断 ~<user>/.kube/config
优先级4: kubectl 默认查找路径 (~/.kube/config)
```

### 3.7 错误诊断设计

`run_kubectl()` 在任何错误路径都会打印：
- 完整命令字符串
- 返回码
- stdout 前 200 字符
- stderr 前 500 字符
- KUBECONFIG 路径（如果设置）

---

## 4. Processor 设计

### 4.1 架构

```
processor.py
├── ID 生成器 (make_id_counter)
├── Meminfo 变换 (transform_node_meminfo)
│   └── mem_total → mem_used = total - available
│       mem_hot = Active, mem_cold = Inactive
├── original_info 构造器
│   ├── build_ne_original_info()        — NE 收藏元摘要
│   ├── build_node_original_info()      — meminfo 原文
│   ├── build_pod_original_info()       — cgroup memory.stat + requests/limits
│   ├── build_container_original_info() — 同上
│   └── build_process_original_info()   — /proc/pid/status + smaps
├── 变换管道（Transform Pipeline）
│   ├── transform_process()    — 进程数据透传 + original_info
│   ├── transform_container()  — 容器数据透传 + 递归处理进程
│   ├── transform_pod()        — Pod 数据透传 + 递归处理容器
│   └── transform_node()       — meminfo 计算 + 递归处理 Pod
├── 聚合层 (aggregate_by_ne)    — 按 ne_id 分组 → NE 列表
└── main() — 3 步处理流程
```

### 4.2 3 步处理流程

```
[1/3] 加载原始数据文件  — 支持 glob 通配符（*.json），跳过解析失败的文件
[2/3] 加工数据           — NE 聚合 + 逐层变换
[3/3] 生成 memory_data.json — 附带 _metadata 统计摘要
```

### 4.3 Meminfo 指标计算

| 输出字段 | 计算方式 |
|----------|----------|
| `mem_total_kb` | `MemTotal` 直接取值 |
| `mem_available_kb` | `MemAvailable` 直接取值 |
| `mem_used_kb` | `MemTotal - MemAvailable` |
| `mem_hot_kb` | `Active`（优先）或 `Active(anon) + Active(file)` |
| `mem_cold_kb` | `Inactive`（优先）或 `Inactive(anon) + Inactive(file)` |

> **为什么用 Active/Inactive 而非 Active(anon)/Inactive(anon)**：Active 和 Active(anon) 的关系是 Active = Active(anon) + Active(file) + ...。当内核版本较新时 `/proc/meminfo` 同时提供总计和细分字段；优先使用总计字段以涵盖更多页面类型（unevictable 等）。如果总计字段缺失（如 cgroup v1 环境），回退到 anon+file 之和。

### 4.4 数据透传

除 meminfo 计算外，所有 cgroup 和进程指标均为**直接透传**，processor 不修改数值：
- Pod/Container: `rss_bytes`, `usage_bytes`/`working_set_bytes`, `active_anon_bytes`, `inactive_anon_bytes`, `active_file_bytes`, `inactive_file_bytes`, `requests_memory_bytes`, `limits_memory_bytes`
- Process: `vm_rss_kb`, `vm_hwm_kb`, `rss_anon_kb`, `rss_file_kb`, `rss_shmem_kb`, `vm_pss_kb`, `vm_uss_kb`, `vm_swap_kb`

---

## 5. 前端设计（memory_report.html）

### 5.1 技术选型

| 项目 | 选型 |
|------|------|
| 框架 | 无框架，100% 原生 HTML/CSS/JS |
| 图表库 | Chart.js 4.4.1（CDN 加载） |
| 样式 | CSS 变量 + 暗色主题 |
| 国际化 | 自研 i18n 系统（`data-i18n` 属性 + `t()` 函数） |
| 数据加载 | `fetch()` API + FileReader（本地文件） |
| 火焰图 | Canvas 2D API 自绘 + 外部 SVG 嵌入 |

### 5.2 页面布局

```
┌──────────────┬────────────────────────────────────────────────┐
│   Sidebar    │               Main Content                     │
│   (240px)    │                                                │
├──────────────┤  ┌───────────────────────────────────────────┐ │
│ Logo + Title │  │ Page Header (面包屑 + 标题 + 操作按钮)      │ │
├──────────────┤  ├───────────────────────────────────────────┤ │
│ 语言切换      │  │                                           │ │
│ [中文|EN]    │  │ Content Area                               │ │
├──────────────┤  │  ├─ Stat Cards (指标卡片)                  │ │
│ 文件加载      │  │  ├─ Flame Chart (Canvas 火焰图)           │ │
│ [JSON][SVG]  │  │  ├─ Charts (Chart.js 图表)                │ │
├──────────────┤  │  ├─ Data Tables (数据表格)                │ │
│ 导航树        │  │  ├─ original_info (原始信息文本框)        │ │
│              │  │  └─ SVG Flamegraph (外部火焰图嵌入)        │ │
│ ▶ 网元总览    │  │                                           │ │
│ ▼ 网元/节点   │  └───────────────────────────────────────────┘ │
│   ▶ NE-01    │                                                │
│     ▶ node01 │                                                │
│       ▶ podA │                                                │
│         ctrA │                                                │
│         proc1│                                                │
└──────────────┴────────────────────────────────────────────────┘
```

### 5.3 导航系统

#### 可折叠树形导航

使用**平铺 DOM + 兄弟节点包装器**的设计（而非嵌套 DOM 树）：

```html
<div class="nav-item">NE-01 ▶</div>       ← nav-item
<div class="nav-children">                ← nav-children (兄弟节点)
  <div class="nav-item nav-indent-1">node01 ▶</div>
  <div class="nav-children collapsed">    ← collapsed = 折叠状态
    <div class="nav-item nav-indent-2">podA ▶</div>
    ...
  </div>
</div>
```

- `▶` / `▼` 图标反映折叠状态
- `.nav-children.collapsed { display: none; }` 控制显隐
- CSS 类 `nav-indent-{1..4}` 控制缩进深度

#### 页面路由

使用基于 DOM 显示/隐藏的简单路由：

```javascript
function navTo(el, pageId) {
  // 1. 移除所有 .nav-item 的 .active
  // 2. 隐藏所有 .page
  // 3. 显示 #page-<pageId>
  // 4. 调用关联的 renderFn() 渲染页面内容
}
```

5 个页面：`ne-overview`（总览）、`node`、`pod`、`container`、`process`

### 5.4 国际化系统

```javascript
// 翻译键值对
var i18nStrings = {
  zh: { 'app.title': 'MemInsight', 'stat.nodes': '节点总数', ... },
  en: { 'app.title': 'MemInsight', 'stat.nodes': 'Total Nodes', ... }
};

// 翻译函数（支持参数替换）
function t(key, params) {
  var str = i18nStrings[currentLang][key] || key;
  // 替换 {key} → params[key]
}

// DOM 属性绑定
<span data-i18n="stat.nodes">节点总数</span>
// applyI18n() 遍历 [data-i18n]，设置 textContent

// 动态文本
statsLabel.textContent = t('stat.nodes');
```

语言偏好持久化到 `localStorage('meminsight-lang')`。切换语言时重新渲染导航树和当前页面。

### 5.5 火焰图实现

#### Canvas 火焰图（5 层）

使用 Canvas 2D API 直接绘制，5 层从上到下依次为：

```
L0: 网元 (NE)       — NEs 按 mem_total_kb 占比分配宽度
L1: 节点 (Node)      — 在所属 NE 范围内按 mem_total_kb 细分
L2: Pod             — 在所属 Node 范围内按 working_set 细分
L3: 容器 (Container) — 在所属 Pod 范围内按 usage_bytes 细分
L4: 进程 (Process)   — 在所属 Container 范围内按 vm_rss_kb 细分
```

**宽度计算核心逻辑**：
```javascript
// L4 示例：进程块的宽度 = 全局总内存占比串行分解
const nodeShare = node.mem_total_kb / grandTotal;
const podFrac = pod.working_set_bytes / nodePodTotal;
const podShare = nodeShare * podFrac;
const ctrShare = podShare * ctr.usage_bytes / podCtrTotal;
const procFrac = ctrShare * (proc.vm_rss_kb * 1024) / procTotal;
const bw = procFrac * (W - PAD*2);  // 像素宽度
```

#### 交互设计

Hitmap 数组记录每个矩形的坐标和关联数据：
```javascript
hitmap.push({ x1, y1, x2, y2, item: {ne, node, pod, ctr}, type: 'container' });
```

Canvas `click` 事件根据坐标查找 hitmap，导航到对应详情页面。

#### 外部 SVG 火焰图

支持加载由 Brendan Gregg FlameGraph 工具生成的 SVG 文件，通过 `fetch()` 读取后直接 `innerHTML` 嵌入：

```html
<div id="flamegraph-embed-area">
  <!-- 完整 SVG DOM 嵌入，保留原有缩放/搜索交互 -->
</div>
```

### 5.6 各层级详情页

每个详情页包含以下区块：

| 层级 | 统计卡片 | 图表 | 数据表格 | original_info |
|------|----------|------|----------|---------------|
| **NE 总览** | 4 张卡片（节点数、总内存、已用、空闲） | 火焰图、柱状图、饼图 | NE 列表 | ✓ |
| **Node** | 5 张卡片（总/已用/可用/热/冷） | 柱状图、饼图 | Pod 列表 | ✓ |
| **Pod** | 配额 vs 实时使用卡片 | 柱状图、饼图 | 容器列表 | ✓ |
| **Container** | 容器内存卡片 | 冷热饼图 | 进程列表 | ✓ |
| **Process** | 进程内存指标卡片 | 多指标柱状图 | — | ✓ |

### 5.7 数据加载策略

```
页面加载
  ├── fetch('./memory_data.json') → 成功 → 自动渲染
  │                                    └── setTimeout → fetch('./memory_flame.svg')
  └── 失败（本地文件/CORS）→ 提示用户通过文件选择器手动加载
                                → FileReader → JSON.parse → 渲染
```

---

## 6. 部署场景

### 6.1 节点直接采集

```bash
# 第一步：导出 Pod 元数据（非 root 用户）
kubectl get pods --all-namespaces \
  --field-selector spec.nodeName=k8s-worker-01 \
  -o json > pods_info.json

# 第二步：采集内存数据（需要 root 权限）
sudo python3 collector.py \
  --ne-id ne-001 \
  --ne-name "NE-Core-01" \
  --ne-type "Core Network Element" \
  --node-name k8s-worker-01 \
  --pods-info pods_info.json \
  -o raw_ne-001_node-01.json
```

### 6.2 DaemonSet 部署

```yaml
# DaemonSet 关键配置
spec:
  containers:
  - name: collector
    env:
    - name: NODE_NAME
      valueFrom:
        fieldRef:
          fieldPath: spec.nodeName   # downward API 注入
    volumeMounts:
    - name: proc
      mountPath: /proc              # hostPath
      readOnly: true
    - name: cgroup
      mountPath: /sys/fs/cgroup     # hostPath
      readOnly: true
    - name: pods-info
      mountPath: /data              # pods_info.json 所在目录
```

### 6.3 数据处理与查看

```bash
# 第三步：聚合所有节点的采集结果
python3 processor.py \
  -i raw_ne-001_node-*.json raw_ne-002_node-*.json \
  -o memory_data.json

# 第四步：本地 HTTP 服务启动报告
# （避免 file:// 协议的 CORS 限制）
python3 -m http.server 8080
# 浏览器打开 http://localhost:8080/memory_report.html
```

---

## 7. 字段映射速查表

### 7.1 /proc/meminfo → Node 指标

| meminfo 字段 | JSON 字段 | 单位 |
|-------------|-----------|------|
| `MemTotal` | `mem_total_kb` | KB |
| `MemAvailable` | `mem_available_kb` | KB |
| `MemTotal - MemAvailable` | `mem_used_kb` | KB |
| `Active` 或 `Active(anon)+Active(file)` | `mem_hot_kb` | KB |
| `Inactive` 或 `Inactive(anon)+Inactive(file)` | `mem_cold_kb` | KB |

### 7.2 cgroup memory.stat → Pod/Container 指标

| memory.stat 字段 | JSON 字段 | 说明 |
|-----------------|-----------|------|
| `total_rss` / `rss` | `rss_bytes` | 物理常驻内存 |
| `usage_in_bytes` | `working_set_bytes` / `usage_bytes` | 工作集 |
| `active_anon` | `active_anon_bytes` | 活跃匿名页 |
| `inactive_anon` | `inactive_anon_bytes` | 非活跃匿名页 |
| `active_file` | `active_file_bytes` | 活跃文件页 |
| `inactive_file` | `inactive_file_bytes` | 非活跃文件页 |

### 7.3 /proc/pid/status → Process 指标

| status 字段 | JSON 字段 | 说明 |
|------------|-----------|------|
| `VmRSS` | `vm_rss_kb` | 进程物理内存 |
| `VmHWM` | `vm_hwm_kb` | 内存峰值 |
| `RssAnon` | `rss_anon_kb` | 匿名内存 |
| `RssFile` | `rss_file_kb` | 文件映射 |
| `RssShmem` | `rss_shmem_kb` | 共享内存 |
| `VmSwap` | `vm_swap_kb` | 交换分区 |
| `Pss` (smaps_rollup) | `vm_pss_kb` | 比例内存 |

### 7.4 K8s API → Pod/Container 配置

| K8s 字段 | JSON 字段 | 说明 |
|---------|-----------|------|
| `spec.containers[].resources.requests.memory` | `requests_memory_bytes` | 内存请求 |
| `spec.containers[].resources.limits.memory` | `limits_memory_bytes` | 内存限制 |
| `metadata.name` | `name` | 名称 |
| `metadata.namespace` | `namespace` | 命名空间 |
| `metadata.uid` | `uid` | 唯一标识（cgroup 匹配用） |

---

## 8. 错误处理策略

| 层级 | 错误类型 | 处理方式 |
|------|---------|---------|
| Collector | 文件读取失败（权限/不存在） | 打印 WARN，跳过该条目，继续采集 |
| Collector | kubectl 不可用 | 打印错误+诊断，返回空数据；K8s 元数据留空 |
| Collector | 进程 PID 消失（瞬态） | `collect_process_info()` 返回 `None`，跳过 |
| Collector | --node-name 未指定且无 NODE_NAME 环境变量 | 打印错误，`sys.exit(1)` |
| Processor | JSON 解析失败 | 打印 WARN，跳过该文件 |
| Processor | 无有效输入文件 | 打印 ERROR，`sys.exit(1)` |
| Report | fetch 失败（file:// 协议） | fallback 提示用户手动选择文件 |
| Report | JSON 解析失败 | `alert()` 弹出错误信息 |
| Report | SVG 加载失败 | 静默跳过（非关键功能） |
