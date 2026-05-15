#!/usr/bin/env python3
"""Convert memory_data.json to FlameGraph folded format.

Output format: stack1;stack2;...;stackN value
One line per entity at every level (NE, Node, Pod, Container, Process).

Unit note: Pod/Container use rss_bytes, others use KB. This is intentional —
the folded file preserves raw values. Convert units before generating SVG if needed.
"""

import argparse
import json
import os
import sys


def export_folded(input_path: str, output_path: str, verbose: bool = False) -> int:
    # --- file existence check ---
    if not os.path.exists(input_path):
        print(f"[ERROR] Input file not found: {input_path}", file=sys.stderr)
        print(f"        Current working directory: {os.getcwd()}", file=sys.stderr)
        return 1

    file_size = os.path.getsize(input_path)
    print(f"[INFO] Reading {input_path} ({file_size:,} bytes)")

    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in {input_path}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] Failed to read {input_path}: {e}", file=sys.stderr)
        return 1

    top_keys = list(data.keys())
    print(f"[INFO] Top-level keys: {top_keys}")

    nes = data.get('network_elements', [])
    if not nes:
        print("[WARN] 'network_elements' is empty or missing. Nothing to export.", file=sys.stderr)
        if '_metadata' in data:
            meta = data['_metadata']
            print(f"[INFO] Metadata: NEs={meta.get('network_elements')}, "
                  f"Nodes={meta.get('total_nodes')}, Pods={meta.get('total_pods')}, "
                  f"Containers={meta.get('total_containers')}, Procs={meta.get('total_processes')}")
        return 1

    print(f"[INFO] Found {len(nes)} network element(s)")

    lines = []
    stats = {'ne': 0, 'node': 0, 'pod': 0, 'container': 0, 'process': 0}

    for ne in nes:
        ne_name = ne.get('name', 'unknown-ne')
        nodes = ne.get('nodes', [])

        ne_total = sum(n.get('mem_used_kb', 0) for n in nodes)
        lines.append(f"{ne_name} {ne_total}")
        stats['ne'] += 1
        if verbose:
            print(f"  NE: {ne_name}  sum(mem_used_kb)={ne_total:,}  nodes={len(nodes)}")

        for node in nodes:
            node_name = node.get('name', 'unknown-node')
            node_val = node.get('mem_used_kb', 0)
            pods = node.get('pods', [])

            lines.append(f"{ne_name};{node_name} {node_val}")
            stats['node'] += 1
            if verbose:
                print(f"    Node: {node_name}  mem_used_kb={node_val:,}  pods={len(pods)}")

            for pod in pods:
                pod_name = pod.get('name', 'unknown-pod')
                pod_val = pod.get('rss_bytes', 0)
                containers = pod.get('containers', [])

                lines.append(f"{ne_name};{node_name};{pod_name} {pod_val}")
                stats['pod'] += 1
                if verbose:
                    print(f"      Pod: {pod_name}  rss_bytes={pod_val:,}  containers={len(containers)}")

                for ctr in containers:
                    ctr_name = ctr.get('name', 'unknown-ctr')
                    ctr_val = ctr.get('rss_bytes', 0)
                    procs = ctr.get('processes', [])

                    lines.append(f"{ne_name};{node_name};{pod_name};{ctr_name} {ctr_val}")
                    stats['container'] += 1
                    if verbose:
                        print(f"        Container: {ctr_name}  rss_bytes={ctr_val:,}  processes={len(procs)}")

                    for proc in procs:
                        proc_name = proc.get('name', 'unknown-proc')
                        proc_val = proc.get('vm_rss_kb', 0)

                        lines.append(
                            f"{ne_name};{node_name};{pod_name};{ctr_name};{proc_name} {proc_val}"
                        )
                        stats['process'] += 1
                        if verbose:
                            print(f"          Process: {proc_name}  vm_rss_kb={proc_val:,}")

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    out_size = os.path.getsize(output_path)
    print(f"\n[INFO] Wrote {len(lines):,} lines ({out_size:,} bytes) → {os.path.abspath(output_path)}")
    print(f"[INFO] Breakdown: NE={stats['ne']}  Node={stats['node']}  "
          f"Pod={stats['pod']}  Container={stats['container']}  Process={stats['process']}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Convert memory_data.json to FlameGraph folded format.',
        epilog='Example: python export_folded.py memory_data.json -o out.folded -v'
    )
    parser.add_argument(
        'input', nargs='?', default='memory_data.json',
        help='Path to memory_data.json (default: ./memory_data.json)'
    )
    parser.add_argument(
        '-o', '--output', default='memory_data.folded',
        help='Output file path (default: memory_data.folded)'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print every entity with its value as parsing proceeds'
    )
    parser.add_argument(
        '-q', '--quiet', action='store_true',
        help='Suppress all output except errors'
    )
    args = parser.parse_args()

    if args.quiet:
        sys.stdout = open(os.devnull, 'w')

    rc = export_folded(args.input, args.output, verbose=args.verbose)
    sys.exit(rc)


if __name__ == '__main__':
    main()
