/* The control bar that sticks to the top of the page as it scrolls (App.tsx):
 * the window and the class, user and agent-type filters, so a tab three
 * screens long still says at its foot what is on screen.
 *
 * Its height is published as `--controls-h`, so whatever else sticks to the
 * top or is scrolled there -- a panel picked in the rail's section list, the
 * Agents tab's list and detail, the trajectory inspector -- lands below the bar
 * rather than under it. Where the bar does not stick (a phone), it is 0.
 */
import { useEffect, useLayoutEffect, useState } from 'react';
import type { RefObject } from 'react';

const VAR = '--controls-h';

/** The bar's height while it sticks, in px; 0 where it does not. */
export function stickyTop(): number {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue(VAR)) || 0;
}

/** Publishes the bar's height, and says whether it is stuck right now: the
 *  sentinel just above it has scrolled out of the top of the viewport. */
export function useStickyBar(
  bar: RefObject<HTMLElement | null>,
  sentinel: RefObject<HTMLElement | null>,
  on: boolean,
): boolean {
  const [stuck, setStuck] = useState(false);

  // Before paint, so a jump made on the first frame already clears the bar.
  useLayoutEffect(() => {
    const root = document.documentElement.style;
    const el = bar.current;
    if (!on || !el) {
      root.setProperty(VAR, '0px');
      return;
    }
    const publish = () =>
      root.setProperty(VAR, getComputedStyle(el).position === 'sticky' ? `${el.offsetHeight}px` : '0px');
    publish();
    // the bar wraps to a second row on a narrow screen, or when a filter is in force
    const ro = new ResizeObserver(publish);
    ro.observe(el);
    // and crossing the breakpoint where it stops sticking changes no size of its own
    window.addEventListener('resize', publish);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', publish);
      root.setProperty(VAR, '0px');
    };
  }, [bar, on]);

  useEffect(() => {
    const s = sentinel.current;
    if (!on || !s) {
      setStuck(false);
      return;
    }
    const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting && e.boundingClientRect.top < 0));
    io.observe(s);
    return () => io.disconnect();
  }, [sentinel, on]);

  return stuck;
}
