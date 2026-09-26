import { useState, useEffect, useCallback } from 'react'

const STORAGE_KEY = 'drpl_advanced_mode'

/**
 * Progressive disclosure: power-user controls (multi-select, batch ops,
 * advanced filters) are hidden by default so non-technical users see a clean,
 * focused view, and revealed when Advanced mode is on. The choice persists per
 * browser. Scoped by `key` so different pages can remember independently;
 * defaults to a shared global flag.
 */
export function useAdvancedMode(key = 'global') {
  const storageKey = `${STORAGE_KEY}:${key}`
  const [advanced, setAdvanced] = useState<boolean>(
    () => localStorage.getItem(storageKey) === '1'
  )

  useEffect(() => {
    localStorage.setItem(storageKey, advanced ? '1' : '0')
  }, [storageKey, advanced])

  const toggle = useCallback(() => setAdvanced((v) => !v), [])

  return { advanced, setAdvanced, toggle }
}
