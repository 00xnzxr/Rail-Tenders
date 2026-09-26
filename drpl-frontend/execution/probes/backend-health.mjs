#!/usr/bin/env node
/**
 * Probe: hits the backend /health/capacity endpoint and verifies the
 * shape consumed by load tests + this frontend's status displays.
 *
 * Env: VITE_API_URL (default: localhost:8000).
 */
import { existsSync, appendFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FE_ROOT = path.resolve(__dirname, "..", "..");
const PROGRESS = path.join(FE_ROOT, "memory", "progress.md");
const API = (process.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

const PROBE = "backend-health";

function appendProgress(status, detail) {
  try {
    const ts = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    const line = `| ${ts} | probe | ${PROBE} | ${status} — ${detail.replace(/\|/g, "\\|")} |\n`;
    if (!existsSync(PROGRESS)) {
      writeFileSync(
        PROGRESS,
        "# Frontend — Progress Log\n\n| Date | Actor | Event | Detail |\n|---|---|---|---|\n"
      );
    }
    appendFileSync(PROGRESS, line);
  } catch (e) {
    console.error(`[probe:${PROBE}] could not write progress: ${e.message}`);
  }
}

async function main() {
  const url = `${API}/health/capacity`;
  let resp;
  try {
    resp = await fetch(url);
  } catch (e) {
    appendProgress("FAIL", `fetch failed against ${API}: ${e.message}`);
    console.error(`[probe:${PROBE}] FAIL — fetch failed: ${e.message}`);
    process.exit(1);
  }

  if (!resp.ok) {
    appendProgress("FAIL", `HTTP ${resp.status} from ${url}`);
    console.error(`[probe:${PROBE}] FAIL — HTTP ${resp.status}`);
    process.exit(1);
  }

  let body;
  try {
    body = await resp.json();
  } catch (e) {
    appendProgress("FAIL", `non-JSON response: ${e.message}`);
    console.error(`[probe:${PROBE}] FAIL — non-JSON response`);
    process.exit(1);
  }

  // Shape check — soft (warn but don't fail on missing keys; backend evolves).
  const required = ["web_db_pool", "v2"];
  const missing = required.filter((k) => !(k in body));
  if (missing.length > 0) {
    appendProgress("FAIL", `missing keys: ${missing.join(", ")}`);
    console.error(`[probe:${PROBE}] FAIL — missing keys: ${missing.join(", ")}`);
    process.exit(1);
  }

  const queueDepth = body?.v2?.queue_depth ?? "unknown";
  const activeRuns = body?.v2?.active_runs_global ?? "unknown";
  const detail = `queue_depth=${queueDepth}, active_runs=${activeRuns}, target=${API}`;
  appendProgress("OK", detail);
  console.log(`[probe:${PROBE}] OK — ${detail}`);
  process.exit(0);
}

main().catch((e) => {
  appendProgress("FAIL", `unhandled: ${e.message}`);
  console.error(`[probe:${PROBE}] FAIL — ${e.stack || e.message}`);
  process.exit(1);
});
