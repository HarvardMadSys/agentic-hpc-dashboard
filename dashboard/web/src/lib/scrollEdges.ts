/* A table wider than its panel scrolls sideways inside it (`.scroller`). Its
 * edge fades where there is more to scroll to (styles.css, `data-more`), so a
 * column cut off at the edge reads as "scroll for more", not as the table's
 * last column: Users at 900px, most tables on a phone.
 *
 * This keeps `data-more` true to the scroll position -- on a scroll, when the
 * box or the table in it changes size, and for a scroller that appears later
 * (a panel filling in, another tab). In script rather than CSS alone: a
 * scroll-driven animation of the mask kept its end-of-scroll fade on a table
 * that had come to fit after the window widened (Chromium), fading the first
 * column of a table with nothing to scroll.
 */
import { useEffect } from 'react';
import type { RefObject } from 'react';

/** Which edges have more beyond them: '' | 'left' | 'right' | 'both'. */
function mark(s: HTMLElement) {
  const max = s.scrollWidth - s.clientWidth;
  const left = max > 1 && s.scrollLeft > 1;
  const right = max > 1 && s.scrollLeft < max - 1;
  const v = left && right ? 'both' : left ? 'left' : right ? 'right' : '';
  if ((s.dataset.more ?? '') === v) return;
  if (v) s.dataset.more = v;
  else delete s.dataset.more;
}

export function useScrollEdges(root: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const watched = new WeakSet<Element>();
    // the scroller's own box (the window resized) and the table in it (its
    // columns widened as data arrived) both move the edges
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const s = e.target.closest<HTMLElement>('.scroller');
        if (s) mark(s);
      }
    });
    const watch = (e: Element | null) => {
      if (e && !watched.has(e)) {
        watched.add(e);
        ro.observe(e);
      }
    };
    let raf = 0;
    const scan = () => {
      raf = 0;
      el.querySelectorAll<HTMLElement>('.scroller').forEach((s) => {
        watch(s);
        watch(s.firstElementChild);
        mark(s);
      });
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(scan);
    };
    // a scroll does not bubble; caught on the way down instead
    const onScroll = (e: Event) => {
      const t = e.target;
      if (t instanceof HTMLElement && t.classList.contains('scroller')) mark(t);
    };
    scan();
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true });
    el.addEventListener('scroll', onScroll, { capture: true, passive: true });
    return () => {
      mo.disconnect();
      ro.disconnect();
      el.removeEventListener('scroll', onScroll, { capture: true });
      if (raf) cancelAnimationFrame(raf);
    };
  }, [root]);
}
