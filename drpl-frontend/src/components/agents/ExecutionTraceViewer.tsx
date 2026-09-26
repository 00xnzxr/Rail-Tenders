import React, { useState } from 'react';
import {
  Brain, Wrench, ChevronDown, ChevronRight, Clock, Coins, Hash,
  CheckCircle2, XCircle, Loader2, MessageSquare
} from 'lucide-react';

interface ExecutionStep {
  type: 'llm_call' | 'tool_call';
  tool_name?: string;
  input?: any;
  output?: any;
  tokens_input?: number;
  tokens_output?: number;
  latency_ms?: number;
  status?: 'success' | 'error';
}

interface ExecutionTraceViewerProps {
  steps: ExecutionStep[];
  finalOutput?: string;
  totalTokens?: number;
  totalLatencyMs?: number;
  totalCost?: number;
  isRunning?: boolean;
}

function formatJson(value: any): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function truncate(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false };
  return { text: text.slice(0, max), truncated: true };
}

function Badge({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${className}`}>
      {children}
    </span>
  );
}

function StepCard({ step, index, expanded, onToggle }: {
  step: ExecutionStep; index: number; expanded: boolean; onToggle: () => void;
}) {
  const [showFullOutput, setShowFullOutput] = useState(false);
  const isLlm = step.type === 'llm_call';
  const outputStr = step.output ? formatJson(step.output) : '';
  const { text: truncatedOutput, truncated: isOutputTruncated } = truncate(outputStr, 500);

  return (
    <div className="flex gap-4">
      <div className="flex flex-col items-center w-8 shrink-0">
        <div className="w-7 h-7 rounded-full bg-accent text-accent-foreground flex items-center justify-center text-xs font-bold z-10">
          {index + 1}
        </div>
        <div className="w-px flex-1 bg-muted" />
      </div>
      <div className="flex-1 mb-4">
        <div
          className="border border-border rounded-lg bg-card shadow-sm cursor-pointer hover:border-border transition-colors"
          onClick={onToggle}
        >
          <div className="flex items-center justify-between px-4 py-3">
            <div className="flex items-center gap-2">
              {expanded ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
              {isLlm ? <Brain className="w-4 h-4 text-accent" /> : <Wrench className="w-4 h-4 text-muted-foreground" />}
              <span className="font-medium text-sm text-foreground">
                {isLlm ? 'LLM Call' : step.tool_name || 'Tool Call'}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {!isLlm && step.status && (
                step.status === 'success'
                  ? <CheckCircle2 className="w-4 h-4 text-green-500" />
                  : <XCircle className="w-4 h-4 text-red-500" />
              )}
              {isLlm && (step.tokens_input != null || step.tokens_output != null) && (
                <Badge className="bg-accent/10 text-accent">
                  <Hash className="w-3 h-3" />
                  {(step.tokens_input || 0) + (step.tokens_output || 0)} tok
                </Badge>
              )}
              {step.latency_ms != null && (
                <Badge className="bg-muted text-muted-foreground">
                  <Clock className="w-3 h-3" />
                  {step.latency_ms}ms
                </Badge>
              )}
            </div>
          </div>
          {expanded && (
            <div className="px-4 pb-4 border-t border-border pt-3 space-y-3" onClick={(e) => e.stopPropagation()}>
              {!isLlm && step.input && (
                <div>
                  <p className="text-xs font-semibold text-muted-foreground mb-1 uppercase tracking-wide">Input</p>
                  <pre className="text-xs bg-muted/40 rounded p-3 overflow-x-auto text-foreground whitespace-pre-wrap">
                    {formatJson(step.input)}
                  </pre>
                </div>
              )}
              {outputStr && (
                <div>
                  <p className="text-xs font-semibold text-muted-foreground mb-1 uppercase tracking-wide">
                    {isLlm ? 'Response' : 'Output'}
                  </p>
                  <pre className="text-xs bg-muted/40 rounded p-3 overflow-x-auto text-foreground whitespace-pre-wrap">
                    {showFullOutput || !isOutputTruncated ? outputStr : truncatedOutput + '...'}
                  </pre>
                  {isOutputTruncated && (
                    <button
                      className="text-xs text-accent hover:text-accent/80 mt-1 font-medium"
                      onClick={() => setShowFullOutput((v) => !v)}
                    >
                      {showFullOutput ? 'Show less' : 'Show more'}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ExecutionTraceViewer({
  steps, finalOutput, totalTokens, totalLatencyMs, totalCost, isRunning
}: ExecutionTraceViewerProps) {
  const [expandedSteps, setExpandedSteps] = useState<Set<number>>(new Set());

  const toggleStep = (index: number) => {
    setExpandedSteps((prev) => {
      const next = new Set(prev);
      next.has(index) ? next.delete(index) : next.add(index);
      return next;
    });
  };

  return (
    <div className="space-y-2">
      {steps.map((step, i) => (
        <StepCard
          key={i}
          step={step}
          index={i}
          expanded={expandedSteps.has(i)}
          onToggle={() => toggleStep(i)}
        />
      ))}

      {isRunning && (
        <div className="flex gap-4">
          <div className="flex flex-col items-center w-8 shrink-0">
            <div className="w-7 h-7 rounded-full bg-accent/15 flex items-center justify-center z-10">
              <Loader2 className="w-4 h-4 text-accent animate-spin" />
            </div>
          </div>
          <div className="flex items-center gap-2 py-2">
            <span className="relative flex h-2.5 w-2.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-accent/100" />
            </span>
            <span className="text-sm text-muted-foreground font-medium">Processing...</span>
          </div>
        </div>
      )}

      {finalOutput && (
        <div className="flex gap-4">
          <div className="flex flex-col items-center w-8 shrink-0">
            <div className="w-7 h-7 rounded-full bg-accent text-accent-foreground flex items-center justify-center z-10">
              <MessageSquare className="w-3.5 h-3.5" />
            </div>
          </div>
          <div className="flex-1 mb-4">
            <div className="border-2 border-accent/40 rounded-lg bg-accent/10 shadow-sm">
              <div className="px-4 py-3">
                <p className="text-sm font-semibold text-accent mb-2">Final Output</p>
                <pre className="text-sm bg-card rounded p-3 overflow-x-auto text-foreground whitespace-pre-wrap border border-accent/20">
                  {finalOutput}
                </pre>
              </div>
            </div>
          </div>
        </div>
      )}

      {(totalTokens != null || totalLatencyMs != null || totalCost != null) && (
        <div className="flex items-center gap-3 pt-2 border-t border-border mt-2 pl-12">
          {totalTokens != null && (
            <Badge className="bg-muted text-muted-foreground">
              <Hash className="w-3 h-3" /> {totalTokens.toLocaleString()} tokens
            </Badge>
          )}
          {totalLatencyMs != null && (
            <Badge className="bg-muted text-muted-foreground">
              <Clock className="w-3 h-3" /> {(totalLatencyMs / 1000).toFixed(1)}s
            </Badge>
          )}
          {totalCost != null && (
            <Badge className="bg-muted text-muted-foreground">
              <Coins className="w-3 h-3" /> ${totalCost.toFixed(4)}
            </Badge>
          )}
        </div>
      )}
    </div>
  );
}
