import { useState, useEffect } from 'react';
import { Save, RotateCcw, Eye, EyeOff, Key, AlertTriangle, CheckCircle2, Search } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import { getAdminSettings, updateAdminSetting, resetAdminSetting, getModelCatalog } from '../../lib/api';
import NotificationPreferencesMatrix from '../../components/notifications/NotificationPreferencesMatrix';

interface PlatformSetting {
  id: number;
  key: string;
  value: string;
  value_type: string;
  category: string;
  description: string | null;
  is_secret: boolean;
  /** Whether a value is stored. For a secret, `value` is the mask either way. */
  is_set?: boolean;
}

const CATEGORIES = ['ai', 'costing', 'general', 'security', 'notifications'];
const CATEGORY_LABELS: Record<string, string> = {
  ai: 'AI & API Keys',
  costing: 'Costing',
  general: 'General',
  security: 'Security',
  notifications: 'Notifications',
};

const CATEGORY_DESCRIPTIONS: Record<string, string> = {
  ai: 'Configure AI models, API keys, and agent parameters. API keys set here override .env values.',
  costing: 'Org-wide default percentages applied by the costing agent and cost-sheet renderer (overhead, profit margin, GST). The agent reads these on every run.',
  general: 'Platform name, authentication, upload limits, and scraping configuration.',
  security: 'Control what data is sent to AI services for processing.',
  notifications: 'Notification and alerting preferences.',
};

// Anthropic Claude models
/**
 * Model lists come from the backend catalog (`GET /api/agent-builder/models`),
 * not from arrays in this file. Three hardcoded copies lived here and drifted
 * out of sync with the runtime, which is how an agent could be saved on one
 * model and displayed as another.
 *
 * Voyage embedding models stay local: they are not in the chat-model catalog.
 */

// Voyage AI embedding models
const VOYAGE_MODEL_OPTIONS = [
  'voyage-3.5',
  'voyage-3-large',
  'voyage-3.5-lite',
  'voyage-code-3',
  'voyage-finance-2',
  'voyage-law-2',
];

export default function PlatformSettingsPage() {
  const [settings, setSettings] = useState<PlatformSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('ai');
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState('');
  const [catalogModels, setCatalogModels] = useState<any[]>([]);

  // Model options come from the backend so this page cannot drift from what
  // the runtime actually supports.
  useEffect(() => {
    getModelCatalog()
      .then((c) => setCatalogModels(c.models || []))
      .catch(() => setCatalogModels([]));
  }, []);

  useEffect(() => {
    getAdminSettings()
      .then((data) => {
        setSettings(data);
        const vals: Record<string, string> = {};
        data.forEach((s: PlatformSetting) => { vals[s.key] = s.value; });
        setEditValues(vals);
      })
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async (key: string) => {
    setSaving(key);
    try {
      await updateAdminSetting(key, editValues[key]);
      setSettings((prev) => prev.map((s) => s.key === key ? { ...s, value: editValues[key] } : s));
      setSaved(key);
      setTimeout(() => setSaved(null), 2000);
    } finally {
      setSaving(null);
    }
  };

  const handleReset = async (key: string) => {
    try {
      const result = await resetAdminSetting(key);
      setEditValues((prev) => ({ ...prev, [key]: result.value }));
      setSettings((prev) => prev.map((s) => s.key === key ? { ...s, value: result.value } : s));
    } catch {}
  };

  const toggleRevealSecret = (key: string) => {
    setRevealedSecrets((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const isChanged = (key: string) => {
    const setting = settings.find((s) => s.key === key);
    return setting && editValues[key] !== setting.value;
  };

  // 55 settings live on the AI tab alone, and the only way to reach one was
  // to scroll for it. Matching the key and the description both, because
  // "runpod" finds the key and "proxy" finds the one whose name does not say so.
  const query = search.trim().toLowerCase();
  const filtered = settings
    .filter((s) => s.category === activeTab)
    .filter((s) => !query
      || s.key.toLowerCase().includes(query)
      || (s.description || '').toLowerCase().includes(query));

  if (loading) return <><Header title="Platform Settings" /><LoadingSpinner /></>;

  const renderSettingInput = (setting: PlatformSetting) => {
    // Special handling for model selection — show provider-appropriate models
    if (setting.key === 'ai_model') {
      const provider = editValues['ai_provider'] || 'anthropic';
      const models = catalogModels
        .filter((m: any) => m.provider === (provider || 'anthropic'))
        .map((m: any) => m.id);
      return (
        <select
          value={editValues[setting.key] || ''}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      );
    }

    // Special handling for Gemini search model
    if (setting.key === 'gemini_search_model') {
      return (
        <select
          value={editValues[setting.key] || 'gemini-2.5-flash'}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          {catalogModels.filter((m: any) => m.provider === 'google').map((m: any) => m.id).map((m: string) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      );
    }

    // Special handling for cache TTL
    if (setting.key === 'cache_ttl') {
      return (
        <select
          value={editValues[setting.key] || '5m'}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          <option value="5m">5 minutes (1.25x write cost — default)</option>
          <option value="1h">1 hour (2x write cost — longer persistence)</option>
        </select>
      );
    }

    // Special handling for Voyage AI embedding model
    if (setting.key === 'voyage_embedding_model') {
      return (
        <select
          value={editValues[setting.key] || 'voyage-3.5'}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          {VOYAGE_MODEL_OPTIONS.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      );
    }

    // Special handling for thinking mode
    if (setting.key === 'thinking_mode') {
      return (
        <select
          value={editValues[setting.key] || 'auto'}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          <option value="auto">Auto (adaptive for 4.6, disabled for older models)</option>
          <option value="adaptive">Adaptive (Claude determines thinking depth)</option>
          <option value="enabled">Enabled (manual budget_tokens control)</option>
          <option value="disabled">Disabled (no extended thinking)</option>
        </select>
      );
    }

    // Special handling for effort level
    if (setting.key === 'effort_level') {
      return (
        <select
          value={editValues[setting.key] || ''}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md bg-card"
        >
          <option value="">Default (high)</option>
          <option value="low">Low (fastest, cheapest, minimal thinking)</option>
          <option value="medium">Medium (balanced speed/quality)</option>
          <option value="high">High (deep reasoning, default)</option>
          <option value="max">Max (deepest analysis, Opus 4.6 only)</option>
        </select>
      );
    }

    // Special handling for provider
    if (setting.key === 'ai_provider') {
      return (
        <select
          value={editValues[setting.key] || ''}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-48 bg-card"
        >
          <option value="anthropic">Anthropic (Claude)</option>
          <option value="openai">OpenAI (GPT)</option>
          <option value="google">Google (Gemini)</option>
        </select>
      );
    }

    // Force-provider kill-switch — overrides every per-agent provider when set
    if (setting.key === 'force_provider_override') {
      return (
        <select
          value={editValues[setting.key] || 'auto'}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-64 bg-card"
        >
          <option value="auto">Auto (use each agent's own provider)</option>
          <option value="anthropic">Force Anthropic (Claude) for ALL agents</option>
          <option value="openai">Force OpenAI (GPT) for ALL agents</option>
          <option value="google">Force Google (Gemini) for ALL agents</option>
        </select>
      );
    }

    // Boolean toggle
    if (setting.value_type === 'bool') {
      const isOn = (editValues[setting.key] || '').toLowerCase() === 'true';
      return (
        <button
          onClick={() => setEditValues((p) => ({ ...p, [setting.key]: isOn ? 'false' : 'true' }))}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${isOn ? 'bg-purple-600' : 'bg-muted-foreground/40'}`}
        >
          <span className={`inline-block h-4 w-4 transform rounded-full bg-card transition-transform ${isOn ? 'translate-x-6' : 'translate-x-1'}`} />
        </button>
      );
    }

    // Secret field (API keys)
    if (setting.is_secret) {
      const revealed = revealedSecrets[setting.key];
      // The stored value never reaches the browser, so the box is always
      // empty and cannot show what is saved. What it CAN say is whether
      // anything is saved -- which is the question, and the one this page
      // used to answer identically for a configured key and a blank one.
      return (
        <div className="flex flex-col gap-1.5 w-full max-w-md">
          <div className="relative flex-1">
            <Key size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type={revealed ? 'text' : 'password'}
              value={editValues[setting.key] || ''}
              onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
              placeholder={setting.is_set ? 'Saved — type a new value to replace it' : 'Not set — paste the key here'}
              className="border border-border rounded-lg pl-9 pr-10 py-2 text-sm w-full font-mono"
            />
            <button
              onClick={() => toggleRevealSecret(setting.key)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-muted-foreground"
            >
              {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          {setting.is_set ? (
            <span className="inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 size={12} /> A value is saved. The key itself is never sent to the browser.
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
              <AlertTriangle size={12} /> Not set — whatever uses this falls back to the .env value, or fails.
            </span>
          )}
        </div>
      );
    }

    // Number input
    if (setting.value_type === 'int' || setting.value_type === 'float') {
      return (
        <input
          type="number"
          step={setting.value_type === 'float' ? '0.1' : '1'}
          value={editValues[setting.key] || ''}
          onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
          className="border border-border rounded-lg px-3 py-2 text-sm w-40"
        />
      );
    }

    // Default text input
    return (
      <input
        type="text"
        value={editValues[setting.key] || ''}
        onChange={(e) => setEditValues((p) => ({ ...p, [setting.key]: e.target.value }))}
        className="border border-border rounded-lg px-3 py-2 text-sm w-full max-w-md"
      />
    );
  };

  return (
    <>
      <Header title="Platform Settings" />
      <div className="mx-auto w-full max-w-[1600px] space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Tabs */}
        <div className="flex gap-1 bg-muted rounded-lg p-1 w-fit">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => setActiveTab(cat)}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                activeTab === cat ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {CATEGORY_LABELS[cat]}
            </button>
          ))}
        </div>

        {/* Category description */}
        <div className="text-sm text-muted-foreground">
          {CATEGORY_DESCRIPTIONS[activeTab]}
        </div>

        {/* API key warning */}
        {activeTab === 'ai' && (
          <div className="flex items-start gap-3 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-lg p-4">
            <AlertTriangle size={18} className="text-amber-500 shrink-0 mt-0.5" />
            <div className="text-sm text-amber-700 dark:text-amber-400">
              <strong>API Key Security:</strong> API keys stored here are encrypted in the database and only accessible to the master admin.
              Keys set here will override any keys in the .env file. If left empty, the system falls back to .env configuration.
            </div>
          </div>
        )}

        {/* Notification preferences matrix (only on the notifications tab) */}
        {activeTab === 'notifications' && (
          <NotificationPreferencesMatrix />
        )}

        {/* Find a setting. The AI tab alone carries 55 of them. */}
        <div className="relative max-w-md">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={`Search ${CATEGORY_LABELS[activeTab] ?? activeTab} settings by name or description…`}
            className="border border-border rounded-lg pl-9 pr-3 py-2 text-sm w-full bg-card"
          />
        </div>

        {/* Settings list */}
        <div className="space-y-3">
          {filtered.length === 0 && (
            <p className="text-sm text-muted-foreground px-1">
              {search
                ? `No setting in ${CATEGORY_LABELS[activeTab] ?? activeTab} matches “${search}”.`
                : 'No settings in this category.'}
            </p>
          )}
          {filtered.map((setting) => (
            <div key={setting.key} className="bg-card rounded-lg border border-border p-4">
              {setting.key === 'database_url' && (
                <div className="flex items-start gap-2 mb-3 bg-amber-50 dark:bg-amber-500/15 border border-amber-200 dark:border-amber-500/20 rounded-lg px-3 py-2">
                  <AlertTriangle size={14} className="text-amber-500 shrink-0 mt-0.5" />
                  <span className="text-xs text-amber-700 dark:text-amber-400">Changing the database URL requires an application restart to take effect. Set this in your .env file.</span>
                </div>
              )}
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <label className="text-sm font-medium text-foreground">{setting.key}</label>
                    {setting.is_secret && (
                      <span className="text-[10px] bg-orange-100 dark:bg-orange-500/20 text-orange-600 dark:text-orange-400 px-1.5 py-0.5 rounded font-medium">SECRET</span>
                    )}
                  </div>
                  {setting.description && (
                    <p className="text-xs text-muted-foreground mt-0.5 mb-2">{setting.description}</p>
                  )}
                  {renderSettingInput(setting)}
                </div>
                <div className="flex gap-1.5 pt-5 shrink-0">
                  <button
                    onClick={() => handleSave(setting.key)}
                    disabled={saving === setting.key || !isChanged(setting.key)}
                    className="flex items-center gap-1 bg-purple-600 text-white px-3 py-1.5 rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-40 transition-colors"
                  >
                    {saved === setting.key ? <><CheckCircle2 size={12} /> Saved</> : saving === setting.key ? 'Saving...' : <><Save size={12} /> Save</>}
                  </button>
                  <button
                    onClick={() => handleReset(setting.key)}
                    className="flex items-center gap-1 border border-border text-muted-foreground px-2 py-1.5 rounded-lg text-xs hover:bg-muted/40 transition-colors"
                    title="Reset to default"
                  >
                    <RotateCcw size={12} />
                  </button>
                </div>
              </div>
            </div>
          ))}
          {filtered.length === 0 && activeTab !== 'notifications' && (
            <div className="text-center py-12 text-muted-foreground text-sm">No settings in this category</div>
          )}
        </div>
      </div>
    </>
  );
}
