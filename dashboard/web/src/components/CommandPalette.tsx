/* Go to anything: a tab, a section of the open tab, a window, an action.
 *
 * Opened with Ctrl+K or Cmd+K, or `/` when no field has the cursor (App.tsx).
 * Twelve tabs and a section list per tab is a lot to reach by pointer; typed,
 * it is three keys. The list is assembled by the page from what it already
 * holds (its tabs, the open tab's sections as the rail lists them, the
 * service's window presets), so it offers nothing the page cannot do.
 *
 * Keyboard: type to filter, arrow keys to move, Enter to go, Escape to close.
 * Focus goes back where it was when the palette closes.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './Icon';
import type { IconName } from './Icon';

export interface PaletteItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: IconName;
  /** extra words it answers to */
  keywords?: string;
  run: () => void;
}

/** Every word typed must appear somewhere in the item; its label leads the rank. */
function rank(item: PaletteItem, q: string): number {
  if (!q) return 1;
  const label = item.label.toLowerCase();
  const all = `${label} ${item.group.toLowerCase()} ${(item.hint ?? '').toLowerCase()} ${(item.keywords ?? '').toLowerCase()}`;
  const words = q.split(/\s+/).filter(Boolean);
  if (!words.every((w) => all.includes(w))) return 0;
  if (label.startsWith(q)) return 4;
  if (label.split(/[\s&·—-]+/).some((w) => w.startsWith(words[0]))) return 3;
  if (label.includes(words[0])) return 2;
  return 1;
}

export function CommandPalette({
  open,
  onClose,
  items,
}: {
  open: boolean;
  onClose: () => void;
  items: PaletteItem[];
}) {
  const [q, setQ] = useState('');
  const [at, setAt] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const back = useRef<Element | null>(null);

  // Opening: remember where focus was, start empty. Closing: put focus back.
  useEffect(() => {
    if (!open) return;
    back.current = document.activeElement;
    setQ('');
    setAt(0);
    const t = requestAnimationFrame(() => input.current?.focus());
    return () => {
      cancelAnimationFrame(t);
      const el = back.current as HTMLElement | null;
      if (el && typeof el.focus === 'function' && document.contains(el)) el.focus({ preventScroll: true });
    };
  }, [open]);

  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const ranked = items
      .map((it, i) => ({ it, i, r: rank(it, ql) }))
      .filter((x) => x.r > 0)
      // ranked when searching; in the page's own order when not
      .sort((a, b) => (ql ? b.r - a.r || a.i - b.i : a.i - b.i))
      .map((x) => x.it);
    // One order to draw and to walk: grouped as the groups first appear,
    // ranked within each. Ranked across groups and drawn grouped, the arrow
    // keys jumped over the rows on screen and back.
    const order: string[] = [];
    const by = new Map<string, PaletteItem[]>();
    for (const it of ranked) {
      if (!by.has(it.group)) {
        by.set(it.group, []);
        order.push(it.group);
      }
      by.get(it.group)!.push(it);
    }
    return order.flatMap((g) => by.get(g)!);
  }, [items, q]);

  useEffect(() => {
    if (at >= shown.length) setAt(Math.max(0, shown.length - 1));
  }, [shown.length, at]);

  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-at="${at}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [at, shown]);

  if (!open) return null;

  const go = (it: PaletteItem | undefined) => {
    if (!it) return;
    onClose();
    // after the palette has closed and focus has gone back, so a scroll the
    // item makes is not undone by the focus moving
    requestAnimationFrame(() => it.run());
  };

  // groups in the order their first item appears
  const groups: { name: string; rows: { it: PaletteItem; n: number }[] }[] = [];
  shown.forEach((it, n) => {
    let g = groups.find((x) => x.name === it.group);
    if (!g) groups.push((g = { name: it.group, rows: [] }));
    g.rows.push({ it, n });
  });

  return (
    <div className="cmdk-wrap" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="Go to a tab, a section or a window">
        <div className="cmdk-in">
          <Icon name="search" size={17} />
          <input
            ref={input}
            value={q}
            placeholder="Go to a tab, a section, a window…"
            aria-label="Go to"
            aria-controls="cmdk-list"
            aria-activedescendant={shown.length ? `cmdk-${at}` : undefined}
            role="combobox"
            aria-expanded="true"
            onChange={(e) => {
              setQ(e.target.value);
              setAt(0);
            }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setAt((a) => (shown.length ? (a + 1) % shown.length : 0));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setAt((a) => (shown.length ? (a - 1 + shown.length) % shown.length : 0));
              } else if (e.key === 'Enter') {
                e.preventDefault();
                go(shown[at]);
              } else if (e.key === 'Escape') {
                e.preventDefault();
                onClose();
              } else if (e.key === 'Tab') {
                // the dialog holds focus: Tab moves through the list instead
                e.preventDefault();
                setAt((a) => (shown.length ? (a + (e.shiftKey ? -1 : 1) + shown.length) % shown.length : 0));
              }
            }}
          />
          <kbd>esc</kbd>
        </div>
        <div className="cmdk-list" id="cmdk-list" role="listbox" ref={list}>
          {!shown.length && <div className="cmdk-none">nothing on this page answers to “{q.trim()}”</div>}
          {groups.map((g) => (
            <div key={g.name} role="group" aria-label={g.name}>
              <div className="cmdk-group">{g.name}</div>
              {g.rows.map(({ it, n }) => (
                <div
                  key={it.id}
                  id={`cmdk-${n}`}
                  data-at={n}
                  role="option"
                  aria-selected={n === at}
                  className="cmdk-item"
                  onMouseMove={() => n !== at && setAt(n)}
                  onClick={() => go(it)}
                >
                  <Icon name={it.icon} size={16} />
                  <span className="cmdk-lbl">{it.label}</span>
                  {it.hint && <span className="cmdk-hint">{it.hint}</span>}
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> go</span>
          <span><kbd>/</kbd> or <kbd>Ctrl K</kbd> opens this</span>
          <span><kbd>[</kbd> folds the sidebar</span>
        </div>
      </div>
    </div>
  );
}
