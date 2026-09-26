#!/usr/bin/env node
/**
 * Probe: verify dist/content-scripts/*.js are import-free.
 *
 * MV3 content scripts cannot use ES module imports. The vite plugin
 * `bundle-content-scripts` in vite.config.ts inlines chunk imports;
 * if any survive, Chrome silently fails to load the script.
 *
 * Run after `npm run build`. Exits 1 on any surviving import.
 */
import { readdir, readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXT_ROOT = path.resolve(__dirname, "..", "..");
const DIST = path.join(EXT_ROOT, "dist", "content-scripts");
const PROGRESS = path.join(EXT_ROOT, "memory", "progress.md");

const PROBE = "check-content-script-bundles";

function nowIso() {
  return new Date().toISOString().replace(/\.\d+Z$/, "Z");
}

async function appendProgress(status, detail) {
  try {
    const line = `| ${nowIso()} | probe | ${PROBE} | ${status} — ${detail.replace(/\|/g, "\\|")} |\n`;
    const { writeFile, appendFile } = await import("node:fs/promises");
    if (!existsSync(PROGRESS)) {
      await writeFile(
        PROGRESS,
        "# Extension — Progress Log\n\n| Date | Actor | Event | Detail |\n|---|---|---|---|\n"
      );
    }
    await appendFile(PROGRESS, line);
  } catch (e) {
    console.error(`[probe:${PROBE}] could not write progress: ${e.message}`);
  }
}

async function main() {
  if (!existsSync(DIST)) {
    await appendProgress("FAIL", "dist/content-scripts/ missing — run `npm run build` first");
    console.error(`[probe:${PROBE}] FAIL — ${DIST} not found. Run \`npm run build\` first.`);
    process.exit(1);
  }

  const files = (await readdir(DIST)).filter((f) => f.endsWith(".js"));
  if (files.length === 0) {
    await appendProgress("FAIL", "dist/content-scripts/ has no .js files");
    console.error(`[probe:${PROBE}] FAIL — no .js files in ${DIST}`);
    process.exit(1);
  }

  const offenders = [];
  for (const f of files) {
    const full = path.join(DIST, f);
    const src = await readFile(full, "utf8");
    // Match `import {` or `import x` at line start (rough but effective).
    const lines = src.split("\n");
    lines.forEach((line, idx) => {
      const trimmed = line.trim();
      if (/^import\s+[{\w*]/.test(trimmed) || /^import\s*["']/.test(trimmed)) {
        offenders.push({ file: f, line: idx + 1, text: trimmed.slice(0, 120) });
      }
    });
  }

  if (offenders.length > 0) {
    const detail = `${offenders.length} surviving import(s): ${offenders
      .slice(0, 3)
      .map((o) => `${o.file}:${o.line}`)
      .join(", ")}${offenders.length > 3 ? "..." : ""}`;
    await appendProgress("FAIL", detail);
    console.error(`[probe:${PROBE}] FAIL — ${detail}`);
    for (const o of offenders) {
      console.error(`  ${o.file}:${o.line}  ${o.text}`);
    }
    process.exit(1);
  }

  const detail = `${files.length} content-script(s) checked, zero surviving imports`;
  await appendProgress("OK", detail);
  console.log(`[probe:${PROBE}] OK — ${detail}`);
  process.exit(0);
}

main().catch(async (e) => {
  await appendProgress("FAIL", `unhandled: ${e.message}`);
  console.error(`[probe:${PROBE}] FAIL — ${e.stack || e.message}`);
  process.exit(1);
});
