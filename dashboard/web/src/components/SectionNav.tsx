/* The open tab's sections, for "go to" (CommandPalette.tsx): what is on this
 * page, and a jump to it.
 *
 * Read off the page itself -- every <Panel> is a `section.panel` carrying its
 * title and an anchor -- so a tab never keeps a second list that could drift
 * from what it renders, and a panel that appears late (a window still
 * building, a section shown only with data) joins the list when it appears.
 */
import { useCallback, useEffect, useState } from 'react';
import type { RefObject } from 'react';

interface Sec {
  id: string;
  title: string;
}

export function useSections(
  root: RefObject<HTMLElement | null>,
  key: string,
): { secs: Sec[]; go: (id: string, behavior?: ScrollBehavior) => void } {
  const [secs, setSecs] = useState<Sec[]>([]);

  useEffect(() => {
    const el = root.current;
    if (!el) return;
    let raf = 0;
    const read = () => {
      raf = 0;
      const next = Array.from(el.querySelectorAll<HTMLElement>('section.panel[data-title]')).map((s) => ({
        id: s.id,
        title: s.dataset.title ?? '',
      }));
      // a live frame re-renders the page several times a second; only a real
      // change in the list is a state change
      setSecs((prev) =>
        prev.length === next.length && prev.every((p, i) => p.id === next[i].id && p.title === next[i].title)
          ? prev
          : next,
      );
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(read);
    };
    read();
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true });
    return () => {
      mo.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [root, key]);

  /** Scroll to a section: it lands just under the sticky bar (styles.css). */
  const go = useCallback((id: string, behavior: ScrollBehavior = 'smooth') => {
    document.getElementById(id)?.scrollIntoView({ behavior, block: 'start' });
  }, []);

  return { secs, go };
}
