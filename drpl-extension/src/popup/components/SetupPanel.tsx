import React, { useState } from 'react';
import { loginToBackend } from '../../utils/api-client';

interface Props {
  onLinked: () => void;
}

export default function SetupPanel({ onLinked }: Props) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [backendUrl, setBackendUrl] = useState('https://drpl-platform-production.up.railway.app');

  const handleLogin = async () => {
    if (!email.trim() || !password.trim()) {
      setError('Please enter your email and password.');
      return;
    }
    setLoading(true);
    setError('');
    const result = await loginToBackend(email, password, backendUrl);
    if (result.success) {
      onLinked();
    } else {
      setError(result.error || 'Login failed.');
    }
    setLoading(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !loading) handleLogin();
  };

  return (
    <div className="space-y-4">
      {/* Logo + title */}
      <div className="flex flex-col items-center pt-2 pb-1">
        <img src="/icons/icon-128.png" alt="" className="h-10 w-10 mb-3 rounded" />
        <h2 className="text-sm font-semibold text-slate-800 leading-tight">Sign in</h2>
        <p className="text-[11px] text-slate-400 mt-1 text-center">
          Use your workspace credentials to connect this extension.
        </p>
      </div>

      {/* Email */}
      <div>
        <label className="block text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-1.5">
          Email
        </label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onKeyDown={handleKeyDown}
          className="w-full px-3 py-2 text-sm text-slate-800 border border-slate-200 rounded-lg placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-400 transition-all"
          placeholder="you@company.com"
          autoFocus
        />
      </div>

      {/* Password */}
      <div>
        <label className="block text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-1.5">
          Password
        </label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={handleKeyDown}
          className="w-full px-3 py-2 text-sm text-slate-800 border border-slate-200 rounded-lg placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-400 transition-all"
          placeholder="Enter your password"
        />
      </div>

      {/* Advanced toggle */}
      <button
        type="button"
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="text-[11px] text-slate-400 hover:text-slate-600 transition-colors font-medium"
      >
        {showAdvanced ? '▾ Hide advanced' : '▸ Advanced settings'}
      </button>

      {showAdvanced && (
        <div>
          <label className="block text-[10px] font-semibold text-slate-400 uppercase tracking-widest mb-1.5">
            Backend URL
          </label>
          <input
            type="url"
            value={backendUrl}
            onChange={(e) => setBackendUrl(e.target.value)}
            className="w-full px-3 py-2 text-sm text-slate-800 border border-slate-200 rounded-lg placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-400 transition-all"
            placeholder="https://your-backend.example.com"
          />
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="px-3 py-2 bg-red-50 border border-red-100 rounded-lg">
          <p className="text-[11px] text-red-600">{error}</p>
        </div>
      )}

      {/* Submit */}
      <button
        onClick={handleLogin}
        disabled={loading}
        className="w-full py-2.5 bg-blue-600 text-white text-sm font-semibold rounded-lg hover:bg-blue-700 active:bg-blue-800 disabled:opacity-50 transition-colors shadow-sm"
      >
        {loading ? 'Signing in…' : 'Sign In'}
      </button>
    </div>
  );
}
