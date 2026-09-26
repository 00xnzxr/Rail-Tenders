# SOP — Content Script Bundling (MV3 ESM Invariant)

## Goal

Manifest V3 content scripts cannot use ES module imports — they run as plain scripts in the page's CSP context. The popup and service worker *can* use modules. Vite naturally code-splits ESM, so a build-time plugin must inline any chunk imports into the content script before it ships.

## Inputs

- **Source files.** `src/content-scripts/ireps.ts`, `gem.ts`, `gem-search-driver.ts`, `aggregators.ts`.
- **Build trigger.** `npm run build` (one-shot) or `npm run dev` (watch).
- **Plugin.** `bundle-content-scripts` defined in [../vite.config.ts:18-151](../vite.config.ts), runs in `closeBundle`.

## Outputs

- **`dist/content-scripts/*.js`** — self-contained scripts with zero `import` statements.
- **`dist/chunks/*.js`** — only chunks still referenced by the popup or service worker. Chunks used solely by content scripts are deleted after inlining.

## Determinism boundary

Pure build-time deterministic transformation. No LLM, no runtime decisions.

## Tool contract

The plugin does this on `closeBundle`:

1. Read every `dist/chunks/*.js` into memory.
2. For each content-script output (`dist/content-scripts/{ireps,gem,gem-search-driver,aggregators}.js`):
   - Find imports of the form `import { ... } from "../chunks/<hash>.js"`.
   - For each matched chunk: parse its `export { ... }` ([vite.config.ts:83](../vite.config.ts)), wrap the chunk body in an IIFE that returns the exports ([vite.config.ts:110](../vite.config.ts)), destructure the IIFE result into local variables ([vite.config.ts:118-121](../vite.config.ts)), and remove the import line ([vite.config.ts:124](../vite.config.ts)).
3. Clean up chunks not referenced by popup or service worker.

## Edge cases

1. **Generic CSS selectors are not portal selectors.** Code that uses `'table tr'` or `'.MuiCard-root'` is fine — these are DOM observers, not portal-specific selectors. The "selectors live in selectors.json" rule is about portal selectors only.
2. **Adding a new content script.** Add the entry to `vite.config.ts` rollup inputs AND verify the plugin's iteration covers it. Run `check-content-script-bundles.mjs` after the first build.
3. **Adding a shared util used by both popup and content script.** The util ends up in a chunk imported by both. The plugin inlines into the content script and leaves the chunk in place for the popup. This is the intended path.

## Failure modes

### Known latent risk — transitive chunk imports

**[../vite.config.ts:101-102](../vite.config.ts)** strips nested chunk imports without inlining them.

Scenario: `gem.ts` imports `extractDetailPageData` from chunk A. Chunk A imports a utility from chunk B. The plugin inlines chunk A's body into `gem.js` — but chunk A's body contains `import { foo } from "../chunks/B.js"`, which is matched by lines 101-102 and **removed without inlining chunk B's body**. The resulting `gem.js` references `foo` but doesn't define it.

This has not bitten production yet because most chunks are leaves. **Mitigation:** the verification probe (below) catches surviving imports. **Long-term fix:** make the plugin inline recursively. Deferred to a follow-up task per [../memory/decisions.md](../memory/decisions.md).

### Other failures

| Symptom | Cause | Fix |
|---|---|---|
| Content script silently does nothing in Chrome | Surviving `import` in `dist/content-scripts/*.js` | Run the verification probe; refactor the offending utility to be self-contained |
| `Identifier 'X' has already been declared` console error | Same chunk inlined twice (multiple imports referencing it) | Plugin should dedupe per content script — currently does; if it regresses, a single chunk shows up twice in the IIFE chain |
| Popup fails to load after a refactor | A chunk used only by popup got deleted because the plugin thought only the content script needed it | Verify chunk usage map in `vite.config.ts` — popup chunks must be retained |

## Verification

```bash
cd drpl-extension

# 1. Build
npm run build

# 2. Run the bundle invariant check
node execution/probes/check-content-script-bundles.mjs

# 3. Manual visual check
grep -E "^import " dist/content-scripts/*.js
# Should produce zero matches
```

The probe exits non-zero if any `import` statement survives in `dist/content-scripts/*.js`.

## Open work

- Make the Vite plugin's chunk-inlining recursive (fixes the latent transitive-import risk at [../vite.config.ts:101-102](../vite.config.ts)).
- Deferred until a real failure surfaces — the probe catches it preemptively in the meantime.
