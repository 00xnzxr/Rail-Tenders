import { useEffect, useState } from 'react';
import { Save, RotateCcw, AlertCircle, CheckCircle2, Gauge, RefreshCw } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getScoringSettings,
  updateScoringSettings,
  getScoringStats,
  getScoringDigest,
  regenerateScoringDigest,
  getScoringBacklog,
  drainScoring,
  type ScoringBacklog,
} from '../../lib/api';

interface ScoringStats {
  total_scored: number;
  unscored_backlog: number;
  last_reaper_run: string | null;
  counts: { to_bid: number; not_bidable: number; discarded: number; unscored: number };
}

/**
 * Auto-tender-scoring — admin control panel.
 *
 * Edits the scoring-agent settings (model, cadence, thresholds, cleanup),
 * surfaces live scoring stats, and shows the latest digest with a manual
 * regenerate action.
 */
export default function AdminTenderScoringPage() {
  const [settings, setSettings] = useState<Record<string, any> | null>(null);
  const [original, setOriginal] = useState<Record<string, any> | null>(null);
  const [stats, setStats] = useState<ScoringStats | null>(null);
  const [digest, setDigest] = useState<string | null>(null);
  const [backlog, setBacklog] = useState<ScoringBacklog | null>(null);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resegmenting, setResegmenting] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [draining, setDraining] = useState(false);
  const [drainMsg, setDrainMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, st, d, bl] = await Promise.all([
        getScoringSettings(),
        getScoringStats(),
        getScoringDigest(),
        getScoringBacklog(),
      ]);
      setSettings(toUiSettings(s));
      setOriginal(JSON.parse(JSON.stringify(toUiSettings(s))));
      setStats(st);
      setDigest(d.digest);
      setBacklog(bl);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to load scoring settings.');
    } finally {
      setLoading(false);
    }
  };

  const refreshStats = async () => {
    try {
      const st = await getScoringStats();
      setStats(st);
    } catch {
      // non-fatal — keep showing stale stats
    }
  };

  // Build the wire payload: convert threshold % fields (0-100 in UI state) back to 0-1.
  const buildPayload = (s: Record<string, any>) => ({
    ...s,
    segment_discard_below: (s.segment_discard_below ?? 0) / 100,
    segment_bidable_at: (s.segment_bidable_at ?? 0) / 100,
  });

  const save = async () => {
    if (!settings) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateScoringSettings(buildPayload(settings));
      setSettings(toUiSettings(updated));
      setOriginal(JSON.parse(JSON.stringify(toUiSettings(updated))));
      setSavedAt(new Date().toLocaleTimeString());
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to save settings.');
    } finally {
      setSaving(false);
    }
  };

  const applyAndResegment = async () => {
    if (!settings) return;
    setResegmenting(true);
    setError(null);
    try {
      const updated = await updateScoringSettings(buildPayload(settings), true);
      setSettings(toUiSettings(updated));
      setOriginal(JSON.parse(JSON.stringify(toUiSettings(updated))));
      setSavedAt(new Date().toLocaleTimeString());
      await refreshStats();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to apply & re-segment.');
    } finally {
      setResegmenting(false);
    }
  };

  const revert = () => {
    if (original) setSettings(JSON.parse(JSON.stringify(original)));
  };

  const runDrain = async (mode: 'live' | 'batch') => {
    setDraining(true);
    setDrainMsg(null);
    try {
      const result = await drainScoring(mode);
      setDrainMsg(
        mode === 'batch'
          ? `Submitted ${result.submitted ?? 0} tenders for batch scoring${
              result.deduped_saved ? ` (${result.deduped_saved} duplicates skipped)` : ''
            }.`
          : `Scored ${result.scored ?? 0} tenders. ${result.failed ?? 0} failed.`,
      );
      setBacklog(await getScoringBacklog());
      await refreshStats();
    } catch (e: any) {
      setDrainMsg(e?.response?.data?.detail || e?.message || 'Scoring drain failed.');
    } finally {
      setDraining(false);
    }
  };

  const regenerateDigest = async () => {
    setRegenerating(true);
    setError(null);
    try {
      const d = await regenerateScoringDigest();
      setDigest(d.digest);
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to regenerate digest.');
    } finally {
      setRegenerating(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col h-full">
        <Header title="Tender Scoring Agent" subtitle="Auto-scoring, segmentation & cleanup" />
        <div className="flex-1 flex items-center justify-center">
          <LoadingSpinner />
        </div>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="flex flex-col h-full">
        <Header title="Tender Scoring Agent" subtitle="Auto-scoring, segmentation & cleanup" />
        <div className="flex-1 p-6">
          <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded p-4 text-red-700 dark:text-red-400 text-sm">
            {error || 'Settings unavailable.'}
          </div>
        </div>
      </div>
    );
  }

  const dirty = JSON.stringify(settings) !== JSON.stringify(original);

  return (
    <div className="flex flex-col h-full">
      <Header
        title="Tender Scoring Agent"
        subtitle="Controls the background scoring/segmentation worker and stale-tender cleanup"
      />

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Status banner */}
          <div className="flex items-center justify-between bg-card border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-sm">
              <Gauge size={16} className="text-accent" />
              <span className="font-medium text-foreground">Scoring agent:</span>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!settings.auto_scoring_enabled}
                  onChange={(e) => setSettings({ ...settings, auto_scoring_enabled: e.target.checked })}
                  className="rounded border-border"
                />
                <span className="text-xs text-muted-foreground">Enabled</span>
              </label>
            </div>
            <div className="flex items-center gap-2">
              {savedAt && !dirty && (
                <span className="flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                  <CheckCircle2 size={14} /> Saved at {savedAt}
                </span>
              )}
              {dirty && (
                <button
                  onClick={revert}
                  className="px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted rounded inline-flex items-center gap-1"
                >
                  <RotateCcw size={14} /> Revert
                </button>
              )}
              <button
                onClick={applyAndResegment}
                disabled={resegmenting || saving}
                className="px-3 py-1.5 text-xs font-semibold text-accent border border-accent/40 hover:bg-accent/10 disabled:opacity-50 rounded inline-flex items-center gap-1"
              >
                <RefreshCw size={14} /> {resegmenting ? 'Applying…' : 'Apply & re-segment'}
              </button>
              <button
                onClick={save}
                disabled={saving || resegmenting || !dirty}
                className="px-4 py-1.5 text-xs font-semibold text-white bg-accent hover:bg-accent/90 disabled:opacity-50 rounded inline-flex items-center gap-1"
              >
                <Save size={14} /> {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-2 bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded p-3">
              <AlertCircle size={16} className="text-red-600 dark:text-red-400 mt-0.5 shrink-0" />
              <p className="text-sm text-red-700 dark:text-red-400">{error}</p>
            </div>
          )}

          {/* Controls */}
          <Section title="Run Controls" description="Model, cadence, and batch/retry limits for the background scoring worker.">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <TextField
                label="Model"
                value={settings.auto_scoring_model ?? ''}
                onChange={(v) => setSettings({ ...settings, auto_scoring_model: v })}
              />
              <NumericField
                label="Interval (seconds)"
                value={settings.auto_scoring_interval_seconds ?? null}
                onChange={(v) => setSettings({ ...settings, auto_scoring_interval_seconds: v })}
              />
              <NumericField
                label="Batch size"
                value={settings.auto_scoring_batch_size ?? null}
                onChange={(v) => setSettings({ ...settings, auto_scoring_batch_size: v })}
              />
              <NumericField
                label="Max retries"
                value={settings.auto_scoring_max_retries ?? null}
                onChange={(v) => setSettings({ ...settings, auto_scoring_max_retries: v })}
              />
            </div>
          </Section>

          <Section title="Segmentation Thresholds" description="Where a tender's fit score lands it: discarded, unscored/needs-review, or ready to bid.">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <NumericField
                label="Discard below (%)"
                value={settings.segment_discard_below ?? null}
                onChange={(v) => setSettings({ ...settings, segment_discard_below: v })}
                max={100}
              />
              <NumericField
                label="Bidable at (%)"
                value={settings.segment_bidable_at ?? null}
                onChange={(v) => setSettings({ ...settings, segment_bidable_at: v })}
                max={100}
              />
              <NumericField
                label="Value threshold (INR)"
                value={settings.value_threshold_inr ?? null}
                onChange={(v) => setSettings({ ...settings, value_threshold_inr: v })}
              />
            </div>
          </Section>

          <Section title="Stale Tender Cleanup" description="Automatically discard tenders that have sat unscored/unbid past this age.">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 items-end">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!settings.auto_discard_enabled}
                  onChange={(e) => setSettings({ ...settings, auto_discard_enabled: e.target.checked })}
                  className="rounded border-border"
                />
                <span className="text-xs text-muted-foreground">Enable auto-discard cleanup</span>
              </label>
              <NumericField
                label="Discard after (days)"
                value={settings.auto_discard_days ?? null}
                onChange={(v) => setSettings({ ...settings, auto_discard_days: v })}
              />
            </div>
          </Section>

          <Section
            title="Archive & Purge Sweep"
            description="Background sweep that archives past-due, untouched tenders and then permanently deletes them."
          >
            <p className="text-xs text-muted-foreground mb-4">
              When enabled, tenders whose closing date passed more than the grace period ago — and
              that nobody has assigned, worked on, or hand-segmented — are archived. Archived rows
              with reason <code>past_due</code> are then <strong>permanently deleted</strong> once
              they have been archived for longer than the purge period. Deletion removes the tender
              and all of its child rows and cannot be undone. Sub-threshold
              (<code>below_threshold</code>) tenders are in scope too, so some rows can be deleted
              having never appeared in the default tender list. Run{' '}
              <code>scripts/archive_sweep_dryrun.py</code> against the target database first to see
              exactly what would be archived and purged before turning this on.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 items-end">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!settings.archive_sweep_enabled}
                  onChange={(e) =>
                    setSettings({ ...settings, archive_sweep_enabled: e.target.checked })
                  }
                  className="rounded border-border"
                />
                <span className="text-xs text-muted-foreground">Enable archive &amp; purge sweep</span>
              </label>
              <NumericField
                label="Grace before archive (days)"
                value={settings.archive_grace_days ?? null}
                onChange={(v) => setSettings({ ...settings, archive_grace_days: v })}
              />
              <NumericField
                label={
                  (settings.archive_purge_days ?? 0) <= 0
                    ? 'Purge after archived (days) — 0 = never delete'
                    : 'Purge after archived (days)'
                }
                value={settings.archive_purge_days ?? null}
                onChange={(v) => setSettings({ ...settings, archive_purge_days: v })}
              />
              <NumericField
                label="Sweep interval (hours)"
                value={settings.archive_sweep_interval_hours ?? null}
                onChange={(v) => setSettings({ ...settings, archive_sweep_interval_hours: v })}
              />
              <NumericField
                label="Sweep batch size (rows per tick)"
                value={settings.archive_sweep_batch_size ?? null}
                onChange={(v) => setSettings({ ...settings, archive_sweep_batch_size: v })}
              />
            </div>
          </Section>

          {/* Backlog + drain */}
          {backlog && (
            <Section
              title="Scoring Backlog"
              description="Unscored tenders and one-click drain. Batch mode is ~50% cheaper (async, dedupes duplicates); live mode is immediate."
            >
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
                <StatTile label="Scored" value={backlog.scored} />
                <StatTile label="Pending" value={backlog.pending} />
                <StatTile label="Drainable" value={backlog.drainable} />
                <StatTile label="Batches in flight" value={backlog.in_flight_batches} />
              </div>
              <p className="text-xs text-muted-foreground mb-4">
                Est. batch cost ≈ ₹{backlog.est_cost_inr} · auto-scoring{' '}
                {backlog.enabled ? 'enabled' : 'disabled'} · {backlog.stuck_at_cap} stuck at retry cap ·
                last reaper run:{' '}
                {backlog.last_reaper_run ? new Date(backlog.last_reaper_run).toLocaleString() : '—'}
              </p>
              {drainMsg && (
                <div className="mb-4 px-3 py-2 bg-accent/10 border border-accent/20 rounded text-xs text-accent">
                  {drainMsg}
                </div>
              )}
              <div className="flex flex-wrap gap-2">
                <button
                  disabled={draining || backlog.drainable === 0}
                  onClick={() => runDrain('live')}
                  className="px-3 py-2 rounded text-xs font-semibold border border-border hover:bg-muted disabled:opacity-50"
                >
                  {draining ? 'Working…' : 'Drain now (fast, full price)'}
                </button>
                <button
                  disabled={draining || backlog.drainable === 0}
                  onClick={() => runDrain('batch')}
                  className="px-3 py-2 rounded text-xs font-semibold text-white bg-accent hover:bg-accent/90 disabled:opacity-50"
                >
                  {draining ? 'Working…' : 'Submit batch (cheap, async)'}
                </button>
              </div>
            </Section>
          )}

          {/* Stats */}
          {stats && (
            <Section title="Live Stats" description="Snapshot of the scoring backlog and the last stale-tender reaper run.">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
                <StatTile label="Total scored" value={stats.total_scored} />
                <StatTile label="Unscored backlog" value={stats.unscored_backlog} />
                <StatTile
                  label="Last reaper run"
                  value={stats.last_reaper_run ? new Date(stats.last_reaper_run).toLocaleString() : 'Never'}
                  small
                />
                <StatTile label="Unscored" value={stats.counts.unscored} />
              </div>
              <div className="grid grid-cols-3 gap-4">
                <SegmentBadge label="To Bid" value={stats.counts.to_bid} tone="emerald" />
                <SegmentBadge label="Not Bidable" value={stats.counts.not_bidable} tone="amber" />
                <SegmentBadge label="Discarded" value={stats.counts.discarded} tone="red" />
              </div>
            </Section>
          )}

          {/* Digest */}
          <Section title="Latest Digest" description="Most recent summary produced by the scoring agent for review.">
            <div className="flex justify-end mb-2">
              <button
                onClick={regenerateDigest}
                disabled={regenerating}
                className="px-3 py-1.5 text-xs font-medium text-accent border border-accent/40 hover:bg-accent/10 disabled:opacity-50 rounded inline-flex items-center gap-1"
              >
                <RefreshCw size={14} /> {regenerating ? 'Regenerating…' : 'Regenerate'}
              </button>
            </div>
            <pre className="text-xs whitespace-pre-wrap bg-muted/40 border border-border rounded-md p-3 max-h-96 overflow-y-auto text-foreground">
              {digest || 'No digest generated yet.'}
            </pre>
          </Section>
        </div>
      </div>
    </div>
  );
}

// Converts a settings payload straight from the API (0-1 thresholds) into the
// UI's percentage representation.
function toUiSettings(s: Record<string, any>): Record<string, any> {
  return {
    ...s,
    segment_discard_below: (s.segment_discard_below ?? 0) * 100,
    segment_bidable_at: (s.segment_bidable_at ?? 0) * 100,
  };
}

// --- Subcomponents ---

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-card border border-border rounded-lg p-5">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <p className="text-xs text-muted-foreground mt-1 mb-4">{description}</p>
      {children}
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-muted-foreground mb-1">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full text-sm px-2.5 py-1.5 border border-border rounded focus:border-accent/60 outline-none"
      />
    </div>
  );
}

function NumericField({
  label,
  value,
  onChange,
  max,
}: {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
  max?: number;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-muted-foreground mb-1">{label}</label>
      <input
        type="number"
        value={value ?? ''}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v === '' ? null : Number(v));
        }}
        className="w-full text-sm px-2.5 py-1.5 border border-border rounded focus:border-accent/60 outline-none"
        placeholder="Optional"
        min={0}
        max={max}
      />
    </div>
  );
}

function StatTile({ label, value, small }: { label: string; value: string | number; small?: boolean }) {
  return (
    <div className="bg-muted/40 border border-border rounded-md p-3">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className={small ? 'text-sm font-semibold text-foreground' : 'text-lg font-semibold text-foreground'}>
        {value}
      </div>
    </div>
  );
}

function SegmentBadge({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: 'emerald' | 'amber' | 'red';
}) {
  const toneClasses: Record<string, string> = {
    emerald: 'bg-emerald-50 dark:bg-emerald-500/15 border-emerald-200 dark:border-emerald-500/20 text-emerald-700 dark:text-emerald-400',
    amber: 'bg-amber-50 dark:bg-amber-500/15 border-amber-200 dark:border-amber-500/20 text-amber-700 dark:text-amber-400',
    red: 'bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/20 text-red-700 dark:text-red-400',
  };
  return (
    <div className={`border rounded-md p-3 text-center ${toneClasses[tone]}`}>
      <div className="text-lg font-semibold">{value}</div>
      <div className="text-xs">{label}</div>
    </div>
  );
}
