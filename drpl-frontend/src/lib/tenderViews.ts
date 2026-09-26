import type { TenderFilters } from '../types/tender';
import type { TenderViewCounts } from './api';

export type TenderViewKey = 'to_bid' | 'worth_a_look' | 'new_unscored' | 'discarded' | 'all';

export interface TenderView {
  key: TenderViewKey;
  slug: string;                 // route segment; '' for All (bare /tenders)
  label: string;                // tab + header label
  blurb: string;                // one plain-language sentence in the header
  color: 'emerald' | 'amber' | 'sky' | 'rose' | 'slate';
  countKey: keyof TenderViewCounts;
  baseFilters: () => Partial<TenderFilters>;
}

/**
 * The tender funnel. Every tender lands in exactly one pile by its AI relevance
 * score — the same green / amber / red tiers the cards use (≥70 / 40–70 / <40) —
 * plus a "New" intake for anything not scored yet. This mirrors what people see
 * on each card, and works off the score that's always populated (unlike the
 * stored `segment`, which depends on the auto-scorer having run a config pass).
 *
 * Boundaries: the upper tier owns each threshold. Review/Set aside cap just
 * below 0.70 / 0.40 (SCORE_* below) so a tender scored exactly 0.70 or 0.40
 * lands in one pile, not two. "All" is a quiet escape hatch, not a pile.
 */
const HI = 0.7;          // ≥ this → Pursue (green)
const MID = 0.4;         // ≥ this → Review (amber); below → Set aside (red)
const EPS = 0.0001;      // keep the lower tier strictly under the threshold

export const TENDER_VIEWS: TenderView[] = [
  {
    key: 'to_bid', slug: 'pursue', label: 'Pursue', color: 'emerald', countKey: 'to_bid',
    blurb: 'Strong AI match (70%+). The tenders most worth bidding on — start here.',
    baseFilters: () => ({ score_min: HI }),
  },
  {
    key: 'worth_a_look', slug: 'review', label: 'Review', color: 'amber', countKey: 'worth_a_look',
    blurb: 'Partial match (40–70%). Skim these and promote the good ones before you skip them.',
    baseFilters: () => ({ score_min: MID, score_max: HI - EPS }),
  },
  {
    key: 'new_unscored', slug: 'new', label: 'New', color: 'sky', countKey: 'new_unscored',
    blurb: 'Freshly ingested tenders the AI has not scored yet.',
    baseFilters: () => ({ unscored: true }),
  },
  {
    key: 'discarded', slug: 'set-aside', label: 'Set aside', color: 'rose', countKey: 'discarded',
    blurb: 'Low match (under 40%) — not for us, cleared out of the way so the good piles stay clean.',
    baseFilters: () => ({ score_max: MID - EPS }),
  },
  {
    key: 'all', slug: '', label: 'All', color: 'slate', countKey: 'all',
    blurb: 'Every tender on the platform, unsorted.',
    baseFilters: () => ({}),
  },
];

/** The four funnel piles, in render order (excludes the "All" escape hatch). */
export const FUNNEL_VIEWS = TENDER_VIEWS.filter((v) => v.key !== 'all');

export function resolveView(slug?: string): TenderView {
  const all = TENDER_VIEWS.find((v) => v.key === 'all')!;
  if (!slug) return all;
  return TENDER_VIEWS.find((v) => v.slug === slug) ?? all;
}
