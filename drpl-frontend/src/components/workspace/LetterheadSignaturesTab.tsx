import { AlertTriangle, RectangleHorizontal, RectangleVertical, Loader2, Info } from 'lucide-react';
import LetterheadPicker from './LetterheadPicker';
import { useLetterheadAndSignatureLists } from './useLetterheadAndSignatureLists';
import type { DocumentWorkspaceDetail } from '../../types/workspace';

interface Props {
  workspace: DocumentWorkspaceDetail['workspace'];
  onLetterheadChange: (templateId: number | null) => void;
  onOrientationChange: (orientation: 'portrait' | 'landscape') => void;
  /** Legacy coordinate-placed signatures, if any. Surfaces a one-click
   *  "clear legacy placements" button so docs migrated from the old UI
   *  don't end up with both inline signatures AND overlay signatures
   *  stacked on the final PDF. */
  legacySignatureCount: number;
  onClearLegacySignatures: () => void;
}

/**
 * "Letterhead & Page" tab. Renders the letterhead picker and orientation
 * toggle for the workspace document. Signature insertion lives in the
 * editor toolbar (Word-style inline image) — the legacy coordinate-based
 * placement board has been removed; the tab only surfaces a one-click
 * cleanup if a doc has stale signatures_json from the previous UI.
 */
export default function LetterheadSignaturesTab({
  workspace,
  onLetterheadChange,
  onOrientationChange,
  legacySignatureCount,
  onClearLegacySignatures,
}: Props) {
  const { templates, loading, error } = useLetterheadAndSignatureLists();

  const isLandscape = workspace.page_orientation === 'landscape';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Letterhead Template
          </label>
          <LetterheadPicker
            templates={templates}
            value={workspace.letterhead_template_id}
            onChange={onLetterheadChange}
            disabled={loading}
          />
        </div>
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Page Orientation
          </label>
          <button
            type="button"
            onClick={() => onOrientationChange(isLandscape ? 'portrait' : 'landscape')}
            title={`Switch to ${isLandscape ? 'portrait' : 'landscape'} orientation`}
            className="inline-flex items-center gap-1.5 border border-border text-foreground bg-card px-3 py-2 rounded-lg text-sm font-medium hover:bg-muted/40 transition-colors"
          >
            {isLandscape
              ? <><RectangleHorizontal size={14} /> Landscape</>
              : <><RectangleVertical size={14} /> Portrait</>}
          </button>
        </div>
        {loading && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 size={12} className="animate-spin" /> Loading lists…
          </span>
        )}
      </div>

      {error && (
        <div className="text-xs text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded-lg px-3 py-2">
          {error}
        </div>
      )}

      {workspace.letterhead_template_id == null && (
        <div className="flex items-start gap-2 text-xs text-amber-800 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-lg px-3 py-2">
          <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
          <span>
            No letterhead selected — the finalized PDF will use a plain page.
          </span>
        </div>
      )}

      <div className="flex items-start gap-2 text-xs text-muted-foreground bg-muted/40 border border-border rounded-lg px-3 py-2">
        <Info size={14} className="flex-shrink-0 mt-0.5 text-muted-foreground" />
        <span>
          <span className="font-medium">To insert a signature:</span> click the{' '}
          <span className="inline-flex items-center gap-0.5 text-foreground font-medium">
            Sign ▾
          </span>{' '}
          button in the editor toolbar and choose one — it will be placed
          at the cursor position, just like inserting a picture in Word.
        </span>
      </div>

      {legacySignatureCount > 0 && (
        <div className="flex items-start gap-2 text-xs text-amber-800 dark:text-amber-400 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-lg px-3 py-2">
          <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p>
              <span className="font-medium">
                {legacySignatureCount} legacy signature
                {legacySignatureCount === 1 ? '' : 's'} placed by coordinates
              </span>{' '}
              still applies to the final PDF. To switch fully to the new
              inline approach, clear it and re-insert via the toolbar.
            </p>
            <button
              type="button"
              onClick={onClearLegacySignatures}
              className="mt-1.5 inline-flex items-center gap-1.5 px-2 py-1 rounded border border-amber-300 bg-card hover:bg-amber-100 dark:bg-amber-500/20 text-amber-900 font-medium"
            >
              Clear legacy placements
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
