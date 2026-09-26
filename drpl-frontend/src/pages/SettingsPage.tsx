import { useState, useEffect } from 'react';
import Header from '../components/layout/Header';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import PortalBadge from '../components/ui/PortalBadge';
import {
  getCurrentUser, getExtensionConfig, createAPIToken,
  getAPITokens, revokeAPIToken, getExtensionStatus,
} from '../lib/api';
import type { UserProfile, APITokenInfo, ExtensionStatus } from '../types/auth';
import { User, Plug, Shield, Key, Wifi, Copy, Trash2, Plus } from 'lucide-react';
import { portalLabel, formatTimeAgo, formatDateTime } from '../lib/formatters';

export default function SettingsPage() {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [config, setConfig] = useState<any>(null);
  const [tokens, setTokens] = useState<APITokenInfo[]>([]);
  const [extStatus, setExtStatus] = useState<ExtensionStatus | null>(null);
  const [loading, setLoading] = useState(true);

  // Token generation state
  const [tokenName, setTokenName] = useState('');
  const [newToken, setNewToken] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  useEffect(() => {
    Promise.all([
      getCurrentUser().catch((e) => { console.error('Profile load failed:', e); return null; }),
      getExtensionConfig().catch(() => null),
      getAPITokens().catch(() => []),
      getExtensionStatus().catch(() => null),
    ]).then(([user, ext, toks, status]) => {
      setProfile(user);
      setConfig(ext);
      setTokens(toks as APITokenInfo[]);
      setExtStatus(status as ExtensionStatus | null);
      setLoading(false);
    });
  }, []);

  const handleGenerateToken = async () => {
    if (!tokenName.trim()) return;
    setGenerating(true);
    try {
      const result = await createAPIToken(tokenName.trim());
      setNewToken(result.token);
      setTokenName('');
      // Refresh token list
      const updated = await getAPITokens();
      setTokens(updated);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to generate token');
    } finally {
      setGenerating(false);
    }
  };

  const handleRevokeToken = async (id: number) => {
    try {
      await revokeAPIToken(id);
      setTokens(tokens.filter((t) => t.id !== id));
    } catch {
      alert('Failed to revoke token');
    }
  };

  const handleCopyToken = () => {
    if (newToken) {
      navigator.clipboard.writeText(newToken);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (loading) return <><Header title="Settings" /><LoadingSpinner /></>;

  return (
    <>
      <Header title="Settings" subtitle="Your profile, connections, and security" />
      <div className="mx-auto w-full max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">
        {/* User Profile */}
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
            <User size={16} />
            User Profile
          </h3>
          {profile ? (
            <div className="grid grid-cols-2 gap-4">
              <InfoItem label="Name" value={profile.name} />
              <InfoItem label="Email" value={profile.email} />
              <InfoItem label="Role" value={profile.role} />
              <InfoItem label="Status" value={profile.is_active ? 'Active' : 'Disabled'} />
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Could not load user profile</p>
          )}
        </div>

        <button type="button" onClick={() => setAdvancedOpen((value) => !value)} aria-expanded={advancedOpen} className="flex w-full items-center justify-between rounded-2xl border border-border bg-card px-5 py-4 text-left shadow-card hover:border-accent/35">
          <span><span className="block text-sm font-bold">Connections and advanced settings</span><span className="mt-1 block text-xs text-muted-foreground">Browser extension, portal sync, and developer access</span></span>
          <span className="text-sm font-semibold text-accent">{advancedOpen ? 'Hide' : 'Show'}</span>
        </button>

        {advancedOpen && <div className="space-y-6 animate-fade-slide-up">
        {/* API Access / Token Management */}
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
            <Key size={16} />
            API Access
          </h3>
          <p className="text-sm text-muted-foreground mb-4">
            The Chrome extension now uses direct login — no token needed.
            API tokens below are for programmatic API access only.
          </p>

          {/* Generate new token */}
          <div className="flex gap-2 mb-4">
            <input
              type="text"
              value={tokenName}
              onChange={(e) => setTokenName(e.target.value)}
              placeholder="Token name (e.g., Chrome Extension)"
              className="flex-1 border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-drpl-secondary focus:border-transparent"
              onKeyDown={(e) => e.key === 'Enter' && handleGenerateToken()}
            />
            <button
              onClick={handleGenerateToken}
              disabled={generating || !tokenName.trim()}
              className="flex items-center gap-2 bg-accent text-accent-foreground px-4 py-2 rounded-lg text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-50"
            >
              <Plus size={16} />
              {generating ? 'Generating...' : 'Generate Token'}
            </button>
          </div>

          {/* Show new token (once) */}
          {newToken && (
            <div className="mb-4 p-3 bg-emerald-50 border border-emerald-200 dark:bg-emerald-500/15 dark:border-emerald-500/20 rounded-lg">
              <p className="text-xs text-emerald-700 font-medium mb-2">
                Token generated! Copy it now — it won't be shown again.
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-card border border-emerald-300 rounded px-3 py-2 text-sm font-mono text-foreground select-all">
                  {newToken}
                </code>
                <button
                  onClick={handleCopyToken}
                  className="flex items-center gap-1 px-3 py-2 rounded-lg text-sm bg-emerald-600 text-white hover:bg-emerald-700"
                >
                  <Copy size={14} />
                  {copied ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>
          )}

          {/* Token list */}
          {tokens.length > 0 ? (
            <div className="border border-border rounded-lg overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="bg-muted/40 border-b border-border">
                    <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase">Name</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase">Token</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase">Last Used</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase">Created</th>
                    <th className="px-4 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {tokens.map((t) => (
                    <tr key={t.id} className="border-b border-border">
                      <td className="px-4 py-2.5 text-sm text-foreground font-medium">{t.name}</td>
                      <td className="px-4 py-2.5 text-sm text-muted-foreground font-mono">{t.prefix}</td>
                      <td className="px-4 py-2.5 text-sm text-muted-foreground">
                        {t.last_used_at ? formatTimeAgo(t.last_used_at) : 'Never'}
                      </td>
                      <td className="px-4 py-2.5 text-sm text-muted-foreground">
                        {formatDateTime(t.created_at)}
                      </td>
                      <td className="px-4 py-2.5">
                        <button
                          onClick={() => handleRevokeToken(t.id)}
                          className="text-red-500 hover:text-red-700 p-1"
                          title="Revoke token"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No API tokens yet. Generate one to link the Chrome extension.</p>
          )}
        </div>

        {/* Extension Status */}
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
            <Wifi size={16} />
            Extension Status
          </h3>
          {extStatus ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className={`w-3 h-3 rounded-full ${extStatus.connected ? 'bg-emerald-500' : 'bg-muted-foreground/40'}`} />
                <span className={`text-sm font-medium ${extStatus.connected ? 'text-emerald-600' : 'text-muted-foreground'}`}>
                  {extStatus.connected ? 'Connected' : 'Not connected'}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <InfoItem label="Last Sync" value={extStatus.last_sync ? formatTimeAgo(extStatus.last_sync) : 'Never'} />
                <InfoItem label="Tenders (24h)" value={String(extStatus.tenders_uploaded_24h)} />
              </div>
              {extStatus.active_portals.length > 0 && (
                <div>
                  <p className="text-xs text-muted-foreground mb-2">Active Portals</p>
                  <div className="flex gap-2">
                    {extStatus.active_portals.map((p) => (
                      <PortalBadge key={p} portal={p} />
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Could not load extension status</p>
          )}
        </div>

        {/* Extension Config */}
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
            <Plug size={16} />
            Extension Configuration
          </h3>
          {config ? (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <InfoItem label="Selectors Version" value={config.selectors_version} />
                <InfoItem label="Scrape Interval" value={`${config.scrape_interval_minutes} minutes`} />
              </div>

              <div>
                <p className="text-xs text-muted-foreground mb-2">Enabled Portals</p>
                <div className="flex gap-2">
                  {config.enabled_portals?.map((p: string) => (
                    <span
                      key={p}
                      className="px-2.5 py-1 bg-accent/10 text-accent rounded-full text-xs font-medium"
                    >
                      {portalLabel(p)}
                    </span>
                  ))}
                </div>
              </div>

              <div>
                <p className="text-xs text-muted-foreground mb-2">Features</p>
                <div className="grid grid-cols-2 gap-2">
                  {config.features &&
                    Object.entries(config.features).map(([key, val]) => (
                      <div key={key} className="flex items-center gap-2">
                        <div
                          className={`w-2 h-2 rounded-full ${val ? 'bg-accent' : 'bg-muted-foreground/40'}`}
                        />
                        <span className="text-sm text-muted-foreground">
                          {key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Could not load extension configuration</p>
          )}
        </div>
        </div>}

        {/* Security */}
        <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
          <h3 className="text-sm font-medium text-muted-foreground uppercase tracking-wider mb-4 flex items-center gap-2">
            <Shield size={16} />
            Security
          </h3>
          <p className="text-sm leading-relaxed text-muted-foreground">
            Your sign-in is protected and this device keeps you signed in securely. Developer access keys can be reviewed or removed from Advanced settings above.
          </p>
        </div>
      </div>
    </>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm font-medium text-foreground mt-1">{value}</p>
    </div>
  );
}
