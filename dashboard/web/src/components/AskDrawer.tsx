/* Ask LLM, beside the open tab: a drawer over the right of the page, so the
 * window being asked about stays on screen while the answer comes. Opened from
 * the head's Ask button (App.tsx) and only where a site turned Ask on. Escape
 * or the scrim closes it; focus returns to where it was. */
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

export function AskDrawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const back = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !(e.target instanceof HTMLTextAreaElement && e.target.value)) onClose();
    };
    window.addEventListener('keydown', k);
    return () => {
      window.removeEventListener('keydown', k);
      if (back && document.contains(back)) back.focus({ preventScroll: true });
    };
  }, [onClose]);
  return (
    <div className="askdrawer-wrap" onClick={onClose}>
      <div
        className="askdrawer"
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label="Ask LLM about the window on screen"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="askdrawer-head">
          <h3>Ask LLM</h3>
          <button className="linkbtn" onClick={onClose} data-tip="or press Esc">
            close
          </button>
        </div>
        <div className="askdrawer-body">{children}</div>
      </div>
    </div>
  );
}
