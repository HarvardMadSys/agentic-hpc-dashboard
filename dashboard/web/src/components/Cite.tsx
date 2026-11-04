/* A citation in an Ask LLM answer: a numbered chip after the figure it backs,
 * and the answer's sources listed under it (lib/cites.ts says where each goes).
 *
 * Hovering a chip (or focusing it) opens a small preview of what the model
 * was given -- the row or the line as it was in the snapshot when the
 * question was asked, not the page as it is now, so it always matches the
 * answer. The figures the answer quotes beside the chip lead it, lit: a
 * class's whole summary is dozens of figures, and the reader came for one.
 * Every figure is one line, by its path. A click goes there in this tab (Back
 * returns to the answer); a Ctrl/Cmd-click or a middle click opens it in a new
 * tab, like any link.
 *
 * A ref the snapshot never held is marked as no source: the model wrote it,
 * and it points at nothing.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent, ReactNode, RefObject } from 'react';
import { createPortal } from 'react-dom';
import type { AskCite, AskJob, AskView } from '../api/types';
import { CITE_RE, citeOrder, go, hrefOf, refsOf, target } from '../lib/cites';
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
  /** the text the chips stand in: what each one backs is read from it */
  texts: string[];
}

/** An answer's citations, or null for a job asked before there were any. */
export function citeCtx(job: AskJob, texts: (string | null | undefined)[]): CiteCtx | null {
  if (!job.cites) return null;
  return {
    cites: job.cites,
    view: job.view ?? null,
    asked: job.submitted ?? null,
    order: citeOrder(texts),
    texts: texts.filter((t): t is string => !!t),
  };
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
    .map(([k, x]) => `${k} ${scalar(x)}`)
    .join(' · ');
}

/* ------------------------------------------------- the figures it backs */

/** A number as the answer wrote it, and how far a figure may be from it and
 *  still be the one it quotes: what its rounding allows. */
interface Quoted {
  v: number;
  tol: number;
}

const SCALE: Record<string, number> = {
  k: 1e3, K: 1e3, thousand: 1e3, M: 1e6, million: 1e6, B: 1e9, billion: 1e9,
};
const NUMBER = /(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*(k|K|M|B|thousand|million|billion)\b)?/g;

/** The numbers of every sentence that cites `r`: what its chip backs. A
 *  sentence runs to a full stop or a line's end, not a semicolon: "8,797.32
 *  CPU-seconds; 405.018 seconds waiting [2]" quotes both. Times and dates are
 *  not figures, and a ref's own digits are not either. */
function quotedBy(texts: string[], r: string): Quoted[] {
  const out: Quoted[] = [];
  for (const text of texts) {
    for (const s of text.split(/(?<=[.!?])\s+|\n+/)) {
      if (!Array.from(s.matchAll(CITE_RE)).some((m) => refsOf(m[1]).includes(r))) continue;
      const bare = s
        .replace(CITE_RE, ' ')
        .replace(/\d{4}-\d{2}-\d{2}/g, ' ')
        .replace(/\d{1,2}:\d{2}(?::\d{2})?/g, ' ');
      for (const m of bare.matchAll(NUMBER)) {
        const dec = m[2] ?? '';
        const scale = m[3] ? SCALE[m[3]] : 1;
        out.push({
          v: parseFloat(m[1].replace(/,/g, '') + (dec ? '.' + dec : '')) * scale,
          tol: 0.5 * 10 ** -dec.length * scale + 1e-9,
        });
      }
    }
  }
  return out;
}

/** One figure of the cited data: its path and value, and whether the answer
 *  quotes it. */
interface Leaf {
  path: string;
  text: string;
  v: number | null;
  quoted: boolean;
}

/** The cited data as one figure a line, in order. A part the service only
 *  counted (`{7 fields}`) is not a figure and is only counted here too. */
function leavesOf(data: unknown): { list: Leaf[]; summed: number } {
  const list: Leaf[] = [];
  let summed = 0;
  const walk = (v: unknown, path: string[]) => {
    if (v != null && typeof v === 'object' && !Array.isArray(v)) {
      for (const [k, x] of Object.entries(v as Record<string, unknown>)) walk(x, [...path, k]);
      return;
    }
    if (typeof v === 'string' && /^\{\d+ fields\}$/.test(v)) {
      summed++;
      return;
    }
    list.push({
      path: path.map((k) => k.replace(/_/g, ' ')).join(' › ') || 'value',
      text: scalar(v),
      v: typeof v === 'number' ? v : null,
      quoted: false,
    });
  };
  walk(data, []);
  return { list, summed };
}

/** Light the figures the answer quotes. A number that three figures or more
 *  could be (a count of 1) says nothing about which, and lights none. */
function markQuoted(list: Leaf[], quoted: Quoted[]) {
  for (const q of quoted) {
    const hits = list.filter((l) => l.v != null && Math.abs(l.v - q.v) <= q.tol);
    if (hits.length > 0 && hits.length < 3) for (const h of hits) h.quoted = true;
  }
}

const SHOWN = 12;

/** The cited data as the model saw it: a line as itself; an object as its
 *  figures, the ones the answer quotes first. */
function Seen({ data, quoted }: { data: unknown; quoted: Quoted[] }) {
  if (typeof data === 'string') return <pre className="cite-line">{data}</pre>;
  if (data == null || typeof data !== 'object' || Array.isArray(data)) {
    return <pre className="cite-line">{scalar(data)}</pre>;
  }
  const { list, summed } = leavesOf(data);
  markQuoted(list, quoted);
  const lit = list.filter((l) => l.quoted);
  const shown = [...lit, ...list.filter((l) => !l.quoted)].slice(0, Math.max(SHOWN, lit.length));
  const more = list.length - shown.length + summed;
  return (
    <>
      {lit.length > 0 && <div className="cite-lit-k">lit: what the answer quotes from it</div>}
      <table className="cite-data">
        <tbody>
          {shown.map((l, i) => (
            <tr key={i} className={l.quoted ? 'q' : undefined}>
              <th>{l.path}</th>
              <td>{l.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {more > 0 && (
        <div className="cite-more">
          and {more} more figure{more === 1 ? '' : 's'} the model was given
        </div>
      )}
    </>
  );
}

/** The panel an agent's part was read from, beside the trajectory a click
 *  opens: a link of its own, filtered to it. */
function AlsoLink({ also, what, onGo }: { also: NonNullable<Target['also']>; what: string; onGo: () => void }) {
  return (
    <a
      className="cite-also"
      href={hrefOf(also.arrival, what)}
      onClick={(e) => {
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        onGo();
        go(also.arrival, what);
      }}
    >
      or see it on {also.where}
      {also.narrows ? `, filtered to ${also.narrows}` : ''} →
    </a>
  );
}

function Preview({ at, popRef, n, t, cite, quoted, asked, what, onEnter, onLeave, onGo }: {
  at: HTMLElement;
  popRef: RefObject<HTMLDivElement | null>;
  n: number;
  t: Target;
  cite: AskCite;
  quoted: Quoted[];
  asked: number | null;
  what: string;
  onEnter: () => void;
  onLeave: () => void;
  onGo: () => void;
}) {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  // placed once its size is known: under the chip, or over it near the bottom
  useLayoutEffect(() => {
    const el = popRef.current;
    if (!el) return;
    const a = at.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const left = Math.max(8, Math.min(a.left - 12, innerWidth - r.width - 8));
    const below = a.bottom + 6;
    const top = below + r.height > innerHeight - 8 ? Math.max(8, a.top - r.height - 6) : below;
    setPos({ left, top });
  }, [at, popRef]);
  return createPortal(
    // focusable, so pressing on it (its scroll bar, a figure to copy) keeps it open
    <div
      ref={popRef}
      className="cite-pop"
      role="tooltip"
      tabIndex={-1}
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
      <Seen data={cite.data} quoted={quoted} />
      <div className="cite-pop-f">
        {t.arrival
          ? `click to ${t.also ? 'open its trajectory' : 'go there'}${
              t.narrows ? `, the page filtered to ${t.narrows}` : ''
            } · Ctrl/⌘-click or middle-click for a new tab`
          : 'the page does not show this on its own: it is the question’s own view'}
        {t.also && (
          <div>
            <AlsoLink also={t.also} what={what} onGo={onGo} />
          </div>
        )}
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
  const pop = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const later = (f: () => void, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(f, ms);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const inPop = (x: EventTarget | null) => !!pop.current && x instanceof Node && pop.current.contains(x);
  // a preview follows its chip only while the page does not scroll; its own
  // scrolling is the reader reading it
  useEffect(() => {
    if (!open) return;
    const close = (e: Event) => {
      if (!inPop(e.target)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
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
        onBlur={(e) => {
          if (!inPop(e.relatedTarget)) setOpen(false);
        }}
        aria-label={`source ${n}: ${t.where}, ${t.label}`}
      >
        {children}
      </a>
      {open && at.current && (
        <Preview
          at={at.current}
          popRef={pop}
          n={n}
          t={t}
          cite={cite}
          quoted={quotedBy(ctx.texts, r)}
          asked={ctx.asked}
          what={what}
          onEnter={() => window.clearTimeout(timer.current)}
          onLeave={() => later(() => setOpen(false), 160)}
          onGo={() => setOpen(false)}
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
