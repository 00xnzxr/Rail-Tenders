// ============================================================
// DRPL Extension — GeM (bidnext) Auto-Search Driver
//
// Activates on bidnext.gem.gov.in/bidnext/ when the service worker sends
// RUN_GEM_AUTO_SEARCH. Drives the Advance Tender Search workspace:
//
//   1. Switch into the ADVANCE_SEARCH_WS workspace (set hash if needed).
//   2. Click the "Next 6 Months" closing-date radio.
//   3. (Optional) Set the Department dropdown to a value from the merged
//      scope profile + historical fallback. When no departments are known,
//      run a single "All" pass.
//   4. Click "Show Results".
//   5. Walk the result pages — extract tender cards + every PDF in each
//      card's inline "List of documents attached" table — and ship them
//      to the service worker as TenderData with classifiedDocuments
//      populated. The service worker uploads tenders to /tenders and then
//      each doc to /documents.
//
// Bails gracefully on captcha / throttle. React-aware setters fire change
// events the framework actually listens for.
// ============================================================

import { TenderData, ScopeProfile, DocumentLinkInfo, TenderDataMessage } from '../utils/types';
import { classifyDocumentLink } from '../utils/detail-extractor';
import { cleanTitle, pickLabeledField } from '../utils/selectors';

const PORTAL = 'gem' as const;
const ADVANCE_SEARCH_WORKSPACE = 'ADVANCE_SEARCH_WS';
const ADVANCE_SEARCH_HASH = `#WORKSPACE_ID=${ADVANCE_SEARCH_WORKSPACE}`;
const DEFAULT_MAX_PAGES = 5;
const PER_DEPARTMENT_DELAY_MS = 4000;
const PER_PAGE_DELAY_MS = 2500;
const RESULTS_WAIT_TIMEOUT_MS = 15000;
const FORM_RENDER_TIMEOUT_MS = 10000;

interface DepartmentRunResult {
  keyword: string; // "department" reused as keyword for the existing progress UI
  hits: number;
  pages: number;
  errors: number;
}

let driverRunning = false;

export function isAdvanceSearchPage(): boolean {
  try {
    const u = new URL(window.location.href);
    if (!u.hostname.endsWith('bidnext.gem.gov.in')) return false;
    if (!u.pathname.startsWith('/bidnext/')) return false;
    // Workspace id sits in the URL fragment, e.g.  #WORKSPACE_ID=ADVANCE_SEARCH_WS
    return u.hash.includes(ADVANCE_SEARCH_WORKSPACE);
  } catch {
    return false;
  }
}

/** Loosely "we're on a bidnext page that can host the advance-search workspace". */
export function isBidnextHost(): boolean {
  try {
    const u = new URL(window.location.href);
    return u.hostname.endsWith('bidnext.gem.gov.in') && u.pathname.startsWith('/bidnext/');
  } catch {
    return false;
  }
}

export function attachGemAutoSearchListener() {
  if (!isBidnextHost()) return;
  console.log('[Ext] GeM (bidnext) auto-search driver standing by on', window.location.href);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'RUN_GEM_AUTO_SEARCH') return;
    if (driverRunning) {
      sendResponse({ error: 'Driver already running on this tab.' });
      return;
    }
    const profile: ScopeProfile | undefined = message.payload?.profile;
    if (!profile) {
      sendResponse({ error: 'No scope profile supplied.' });
      return;
    }
    driverRunning = true;
    runAutoSearch(profile)
      .catch((err) => console.error('[Ext] GeM auto-search fatal:', err))
      .finally(() => {
        driverRunning = false;
      });
    sendResponse({ started: true });
    return true;
  });
}

async function runAutoSearch(profile: ScopeProfile) {
  // Make sure we're on the advance search workspace. If not, set the hash and
  // wait for the SPA to render the form.
  if (!isAdvanceSearchPage()) {
    console.log('[Ext] Switching to advance search workspace');
    window.location.hash = `WORKSPACE_ID=${ADVANCE_SEARCH_WORKSPACE}`;
    await waitForAdvanceSearchForm();
  } else {
    await waitForAdvanceSearchForm();
  }

  // Departments to iterate. Prefer the scope profile if the admin has tagged
  // groups with department-style labels; fall back to historical departments.
  const departments = pickDepartments(profile);
  console.log(`[Ext] GeM auto-search starting — departments: ${JSON.stringify(departments)}`);

  const results: DepartmentRunResult[] = [];
  for (const department of departments) {
    if (await isCaptchaOrThrottle()) {
      console.warn('[Ext] CAPTCHA / throttle detected — pausing driver');
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon-48.png',
        title: 'Auto-search paused',
        message: 'The portal is asking for human verification. Solve it, then re-trigger from the popup.',
      });
      break;
    }
    try {
      const result = await runForDepartment(department, profile);
      results.push(result);
      reportProgress(result);
    } catch (err) {
      const errResult: DepartmentRunResult = { keyword: department, hits: 0, pages: 0, errors: 1 };
      results.push(errResult);
      reportProgress(errResult);
      console.error(`[Ext] Department "${department}" run failed:`, err);
    }
    await sleep(PER_DEPARTMENT_DELAY_MS);
  }

  const totalHits = results.reduce((acc, r) => acc + r.hits, 0);
  chrome.runtime.sendMessage({
    type: 'GEM_AUTO_SEARCH_COMPLETE',
    payload: { keywordsRun: results.length, totalHits, results },
  });
  console.log(`[Ext] GeM auto-search done: ${totalHits} hits across ${results.length} departments`);
}

function pickDepartments(profile: ScopeProfile): string[] {
  const fromScope: string[] = (profile.keyword_groups || [])
    .map((g) => (g.label || '').trim())
    .filter((l) => l && !/^group$/i.test(l));
  const fromHistorical: string[] = profile.historical_fallback?.departments || [];
  // De-dup preserving order, scope wins.
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const dept of [...fromScope, ...fromHistorical]) {
    const k = dept.trim();
    if (!k || seen.has(k.toLowerCase())) continue;
    seen.add(k.toLowerCase());
    merged.push(k);
  }
  // Always at least one pass — "All" means no department filter.
  return merged.length > 0 ? merged : ['All'];
}

function maxPagesForRun(profile: ScopeProfile): number {
  const n = Number((profile as any).max_pages_per_keyword);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_MAX_PAGES;
}

function reportProgress(result: DepartmentRunResult) {
  chrome.runtime.sendMessage({ type: 'GEM_AUTO_SEARCH_PROGRESS', payload: result });
}

async function runForDepartment(department: string, profile: ScopeProfile): Promise<DepartmentRunResult> {
  // Reset to the advance-search form before each iteration (the SPA returns
  // you to results otherwise).
  if (!isAdvanceSearchPage()) {
    window.location.hash = `WORKSPACE_ID=${ADVANCE_SEARCH_WORKSPACE}`;
    await waitForAdvanceSearchForm();
  }

  await selectClosingDateNext6Months();
  if (department && !/^all$/i.test(department)) {
    await setDepartment(department);
  }
  await clickShowResults();

  const ok = await waitForResults();
  if (!ok) {
    return { keyword: department, hits: 0, pages: 0, errors: 1 };
  }

  let totalHits = 0;
  let pages = 0;
  const maxPages = maxPagesForRun(profile);
  for (let page = 1; page <= maxPages; page++) {
    const tenders = await collectTendersOnPage();
    const tagged = tenders.map((t) => ({ ...t, searchMatchKeyword: department }));
    if (tagged.length > 0) {
      sendTenders(tagged);
      totalHits += tagged.length;
    }
    pages = page;

    const advanced = await goToNextPage();
    if (!advanced) break;
    await sleep(PER_PAGE_DELAY_MS);
  }

  return { keyword: department, hits: totalHits, pages, errors: 0 };
}

// --- Form interaction ---

/** Find the first visible select whose accessible-name / nearby label matches. */
function findSelectByLabel(patterns: RegExp[]): HTMLSelectElement | null {
  const selects = Array.from(document.querySelectorAll<HTMLSelectElement>('select'));
  for (const sel of selects) {
    if (sel.offsetParent === null) continue;
    const context = labelContext(sel);
    if (patterns.some((p) => p.test(context))) return sel;
  }
  return null;
}

function labelContext(el: Element): string {
  // Collect labelling hints: id label, aria-label, neighbour text.
  const parts: string[] = [];
  const id = (el as HTMLElement).id;
  if (id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (lbl?.textContent) parts.push(lbl.textContent);
  }
  const aria = (el as HTMLElement).getAttribute('aria-label');
  if (aria) parts.push(aria);
  // Parent + grand-parent text (truncated) — useful for table-like layouts.
  const parent = el.parentElement;
  if (parent) parts.push(parent.textContent?.slice(0, 200) || '');
  const grand = parent?.parentElement;
  if (grand) parts.push(grand.textContent?.slice(0, 200) || '');
  return parts.join(' | ').toLowerCase();
}

async function setDepartment(department: string) {
  const select = findSelectByLabel([/department/i]);
  if (!select) {
    console.warn('[Ext] Department <select> not found — skipping filter');
    return;
  }
  // Prefer an option whose visible text equals or contains the requested dept.
  const wanted = department.trim().toLowerCase();
  let matched: HTMLOptionElement | null = null;
  for (const opt of Array.from(select.options)) {
    const txt = (opt.textContent || opt.value || '').trim().toLowerCase();
    if (!txt) continue;
    if (txt === wanted) { matched = opt; break; }
    if (!matched && txt.includes(wanted)) matched = opt;
  }
  if (!matched) {
    console.warn(`[Ext] No matching Department option for "${department}"`);
    return;
  }
  select.value = matched.value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(250);
}

async function selectClosingDateNext6Months() {
  // The radio button is sometimes a real <input type="radio"> next to a
  // label, sometimes a styled <span>/<label> with an inner input. Try both.
  const radios = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
  let target: HTMLElement | null = null;
  for (const r of radios) {
    if (r.offsetParent === null && r.type !== 'radio') continue;
    const ctx = labelContext(r);
    if (/next\s*6\s*months/.test(ctx)) {
      target = r;
      break;
    }
  }
  if (!target) {
    // Fallback: click the visible label that says "Next 6 Months".
    const labelish = Array.from(document.querySelectorAll<HTMLElement>('label, span, div, button'));
    target = labelish.find((el) => {
      if (el.offsetParent === null) return false;
      const t = (el.textContent || '').trim().toLowerCase();
      return t === 'next 6 months' || /^next\s*6\s*months$/.test(t);
    }) || null;
  }
  if (!target) {
    console.warn('[Ext] "Next 6 Months" radio not found');
    return;
  }
  target.click();
  await sleep(250);
}

async function clickShowResults() {
  const buttons = Array.from(
    document.querySelectorAll<HTMLElement>('button, input[type="submit"], input[type="button"]'),
  );
  const btn = buttons.find((b) => {
    if (b.offsetParent === null) return false;
    const txt = (b.textContent || (b as HTMLInputElement).value || '').trim().toLowerCase();
    return /^show\s*results$/.test(txt) || /^search$/.test(txt);
  });
  if (btn) {
    btn.click();
    return;
  }
  // Fallback — submit the form.
  const form = document.querySelector('form');
  if (form) (form as HTMLFormElement).submit();
}

// --- Results detection ---

async function waitForAdvanceSearchForm(): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < FORM_RENDER_TIMEOUT_MS) {
    // The form renders when we can see a "Show Results" button + a Railway
    // Zone or Department <select>.
    const hasBtn = Array.from(document.querySelectorAll<HTMLElement>('button, input')).some((el) => {
      if (el.offsetParent === null) return false;
      const txt = (el.textContent || (el as HTMLInputElement).value || '').trim().toLowerCase();
      return /^show\s*results$/.test(txt);
    });
    const hasSelect = !!findSelectByLabel([/department/i, /railway\s*zone/i]);
    if (hasBtn || hasSelect) return;
    await sleep(300);
  }
}

async function waitForResults(): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < RESULTS_WAIT_TIMEOUT_MS) {
    const cards = findTenderCards();
    const text = (document.body?.innerText || '').toLowerCase();
    if (cards.length > 0 || /no\s+(records|tenders|results)\s+found|sorry,\s*there\s+is\s+no\s+data/.test(text)) {
      return true;
    }
    await sleep(400);
  }
  return false;
}

async function isCaptchaOrThrottle(): Promise<boolean> {
  const text = (document.body?.innerText || '').toLowerCase();
  if (/captcha|are you (a )?human|verify you are/i.test(text)) return true;
  if (/too many requests|rate limit|429/i.test(text)) return true;
  return false;
}

// --- Card extraction (bidnext) ---

function findTenderCards(): Element[] {
  // bidnext renders each tender as a block with the literal text
  // "Railway Tender Number:" inside it. Walk up from that label to the
  // enclosing card container.
  const labelNodes = Array.from(document.querySelectorAll<HTMLElement>('*')).filter((el) => {
    if (!el.textContent) return false;
    if (el.children.length > 0) return false; // leaf-ish only
    return /railway\s*tender\s*number\s*:/i.test(el.textContent);
  });
  const cards: Element[] = [];
  const seen = new Set<Element>();
  for (const n of labelNodes) {
    let card: Element | null = n;
    // Walk up until we find a container that also includes "Tender Title" — that's the card.
    for (let depth = 0; depth < 8 && card; depth++) {
      const txt = card.textContent || '';
      if (/tender\s*title\s*:/i.test(txt) && /close\s*date\s*:/i.test(txt) && txt.length < 6000) {
        if (!seen.has(card)) {
          seen.add(card);
          cards.push(card);
        }
        break;
      }
      card = card.parentElement;
    }
  }
  return cards;
}

async function collectTendersOnPage(): Promise<TenderData[]> {
  const cards = findTenderCards();
  const out: TenderData[] = [];
  for (const card of cards) {
    const tender = await cardToTender(card);
    if (tender) out.push(tender);
  }
  return out;
}

function pickField(text: string, label: string): string {
  return pickLabeledField(text, label);
}

async function cardToTender(card: Element): Promise<TenderData | null> {
  const text = (card.textContent || '').replace(/\s+/g, ' ').trim();
  const bidNo = pickField(text, 'Railway\\s*Tender\\s*Number');
  if (!bidNo) return null;

  const title = cleanTitle(pickField(text, 'Tender\\s*Title')) || bidNo;
  const zone = pickField(text, 'Zone\\s*Name');
  const department = pickField(text, 'Department');
  const unit = pickField(text, 'Unit');
  const status = pickField(text, 'Tender\\s*Status') || 'Open';
  const published = pickField(text, 'Published\\s*Date');
  const closing = pickField(text, 'Close\\s*Date');

  const classifiedDocuments = extractInlineDocs(card);

  return {
    portal: PORTAL,
    tenderId: bidNo,
    title,
    department,
    organisation: zone,
    description: [title, zone, department, unit].filter(Boolean).join(' | '),
    estimatedValue: null,
    currency: 'INR',
    openingDate: published || null,
    closingDate: closing || null,
    emdAmount: null,
    preBidDate: null,
    status,
    documentLinks: classifiedDocuments.map((d) => d.url),
    sourceUrl: window.location.href,
    extractedAt: new Date().toISOString(),
    classifiedDocuments,
    nitDocumentLinks: classifiedDocuments.filter((d) => d.type === 'nit').map((d) => d.url),
    corrigendaLinks: classifiedDocuments.filter((d) => d.type === 'corrigendum').map((d) => d.url),
    amendmentLinks: classifiedDocuments.filter((d) => d.type === 'amendment').map((d) => d.url),
    isDetailExtracted: classifiedDocuments.length > 0,
  };
}

/**
 * Walk the card's DOM (and the immediately-following sibling block, which is
 * where the "List of documents attached" table sometimes renders) for PDF /
 * DOC / XLS links. The download cells on bidnext are anchor tags whose href
 * points to a portal action; classify by adjacent label text.
 */
function extractInlineDocs(card: Element): DocumentLinkInfo[] {
  const out: DocumentLinkInfo[] = [];
  const seen = new Set<string>();

  const containers: Element[] = [card];
  let sibling = card.nextElementSibling;
  let hops = 0;
  while (sibling && hops < 3) {
    const t = (sibling.textContent || '').toLowerCase();
    if (t.includes('list of documents') || t.includes('document description') || t.includes('corrigend')) {
      containers.push(sibling);
    }
    sibling = sibling.nextElementSibling;
    hops++;
  }

  for (const container of containers) {
    // Anchor-based downloads
    const anchors = container.querySelectorAll<HTMLAnchorElement>('a[href]');
    anchors.forEach((a) => {
      const href = a.href;
      if (!href || seen.has(href)) return;
      const looksLikeDoc =
        /\.(pdf|doc|docx|xls|xlsx|zip|rar)(\?|$)/i.test(href) ||
        /download|attachment|file|getfile|displaydoc/i.test(href) ||
        (a.getAttribute('download') !== null);
      if (!looksLikeDoc) return;

      // Label: prefer the matching row's "Document Description" cell.
      let label = (a.textContent || '').trim();
      if (!label || label.length < 2) {
        const row = a.closest('tr');
        if (row) {
          const cell = row.querySelector('td');
          label = (cell?.textContent || '').trim();
        }
      }
      if (!label) label = 'Document';
      seen.add(href);
      out.push(classifyDocumentLink(href, label));
    });

    // Button-based downloads with data-url / data-href fallbacks
    const buttons = container.querySelectorAll<HTMLElement>('button[data-url], button[data-href], [data-doc-url], [data-file-url]');
    buttons.forEach((b) => {
      const href = (b.getAttribute('data-url') || b.getAttribute('data-href') || b.getAttribute('data-doc-url') || b.getAttribute('data-file-url') || '').trim();
      if (!href || seen.has(href)) return;
      seen.add(href);
      const row = b.closest('tr');
      const label = (row?.querySelector('td')?.textContent || b.textContent || '').trim() || 'Document';
      out.push(classifyDocumentLink(href, label));
    });
  }

  return out;
}

function sendTenders(tenders: TenderData[]) {
  if (!tenders.length) return;
  const message: TenderDataMessage = {
    type: 'TENDER_DATA_EXTRACTED',
    portal: PORTAL,
    payload: {
      tenders,
      pageType: 'search_results',
      pageUrl: window.location.href,
    },
  };
  (message as any).manual = true; // skip local dedup so re-runs trickle through
  chrome.runtime.sendMessage(message);
}

// --- Pagination ---

async function goToNextPage(): Promise<boolean> {
  // bidnext uses numbered pages with «  ‹  1 2 3 4 5  ›  »  buttons. Click the
  // "next" arrow (› or aria-label="next") when not disabled. Fall back to
  // clicking the page number = currentPage + 1 if we can find it.
  const beforeMarker = snapshotFirstTenderId();

  const candidates = Array.from(
    document.querySelectorAll<HTMLElement>('a, button, li, span'),
  ).filter((el) => el.offsetParent !== null);

  const nextEl = candidates.find((el) => {
    if (el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
    const txt = (el.textContent || '').trim().toLowerCase();
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    return txt === '›' || txt === '>' || txt === 'next' || aria.includes('next');
  });
  if (!nextEl) return false;

  nextEl.click();
  return waitForPageChange(beforeMarker);
}

function snapshotFirstTenderId(): string {
  const txt = (findTenderCards()[0]?.textContent || '').slice(0, 80);
  return txt.replace(/\s+/g, ' ').trim();
}

async function waitForPageChange(before: string): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < RESULTS_WAIT_TIMEOUT_MS) {
    await sleep(400);
    const now = snapshotFirstTenderId();
    if (now && now !== before) return true;
  }
  return false;
}

// --- Utilities ---

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}
