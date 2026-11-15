/* One document-level tooltip, driven by `data-tip` anywhere in the tree.
 *
 * Ported from the archived template: it works uniformly over HTML and SVG (SVG
 * elements cannot carry a `title` attribute usefully), so a chart rect and a
 * table cell get the same affordance with no per-element wiring.
 *
 * The pointer moves it; so does the keyboard. A control reached with Tab shows
 * its tip under itself, because the caveat about how a number is counted lives
 * in these tips, and a reader who does not use a mouse needs it as much.
 */
import { useEffect, useRef } from 'react';

/** Focus that came from the keyboard. A browser that cannot parse the selector
 *  throws rather than answering, and then the tip simply stays pointer-only. */
function keyboardFocus(t: Element): boolean {
  try {
    return t.matches(':focus-visible');
  } catch {
    return false;
  }
}

/** The tip a focused element answers to: its own or an ancestor's, else, for a
 *  table header, the "?" term on its label (SortTh puts the tip there). */
function tipOf(t: Element): HTMLElement | null {
  const own = t.closest<HTMLElement>('[data-tip]');
  if (own?.dataset.tip) return own;
  if (t.tagName === 'TH') {
    const inner = t.querySelector<HTMLElement>('[data-tip]');
    if (inner?.dataset.tip) return inner;
  }
  return null;
}

export function TooltipLayer() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fill = (src: HTMLElement) => {
      el.textContent = src.dataset.tip ?? '';
      // a whole command line reads better in a wider box than a sentence does
      el.classList.toggle('wide', src.hasAttribute('data-tip-wide'));
      el.style.opacity = '1';
      return el.getBoundingClientRect();
    };
    const move = (e: MouseEvent) => {
      const t = e.target as Element | null;
      const src = t && 'closest' in t ? t.closest<HTMLElement>('[data-tip]') : null;
      if (!src || !src.dataset.tip) {
        el.style.opacity = '0';
        return;
      }
      const r = fill(src);
      let x = e.clientX + 13;
      let y = e.clientY + 13;
      if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 10;
      if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 10;
      el.style.left = x + 'px';
      el.style.top = y + 'px';
    };
    const leave = () => {
      el.style.opacity = '0';
    };
    // Keyboard focus only: a click also focuses its button, and the pointer's
    // tip is already showing there.
    const focus = (e: FocusEvent) => {
      const t = e.target;
      if (!(t instanceof Element) || !keyboardFocus(t)) return;
      const src = tipOf(t);
      if (!src) {
        el.style.opacity = '0';
        return;
      }
      const r = fill(src);
      const a = t.getBoundingClientRect();
      let x = Math.min(a.left, innerWidth - 8 - r.width);
      let y = a.bottom + 6;
      if (y + r.height > innerHeight - 8) y = a.top - r.height - 6;
      el.style.left = Math.max(8, x) + 'px';
      el.style.top = Math.max(8, y) + 'px';
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') leave();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseleave', leave);
    document.addEventListener('focusin', focus);
    document.addEventListener('focusout', leave);
    document.addEventListener('keydown', esc);
    window.addEventListener('scroll', leave, true);
    return () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseleave', leave);
      document.removeEventListener('focusin', focus);
      document.removeEventListener('focusout', leave);
      document.removeEventListener('keydown', esc);
      window.removeEventListener('scroll', leave, true);
    };
  }, []);

  return <div id="tip" ref={ref} role="tooltip" aria-hidden="true" />;
}
