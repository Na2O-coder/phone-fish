#!/usr/bin/env node
import { runStoryboard, formatManifest, saveStoryboards } from '../src/pipeline.js';

const input = process.argv[2];
if (!input) { console.error('usage: zoom-cli.mjs <video> --from A --to B [--cells N] --out sheet.png'); process.exit(2); }
function opt(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}
const from = Number(opt('from', 0));
const to = Number(opt('to', 0));
const cells = Math.max(1, Math.min(16, Number(opt('cells', 6)) || 6));
const out = opt('out', 'zoom.png');
const minGap = Number(opt('min-gap', 0.4)) || 0.4;
const result = await runStoryboard({
  input, from, to, cells, sheets: 1, pick: 'sharp', burn: 'index',
  minGap, transcript: 'off', adaptive: false,
});
await saveStoryboards(result, out);
console.log(formatManifest(result, { maxCellChars: 160 }));
