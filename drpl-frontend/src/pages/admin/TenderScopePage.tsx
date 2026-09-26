import { useEffect, useState } from 'react';
import { Save, RotateCcw, Plus, X, AlertCircle, CheckCircle2, Target } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getScopeProfile,
  updateScopeProfile,
  TenderScopeProfile,
  ScopeKeywordGroup,
} from '../../lib/api';

/**
 * Phase 7 — Tender Scope Profile admin page.
 *
 * Drives the Chrome extension's GeM auto-search (each keyword runs as its own
 * BOQ Title query) and seeds the scope-aware relevance agent prompt at ingest.
 */
export default function TenderScopePage() {
  const [profile, setProfile] = useState<TenderScopeProfile | null>(null);
  const [original, setOriginal] = useState<TenderScopeProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getScopeProfile();
      setProfile(data);
      setOriginal(JSON.parse(JSON.stringify(data)));
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to load scope profile.');
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    if (!profile) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateScopeProfile({
        keyword_groups: profile.keyword_groups,
        exclusion_terms: profile.exclusion_terms,
        target_ministries: profile.target_ministries,
        value_min: profile.value_min,
        value_max: profile.value_max,
        relevance_threshold: profile.relevance_threshold,
        is_active: profile.is_active,
      });
      setProfile(updated);
      setOriginal(JSON.parse(JSON.stringify(updated)));
      setSavedAt(new Date().toLocaleTimeString());
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || 'Failed to save scope profile.');
    } finally {
      setSaving(false);
    }
  };

  const revert = () => {
    if (original) setProfile(JSON.parse(JSON.stringify(original)));
  };

  if (loading) {
    return (
      <div className="flex flex-col h-full">
        <Header title="Tender Scope Profile" subtitle="Define what tenders count as a fit" />
        <div className="flex-1 flex items-center justify-center">
          <LoadingSpinner />
        </div>
      </div>
    );
  }

  if (!profile) {
    return (
      <div className="flex flex-col h-full">
        <Header title="Tender Scope Profile" subtitle="Define what tenders count as a fit" />
        <div className="flex-1 p-6">
          <div className="bg-red-50 dark:bg-red-500/15 border border-red-200 dark:border-red-500/20 rounded p-4 text-red-700 dark:text-red-400 text-sm">
            {error || 'Profile unavailable.'}
          </div>
        </div>
      </div>
    );
  }

  const dirty = JSON.stringify(profile) !== JSON.stringify(original);

  return (
    <div className="flex flex-col h-full">
      <Header
        title="Tender Scope Profile"
        subtitle="Drives the Chrome extension's GeM auto-search and the relevance agent's fit reasoning"
      />

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Status banner */}
          <div className="flex items-center justify-between bg-card border border-border rounded-lg p-3">
            <div className="flex items-center gap-2 text-sm">
              <Target size={16} className="text-accent" />
              <span className="font-medium text-foreground">Active profile:</span>
              <span className="text-muted-foreground">{profile.name}</span>
              <label className="flex items-center gap-2 ml-4 cursor-pointer">
                <input
                  type="checkbox"
                  checked={profile.is_active}
                  onChange={(e) => setProfile({ ...profile, is_active: e.target.checked })}
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
                onClick={save}
                disabled={saving || !dirty}
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

          {/* Keyword groups */}
          <Section
            title="Keyword Groups"
            description="Each keyword is fed into the GeM advance-search BOQ Title input as its own query. Group them by capability so the popup keyword panel stays readable."
          >
            <KeywordGroupEditor
              groups={profile.keyword_groups}
              onChange={(groups) => setProfile({ ...profile, keyword_groups: groups })}
            />
          </Section>

          {/* Exclusion terms */}
          <Section
            title="Exclusion Terms"
            description="If a tender's title or description prominently matches one of these, the relevance agent will score it ≤0.3."
          >
            <TagInput
              tags={profile.exclusion_terms}
              onChange={(t) => setProfile({ ...profile, exclusion_terms: t })}
              placeholder="Add exclusion term and press Enter"
            />
          </Section>

          {/* Target ministries */}
          <Section
            title="Target Ministries / Buyers"
            description="Applied as an additional filter alongside each GeM keyword search."
          >
            <TagInput
              tags={profile.target_ministries}
              onChange={(t) => setProfile({ ...profile, target_ministries: t })}
              placeholder="e.g. Ministry of Railways"
            />
          </Section>

          {/* Value range + threshold */}
          <Section
            title="Value Range &amp; Relevance Threshold"
            description="Constrain estimated value (INR) and the minimum AI relevance score for a tender to be surfaced in the dashboard."
          >
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <NumericField
                label="Min value (INR)"
                value={profile.value_min}
                onChange={(v) => setProfile({ ...profile, value_min: v })}
              />
              <NumericField
                label="Max value (INR)"
                value={profile.value_max}
                onChange={(v) => setProfile({ ...profile, value_max: v })}
              />
              <div>
                <label className="block text-xs font-medium text-muted-foreground mb-1">
                  Relevance threshold ({Math.round((profile.relevance_threshold || 0) * 100)}%)
                </label>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={profile.relevance_threshold}
                  onChange={(e) =>
                    setProfile({ ...profile, relevance_threshold: Number(e.target.value) })
                  }
                  className="w-full"
                />
              </div>
            </div>
          </Section>
        </div>
      </div>
    </div>
  );
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

function KeywordGroupEditor({
  groups,
  onChange,
}: {
  groups: ScopeKeywordGroup[];
  onChange: (g: ScopeKeywordGroup[]) => void;
}) {
  const updateAt = (idx: number, patch: Partial<ScopeKeywordGroup>) => {
    const next = groups.slice();
    next[idx] = { ...next[idx], ...patch };
    onChange(next);
  };

  const removeAt = (idx: number) => {
    onChange(groups.filter((_, i) => i !== idx));
  };

  const addGroup = () => {
    onChange([...groups, { label: 'New Group', keywords: [] }]);
  };

  return (
    <div className="space-y-4">
      {groups.map((g, idx) => (
        <div key={idx} className="border border-border rounded-md p-3">
          <div className="flex items-center gap-2 mb-2">
            <input
              type="text"
              value={g.label}
              onChange={(e) => updateAt(idx, { label: e.target.value })}
              className="flex-1 text-sm font-semibold text-foreground px-2 py-1 border border-transparent hover:border-border focus:border-accent/60 rounded outline-none"
              placeholder="Group name"
            />
            <button
              onClick={() => removeAt(idx)}
              className="text-muted-foreground hover:text-red-500 p-1"
              title="Remove group"
            >
              <X size={14} />
            </button>
          </div>
          <TagInput
            tags={g.keywords}
            onChange={(kws) => updateAt(idx, { keywords: kws })}
            placeholder="Add keyword and press Enter"
          />
        </div>
      ))}
      <button
        onClick={addGroup}
        className="inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:text-accent/80"
      >
        <Plus size={14} /> Add group
      </button>
    </div>
  );
}

function TagInput({
  tags,
  onChange,
  placeholder,
}: {
  tags: string[];
  onChange: (t: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState('');

  const commit = () => {
    const v = draft.trim();
    if (!v) return;
    if (tags.includes(v)) {
      setDraft('');
      return;
    }
    onChange([...tags, v]);
    setDraft('');
  };

  const remove = (t: string) => {
    onChange(tags.filter((x) => x !== t));
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5 border border-border rounded-md bg-muted/40">
      {tags.map((t) => (
        <span
          key={t}
          className="inline-flex items-center gap-1 bg-accent/15 text-accent text-xs font-medium px-2 py-0.5 rounded"
        >
          {t}
          <button
            onClick={() => remove(t)}
            className="text-accent hover:text-accent/80"
            title="Remove"
          >
            <X size={11} />
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            commit();
          } else if (e.key === 'Backspace' && draft === '' && tags.length > 0) {
            onChange(tags.slice(0, -1));
          }
        }}
        onBlur={commit}
        placeholder={placeholder}
        className="flex-1 min-w-[120px] text-xs bg-transparent outline-none placeholder:text-muted-foreground"
      />
    </div>
  );
}

function NumericField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number | null;
  onChange: (v: number | null) => void;
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
      />
    </div>
  );
}
