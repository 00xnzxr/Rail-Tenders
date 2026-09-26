// ============================================================
// DRPL Extension - Shared Detail Extraction Utilities
// Common functions for extracting rich tender data from detail pages
// ============================================================

import { DocumentLinkInfo } from './types';

const FOOTER_PDF_BLOCKLIST = [
  /copyright/i, /terms\s*and\s*condition/i, /termsandcondition/i,
  /privacy\s*policy/i, /privacypolicy/i, /disclaimer/i,
  /cookie\s*policy/i, /about\s*us/i, /contact\s*us/i,
  /faq\.pdf/i, /sitemap/i, /help\.pdf/i,
];

export function isFooterDocument(url: string, label: string): boolean {
  const combined = (url + ' ' + label).toLowerCase();
  return FOOTER_PDF_BLOCKLIST.some((p) => p.test(combined));
}

// --- Key-Value Pair Extraction ---

/**
 * Extract key-value pairs from a page's text content.
 * Looks for patterns like "Label: Value" or "Label - Value" in the DOM.
 */
export function extractKeyValuePairs(container: Element | Document): Map<string, string> {
  const pairs = new Map<string, string>();
  const text = container.textContent || '';

  // Pattern 1: "Label : Value" on the same line
  const linePattern = /^([A-Za-z][A-Za-z\s\/&()-]{2,50})\s*[:]\s*(.{2,500})$/gm;
  let match: RegExpExecArray | null;
  while ((match = linePattern.exec(text)) !== null) {
    const key = match[1].trim().toLowerCase();
    const value = match[2].trim();
    if (value && !value.startsWith('http')) {
      pairs.set(key, value);
    }
  }

  // Pattern 2: Table rows with th/td or label/value pairs
  const rows = container.querySelectorAll('tr, .row, .detail-row, .field-row');
  rows.forEach((row) => {
    const cells = row.querySelectorAll('td, th, .label, .value, .field-label, .field-value');
    if (cells.length >= 2) {
      const key = (cells[0].textContent || '').trim().toLowerCase().replace(/[:\s]+$/, '');
      const value = (cells[1].textContent || '').trim();
      if (key.length > 1 && key.length < 60 && value.length > 0) {
        pairs.set(key, value);
      }
    }
  });

  return pairs;
}

// --- Eligibility Criteria Extraction ---

const ELIGIBILITY_KEYWORDS = [
  'eligibility', 'qualification', 'pre-qualification', 'prequalification',
  'minimum requirement', 'bidder requirement', 'vendor requirement',
  'experience requirement', 'turnover requirement', 'annual turnover',
  'similar work', 'past experience', 'technical capability',
  'financial capability', 'registration', 'certificate', 'certification',
  'empanelment', 'class of contractor', 'mse', 'msme', 'startup',
];

export function extractEligibilityCriteria(text: string): string {
  const sections: string[] = [];

  // Find sections that contain eligibility-related keywords
  const paragraphs = text.split(/\n{2,}|\r\n{2,}/);
  for (const para of paragraphs) {
    const lower = para.toLowerCase();
    if (ELIGIBILITY_KEYWORDS.some((kw) => lower.includes(kw))) {
      const cleaned = para.trim();
      if (cleaned.length > 20 && cleaned.length < 5000) {
        sections.push(cleaned);
      }
    }
  }

  // Also look for numbered/bulleted lists near eligibility headers
  const headerPattern = /(?:eligibility|qualification|pre-qualification|bidder.*requirement)[^\n]*\n((?:\s*[\d.)\-•*]+\s+[^\n]+\n?)+)/gi;
  let match: RegExpExecArray | null;
  while ((match = headerPattern.exec(text)) !== null) {
    sections.push(match[0].trim());
  }

  return [...new Set(sections)].join('\n\n').substring(0, 10000);
}

// --- Technical Specifications Extraction ---

const TECH_SPEC_KEYWORDS = [
  'technical specification', 'tech spec', 'scope of work', 'scope of supply',
  'bill of quantities', 'boq', 'item description', 'material specification',
  'quality standard', 'is standard', 'iso standard', 'specification',
  'quantity', 'unit of measurement', 'uom', 'make', 'brand',
  'tolerance', 'dimension', 'grade', 'composition',
];

export function extractTechSpecs(text: string): string {
  const sections: string[] = [];

  const paragraphs = text.split(/\n{2,}|\r\n{2,}/);
  for (const para of paragraphs) {
    const lower = para.toLowerCase();
    if (TECH_SPEC_KEYWORDS.some((kw) => lower.includes(kw))) {
      const cleaned = para.trim();
      if (cleaned.length > 20 && cleaned.length < 10000) {
        sections.push(cleaned);
      }
    }
  }

  return [...new Set(sections)].join('\n\n').substring(0, 15000);
}

// --- Evaluation Criteria Extraction ---

const EVAL_KEYWORDS = [
  'evaluation criteria', 'evaluation method', 'scoring', 'marking scheme',
  'selection criteria', 'award criteria', 'l1', 'lowest bidder',
  'quality cum cost', 'qcbs', 'technical score', 'financial score',
  'weightage', 'evaluation factor', 'bid evaluation',
];

export function extractEvaluationCriteria(text: string): string {
  const sections: string[] = [];

  const paragraphs = text.split(/\n{2,}|\r\n{2,}/);
  for (const para of paragraphs) {
    const lower = para.toLowerCase();
    if (EVAL_KEYWORDS.some((kw) => lower.includes(kw))) {
      const cleaned = para.trim();
      if (cleaned.length > 15 && cleaned.length < 5000) {
        sections.push(cleaned);
      }
    }
  }

  return [...new Set(sections)].join('\n\n').substring(0, 10000);
}

// --- Buyer Contact Extraction ---

export function extractBuyerContact(text: string): {
  name: string;
  email: string;
  phone: string;
} {
  const result = { name: '', email: '', phone: '' };

  // Email
  const emailMatch = text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
  if (emailMatch) result.email = emailMatch[0];

  // Phone (Indian format)
  const phoneMatch = text.match(/(?:\+91[\s-]?)?(?:0\d{2,4}[\s-]?)?\d{6,10}/);
  if (phoneMatch) result.phone = phoneMatch[0].trim();

  // Contact name — look near "contact person", "officer", "manager"
  const namePattern = /(?:contact\s*(?:person|officer|name)|officer\s*(?:name|in\s*charge)|name\s*of\s*(?:officer|contact))\s*[:\-]\s*([A-Z][a-zA-Z.\s]{3,40})/i;
  const nameMatch = text.match(namePattern);
  if (nameMatch) result.name = nameMatch[1].trim();

  return result;
}

// --- Performance Guarantee Extraction ---

export function extractPerformanceGuarantee(text: string): {
  amount: number | null;
  percent: number | null;
} {
  const result = { amount: null as number | null, percent: null as number | null };

  // Percentage pattern
  const percentMatch = text.match(
    /(?:performance\s*(?:guarantee|security|bank\s*guarantee)|pbg)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%/i
  );
  if (percentMatch) result.percent = parseFloat(percentMatch[1]);

  // Amount pattern (INR)
  const amountMatch = text.match(
    /(?:performance\s*(?:guarantee|security|bank\s*guarantee)|pbg)\s*[:\-]?\s*(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)/i
  );
  if (amountMatch) result.amount = parseFloat(amountMatch[1].replace(/,/g, ''));

  return result;
}

// --- Delivery Info Extraction ---

export function extractDeliveryInfo(text: string): {
  location: string;
  timeline: string;
} {
  const result = { location: '', timeline: '' };

  // Delivery location
  const locPattern = /(?:delivery\s*(?:location|place|address|at|to)|place\s*of\s*(?:delivery|supply)|destination|consignee)\s*[:\-]\s*([^\n]{5,200})/i;
  const locMatch = text.match(locPattern);
  if (locMatch) result.location = locMatch[1].trim();

  // Delivery timeline/period
  const timePattern = /(?:delivery\s*(?:period|time|schedule|within)|completion\s*(?:period|time)|supply\s*(?:period|within))\s*[:\-]\s*([^\n]{3,200})/i;
  const timeMatch = text.match(timePattern);
  if (timeMatch) result.timeline = timeMatch[1].trim();

  return result;
}

// --- Document Link Classification ---

export function classifyDocumentLink(url: string, label: string): DocumentLinkInfo {
  const lower = (label + ' ' + url).toLowerCase();

  if (/\bnit\b|notice\s*invit|tender\s*notice|tender\s*document|download\s*tender\s*doc/.test(lower)) {
    return { url, label, type: 'nit' };
  }
  if (/corrigend|^corr|addendum|clarification|slip/i.test(lower)) {
    return { url, label, type: 'corrigendum' };
  }
  if (/amendment|modif|revision|revised/.test(lower)) {
    return { url, label, type: 'amendment' };
  }
  if (/specification|tech\s*spec|scope|boq|bill\s*of\s*quantit|gcc|general\s*condition/.test(lower)) {
    return { url, label, type: 'specification' };
  }

  return { url, label, type: 'other' };
}

// --- Extract All Document Links from Page ---

export function extractAllDocumentLinks(container: Element | Document): DocumentLinkInfo[] {
  const links: DocumentLinkInfo[] = [];
  const seen = new Set<string>();

  // Find all links to PDFs and documents
  const anchors = container.querySelectorAll('a[href]');
  anchors.forEach((a) => {
    const href = (a as HTMLAnchorElement).href;
    const text = (a.textContent || '').trim();

    if (!href || seen.has(href)) return;

    // Match document URLs (PDFs, DOCs, etc.)
    const isDoc =
      /\.(pdf|doc|docx|xls|xlsx|zip|rar)(\?|$)/i.test(href) ||
      /download|document|attachment|file|upload/i.test(href) ||
      /\.pdf|\.doc/i.test(text);

    if (isDoc && !isFooterDocument(href, text)) {
      seen.add(href);
      links.push(classifyDocumentLink(href, text || 'Document'));
    }
  });

  return links;
}

// --- Count Amendments/Corrigenda ---

export function countAmendments(container: Element | Document): number {
  const text = (container.textContent || '').toLowerCase();
  let count = 0;

  // Count corrigendum/amendment mentions
  const corrigendaMatches = text.match(/corrigendum|addendum|amendment/gi);
  if (corrigendaMatches) count = corrigendaMatches.length;

  // Also check for numbered corrigenda (Corrigendum 1, Corrigendum 2, etc.)
  const numberedMatch = text.match(/corrigendum\s*(?:no\.?\s*)?(\d+)/gi);
  if (numberedMatch) {
    const maxNum = Math.max(
      ...numberedMatch.map((m) => {
        const numMatch = m.match(/(\d+)/);
        return numMatch ? parseInt(numMatch[1]) : 0;
      })
    );
    count = Math.max(count, maxNum);
  }

  return count;
}

// --- Full Detail Page Extraction (generic) ---

/**
 * Generic detail page extractor. Each portal-specific script can call this
 * and override/supplement with portal-specific logic.
 */
export function extractDetailPageData(container: Element | Document): {
  fullDescription: string;
  eligibilityCriteria: string;
  technicalSpecifications: string;
  evaluationCriteria: string;
  performanceGuarantee: number | null;
  performanceGuaranteePercent: number | null;
  preBidMeetingLocation: string;
  deliveryLocation: string;
  deliveryTimeline: string;
  buyerContactName: string;
  buyerContactEmail: string;
  buyerContactPhone: string;
  numberOfAmendments: number;
  classifiedDocuments: DocumentLinkInfo[];
} {
  const pageText = container.textContent || '';
  const contact = extractBuyerContact(pageText);
  const pbg = extractPerformanceGuarantee(pageText);
  const delivery = extractDeliveryInfo(pageText);
  const docs = extractAllDocumentLinks(container);

  // Full description — try to find the main content area
  let fullDesc = '';
  const descSelectors = [
    '.tender-description', '.scope-of-work', '.work-description',
    '#tenderDescription', '#scopeOfWork', '.detail-content',
    '[class*="description"]', '[class*="scope"]',
  ];
  for (const sel of descSelectors) {
    const el = container.querySelector(sel);
    if (el && (el.textContent || '').trim().length > 50) {
      fullDesc = (el.textContent || '').trim();
      break;
    }
  }
  // Fallback: use first large text block
  if (!fullDesc) {
    const paras = container.querySelectorAll('p, .content, .body, td');
    for (const p of paras) {
      const t = (p.textContent || '').trim();
      if (t.length > 200 && t.length < 20000) {
        fullDesc = t;
        break;
      }
    }
  }

  // Pre-bid meeting location
  let preBidLocation = '';
  const preBidMatch = pageText.match(
    /(?:pre[\s-]*bid\s*(?:meeting|conference))\s*(?:venue|location|place|at)\s*[:\-]\s*([^\n]{5,200})/i
  );
  if (preBidMatch) preBidLocation = preBidMatch[1].trim();

  return {
    fullDescription: fullDesc.substring(0, 20000),
    eligibilityCriteria: extractEligibilityCriteria(pageText),
    technicalSpecifications: extractTechSpecs(pageText),
    evaluationCriteria: extractEvaluationCriteria(pageText),
    performanceGuarantee: pbg.amount,
    performanceGuaranteePercent: pbg.percent,
    preBidMeetingLocation: preBidLocation,
    deliveryLocation: delivery.location,
    deliveryTimeline: delivery.timeline,
    buyerContactName: contact.name,
    buyerContactEmail: contact.email,
    buyerContactPhone: contact.phone,
    numberOfAmendments: countAmendments(container),
    classifiedDocuments: docs,
  };
}
