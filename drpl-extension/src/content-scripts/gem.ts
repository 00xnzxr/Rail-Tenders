// ============================================================
// DRPL Extension - GeM Content Script
// Extracts bid/tender data from GeM portal pages
// GeM uses React — adaptive extraction with DOM observation
// ============================================================

import { extractText, extractNumber, extractHref, cleanTitle, pickLabeledField } from '../utils/selectors';
import { extractGemItemsTitle } from '../utils/gem-title';
import { TenderData, TenderDataMessage, PageType } from '../utils/types';
import { extractDetailPageData, extractAllDocumentLinks } from '../utils/detail-extractor';
import {
  attachGemAutoSearchListener,
  isAdvanceSearchPage,
  isBidnextHost,
} from './gem-search-driver';

const PORTAL = 'gem' as const;
const BIDNEXT_ADVANCE_SEARCH_URL =
  'https://bidnext.gem.gov.in/bidnext/#WORKSPACE_ID=ADVANCE_SEARCH_WS';
const REDIRECT_FLAG_KEY = 'gemBidnextAutoRedirectDone';
const AUTO_RUN_FLAG_KEY = 'gemBidnextAutoRunDone';

/** Once-per-browser-session redirect: if the user lands on the bidnext app
 * (post-login) and isn't already on the Advance Tender Search workspace, jump
 * them there so the search driver can run. Uses chrome.storage.session so the
 * flag resets when Chrome restarts. */
async function maybeAutoRedirectToAdvanceSearch(): Promise<boolean> {
  if (!isBidnextHost()) return false;
  if (isAdvanceSearchPage()) return false;

  const store: chrome.storage.StorageArea | undefined =
    (chrome.storage as any).session ?? chrome.storage.local;
  if (!store) return false;

  try {
    const existing = await store.get(REDIRECT_FLAG_KEY);
    if (existing && (existing as any)[REDIRECT_FLAG_KEY]) return false;
    await store.set({ [REDIRECT_FLAG_KEY]: true });
  } catch (err) {
    console.warn('[Ext] Auto-redirect flag check failed; skipping redirect', err);
    return false;
  }

  console.log('[Ext] Auto-redirecting to bidnext Advance Tender Search');
  // Same-origin hash change keeps the SPA mounted. assign() also reloads when
  // the path differs, which we want when landing on /bidnext/home.
  window.location.assign(BIDNEXT_ADVANCE_SEARCH_URL);
  return true;
}

/** Once per browser session, after we've landed on the advance-search
 * workspace, ask the service worker to kick off the keyword-driven search.
 * The service worker fetches the latest scope profile and posts
 * RUN_GEM_AUTO_SEARCH to this tab's content script. */
async function maybeAutoRunSearch(): Promise<void> {
  if (!isAdvanceSearchPage()) return;
  const store: chrome.storage.StorageArea | undefined =
    (chrome.storage as any).session ?? chrome.storage.local;
  if (!store) return;
  try {
    const existing = await store.get(AUTO_RUN_FLAG_KEY);
    if (existing && (existing as any)[AUTO_RUN_FLAG_KEY]) return;
    await store.set({ [AUTO_RUN_FLAG_KEY]: true });
  } catch (err) {
    console.warn('[Ext] Auto-run flag check failed; skipping auto-run', err);
    return;
  }
  // Give the advance-search workspace a beat to mount before driver pokes at the form.
  setTimeout(() => {
    chrome.runtime.sendMessage({ type: 'START_GEM_AUTO_SEARCH' }, (resp) => {
      if (chrome.runtime.lastError) {
        console.warn('[Ext] START_GEM_AUTO_SEARCH dispatch failed', chrome.runtime.lastError.message);
      } else {
        console.log('[Ext] Auto-search dispatched', resp);
      }
    });
  }, 1500);
}

function init() {
  console.log('[Ext] GeM content script loaded on:', window.location.href);

  // Attach the auto-search driver listener whenever we're on the bidnext app —
  // the workspace hash may not be present yet when the script loads, so we
  // listen broadly and react when the popup (or chained flow) triggers a run.
  if (isBidnextHost()) {
    attachGemAutoSearchListener();
  }

  // First load after login — redirect into the advance-search workspace.
  // The redirect fires asynchronously; downstream init logic below short-
  // circuits when we're on the advance-search page anyway.
  void maybeAutoRedirectToAdvanceSearch();

  if (isAdvanceSearchPage()) {
    // Kick off the keyword-driven search exactly once per session.
    void maybeAutoRunSearch();
    // Don't run passive scrape on the bare advance-search form.
    return;
  }

  const url = window.location.href;
  // Skip homepage
  if (url === 'https://gem.gov.in/' || url === 'https://mkp.gem.gov.in/') {
    console.log('[Ext] On GeM homepage, skipping');
    return;
  }

  // GeM is React-based — wait for hydration before extracting.
  waitForReactRender(() => {
    const pageType = detectPageType();
    console.log('[Ext] GeM page type:', pageType);

    if (pageType === 'unknown') return;

    // IMPORTANT: do NOT passively auto-scrape GeM *listing* pages.
    //
    // The bidnext "Advance Tender Search" results are scraped by the dedicated
    // driver (gem-search-driver.ts), which anchors on the labelled
    // "Tender Title:" / "Railway Tender Number:" fields and only emits real
    // tenders. The generic listing extractor below (findBidCards/extractFrom*)
    // is intentionally permissive and, on marketplace / mkp.gem.gov.in pages,
    // scrapes non-tender rows — pushing page chrome ("GeM | Bidding"), field
    // labels ("Department Name And Address: ...") and quantities
    // ("Quantity: 1800") into the title/value columns. Disabling the passive
    // listing path eliminates that garbage at the source.
    //
    // Detail pages are still auto-extracted (rich, single-record, reliable),
    // and a user can still force a listing scrape via the popup
    // (TRIGGER_SCRAPE), which now runs through the same strict validation.
    if (pageType !== 'tender_detail') {
      console.log('[Ext] Skipping passive listing scrape — use Advance Search driver for tenders');
      return;
    }

    const tenders = extractBids(pageType);
    if (tenders.length > 0) {
      sendTendersToBackground(tenders, pageType);
    } else {
      console.log('[Ext] No bids extracted from this page');
    }
  });
}

// --- Page Type Detection (adaptive) ---

function detectPageType(): PageType {
  const url = window.location.href.toLowerCase();

  if (url.includes('bid-detail') || url.includes('biddetail') || url.includes('/bid/')) {
    return 'tender_detail';
  }
  if (url.includes('bid-search') || url.includes('bidsearch') || url.includes('search') || url.includes('list')) {
    return 'search_results';
  }
  if (url.includes('dashboard')) {
    return 'seller_dashboard';
  }
  if (url.includes('bid') || url.includes('tender')) {
    return 'tender_listing';
  }

  // Check page content
  const bodyText = document.body?.innerText?.toLowerCase() || '';
  if (bodyText.includes('bid') || bodyText.includes('tender') || bodyText.includes('procurement')) {
    return 'tender_listing';
  }

  return 'unknown';
}

// --- Extraction Logic ---

// A GeM bid/tender number always looks like GEM/<year>/B/<digits>. We require a
// real one before emitting any listing row — this is the single strongest
// signal that a scraped block is an actual tender rather than page chrome, a
// product card, or a stray field label.
const GEM_BID_NO_RE = /\bGEM\/\d{4}\/B\/\d+\b/i;

// Field labels / page chrome that must never be accepted as a tender title.
// These leak in when the generic extractor grabs the first labelled value it
// finds (e.g. "Department Name And Address: ...", "Quantity: 1800") or falls
// back to a raw text slice ("GeM | Bidding").
const BAD_TITLE_RE = /^(gem\s*\|\s*bidding|quantity\s*:|department\s+name\s+and\s+address\s*:|total\s+quantity|item\s+category\s*:)/i;

/**
 * Gate a listing row: only keep it if it has a real GeM bid number AND a title
 * that is a plausible tender name (not a field label, page-chrome string, or a
 * bare "Quantity: N"). Returns true when the row should be emitted.
 */
function isValidListingTender(t: TenderData): boolean {
  if (!t.tenderId || !GEM_BID_NO_RE.test(t.tenderId)) return false;
  const title = (t.title || '').trim();
  if (!title) return false;
  if (BAD_TITLE_RE.test(title)) return false;
  // A title that is just the bid number back again carries no information.
  if (title === t.tenderId) return false;
  // Guard against quantity / pure-number titles ("1800", "Quantity: 1").
  if (/^(quantity\s*:?\s*)?\d[\d,]*$/i.test(title)) return false;
  return true;
}

function extractBids(pageType: PageType): TenderData[] {
  if (pageType === 'tender_detail') {
    return extractFromBidDetail();
  }
  return extractFromListing();
}

function extractFromListing(): TenderData[] {
  const tenders: TenderData[] = [];

  // Strategy 1: Find cards/rows with bid data
  const cards = findBidCards();
  if (cards.length > 0) {
    console.log(`[Ext] Found ${cards.length} bid cards`);
    cards.forEach(card => {
      const tender = extractFromCard(card);
      if (tender) tenders.push(tender);
    });
  }

  // Strategy 2: Try table-based extraction
  if (tenders.length === 0) {
    const tableTenders = extractFromTables();
    tenders.push(...tableTenders);
  }

  // Strategy 3: Try extracting from any structured data
  if (tenders.length === 0) {
    const jsonTenders = extractFromJsonLd();
    tenders.push(...jsonTenders);
  }

  // Strict gate: drop anything that isn't a real tender (no valid GEM/…/B/…
  // number, or a junk/field-label title). This is what prevents page chrome
  // ("GeM | Bidding"), field labels and quantities from reaching the backend.
  const valid = tenders.filter(isValidListingTender);
  const dropped = tenders.length - valid.length;
  if (dropped > 0) {
    console.log(`[Ext] Dropped ${dropped} non-tender row(s) during validation`);
  }
  console.log(`[Ext] Extracted ${valid.length} valid bids from GeM`);
  return valid;
}

function findBidCards(): Element[] {
  // Try various card selectors that GeM might use
  const selectorPatterns = [
    // React class-based patterns (GeM uses Material UI / custom React)
    '[class*="bid-card"]', '[class*="BidCard"]', '[class*="bidCard"]',
    '[class*="tender-card"]', '[class*="TenderCard"]',
    '[class*="result-card"]', '[class*="ResultCard"]',
    '[class*="search-result"]', '[class*="SearchResult"]',
    // Data attribute patterns
    '[data-bid-id]', '[data-tender-id]',
    // Generic card patterns
    '.card', '.MuiCard-root', '.MuiPaper-root',
    // Table row patterns
    'tr[class*="bid"]', 'tr[class*="Bid"]',
    // List patterns
    '.list-group-item', 'li[class*="bid"]', 'li[class*="result"]',
  ];

  for (const selector of selectorPatterns) {
    try {
      const elements = document.querySelectorAll(selector);
      if (elements.length >= 1) {
        // Verify these actually contain bid data
        const firstText = elements[0].textContent?.toLowerCase() || '';
        if (firstText.includes('bid') || firstText.includes('gem') || firstText.includes('buyer') ||
            firstText.includes('quantity') || firstText.includes('delivery') || firstText.length > 50) {
          return Array.from(elements);
        }
      }
    } catch {
      continue;
    }
  }

  return [];
}

function extractFromCard(card: Element): TenderData | null {
  const text = card.textContent || '';
  if (text.trim().length < 20) return null;

  // Extract bid number from text
  const bidNo = extractBidNumber(card, text);
  if (!bidNo) return null;

  // Title: on GeM `all-bids` the identifying text is the "Items:" field (the bid
  // category), and GeM CLIPS its visible text to 30 chars + "..." while keeping
  // the full value in the anchor's data-content attribute. extractGemItemsTitle()
  // reads that full value. Fall back to an explicit "Tender Title"/"Title" label
  // for other GeM layouts. If neither is found, leave it empty and let
  // isValidListingTender() drop the row rather than inventing a title (the old
  // label-only heuristic pulled in "Department Name And Address: ...").
  const title =
    extractGemItemsTitle(card) ||
    cleanTitle(pickLabeledField(text, 'Tender\\s*Title') || pickLabeledField(text, 'Title'));
  const buyer = extractFieldByLabel(card, ['buyer', 'organization', 'organisation', 'ministry', 'department']);
  const category = extractFieldByLabel(card, ['category', 'type', 'classification']);
  const startDate = extractFieldByLabel(card, ['start', 'publish', 'open']);
  const endDate = extractFieldByLabel(card, ['end', 'close', 'due', 'last', 'deadline']);
  // Estimated value: only a real value/amount field. Never fall back to
  // quantity — quantity (e.g. 1800) is not a rupee value and was wrongly
  // surfacing in the Est. Value column.
  const value = extractFieldByLabel(card, ['value', 'amount', 'estimated', 'budget']);
  // Detailed-card fields (2026-07, best-effort) — same label-scan helper used
  // above; returns '' (→ undefined below) when the card doesn't expose them.
  const locationText = extractFieldByLabel(card, ['delivery location', 'consignee location', 'destination', 'location']);
  const bidTypeText = extractFieldByLabel(card, ['bid type', 'type of bid']);

  // Find detail link
  const link = card.querySelector('a[href*="bid"], a[href*="detail"], a[href]') as HTMLAnchorElement;
  const detailUrl = link?.href || '';

  return {
    portal: PORTAL,
    // Leave title empty when no labelled tender title was found — the
    // validation gate (isValidListingTender) will drop the row instead of
    // letting a bid number or junk slice stand in as the title.
    tenderId: bidNo,
    title,
    department: category || '',
    organisation: buyer || '',
    description: title || '',
    estimatedValue: extractNumber(value || ''),
    currency: 'INR',
    openingDate: startDate || null,
    closingDate: endDate || null,
    emdAmount: null,
    preBidDate: null,
    status: 'Open',
    documentLinks: [],
    sourceUrl: window.location.href,
    extractedAt: new Date().toISOString(),
    detailUrl: detailUrl || undefined,
    // Detailed-card fields — undefined when the DOM doesn't expose them.
    location: locationText || undefined,   // buyer address / delivery location if scraped
    bidType: bidTypeText || undefined,     // GeM "Bid Type" field if present
    category: category || undefined,       // same category-labelled text already used for `department`
  };
}

function extractBidNumber(card: Element, text: string): string {
  // Try data attributes first
  const bidAttr = card.getAttribute('data-bid-id') || card.getAttribute('data-id');
  if (bidAttr) return bidAttr;

  // Try text patterns
  const patterns = [
    /GEM\/\d{4}\/B\/\d+/i,              // GEM/2026/B/12345
    /bid\s*(?:no|number|id|#)?[:\s]*([A-Z0-9\-\/]+)/i,
    /(?:ra|ge)[m]\s*[\/-]\s*([A-Z0-9\-\/]+)/i,
  ];

  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return match[0] || match[1];
  }

  // Try finding bid number in links
  const links = card.querySelectorAll('a[href]');
  for (const link of links) {
    const href = (link as HTMLAnchorElement).href;
    const urlMatch = href.match(/bid[_-]?(?:id|no)?[=\/]([A-Z0-9\-\/]+)/i);
    if (urlMatch) return urlMatch[1];
  }

  return '';
}

function extractFieldByLabel(container: Element, keywords: string[]): string {
  // Strategy 1: Find label-value pairs (common in React UIs)
  const allElements = container.querySelectorAll('*');

  for (const el of allElements) {
    const text = el.textContent?.trim().toLowerCase() || '';
    const isLabel = keywords.some(kw => text.includes(kw));

    if (isLabel && text.length < 50) {
      // Value is likely the next sibling or child
      const nextSibling = el.nextElementSibling;
      if (nextSibling) {
        const value = nextSibling.textContent?.trim() || '';
        if (value.length > 0 && value.length < 500) return value;
      }
      // Or parent's next child
      const parent = el.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children);
        const idx = siblings.indexOf(el);
        if (idx >= 0 && idx + 1 < siblings.length) {
          const value = siblings[idx + 1].textContent?.trim() || '';
          if (value.length > 0 && value.length < 500) return value;
        }
      }
    }
  }

  // Strategy 2: Regex in full text
  const fullText = container.textContent || '';
  for (const kw of keywords) {
    const pattern = new RegExp(`${kw}[:\\s]+([^\\n]{3,100})`, 'i');
    const match = fullText.match(pattern);
    if (match) return match[1].trim();
  }

  return '';
}

function extractFromTables(): TenderData[] {
  const tenders: TenderData[] = [];
  const tables = document.querySelectorAll('table');

  tables.forEach(table => {
    const rows = table.querySelectorAll('tbody tr, tr');
    if (rows.length < 2) return;

    // Check if table has bid-related headers
    const headerText = Array.from(table.querySelectorAll('thead th, th'))
      .map(th => th.textContent?.trim() || '')
      .join(' | ')
      .toLowerCase();

    if (!headerText.includes('bid') && !headerText.includes('tender') &&
        !headerText.includes('gem') && !headerText.includes('buyer')) return;

    rows.forEach(row => {
      const cells = row.querySelectorAll('td');
      if (cells.length < 3) return;

      const texts = Array.from(cells).map(c => extractText(c));
      const fullText = texts.join(' ');

      // Find bid number in row
      const bidNo = fullText.match(/GEM\/\d{4}\/B\/\d+/i)?.[0] ||
                    texts.find(t => /^[A-Z]{2,}[\/-]\d+/i.test(t)) || '';

      if (!bidNo) return;

      tenders.push({
        portal: PORTAL,
        tenderId: bidNo,
        title: texts[1] || texts[0] || bidNo,
        department: '',
        organisation: texts.find(t => /ministry|dept|corporation|limited|ltd/i.test(t)) || '',
        description: texts.slice(0, 3).join(' '),
        estimatedValue: null,
        currency: 'INR',
        openingDate: null,
        closingDate: texts.find(t => /\d{2}[\/-]\d{2}[\/-]\d{2,4}/.test(t)) || null,
        emdAmount: null,
        preBidDate: null,
        status: 'Open',
        documentLinks: [],
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
      });
    });
  });

  return tenders;
}

function extractFromJsonLd(): TenderData[] {
  const tenders: TenderData[] = [];
  const scripts = document.querySelectorAll('script[type="application/ld+json"]');

  scripts.forEach(script => {
    try {
      const data = JSON.parse(script.textContent || '');
      const items = Array.isArray(data) ? data : [data];

      items.forEach(item => {
        if (item['@type'] === 'Product' || item['@type'] === 'Offer') {
          const id = item.identifier || item.sku || item.name?.slice(0, 40) || '';
          if (!id) return;

          tenders.push({
            portal: PORTAL,
            tenderId: `gem-${id}`,
            title: item.name || '',
            department: item.category || '',
            organisation: item.brand?.name || item.seller?.name || '',
            description: item.description || item.name || '',
            estimatedValue: item.offers?.price ? parseFloat(item.offers.price) : null,
            currency: item.offers?.priceCurrency || 'INR',
            openingDate: null,
            closingDate: null,
            emdAmount: null,
            preBidDate: null,
            status: 'Open',
            documentLinks: [],
            sourceUrl: window.location.href,
            extractedAt: new Date().toISOString(),
          });
        }
      });
    } catch {
      // Invalid JSON-LD, skip
    }
  });

  return tenders;
}

function extractFromBidDetail(): TenderData[] {
  const text = document.body?.innerText || '';

  // Find bid number
  const bidNoMatch = text.match(/GEM\/\d{4}\/B\/\d+/i) ||
                     text.match(/bid\s*(?:no|number|id|#)?[:\s]*([A-Z0-9\-\/]+)/i);
  const bidNo = bidNoMatch?.[0] || bidNoMatch?.[1] || '';
  if (!bidNo) return [];

  const getValue = (patterns: RegExp[]): string => {
    for (const p of patterns) {
      const m = text.match(p);
      if (m) return m[1]?.trim() || m[0]?.trim() || '';
    }
    return '';
  };

  // Use shared detail extractor for rich fields
  const detailData = extractDetailPageData(document);
  const allDocs = extractAllDocumentLinks(document);

  // Detailed-card fields (2026-07, best-effort). `categoryText` reuses the same
  // category/type label already read for `department` below; `bidTypeText`
  // looks for a more specific "Bid Type" label (e.g. Single/Two Packet System).
  const categoryText = getValue([/(?:category|type)[:\s]*(.+?)(?:\n|$)/i]);
  const bidTypeText = getValue([/(?:bid|tender)\s*type[:\s]*(.+?)(?:\n|$)/i]);

  return [{
    portal: PORTAL,
    tenderId: bidNo,
    title: getValue([/(?:item|product|description)[:\s]*(.+?)(?:\n|$)/i]) || document.title,
    department: categoryText,
    organisation: getValue([/(?:buyer|ministry|department|organisation)[:\s]*(.+?)(?:\n|$)/i]),
    description: getValue([/(?:description|specification|spec)[:\s]*(.+?)(?:\n|$)/i]),
    estimatedValue: extractNumber(getValue([/(?:estimated|total)\s*(?:value|cost|amount)[:\s]*([\d,₹Rs\s.]+)/i])),
    currency: 'INR',
    openingDate: getValue([/(?:start|publish|open)\s*date[:\s]*(.+?)(?:\n|$)/i]) || null,
    closingDate: getValue([/(?:end|close|due|last|deadline)\s*date[:\s]*(.+?)(?:\n|$)/i]) || null,
    emdAmount: null,
    preBidDate: null,
    status: 'Open',
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
    deliveryLocation: detailData.deliveryLocation,
    deliveryTimeline: detailData.deliveryTimeline,
    buyerContactName: detailData.buyerContactName,
    buyerContactEmail: detailData.buyerContactEmail,
    buyerContactPhone: detailData.buyerContactPhone,
    numberOfAmendments: detailData.numberOfAmendments,
    nitDocumentLinks: allDocs.filter(d => d.type === 'nit').map(d => d.url),
    corrigendaLinks: allDocs.filter(d => d.type === 'corrigendum').map(d => d.url),
    amendmentLinks: allDocs.filter(d => d.type === 'amendment').map(d => d.url),
    classifiedDocuments: allDocs,
    // Detailed-card fields — detailData.deliveryLocation is already extracted
    // by the shared detail extractor (delivery/place/destination/consignee regex).
    location: detailData.deliveryLocation || undefined,
    bidType: bidTypeText || undefined,
    category: categoryText || undefined,
  }];
}

// --- React-Aware DOM Observation ---

function waitForReactRender(callback: () => void, timeout = 15000) {
  const checkReady = () => {
    // Look for any meaningful content on the page
    const tables = document.querySelectorAll('table tr');
    const cards = document.querySelectorAll('[class*="bid"], [class*="Bid"], [class*="card"], [class*="Card"], [class*="result"], [class*="Result"]');
    const hasContent = document.body?.innerText?.length > 200;
    return tables.length > 2 || cards.length > 0 || hasContent;
  };

  if (checkReady()) {
    setTimeout(callback, 500);
    return;
  }

  const observer = new MutationObserver((_mutations, obs) => {
    if (checkReady()) {
      obs.disconnect();
      setTimeout(callback, 500);
    }
  });

  observer.observe(document.body || document.documentElement, { childList: true, subtree: true });

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
      console.log(`[Ext] Sent ${tenders.length} bids to background`);
    }
  });
}

// --- Listen for messages from popup and service worker ---
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'TRIGGER_SCRAPE') {
    console.log('[Ext] Manual scrape triggered on GeM page');
    const pageType = detectPageType();
    const tenders = extractBids(pageType);
    if (tenders.length > 0) {
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
    console.log('[Ext] EXTRACT_DETAIL received on GeM page');
    const detailData = extractDetailPageData(document);
    const allDocs = extractAllDocumentLinks(document);

    sendResponse({
      data: {
        ...detailData,
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
