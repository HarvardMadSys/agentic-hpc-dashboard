/* "How to read this panel": every explanation a panel's labels hold, in one
 * place a reader can see, scroll and tab through.
 *
 * The page keeps method notes off its panels (README rule 7) and puts what a
 * reader needs to read a number in that number's hover tip: some two thousand
 * across the tabs. A tip is invisible until hovered, needs a tap on a phone,
 * and is hidden from a screen reader. This drawer gathers a panel's tips as
 * they are drawn -- its column headers, labels, terms and controls, not the
 * per-row and per-point ones -- so nothing is written twice and nothing here can
 * drift from the panel: the words are the panel's own.
 */
import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';

interface Note {
  label: string;
  tip: string;
}

/** The explanations under `root`: one per distinct tip, in reading order. */
export function notesOf(root: HTMLElement | null): Note[] {
  if (!root) return [];
  const out: Note[] = [];
  const seen = new Set<string>();
  root.querySelectorAll<HTMLElement>('[data-tip]').forEach((el) => {
    // a chart's point targets and a table's rows carry one tip each: values, not explanations
    if (el.closest('svg') || el.closest('tbody') || el.closest('.about-skip')) return;
    const tip = (el.getAttribute('data-tip') ?? '').trim();
    if (!tip || seen.has(tip)) return;
    seen.add(tip);
    const text = (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    out.push({ label: text.length > 70 ? `${text.slice(0, 68)}…` : text, tip });
  });
  return out;
}

export function AboutButton({ title, root }: { title: string; root: React.RefObject<HTMLElement | null> }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={btn}
        className="iconbtn aboutbtn about-skip"
        aria-haspopup="dialog"
        aria-label={`how to read ${title}`}
        data-tip="how to read this panel: every explanation its labels hold, in one place"
        onClick={() => setOpen(true)}
      >
        <Icon name="info" size={16} />
      </button>
      {open && (
        <AboutDrawer
          title={title}
          notes={notesOf(root.current)}
          onClose={() => {
            setOpen(false);
            btn.current?.focus();
          }}
        />
      )}
    </>
  );
}

function AboutDrawer({ title, notes, onClose }: { title: string; notes: Note[]; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    panel.current?.focus();
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', k, true);
    return () => window.removeEventListener('keydown', k, true);
  }, [onClose]);
  return (
    <div className="about-wrap" onClick={onClose}>
      <div
        className="about"
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="about-h"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="about-head">
          <h3 id="about-h">How to read: {title}</h3>
          <button className="linkbtn" onClick={onClose} data-tip="or press Esc">
            close
          </button>
        </div>
        <div className="about-body">
          {notes.length ? (
            <dl>
              {notes.map((n) => (
                <div key={n.tip} className="about-note">
                  {n.label && <dt>{n.label}</dt>}
                  <dd>{n.tip}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="nm">This panel&rsquo;s labels carry no further explanation.</p>
          )}
          <p className="about-foot nm">
            What each figure computes, from which records and at what cost: <code>docs/figures/README.md</code> in the
            repository.
          </p>
        </div>
      </div>
    </div>
  );
}
