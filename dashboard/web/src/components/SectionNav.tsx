/* The open tab's sections, listed under it in the rail: what is on this page,
 * which one is on screen, and a click to go to it.
 *
 * Read off the page itself -- every <Panel> is a `section.panel` carrying its
 * title and an anchor -- so a tab never keeps a second list that could drift
 * from what it renders, and a panel that appears late (a window still
 * building, a section shown only with data) joins the list when it appears.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { stickyTop } from '../lib/sticky';

interface Sec {
  id: string;
  title: string;
}

/** "Tool calls — did they run namespace-isolated?" -> "Tool calls";
 *  "Process exits per minute · last 24h" -> "Process exits per minute" */
const short = (t: string) => t.split(/ · | — /)[0];

/** How far below the sticky bar a section's top must pass to be the current one, in px. */
const LINE = 64;
/** Keys that scroll the page: pressing one is scrolling by hand. */
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);
const typing = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

export function useSections(
  root: RefObject<HTMLElement | null>,
  key: string,
): { secs: Sec[]; active: string | null; go: (id: string, behavior?: ScrollBehavior) => void } {
  const [secs, setSecs] = useState<Sec[]>([]);
  const [active, setActive] = useState<string | null>(null);

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

  // A section picked from the list or the palette stays the current one
  // while it is on screen: near the foot of a page the browser cannot bring it
  // up to the line, and the line alone would then name one above it. Scrolling
  // by hand, or the section leaving the screen once it was on it, lets go.
  const picked = useRef<{ id: string; seen: boolean } | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let raf = 0;
    const measure = () => {
      raf = 0;
      if (!secs.length) {
        setActive(null);
        return;
      }
      // A jump lands a section just under the sticky bar (styles.css); the
      // line sits a little below that, so the section jumped to has passed it.
      const line = stickyTop() + LINE;
      // Nothing has passed the line yet: the first section is what is on screen.
      let cur = secs[0].id;
      for (const s of secs) {
        const e = document.getElementById(s.id);
        if (e && e.getBoundingClientRect().top < line) cur = s.id;
      }
      // At the foot of a page that scrolls, the last one, which may never
      // reach the line. Not on a page too short to scroll: it is always at its
      // foot, and its first section is the one on screen.
      const doc = document.documentElement;
      if (doc.scrollHeight > window.innerHeight + 4 && window.innerHeight + window.scrollY >= doc.scrollHeight - 4) {
        cur = secs[secs.length - 1].id;
      }
      const p = picked.current;
      const e = p && document.getElementById(p.id);
      if (p && e) {
        const r = e.getBoundingClientRect();
        const on = r.bottom > line && r.top < window.innerHeight;
        if (on) p.seen = true;
        if (on || !p.seen) cur = p.id;
        else picked.current = null;
      } else if (p) {
        picked.current = null;
      }
      setActive(cur);
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    const byHand = (ev: Event) => {
      if (ev instanceof KeyboardEvent && (!SCROLL_KEYS.has(ev.key) || typing(ev.target))) return;
      if (picked.current) {
        picked.current = null;
        schedule();
      }
    };
    measure();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule, { passive: true });
    window.addEventListener('wheel', byHand, { passive: true });
    window.addEventListener('touchstart', byHand, { passive: true });
    window.addEventListener('keydown', byHand);
    return () => {
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      window.removeEventListener('wheel', byHand);
      window.removeEventListener('touchstart', byHand);
      window.removeEventListener('keydown', byHand);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [secs, tick]);

  /** Scroll to a section and hold it as the current one (see `picked`). */
  const go = useCallback((id: string, behavior: ScrollBehavior = 'smooth') => {
    const e = document.getElementById(id);
    if (!e) return;
    picked.current = { id, seen: false };
    e.scrollIntoView({ behavior, block: 'start' });
    setTick((n) => n + 1);
  }, []);

  return { secs, active, go };
}

export function SectionList({
  secs,
  active,
  go,
}: {
  secs: Sec[];
  active: string | null;
  go: (id: string) => void;
}) {
  if (secs.length < 2) return null;
  return (
    <div className="secnav">
      {secs.map((s) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          className={s.id === active ? 'on' : undefined}
          aria-current={s.id === active ? 'location' : undefined}
          data-tip={short(s.title) !== s.title ? s.title : undefined}
          onClick={(e) => {
            e.preventDefault();
            go(s.id);
          }}
        >
          {short(s.title)}
        </a>
      ))}
    </div>
  );
}
