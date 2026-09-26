// ============================================================
// DRPL Extension - Selector Utility
// Loads selector config and provides DOM querying helpers
// ============================================================

import selectorsConfig from '../config/selectors.json';
import { PortalName, PageType } from './types';

type SelectorConfig = typeof selectorsConfig;

/** Get the selector config for a specific portal */
export function getPortalSelectors(portal: PortalName) {
  return (selectorsConfig.portals as any)[portal] || null;
}

/** Try multiple CSS selectors and return the first match */
export function queryFirst(parent: Element | Document, selectorString: string): Element | null {
  const selectors = selectorString.split(',').map((s) => s.trim());
  for (const selector of selectors) {
    try {
      const el = parent.querySelector(selector);
      if (el) return el;
    } catch {
      // Invalid selector, skip
    }
  }
  return null;
}

/** Try multiple CSS selectors and return all matches */
export function queryAll(parent: Element | Document, selectorString: string): Element[] {
  const selectors = selectorString.split(',').map((s) => s.trim());
  const results: Element[] = [];
  const seen = new Set<Element>();

  for (const selector of selectors) {
    try {
      parent.querySelectorAll(selector).forEach((el) => {
        if (!seen.has(el)) {
          seen.add(el);
          results.push(el);
        }
      });
    } catch {
      // Invalid selector, skip
    }
  }
  return results;
}

/** Extract text content from an element, cleaned up */
export function extractText(element: Element | null): string {
  if (!element) return '';
  return (element.textContent || '').replace(/\s+/g, ' ').trim();
}

/**
 * Normalize a scraped tender title. Mirrors the backend clean_tender_title():
 * drop NBSP, the embedded "Tender Type:" suffix, and trailing ellipsis runs,
 * then collapse whitespace.
 */
export function cleanTitle(raw: string | null | undefined): string {
  if (!raw) return '';
  return raw
    .replace(/\u00A0/g, ' ')
    .replace(/\s*Tender\s*Type\s*:.*$/i, '')
    .replace(/[.…]{2,}\s*$/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Extract a labelled value from a card's collapsed text, e.g.
 * pickLabeledField(text, 'Tender\\s*Title') → the title up to the next label.
 * (Same logic the GeM search-driver uses; shared here so passive paths reuse it.)
 */
export function pickLabeledField(text: string, label: string): string {
  const re = new RegExp(`${label}\\s*:\\s*([^\\n]+?)(?=\\s{2,}\\w[\\w ]+\\s*:|\\n|$)`, 'i');
  const m = text.match(re);
  return m ? m[1].trim() : '';
}

/** Extract a number from text (handles ₹, commas, etc.) */
export function extractNumber(text: string): number | null {
  const cleaned = text.replace(/[₹,\s]/g, '');
  const num = parseFloat(cleaned);
  return isNaN(num) ? null : num;
}

/** Extract href from a link element */
export function extractHref(element: Element | null): string {
  if (!element) return '';
  return (element as HTMLAnchorElement).href || element.getAttribute('href') || '';
}

/** Detect the current page type based on URL and DOM indicators */
export function detectPageType(portal: PortalName): PageType {
  const config = getPortalSelectors(portal);
  if (!config?.pageDetection) return 'unknown';

  const url = window.location.href;

  for (const [pageType, detection] of Object.entries(config.pageDetection) as any) {
    const { urlPattern, indicator } = detection;

    // Check URL pattern
    if (urlPattern && url.includes(urlPattern)) {
      // Verify with DOM indicator if available
      if (indicator) {
        const el = queryFirst(document, indicator);
        if (el) return pageType as PageType;
      } else {
        return pageType as PageType;
      }
    }
  }

  return 'unknown';
}

/** Check if the user is currently logged into the portal */
export function isLoggedIn(portal: PortalName): boolean {
  const config = getPortalSelectors(portal);
  if (!config?.sessionIndicators) return false;

  const loggedInEl = queryFirst(document, config.sessionIndicators.loggedIn);
  const expiredEl = queryFirst(document, config.sessionIndicators.expired);

  return !!loggedInEl && !expiredEl;
}

/** Check if the session has expired */
export function isSessionExpired(portal: PortalName): boolean {
  const config = getPortalSelectors(portal);
  if (!config?.sessionIndicators) return false;

  const expiredEl = queryFirst(document, config.sessionIndicators.expired);
  const loggedInEl = queryFirst(document, config.sessionIndicators.loggedIn);

  return !!expiredEl && !loggedInEl;
}
