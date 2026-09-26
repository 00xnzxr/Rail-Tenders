import { useEffect, useState } from 'react';
import { Bell, Mail, Loader2 } from 'lucide-react';
import {
  listNotificationPreferences,
  updateNotificationPreference,
} from '../../lib/notifications';
import type { NotificationPreference } from '../../types/notification';

/**
 * Admin-facing toggle matrix for notification preferences.
 *
 * Rows: notification kinds (seeded server-side).
 * Columns: in-app on/off, email on/off.
 *
 * Saves on every toggle (optimistic). Master admin only — guarded by the
 * route guard upstream, plus the backend route requires the same role.
 */
export default function NotificationPreferencesMatrix() {
  const [prefs, setPrefs] = useState<NotificationPreference[]>([]);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState<Record<string, boolean>>({});

  useEffect(() => {
    listNotificationPreferences()
      .then(setPrefs)
      .finally(() => setLoading(false));
  }, []);

  async function toggle(kind: string, field: 'in_app_enabled' | 'email_enabled', value: boolean) {
    const tag = `${kind}:${field}`;
    setUpdating((u) => ({ ...u, [tag]: true }));
    // Optimistic update
    setPrefs((prev) =>
      prev.map((p) => (p.kind === kind ? { ...p, [field]: value } : p)),
    );
    try {
      await updateNotificationPreference(kind, { [field]: value });
    } catch {
      // Roll back on failure
      setPrefs((prev) =>
        prev.map((p) => (p.kind === kind ? { ...p, [field]: !value } : p)),
      );
    } finally {
      setUpdating((u) => ({ ...u, [tag]: false }));
    }
  }

  if (loading) {
    return (
      <div className="bg-card rounded-lg border border-border p-6 flex items-center justify-center gap-2 text-muted-foreground">
        <Loader2 size={16} className="animate-spin" />
        <span className="text-sm">Loading notification preferences...</span>
      </div>
    );
  }

  if (prefs.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-border p-6 text-center text-sm text-muted-foreground">
        No notification kinds registered. They are seeded at backend startup.
      </div>
    );
  }

  return (
    <div className="bg-card rounded-lg border border-border overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold text-foreground">Notification Channels</h3>
        <p className="text-xs text-muted-foreground mt-1">
          Toggle which events deliver in-app notifications and/or email. Applies system-wide; users cannot override.
        </p>
      </div>
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="text-left px-4 py-2 font-medium">Event</th>
            <th className="text-center px-4 py-2 font-medium">
              <span className="inline-flex items-center gap-1.5">
                <Bell size={12} /> In-App
              </span>
            </th>
            <th className="text-center px-4 py-2 font-medium">
              <span className="inline-flex items-center gap-1.5">
                <Mail size={12} /> Email
              </span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {prefs.map((p) => (
            <tr key={p.kind}>
              <td className="px-4 py-3">
                <div className="font-mono text-xs text-foreground">{p.kind}</div>
                {p.description && (
                  <div className="text-xs text-muted-foreground mt-0.5">{p.description}</div>
                )}
              </td>
              <td className="text-center px-4 py-3">
                <Toggle
                  checked={p.in_app_enabled}
                  loading={!!updating[`${p.kind}:in_app_enabled`]}
                  onChange={(v) => toggle(p.kind, 'in_app_enabled', v)}
                />
              </td>
              <td className="text-center px-4 py-3">
                <Toggle
                  checked={p.email_enabled}
                  loading={!!updating[`${p.kind}:email_enabled`]}
                  onChange={(v) => toggle(p.kind, 'email_enabled', v)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Toggle({
  checked, loading, onChange,
}: { checked: boolean; loading: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={loading}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        checked ? 'bg-accent' : 'bg-muted-foreground/40'
      } ${loading ? 'opacity-60 cursor-wait' : 'cursor-pointer'}`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-card transition-transform ${
          checked ? 'translate-x-5' : 'translate-x-0.5'
        }`}
      />
    </button>
  );
}
