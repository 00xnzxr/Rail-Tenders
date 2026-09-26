#!/usr/bin/env node
/**
 * Probe: verify the backend OTA selectors endpoint serves a JSON shape
 * compatible with src/config/selectors.json.
 *
 * STATUS: the OTA path is currently INERT — the extension does not
 * fetch this endpoint. This probe verifies the *backend half* still
 * works so that when we eventually wire the extension fetch loop,
 * there's no server-side surprise.
 *
 * Env: DRPL_BACKEND_URL (default: prod URL).
 */
import { readFile } from "node:fs/promises";
import { existsSync, appendFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXT_ROOT = path.resolve(__dirname, "..", "..");
const BUNDLED = path.join(EXT_ROOT, "src", "config", "selectors.json");
const PROGRESS = path.join(EXT_ROOT, "memory", "progress.md");
const BACKEND = (process.env.DRPL_BACKEND_URL || "https://drpl-platform-production.up.railway.app").replace(/\/+$/, "");

const PROBE = "check-selectors-ota";
const PORTALS = ["ireps", "gem"];

function appendProgress(status, detail) {
  try {
    const ts = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    const line = `| ${ts} | probe | ${PROBE} | ${status} — ${detail.replace(/\|/g, "\\|")} |\n`;
    if (!existsSync(PROGRESS)) {
      writeFileSync(
        PROGRESS,
        "# Extension — Progress Log\n\n| Date | Actor | Event | Detail |\n|---|---|---|---|\n"
      );
    }
    appendFileSync(PROGRESS, line);
  } catch (e) {
    console.error(`[probe:${PROBE}] could not write progress: ${e.message}`);
  }
}

async function main() {
  if (!existsSync(BUNDLED)) {
    appendProgress("FAIL", `bundled selectors.json missing at ${BUNDLED}`);
    console.error(`[probe:${PROBE}] FAIL — bundled selectors.json missing`);
    process.exit(1);
  }

  let bundled;
  try {
    bundled = JSON.parse(await readFile(BUNDLED, "utf8"));
  } catch (e) {
    appendProgress("FAIL", `bundled selectors.json invalid JSON: ${e.message}`);
    console.error(`[probe:${PROBE}] FAIL — bundled selectors.json invalid: ${e.message}`);
    process.exit(1);
  }

  let okCount = 0;
  const issues = [];

  for (const portal of PORTALS) {
    const url = `${BACKEND}/api/extension/selectors/${portal}`;
    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        issues.push(`${portal}: HTTP ${resp.status}`);
        continue;
      }
      const body = await resp.json();
      if (typeof body !== "object" || body === null) {
        issues.push(`${portal}: non-object response`);
        continue;
      }
      okCount += 1;
    } catch (e) {
      issues.push(`${portal}: ${e.message}`);
    }
  }

  if (issues.length === 0) {
    appendProgress("OK", `${okCount}/${PORTALS.length} portals responded with JSON`);
    console.log(`[probe:${PROBE}] OK — ${okCount}/${PORTALS.length} portals responded`);
    process.exit(0);
  }

  appendProgress("FAIL", issues.join("; "));
  console.error(`[probe:${PROBE}] FAIL — ${issues.join("; ")}`);
  process.exit(1);
}

main().catch((e) => {
  appendProgress("FAIL", `unhandled: ${e.message}`);
  console.error(`[probe:${PROBE}] FAIL — ${e.stack || e.message}`);
  process.exit(1);
});
