// ============================================================
// DRPL Extension — IREPS Auto-Search Driver
//
// Activates on ireps.gov.in when the service worker sends
// RUN_IREPS_AUTO_SEARCH. Drives the Advanced Search form:
//
//   1. Wait for the form to render.
//   2. Set Work Area to "Works".
//   3. Set Organisation to "All".
//   4. Wait for cascading AJAX, set Railway/PU to "All".
//   5. Wait for cascading AJAX, set Department to "Electrical".
//   6. Select "Custom Date" and set From=today, To=today+3 months.
//   7. Click "Show Results".
//
// IREPS uses a table-layout form with TWO label-select pairs per
// <tr>. The driver finds the label <td> first, then looks for the
// <select> in the NEXT sibling <td> — not just any select in the row.
// ============================================================

const FORM_RENDER_TIMEOUT_MS = 10000;
const CASCADE_POLL_TIMEOUT_MS = 5000;

let driverRunning = false;

export function isIrepsAdvancedSearchPage(): boolean {
  try {
    const url = window.location.href.toLowerCase();
    if (!url.includes('advancedsearch')) return false;
    const bodyText = document.body?.innerText || '';
    if (/tender\s*search\s*results?\s*\d+/i.test(bodyText)) return false;
    return true;
  } catch {
    return false;
  }
}

export function isIrepsHost(): boolean {
  try {
    return window.location.hostname.endsWith('ireps.gov.in');
  } catch {
    return false;
  }
}

export function attachIrepsAutoSearchListener() {
  if (!isIrepsHost()) return;
  console.log('[Ext] IREPS auto-search driver standing by on', window.location.href);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'RUN_IREPS_AUTO_SEARCH') return;
    if (driverRunning) {
      sendResponse({ error: 'Driver already running on this tab.' });
      return;
    }
    driverRunning = true;
    runAutoSearch()
      .catch((err) => console.error('[Ext] IREPS auto-search fatal:', err))
      .finally(() => {
        driverRunning = false;
      });
    sendResponse({ started: true });
    return true;
  });
}

async function runAutoSearch() {
  console.log('[Ext] IREPS auto-search starting');

  if (!isIrepsAdvancedSearchPage()) {
    console.warn('[Ext] Not on advanced search page, aborting');
    return;
  }

  await waitForFormReady();
  logAllSelects();

  // 1. Work Area → "Works"
  await setField(/work\s*area/i, 'Works', 'Work Area');
  await sleep(300);

  // 2. Organisation → "All"
  await setFieldToAll(/organi[sz]ation/i, 'Organisation');
  await sleep(1000);

  // 3. Railway/PU → "All" (cascading from Organisation — poll until populated)
  await pollAndSetToAll(/railway|rly\s*\/?\s*pu/i, 'Railway/PU');
  await sleep(1000);

  // 4. Department → "Electrical" (cascading from Railway/PU — poll until populated)
  await pollAndSetField(/department/i, 'Electrical', 'Department');
  await sleep(300);

  // 5. Custom Date: From=today, To=today+3 months
  await selectCustomDateRange();

  // 6. Submit
  await clickShowResults();

  chrome.runtime.sendMessage({
    type: 'IREPS_AUTO_SEARCH_SUBMITTED',
    payload: { submittedAt: new Date().toISOString() },
  });
  console.log('[Ext] IREPS auto-search form submitted');
}

// --- Select-finding: label cell → next sibling cell's <select> ---

/**
 * IREPS lays out two label-select pairs per <tr>:
 *   <td>Label A</td><td><select>…</select></td>
 *   <td>Label B</td><td><select>…</select></td>
 *
 * This function finds the label <td> matching `pattern`, then walks
 * forward through sibling <td>s to find the adjacent <select>.
 */
function findSelectByAdjacentLabel(pattern: RegExp): HTMLSelectElement | null {
  const cells = document.querySelectorAll<HTMLTableCellElement>('td, th');
  for (const cell of Array.from(cells)) {
    const text = (cell.textContent || '').trim();
    // Label cells are short and don't contain a <select>
    if (text.length > 80) continue;
    if (cell.querySelector('select')) continue;
    if (!pattern.test(text)) continue;

    // Walk forward through sibling cells to find the select
    let next: Element | null = cell.nextElementSibling;
    for (let hops = 0; hops < 3 && next; hops++) {
      const sel = next.querySelector('select') as HTMLSelectElement | null;
      if (sel) return sel;
      next = next.nextElementSibling;
    }
  }
  return null;
}

/**
 * Fallback: identify a <select> by the options it contains.
 * E.g., the Organisation dropdown is the one with "Indian Railway" among its options.
 */
function findSelectByOptionContent(optionPattern: RegExp): HTMLSelectElement | null {
  const selects = document.querySelectorAll<HTMLSelectElement>('select');
  for (const sel of Array.from(selects)) {
    for (const opt of Array.from(sel.options)) {
      const txt = (opt.textContent || '').trim();
      if (optionPattern.test(txt)) return sel;
    }
  }
  return null;
}

function findSelect(labelPattern: RegExp, optionFallbackPattern?: RegExp): HTMLSelectElement | null {
  const sel = findSelectByAdjacentLabel(labelPattern);
  if (sel) return sel;
  if (optionFallbackPattern) return findSelectByOptionContent(optionFallbackPattern);
  return null;
}

// --- Set helpers ---

function setSelectValue(select: HTMLSelectElement, value: string) {
  select.value = value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

function pickOption(select: HTMLSelectElement, text: string): HTMLOptionElement | null {
  const wanted = text.trim().toLowerCase();
  let partial: HTMLOptionElement | null = null;
  for (const opt of Array.from(select.options)) {
    const txt = (opt.textContent || '').trim().toLowerCase();
    if (!txt) continue;
    if (txt === wanted) return opt;
    if (!partial && txt.includes(wanted)) partial = opt;
  }
  return partial;
}

function pickAllOption(select: HTMLSelectElement): HTMLOptionElement | null {
  for (const opt of Array.from(select.options)) {
    if ((opt.textContent || '').trim().toLowerCase() === 'all') return opt;
  }
  return null;
}

async function setField(labelPattern: RegExp, optionText: string, fieldName: string) {
  const select = findSelect(labelPattern);
  if (!select) {
    console.warn(`[Ext] ${fieldName}: <select> not found`);
    return false;
  }
  const opt = pickOption(select, optionText);
  if (!opt) {
    console.warn(`[Ext] ${fieldName}: option "${optionText}" not found among ${select.options.length} options`);
    return false;
  }
  setSelectValue(select, opt.value);
  console.log(`[Ext] ${fieldName} → "${opt.textContent?.trim()}" (value="${opt.value}")`);
  return true;
}

async function setFieldToAll(labelPattern: RegExp, fieldName: string) {
  const select = findSelect(labelPattern);
  if (!select) {
    console.warn(`[Ext] ${fieldName}: <select> not found`);
    return false;
  }
  const opt = pickAllOption(select);
  if (!opt) {
    console.warn(`[Ext] ${fieldName}: no "All" option — options are: ${dumpOptions(select)}`);
    return false;
  }
  setSelectValue(select, opt.value);
  console.log(`[Ext] ${fieldName} → "All" (value="${opt.value}")`);
  return true;
}

/**
 * Poll for a cascading <select> to populate, then set it to "All".
 * After Organisation changes, Railway/PU and Department options are
 * refreshed via AJAX — the <select> may temporarily have only the
 * default placeholder option.
 */
async function pollAndSetToAll(labelPattern: RegExp, fieldName: string) {
  const start = Date.now();
  while (Date.now() - start < CASCADE_POLL_TIMEOUT_MS) {
    const select = findSelect(labelPattern);
    if (select && select.options.length > 1) {
      const opt = pickAllOption(select);
      if (opt) {
        setSelectValue(select, opt.value);
        console.log(`[Ext] ${fieldName} → "All" (value="${opt.value}")`);
        return true;
      }
    }
    await sleep(400);
  }
  // Last-ditch: try to select whatever is there
  const select = findSelect(labelPattern);
  if (select) {
    console.warn(`[Ext] ${fieldName}: "All" not found after polling — options: ${dumpOptions(select)}`);
  } else {
    console.warn(`[Ext] ${fieldName}: <select> not found after polling`);
  }
  return false;
}

async function pollAndSetField(labelPattern: RegExp, optionText: string, fieldName: string) {
  const start = Date.now();
  while (Date.now() - start < CASCADE_POLL_TIMEOUT_MS) {
    const select = findSelect(labelPattern);
    if (select && select.options.length > 1) {
      const opt = pickOption(select, optionText);
      if (opt) {
        setSelectValue(select, opt.value);
        console.log(`[Ext] ${fieldName} → "${opt.textContent?.trim()}" (value="${opt.value}")`);
        return true;
      }
    }
    await sleep(400);
  }
  const select = findSelect(labelPattern);
  if (select) {
    console.warn(`[Ext] ${fieldName}: "${optionText}" not found after polling — options: ${dumpOptions(select)}`);
  } else {
    console.warn(`[Ext] ${fieldName}: <select> not found after polling`);
  }
  return false;
}

// --- Date handling ---

function findDateInputs(): { from: HTMLInputElement | null; to: HTMLInputElement | null } {
  const allInputs = Array.from(document.querySelectorAll<HTMLInputElement>('input'));
  const dateInputs: HTMLInputElement[] = [];

  for (const input of allInputs) {
    if (input.offsetParent === null) continue;
    if (/^\d{2}\/\d{2}\/\d{4}$/.test(input.value || '')) {
      dateInputs.push(input);
    }
  }

  if (dateInputs.length >= 2) {
    return { from: dateInputs[0], to: dateInputs[1] };
  }
  if (dateInputs.length === 1) {
    return { from: dateInputs[0], to: null };
  }
  return { from: null, to: null };
}

function setInputValue(input: HTMLInputElement, value: string) {
  const wasReadonly = input.hasAttribute('readonly');
  if (wasReadonly) input.removeAttribute('readonly');

  input.value = value;
  input.dispatchEvent(new Event('focus', { bubbles: true }));
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.dispatchEvent(new Event('blur', { bubbles: true }));

  if (wasReadonly) input.setAttribute('readonly', '');
}

async function selectCustomDateRange() {
  const radios = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
  let customRadio: HTMLInputElement | null = null;

  for (const r of radios) {
    const parent = r.parentElement;
    const text = (parent?.textContent || '').trim().toLowerCase();
    if (/custom\s*date/.test(text)) {
      customRadio = r;
      break;
    }
  }

  if (customRadio) {
    customRadio.click();
    console.log('[Ext] Clicked "Custom Date" radio');
    await sleep(300);
  } else {
    console.warn('[Ext] "Custom Date" radio not found — setting dates directly');
  }

  const today = new Date();
  const future = new Date(today);
  future.setMonth(future.getMonth() + 3);

  const fromStr = formatDate(today);
  const toStr = formatDate(future);
  const { from, to } = findDateInputs();

  if (from) {
    setInputValue(from, fromStr);
    console.log(`[Ext] From → "${fromStr}"`);
  } else {
    console.warn('[Ext] From date input not found');
  }

  if (to) {
    setInputValue(to, toStr);
    console.log(`[Ext] To → "${toStr}"`);
  } else {
    console.warn('[Ext] To date input not found');
  }

  await sleep(250);
}

function formatDate(date: Date): string {
  const dd = String(date.getDate()).padStart(2, '0');
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const yyyy = date.getFullYear();
  return `${dd}/${mm}/${yyyy}`;
}

// --- Form submission ---

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
    console.log('[Ext] Clicked "Show Results"');
    return;
  }
  const form = document.querySelector('form');
  if (form) {
    (form as HTMLFormElement).submit();
    console.log('[Ext] Submitted form via fallback');
  }
}

// --- Diagnostics ---

function dumpOptions(select: HTMLSelectElement): string {
  return Array.from(select.options)
    .slice(0, 8)
    .map((o) => `"${(o.textContent || '').trim()}"`)
    .join(', ') + (select.options.length > 8 ? ` …(${select.options.length} total)` : '');
}

function logAllSelects() {
  const selects = document.querySelectorAll<HTMLSelectElement>('select');
  console.log(`[Ext] IREPS form: ${selects.length} <select> elements found`);
  selects.forEach((sel, i) => {
    const row = sel.closest('tr');
    const prevCell = sel.closest('td')?.previousElementSibling;
    const label = prevCell ? (prevCell.textContent || '').trim().slice(0, 40) : '(no prev cell)';
    console.log(
      `[Ext]   #${i}: name="${sel.name}" id="${sel.id}" label="${label}" opts=${sel.options.length} ` +
      `current="${(sel.options[sel.selectedIndex]?.textContent || '').trim()}"`,
    );
  });
}

// --- Wait helpers ---

async function waitForFormReady(): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < FORM_RENDER_TIMEOUT_MS) {
    const hasBtn = Array.from(document.querySelectorAll<HTMLElement>('button, input')).some((el) => {
      if (el.offsetParent === null) return false;
      const txt = (el.textContent || (el as HTMLInputElement).value || '').trim().toLowerCase();
      return /^show\s*results$/.test(txt);
    });
    if (hasBtn) {
      console.log('[Ext] IREPS search form is ready');
      return;
    }
    await sleep(300);
  }
  console.warn('[Ext] Form readiness timeout — proceeding anyway');
}

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}
