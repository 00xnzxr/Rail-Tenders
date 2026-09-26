// ============================================================
// DRPL Extension - IREPS Content Script
// Extracts tender data from IREPS portal pages
// Uses adaptive extraction: detects tables by structure, not hardcoded selectors
// ============================================================

import { extractText, extractNumber, extractHref, cleanTitle } from '../utils/selectors';
import { pickTenderTitle } from '../utils/detail-title';
import { TenderData, TenderDataMessage, PageType, DocumentLinkInfo } from '../utils/types';
import { extractDetailPageData, extractAllDocumentLinks, classifyDocumentLink, isFooterDocument } from '../utils/detail-extractor';
import { attachIrepsAutoSearchListener, isIrepsAdvancedSearchPage } from './ireps-search-driver';

const PORTAL = 'ireps' as const;

// --- Phase 7: Blue-tick eligibility config (loaded from selectors.json, OTA-updateable) ---

interface IrepsEligibilityConfig {
  imageSrcPatterns: string[];
  cssClassFragments: string[];
  colorHexList: string[];
  ariaLabelPatterns: string[];
}

const DEFAULT_ELIGIBILITY_CONFIG: IrepsEligibilityConfig = {
  imageSrcPatterns: ['blue_arrow', 'blue_tick', 'bluearrow', 'bluetick', 'eligible', 'tickmark', '/icons/right', 'tick.png', 'tick.gif', 'submit_for_approval', 'icon_submit'],
  cssClassFragments: ['blue-tick', 'eligible-row', 'is-eligible', 'tender-eligible', 'blueArrow'],
  colorHexList: ['#1e90ff', '#0066cc', '#0d6efd', '#1976d2', '#2196f3'],
  ariaLabelPatterns: ['eligible', 'tender eligible', 'you can bid'],
};

let eligibilityConfig: IrepsEligibilityConfig = DEFAULT_ELIGIBILITY_CONFIG;
let blueTickGatingEnabled = true;
let skippedRowSamples: string[] = [];
const MAX_SKIPPED_ROW_SAMPLES = 100;

async function loadEligibilityConfig(): Promise<void> {
  try {
    const url = chrome.runtime.getURL('config/selectors.json');
    const resp = await fetch(url);
    if (resp.ok) {
      const data = await resp.json();
      const cfg = data?.portals?.ireps?.eligibility;
      if (cfg) {
        eligibilityConfig = {
          imageSrcPatterns: cfg.imageSrcPatterns || DEFAULT_ELIGIBILITY_CONFIG.imageSrcPatterns,
          cssClassFragments: cfg.cssClassFragments || DEFAULT_ELIGIBILITY_CONFIG.cssClassFragments,
          colorHexList: cfg.colorHexList || DEFAULT_ELIGIBILITY_CONFIG.colorHexList,
          ariaLabelPatterns: cfg.ariaLabelPatterns || DEFAULT_ELIGIBILITY_CONFIG.ariaLabelPatterns,
        };
      }
    }
  } catch (err) {
    console.warn('[Ext] Failed to load eligibility config, using defaults', err);
  }

  // Allow service worker / popup to disable gating (e.g., for ad-hoc full scrapes)
  try {
    const stored = await chrome.storage.local.get('irepsBlueTickGating');
    if (stored.irepsBlueTickGating === false) {
      blueTickGatingEnabled = false;
      console.log('[Ext] IREPS blue-tick gating DISABLED via storage flag');
    }
  } catch {}
}

// --- Main Entry Point ---

function init() {
  console.log('[Ext] IREPS content script loaded on:', window.location.href);

  // Attach the auto-search driver listener on all IREPS pages
  attachIrepsAutoSearchListener();

  // On the IREPS homepage or login pages, don't attempt extraction
  const url = window.location.href;
  if (url === 'https://www.ireps.gov.in/' || url === 'https://ireps.gov.in/') {
    console.log('[Ext] On IREPS homepage, skipping extraction');
    return;
  }

  // On the advanced search form page, skip extraction (the search driver handles it)
  if (isIrepsAdvancedSearchPage()) {
    console.log('[Ext] On IREPS advanced search form, skipping extraction');
    return;
  }

  // Wait for content to appear then extract
  waitForContent(async () => {
    await loadEligibilityConfig();
    const pageType = detectPageType();

    // Disable blue-tick gating on search results pages — the search criteria
    // (Works / Electrical / date range) already filter for relevant tenders.
    // Blue-tick gating only applies on the logged-in tender listing page where
    // IREPS marks pre-qualified rows with a specific icon.
    if (pageType === 'search_results') {
      blueTickGatingEnabled = false;
    }

    console.log('[Ext] Detected page type:', pageType, '| blue-tick gating:', blueTickGatingEnabled);

    // Diagnostic: log first few rows' Actions column HTML for selector tuning
    if (pageType === 'search_results' || pageType === 'tender_listing') {
      logFirstRowActions();
    }

    // Read user-set tender limit (default 10 for quality-over-quantity)
    const limitStore = await chrome.storage.local.get('tenderLimit');
    const tenderLimit = Math.max(1, Math.min(100, limitStore.tenderLimit || 10));

    let tenders = extractTenders(pageType);
    if (tenders.length > tenderLimit) {
      console.log(`[Ext] Capping to user-specified limit: ${tenderLimit} of ${tenders.length} valid tenders`);
      tenders = tenders.slice(0, tenderLimit);
    }

    if (tenders.length > 0) {
      const withDetail = tenders.filter(t => t.detailUrl);
      console.log(`[Ext] Extracted ${tenders.length} tenders (${withDetail.length} with detailUrl, limit=${tenderLimit})`);
      if (tenders[0]) {
        console.log(`[Ext] Sample tender: id=${tenders[0].tenderId}, closingDate=${tenders[0].closingDate}, detailUrl=${tenders[0].detailUrl || '(none)'}`);
      }
      sendTendersToBackground(tenders, pageType);
    } else {
      console.log('[Ext] No tenders extracted from this page');
    }

    // Pagination is DISABLED by default in quality mode — user picks N tenders and we process those.
    // Background tabs opened by the pagination walker have pageNo= in their URL.
    const PAGINATION_WALK_ENABLED: boolean = false;
    if (PAGINATION_WALK_ENABLED && (pageType === 'search_results' || pageType === 'tender_listing')) {
      const hasPageNoParam = /[?&]pageNo=/i.test(window.location.href);
      if (!hasPageNoParam) {
        const pagination = extractPaginationInfo();
        if (pagination && pagination.pageUrls.length > 0) {
          console.log(`[Ext] Pagination detected: ${pagination.totalCount} results, ${pagination.pageUrls.length} more pages`);
          chrome.runtime.sendMessage({
            type: 'IREPS_SEARCH_PAGINATION',
            portal: PORTAL,
            payload: pagination,
          });
        }
      }
    }

    if (skippedRowSamples.length > 0) {
      console.log(`[Ext] Logged ${skippedRowSamples.length} non-blue-tick row HTML samples for selector tuning`);
      try {
        await chrome.storage.local.set({
          irepsSkippedRowSamples: skippedRowSamples.slice(0, MAX_SKIPPED_ROW_SAMPLES),
          irepsSkippedRowSamplesAt: new Date().toISOString(),
        });
      } catch {}
    }
  });
}

// --- Page Type Detection (adaptive) ---

function detectPageType(): PageType {
  const url = window.location.href.toLowerCase();
  const bodyText = document.body?.innerText?.toLowerCase() || '';

  // Advanced search form page (no results on page) — not a results page
  if (url.includes('advancedsearch')) {
    if (!/tender\s*search\s*results?\s*\d+/.test(bodyText)) {
      return 'unknown';
    }
  }

  // URL-based detection
  if (url.includes('tenderdetail') || url.includes('tender_detail') || url.includes('viewtender')
    || url.includes('nitpublish') || url.includes('viewnit')) {
    return 'tender_detail';
  }
  if (url.includes('search') || url.includes('result') || url.includes('list')) {
    return 'search_results';
  }
  if (url.includes('bidstatus') || url.includes('bid_status') || url.includes('bidresult')) {
    return 'bid_status';
  }
  if (url.includes('etender') || url.includes('e-tender') || url.includes('tender')) {
    return 'tender_listing';
  }

  // If we have tables with tender-like data, treat as listing
  const tables = findTenderTables();
  if (tables.length > 0) {
    return 'tender_listing';
  }

  // Check page content for tender keywords
  if (bodyText.includes('tender') || bodyText.includes('bid') || bodyText.includes('e-procurement')) {
    return 'tender_listing';
  }

  return 'unknown';
}

// --- Adaptive Table Detection ---

interface ColumnMapping {
  tenderNo: number;
  title: number;
  department: number;
  organisation: number;
  value: number;
  openingDate: number;
  closingDate: number;
  emd: number;
  status: number;
}

// Keywords to match column headers to fields
const COLUMN_PATTERNS: Record<keyof ColumnMapping, RegExp[]> = {
  tenderNo: [/tender\s*(no|number|id|#)/i, /^s\.?\s*no/i, /tender\s*ref/i, /case\s*no/i, /nit\s*no/i],
  title: [/tender\s*title/i, /title|description|subject|scope|item/i, /name\s*of\s*(work|tender)/i, /brief\s*desc/i],
  department: [/deptt|dept|department|division|section|branch/i],
  organisation: [/^zone$/i, /^railway$/i, /^org(ani[sz]ation)?$/i, /zonal/i],
  value: [/value|amount|cost|estimate|budget/i, /approx|tender\s*value/i, /rs\.?|inr|₹/i],
  openingDate: [/open(ing)?\s*date/i, /start\s*date|publish/i, /date\s*of\s*(open|publish|issue)/i],
  closingDate: [/due\s*date|due.*time/i, /clos(ing|e)?\s*date/i, /last\s*date|deadline|end\s*date|submission/i],
  emd: [/emd|earnest\s*money|bid\s*security/i],
  status: [/status|state|stage/i],
};

function findTenderTables(): HTMLTableElement[] {
  const allTables = document.querySelectorAll('table');
  const tenderTables: HTMLTableElement[] = [];

  allTables.forEach((table) => {
    // Skip tiny tables (nav bars, layout tables)
    const rows = table.querySelectorAll('tbody tr, tr');
    if (rows.length < 2) return;

    // Check if table has tender-related headers
    const headerText = getTableHeaderText(table).toLowerCase();
    const tenderKeywords = ['tender', 'bid', 'nit', 'e-tender', 'closing date', 'opening date',
      'estimated value', 'emd', 'department', 'work', 'zone', 'railway'];

    const matchCount = tenderKeywords.filter(kw => headerText.includes(kw)).length;
    if (matchCount >= 2) {
      tenderTables.push(table);
    }
  });

  return tenderTables;
}

function getTableHeaderText(table: HTMLTableElement): string {
  const headers: string[] = [];
  // Check thead
  table.querySelectorAll('thead th, thead td').forEach(th => {
    headers.push(th.textContent?.trim() || '');
  });
  // If no thead, check first row
  if (headers.length === 0) {
    const firstRow = table.querySelector('tr');
    firstRow?.querySelectorAll('th, td').forEach(cell => {
      headers.push(cell.textContent?.trim() || '');
    });
  }
  return headers.join(' | ');
}

function mapColumns(table: HTMLTableElement): ColumnMapping | null {
  const headers: string[] = [];

  // Get header cells
  const headerCells = table.querySelectorAll('thead th, thead td');
  if (headerCells.length > 0) {
    headerCells.forEach(th => headers.push(th.textContent?.trim() || ''));
  } else {
    // Try first row as headers
    const firstRow = table.querySelector('tr');
    firstRow?.querySelectorAll('th, td').forEach(cell => {
      headers.push(cell.textContent?.trim() || '');
    });
  }

  if (headers.length < 3) return null;

  const mapping: Partial<ColumnMapping> = {};

  headers.forEach((header, index) => {
    for (const [field, patterns] of Object.entries(COLUMN_PATTERNS)) {
      if (mapping[field as keyof ColumnMapping] !== undefined) continue;
      for (const pattern of patterns) {
        if (pattern.test(header)) {
          mapping[field as keyof ColumnMapping] = index;
          break;
        }
      }
    }
  });

  // Must have at least tender number or title to be useful
  if (mapping.tenderNo === undefined && mapping.title === undefined) {
    return null;
  }

  // Reject degenerate mappings where most fields map to the same column
  // (happens with layout tables wrapping the whole page)
  const usedCols = Object.values(mapping).filter((v) => v !== undefined);
  const uniqueCols = new Set(usedCols);
  if (usedCols.length > 2 && uniqueCols.size <= 2) {
    return null;
  }

  // Fill in defaults (-1 means not found)
  return {
    tenderNo: mapping.tenderNo ?? -1,
    title: mapping.title ?? -1,
    department: mapping.department ?? -1,
    organisation: mapping.organisation ?? -1,
    value: mapping.value ?? -1,
    openingDate: mapping.openingDate ?? -1,
    closingDate: mapping.closingDate ?? -1,
    emd: mapping.emd ?? -1,
    status: mapping.status ?? -1,
  };
}

function getCellText(row: Element, colIndex: number): string {
  if (colIndex < 0) return '';
  const cells = row.querySelectorAll('td');
  if (colIndex >= cells.length) return '';
  return extractText(cells[colIndex]);
}

/**
 * Resolve a tender title from a table cell, preferring the cell's (or its inner
 * anchor's) `title=` attribute — IREPS sets the full, untruncated text there
 * when the visible cell text is clipped — then falling back to the cell text.
 * Always cleaned (strips "Tender Type:" suffix + trailing "......").
 *
 * ASSUMPTION: IREPS populates title= on the title cell/anchor. The textContent
 * fallback works regardless; verify on a live IREPS listing.
 */
/** Same cell resolution as getTitleCellText(), but returns the raw (uncleaned)
 * text so callers can also pull the "Tender Type:" suffix before it's stripped. */
function getTitleCellRaw(row: Element, colIndex: number): string {
  if (colIndex < 0) return '';
  const cells = row.querySelectorAll('td');
  if (colIndex >= cells.length) return '';
  const cell = cells[colIndex];
  const attr =
    cell.getAttribute('title') ||
    cell.querySelector('a[title], [title]')?.getAttribute('title') ||
    '';
  return attr.trim() || extractText(cell);
}

function getTitleCellText(row: Element, colIndex: number): string {
  return cleanTitle(getTitleCellRaw(row, colIndex));
}

/**
 * IREPS - Detailed-card field (2026-07, best-effort). The title cell's raw
 * text carries an embedded "Tender Type: Open/Limited/Global/..." suffix that
 * cleanTitle() strips off before display (see its docstring above). Pull that
 * same suffix out as the bidType field instead of discarding it.
 */
function extractBidTypeFromTitleCell(raw: string): string {
  const m = raw.match(/Tender\s*Type\s*:\s*([^\n]{2,60}?)(?:[.…]{2,}\s*$|$)/i);
  return m ? m[1].trim() : '';
}

// --- Extraction Logic ---

function extractTenders(pageType: PageType): TenderData[] {
  if (pageType === 'unknown') return [];

  if (pageType === 'tender_detail') {
    return extractFromDetail();
  }

  return extractFromTables();
}

function extractFromTables(): TenderData[] {
  const tenders: TenderData[] = [];
  const tables = findTenderTables();

  if (tables.length === 0) {
    // Fallback: try ALL tables with enough rows
    console.log('[Ext] No tender tables found by header, trying all tables...');
    document.querySelectorAll('table').forEach(table => {
      const rows = table.querySelectorAll('tbody tr, tr');
      if (rows.length >= 3) {
        const extracted = extractFromTable(table as HTMLTableElement);
        tenders.push(...extracted);
      }
    });
  } else {
    for (const table of tables) {
      const extracted = extractFromTable(table);
      tenders.push(...extracted);
    }
  }

  // Also try any non-table listing patterns (divs, cards, lists)
  if (tenders.length === 0) {
    const cardTenders = extractFromCards();
    tenders.push(...cardTenders);
  }

  console.log(`[Ext] Extracted ${tenders.length} tenders total`);
  return tenders;
}

/**
 * Tolerant validation: the "T" icon (viewTndrDoc.gif) is the primary signal —
 * every real IREPS tender row has it, and nothing else does. This single check
 * eliminates form headers, dropdown text, and helper labels. We also require a
 * non-empty tender ID as a final sanity check.
 */
function isValidTenderRow(row: Element, tenderId: string, _title: string): { valid: boolean; reason?: string } {
  // PRIMARY: row must contain a "T" icon image (viewTndrDoc.gif)
  // CRITICAL: the image must be in the SAME table as this row, not a nested table.
  // Otherwise outer layout <tr>s that wrap the entire content match every T-icon
  // of every real tender row as a descendant.
  const rowTable = row.closest('table');
  const imgs = row.querySelectorAll('img');
  const hasTIcon = Array.from(imgs).some((img) => {
    const src = (img.getAttribute('src') || '').toLowerCase();
    if (!src.includes('viewtndrdoc')) return false;
    return img.closest('table') === rowTable;
  });
  if (!hasTIcon) return { valid: false, reason: 'no T icon in this row' };

  // SECONDARY: tender ID must be non-empty AND must not look like a header label
  const cleanId = (tenderId || '').trim();
  if (cleanId.length < 3) return { valid: false, reason: 'empty/too-short ID' };
  // Reject obvious header text masquerading as IDs
  if (/^(tender|search|deptt|department|please|select|status|portal|all\s)/i.test(cleanId)) {
    return { valid: false, reason: `header text "${cleanId.slice(0, 30)}"` };
  }

  return { valid: true };
}

function extractFromTable(table: HTMLTableElement): TenderData[] {
  const mapping = mapColumns(table);
  const tenders: TenderData[] = [];

  if (mapping) {
    console.log('[Ext] Column mapping:', JSON.stringify(mapping));
  } else {
    console.log('[Ext] No column mapping found, using positional heuristics');
  }

  let rejectedCount = 0;
  const rejectionReasons: Record<string, number> = {};

  // Get data rows (skip header row)
  const rows = table.querySelectorAll('tbody tr');
  const dataRows = rows.length > 0 ? rows : Array.from(table.querySelectorAll('tr')).slice(1);

  dataRows.forEach((row) => {
    try {
      const cells = row.querySelectorAll('td');
      if (cells.length < 3) return; // Skip rows with too few cells

      // Phase 7 — only emit rows the buyer flagged as eligible (blue tick / arrow)
      const isBlueTick = detectBlueTickIndicator(row as Element);
      if (blueTickGatingEnabled && !isBlueTick) {
        recordSkippedRow(row as Element);
        return;
      }

      let tenderNo = '';
      let title = '';
      let department = '';
      let organisation = '';
      let valueStr = '';
      let openingDate = '';
      let closingDate = '';
      let emdStr = '';
      let status = '';
      let bidTypeText = '';

      if (mapping) {
        // Use column mapping from headers
        tenderNo = getCellText(row, mapping.tenderNo);
        const titleRaw = getTitleCellRaw(row, mapping.title);
        title = cleanTitle(titleRaw);
        bidTypeText = extractBidTypeFromTitleCell(titleRaw);
        department = getCellText(row, mapping.department);
        organisation = getCellText(row, mapping.organisation);
        valueStr = getCellText(row, mapping.value);
        openingDate = getCellText(row, mapping.openingDate);
        closingDate = getCellText(row, mapping.closingDate);
        emdStr = getCellText(row, mapping.emd);
        status = getCellText(row, mapping.status);
      } else {
        // No mapping — use positional heuristics
        // Typical IREPS tables: SNo | TenderNo | Title | Dept | Zone | Value | ClosingDate | Status
        const texts = Array.from(cells).map(c => extractText(c));
        tenderNo = texts[1] || texts[0] || '';
        const titleRaw = texts[2] || texts[1] || '';
        title = cleanTitle(titleRaw);
        bidTypeText = extractBidTypeFromTitleCell(titleRaw);
        department = texts[3] || '';
        organisation = texts[4] || '';
        valueStr = texts.find(t => /[\d,]+\.?\d*/.test(t) && (t.includes('₹') || t.includes('Rs') || parseFloat(t.replace(/[,\s]/g, '')) > 1000)) || '';
        closingDate = texts.find(t => /\d{2}[\/-]\d{2}[\/-]\d{2,4}/.test(t) || /\d{2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(t)) || '';
      }

      // Must have some identifier
      if (!tenderNo && !title) return;

      // STRICT validation — reject form headers, dropdown text, helper labels, etc.
      const validation = isValidTenderRow(row as Element, tenderNo, title);
      if (!validation.valid) {
        rejectedCount++;
        const r = validation.reason || 'unknown';
        rejectionReasons[r] = (rejectionReasons[r] || 0) + 1;
        return;
      }

      // Find the viewNIT detail link (handles direct href, onclick popups, and generic links)
      const detailUrl = extractDetailLink(row as Element, tenderNo);
      const tenderId = tenderNo || (detailUrl ? new URL(detailUrl).pathname.split('/').pop() || '' : '') || title.slice(0, 40);

      if (!tenderId) return;

      // Extract ALL document links from the row and classify them for download
      const actionsDocs = extractActionsColumnDocs(row as Element);
      const seenUrls = new Set(actionsDocs.map(d => d.url));

      // Also classify general doc-like links from the row (PDFs, etc.)
      const allLinks = row.querySelectorAll('a[href]');
      for (const a of Array.from(allLinks)) {
        const href = (a as HTMLAnchorElement).href;
        const linkText = (a.textContent || '').trim();
        if (!href || seenUrls.has(href) || href.includes('javascript:')) continue;
        if (isFooterDocument(href, linkText)) continue;
        if (/\.(pdf|doc|docx|xls|xlsx)(\?|$)/i.test(href)
          || (/pdfdocs|viewnitpdf|download/i.test(href) && !href.includes('advancedSearch'))) {
          seenUrls.add(href);
          actionsDocs.push(classifyDocumentLink(href, linkText || 'Document'));
        }
      }

      const allDocUrls = actionsDocs.map(d => d.url);

      const tender: TenderData = {
        portal: PORTAL,
        tenderId: tenderId,
        title: title || tenderNo,
        department,
        organisation,
        description: title,
        estimatedValue: extractNumber(valueStr),
        currency: 'INR',
        openingDate: openingDate || null,
        closingDate: closingDate || null,
        emdAmount: extractNumber(emdStr),
        preBidDate: null,
        status: status || 'Open',
        documentLinks: allDocUrls,
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
        isEligibleIndicator: isBlueTick,
        eligibilityStatus: isBlueTick ? 'eligible' : 'unknown',
        detailUrl: detailUrl || undefined,
        classifiedDocuments: actionsDocs.length > 0 ? actionsDocs : undefined,
        nitDocumentLinks: actionsDocs.filter(d => d.type === 'nit').map(d => d.url),
        // Detailed-card field (2026-07, best-effort): tender type embedded in
        // the title cell (see extractBidTypeFromTitleCell). location/category
        // are omitted here — the listing table has no plausible column for
        // either; both are populated on the detail page below where available.
        bidType: bidTypeText || undefined,
      };

      tenders.push(tender);
    } catch (err) {
      console.warn('[Ext] Error extracting row:', err);
    }
  });

  console.log(`[Ext] Validation: ${tenders.length} valid, ${rejectedCount} rejected. Reasons:`, rejectionReasons);

  return tenders;
}

function extractFromCards(): TenderData[] {
  const tenders: TenderData[] = [];

  // Look for card/list-based tender layouts
  const cardSelectors = [
    '.tender-card', '.tender-item', '.tender-row', '.bid-item',
    '[class*="tender"]', '[class*="Tender"]',
    '.card[class*="result"]', '.list-group-item',
  ];

  for (const selector of cardSelectors) {
    try {
      const cards = document.querySelectorAll(selector);
      if (cards.length === 0) continue;

      cards.forEach(card => {
        const text = extractText(card);
        if (text.length < 20) return; // Skip tiny elements

        // Try to find a link with tender details
        const link = card.querySelector('a[href]') as HTMLAnchorElement;
        const href = link?.href || '';
        const linkText = link?.textContent?.trim() || '';

        // Try to find a tender number pattern in the text
        const tenderNoMatch = text.match(/(?:tender|bid|nit|case)\s*(?:no|number|id|#)?[:\s]*([A-Z0-9\-\/]+)/i);
        const tenderId = tenderNoMatch?.[1] || linkText.slice(0, 40) || text.slice(0, 40);

        if (!tenderId) return;

        const tender: TenderData = {
          portal: PORTAL,
          tenderId,
          title: linkText || text.slice(0, 200),
          department: '',
          organisation: '',
          description: text.slice(0, 500),
          estimatedValue: null,
          currency: 'INR',
          openingDate: null,
          closingDate: null,
          emdAmount: null,
          preBidDate: null,
          status: 'Open',
          documentLinks: href.includes('.pdf') ? [href] : [],
          sourceUrl: window.location.href,
          extractedAt: new Date().toISOString(),
        };

        tenders.push(tender);
      });

      if (tenders.length > 0) break; // Found tenders with this selector
    } catch {
      continue;
    }
  }

  return tenders;
}

// --- Actions column document extraction ---

function extractActionsColumnDocs(row: Element): DocumentLinkInfo[] {
  const cells = row.querySelectorAll('td');
  if (cells.length === 0) return [];

  // Actions is typically the last column
  const actionsCell = cells[cells.length - 1];
  if (!actionsCell) return [];

  const docs: DocumentLinkInfo[] = [];
  const seen = new Set<string>();

  // Scan ALL links in the row (not just Actions) for key document links
  const allLinks = row.querySelectorAll('a');

  for (const a of Array.from(allLinks)) {
    const anchor = a as HTMLAnchorElement;
    const href = anchor.href || '';
    const title = (anchor.getAttribute('title') || '').trim();
    const onclick = anchor.getAttribute('onclick') || '';

    // "T" icon = viewTndrDoc.gif image inside the link → NIT document
    const imgs = anchor.querySelectorAll('img');
    for (const img of Array.from(imgs)) {
      const src = (img.getAttribute('src') || '').toLowerCase();
      if (src.includes('viewtndrdoc') || src.includes('viewtenderdoc')) {
        // The href might be "#" with an onclick, or a direct link
        let docUrl = href && !href.endsWith('#') && !href.includes('javascript:') ? href : '';
        if (!docUrl && onclick) {
          const urlMatch = onclick.match(/(?:window\.open|open)\s*\(\s*['"]([^'"]+)['"]/);
          if (urlMatch) {
            try { docUrl = new URL(urlMatch[1], window.location.origin).href; } catch { docUrl = urlMatch[1]; }
          }
        }
        if (docUrl && !seen.has(docUrl)) {
          seen.add(docUrl);
          docs.push({ url: docUrl, label: title || 'Tender Document (NIT)', type: 'nit' });
        }
      }
    }

    // Corrigendum link: title="View Published Corrigendum" or href contains viewCorrigendum
    if (/corrigendum/i.test(title) || /viewcorrigendum/i.test(href)) {
      const docUrl = href && !href.includes('javascript:') ? href : '';
      if (docUrl && !seen.has(docUrl)) {
        seen.add(docUrl);
        docs.push({ url: docUrl, label: title || 'Corrigendum', type: 'corrigendum' });
      }
    }

    // Direct PDF/document links
    if (href && !href.includes('javascript:') && !seen.has(href)
      && (/\.(pdf|doc|docx|xls|xlsx)(\?|$)/i.test(href) || /download.*doc|nit.*publish/i.test(href))) {
      seen.add(href);
      docs.push(classifyDocumentLink(href, title || (anchor.textContent || '').trim() || 'Document'));
    }
  }

  return docs;
}

/**
 * Extract the viewNIT detail link from the tender number column.
 * IREPS tender numbers may be plain links, or onclick/javascript handlers
 * that open a popup window to nitPublish.do.
 */
function extractDetailLink(row: Element, tenderNo: string): string {
  const links = row.querySelectorAll('a');

  for (const a of Array.from(links)) {
    const anchor = a as HTMLAnchorElement;
    const href = anchor.href || '';
    const text = (anchor.textContent || '').trim();

    // Direct href to viewNIT / nitPublish page
    if (href && !href.includes('javascript:')
      && (href.includes('nitPublish') || href.includes('viewNIT')
        || href.includes('viewTender') || href.includes('tenderDetail'))) {
      return href;
    }

    // onclick handler that opens a popup — parse URL from it
    const onclick = anchor.getAttribute('onclick') || '';
    if (onclick) {
      const urlMatch = onclick.match(/(?:window\.open|open)\s*\(\s*['"]([^'"]+)['"]/);
      if (urlMatch) {
        const popupUrl = urlMatch[1];
        if (popupUrl.includes('nitPublish') || popupUrl.includes('viewNIT')
          || popupUrl.includes('tender')) {
          try {
            return new URL(popupUrl, window.location.origin).href;
          } catch {
            return popupUrl;
          }
        }
      }
    }
  }

  // Fallback: existing generic link detection
  for (const a of Array.from(links)) {
    const anchor = a as HTMLAnchorElement;
    const href = anchor.href || '';
    if (href && !href.includes('javascript:')
      && (href.includes('tender') || href.includes('detail') || href.includes('view'))) {
      return href;
    }
  }

  return '';
}

// --- Pagination detection ---

interface PaginationInfo {
  totalCount: number;
  currentPage: number;
  pageUrls: string[];
}

function extractPaginationInfo(): PaginationInfo | null {
  const bodyText = document.body?.innerText || '';

  const countMatch = bodyText.match(/tender\s*search\s*results?\s*(\d+)/i);
  if (!countMatch) return null;

  const totalCount = parseInt(countMatch[1]);
  if (!totalCount || totalCount <= 0) return null;

  // Detect current page from URL or default to 1
  const urlParams = new URLSearchParams(window.location.search);
  const currentPage = parseInt(urlParams.get('pageNo') || '1');

  // Extract actual page URLs from the pagination links on the page.
  // These contain the full URL with all search parameters (critical for
  // POST-based forms where the current URL has no query params).
  const pageUrlMap = new Map<number, string>();
  const allLinks = document.querySelectorAll('a');

  for (const a of Array.from(allLinks)) {
    const href = (a as HTMLAnchorElement).href;
    const text = (a.textContent || '').trim();
    if (!href || !text) continue;

    // Match numbered page links: "2", "3", etc.
    if (/^\d+$/.test(text) && /pageNo=\d+/i.test(href)) {
      const pageNum = parseInt(text);
      if (pageNum > 1 && !pageUrlMap.has(pageNum)) {
        pageUrlMap.set(pageNum, href);
      }
    }

    // Match "next" link
    if (/^next$/i.test(text) && /pageNo=\d+/i.test(href)) {
      const nextMatch = href.match(/pageNo=(\d+)/i);
      if (nextMatch) {
        const nextPage = parseInt(nextMatch[1]);
        if (!pageUrlMap.has(nextPage)) pageUrlMap.set(nextPage, href);
      }
    }
  }

  // If we found page links, use them as a template to construct all page URLs
  const pageUrls: string[] = [];
  if (pageUrlMap.size > 0) {
    // Use the first found URL as a template — replace pageNo=N
    const templateEntry = [...pageUrlMap.entries()][0];
    const templateUrl = templateEntry[1];
    const templatePage = templateEntry[0];

    // Estimate total pages
    const perPage = Math.max(totalCount > 0 ? Math.ceil(totalCount / Math.max(currentPage === 1 ? 113 : 100, 10)) : 10, 1);
    const totalPages = Math.min(Math.ceil(totalCount / perPage), 100);

    for (let p = 2; p <= totalPages; p++) {
      // Use existing URL if we have it, otherwise construct from template
      if (pageUrlMap.has(p)) {
        pageUrls.push(pageUrlMap.get(p)!);
      } else {
        pageUrls.push(templateUrl.replace(`pageNo=${templatePage}`, `pageNo=${p}`));
      }
    }
  }

  if (pageUrls.length === 0) return null;

  console.log(`[Ext] Pagination URLs extracted: ${pageUrls.length} pages (sample: ${pageUrls[0]?.slice(0, 80)}...)`);
  return { totalCount, currentPage, pageUrls };
}

// --- Diagnostic logging ---

function logFirstRowActions() {
  const tables = findTenderTables();
  if (tables.length === 0) return;

  const rows = tables[0].querySelectorAll('tbody tr, tr');
  const sampleCount = Math.min(3, rows.length);
  for (let i = 0; i < sampleCount; i++) {
    const cells = rows[i].querySelectorAll('td');
    if (cells.length === 0) continue;
    const actionsCell = cells[cells.length - 1];
    console.log(`[Ext] Row ${i} Actions HTML (${actionsCell?.innerHTML?.length || 0} chars):`,
      actionsCell?.innerHTML?.slice(0, 500) || '(empty)');
    // Log all images in the row for blue-arrow debugging
    const imgs = rows[i].querySelectorAll('img');
    if (imgs.length > 0) {
      console.log(`[Ext] Row ${i} images:`,
        Array.from(imgs).map(img =>
          `src="${img.getAttribute('src')}" alt="${img.getAttribute('alt')}" title="${img.getAttribute('title')}"`
        ).join(' | ')
      );
    }
  }
}

function extractFromDetail(): TenderData[] {
  // On detail pages, scrape all visible key-value pairs + rich data
  const text = document.body.innerText;

  const getValue = (patterns: RegExp[]): string => {
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return match[1].trim();
    }
    return '';
  };

  const tenderNo = getValue([
    /tender\s*(?:no|number|id|#)[:\s]*([A-Z0-9\-\/]+)/i,
    /nit\s*(?:no|number)[:\s]*([A-Z0-9\-\/]+)/i,
    /case\s*(?:no|number)[:\s]*([A-Z0-9\-\/]+)/i,
  ]);

  if (!tenderNo) return [];

  // Use shared detail extractor for rich fields
  const detailData = extractDetailPageData(document);
  const allDocs = extractAllDocumentLinks(document);
  const nitDocs = allDocs.filter(d => d.type === 'nit').map(d => d.url);
  const corrigendaDocs = allDocs.filter(d => d.type === 'corrigendum').map(d => d.url);
  const amendmentDocs = allDocs.filter(d => d.type === 'amendment').map(d => d.url);

  const tender: TenderData = {
    portal: PORTAL,
    tenderId: tenderNo,
    // Full title lives ONLY on the detail page (the listing is 60-char capped by
    // IREPS). pickTenderTitle pulls the labelled, untruncated title; the old
    // inline regex is kept as a secondary fallback. Never fall back to
    // document.title — on IREPS that is login/page chrome, not the tender title.
    title: pickTenderTitle(text)
      || cleanTitle(getValue([/(?:title|subject|work|description)[:\s]*(.+?)(?:\n|$)/i])),
    department: getValue([/(?:department|division|branch)[:\s]*(.+?)(?:\n|$)/i]),
    organisation: getValue([/(?:zone|railway|organisation|organization)[:\s]*(.+?)(?:\n|$)/i]),
    description: getValue([/(?:scope|description|brief)[:\s]*(.+?)(?:\n|$)/i]),
    estimatedValue: extractNumber(getValue([/(?:estimated|tender)\s*value[:\s]*([\d,₹Rs\s.]+)/i, /value[:\s]*([\d,₹Rs\s.]+)/i])),
    currency: 'INR',
    openingDate: getValue([/(?:opening|start|publish)\s*date[:\s]*(.+?)(?:\n|$)/i]) || null,
    closingDate: getValue([/(?:closing|due|last|end|deadline)\s*date[:\s]*(.+?)(?:\n|$)/i]) || null,
    emdAmount: extractNumber(getValue([/emd[:\s]*([\d,₹Rs\s.]+)/i, /earnest\s*money[:\s]*([\d,₹Rs\s.]+)/i])),
    preBidDate: getValue([/pre[\s-]?bid[:\s]*(.+?)(?:\n|$)/i]) || null,
    status: getValue([/status[:\s]*(.+?)(?:\n|$)/i]) || 'Open',
    documentLinks: allDocs.map(d => d.url),
    sourceUrl: window.location.href,
    extractedAt: new Date().toISOString(),
    // Deep scrape fields
    isDetailExtracted: true,
    detailUrl: window.location.href,
    fullDescription: detailData.fullDescription,
    eligibilityCriteria: detailData.eligibilityCriteria,
    technicalSpecifications: detailData.technicalSpecifications,
    evaluationCriteria: detailData.evaluationCriteria,
    performanceGuarantee: detailData.performanceGuarantee,
    performanceGuaranteePercent: detailData.performanceGuaranteePercent,
    preBidMeetingLocation: detailData.preBidMeetingLocation,
    deliveryLocation: detailData.deliveryLocation,
    deliveryTimeline: detailData.deliveryTimeline,
    buyerContactName: detailData.buyerContactName,
    buyerContactEmail: detailData.buyerContactEmail,
    buyerContactPhone: detailData.buyerContactPhone,
    numberOfAmendments: detailData.numberOfAmendments,
    nitDocumentLinks: nitDocs,
    corrigendaLinks: corrigendaDocs,
    amendmentLinks: amendmentDocs,
    classifiedDocuments: allDocs,
    // Detailed-card fields (2026-07, best-effort). detailData.deliveryLocation
    // is already extracted by the shared detail extractor (delivery/place/
    // destination/consignee regex). bidType reuses the same "Tender Type:"
    // label the listing title cell embeds. category is omitted — no plausible
    // commodity/category field found on the IREPS detail page.
    location: detailData.deliveryLocation || undefined,
    bidType: getValue([/tender\s*type[:\s]*(.+?)(?:\n|$)/i]) || undefined,
  };

  return [tender];
}

// --- Phase 7: Blue-tick indicator detection ---

/**
 * IREPS visually flags rows the buyer pre-qualified for the logged-in vendor
 * with a small blue tick / arrow icon (or coloured row background). This
 * function returns true ONLY when one of those positive signals is present.
 *
 * Patterns are loaded from config/selectors.json so they can be OTA-tuned
 * via /api/extension/selectors/ireps without re-publishing the extension.
 */
function detectBlueTickIndicator(row: Element): boolean {
  const html = row.innerHTML.toLowerCase();

  // Negative signals — don't be tricked by "not eligible" rows
  if (/not[_\s-]?eligible|reject|disqualif/.test(html)) return false;

  // 1. Image src match
  const imgs = row.querySelectorAll('img');
  for (const img of Array.from(imgs)) {
    const src = (img.getAttribute('src') || '').toLowerCase();
    if (!src) continue;
    if (eligibilityConfig.imageSrcPatterns.some((p) => src.includes(p.toLowerCase()))) {
      return true;
    }
    const alt = (img.getAttribute('alt') || '').toLowerCase();
    if (eligibilityConfig.ariaLabelPatterns.some((p) => alt.includes(p.toLowerCase()))) {
      return true;
    }
  }

  // 2. CSS class fragment match (on the row itself or any descendant)
  const classBag = (row.getAttribute('class') || '').toLowerCase();
  if (eligibilityConfig.cssClassFragments.some((c) => classBag.includes(c.toLowerCase()))) {
    return true;
  }
  for (const frag of eligibilityConfig.cssClassFragments) {
    if (row.querySelector(`[class*="${frag}"]`)) return true;
  }

  // 3. Inline-style colour match
  const styleAttrs = [row.getAttribute('style') || ''];
  row.querySelectorAll('[style]').forEach((el) => styleAttrs.push(el.getAttribute('style') || ''));
  const style = styleAttrs.join(' ').toLowerCase();
  if (eligibilityConfig.colorHexList.some((hex) => style.includes(hex.toLowerCase()))) {
    return true;
  }

  // 4. ARIA label / title match on row or descendants
  const labelTargets = [row, ...Array.from(row.querySelectorAll('[aria-label], [title]'))];
  for (const el of labelTargets) {
    const aria = ((el as Element).getAttribute('aria-label') || '').toLowerCase();
    const title = ((el as Element).getAttribute('title') || '').toLowerCase();
    if (
      eligibilityConfig.ariaLabelPatterns.some(
        (p) => aria.includes(p.toLowerCase()) || title.includes(p.toLowerCase()),
      )
    ) {
      return true;
    }
  }

  return false;
}

function recordSkippedRow(row: Element) {
  if (skippedRowSamples.length >= MAX_SKIPPED_ROW_SAMPLES) return;
  // Trim to keep storage payload small
  const html = (row.outerHTML || '').slice(0, 1500);
  skippedRowSamples.push(html);
}

// --- DOM Observation ---

function waitForContent(callback: () => void, timeout = 10000) {
  // Check for existing content
  const hasTables = document.querySelectorAll('table tr').length > 2;
  const hasCards = document.querySelectorAll('[class*="tender"], [class*="bid"]').length > 0;

  if (hasTables || hasCards) {
    // Small delay for any remaining rendering
    setTimeout(callback, 500);
    return;
  }

  // Watch for dynamic content
  const observer = new MutationObserver((_mutations, obs) => {
    const tables = document.querySelectorAll('table tr');
    const cards = document.querySelectorAll('[class*="tender"], [class*="bid"]');
    if (tables.length > 2 || cards.length > 0) {
      obs.disconnect();
      setTimeout(callback, 500);
    }
  });

  observer.observe(document.body || document.documentElement, { childList: true, subtree: true });

  // Timeout fallback — try extraction anyway
  setTimeout(() => {
    observer.disconnect();
    callback();
  }, timeout);
}

// --- Message Passing ---

function sendTendersToBackground(tenders: TenderData[], pageType: PageType) {
  const message: TenderDataMessage = {
    type: 'TENDER_DATA_EXTRACTED',
    portal: PORTAL,
    payload: {
      tenders,
      pageType,
      pageUrl: window.location.href,
    },
  };

  chrome.runtime.sendMessage(message, (response) => {
    if (chrome.runtime.lastError) {
      console.warn('[Ext] Failed to send to background:', chrome.runtime.lastError.message);
    } else {
      console.log(`[Ext] Sent ${tenders.length} tenders to background`);
    }
  });
}

// --- Listen for messages from popup and service worker ---
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'TRIGGER_SCRAPE') {
    console.log('[Ext] Manual scrape triggered on IREPS page');
    const limit = Math.max(1, Math.min(100, message.payload?.tenderLimit || message.pageCount || 10));
    // Disable blue-tick gating for manual scrapes on search results
    if (detectPageType() === 'search_results') blueTickGatingEnabled = false;
    const pageType = detectPageType();
    let tenders = extractTenders(pageType);
    if (tenders.length > limit) {
      console.log(`[Ext] Capping to ${limit} of ${tenders.length} valid tenders`);
      tenders = tenders.slice(0, limit);
    }
    if (tenders.length > 0) {
      // Send with manual flag to skip local dedup
      const msg: TenderDataMessage = {
        type: 'TENDER_DATA_EXTRACTED',
        portal: PORTAL,
        payload: { tenders, pageType, pageUrl: window.location.href },
      };
      (msg as any).manual = true;
      chrome.runtime.sendMessage(msg);
      sendResponse({ extracted: tenders.length });
    } else {
      sendResponse({ extracted: 0 });
    }
  } else if (message.type === 'EXTRACT_DETAIL') {
    // Deep scrape: extract rich data from this detail page
    console.log('[Ext] EXTRACT_DETAIL received on IREPS page');
    const detailData = extractDetailPageData(document);
    const allDocs = extractAllDocumentLinks(document);

    // Also try to extract tender ID from the page
    const text = document.body.innerText;
    const tenderNoMatch = text.match(/tender\s*(?:no|number|id|#)[:\s]*([A-Z0-9\-\/]+)/i)
      || text.match(/nit\s*(?:no|number)[:\s]*([A-Z0-9\-\/]+)/i)
      || text.match(/case\s*(?:no|number)[:\s]*([A-Z0-9\-\/]+)/i);

    sendResponse({
      data: {
        ...detailData,
        tenderId: tenderNoMatch?.[1] || '',
        documentLinks: allDocs.map(d => d.url),
        nitDocumentLinks: allDocs.filter(d => d.type === 'nit').map(d => d.url),
        corrigendaLinks: allDocs.filter(d => d.type === 'corrigendum').map(d => d.url),
        amendmentLinks: allDocs.filter(d => d.type === 'amendment').map(d => d.url),
        classifiedDocuments: allDocs,
        isDetailExtracted: true,
        detailUrl: window.location.href,
      },
    });
  }
  return true;
});

// --- Start ---
init();
