# Chrome Web Store Submission Checklist — DRPL Tender Intelligence

This checklist tracks everything required to publish `drpl-extension` v2.0.0
(Phase 7: scope-driven scraping) to the Chrome Web Store as an Unlisted
internal extension.

## 1. Build the package

```bash
cd drpl-extension
npm install                # picks up the new `archiver` dev dep
npm run package            # produces releases/drpl-extension-v2.0.0.zip
```

The zip is the artifact uploaded to the developer dashboard.

## 2. Developer account

- One-time **$5** registration fee at https://chrome.google.com/webstore/devconsole/.
- Sign in with the Google account that should "own" the listing
  (recommend a shared `dev@drpl.in` style mailbox, not a personal account).

## 3. Listing copy

- **Name**: `DRPL Tender Intelligence`
- **Short description (≤132 chars)**: "Scope-driven extraction of relevant
  tenders from IREPS (blue-tick eligible) and GeM (auto-search by keyword),
  with AI fit scoring."
- **Detailed description**: see `LISTING_DESCRIPTION.md` (write before
  submission — describe the GeM auto-search, IREPS blue-tick gating, and
  that all tender data is posted only to the user's own DRPL backend).
- **Category**: Productivity
- **Language**: English (United States)
- **Visibility**: **Unlisted** (corporate tool — not for public discovery).

## 4. Listing assets

| Asset | Spec | Status |
| --- | --- | --- |
| Icon (already shipped) | 128×128 PNG | `public/icons/icon-128.png` ✅ |
| Small promo tile | 440×280 PNG | TODO (`assets/store/promo-440x280.png`) |
| Screenshot 1 — popup with KeywordPanel | 1280×800 PNG | TODO |
| Screenshot 2 — GeM auto-search running | 1280×800 PNG | TODO |
| Screenshot 3 — IREPS blue-tick gated list | 1280×800 PNG | TODO |
| Screenshot 4 — Tender Scope admin page | 1280×800 PNG | TODO |

## 5. Privacy policy

Required because the extension reads page content from authenticated portals.

- Host the policy at a public URL on the DRPL site (e.g. `drpl.in/privacy/extension`).
- Required points to cover:
  - The extension reads tender content from `*.ireps.gov.in`, `*.gem.gov.in`,
    and the configured aggregator portals **only** while the user is browsing them.
  - All extracted data is sent to the **user's own DRPL backend** (URL configured
    in the popup), never to a third party.
  - JWT tokens stored in `chrome.storage.local` are not transmitted to anyone
    other than that backend.
  - Document downloads (NIT PDFs) are uploaded to the same backend.
  - No analytics, no telemetry, no advertising.
  - The user can disconnect at any time via the popup's logout button, which
    clears the stored token.

## 6. Permissions justifications

The Web Store review team requires a per-permission rationale.

| Permission | Justification |
| --- | --- |
| `storage` | Cache scope profile, dedup state, and the user's auth token. |
| `alarms` | Run the optional scheduled scrape every N minutes. |
| `notifications` | Surface "auto-search complete" / "session expired" / "GeM paused for CAPTCHA" toasts. |
| `tabs` | Open or focus the GeM advance-search and IREPS listing tabs in response to popup actions. |
| `sidePanel` | Reserved for future use (proposal authoring side panel). Disable in listing if reviewer pushes back. |
| `downloads` | Reserved for future use (NIT PDF downloads). Currently unused — remove if reviewer pushes back. |
| `host_permissions: https://*.ireps.gov.in/*` | Read tender listings + detail pages on the IREPS portal. |
| `host_permissions: https://*.gem.gov.in/*` | Read bid listings + detail pages on the GeM portal, drive the advance-search form. |
| `host_permissions: https://mkp.gem.gov.in/*` | GeM marketplace child domain (covered by parent rule but listed explicitly). |
| `host_permissions: tendertiger / bidassist / tenderdetail / tendersinfo / projectstoday` | Read tender aggregator listings for cross-portal coverage. |
| `host_permissions: http://localhost:8000/*` | Dev-mode backend. **Remove for production submission.** |

**Important**: before submission, remove `http://localhost:8000/*` from the
manifest's `host_permissions` and from the popup setup defaults — replace
with the production backend URL. Keep one staging URL as well if needed.

## 7. Pre-submission checklist

- [ ] Bump `version` in `package.json` (zip script syncs `manifest.json`).
- [ ] Run `npm run package` and load `dist/` unpacked to smoke-test.
- [ ] Manually verify popup login → KeywordPanel → Auto-Search GeM works.
- [ ] Manually verify IREPS blue-tick gating drops non-eligible rows.
- [ ] Replace `localhost:8000` in manifest + popup defaults with production URL.
- [ ] Capture the four 1280×800 screenshots.
- [ ] Author + host privacy policy at the public URL.
- [ ] Upload `releases/drpl-extension-v2.0.0.zip` to dev console.
- [ ] Set listing visibility to **Unlisted**.
- [ ] Submit for review (typically 1–3 business days for the first submission).

## 8. After approval

- Distribute the listing URL to internal users via Slack / email.
- When publishing updates, bump the version (`npm run release` does this),
  rebuild, and upload the new zip — Chrome auto-updates installed instances
  within ~6 hours.
