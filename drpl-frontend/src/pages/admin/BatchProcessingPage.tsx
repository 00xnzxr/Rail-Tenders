import { useState, useEffect, useCallback } from 'react';
import {
  Layers, Play, RefreshCw, XCircle, CheckCircle2, Clock, AlertTriangle,
  Download, Zap, DollarSign, BarChart3, Loader2, ChevronDown, ChevronUp,
} from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  createBatchAnalysis, listBatches, getBatchDetail, getBatchItems,
  pollBatch, cancelBatch, getBatchStats,
} from '../../lib/api';

interface BatchJob {
  id: number;
  batch_id: string;
  batch_type: string;
  status: string;
  model: string;
  total_requests: number;
  succeeded_count: number;
  errored_count: number;
  expired_count: number;
  canceled_count: number;
  processing_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  estimated_cost: number;
  results_processed: boolean;
  metadata: Record<string, any>;
  error_message: string | null;
  created_at: string;
  ended_at: string | null;
  expires_at: string | null;
}

interface BatchStats {
  total_batches: number;
  active_batches: number;
  completed_batches: number;
  total_requests_submitted: number;
  total_requests_succeeded: number;
  total_cost_saved: number;
  standard_cost_equivalent: number;
}

const STATUS_CONFIG: Record<string, { color: string; bg: string; icon: any }> = {
  in_progress: { color: 'text-accent', bg: 'bg-accent/10 border-accent/20', icon: Loader2 },
  created: { color: 'text-yellow-700 dark:text-yellow-400', bg: 'bg-yellow-50 dark:bg-yellow-500/15 border-yellow-200 dark:border-yellow-500/20', icon: Clock },
  ended: { color: 'text-green-700 dark:text-green-400', bg: 'bg-green-50 dark:bg-green-500/15 border-green-200 dark:border-green-500/20', icon: CheckCircle2 },
  canceling: { color: 'text-orange-700 dark:text-orange-400', bg: 'bg-orange-50 dark:bg-orange-500/15 border-orange-200 dark:border-orange-500/20', icon: XCircle },
  canceled: { color: 'text-muted-foreground', bg: 'bg-muted/40 border-border', icon: XCircle },
  expired: { color: 'text-red-700 dark:text-red-400', bg: 'bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/20', icon: AlertTriangle },
  failed: { color: 'text-red-700 dark:text-red-400', bg: 'bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/20', icon: AlertTriangle },
};

export default function BatchProcessingPage() {
  const [batches, setBatches] = useState<BatchJob[]>([]);
  const [stats, setStats] = useState<BatchStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [polling, setPolling] = useState<string | null>(null);
  const [canceling, setCanceling] = useState<string | null>(null);
  const [expandedBatch, setExpandedBatch] = useState<string | null>(null);
  const [batchItems, setBatchItems] = useState<any[]>([]);
  const [loadingItems, setLoadingItems] = useState(false);

  // Create batch form
  const [batchSize, setBatchSize] = useState(50);

  const loadData = useCallback(async () => {
    try {
      const [batchList, batchStats] = await Promise.all([
        listBatches(undefined, 50),
        getBatchStats(),
      ]);
      setBatches(batchList);
      setStats(batchStats);
    } catch (err) {
      console.error('Failed to load batch data:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Auto-poll active batches every 30 seconds
  useEffect(() => {
    const activeBatches = batches.filter(b => b.status === 'in_progress' || b.status === 'created');
    if (activeBatches.length === 0) return;

    const interval = setInterval(async () => {
      for (const batch of activeBatches) {
        try {
          const result = await pollBatch(batch.batch_id, true);
          if (result.status !== batch.status || result.results_processed) {
            loadData(); // Refresh when status changes
          }
        } catch (err) {
          console.error(`Auto-poll failed for ${batch.batch_id}:`, err);
        }
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [batches, loadData]);

  const handleCreateBatch = async () => {
    setCreating(true);
    try {
      await createBatchAnalysis(batchSize);
      await loadData();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Failed to create batch');
    } finally {
      setCreating(false);
    }
  };

  const handlePoll = async (batchId: string) => {
    setPolling(batchId);
    try {
      await pollBatch(batchId, true);
      await loadData();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Poll failed');
    } finally {
      setPolling(null);
    }
  };

  const handleCancel = async (batchId: string) => {
    if (!confirm('Cancel this batch? Partial results may still be available.')) return;
    setCanceling(batchId);
    try {
      await cancelBatch(batchId);
      await loadData();
    } catch (err: any) {
      alert(err?.response?.data?.detail || 'Cancel failed');
    } finally {
      setCanceling(null);
    }
  };

  const handleToggleExpand = async (batchId: string) => {
    if (expandedBatch === batchId) {
      setExpandedBatch(null);
      setBatchItems([]);
      return;
    }
    setExpandedBatch(batchId);
    setLoadingItems(true);
    try {
      const items = await getBatchItems(batchId);
      setBatchItems(items);
    } catch (err) {
      console.error('Failed to load items:', err);
    } finally {
      setLoadingItems(false);
    }
  };

  const formatDate = (iso: string | null) => {
    if (!iso) return '—';
    return new Date(iso).toLocaleString();
  };

  const progressPercent = (batch: BatchJob) => {
    if (batch.total_requests === 0) return 0;
    const done = batch.succeeded_count + batch.errored_count + batch.expired_count + batch.canceled_count;
    return Math.round((done / batch.total_requests) * 100);
  };

  if (loading) return <><Header title="Batch Processing" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Batch Processing" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Stats Cards */}
        {stats && (
          <div className="grid grid-cols-4 gap-4">
            <div className="bg-card rounded-lg border border-border p-4">
              <div className="flex items-center gap-2 text-muted-foreground text-xs font-medium mb-1">
                <Layers size={14} /> Total Batches
              </div>
              <div className="text-2xl font-bold text-foreground">{stats.total_batches}</div>
              <div className="text-xs text-muted-foreground mt-1">
                {stats.active_batches} active, {stats.completed_batches} completed
              </div>
            </div>
            <div className="bg-card rounded-lg border border-border p-4">
              <div className="flex items-center gap-2 text-muted-foreground text-xs font-medium mb-1">
                <BarChart3 size={14} /> Requests Processed
              </div>
              <div className="text-2xl font-bold text-foreground">{stats.total_requests_succeeded.toLocaleString()}</div>
              <div className="text-xs text-muted-foreground mt-1">
                of {stats.total_requests_submitted.toLocaleString()} submitted
              </div>
            </div>
            <div className="bg-card rounded-lg border border-border p-4">
              <div className="flex items-center gap-2 text-green-600 dark:text-green-400 text-xs font-medium mb-1">
                <DollarSign size={14} /> Batch Cost (50% Off)
              </div>
              <div className="text-2xl font-bold text-green-700 dark:text-green-400">${stats.total_cost_saved.toFixed(4)}</div>
              <div className="text-xs text-green-500 mt-1">
                Standard: ${stats.standard_cost_equivalent.toFixed(4)}
              </div>
            </div>
            <div className="bg-card rounded-lg border border-border p-4">
              <div className="flex items-center gap-2 text-purple-600 dark:text-purple-400 text-xs font-medium mb-1">
                <Zap size={14} /> Cost Savings
              </div>
              <div className="text-2xl font-bold text-purple-700 dark:text-purple-400">
                ${(stats.standard_cost_equivalent - stats.total_cost_saved).toFixed(4)}
              </div>
              <div className="text-xs text-purple-400 mt-1">50% discount on all batch requests</div>
            </div>
          </div>
        )}

        {/* Create Batch Section */}
        <div className="bg-card rounded-lg border border-border p-5">
          <h3 className="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
            <Play size={16} className="text-purple-600 dark:text-purple-400" />
            Create New Batch Analysis
          </h3>
          <p className="text-xs text-muted-foreground mb-4">
            Submit unanalyzed tenders for batch processing via Claude's Message Batches API.
            Each tender gets 4 AI analyses (classify, relevance, risk, summary) at 50% cost.
            Batches typically complete within 1 hour.
          </p>
          <div className="flex items-end gap-4">
            <div>
              <label className="block text-xs text-muted-foreground mb-1">Tender Count</label>
              <input
                type="number"
                min={1}
                max={500}
                value={batchSize}
                onChange={(e) => setBatchSize(Number(e.target.value))}
                className="border border-border rounded-lg px-3 py-2 text-sm w-32"
              />
              <p className="text-[10px] text-muted-foreground mt-0.5">
                = {batchSize * 4} API requests
              </p>
            </div>
            <button
              onClick={handleCreateBatch}
              disabled={creating}
              className="flex items-center gap-2 bg-purple-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-purple-700 disabled:opacity-50 transition-colors"
            >
              {creating ? (
                <><Loader2 size={14} className="animate-spin" /> Creating...</>
              ) : (
                <><Layers size={14} /> Create Batch (50% Off)</>
              )}
            </button>
            <button
              onClick={loadData}
              className="flex items-center gap-1.5 border border-border text-muted-foreground px-3 py-2 rounded-lg text-sm hover:bg-muted/40 transition-colors"
            >
              <RefreshCw size={14} /> Refresh
            </button>
          </div>
        </div>

        {/* Batch Jobs List */}
        <div className="space-y-3">
          <h3 className="text-sm font-semibold text-foreground">Batch Jobs</h3>

          {batches.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm bg-card rounded-lg border border-border">
              No batch jobs yet. Create one above to get started.
            </div>
          ) : (
            batches.map((batch) => {
              const config = STATUS_CONFIG[batch.status] || STATUS_CONFIG.failed;
              const StatusIcon = config.icon;
              const progress = progressPercent(batch);
              const isExpanded = expandedBatch === batch.batch_id;
              const isActive = batch.status === 'in_progress' || batch.status === 'created';

              return (
                <div key={batch.batch_id} className="bg-card rounded-lg border border-border overflow-hidden">
                  <div className="p-4">
                    <div className="flex items-start justify-between">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-3 mb-2">
                          <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium border ${config.bg} ${config.color}`}>
                            <StatusIcon size={12} className={batch.status === 'in_progress' ? 'animate-spin' : ''} />
                            {batch.status.replace('_', ' ')}
                          </span>
                          <code className="text-xs text-muted-foreground font-mono">{batch.batch_id}</code>
                          {batch.results_processed && (
                            <span className="text-[10px] bg-green-100 dark:bg-green-500/20 text-green-600 dark:text-green-400 px-1.5 py-0.5 rounded font-medium">
                              PROCESSED
                            </span>
                          )}
                        </div>

                        <div className="flex items-center gap-6 text-xs text-muted-foreground mb-2">
                          <span>Model: <strong className="text-foreground">{batch.model}</strong></span>
                          <span>Tenders: <strong className="text-foreground">{batch.metadata?.tender_count || '?'}</strong></span>
                          <span>Requests: <strong className="text-foreground">{batch.total_requests}</strong></span>
                          <span>Created: {formatDate(batch.created_at)}</span>
                          {batch.ended_at && <span>Ended: {formatDate(batch.ended_at)}</span>}
                        </div>

                        {/* Progress Bar */}
                        {batch.total_requests > 0 && (
                          <div className="mb-2">
                            <div className="flex items-center gap-2 mb-1">
                              <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden">
                                <div
                                  className={`h-full rounded-full transition-all duration-500 ${
                                    batch.status === 'ended' ? 'bg-green-500' : 'bg-accent'
                                  }`}
                                  style={{ width: `${progress}%` }}
                                />
                              </div>
                              <span className="text-xs text-muted-foreground font-mono w-10 text-right">{progress}%</span>
                            </div>
                            <div className="flex gap-4 text-[10px] text-muted-foreground">
                              <span className="text-green-600 dark:text-green-400">{batch.succeeded_count} succeeded</span>
                              {batch.errored_count > 0 && (
                                <span className="text-red-500">{batch.errored_count} errored</span>
                              )}
                              {batch.expired_count > 0 && (
                                <span className="text-orange-500">{batch.expired_count} expired</span>
                              )}
                              {batch.processing_count > 0 && (
                                <span className="text-accent">{batch.processing_count} processing</span>
                              )}
                            </div>
                          </div>
                        )}

                        {/* Cost info for completed batches */}
                        {batch.results_processed && batch.estimated_cost > 0 && (
                          <div className="flex gap-4 text-[10px] text-muted-foreground mt-1">
                            <span>Tokens: {(batch.total_input_tokens + batch.total_output_tokens).toLocaleString()}</span>
                            <span className="text-green-600 dark:text-green-400 font-medium">
                              Batch cost: ${batch.estimated_cost.toFixed(4)}
                              <span className="text-muted-foreground ml-1">(saved ${batch.estimated_cost.toFixed(4)})</span>
                            </span>
                          </div>
                        )}
                      </div>

                      {/* Actions */}
                      <div className="flex gap-1.5 shrink-0 ml-4">
                        {isActive && (
                          <>
                            <button
                              onClick={() => handlePoll(batch.batch_id)}
                              disabled={polling === batch.batch_id}
                              className="flex items-center gap-1 bg-accent/10 text-accent px-2.5 py-1.5 rounded-lg text-xs font-medium hover:bg-accent/15 disabled:opacity-50 transition-colors"
                            >
                              {polling === batch.batch_id ? (
                                <Loader2 size={12} className="animate-spin" />
                              ) : (
                                <RefreshCw size={12} />
                              )}
                              Poll
                            </button>
                            <button
                              onClick={() => handleCancel(batch.batch_id)}
                              disabled={canceling === batch.batch_id}
                              className="flex items-center gap-1 bg-red-50 dark:bg-red-500/15 text-red-600 dark:text-red-400 px-2.5 py-1.5 rounded-lg text-xs font-medium hover:bg-red-100 dark:bg-red-500/20 disabled:opacity-50 transition-colors"
                            >
                              <XCircle size={12} /> Cancel
                            </button>
                          </>
                        )}
                        {batch.status === 'ended' && !batch.results_processed && (
                          <button
                            onClick={() => handlePoll(batch.batch_id)}
                            disabled={polling === batch.batch_id}
                            className="flex items-center gap-1 bg-green-50 dark:bg-green-500/15 text-green-600 dark:text-green-400 px-2.5 py-1.5 rounded-lg text-xs font-medium hover:bg-green-100 dark:bg-green-500/20 disabled:opacity-50 transition-colors"
                          >
                            <Download size={12} /> Process Results
                          </button>
                        )}
                        <button
                          onClick={() => handleToggleExpand(batch.batch_id)}
                          className="flex items-center gap-1 border border-border text-muted-foreground px-2 py-1.5 rounded-lg text-xs hover:bg-muted/40 transition-colors"
                        >
                          {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                          Items
                        </button>
                      </div>
                    </div>
                  </div>

                  {/* Expanded Items View */}
                  {isExpanded && (
                    <div className="border-t border-border bg-muted/40 p-4">
                      {loadingItems ? (
                        <div className="text-center py-4 text-muted-foreground text-xs">Loading items...</div>
                      ) : batchItems.length === 0 ? (
                        <div className="text-center py-4 text-muted-foreground text-xs">No items found</div>
                      ) : (
                        <div className="max-h-64 overflow-y-auto">
                          <table className="w-full text-xs">
                            <thead>
                              <tr className="text-left text-muted-foreground border-b border-border">
                                <th className="pb-2 font-medium">Tender ID</th>
                                <th className="pb-2 font-medium">Agent</th>
                                <th className="pb-2 font-medium">Status</th>
                                <th className="pb-2 font-medium">Result Preview</th>
                                <th className="pb-2 font-medium text-right">Tokens</th>
                              </tr>
                            </thead>
                            <tbody>
                              {batchItems.map((item: any) => (
                                <tr key={item.id} className="border-b border-border">
                                  <td className="py-1.5 font-mono text-muted-foreground">#{item.tender_id}</td>
                                  <td className="py-1.5">
                                    <span className="px-1.5 py-0.5 bg-purple-50 dark:bg-purple-500/15 text-purple-600 dark:text-purple-400 rounded text-[10px] font-medium">
                                      {item.item_type}
                                    </span>
                                  </td>
                                  <td className="py-1.5">
                                    <span className={`${
                                      item.result_status === 'succeeded' ? 'text-green-600 dark:text-green-400' :
                                      item.result_status === 'errored' ? 'text-red-500' :
                                      item.result_status === 'pending' ? 'text-yellow-600 dark:text-yellow-400' :
                                      'text-muted-foreground'
                                    }`}>
                                      {item.result_status}
                                    </span>
                                  </td>
                                  <td className="py-1.5 text-muted-foreground truncate max-w-xs">
                                    {item.result_text || item.error_message || '—'}
                                  </td>
                                  <td className="py-1.5 text-right text-muted-foreground font-mono">
                                    {item.input_tokens + item.output_tokens > 0
                                      ? (item.input_tokens + item.output_tokens).toLocaleString()
                                      : '—'}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Info Banner */}
        <div className="flex items-start gap-3 bg-accent/10 border border-accent/20 rounded-lg p-4">
          <Zap size={18} className="text-accent shrink-0 mt-0.5" />
          <div className="text-sm text-accent">
            <strong>About Batch Processing:</strong> Claude's Message Batches API processes requests asynchronously
            at <strong>50% of standard API costs</strong>. Batches typically complete within 1 hour.
            Each tender analysis creates 4 requests (classify, relevance, risk, summary).
            Results are automatically applied to tenders when processing completes.
            Active batches are auto-polled every 30 seconds.
          </div>
        </div>
      </div>
    </>
  );
}
