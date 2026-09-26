// scripts/generate-icons.mjs
// Generates Workflow Companion icons (16, 48, 128) from an inline SVG.
// Uses @resvg/resvg-js (already in devDependencies). Run with:
//   node scripts/generate-icons.mjs
// Output goes into public/icons/.

import { Resvg } from '@resvg/resvg-js';
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ICONS_DIR = resolve(__dirname, '..', 'public', 'icons');

// Two parallel arrows (sync glyph) on a deep navy rounded square.
// Brand-neutral. Top arrow points right, bottom arrow points left.
const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="22" fill="#1e293b"/>
  <g fill="none" stroke="#f97316" stroke-width="10" stroke-linecap="round" stroke-linejoin="round">
    <!-- Top arrow: shaft + arrowhead pointing right -->
    <line x1="36" y1="50" x2="86" y2="50"/>
    <polyline points="76,38 90,50 76,62"/>
    <!-- Bottom arrow: shaft + arrowhead pointing left -->
    <line x1="92" y1="80" x2="42" y2="80"/>
    <polyline points="52,68 38,80 52,92"/>
  </g>
</svg>`.trim();

function render(size) {
  const resvg = new Resvg(SVG, {
    fitTo: { mode: 'width', value: size },
    background: 'rgba(0,0,0,0)',
  });
  return resvg.render().asPng();
}

if (!existsSync(ICONS_DIR)) mkdirSync(ICONS_DIR, { recursive: true });

for (const size of [16, 48, 128]) {
  const png = render(size);
  const path = resolve(ICONS_DIR, `icon-${size}.png`);
  writeFileSync(path, png);
  console.log(`[icons] wrote ${path} (${png.length} bytes)`);
}
