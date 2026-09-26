import React, { useEffect, useState } from 'react';
import { logoutFromBackend } from '../../utils/api-client';

interface Settings {
  passiveMode: boolean;
  notificationsEnabled: boolean;
  scrapeIntervalMinutes: number;
  maxTendersPerBatch: number;
  enabledPortals: string[];
}

const DEFAULTS: Settings = {
  passiveMode: true,
  notificationsEnabled: true,
  scrapeIntervalMinutes: 360,
  maxTendersPerBatch: 50,
  enabledPortals: ['ireps', 'gem'],
};

export default function SettingsPanel() {
  const [settings, setSettings] = useState<Settings>(DEFAULTS);
  const [saved, setSaved] = useState(false);
  const [userEmail, setUserEmail] = useState<string | null>(null);

  useEffect(() => {
    chrome.storage.local.get(['settings', 'userEmail'], (result) => {
      if (result.settings) {
        setSettings({ ...DEFAULTS, ...result.settings });
      }
      setUserEmail(result.userEmail || null);
    });
  }, []);

  const save = () => {
    chrome.storage.local.set({ settings }, () => {
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    });
  };

  const toggle = (key: keyof Settings) => {
    setSettings((s) => ({ ...s, [key]: !s[key] }));
  };

  const logout = async () => {
    if (confirm('Log out of your workspace?')) {
      await logoutFromBackend();
    }
  };

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-slate-700">Extension Settings</h2>

      <div className="bg-white rounded-xl border border-slate-100 divide-y divide-slate-50 overflow-hidden">
        <div className="px-3 py-3">
          <ToggleRow
            label="Passive Monitoring"
            description="Auto-extract data from pages you visit"
            checked={settings.passiveMode}
            onChange={() => toggle('passiveMode')}
          />
        </div>
        <div className="px-3 py-3">
          <ToggleRow
            label="Notifications"
            description="Show alerts for new items and session expiry"
            checked={settings.notificationsEnabled}
            onChange={() => toggle('notificationsEnabled')}
          />
        </div>
      </div>

      <div>
        <label className="block text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-1.5">
          Scrape Reminder Interval
        </label>
        <select
          value={settings.scrapeIntervalMinutes}
          onChange={(e) => setSettings((s) => ({ ...s, scrapeIntervalMinutes: Number(e.target.value) }))}
          className="w-full px-3 py-2 text-sm font-medium text-slate-700 border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-400 transition-all"
        >
          <option value={60}>Every 1 hour</option>
          <option value={180}>Every 3 hours</option>
          <option value={360}>Every 6 hours</option>
          <option value={720}>Every 12 hours</option>
        </select>
      </div>

      <button
        onClick={save}
        className={`w-full py-2 text-white text-sm font-semibold rounded-lg transition-colors shadow-sm ${
          saved
            ? 'bg-emerald-600 hover:bg-emerald-700'
            : 'bg-blue-600 hover:bg-blue-700 active:bg-blue-800'
        }`}
      >
        {saved ? '✓ Settings Saved' : 'Save Settings'}
      </button>

      <hr className="border-slate-100" />

      {userEmail && (
        <p className="text-[11px] text-slate-400 text-center">
          Signed in as{' '}
          <span className="font-semibold text-slate-600">{userEmail}</span>
        </p>
      )}

      <button
        onClick={logout}
        className="w-full py-2 text-red-500 text-sm font-semibold border border-red-100 rounded-lg hover:bg-red-50 active:bg-red-100 transition-colors"
      >
        Log Out
      </button>
    </div>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-slate-700 leading-tight">{label}</div>
        <div className="text-[11px] text-slate-400 mt-0.5 leading-relaxed">{description}</div>
      </div>
      <button
        onClick={onChange}
        className={`relative shrink-0 w-9 h-5 rounded-full overflow-hidden p-0 transition-colors duration-200 ${
          checked ? 'bg-blue-600' : 'bg-slate-200'
        }`}
        aria-checked={checked}
        role="switch"
      >
        <span
          className={`absolute left-0.5 top-0.5 w-4 h-4 bg-white rounded-full shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[16px]' : 'translate-x-0'
          }`}
        />
      </button>
    </div>
  );
}
