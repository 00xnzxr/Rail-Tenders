import { useState } from 'react';
import { ChevronDown, ChevronRight, Wrench, Clock, CheckCircle2, XCircle } from 'lucide-react';

interface ToolCallProps {
  tool: string;
  input?: Record<string, any>;
  output?: string;
  latencyMs?: number;
  status?: 'success' | 'error';
}

export default function ToolCallTrace({ tool, input, output, latencyMs, status = 'success' }: ToolCallProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="border border-border rounded-lg bg-muted/40 text-sm">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-muted transition-colors rounded-lg"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Wrench size={14} className="text-accent" />
        <span className="font-medium text-foreground">{tool}</span>
        {latencyMs !== undefined && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground ml-auto">
            <Clock size={12} />
            {latencyMs}ms
          </span>
        )}
        {status === 'success' ? (
          <CheckCircle2 size={14} className="text-green-500" />
        ) : (
          <XCircle size={14} className="text-red-500" />
        )}
      </button>

      {expanded && (
        <div className="px-3 pb-3 space-y-2">
          {input && (
            <div>
              <p className="text-xs font-semibold text-muted-foreground mb-1">Input</p>
              <pre className="text-xs bg-card border border-border rounded p-2 overflow-x-auto max-h-40">
                {typeof input === 'string' ? input : JSON.stringify(input, null, 2)}
              </pre>
            </div>
          )}
          {output && (
            <div>
              <p className="text-xs font-semibold text-muted-foreground mb-1">Output</p>
              <pre className="text-xs bg-card border border-border rounded p-2 overflow-x-auto max-h-40">
                {output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
