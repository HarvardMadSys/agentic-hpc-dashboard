/* One document-level tooltip, driven by `data-tip` anywhere in the tree.
 *
 * Ported from the archived template: it works uniformly over HTML and SVG (SVG
 * elements cannot carry a `title` attribute usefully), so a chart rect and a
 * table cell get the same affordance with no per-element wiring.
 *
 * A mouse shows it on hover, beside the pointer. The keyboard shows it too: a
 * control reached with Tab shows its tip under itself, because the caveat about
 * how a number is counted lives in these tips, and a reader who does not use a
 * mouse needs it as much. A finger has no hover, so on a touch screen a tap
 * shows it instead -- above the finger, not under it -- and it stays until the
 * next tap or a scroll. There it is the only way to read a bar's value, or what
 * a "?" explains. A tap on a control (a button, a link, a field) is taken as
 * working it, and shows nothing: its hint is about using it with a mouse, and
 * would only sit over what it opened.
 */
import { useEffect, useRef } from 'react';

const CONTROL = 'button, a[href], input, select, textarea, summary, label, [role="button"]';
/** How long after a tap the focus and blur it causes are the tap's, in ms. */
const TAP_MS = 600;

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

/** The element with a tip that `target` is in, if any. */
function tipAt(target: EventTarget | null): HTMLElement | null {
  const t = target as Element | null;
  const src = t && 'closest' in t ? t.closest<HTMLElement>('[data-tip]') : null;
  return src?.dataset.tip ? src : null;
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
    const hide = () => {
      el.style.opacity = '0';
    };
    const place = (x: number, y: number) => {
      el.style.left = x + 'px';
      el.style.top = y + 'px';
    };
    // A mouse or a pen hovering: beside the pointer, flipped at the window's edges.
    const move = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return;
      const src = tipAt(e.target);
      if (!src) return hide();
      const r = fill(src);
      let x = e.clientX + 13;
      let y = e.clientY + 13;
      if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 10;
      if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 10;
      place(x, y);
    };
    // A finger: centred over where it touched and clear of it, below only when
    // there is no room above. The focus a tap moves is the tap's, not the
    // keyboard's: it neither shows a control's tip nor hides this one.
    let tappedAt = -Infinity;
    const tap = (e: PointerEvent) => {
      if (e.pointerType !== 'touch') return;
      tappedAt = performance.now();
      const t = e.target as Element | null;
      const src = t && 'closest' in t && t.closest(CONTROL) ? null : tipAt(t);
      if (!src) return hide();
      const r = fill(src);
      const x = Math.max(8, Math.min(e.clientX - r.width / 2, innerWidth - r.width - 8));
      const above = e.clientY - r.height - 28;
      place(x, above >= 8 ? above : e.clientY + 28);
    };
    const tapping = () => performance.now() - tappedAt < TAP_MS;
    // under the control, or over it where there is no room below
    const placeAt = (t: Element, src: HTMLElement) => {
      const r = fill(src);
      const a = t.getBoundingClientRect();
      const x = Math.min(a.left, innerWidth - 8 - r.width);
      let y = a.bottom + 6;
      if (y + r.height > innerHeight - 8) y = a.top - r.height - 6;
      place(Math.max(8, x), Math.max(8, y));
    };
    // A control reached with Tab is scrolled into view by the browser after
    // the focus event, so the tip is placed again over the next two frames,
    // from where the control landed. A scroll inside those frames is that one,
    // not the reader moving away, and keeps the tip; any later scroll hides it.
    let settle = 0;
    // Keyboard focus only: a click also focuses its button, and the pointer's
    // tip is already showing there.
    const focus = (e: FocusEvent) => {
      const t = e.target;
      if (!(t instanceof Element) || tapping() || !keyboardFocus(t)) return;
      const src = tipOf(t);
      cancelAnimationFrame(settle);
      settle = 0;
      if (!src) return hide();
      placeAt(t, src);
      let frames = 2;
      const again = () => {
        if (document.activeElement === t) placeAt(t, src);
        settle = --frames > 0 ? requestAnimationFrame(again) : 0;
      };
      settle = requestAnimationFrame(again);
    };
    const blur = () => {
      if (!tapping()) hide();
    };
    const scrolled = () => {
      if (!settle) hide();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') hide();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerdown', tap);
    document.addEventListener('mouseleave', hide);
    document.addEventListener('focusin', focus);
    document.addEventListener('focusout', blur);
    document.addEventListener('keydown', esc);
    window.addEventListener('scroll', scrolled, true);
    return () => {
      cancelAnimationFrame(settle);
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerdown', tap);
      document.removeEventListener('mouseleave', hide);
      document.removeEventListener('focusin', focus);
      document.removeEventListener('focusout', blur);
      document.removeEventListener('keydown', esc);
      window.removeEventListener('scroll', scrolled, true);
    };
  }, []);

  return <div id="tip" ref={ref} role="tooltip" aria-hidden="true" />;
}
