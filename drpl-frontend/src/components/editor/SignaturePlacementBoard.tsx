import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Move } from 'lucide-react';

// Display width of the placement board, in CSS pixels. The board renders the
// actual PDF pages (rasterised server-side) stacked vertically, scaled to fit
// this width. All chip positions are stored in mm from the bottom-left of the
// real page; the drag math converts between mm and pixels via a SCALE derived
// from the actual page dimensions (see below) so coordinates land exactly
// where they will on the final PDF — for any page shape, not just A4.
const BOARD_WIDTH_PX = 520;

// Fallback A4 geometry, used only until the real rasterised page dimensions
// arrive (or if the preview is unavailable entirely).
const A4_WIDTH_MM = 210;
const A4_HEIGHT_MM = 297;

// The backend rasterises at this DPI (see pdf_render_service.rasterize_pdf_pages).
// px → mm = px / (DPI / 25.4). Keeps the board's mm grid aligned to the real
// page so chip coordinates match the overlay engine, which reads position_x/y
// as mm from the page's bottom-left.
const RASTER_DPI = 100;
const PX_PER_MM_AT_RASTER_DPI = RASTER_DPI / 25.4; // ~3.94 px/mm

// Signature chip footprint, in mm (matches the overlay's default sig box).
const SIG_WIDTH_MM = 60;
const SIG_HEIGHT_MM = 25;

// Viewport height for the scrollable strip. The chip-drag math always uses
// scrollTop, so this can be any reasonable number — it just caps how much
// is visible at once.
const VIEWPORT_HEIGHT_PX = 640;

// Named preset positions, expressed as fractions of the page (col, row from
// top). Resolved to mm against the *real* page size so a preset lands in the
// same relative spot on A4, Letter, or landscape pages alike.
const PRESET_FRAC: Record<string, { fx: number; fy: number }> = {
  'top-left':      { fx: 0.1,  fy: 0.12 },
  'top-center':    { fx: 0.5,  fy: 0.12 },
  'top-right':     { fx: 0.85, fy: 0.12 },
  'middle-left':   { fx: 0.1,  fy: 0.5  },
  'middle-center': { fx: 0.5,  fy: 0.5  },
  'middle-right':  { fx: 0.85, fy: 0.5  },
  'bottom-left':   { fx: 0.1,  fy: 0.9  },
  'bottom-center': { fx: 0.5,  fy: 0.9  },
  'bottom-right':  { fx: 0.85, fy: 0.9  },
};

type SignatureConfig = {
  signature_id: number;
  position: string;
  position_x?: number;
  position_y?: number;
  page: string | number;
};

interface SignaturePlacementBoardProps {
  signatures: any[];
  signatureConfigs: SignatureConfig[];
  onUpdateConfig: (sigId: number, updates: Partial<SignatureConfig>) => void;
  /**
   * Authoritative page count from the most recent rendered PDF preview
   * (X-PDF-Page-Count). Used to draw page boundaries and to bound multi-
   * page chip positions.
   */
  overridePageCount?: number | null;
  /**
   * URL of the rasterised PDF (all pages stacked into one tall PNG)
   * produced by the backend's /preview-pages.png endpoint. When set, the
   * board renders that image so drag positions land at the same coordinates
   * the final PDF uses. When null (e.g. preview still loading) the board
   * falls back to a plain A4 grid.
   */
  pdfPagesImageUrl?: string | null;
  /** Native pixel height of one page in the rasterised PNG (from the
   *  X-Image-Page-Height-Px header). Lets the board align page boundaries
   *  precisely with the image even if the chosen DPI changes server-side. */
  pdfPagesPageHeightPx?: number | null;
  /** Native pixel width of one page in the rasterised PNG. Combined with
   *  pdfPagesPageHeightPx, gives the exact aspect ratio per page. */
  pdfPagesPageWidthPx?: number | null;
}

/**
 * Resolve a chip's config to a (pageIndex, x_mm, y_mm) triple. y_mm is
 * measured from the bottom of that page. Presets resolve relative to the real
 * page size (pageWidthMm × pageHeightMm) so they land correctly on any shape.
 */
function resolveConfigToPagePosition(
  config: SignatureConfig,
  totalPages: number,
  pageWidthMm: number,
  pageHeightMm: number,
): { pageIndex: number; xMm: number; yMm: number } {
  let xMm: number;
  let yMm: number;

  if (config.position === 'custom' && config.position_x != null && config.position_y != null) {
    xMm = config.position_x;
    yMm = config.position_y;
  } else if (config.position in PRESET_FRAC) {
    const { fx, fy } = PRESET_FRAC[config.position];
    // fx/fy are fractions from top-left; convert to mm-from-bottom-left,
    // anchoring the chip's top-left so it reads like the preset label.
    xMm = fx * pageWidthMm - SIG_WIDTH_MM / 2;
    yMm = (1 - fy) * pageHeightMm - SIG_HEIGHT_MM / 2;
  } else {
    // Default: bottom-right.
    xMm = pageWidthMm - SIG_WIDTH_MM - 10;
    yMm = 10;
  }

  xMm = Math.max(0, Math.min(xMm, pageWidthMm - SIG_WIDTH_MM));
  yMm = Math.max(0, Math.min(yMm, pageHeightMm - SIG_HEIGHT_MM));

  let pageIndex = 0;
  if (typeof config.page === 'number' && config.page >= 1) {
    pageIndex = Math.min(config.page - 1, Math.max(0, totalPages - 1));
  } else if (config.page === 'last') {
    pageIndex = Math.max(0, totalPages - 1);
  } else {
    pageIndex = 0;
  }

  return { pageIndex, xMm, yMm };
}

export default function SignaturePlacementBoard({
  signatures,
  signatureConfigs,
  onUpdateConfig,
  overridePageCount,
  pdfPagesImageUrl,
  pdfPagesPageHeightPx,
  pdfPagesPageWidthPx,
}: SignaturePlacementBoardProps) {
  const stripRef = useRef<HTMLDivElement>(null);
  // Natural pixel size of the rasterised image, once it loads.
  const [imgNaturalSize, setImgNaturalSize] = useState<{ w: number; h: number } | null>(null);
  // Set true on image error so we can fall back to the plain grid.
  const [imgFailed, setImgFailed] = useState(false);

  useEffect(() => {
    setImgFailed(false);
    setImgNaturalSize(null);
  }, [pdfPagesImageUrl]);

  // Real page dimensions in mm, derived from the rasterised page's pixel size
  // at the known DPI. Falls back to A4 until the preview loads. These drive
  // ALL geometry so the board mirrors the true page shape (portrait, landscape,
  // Letter, …) and chip mm-coordinates match the overlay engine exactly.
  const rasterPageWidthPx = pdfPagesPageWidthPx ?? imgNaturalSize?.w ?? null;
  const rasterPageHeightPx = pdfPagesPageHeightPx ?? null;
  const pageWidthMm =
    rasterPageWidthPx && rasterPageWidthPx > 0
      ? rasterPageWidthPx / PX_PER_MM_AT_RASTER_DPI
      : A4_WIDTH_MM;
  const pageHeightMm =
    rasterPageHeightPx && rasterPageHeightPx > 0
      ? rasterPageHeightPx / PX_PER_MM_AT_RASTER_DPI
      : A4_HEIGHT_MM;

  // px-per-mm at the board's display width. Single source of truth for every
  // mm↔px conversion below (drag, click, chip placement).
  const SCALE = BOARD_WIDTH_PX / pageWidthMm;

  // One page's displayed height, in CSS pixels. Derived from the real page
  // aspect ratio so the rendered image is never squashed.
  const displayedPageHeightPx = Math.round(pageHeightMm * SCALE);

  // Chip footprint in displayed CSS pixels.
  const SIG_WIDTH_PX = Math.round(SIG_WIDTH_MM * SCALE);
  const SIG_HEIGHT_PX = Math.round(SIG_HEIGHT_MM * SCALE);

  // Compute total pages: prefer the override (which already reflects the
  // real PDF), and never go below the highest page index any chip is
  // pinned to (so chips on later pages stay visible).
  const maxPinnedPage = signatureConfigs.reduce((m, c) => {
    let p = 0;
    if (typeof c.page === 'number' && c.page >= 1) p = c.page - 1;
    else if (c.page === 'last') p = (overridePageCount ?? 1) - 1;
    return Math.max(m, p);
  }, 0);
  const totalPages = Math.max(
    1,
    overridePageCount && overridePageCount > 0 ? overridePageCount : 1,
    maxPinnedPage + 1,
  );

  const stripHeightPx = totalPages * displayedPageHeightPx;

  // ─────────────────────────────────────────────────────────────────────
  // Cursor-offset-aware drag math.
  //
  // The previous implementation positioned the chip's top-left at the
  // cursor's start position + delta. That meant if the user clicked the
  // chip's centre and dragged, the chip would appear to "lead" the cursor
  // by half its own size. By capturing the cursor's offset within the chip
  // on mousedown and subtracting it on each mousemove, the chip follows
  // the cursor exactly 1:1 — the dragged point on the chip stays under the
  // cursor for the entire drag.
  // ─────────────────────────────────────────────────────────────────────
  const handleMouseDown = (e: React.MouseEvent, sigId: number) => {
    e.preventDefault();
    e.stopPropagation();

    const config = signatureConfigs.find((s) => s.signature_id === sigId);
    if (!config) return;
    const strip = stripRef.current;
    if (!strip) return;

    const chipEl = e.currentTarget as HTMLElement;
    const chipRect = chipEl.getBoundingClientRect();
    // Offset of the cursor within the chip, in CSS pixels.
    const offsetXInChip = e.clientX - chipRect.left;
    const offsetYInChip = e.clientY - chipRect.top;

    const handleMouseMove = (moveEvent: MouseEvent) => {
      const stripRect = strip.getBoundingClientRect();
      // Cursor position in the strip's coordinate space, including
      // any scrolling that happened during the drag.
      const cursorXInStrip = moveEvent.clientX - stripRect.left + strip.scrollLeft;
      const cursorYInStrip = moveEvent.clientY - stripRect.top + strip.scrollTop;

      // The chip's top-left is the cursor minus the in-chip offset, so the
      // exact point the user grabbed remains under the cursor.
      let newLeft = cursorXInStrip - offsetXInChip;
      let newTop = cursorYInStrip - offsetYInChip;

      // Clamp horizontally to a single page width.
      newLeft = Math.max(0, Math.min(newLeft, BOARD_WIDTH_PX - SIG_WIDTH_PX));
      // Clamp vertically across the full strip.
      newTop = Math.max(0, Math.min(newTop, stripHeightPx - SIG_HEIGHT_PX));

      // Snap to whichever page the chip's vertical centre falls into.
      const newPageIndex = Math.min(
        totalPages - 1,
        Math.max(0, Math.floor((newTop + SIG_HEIGHT_PX / 2) / displayedPageHeightPx)),
      );

      // Convert pixels back to mm relative to that page.
      const yWithinPagePx = newTop - newPageIndex * displayedPageHeightPx;
      const newXMm = Math.round((newLeft / SCALE) * 10) / 10;
      const newYMm = Math.round(
        ((displayedPageHeightPx - yWithinPagePx - SIG_HEIGHT_PX) / SCALE) * 10,
      ) / 10;

      const updates: Partial<SignatureConfig> = {
        position: 'custom',
        position_x: newXMm,
        position_y: newYMm,
      };
      // Drag changes the page only for the 'all' semantic we leave alone —
      // a signature configured for every page shouldn't get reassigned to
      // a specific page by a drag.
      if (config.page !== 'all') {
        updates.page = newPageIndex + 1;
      }

      onUpdateConfig(sigId, updates);
    };

    const handleMouseUp = () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  };

  // Click on a blank area of a page → move the FIRST selected chip to that
  // location. Cheap way to add precise positioning without forcing the user
  // to hit the tiny chip body.
  const handlePageMouseDown = (e: React.MouseEvent) => {
    if (signatureConfigs.length === 0) return;
    // Only act on direct background clicks (not chip clicks bubbling up).
    if (e.target !== e.currentTarget) return;

    const strip = stripRef.current;
    if (!strip) return;
    const stripRect = strip.getBoundingClientRect();
    const cursorXInStrip = e.clientX - stripRect.left + strip.scrollLeft;
    const cursorYInStrip = e.clientY - stripRect.top + strip.scrollTop;

    const newLeft = Math.max(
      0,
      Math.min(cursorXInStrip - SIG_WIDTH_PX / 2, BOARD_WIDTH_PX - SIG_WIDTH_PX),
    );
    const newTop = Math.max(
      0,
      Math.min(cursorYInStrip - SIG_HEIGHT_PX / 2, stripHeightPx - SIG_HEIGHT_PX),
    );
    const newPageIndex = Math.min(
      totalPages - 1,
      Math.max(0, Math.floor((newTop + SIG_HEIGHT_PX / 2) / displayedPageHeightPx)),
    );
    const yWithinPagePx = newTop - newPageIndex * displayedPageHeightPx;
    const newXMm = Math.round((newLeft / SCALE) * 10) / 10;
    const newYMm = Math.round(
      ((displayedPageHeightPx - yWithinPagePx - SIG_HEIGHT_PX) / SCALE) * 10,
    ) / 10;

    const first = signatureConfigs[0];
    onUpdateConfig(first.signature_id, {
      position: 'custom',
      position_x: newXMm,
      position_y: newYMm,
      ...(first.page !== 'all' ? { page: newPageIndex + 1 } : {}),
    });
  };

  // Recompute layout when window resizes (rare, but keeps things stable).
  useLayoutEffect(() => {
    // No-op for now; placeholder so future resize-aware logic has a home.
  }, []);

  const activeConfigs = signatureConfigs.filter((c) =>
    signatures.some((s) => s.id === c.signature_id)
  );

  return (
    <div>
      <p className="text-[10px] text-muted-foreground mb-2 flex items-center gap-1">
        <Move size={10} /> Drag a chip to position it, or click anywhere on a page to send the first chip there. Scroll to reach later pages.
      </p>

      <div
        ref={stripRef}
        className="relative border border-border bg-muted rounded shadow-inner overflow-y-auto overflow-x-hidden select-none"
        style={{
          width: BOARD_WIDTH_PX,
          maxHeight: VIEWPORT_HEIGHT_PX,
        }}
      >
        <div
          className="relative bg-card"
          style={{ width: BOARD_WIDTH_PX, height: stripHeightPx }}
          onMouseDown={handlePageMouseDown}
        >
          {/* PDF pages image (full strip), or fallback grid when missing. */}
          {pdfPagesImageUrl && !imgFailed ? (
            <img
              src={pdfPagesImageUrl}
              alt="PDF pages preview"
              draggable={false}
              onLoad={(e) => {
                const img = e.currentTarget;
                setImgNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
              }}
              onError={() => setImgFailed(true)}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: BOARD_WIDTH_PX,
                height: stripHeightPx,
                pointerEvents: 'none',
                userSelect: 'none',
              }}
            />
          ) : (
            // Plain grid fallback: light dashed lines at thirds for each
            // page so the user can still gauge position roughly.
            <>
              {Array.from({ length: totalPages }).map((_, i) => (
                <div
                  key={`grid-${i}`}
                  className="absolute left-0 right-0 pointer-events-none"
                  style={{ top: i * displayedPageHeightPx, height: displayedPageHeightPx }}
                >
                  <div className="absolute top-0 bottom-0 border-l border-dashed border-border" style={{ left: '33.33%' }} />
                  <div className="absolute top-0 bottom-0 border-l border-dashed border-border" style={{ left: '66.66%' }} />
                  <div className="absolute left-0 right-0 border-t border-dashed border-border" style={{ top: '33.33%' }} />
                  <div className="absolute left-0 right-0 border-t border-dashed border-border" style={{ top: '66.66%' }} />
                </div>
              ))}
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                <p className="text-[10px] text-muted-foreground text-center px-4">
                  {pdfPagesImageUrl
                    ? 'Loading page preview…'
                    : 'Save the document and pick a letterhead to see the live page preview.'}
                </p>
              </div>
            </>
          )}

          {/* Page labels + page-break lines (above the image so the user
              can see them clearly). */}
          {Array.from({ length: totalPages }).map((_, i) => (
            <div
              key={`page-${i}`}
              className="absolute left-0 right-0 pointer-events-none"
              style={{ top: i * displayedPageHeightPx, height: displayedPageHeightPx }}
            >
              <span className="absolute top-1 left-1.5 text-[9px] font-medium text-muted-foreground bg-card/85 px-1 rounded">
                Page {i + 1}
              </span>
              {i > 0 && (
                <div className="absolute top-0 left-0 right-0 border-t-2 border-dashed border-border" />
              )}
            </div>
          ))}

          {/* Signature chips. */}
          {activeConfigs.map((config) => {
            const sig = signatures.find((s) => s.id === config.signature_id);
            if (!sig) return null;
            const { pageIndex, xMm, yMm } = resolveConfigToPagePosition(config, totalPages, pageWidthMm, pageHeightMm);
            const left = xMm * SCALE;
            const top =
              pageIndex * displayedPageHeightPx +
              (displayedPageHeightPx - yMm * SCALE - SIG_HEIGHT_PX);
            const pageLabel =
              config.page === 'all'
                ? 'all pages'
                : config.page === 'first'
                ? 'page 1'
                : config.page === 'last'
                ? `page ${totalPages}`
                : `page ${config.page}`;
            return (
              <div
                key={config.signature_id}
                className="absolute cursor-move rounded border-2 border-drpl-secondary bg-accent/10/90 flex flex-col items-center justify-center text-center px-1 shadow hover:bg-accent/15/90 z-10 select-none"
                style={{
                  left,
                  top,
                  width: SIG_WIDTH_PX,
                  height: SIG_HEIGHT_PX,
                }}
                onMouseDown={(e) => handleMouseDown(e, config.signature_id)}
                title={`Drag to reposition ${sig.name} — currently on ${pageLabel}`}
              >
                <span className="text-[9px] font-semibold text-accent truncate w-full text-center leading-tight pointer-events-none">
                  {sig.name}
                </span>
                {sig.designation && (
                  <span className="text-[8px] text-muted-foreground truncate w-full text-center leading-tight pointer-events-none">
                    {sig.designation}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Position readout per active chip. */}
      <div className="mt-2 space-y-0.5">
        {activeConfigs.map((config) => {
          const sig = signatures.find((s) => s.id === config.signature_id);
          if (!sig) return null;
          const { pageIndex, xMm, yMm } = resolveConfigToPagePosition(config, totalPages, pageWidthMm, pageHeightMm);
          const pageLabel =
            config.page === 'all'
              ? 'all pages'
              : config.page === 'first'
              ? 'page 1'
              : config.page === 'last'
              ? `page ${totalPages}`
              : `page ${pageIndex + 1}`;
          return (
            <p key={config.signature_id} className="text-[9px] text-muted-foreground">
              <span className="font-medium text-foreground">{sig.name}</span>:{' '}
              {xMm.toFixed(1)}mm from left, {yMm.toFixed(1)}mm from bottom ({pageLabel})
            </p>
          );
        })}
      </div>
    </div>
  );
}
