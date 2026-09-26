import { useState, useEffect } from 'react';
import { Plus, Trash2, Lock, FlaskConical, Shield, Eye } from 'lucide-react';
import Header from '../../components/layout/Header';
import LoadingSpinner from '../../components/ui/LoadingSpinner';
import {
  getRedactionRules, createRedactionRule, updateRedactionRule,
  deleteRedactionRule, testRedactionPattern, getAdminSettings, updateAdminSetting,
  getRetentionPolicies, updateRetentionPolicy,
} from '../../lib/api';

interface RedactionRule {
  id: number;
  name: string;
  pattern: string;
  replacement: string;
  category: string;
  is_enabled: boolean;
  is_system: boolean;
  description: string | null;
}

const CATEGORY_COLORS: Record<string, string> = {
  pii: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
  financial: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
  custom: 'bg-accent/15 text-accent',
};

export default function DataSecurityPage() {
  const [rules, setRules] = useState<RedactionRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'rules' | 'controls' | 'retention'>('rules');
  const [policies, setPolicies] = useState<any[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [newRule, setNewRule] = useState({ name: '', pattern: '', replacement: '', category: 'custom', description: '' });
  const [testModal, setTestModal] = useState<{ pattern: string; open: boolean }>({ pattern: '', open: false });
  const [testInput, setTestInput] = useState('Test PAN: ABCDE1234F, Phone: +91 9876543210');
  const [testResult, setTestResult] = useState<any>(null);

  // AI Data Controls
  const [controls, setControls] = useState<Record<string, string>>({});
  const [controlsLoading, setControlsLoading] = useState(false);

  const fetchRules = () => {
    setLoading(true);
    getRedactionRules().then(setRules).finally(() => setLoading(false));
  };

  const fetchControls = () => {
    setControlsLoading(true);
    getAdminSettings().then((settings) => {
      const c: Record<string, string> = {};
      settings.forEach((s: any) => {
        if (s.key.startsWith('ai_send_') || s.key === 'ai_max_context_chars') {
          c[s.key] = s.value;
        }
      });
      setControls(c);
    }).finally(() => setControlsLoading(false));
  };

  const fetchPolicies = () => {
    getRetentionPolicies().then(setPolicies).catch(() => {});
  };

  useEffect(() => { fetchRules(); fetchControls(); fetchPolicies(); }, []);

  const handleCreate = async () => {
    if (!newRule.name || !newRule.pattern || !newRule.replacement) return;
    await createRedactionRule(newRule);
    setNewRule({ name: '', pattern: '', replacement: '', category: 'custom', description: '' });
    setShowAdd(false);
    fetchRules();
  };

  const handleToggle = async (rule: RedactionRule) => {
    await updateRedactionRule(rule.id, { is_enabled: !rule.is_enabled });
    fetchRules();
  };

  const handleDelete = async (id: number) => {
    await deleteRedactionRule(id);
    fetchRules();
  };

  const handleTest = async (pattern: string) => {
    setTestModal({ pattern, open: true });
    setTestResult(null);
  };

  const runTest = async () => {
    const result = await testRedactionPattern(testModal.pattern, testInput);
    setTestResult(result);
  };

  const handleControlToggle = async (key: string) => {
    const newVal = controls[key] === 'true' ? 'false' : 'true';
    await updateAdminSetting(key, newVal);
    setControls((p) => ({ ...p, [key]: newVal }));
  };

  if (loading) return <><Header title="Data Security" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Data Security" />
      <div className="mx-auto w-full max-w-[1600px] space-y-5 p-4 sm:p-6 lg:p-8">
        {/* Tabs */}
        <div className="flex gap-1 bg-muted rounded-lg p-1 w-fit">
          <button onClick={() => setActiveTab('rules')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${activeTab === 'rules' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'}`}>
            Redaction Rules
          </button>
          <button onClick={() => setActiveTab('retention')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${activeTab === 'retention' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'}`}>
            Retention Policies
          </button>
          <button onClick={() => setActiveTab('controls')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${activeTab === 'controls' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'}`}>
            AI Data Controls
          </button>
        </div>

        {activeTab === 'rules' && (
          <div className="space-y-4">
            <div className="flex justify-between items-center">
              <p className="text-sm text-muted-foreground">{rules.length} redaction rules ({rules.filter(r => r.is_system).length} system, {rules.filter(r => !r.is_system).length} custom)</p>
              <button onClick={() => setShowAdd(true)} className="flex items-center gap-2 bg-purple-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-purple-700">
                <Plus size={16} /> Add Custom Rule
              </button>
            </div>

            {showAdd && (
              <div className="bg-card rounded-lg border border-border p-5 space-y-3">
                <h3 className="text-sm font-semibold text-foreground">New Redaction Rule</h3>
                <div className="grid grid-cols-2 gap-3">
                  <input placeholder="Name (e.g. Credit Card)" value={newRule.name} onChange={(e) => setNewRule(p => ({ ...p, name: e.target.value }))} className="border border-border rounded-lg px-3 py-2 text-sm" />
                  <input placeholder="Regex pattern" value={newRule.pattern} onChange={(e) => setNewRule(p => ({ ...p, pattern: e.target.value }))} className="border border-border rounded-lg px-3 py-2 text-sm font-mono" />
                  <input placeholder="Replacement (e.g. [REDACTED-CC])" value={newRule.replacement} onChange={(e) => setNewRule(p => ({ ...p, replacement: e.target.value }))} className="border border-border rounded-lg px-3 py-2 text-sm" />
                  <select value={newRule.category} onChange={(e) => setNewRule(p => ({ ...p, category: e.target.value }))} className="border border-border rounded-lg px-3 py-2 text-sm">
                    <option value="custom">Custom</option>
                    <option value="pii">PII</option>
                    <option value="financial">Financial</option>
                  </select>
                </div>
                <div className="flex gap-2">
                  <button onClick={handleCreate} className="bg-purple-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-purple-700">Create</button>
                  <button onClick={() => setShowAdd(false)} className="border border-border text-muted-foreground px-4 py-2 rounded-lg text-sm hover:bg-muted/40">Cancel</button>
                </div>
              </div>
            )}

            <div className="bg-card rounded-lg border border-border overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="bg-muted/40 border-b border-border">
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Name</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Pattern</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Replacement</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Category</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Enabled</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {rules.map((rule) => (
                    <tr key={rule.id} className="border-b border-border">
                      <td className="px-4 py-3 text-sm font-medium text-foreground">
                        <div className="flex items-center gap-2">
                          {rule.is_system && <Lock size={12} className="text-muted-foreground" />}
                          {rule.name}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-xs font-mono text-muted-foreground max-w-[200px] truncate">{rule.pattern}</td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">{rule.replacement}</td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${CATEGORY_COLORS[rule.category] || CATEGORY_COLORS.custom}`}>{rule.category}</span>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <button onClick={() => handleToggle(rule)}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${rule.is_enabled ? 'bg-purple-600' : 'bg-muted-foreground/40'}`}>
                          <span className={`inline-block h-3 w-3 transform rounded-full bg-card transition-transform ${rule.is_enabled ? 'translate-x-5' : 'translate-x-1'}`} />
                        </button>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <div className="flex justify-center gap-1">
                          <button onClick={() => handleTest(rule.pattern)} className="text-xs text-accent hover:underline px-1" title="Test"><FlaskConical size={13} /></button>
                          {!rule.is_system && (
                            <button onClick={() => handleDelete(rule.id)} className="text-xs text-red-600 dark:text-red-400 hover:underline px-1" title="Delete"><Trash2 size={13} /></button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {activeTab === 'controls' && (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">Control what data is sent to AI services for processing.</p>
            {[
              { key: 'ai_send_tender_description', label: 'Send tender descriptions to AI', desc: 'Allow AI agents to process tender title and description text' },
              { key: 'ai_send_document_content', label: 'Send document content to AI', desc: 'Allow AI agents to read uploaded tender documents' },
              { key: 'ai_send_financial_data', label: 'Send financial data to AI', desc: 'Allow AI agents to see EMD amounts, estimated values, and pricing' },
            ].map(({ key, label, desc }) => (
              <div key={key} className="bg-card rounded-lg border border-border p-4 flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-foreground">{label}</p>
                  <p className="text-xs text-muted-foreground">{desc}</p>
                </div>
                <button onClick={() => handleControlToggle(key)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${controls[key] === 'true' ? 'bg-purple-600' : 'bg-muted-foreground/40'}`}>
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-card transition-transform ${controls[key] === 'true' ? 'translate-x-6' : 'translate-x-1'}`} />
                </button>
              </div>
            ))}
          </div>
        )}

        {activeTab === 'retention' && (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">Configure how long different data types are retained. Set to 0 days to keep forever.</p>
            <div className="bg-card rounded-lg border border-border overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="bg-muted/40 border-b border-border">
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Data Type</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase">Description</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Retention</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Auto Delete</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-muted-foreground uppercase">Enabled</th>
                  </tr>
                </thead>
                <tbody>
                  {policies.map((p: any) => (
                    <tr key={p.id} className="border-b border-border">
                      <td className="px-4 py-3 text-sm font-medium text-foreground font-mono">{p.data_type}</td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">{p.description}</td>
                      <td className="px-4 py-3 text-center">
                        <select value={p.retention_days}
                          onChange={async (e) => { await updateRetentionPolicy(p.id, { retention_days: parseInt(e.target.value) }); fetchPolicies(); }}
                          className="border border-border rounded px-2 py-1 text-xs bg-card">
                          <option value={0}>Forever</option>
                          <option value={30}>30 days</option>
                          <option value={60}>60 days</option>
                          <option value={90}>90 days</option>
                          <option value={180}>180 days</option>
                          <option value={365}>1 year</option>
                        </select>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <button onClick={async () => { await updateRetentionPolicy(p.id, { auto_delete: !p.auto_delete }); fetchPolicies(); }}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${p.auto_delete ? 'bg-red-500' : 'bg-muted-foreground/40'}`}>
                          <span className={`inline-block h-3 w-3 transform rounded-full bg-card transition-transform ${p.auto_delete ? 'translate-x-5' : 'translate-x-1'}`} />
                        </button>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <button onClick={async () => { await updateRetentionPolicy(p.id, { is_enabled: !p.is_enabled }); fetchPolicies(); }}
                          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${p.is_enabled ? 'bg-purple-600' : 'bg-muted-foreground/40'}`}>
                          <span className={`inline-block h-3 w-3 transform rounded-full bg-card transition-transform ${p.is_enabled ? 'translate-x-5' : 'translate-x-1'}`} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Test modal */}
        {testModal.open && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setTestModal({ pattern: '', open: false })}>
            <div className="bg-card rounded-xl p-6 w-[500px] max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
              <h3 className="text-sm font-semibold text-foreground mb-3">Test Redaction Pattern</h3>
              <p className="text-xs text-muted-foreground mb-2 font-mono">{testModal.pattern}</p>
              <textarea value={testInput} onChange={(e) => setTestInput(e.target.value)} className="w-full border border-border rounded-lg px-3 py-2 text-sm mb-3" rows={4} placeholder="Enter sample text..." />
              <button onClick={runTest} className="bg-purple-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-purple-700 mb-3">Run Test</button>
              {testResult && (
                <div className="space-y-2">
                  <p className="text-xs font-medium text-muted-foreground">Matches found: <span className="text-purple-600 dark:text-purple-400">{testResult.match_count}</span></p>
                  {testResult.matches?.length > 0 && (
                    <div className="bg-muted/40 rounded p-2 text-xs font-mono">{testResult.matches.join(', ')}</div>
                  )}
                  <p className="text-xs font-medium text-muted-foreground mt-2">Redacted output:</p>
                  <div className="bg-muted/40 rounded p-2 text-xs">{testResult.redacted_text}</div>
                </div>
              )}
              <button onClick={() => setTestModal({ pattern: '', open: false })} className="mt-3 text-xs text-muted-foreground hover:underline">Close</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
