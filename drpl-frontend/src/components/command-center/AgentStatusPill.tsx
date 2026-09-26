import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';

export interface AgentStatusPillProps {
  message: string;
  startedAtMs: number;
}

export function AgentStatusPill({ message, startedAtMs }: AgentStatusPillProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(id);
  }, []);

  const elapsedMs = Math.max(0, now - startedAtMs);
  const elapsed =
    elapsedMs < 10_000
      ? `${(elapsedMs / 1000).toFixed(1)}s`
      : `${Math.round(elapsedMs / 1000)}s`;

  return (
    <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground bg-muted/40 border border-border rounded px-2 py-1 w-fit">
      <Loader2 size={12} className="animate-spin text-muted-foreground" />
      <span>{message}…</span>
      <span className="tabular-nums text-muted-foreground">{elapsed}</span>
    </div>
  );
}
