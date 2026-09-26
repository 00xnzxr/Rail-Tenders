import { useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Plus, Search } from 'lucide-react';
import type { CommandCenterSession } from '../../types/command-center';
import { searchSessions } from './searchSessions';

interface SessionSwitcherPaletteProps {
  open: boolean;
  onClose: () => void;
  sessions: CommandCenterSession[];
  currentSessionId: number | null;
  onSwitch: (sessionId: number) => void;
  onNew: () => void;
}

/**
 * Cmd/Ctrl+K quick-switcher palette. Opens centered as a modal overlay
 * (~520px wide). Search input filters the session list via searchSessions.
 *
 * Keyboard:
 *   - Esc: close
 *   - Enter: select focused row
 *   - ArrowUp / ArrowDown: move focus
 *   - any printable: types into search input (always focused on open)
 *
 * Click outside the palette card to dismiss.
 */
export default function SessionSwitcherPalette({
  open,
  onClose,
  sessions,
  currentSessionId,
  onSwitch,
  onNew,
}: SessionSwitcherPaletteProps) {
  const [query, setQuery] = useState('');
  const [focusedIndex, setFocusedIndex] = useState(0);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Reset query + focus when the palette opens/closes
  useEffect(() => {
    if (open) {
      setQuery('');
      setFocusedIndex(0);
      // Defer focus so the input is mounted
      requestAnimationFrame(() => searchRef.current?.focus());
    }
  }, [open]);

  const matches = useMemo(
    () => searchSessions(sessions, query),
    [sessions, query],
  );

  // Clamp focusedIndex when matches shrink
  useEffect(() => {
    if (focusedIndex >= matches.length) {
      setFocusedIndex(Math.max(0, matches.length - 1));
    }
  }, [matches.length, focusedIndex]);

  // Keep focused row scrolled into view
  useEffect(() => {
    if (!listRef.current) return;
    const focusedEl = listRef.current.querySelector<HTMLElement>(
      `[data-palette-index="${focusedIndex}"]`,
    );
    focusedEl?.scrollIntoView({ block: 'nearest' });
  }, [focusedIndex]);

  if (!open) return null;

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setFocusedIndex((i) => Math.min(matches.length - 1, i + 1));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setFocusedIndex((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const picked = matches[focusedIndex];
      if (picked) {
        onSwitch(picked.id);
        onClose();
      }
      return;
    }
  };

  return (
    <div
      role="dialog"
      aria-label="Switch session"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 backdrop-blur-sm pt-[12vh] px-4"
      onClick={onClose}
      onKeyDown={handleKey}
    >
      <div
        className="w-full max-w-[520px] bg-card rounded-xl shadow-2xl border border-border overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
          <Search size={16} className="text-muted-foreground" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setFocusedIndex(0);
            }}
            placeholder="Search sessions by tender, title, organisation…"
            className="flex-1 bg-transparent text-sm text-foreground placeholder-slate-400 focus:outline-none"
            aria-label="Search sessions"
          />
          <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground bg-muted border border-border">
            esc
          </kbd>
        </div>

        {/* Results */}
        <div ref={listRef} className="max-h-[50vh] overflow-y-auto py-1">
          {matches.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted-foreground">No sessions match.</p>
              <button
                onClick={() => {
                  onNew();
                  onClose();
                }}
                className="mt-2 inline-flex items-center gap-1.5 text-xs text-accent hover:text-accent/80 hover:underline"
              >
                <Plus size={12} />
                Start a new session
              </button>
            </div>
          ) : (
            matches.map((s, i) => (
              <PaletteRow
                key={s.id}
                session={s}
                isFocused={i === focusedIndex}
                isCurrent={s.id === currentSessionId}
                index={i}
                onClick={() => {
                  onSwitch(s.id);
                  onClose();
                }}
                onHover={() => setFocusedIndex(i)}
              />
            ))
          )}
        </div>

        {/* Footer hint */}
        <div className="flex items-center justify-between px-4 py-2 border-t border-border bg-muted/40 text-[10px] text-muted-foreground">
          <span>
            <kbd className="font-mono px-1 py-0.5 rounded bg-card border border-border">↑↓</kbd> navigate
            <span className="mx-1.5">·</span>
            <kbd className="font-mono px-1 py-0.5 rounded bg-card border border-border">↵</kbd> select
          </span>
          <span>{matches.length} session{matches.length === 1 ? '' : 's'}</span>
        </div>
      </div>
    </div>
  );
}

function PaletteRow({
  session,
  isFocused,
  isCurrent,
  index,
  onClick,
  onHover,
}: {
  session: CommandCenterSession;
  isFocused: boolean;
  isCurrent: boolean;
  index: number;
  onClick: () => void;
  onHover: () => void;
}) {
  const title =
    (session.tender_id ? session.tender_title : null) ||
    session.title ||
    'Untitled Session';
  const subtitle = session.tender_organisation || '';

  return (
    <button
      data-palette-index={index}
      onMouseEnter={onHover}
      onClick={onClick}
      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
        isFocused ? 'bg-accent/10' : 'hover:bg-muted/40'
      }`}
    >
      <div className="w-7 h-7 rounded-md bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center flex-shrink-0">
        <Bot size={14} className="text-white" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-foreground truncate">{title}</p>
        {subtitle && <p className="text-xs text-muted-foreground truncate">{subtitle}</p>}
      </div>
      {isCurrent && (
        <span className="text-[10px] font-medium text-accent bg-accent/15 px-1.5 py-0.5 rounded">
          current
        </span>
      )}
    </button>
  );
}
