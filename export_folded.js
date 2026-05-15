#!/usr/bin/env node
/**
 * Convert memory_data.json to FlameGraph folded format.
 *
 * Output format: stack1;stack2;...;stackN value
 * One line per entity at every level (NE, Node, Pod, Container, Process).
 *
 * Usage:
 *   node export_folded.js [input.json] [-o output.folded] [-v] [-q] [--help]
 */

const fs = require('fs');
const path = require('path');

function printHelp() {
  console.log(`
Usage: node export_folded.js [INPUT] [OPTIONS]

Arguments:
  INPUT                     Path to memory_data.json (default: ./memory_data.json)

Options:
  -o, --output <path>       Output file path (default: memory_data.folded)
  -v, --verbose             Print every entity with its value
  -q, --quiet               Suppress info messages (errors only)
  -h, --help                Show this help

Example:
  node export_folded.js memory_data.json -o out.folded -v
  `);
}

function parseArgs(argv) {
  const args = { input: 'memory_data.json', output: 'memory_data.folded', verbose: false, quiet: false };
  let i = 0;
  while (i < argv.length) {
    const a = argv[i];
    if (a === '-h' || a === '--help') { args.help = true; }
    else if (a === '-v' || a === '--verbose') { args.verbose = true; }
    else if (a === '-q' || a === '--quiet') { args.quiet = true; }
    else if (a === '-o' || a === '--output') { args.output = argv[++i] || args.output; }
    else if (!a.startsWith('-')) { args.input = a; }
    i++;
  }
  return args;
}

function exportFolded(inputPath, outputPath, verbose, quiet) {
  const log = quiet ? () => {} : console.log;
  const warn = console.error;

  // --- file existence ---
  if (!fs.existsSync(inputPath)) {
    warn(`[ERROR] Input file not found: ${inputPath}`);
    warn(`        Current working directory: ${process.cwd()}`);
    return 1;
  }

  const fileSize = fs.statSync(inputPath).size;
  log(`[INFO] Reading ${inputPath} (${fileSize.toLocaleString()} bytes)`);

  let data;
  try {
    data = JSON.parse(fs.readFileSync(inputPath, 'utf-8'));
  } catch (e) {
    warn(`[ERROR] Failed to parse ${inputPath}: ${e.message}`);
    return 1;
  }

  const topKeys = Object.keys(data);
  log(`[INFO] Top-level keys: [${topKeys.join(', ')}]`);

  const nes = data.network_elements;
  if (!nes || nes.length === 0) {
    warn("[WARN] 'network_elements' is empty or missing. Nothing to export.");
    if (data._metadata) {
      const m = data._metadata;
      log(`[INFO] Metadata: NEs=${m.network_elements}, Nodes=${m.total_nodes}, Pods=${m.total_pods}, Containers=${m.total_containers}, Procs=${m.total_processes}`);
    }
    return 1;
  }

  log(`[INFO] Found ${nes.length} network element(s)`);

  const lines = [];
  const stats = { ne: 0, node: 0, pod: 0, container: 0, process: 0 };

  for (const ne of nes) {
    const neName = ne.name || 'unknown-ne';
    const nodes = ne.nodes || [];

    const neTotal = nodes.reduce((s, n) => s + (n.mem_used_kb || 0), 0);
    lines.push(`${neName} ${neTotal}`);
    stats.ne++;
    if (verbose) {
      log(`  NE: ${neName}  sum(mem_used_kb)=${neTotal.toLocaleString()}  nodes=${nodes.length}`);
    }

    for (const node of nodes) {
      const nodeName = node.name || 'unknown-node';
      const nodeVal = node.mem_used_kb || 0;
      const pods = node.pods || [];

      lines.push(`${neName};${nodeName} ${nodeVal}`);
      stats.node++;
      if (verbose) {
        log(`    Node: ${nodeName}  mem_used_kb=${nodeVal.toLocaleString()}  pods=${pods.length}`);
      }

      for (const pod of pods) {
        const podName = pod.name || 'unknown-pod';
        const podVal = pod.rss_bytes || 0;
        const containers = pod.containers || [];

        lines.push(`${neName};${nodeName};${podName} ${podVal}`);
        stats.pod++;
        if (verbose) {
          log(`      Pod: ${podName}  rss_bytes=${podVal.toLocaleString()}  containers=${containers.length}`);
        }

        for (const ctr of containers) {
          const ctrName = ctr.name || 'unknown-ctr';
          const ctrVal = ctr.rss_bytes || 0;
          const procs = ctr.processes || [];

          lines.push(`${neName};${nodeName};${podName};${ctrName} ${ctrVal}`);
          stats.container++;
          if (verbose) {
            log(`        Container: ${ctrName}  rss_bytes=${ctrVal.toLocaleString()}  processes=${procs.length}`);
          }

          for (const proc of procs) {
            const procName = proc.name || 'unknown-proc';
            const procVal = proc.vm_rss_kb || 0;

            lines.push(`${neName};${nodeName};${podName};${ctrName};${procName} ${procVal}`);
            stats.process++;
            if (verbose) {
              log(`          Process: ${procName}  vm_rss_kb=${procVal.toLocaleString()}`);
            }
          }
        }
      }
    }
  }

  const outDir = path.dirname(path.resolve(outputPath));
  if (outDir && !fs.existsSync(outDir)) {
    fs.mkdirSync(outDir, { recursive: true });
  }

  fs.writeFileSync(outputPath, lines.join('\n') + '\n', 'utf-8');

  const outSize = fs.statSync(outputPath).size;
  log(`\n[INFO] Wrote ${lines.length.toLocaleString()} lines (${outSize.toLocaleString()} bytes) → ${path.resolve(outputPath)}`);
  log(`[INFO] Breakdown: NE=${stats.ne}  Node=${stats.node}  Pod=${stats.pod}  Container=${stats.container}  Process=${stats.process}`);
  return 0;
}

// --- main ---
const args = parseArgs(process.argv.slice(2));

if (args.help) {
  printHelp();
  process.exit(0);
}

const rc = exportFolded(args.input, args.output, args.verbose, args.quiet);
process.exit(rc);
