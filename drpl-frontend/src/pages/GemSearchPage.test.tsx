import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/**
 * What a finished sweep is allowed to claim.
 *
 * The collector reports coverage as arithmetic, and this page is where that
 * reaches a person. Three cases differ, and the third is the one worth pinning:
 * an `incremental` sweep answers "what is new?" and stops on known ground, so
 * it has NOT seen the rest of the portal -- on a cold ledger it reached 23% of
 * the live railway tenders. A perfectly good answer to the question asked, and
 * a dangerous one to read as "these are all of them". So `portal_done` omits
 * `coverage` in that case and the page must say so rather than filling in a
 * reassuring default.
 *
 * The progress bar is the same argument in miniature: a `ministry` sweep makes
 * several convergence passes over the same pages, so pages-fetched over
 * pages-expected pegs at 100% long before the sweep is done, while distinct
 * rows over the portal's own total is the figure actually converging.
 */

vi.mock('../components/layout/Header', () => ({
  default: ({ title }: any) => <h1>{title}</h1>,
}))
vi.mock('../components/ui/PortalBadge', () => ({ default: () => <span /> }))

// `vi.hoisted`, because vi.mock is lifted above the module's own imports and
// a plain const would not exist yet when the factory runs.
const api = vi.hoisted(() => ({
  startCollectRun: vi.fn(),
  getActiveCollectRun: vi.fn(),
  getCollectCoverage: vi.fn(),
  getRecentCollectRuns: vi.fn(),
  getTenderById: vi.fn(),
  openRunStream: vi.fn(),
  cancelRun: vi.fn(),
}))
vi.mock('../lib/api', () => api)

import GemSearchPage from './GemSearchPage'

/** An SSE body in the shape the page parses. `hang` leaves the run running. */
function streamOf(events: Array<[string, unknown]>, { hang = false } = {}) {
  const enc = new TextEncoder()
  const chunks = events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)
  let i = 0
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (i < chunks.length) return { done: false, value: enc.encode(chunks[i++]) }
          if (hang) return new Promise(() => {})   // still sweeping
          return { done: true, value: undefined }
        },
      }),
    },
  } as any
}

async function press(label: string) {
  render(<MemoryRouter><GemSearchPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: label }))
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  api.getActiveCollectRun.mockResolvedValue(null as any)
  api.getCollectCoverage.mockResolvedValue({ available: false, portal: 'gem' } as any)
  api.getRecentCollectRuns.mockResolvedValue([] as any)
  api.startCollectRun.mockResolvedValue({ run_id: 'run-1' } as any)
  api.getTenderById.mockResolvedValue(null as any)
  api.cancelRun.mockResolvedValue({} as any)
})

describe('GemSearchPage', () => {
  it('offers the three modes the route accepts', async () => {
    render(<MemoryRouter><GemSearchPage /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: /Search GeM/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Complete sweep/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Full portal audit/ })).toBeTruthy()
  })

  it('sends mode=ministry for the complete sweep', async () => {
    api.openRunStream.mockResolvedValue(streamOf([['run_done', { status: 'completed' }]]))
    await press('Complete sweep')
    expect(api.startCollectRun).toHaveBeenCalledWith({ portals: ['gem_full'], mode: 'ministry' })
  })

  it('claims completeness only with the arithmetic behind it', async () => {
    api.openRunStream.mockResolvedValue(streamOf([
      ['collect_started', { mode: 'ministry' }],
      ['portal_done', {
        portal: 'gem', mode: 'ministry', status: 'completed',
        coverage: 1.0, complete: true, rows_distinct: 1703, final_total: 1703, pages_failed: 0,
      }],
      ['run_done', { status: 'completed' }],
    ]))
    await press('Complete sweep')
    expect(await screen.findByText(
      /Complete - examined 1,703 of 1,703 live bids for this ministry/,
    )).toBeTruthy()
  })

  it('reports a sweep that fell short as partial', async () => {
    api.openRunStream.mockResolvedValue(streamOf([
      ['collect_started', { mode: 'ministry' }],
      ['portal_done', {
        portal: 'gem', mode: 'ministry', status: 'completed',
        coverage: 0.62, complete: false, rows_distinct: 1050, final_total: 1703, pages_failed: 3,
      }],
      ['run_done', { status: 'completed' }],
    ]))
    await press('Complete sweep')
    expect(await screen.findByText(
      /Partial - covered 62% of the live list, 3 page\(s\) failed/,
    )).toBeTruthy()
  })

  it('does not invent a coverage figure for a sweep that measured none', async () => {
    // An incremental portal_done carries no coverage keys at all.
    api.openRunStream.mockResolvedValue(streamOf([
      ['collect_started', { mode: 'incremental' }],
      ['portal_done', { portal: 'gem', mode: 'incremental', status: 'completed' }],
      ['run_done', { status: 'completed' }],
    ]))
    await press('Search GeM')
    expect(await screen.findByText(/Newest bids only/)).toBeTruthy()
    expect(screen.queryByText(/Complete - examined/)).toBeNull()
    expect(screen.queryByText(/Partial - covered/)).toBeNull()
  })

  it('measures progress in distinct bids, not in repeated pages', async () => {
    // 340 pages fetched on a ~171-page query is the second convergence pass:
    // the page ratio would read 198%, the distinct ratio reads 53%.
    api.openRunStream.mockResolvedValue(streamOf([
      ['collect_started', { mode: 'ministry' }],
      ['collect_progress', {
        portal: 'gem', term: null, page: 340, pages_done: 340,
        rows_seen: 10, in_scope: 4, expected_total: 1703, rows_distinct: 900,
      }],
    ], { hang: true }))
    await press('Complete sweep')
    expect(await screen.findByText(/900 of ~1,703 bids examined/)).toBeTruthy()
    const bar = document.querySelector('.bg-emerald-500') as HTMLElement
    expect(bar?.style.width).toBe('53%')
  })
})
