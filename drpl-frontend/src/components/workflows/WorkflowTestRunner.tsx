import { useState, useRef, useCallback } from 'react';
import {
  Play, Square, Loader2, CheckCircle2, XCircle, Clock,
  ChevronDown, ChevronRight, AlertCircle, Pause, Paperclip, X,
} from 'lucide-react';
import { getWorkflowTestStreamUrl } from '../../lib/api';

interface NodeTrace {
  node_key: string;
  node_type: string;
  display_name?: string;
  status: 'running' | 'completed' | 'failed' | 'skipped';
  latency_ms?: number;
  output_data?: any;
  agent_key?: string;
  error?: string;
  subgraph_parent?: string;
}

interface WorkflowTestRunnerProps {
  workflowId: number;
  onActiveNode: (nodeKey: string | null) => void;
}

export default function WorkflowTestRunner({ workflowId, onActiveNode }: WorkflowTestRunnerProps) {
  const [message, setMessage] = useState('');
  const [tenderId, setTenderId] = useState('');
  const [testFiles, setTestFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [running, setRunning] = useState(false);
  const [nodeTraces, setNodeTraces] = useState<NodeTrace[]>([]);
  const [streamedText, setStreamedText] = useState('');
  const [executionStatus, setExecutionStatus] = useState<string | null>(null);
  const [executionId, setExecutionId] = useState<number | null>(null);
  const [totalLatency, setTotalLatency] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedNode, setExpandedNode] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const runTest = useCallback(async () => {
    if (!message.trim() || running) return;

    // Reset state
    setRunning(true);
    setNodeTraces([]);
    setStreamedText('');
    setExecutionStatus(null);
    setExecutionId(null);
    setTotalLatency(null);
    setError(null);
    setExpandedNode(null);
    onActiveNode(null);

    const controller = new AbortController();
    abortRef.current = controller;

    const token = localStorage.getItem('drpl_token');
    const url = getWorkflowTestStreamUrl(workflowId);

    try {
      // Build FormData to support file uploads
      const formData = new FormData();
      formData.append('message', message.trim());
      if (tenderId) formData.append('tender_id', tenderId);
      testFiles.forEach((f) => formData.append('files', f));

      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          // Don't set Content-Type — browser sets it with boundary for FormData
        },
        body: formData,
        signal: controller.signal,
      });

      if (!response.ok) {
        const errText = await response.text();
        setError(`HTTP ${response.status}: ${errText}`);
        setRunning(false);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        setError('No response stream');
        setRunning(false);
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';
      let fullText = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        let eventType = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith('data: ') && eventType) {
            try {
              const data = JSON.parse(line.slice(6));
              handleSSEEvent(eventType, data);
            } catch {
              // Non-JSON data — append as text
              if (eventType === 'token') {
                fullText += line.slice(6);
                setStreamedText(fullText);
              }
            }
            eventType = '';
          }
        }
      }

      function handleSSEEvent(type: string, data: any) {
        switch (type) {
          case 'workflow_start':
            setExecutionId(data.execution_id);
            break;

          case 'workflow_node_start':
            onActiveNode(data.node_key);
            setNodeTraces((prev) => [
              ...prev,
              {
                node_key: data.node_key,
                node_type: data.node_type,
                display_name: data.display_name,
                status: 'running',
                subgraph_parent: data.subgraph_parent,
              },
            ]);
            break;

          case 'workflow_node_complete':
            onActiveNode(null);
            setNodeTraces((prev) =>
              prev.map((t) =>
                t.node_key === data.node_key && t.status === 'running'
                  ? { ...t, status: data.status || 'completed' }
                  : t,
              ),
            );
            break;

          case 'agent_start':
            setNodeTraces((prev) => {
              const last = prev[prev.length - 1];
              if (last && last.status === 'running') {
                return [...prev.slice(0, -1), { ...last, agent_key: data.agent_key }];
              }
              return prev;
            });
            break;

          case 'token':
            fullText += data.content || '';
            setStreamedText(fullText);
            break;

          case 'agent_complete':
            break;

          case 'routing':
            break;

          case 'for_each_iteration_start':
            setNodeTraces((prev) => [
              ...prev,
              {
                node_key: `${data.parent_node}_iter_${data.index}`,
                node_type: 'for_each_iteration',
                display_name: `Iteration ${data.index + 1}/${data.total}: ${data.item_name || ''}`,
                status: 'running',
                subgraph_parent: data.parent_node,
              },
            ]);
            break;

          case 'for_each_iteration_complete':
            setNodeTraces((prev) =>
              prev.map((t) =>
                t.node_key === `${data.parent_node}_iter_${data.index}` && t.status === 'running'
                  ? { ...t, status: 'completed' }
                  : t,
              ),
            );
            break;

          case 'parallel_branch_start':
            setNodeTraces((prev) => [
              ...prev,
              {
                node_key: `${data.parent_node}_branch_${data.branch}`,
                node_type: 'parallel_branch',
                display_name: `Branch: ${data.branch}`,
                status: 'running',
                subgraph_parent: data.parent_node,
              },
            ]);
            break;

          case 'parallel_branch_complete':
            setNodeTraces((prev) =>
              prev.map((t) =>
                t.node_key === `${data.parent_node}_branch_${data.branch}` && t.status === 'running'
                  ? { ...t, status: 'completed' }
                  : t,
              ),
            );
            break;

          case 'workflow_paused':
            setExecutionStatus('paused');
            setError(`Paused at ${data.node_key}: ${data.prompt}`);
            break;

          case 'workflow_error':
            setNodeTraces((prev) =>
              prev.map((t) =>
                t.node_key === data.node_key && t.status === 'running'
                  ? { ...t, status: 'failed', error: data.error }
                  : t,
              ),
            );
            break;

          case 'error':
            setError(data.message);
            break;

          case 'done':
            setExecutionStatus(data.status || 'completed');
            setTotalLatency(data.latency_ms);
            setExecutionId(data.execution_id);
            break;
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'Test execution failed');
      }
    } finally {
      setRunning(false);
      onActiveNode(null);
      abortRef.current = null;
    }
  }, [message, tenderId, testFiles, running, workflowId, onActiveNode]);

  const cancelTest = () => {
    abortRef.current?.abort();
    setRunning(false);
    setExecutionStatus('cancelled');
    onActiveNode(null);
  };

  const statusIcon = (status: string) => {
    switch (status) {
      case 'completed': return <CheckCircle2 size={12} className="text-green-500" />;
      case 'failed': return <XCircle size={12} className="text-red-500" />;
      case 'running': return <Loader2 size={12} className="text-accent animate-spin" />;
      case 'skipped': return <Clock size={12} className="text-muted-foreground" />;
      case 'paused': return <Pause size={12} className="text-amber-500" />;
      default: return <Clock size={12} className="text-muted-foreground" />;
    }
  };

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: Input + Controls */}
      <div className="w-72 shrink-0 border-r border-border flex flex-col">
        {/* Scrollable form fields */}
        <div className="flex-1 overflow-auto p-3 space-y-2">
          <div>
            <label className="block text-[10px] font-semibold text-muted-foreground uppercase mb-1">Test Message</label>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="e.g., Analyze this tender for risks..."
              rows={2}
              disabled={running}
              className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-ring resize-none disabled:opacity-50"
            />
          </div>
          <div className="flex gap-2">
            <div className="flex-1">
              <label className="block text-[10px] font-semibold text-muted-foreground uppercase mb-1">Tender ID</label>
              <input
                value={tenderId}
                onChange={(e) => setTenderId(e.target.value)}
                placeholder="e.g., 5"
                disabled={running}
                className="w-full px-2.5 py-1.5 border border-border rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
              />
            </div>
          </div>
          {/* File Upload */}
          <div>
            <label className="block text-[10px] font-semibold text-muted-foreground uppercase mb-1">Document</label>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.doc,.docx,.xls,.xlsx,.txt,.csv"
              multiple
              onChange={(e) => {
                if (e.target.files) setTestFiles(Array.from(e.target.files));
              }}
              disabled={running}
              className="hidden"
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={running}
              className="w-full inline-flex items-center justify-center gap-1.5 px-2.5 py-1.5 border border-dashed border-border rounded-md text-xs text-muted-foreground hover:border-blue-400 hover:text-accent hover:bg-accent/5 transition-colors disabled:opacity-50"
            >
              <Paperclip size={10} />
              {testFiles.length > 0 ? `${testFiles.length} file(s)` : 'Attach file'}
            </button>
            {testFiles.length > 0 && (
              <div className="mt-1 space-y-0.5">
                {testFiles.map((f, i) => (
                  <div key={i} className="flex items-center gap-1 text-[10px] text-muted-foreground bg-muted/40 rounded px-1.5 py-0.5">
                    <Paperclip size={8} className="shrink-0" />
                    <span className="truncate flex-1">{f.name}</span>
                    <span className="text-muted-foreground shrink-0">{(f.size / 1024).toFixed(0)}KB</span>
                    <button
                      onClick={() => setTestFiles((prev) => prev.filter((_, j) => j !== i))}
                      className="text-muted-foreground hover:text-red-500 shrink-0"
                    >
                      <X size={8} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Status summary */}
          {executionStatus && (
            <div className={`px-2.5 py-1.5 rounded-md text-[10px] font-medium ${
              executionStatus === 'completed' ? 'bg-green-50 dark:bg-green-500/15 text-green-700 dark:text-green-400 border border-green-200 dark:border-green-500/20' :
              executionStatus === 'failed' ? 'bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-500/20' :
              executionStatus === 'paused' ? 'bg-amber-50 dark:bg-amber-500/15 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-500/20' :
              'bg-muted/40 text-muted-foreground border border-border'
            }`}>
              <div className="flex items-center gap-1.5">
                {statusIcon(executionStatus)}
                {executionStatus.charAt(0).toUpperCase() + executionStatus.slice(1)}
                {totalLatency != null && <span className="text-muted-foreground ml-auto">{(totalLatency / 1000).toFixed(1)}s</span>}
              </div>
              {executionId && <div className="text-[9px] text-muted-foreground mt-0.5">Execution #{executionId}</div>}
            </div>
          )}

          {error && (
            <div className="px-2.5 py-1.5 rounded-md bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 text-[10px] text-red-600 dark:text-red-400">
              <AlertCircle size={10} className="inline mr-1" />{error}
            </div>
          )}
        </div>

        {/* Pinned Run button at bottom */}
        <div className="shrink-0 p-3 pt-0">
          {running ? (
            <button
              onClick={cancelTest}
              className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium rounded-md bg-red-50 dark:bg-red-500/15 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-500/20 hover:bg-red-100 dark:bg-red-500/20"
            >
              <Square size={10} /> Stop
            </button>
          ) : (
            <button
              onClick={runTest}
              disabled={!message.trim()}
              className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium rounded-md bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-40 shadow-sm"
            >
              <Play size={10} /> Run Test
            </button>
          )}
        </div>
      </div>

      {/* Middle: Node Trace Timeline */}
      <div className="flex-1 overflow-auto border-r border-border">
        {nodeTraces.length === 0 && !running ? (
          <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
            Run a test to see node execution trace
          </div>
        ) : (
          <div className="p-2 space-y-0.5">
            <div className="text-[10px] font-semibold text-muted-foreground uppercase mb-1.5 px-1">Node Trace</div>
            {nodeTraces.map((trace) => {
              const isExpanded = expandedNode === trace.node_key;
              const indent = trace.subgraph_parent ? 'ml-4' : '';
              return (
                <div key={trace.node_key} className={indent}>
                  <button
                    onClick={() => setExpandedNode(isExpanded ? null : trace.node_key)}
                    className={`w-full flex items-center gap-1.5 px-2 py-1 rounded text-left text-[11px] hover:bg-muted/40 transition-colors ${
                      isExpanded ? 'bg-muted/40' : ''
                    }`}
                  >
                    {statusIcon(trace.status)}
                    <span className="font-medium text-foreground truncate flex-1">
                      {trace.display_name || trace.node_key}
                    </span>
                    <span className="text-[9px] text-muted-foreground font-mono">{trace.node_type}</span>
                    {trace.latency_ms != null && (
                      <span className="text-[9px] text-muted-foreground">{trace.latency_ms}ms</span>
                    )}
                    {isExpanded ? <ChevronDown size={10} className="text-muted-foreground" /> : <ChevronRight size={10} className="text-muted-foreground" />}
                  </button>
                  {isExpanded && trace.output_data && (
                    <pre className="ml-6 mt-0.5 mb-1 px-2 py-1.5 bg-muted/40 rounded text-[9px] text-muted-foreground font-mono overflow-auto max-h-32 border border-border">
                      {typeof trace.output_data === 'string'
                        ? trace.output_data
                        : JSON.stringify(trace.output_data, null, 2)}
                    </pre>
                  )}
                  {isExpanded && trace.error && (
                    <div className="ml-6 mt-0.5 mb-1 px-2 py-1.5 bg-red-50 dark:bg-red-500/15 rounded text-[9px] text-red-600 dark:text-red-400 border border-red-100">
                      {trace.error}
                    </div>
                  )}
                </div>
              );
            })}
            {running && (
              <div className="flex items-center gap-1.5 px-2 py-1 text-[10px] text-accent">
                <Loader2 size={10} className="animate-spin" /> Executing...
              </div>
            )}
          </div>
        )}
      </div>

      {/* Right: Streamed Output */}
      <div className="w-80 shrink-0 overflow-auto">
        <div className="p-2">
          <div className="text-[10px] font-semibold text-muted-foreground uppercase mb-1.5">Output</div>
          {streamedText ? (
            <pre className="text-[11px] text-foreground whitespace-pre-wrap break-words leading-relaxed font-sans">
              {streamedText}
            </pre>
          ) : (
            <div className="text-xs text-muted-foreground mt-4 text-center">
              {running ? (
                <span className="flex items-center justify-center gap-1.5">
                  <Loader2 size={12} className="animate-spin" /> Waiting for output...
                </span>
              ) : (
                'No output yet'
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
