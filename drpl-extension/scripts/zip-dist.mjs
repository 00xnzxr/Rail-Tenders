// scripts/zip-dist.mjs
// Phase 7 — package the built `dist/` directory as a Chrome Web Store
// upload zip. Synchronises the version stamped into manifest.json with
// the package.json version, then zips dist/ into releases/.

import { readFileSync, writeFileSync, mkdirSync, existsSync, createWriteStream } from 'node:fs';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import archiver from 'archiver';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const ROOT = resolve(__dirname, '..');
const DIST = join(ROOT, 'dist');
const RELEASES = join(ROOT, 'releases');
const PKG_JSON = join(ROOT, 'package.json');
const MANIFEST = join(DIST, 'manifest.json');

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf-8'));
}

function writeJson(path, obj) {
  writeFileSync(path, JSON.stringify(obj, null, 2) + '\n');
}

async function main() {
  if (!existsSync(DIST)) {
    console.error(`[zip-dist] dist/ does not exist at ${DIST}. Run 'vite build' first.`);
    process.exit(1);
  }

  const pkg = readJson(PKG_JSON);
  const version = pkg.version;

  // Sync manifest version with package.json
  if (existsSync(MANIFEST)) {
    const manifest = readJson(MANIFEST);
    if (manifest.version !== version) {
      manifest.version = version;
      writeJson(MANIFEST, manifest);
      console.log(`[zip-dist] manifest version → ${version}`);
    }
  } else {
    console.warn(`[zip-dist] No manifest.json found at ${MANIFEST} — skipping version sync.`);
  }

  if (!existsSync(RELEASES)) {
    mkdirSync(RELEASES, { recursive: true });
  }

  const zipName = `drpl-extension-v${version}.zip`;
  const zipPath = join(RELEASES, zipName);
  const output = createWriteStream(zipPath);
  const archive = archiver('zip', { zlib: { level: 9 } });

  await new Promise((resolvePromise, rejectPromise) => {
    output.on('close', resolvePromise);
    output.on('error', rejectPromise);
    archive.on('error', rejectPromise);
    archive.pipe(output);
    // Pack the contents of dist/ at the root of the zip (Chrome Web Store
    // expects manifest.json at the top level).
    archive.directory(DIST, false);
    archive.finalize();
  });

  const sizeKb = (archive.pointer() / 1024).toFixed(1);
  console.log(`[zip-dist] wrote ${zipPath} (${sizeKb} KB)`);
}

main().catch((err) => {
  console.error('[zip-dist] failed:', err);
  process.exit(1);
});
