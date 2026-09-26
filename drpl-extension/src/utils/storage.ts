// ============================================================
// DRPL Extension - Storage Utility
// Wrapper around chrome.storage.local for typed access
// ============================================================

import { StorageData, ExtensionSettings, ScrapeSession } from './types';

const DEFAULT_SETTINGS: ExtensionSettings = {
  passiveMode: true,
  notificationsEnabled: true,
  scrapeIntervalMinutes: 360,     // 6 hours
  maxTendersPerBatch: 50,
  enabledPortals: ['ireps', 'gem'],
  deepScrapeEnabled: true,
  deepScrapeDelayMs: 2500,
  downloadDocumentsEnabled: true,
};

/** Get a value from storage */
export async function getStorage<K extends keyof StorageData>(
  key: K
): Promise<StorageData[K] | undefined> {
  const result = await chrome.storage.local.get(key);
  return result[key];
}

/** Set a value in storage */
export async function setStorage<K extends keyof StorageData>(
  key: K,
  value: StorageData[K]
): Promise<void> {
  await chrome.storage.local.set({ [key]: value });
}

/** Get extension settings with defaults */
export async function getSettings(): Promise<ExtensionSettings> {
  const settings = await getStorage('settings');
  return { ...DEFAULT_SETTINGS, ...settings };
}

/** Update extension settings (partial) */
export async function updateSettings(partial: Partial<ExtensionSettings>): Promise<void> {
  const current = await getSettings();
  await setStorage('settings', { ...current, ...partial });
}

/** Check if a tender ID has already been extracted (local dedup) */
export async function isTenderExtracted(tenderId: string): Promise<boolean> {
  const ids = (await getStorage('extractedTenderIds')) || [];
  return ids.includes(tenderId);
}

/** Mark tender IDs as extracted */
export async function markTendersExtracted(tenderIds: string[]): Promise<void> {
  const existing = (await getStorage('extractedTenderIds')) || [];
  const updated = [...new Set([...existing, ...tenderIds])];
  // Keep only last 10,000 IDs to avoid storage bloat
  const trimmed = updated.slice(-10000);
  await setStorage('extractedTenderIds', trimmed);
}

/** Add a scrape session to history */
export async function addScrapeSession(session: ScrapeSession): Promise<void> {
  const history = (await getStorage('scrapeHistory')) || [];
  history.push(session);
  // Keep only last 100 sessions
  const trimmed = history.slice(-100);
  await setStorage('scrapeHistory', trimmed);
}

/** Update the latest scrape session */
export async function updateLatestSession(
  updates: Partial<ScrapeSession>
): Promise<void> {
  const history = (await getStorage('scrapeHistory')) || [];
  if (history.length > 0) {
    history[history.length - 1] = { ...history[history.length - 1], ...updates };
    await setStorage('scrapeHistory', history);
  }
}
