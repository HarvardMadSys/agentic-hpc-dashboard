/* One document-level tooltip, driven by `data-tip` anywhere in the tree.
 *
 * Ported from the archived template: it works uniformly over HTML and SVG (SVG
 * elements cannot carry a `title` attribute usefully), so a chart rect and a
 * table cell get the same affordance with no per-element wiring.
 *
 * A mouse shows it on hover, beside the pointer. A finger has no hover, so on a
 * touch screen a tap shows it instead -- above the finger, not under it -- and
 * it stays until the next tap or a scroll. There it is the only way to read a
 * bar's value, or what a "?" explains. A tap on a control (a button, a link, a
 * field) is taken as working it, and shows nothing: its hint is about using it
 * with a mouse, and would only sit over what it opened.
 */
import { useEffect, useRef } from 'react';

const CONTROL = 'button, a[href], input, select, textarea, summary, label, [role="button"]';

export function TooltipLayer() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const hide = () => {
      el.style.opacity = '0';
    };
    /** Shows the tip of whatever `target` is in; false when nothing there has one. */
    const fill = (target: EventTarget | null): boolean => {
      const t = target as Element | null;
      const src = t && 'closest' in t ? (t.closest('[data-tip]') as HTMLElement | null) : null;
      const txt = src?.dataset.tip;
      if (!src || !txt) return false;
      el.textContent = txt;
      // a whole command line reads better in a wider box than a sentence does
      el.classList.toggle('wide', src.hasAttribute('data-tip-wide'));
      el.style.opacity = '1';
      return true;
    };
    const place = (x: number, y: number) => {
      el.style.left = x + 'px';
      el.style.top = y + 'px';
    };
    // A mouse or a pen hovering: beside the pointer, flipped at the window's edges.
    const move = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return;
      if (!fill(e.target)) return hide();
      const r = el.getBoundingClientRect();
      let x = e.clientX + 13;
      let y = e.clientY + 13;
      if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 10;
      if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 10;
      place(x, y);
    };
    // A finger: centred over where it touched and clear of it, below only when
    // there is no room above.
    const tap = (e: PointerEvent) => {
      if (e.pointerType !== 'touch') return;
      const t = e.target as Element | null;
      if ((t && 'closest' in t && t.closest(CONTROL)) || !fill(t)) return hide();
      const r = el.getBoundingClientRect();
      const x = Math.max(8, Math.min(e.clientX - r.width / 2, innerWidth - r.width - 8));
      const above = e.clientY - r.height - 28;
      place(x, above >= 8 ? above : e.clientY + 28);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerdown', tap);
    document.addEventListener('mouseleave', hide);
    window.addEventListener('scroll', hide, true);
    return () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerdown', tap);
      document.removeEventListener('mouseleave', hide);
      window.removeEventListener('scroll', hide, true);
    };
  }, []);

  return <div id="tip" ref={ref} role="tooltip" aria-hidden="true" />;
}
