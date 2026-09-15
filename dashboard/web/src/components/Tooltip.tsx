/* One document-level tooltip, driven by `data-tip` anywhere in the tree.
 *
 * Ported from the archived template: it works uniformly over HTML and SVG (SVG
 * elements cannot carry a `title` attribute usefully), so a chart rect and a
 * table cell get the same affordance with no per-element wiring.
 */
import { useEffect, useRef } from 'react';

export function TooltipLayer() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const move = (e: MouseEvent) => {
      const t = e.target as Element | null;
      const src = t && 'closest' in t ? t.closest('[data-tip]') : null;
      if (!src) {
        el.style.opacity = '0';
        return;
      }
      const txt = (src as HTMLElement).dataset.tip;
      if (!txt) {
        el.style.opacity = '0';
        return;
      }
      el.textContent = txt;
      el.style.opacity = '1';
      const r = el.getBoundingClientRect();
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
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseleave', leave);
    window.addEventListener('scroll', leave, true);
    return () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseleave', leave);
      window.removeEventListener('scroll', leave, true);
    };
  }, []);

  return <div id="tip" ref={ref} role="tooltip" aria-hidden="true" />;
}
