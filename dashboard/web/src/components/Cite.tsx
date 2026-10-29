/* A citation in an Ask LLM answer: a numbered chip after the figure it backs,
 * and the answer's sources listed under it (lib/cites.ts says where each goes).
 *
 * Hovering a chip (or focusing it) opens a small preview of what the model
 * was given -- the row or the line as it was in the snapshot when the
 * question was asked, not the page as it is now, so it always matches the
 * answer. A click goes there in this tab (Back returns to the answer); a
 * Ctrl/Cmd-click or a middle click opens it in a new tab, like any link.
 *
 * A ref the snapshot never held is marked as no source: the model wrote it,
 * and it points at nothing.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { AskCite, AskJob, AskView } from '../api/types';
import { citeOrder, go, hrefOf, target } from '../lib/cites';
import type { Target } from '../lib/cites';
import { clock } from '../lib/format';

/** What an answer's citations are read against. */
export interface CiteCtx {
  cites: Record<string, AskCite>;
  view: AskView | null;
  /** when it was asked, epoch seconds: what the preview is as of */
  asked: number | null;
  /** the refs in the order they are first cited: a source's number */
  order: string[];
}

/** An answer's citations, or null for a job asked before there were any. */
export function citeCtx(job: AskJob, texts: (string | null | undefined)[]): CiteCtx | null {
  if (!job.cites) return null;
  return { cites: job.cites, view: job.view ?? null, asked: job.submitted ?? null, order: citeOrder(texts) };
}

const num = (v: number) =>
  Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US', { maximumFractionDigits: 3 });

function scalar(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'number') return num(v);
  if (typeof v === 'string') return v;
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (Array.isArray(v)) return v.map(scalar).join(', ');
  return Object.entries(v as Record<string, unknown>)
    .map(([k, x]) => `${k} ${x != null && typeof x === 'object' ? '{…}' : scalar(x)}`)
    .join(' · ');
}

const SHOWN = 16;

/** The cited data as the model saw it: a line as itself, an object as fields. */
function Seen({ data }: { data: unknown }) {
  if (typeof data === 'string') return <pre className="cite-line">{data}</pre>;
  if (data == null || typeof data !== 'object' || Array.isArray(data)) {
    return <pre className="cite-line">{scalar(data)}</pre>;
  }
  const rows = Object.entries(data as Record<string, unknown>);
  return (
    <table className="cite-data">
      <tbody>
        {rows.slice(0, SHOWN).map(([k, v]) => (
          <tr key={k}>
            <th>{k.replace(/_/g, ' ')}</th>
            <td>{scalar(v)}</td>
          </tr>
        ))}
        {rows.length > SHOWN && (
          <tr>
            <th />
            <td className="nm">… {rows.length - SHOWN} more fields</td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

function Preview({ at, n, t, cite, asked, onEnter, onLeave }: {
  at: HTMLElement;
  n: number;
  t: Target;
  cite: AskCite;
  asked: number | null;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  // placed once its size is known: under the chip, or over it near the bottom
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const a = at.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const left = Math.max(8, Math.min(a.left - 12, innerWidth - r.width - 8));
    const below = a.bottom + 6;
    const top = below + r.height > innerHeight - 8 ? Math.max(8, a.top - r.height - 6) : below;
    setPos({ left, top });
  }, [at]);
  return createPortal(
    <div
      ref={ref}
      className="cite-pop"
      role="tooltip"
      style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: 0 }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <div className="cite-pop-h">
        <span className="cite-n">{n}</span>
        <span className="cite-where">{t.where}</span>
      </div>
      <div className="cite-pop-l">{t.label}</div>
      <div className="cite-pop-k">
        what the model was given{asked ? `, as asked at ${clock(asked)}` : ''}
      </div>
      <Seen data={cite.data} />
      <div className="cite-pop-f">
        {t.arrival
          ? 'click to go there · Ctrl/⌘-click or middle-click for a new tab'
          : 'the page does not show this on its own: it is the question’s own view'}
      </div>
    </div>,
    document.body,
  );
}

/** Any content that opens citation `r` the way a chip does. */
export function CiteLink({ r, ctx, className, children }: {
  r: string;
  ctx: CiteCtx;
  className?: string;
  children: ReactNode;
}) {
  const cite = ctx.cites[r];
  const n = ctx.order.indexOf(r) + 1;
  const [open, setOpen] = useState(false);
  const at = useRef<HTMLAnchorElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const later = (f: () => void, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(f, ms);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  // a preview follows its chip only while nothing scrolls
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && close();
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', esc);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', esc);
    };
  }, [open]);

  if (!cite) {
    return (
      <span
        className={`${className ?? ''} cite-bad`}
        data-tip={`[${r}] is not in what the model was given: it cites nothing, and is not a source`}
      >
        {children}
      </span>
    );
  }
  const t = target(cite, ctx.view);
  const what = `source ${n}: ${t.label}`;
  const onClick = (e: ReactMouseEvent<HTMLAnchorElement>) => {
    if (!t.arrival) {
      e.preventDefault();
      return;
    }
    // the browser's own: a new tab or window
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    setOpen(false);
    go(t.arrival, what);
  };
  return (
    <>
      <a
        ref={at}
        className={className}
        href={hrefOf(t.arrival, what)}
        onClick={onClick}
        onMouseEnter={() => later(() => setOpen(true), 120)}
        onMouseLeave={() => later(() => setOpen(false), 160)}
        onFocus={() => {
          window.clearTimeout(timer.current);
          setOpen(true);
        }}
        onBlur={() => setOpen(false)}
        aria-label={`source ${n}: ${t.where}, ${t.label}`}
      >
        {children}
      </a>
      {open && at.current && (
        <Preview
          at={at.current}
          n={n}
          t={t}
          cite={cite}
          asked={ctx.asked}
          onEnter={() => window.clearTimeout(timer.current)}
          onLeave={() => later(() => setOpen(false), 160)}
        />
      )}
    </>
  );
}

/** The chip after a figure: its source's number. */
export function CiteMark({ r, ctx }: { r: string; ctx: CiteCtx }) {
  return (
    <CiteLink r={r} ctx={ctx} className="cite">
      {ctx.order.indexOf(r) + 1}
    </CiteLink>
  );
}

/** An answer's sources, numbered as its chips are. */
export function Sources({ ctx }: { ctx: CiteCtx }) {
  if (!ctx.order.length) return null;
  return (
    <div className="cite-src">
      <div className="cite-src-h">sources</div>
      <ol>
        {ctx.order.map((r) => {
          const c = ctx.cites[r];
          const t = c ? target(c, ctx.view) : null;
          return (
            <li key={r}>
              <CiteMark r={r} ctx={ctx} />
              {t ? (
                <CiteLink r={r} ctx={ctx} className="cite-src-l">
                  <span className="cite-where">{t.where}</span>
                  {' · '}
                  <b>{t.label}</b>
                </CiteLink>
              ) : (
                <span className="cite-src-l nm">
                  [{r}] is not in what the model was given: no source
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
