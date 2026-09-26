// ============================================================
// DRPL Extension - Aggregator Content Script
// Extraction for TenderTiger, BidAssist, TenderDetail, TendersInfo, ProjectsToday
// Uses adaptive extraction based on real DOM structures
// ============================================================

import { TenderData, TenderDataMessage } from '../utils/types';
import { extractDetailPageData, extractAllDocumentLinks } from '../utils/detail-extractor';

type AggregatorSite = 'tendertiger' | 'bidassist' | 'tenderdetail' | 'tendersinfo' | 'projectstoday';

function init() {
  const site = detectSite();
  if (!site) return;

  console.log(`[Ext] Aggregator content script loaded for: ${site} on ${window.location.href}`);
  // Aggregator sites wait for manual "Start Scraping" from popup.
  // The TRIGGER_SCRAPE listener below handles extraction.
}

function detectSite(): AggregatorSite | null {
  const host = window.location.hostname;
  if (host.includes('tendertiger')) return 'tendertiger';
  if (host.includes('bidassist')) return 'bidassist';
  if (host.includes('tenderdetail')) return 'tenderdetail';
  if (host.includes('tendersinfo')) return 'tendersinfo';
  if (host.includes('projectstoday')) return 'projectstoday';
  return null;
}

function waitForContent(site: AggregatorSite, callback: () => void) {
  // Check if content is already loaded
  const hasContent = () => {
    if (site === 'tendertiger') {
      // TenderTiger uses <ul>/<ol> with <li> items containing TID
      const items = document.querySelectorAll('li');
      for (const li of items) {
        if (li.textContent?.includes('TID:')) return true;
      }
    }
    // Generic: check for substantial list items or table rows
    const rows = document.querySelectorAll('li, tr, .card, [class*="tender"], [class*="bid"]');
    return rows.length > 5;
  };

  if (hasContent()) {
    setTimeout(callback, 500);
    return;
  }

  // Watch for dynamic content
  const observer = new MutationObserver((_mutations, obs) => {
    if (hasContent()) {
      obs.disconnect();
      setTimeout(callback, 500);
    }
  });

  observer.observe(document.body || document.documentElement, { childList: true, subtree: true });

  // Timeout fallback
  setTimeout(() => {
    observer.disconnect();
    callback();
  }, 8000);
}

// --- Main extraction dispatcher ---

function extractTenders(site: AggregatorSite): TenderData[] {
  switch (site) {
    case 'tendertiger':
      return extractTenderTiger();
    case 'bidassist':
      return extractBidAssist();
    case 'tenderdetail':
      return extractTenderDetail();
    case 'tendersinfo':
    case 'projectstoday':
      return extractGeneric(site);
  }
}

// --- TenderTiger Extraction (based on real DOM inspection) ---
// Structure: <ul>/<ol> > <li> items containing:
//   - "TID:" label + TID value in next element
//   - Link with org name (e.g., "Railways Transport Services")
//   - Location text (e.g., "Ambala, Haryana, India")
//   - Link with title/description (href to TenderDetail page)
//   - "Worth :" label + value (e.g., "INR 1.43 Cr")
//   - "EMD :" label + value (e.g., "INR 2.22 Lac")
//   - "Due Date :" label + date (e.g., "06 April 2026")

function extractTenderTiger(): TenderData[] {
  const tenders: TenderData[] = [];

  // Find all list items on the page
  const allListItems = document.querySelectorAll('li');

  allListItems.forEach((li) => {
    try {
      const text = li.textContent || '';

      // Only process items that contain TID (tender ID marker)
      if (!text.includes('TID:')) return;

      // Skip items that are too short (not actual tender items)
      if (text.length < 50) return;

      // Extract TID
      const tidMatch = text.match(/TID:\s*(\d+)/);
      if (!tidMatch) return;
      const tid = tidMatch[1];

      // Extract all child elements for field detection
      const children = Array.from(li.children);

      // Find the detail link (longest link text, links to TenderDetail)
      const links = li.querySelectorAll('a[href]');
      let detailLink = '';
      let title = '';
      let org = '';

      for (const link of links) {
        const href = (link as HTMLAnchorElement).href || '';
        const linkText = link.textContent?.trim() || '';

        if (href.includes('TenderDetail') || href.includes('tenderNo=') || href.includes('Tenderinformation')) {
          if (linkText.length > title.length && linkText.length > 20) {
            title = linkText;
            detailLink = href;
          }
        }

        // Organization is typically a short link text like "Railways Transport Services"
        if (linkText.length > 5 && linkText.length < 80 && !href.includes('TenderDetail') &&
            !href.includes('whatsapp') && !linkText.includes('Refer Document') &&
            !linkText.includes('Buy') && !linkText.includes('Whatsapp')) {
          org = linkText;
        }
      }

      if (!title && !tid) return;

      // Extract Worth, EMD, Due Date using text patterns
      const worthMatch = text.match(/Worth\s*:\s*(.*?)(?:\s*(?:EMD|Due|$))/i);
      const emdMatch = text.match(/EMD\s*:\s*(.*?)(?:\s*(?:Due|Worth|$))/i);
      const dueDateMatch = text.match(/Due Date\s*:\s*(\d{1,2}\s+\w+\s+\d{4})/i);

      // Parse value from "INR 1.43 Cr" or "Refer Document"
      const worthText = worthMatch?.[1]?.trim() || '';
      const emdText = emdMatch?.[1]?.trim() || '';
      const estimatedValue = parseIndianValue(worthText);
      const emdAmount = parseIndianValue(emdText);

      const { location, bidType, sourcePortal } = parseAggregatorTokens(text);
      // Category: the sector link text (org link already captured as `org`).
      const category = org || undefined;

      const tender: TenderData = {
        portal: 'tendertiger',
        tenderId: tid,
        title: title || `TenderTiger TID:${tid}`,
        department: '',
        organisation: org,
        description: title,
        estimatedValue,
        currency: 'INR',
        openingDate: null,
        closingDate: dueDateMatch?.[1] || null,
        emdAmount,
        preBidDate: null,
        status: 'Open',
        location,
        bidType,
        sourcePortal,
        category,
        documentLinks: detailLink ? [detailLink] : [],
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
      };

      tenders.push(tender);
    } catch (err) {
      // Skip bad items
    }
  });

  console.log(`[Ext] TenderTiger: extracted ${tenders.length} tenders`);
  return tenders;
}

// --- BidAssist Extraction ---

function extractBidAssist(): TenderData[] {
  const tenders: TenderData[] = [];

  // BidAssist uses card-based layouts
  // Try multiple possible selectors for tender cards
  const cardSelectors = [
    '.card', '.tender-card', '.result-item', '.bid-item',
    '[class*="card"]', '[class*="tender"]', '[class*="result"]',
    'li', 'article',
  ];

  let items: Element[] = [];
  for (const sel of cardSelectors) {
    const found = document.querySelectorAll(sel);
    // Filter to only items that look like tenders
    const tenderItems = Array.from(found).filter(el => {
      const text = el.textContent || '';
      return text.length > 50 && (
        text.includes('Tender') || text.includes('tender') ||
        text.includes('Bid') || text.includes('bid') ||
        text.includes('Due') || text.includes('Closing') ||
        /T\d{6,}/.test(text) || /TID/.test(text)
      );
    });
    if (tenderItems.length >= 3) {
      items = tenderItems;
      break;
    }
  }

  items.forEach(item => {
    try {
      const text = item.textContent || '';
      if (text.length < 50) return;

      // Try to find tender ID
      const tidMatch = text.match(/(?:TID|Tender\s*(?:No|ID|Number))[:\s]*(\d+)/i) ||
                        text.match(/\b(\d{8,})\b/);
      if (!tidMatch) return;

      const tid = tidMatch[1];

      // Find title (longest link text)
      const links = item.querySelectorAll('a[href]');
      let title = '';
      let detailLink = '';
      for (const link of links) {
        const linkText = link.textContent?.trim() || '';
        if (linkText.length > title.length && linkText.length > 15) {
          title = linkText;
          detailLink = (link as HTMLAnchorElement).href;
        }
      }

      // Extract value and date from text
      const valueMatch = text.match(/(?:Worth|Value|Amount|Estimated)[:\s]*([\w\s₹.,]+(?:Cr|Lac|Lakh|crore|lakh))/i) ||
                          text.match(/(?:INR|₹)\s*([\d,.]+\s*(?:Cr|Lac|Lakh)?)/i);
      const dateMatch = text.match(/(?:Due|Closing|Last)\s*(?:Date)?[:\s]*(\d{1,2}[\s\-\/]\w{3,9}[\s\-\/]\d{4})/i);

      // Extract org
      const orgMatch = text.match(/(?:Organisation|Organization|Department|Authority)[:\s]*(.+?)(?:\n|Due|Worth|EMD|$)/i);

      tenders.push({
        portal: 'bidassist',
        tenderId: tid,
        title: title || text.slice(0, 200),
        department: '',
        organisation: orgMatch?.[1]?.trim() || '',
        description: title || text.slice(0, 500),
        estimatedValue: parseIndianValue(valueMatch?.[1] || ''),
        currency: 'INR',
        openingDate: null,
        closingDate: dateMatch?.[1] || null,
        emdAmount: null,
        preBidDate: null,
        status: 'Open',
        documentLinks: detailLink ? [detailLink] : [],
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
      });
    } catch {
      // Skip
    }
  });

  console.log(`[Ext] BidAssist: extracted ${tenders.length} tenders`);
  return tenders;
}

// --- TenderDetail Extraction ---

function extractTenderDetail(): TenderData[] {
  const tenders: TenderData[] = [];
  const seen = new Set<string>();

  // TenderDetail page uses div-based card layout.
  // Each tender card contains:
  //   - An org/location link (e.g. "Local Bodies - Varanasi - Uttar Pradesh")
  //   - A tender ID link (orange, 8-digit number like "54778970")
  //   - A title/description link
  //   - "Due Date : Mar 27, 2026", "Tender Value : ₹ 8.72 Lakhs", "View Notice"

  // Strategy: find all links with 7-8 digit tender IDs, then gather surrounding data
  const allLinks = document.querySelectorAll('a[href]');

  allLinks.forEach(link => {
    try {
      const linkText = link.textContent?.trim() || '';
      // Match standalone 7-8 digit tender IDs
      if (!/^\d{7,8}$/.test(linkText)) return;

      const tid = linkText;
      if (seen.has(tid)) return;
      seen.add(tid);

      // Walk up to find the containing card/block
      let container = link.parentElement;
      for (let i = 0; i < 5 && container; i++) {
        const text = container.textContent || '';
        // The right container has "Due Date" or "Tender Value" text
        if (text.includes('Due Date') || text.includes('Tender Value') || text.includes('View Notice')) break;
        container = container.parentElement;
      }

      if (!container) container = link.parentElement;

      const text = container?.textContent || '';
      const links = container?.querySelectorAll('a[href]') || [];

      // Extract org (first link, usually "Org - Location - State")
      let org = '';
      let title = '';
      let detailLink = '';

      for (const a of links) {
        const t = (a.textContent?.trim() || '');
        const href = (a as HTMLAnchorElement).href;

        // Skip the tender ID link itself, and utility links
        if (/^\d{7,8}$/.test(t)) continue;
        if (t === 'View Notice' || t.length < 5) continue;

        // The longest text link is usually the title/description
        if (t.length > title.length && t.length > 15) {
          title = t;
          detailLink = href;
        }

        // Shorter links that contain " - " are typically org/location
        if (t.includes(' - ') && t.length < 100 && !org) {
          org = t;
        }
      }

      // Extract value: "₹ 8.72 Lakhs" or "₹ 1.50 Crore" or "Ref. Document"
      const valueMatch = text.match(/(?:Tender\s*Value\s*:\s*₹?\s*)([\d,.]+\s*(?:Crore|Cr|Lakhs?|Lacs?)?)/i);
      const estimatedValue = valueMatch ? parseIndianValue(valueMatch[1]) : null;

      // Extract due date: "Due Date : Mar 27, 2026" or "Due Date : Apr 4, 2026"
      const dateMatch = text.match(/Due\s*Date\s*:\s*(\w{3,9}\s+\d{1,2},?\s+\d{4})/i)
                     || text.match(/Due\s*Date\s*:\s*(\d{1,2}[\s\-\/]\w{3,9}[\s\-\/]\d{4})/i);

      tenders.push({
        portal: 'tenderdetail',
        tenderId: tid,
        title: title || `TenderDetail #${tid}`,
        department: '',
        organisation: org,
        description: title,
        estimatedValue,
        currency: 'INR',
        openingDate: null,
        closingDate: dateMatch?.[1] || null,
        emdAmount: null,
        preBidDate: null,
        status: 'Open',
        documentLinks: detailLink ? [detailLink] : [],
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
      });
    } catch {
      // Skip malformed entries
    }
  });

  console.log(`[Ext] TenderDetail: extracted ${tenders.length} tenders`);
  return tenders;
}

// --- Generic Extraction (for TendersInfo, ProjectsToday) ---

function extractGeneric(site: AggregatorSite): TenderData[] {
  const tenders: TenderData[] = [];

  // Try to find any listing elements
  const candidates = document.querySelectorAll('li, tr, .card, article, [class*="item"], [class*="result"]');

  const items = Array.from(candidates).filter(el => {
    const text = el.textContent || '';
    return text.length > 40 && text.length < 5000 && (
      text.includes('tender') || text.includes('Tender') ||
      text.includes('bid') || text.includes('Bid') ||
      text.includes('project') || text.includes('Project') ||
      /\d{6,}/.test(text)
    );
  });

  items.forEach((item, index) => {
    try {
      const text = item.textContent || '';

      // Find a link for title
      const links = item.querySelectorAll('a[href]');
      let title = '';
      let detailLink = '';
      for (const link of links) {
        const t = link.textContent?.trim() || '';
        const href = (link as HTMLAnchorElement).href || '';
        if (t.length > title.length && t.length > 10 && !href.includes('javascript:')) {
          title = t;
          detailLink = href;
        }
      }

      if (!title && text.length < 40) return;

      // Try to extract ID
      const idMatch = text.match(/(?:Ref|ID|TID|No)[:\s#]*(\d{5,})/i) || text.match(/\b(\d{7,})\b/);
      const tid = idMatch?.[1] || `${site}-${index}-${Date.now()}`;

      // Extract dates and values
      const dateMatch = text.match(/(\d{1,2}[\s\-\/]\w{3,9}[\s\-\/]\d{4})/);
      const valueMatch = text.match(/(?:INR|₹|Rs|USD|\$)\s*([\d,.]+\s*(?:Cr|Lac|Lakh|million|billion)?)/i);

      tenders.push({
        portal: site,
        tenderId: tid,
        title: title || text.slice(0, 200),
        department: '',
        organisation: '',
        description: (title || text).slice(0, 500),
        estimatedValue: parseIndianValue(valueMatch?.[1] || ''),
        currency: 'INR',
        openingDate: null,
        closingDate: dateMatch?.[1] || null,
        emdAmount: null,
        preBidDate: null,
        status: 'Open',
        documentLinks: detailLink ? [detailLink] : [],
        sourceUrl: window.location.href,
        extractedAt: new Date().toISOString(),
      });
    } catch {
      // Skip
    }
  });

  console.log(`[Ext] ${site}: extracted ${tenders.length} tenders`);
  return tenders;
}

// --- Helpers ---

/**
 * Pull card tokens out of an aggregator row's flat text. Pure + testable —
 * the DOM-dependent bits (category link) are handled by the caller.
 */
export function parseAggregatorTokens(text: string): { location?: string; bidType?: string; sourcePortal?: string } {
  const location = text.match(/([A-Z][a-z]+(?:,\s*[A-Za-z ]+)*,\s*India)/)?.[1];
  const bidType = text.match(/\b(NCB|GCB|LTE|Limited|Single|Global|Open|EOI)\b/)?.[1];
  const portalRaw = text.match(/\b(GeM|IREPS|CPPP|eProc|eProcurement)\b/i)?.[1];
  const portalMap: Record<string, string> = {
    gem: 'gem', ireps: 'ireps', cppp: 'cppp', eproc: 'eproc', eprocurement: 'eproc',
  };
  const sourcePortal = portalRaw ? portalMap[portalRaw.toLowerCase()] : undefined;
  return { location, bidType, sourcePortal };
}

/** Parse Indian currency values like "INR 1.43 Cr", "2.22 Lac", "₹50,000" */
function parseIndianValue(text: string): number | null {
  if (!text || text.includes('Refer')) return null;

  const cleaned = text.replace(/[₹,\s]/g, '').replace(/INR/i, '').replace(/Rs\.?/i, '').trim();
  if (!cleaned) return null;

  const numMatch = cleaned.match(/([\d.]+)\s*(Cr|Crore|Lac|Lakh|L|K|M|Million|Billion)?/i);
  if (!numMatch) return null;

  let value = parseFloat(numMatch[1]);
  if (isNaN(value)) return null;

  const unit = (numMatch[2] || '').toLowerCase();
  if (unit.startsWith('cr')) value *= 10000000; // 1 Cr = 10M
  else if (unit.startsWith('lac') || unit.startsWith('lakh') || unit === 'l') value *= 100000;
  else if (unit === 'k') value *= 1000;
  else if (unit === 'm' || unit.startsWith('million')) value *= 1000000;
  else if (unit.startsWith('billion')) value *= 1000000000;

  return value;
}

// --- Listen for messages from popup and service worker ---
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'TRIGGER_SCRAPE') {
    console.log('[Ext] Manual scrape triggered on aggregator page');
    const site = detectSite();
    if (site) {
      const tenders = extractTenders(site);
      console.log(`[Ext] Manual scrape found ${tenders.length} tenders on ${site}`);
      if (tenders.length > 0) {
        const msg: TenderDataMessage = {
          type: 'TENDER_DATA_EXTRACTED',
          portal: site,
          payload: { tenders, pageType: 'search_results', pageUrl: window.location.href },
        };
        // Mark as manual so service worker skips local dedup
        (msg as any).manual = true;
        chrome.runtime.sendMessage(msg, (response) => {
          console.log('[Ext] Background acknowledged manual scrape:', response);
        });
        sendResponse({ extracted: tenders.length });
      } else {
        sendResponse({ extracted: 0, site });
      }
    } else {
      sendResponse({ error: 'Not on a supported aggregator site', hostname: window.location.hostname });
    }
  } else if (message.type === 'EXTRACT_DETAIL') {
    // Deep scrape: extract rich data from this detail page
    console.log('[Ext] EXTRACT_DETAIL received on aggregator page');
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
  return true; // Keep message channel open for async response
});

// --- Start ---
init();
