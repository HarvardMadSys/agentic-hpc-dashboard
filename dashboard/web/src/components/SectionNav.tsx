/* The open tab's sections, listed under it in the rail: what is on this page,
 * which one is on screen, and a click to go to it.
 *
 * Read off the page itself -- every <Panel> is a `section.panel` carrying its
 * title and an anchor -- so a tab never keeps a second list that could drift
 * from what it renders, and a panel that appears late (a window still
 * building, a section shown only with data) joins the list when it appears.
 */
import { useEffect, useState } from 'react';
import type { RefObject } from 'react';

interface Sec {
  id: string;
  title: string;
}

/** "Tool calls — did they run namespace-isolated?" -> "Tool calls";
 *  "Process exits per minute · last 24h" -> "Process exits per minute" */
const short = (t: string) => t.split(/ · | — /)[0];

export function useSections(root: RefObject<HTMLElement | null>, key: string): { secs: Sec[]; active: string | null } {
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

  useEffect(() => {
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        // the section whose top has passed a line a little below the top of
        // the viewport; at the very bottom of the page, the last one
        let cur: string | null = null;
        for (const s of secs) {
          const e = document.getElementById(s.id);
          if (e && e.getBoundingClientRect().top < 140) cur = s.id;
        }
        if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4 && secs.length) {
          cur = secs[secs.length - 1].id;
        }
        setActive(cur);
      });
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.removeEventListener('scroll', onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [secs]);

  return { secs, active };
}

export function SectionList({ secs, active }: { secs: Sec[]; active: string | null }) {
  if (secs.length < 2) return null;
  return (
    <div className="secnav">
      {secs.map((s) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          className={s.id === active ? 'on' : undefined}
          data-tip={short(s.title) !== s.title ? s.title : undefined}
          onClick={(e) => {
            e.preventDefault();
            document.getElementById(s.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }}
        >
          {short(s.title)}
        </a>
      ))}
    </div>
  );
}
