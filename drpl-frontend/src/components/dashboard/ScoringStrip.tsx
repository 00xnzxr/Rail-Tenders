import { useEffect, useState } from 'react';
import { Zap } from 'lucide-react';
import { getScoringBacklog, drainScoring, type ScoringBacklog } from '../../lib/api';

export default function ScoringStrip() {
  const [backlog, setBacklog] = useState<ScoringBacklog | null>(null);
  const [draining, setDraining] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    try { setBacklog(await getScoringBacklog()); } catch { /* best-effort */ }
  };
  useEffect(() => { void load(); }, []);

  const drain = async () => {
    if (!backlog) return;
    setDraining(true);
    setMsg(null);
    try {
      const mode = backlog.drainable > 50 ? 'batch' : 'live';
      const r = await drainScoring(mode);
      setMsg(mode === 'batch'
        ? `Submitted ${r.submitted ?? backlog.drainable} for scoring${r.deduped_saved ? ` (${r.deduped_saved} dupes skipped)` : ''}.`
        : `Scored ${r.scored ?? 0}. ${r.failed ?? 0} failed.`);
      await load();
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || 'Scoring failed. Check API key.');
    } finally {
      setDraining(false);
    }
  };

  if (!backlog) return null;

  return (
    <div className="flex items-center gap-4 flex-wrap bg-card border border-border rounded-xl px-5 py-3 shadow-card">
      <span className="text-xs text-muted-foreground">
        <span className="font-bold text-foreground">{backlog.scored.toLocaleString()}</span> scored ·{' '}
        <span className="font-bold text-foreground">{backlog.pending.toLocaleString()}</span> pending
      </span>
      {msg && <span className="text-xs text-accent">{msg}</span>}
      <div className="flex-1" />
      <button
        onClick={drain}
        disabled={draining || backlog.drainable === 0}
        className="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-50"
      >
        <Zap size={13} /> {draining ? 'Scoring…' : 'Score Pending'}
      </button>
    </div>
  );
}
