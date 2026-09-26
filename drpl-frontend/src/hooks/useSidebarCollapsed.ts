import { useEffect, useState } from 'react';

const STORAGE_KEY = 'drpl_cc_sidebar_collapsed';

/**
 * Persisted boolean state for the Command Center sidebar collapse toggle.
 * Defaults to `false` (expanded). Survives reloads via localStorage.
 */
export function useSidebarCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsedState] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
    } catch {
      // ignore quota / privacy-mode failures
    }
  }, [collapsed]);

  return [collapsed, setCollapsedState];
}
