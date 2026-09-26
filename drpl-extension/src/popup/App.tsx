import React, { useEffect, useState } from 'react';
import StatusPanel from './components/StatusPanel';
import ScrapeControls from './components/ScrapeControls';
import ScrapeProgressPanel from './components/ScrapeProgressPanel';
import SettingsPanel from './components/SettingsPanel';
import SetupPanel from './components/SetupPanel';
import KeywordPanel from './components/KeywordPanel';
import type { ScrapeJobProgress } from '../utils/types';

type View = 'status' | 'settings' | 'login';

export default function App() {
  const [view, setView] = useState<View>('status');
  const [isLoggedIn, setIsLoggedIn] = useState(false);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);

  useEffect(() => {
    chrome.storage.local.get(['authToken', 'userEmail', 'scrapeJobProgress'], (result) => {
      const loggedIn = !!result.authToken;
      setIsLoggedIn(loggedIn);
      setUserEmail(result.userEmail || null);
      const job: ScrapeJobProgress | undefined = result.scrapeJobProgress;
      setJobActive(!!job && (job.status === 'running' || job.status === 'completed'));
      if (!loggedIn) setView('login');
    });

    const listener = (changes: { [key: string]: chrome.storage.StorageChange }) => {
      if ('authToken' in changes) {
        const loggedIn = !!changes.authToken.newValue;
        setIsLoggedIn(loggedIn);
        if (!loggedIn) {
          setUserEmail(null);
          setView('login');
        }
      }
      if ('userEmail' in changes) {
        setUserEmail(changes.userEmail.newValue || null);
      }
      if ('scrapeJobProgress' in changes) {
        const job: ScrapeJobProgress | undefined = changes.scrapeJobProgress.newValue;
        setJobActive(!!job && (job.status === 'running' || job.status === 'completed'));
      }
    };
    chrome.storage.onChanged.addListener(listener);
    return () => chrome.storage.onChanged.removeListener(listener);
  }, []);

  const handleLoggedIn = () => {
    chrome.storage.local.get(['userEmail'], (result) => {
      setUserEmail(result.userEmail || null);
    });
    setIsLoggedIn(true);
    setView('status');
  };

  return (
    <div className="bg-slate-50 min-h-[480px] flex flex-col">

      {/* Header */}
      <header className="bg-white border-b border-slate-100 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <img
            src="/icons/icon-128.png"
            alt=""
            className="h-7 w-7 rounded"
          />
          <span className="text-sm font-semibold text-slate-700">Workflow Companion</span>
        </div>
        {isLoggedIn && (
          <div className="flex gap-1">
            <NavButton active={view === 'status'} onClick={() => setView('status')} label="Status" />
            <NavButton active={view === 'settings'} onClick={() => setView('settings')} label="Settings" />
          </div>
        )}
      </header>

      {/* Connection status bar */}
      <div
        className={`px-4 py-1.5 flex items-center gap-2 text-xs font-medium border-b ${
          isLoggedIn
            ? 'bg-emerald-50 text-emerald-700 border-emerald-100'
            : 'bg-amber-50 text-amber-700 border-amber-100'
        }`}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${
            isLoggedIn ? 'bg-emerald-500 animate-pulse' : 'bg-amber-400'
          }`}
        />
        {isLoggedIn
          ? `Connected${userEmail ? ` — ${userEmail}` : ''}`
          : 'Not connected — login required'}
      </div>

      {/* Main content */}
      <main className="flex-1 p-4 overflow-y-auto">
        {!isLoggedIn && <SetupPanel onLinked={handleLoggedIn} />}
        {isLoggedIn && view === 'status' && (
          <>
            <KeywordPanel />
            <ScrapeControls />
            {jobActive && <ScrapeProgressPanel />}
            <StatusPanel />
          </>
        )}
        {isLoggedIn && view === 'settings' && <SettingsPanel />}
      </main>

      {/* Footer */}
      <footer className="px-4 py-2 text-center text-[10px] text-slate-300 border-t border-slate-100 font-medium tracking-wide">
        v2.0.0 — Private Distribution
      </footer>
    </div>
  );
}

function NavButton({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1 rounded-md text-xs font-semibold transition-all duration-150 ${
        active
          ? 'bg-blue-50 text-blue-700 border border-blue-200'
          : 'text-slate-500 hover:bg-slate-100 border border-transparent'
      }`}
    >
      {label}
    </button>
  );
}
