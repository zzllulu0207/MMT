# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**MemInsight** — a K8s node memory analysis toolchain: data collection → aggregation → visualization. Target deployment is telecom-style "network elements" (NEs) that group K8s nodes.

## Two collection architectures

**V1** (root-level, 2 scripts):
- `collector.py` — runs directly on each K8s node (needs root), does cgroup exploration + kubectl calls, outputs `raw_<ne-id>.json`
- `processor.py` — aggregates raw JSON files by `ne_id`, computes derived metrics, outputs `memory_data.json`

**V2** (`data_collect_V2/`, 3 scripts) — separates kubectl from root-required node collection:
- `mgmt_collector.py` — runs once on a management node with kubectl access, outputs `nodes_info.json` + `pods_info.json`
- `node_collector.py` — runs on each data node (root), takes `--pods-info pods_info.json`, outputs `raw_<node>.json`. **Has zero kubectl dependency** — all K8s metadata comes from the pods_info.json file.
- `aggregator.py` — merges management metadata + node data, outputs `memory_data.json` (same format as V1)

V1 and V2 produce identical `memory_data.json` format — the HTML report works with either.

Pre-built `memory_data.json` and `memory_data.folded` exist in the repo root for immediate testing — no collectors needed to open the HTML report.

See also: `DESIGN.md` (V1 architecture, Chinese), `data_collect_V2/DESIGN.md` (V2 architecture, Chinese).

## Data model (5-level hierarchy)

`memory_data.json` has a nested structure: **NE → Node → Pod → Container → Process**, with globally unique sequential IDs (`ne-001`, `node-001`, `pod-001`, `ctr-001`, `proc-001`). Every entity carries an `original_info` field with the raw system text for traceability. See `DESIGN.md` §2 for the full JSON schema.

## How to run

No build system, no dependencies beyond Python 3.6+ stdlib.

```bash
# V1 — single node collection
sudo python3 collector.py --ne-id ne-001 --ne-name "NE-Core-01" \
  --node-name k8s-worker-01 --pods-info pods_info.json -o raw_ne-001.json

# V1 — aggregate
python3 processor.py -i raw_ne-*.json -o memory_data.json

# V2 — management node
python3 data_collect_V2/mgmt_collector.py --kubeconfig ~/.kube/config -o ./data/

# V2 — data node (distribute pods_info.json first)
sudo python3 data_collect_V2/node_collector.py \
  --node-name k8s-worker-01 --pods-info pods_info.json

# V2 — aggregate
python3 data_collect_V2/aggregator.py --nodes-info data/nodes_info.json \
  --pods-info data/pods_info.json --ne-id ne-001 --ne-name "NE-Core-01" \
  --raw-files data/raw_*.json -o memory_data.json

# View report
python3 -m http.server 8080
# Open http://localhost:8080/memory_report.html

# Export to FlameGraph folded format (Python or Node)
python3 export_folded.py memory_data.json -o memory_data.folded
node export_folded.js memory_data.json -o memory_data.folded
```

## Key design patterns

- **Data sources**: `/proc/meminfo` → node-level memory; `/sys/fs/cgroup/memory/kubepods/**/memory.stat` → pod/container cgroup stats; `/proc/<pid>/status` + `smaps_rollup` → process memory
- **Cgroup-to-K8s matching**: extracts UID from cgroup directory name (`pod<uid>`), matches against kubectl API by exact UID, falls back to 8-char prefix match; unmatched pods become `unknown-*`
- **original_info**: raw source text preserved at every level so the HTML report can display it without needing file system access
- **Meminfo derived metrics**: `mem_used = MemTotal - MemAvailable`, `mem_hot = Active` (falls back to `Active(anon) + Active(file)`), `mem_cold = Inactive` (same fallback)
- **HTML report** (`memory_report.html`): single ~2000-line zero-framework file, uses Chart.js CDN + Canvas 2D flame chart, custom i18n (`data-i18n` attributes), collapsible tree nav with flat DOM sibling-wrapper pattern
- **kubectl bypass**: Collector/node_collector accepts `--pods-info` to load K8s metadata from a file, avoiding in-cluster kubectl calls entirely
- **CSV export**: the HTML report exports CSV at every level (NE, Node, Pod, Container, Process) with UTF-8 BOM for Excel compatibility. All export buttons use the shared `exportToCSV()` function.
- **Data loading**: the report fetches `memory_data.json` via `fetch()` on load; if that fails (file:// protocol), it falls back to a manual file-picker via FileReader
- **FlameGraph export**: `export_folded.py` (Python) and `export_folded.js` (Node) both convert `memory_data.json` to Brendan Gregg's folded stack format. They produce identical output — pick whichever runtime is available. The output feeds into flamegraph.pl for SVG generation.

## Commit conventions

This repo uses conventional commits: `feat:`, `fix:`, `docs:`, `chore:` prefixes.

## Known quirks

- **Timezone hardcoded to UTC+8**: all scripts use `timezone(timedelta(hours=8))` for `collection_time` / `processed_at` timestamps. This is not configurable — deployments outside CST will get timestamps offset by 8 hours.
- **Code duplication**: `parse_k8s_memory()` is duplicated across `collector.py`, `mgmt_collector.py`, and `node_collector.py`. `parse_kv_file()` and `parse_kv_pairs()` are duplicated between `collector.py` and `node_collector.py`. When fixing bugs in these helpers, check all copies.
