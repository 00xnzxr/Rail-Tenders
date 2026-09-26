import { useState, useEffect, useCallback } from 'react';
import {
  Brain, Zap, Database, RefreshCw, Activity, DollarSign,
  Loader2, Shield, Settings2, Clock, Hash,
} from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getContextModels, getContextConfig, getCacheStats } from '../../lib/api';

interface ModelInfo {
  model: string;
  provider: string;
  context_window: number;
  supports_compaction: boolean;
  supports_context_editing: boolean;
  context_aware: boolean;
  min_cache_tokens: number;
  supports_caching: boolean;
}

interface CacheStats {
  period_days: number;
  total_api_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  estimated_cache_savings: number;
  cache_hit_rate: number;
}

export default function ContextManagementPage() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [config, setConfig] = useState<any>(null);
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);
  const [loading, setLoading] = useState(true);

  const loadData = useCallback(async () => {
    try {
      const [modelsData, configData, statsData] = await Promise.all([
        getContextModels(),
        getContextConfig(),
        getCacheStats(30),
      ]);
      setModels(modelsData);
      setConfig(configData);
      setCacheStats(statsData);
    } catch (err) {
      console.error('Failed to load context data:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const formatTokens = (n: number) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
    return n.toString();
  };

  if (loading) return <><Header title="Context Management" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Context Management" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Active Config Summary */}
        {config && (
          <div className="bg-gradient-to-r from-indigo-50 to-purple-50 border border-indigo-200 dark:border-indigo-500/20 rounded-lg p-5">
            <h3 className="text-sm font-semibold text-indigo-800 dark:text-indigo-400 mb-3 flex items-center gap-2">
              <Settings2 size={16} /> Active Context Configuration
            </h3>
            <div className="grid grid-cols-4 gap-4">
              <div>
                <div className="text-[10px] uppercase text-indigo-500 font-medium">Model</div>
                <div className="text-sm font-mono text-indigo-900">{config.model}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-indigo-500 font-medium">Context Window</div>
                <div className="text-sm font-bold text-indigo-900">
                  {formatTokens(config.model_info?.context_window || 200000)} tokens
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-indigo-500 font-medium">Prompt Caching</div>
                <div className={`text-sm font-medium ${config.prompt_caching_enabled ? 'text-green-600 dark:text-green-400' : 'text-muted-foreground'}`}>
                  {config.prompt_caching_enabled ? 'Enabled' : 'Disabled'}
                  {config.cache_control?.ttl === '1h' ? ' (1h TTL)' : ' (5m TTL)'}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase text-indigo-500 font-medium">Beta Features</div>
                <div className="text-sm text-indigo-700 dark:text-indigo-400">
                  {config.beta_headers?.length > 0
                    ? config.beta_headers.join(', ')
                    : 'None active'}
                </div>
              </div>
            </div>
            {config.context_management && (
              <div className="mt-3 pt-3 border-t border-indigo-200 dark:border-indigo-500/20">
                <div className="text-[10px] uppercase text-indigo-500 font-medium mb-1">Active Edits</div>
                <div className="flex gap-2">
                  {config.context_management.edits?.map((edit: any, i: number) => (
                    <span key={i} className="text-xs bg-indigo-100 dark:bg-indigo-500/20 text-indigo-700 dark:text-indigo-400 px-2 py-0.5 rounded-full font-medium">
                      {edit.type.replace(/_\d+$/, '').replace(/_/g, ' ')}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Cache Performance Stats */}
        {cacheStats && (
          <div>
            <h3 className="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
              <Zap size={16} className="text-amber-500" /> Prompt Caching Performance (Last {cacheStats.period_days} Days)
            </h3>
            <div className="grid grid-cols-5 gap-3">
              <div className="bg-card rounded-lg border border-border p-4">
                <div className="flex items-center gap-1.5 text-muted-foreground text-[10px] font-medium mb-1">
                  <Activity size={12} /> API Calls
                </div>
                <div className="text-xl font-bold text-foreground">{cacheStats.total_api_calls.toLocaleString()}</div>
              </div>
              <div className="bg-card rounded-lg border border-border p-4">
                <div className="flex items-center gap-1.5 text-muted-foreground text-[10px] font-medium mb-1">
                  <Hash size={12} /> Input Tokens
                </div>
                <div className="text-xl font-bold text-foreground">{formatTokens(cacheStats.total_input_tokens)}</div>
              </div>
              <div className="bg-card rounded-lg border border-border p-4">
                <div className="flex items-center gap-1.5 text-green-600 dark:text-green-400 text-[10px] font-medium mb-1">
                  <Database size={12} /> Cache Reads
                </div>
                <div className="text-xl font-bold text-green-700 dark:text-green-400">{formatTokens(cacheStats.cache_read_tokens)}</div>
                <div className="text-[10px] text-green-500">90% cost reduction</div>
              </div>
              <div className="bg-card rounded-lg border border-border p-4">
                <div className="flex items-center gap-1.5 text-amber-600 dark:text-amber-400 text-[10px] font-medium mb-1">
                  <Clock size={12} /> Cache Writes
                </div>
                <div className="text-xl font-bold text-amber-700 dark:text-amber-400">{formatTokens(cacheStats.cache_creation_tokens)}</div>
                <div className="text-[10px] text-amber-500">1.25x write cost</div>
              </div>
              <div className="bg-card rounded-lg border border-border p-4">
                <div className="flex items-center gap-1.5 text-purple-600 dark:text-purple-400 text-[10px] font-medium mb-1">
                  <DollarSign size={12} /> Total Cost
                </div>
                <div className="text-xl font-bold text-purple-700 dark:text-purple-400">${cacheStats.total_cost.toFixed(4)}</div>
                {cacheStats.estimated_cache_savings > 0 && (
                  <div className="text-[10px] text-green-500">~${cacheStats.estimated_cache_savings.toFixed(4)} saved</div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Model Context Windows */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
              <Brain size={16} className="text-indigo-500" /> Model Context Windows
            </h3>
            <button
              onClick={loadData}
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-muted-foreground transition-colors"
            >
              <RefreshCw size={12} /> Refresh
            </button>
          </div>

          <div className="bg-card rounded-lg border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/40 border-b border-border text-left text-xs text-muted-foreground font-medium">
                  <th className="px-4 py-2.5">Provider</th>
                  <th className="px-4 py-2.5">Model</th>
                  <th className="px-4 py-2.5 text-right">Context Window</th>
                  <th className="px-4 py-2.5 text-center">Caching</th>
                  <th className="px-4 py-2.5 text-center">Compaction</th>
                  <th className="px-4 py-2.5 text-center">Context Editing</th>
                  <th className="px-4 py-2.5 text-center">Context Aware</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.model} className="border-b border-border hover:bg-muted/40">
                    <td className="px-4 py-2">
                      <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase tracking-wide ${
                        m.provider === 'anthropic' ? 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400' :
                        m.provider === 'openai' ? 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400' :
                        'bg-accent/15 text-accent'
                      }`}>
                        {m.provider === 'anthropic' ? 'Claude' : m.provider === 'openai' ? 'OpenAI' : 'Gemini'}
                      </span>
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-foreground">{m.model}</td>
                    <td className="px-4 py-2 text-right">
                      <span className={`font-bold ${m.context_window >= 1_000_000 ? 'text-purple-600 dark:text-purple-400' : 'text-foreground'}`}>
                        {formatTokens(m.context_window)}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-center">
                      {m.supports_caching ? (
                        <span className="text-green-500 text-xs font-medium">
                          {m.min_cache_tokens.toLocaleString()}
                        </span>
                      ) : (
                        <span className="text-muted-foreground/50 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-center">
                      {m.supports_compaction ? (
                        <span className="text-green-500 text-xs font-medium">Yes</span>
                      ) : (
                        <span className="text-muted-foreground/50 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-center">
                      {m.supports_context_editing ? (
                        <span className="text-green-500 text-xs font-medium">Yes</span>
                      ) : (
                        <span className="text-muted-foreground/50 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-center">
                      {m.context_aware ? (
                        <span className="text-accent text-xs font-medium">Yes</span>
                      ) : (
                        <span className="text-muted-foreground/50 text-xs">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Feature Info Cards */}
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-card rounded-lg border border-border p-4">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-8 h-8 bg-green-50 dark:bg-green-500/15 rounded-lg flex items-center justify-center">
                <Zap size={16} className="text-green-600 dark:text-green-400" />
              </div>
              <h4 className="text-sm font-semibold text-foreground">Prompt Caching</h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Caches prompt prefixes and reuses them across requests. <strong>90% cost reduction</strong> on cache hits.
              System prompts, tools, and conversation history are cached automatically.
              Configure in Platform Settings under AI category.
            </p>
          </div>

          <div className="bg-card rounded-lg border border-border p-4">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-8 h-8 bg-accent/10 rounded-lg flex items-center justify-center">
                <Shield size={16} className="text-accent" />
              </div>
              <h4 className="text-sm font-semibold text-foreground">Compaction</h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Server-side summarization for long conversations (beta, Claude 4.6). Automatically condenses older context
              when approaching window limits. Enable in Platform Settings and set the trigger threshold.
            </p>
          </div>

          <div className="bg-card rounded-lg border border-border p-4">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-8 h-8 bg-purple-50 dark:bg-purple-500/15 rounded-lg flex items-center justify-center">
                <Brain size={16} className="text-purple-600 dark:text-purple-400" />
              </div>
              <h4 className="text-sm font-semibold text-foreground">Context Editing</h4>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Fine-grained control: clear old tool results and manage thinking blocks (beta).
              Reduces context rot in agentic workflows. Configure tool clearing trigger and keep count in settings.
            </p>
          </div>
        </div>

        {/* Token Counting Info */}
        <div className="flex items-start gap-3 bg-accent/10 border border-accent/20 rounded-lg p-4">
          <Hash size={18} className="text-accent shrink-0 mt-0.5" />
          <div className="text-sm text-accent">
            <strong>Token Counting API:</strong> Use <code className="bg-accent/15 px-1 rounded text-xs">/api/context/count-tokens</code> to
            estimate token usage before sending requests. Supports all providers: <strong>Claude</strong> (free Anthropic API),
            <strong>OpenAI</strong> (tiktoken), and <strong>Gemini</strong> (estimation). Useful for context window management,
            cost planning, and smart model routing.
          </div>
        </div>
      </div>
    </>
  );
}
