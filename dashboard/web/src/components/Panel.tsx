/* Panel chrome, and the two different kinds of nothing.
 *
 * Rule: zero is not missing.
 *   `_present === false`  -> <EmptyState>: the frame, the title, the feed name,
 *                           and one line naming `_paths` verbatim.
 *   present, but no rows  -> <NoRows>: the word `none`, which looks NOTHING like
 *                           the empty state, because it is a different fact.
 *
 * Rule: data only. A panel carries a title, the feed that fills it, and the
 * chart or table. `notes` / `basis` / `cpu_basis` go in <Basis>, a collapsed
 * affordance -- never body prose.
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import type { FeedStatus, PanelEnvelope, WindowStatus } from '../api/types';
import { AskBox, canAsk, useAskScope } from './Ask';
import type { AskRef } from './Ask';

export function StatusPill({
  status,
  text,
  beat,
  tip,
}: {
  status: FeedStatus | 'live';
  text?: string;
  beat?: boolean;
  tip?: string;
}) {
  const cls = status === 'live' ? (beat ? '' : 'ok') : status;
  return (
    <span className={`pill ${cls}`} data-tip={tip}>
      <i className="ld" />
      {text ?? status}
    </span>
  );
}

export function EmptyState({
  title,
  feed,
  paths,
  status,
  notice,
}: {
  title?: string;
  feed: string;
  paths: string[];
  status?: FeedStatus;
  notice?: string | null;
}) {
  const shown = paths && paths.length ? paths : null;
  return (
    <div className="nofeed">
      <div className="hd">
        {status && <StatusPill status={status} />}
        <span>
          {title ? title + ' · ' : ''}feed <b>{feed || 'unknown'}</b>
        </span>
      </div>
      <p className="paths">
        {shown ? (
          shown.map((p) => <span key={p}>no data from {p}</span>)
        ) : (
          <span>{'no data — the feed reported no configured path'}</span>
        )}
      </p>
      {notice && <p className="paths">{notice}</p>}
    </div>
  );
}

/** Feed IS present, but the selection/window holds no rows. */
export function NoRows({ what }: { what?: string }) {
  return <div className="none">none{what ? ` · ${what}` : ''}</div>;
}

/** The feed is fine and the rows exist -- we have not finished reducing them.
 *
 * A THIRD kind of nothing, and it has to look like neither of the other two.
 * Rendering this as `none` would assert a measurement over a window that has
 * not been read yet; rendering it as an empty state would blame the feed.
 */
export function Building({ w }: { w: WindowStatus }) {
  const p = w.progress || { pct: 0, records: 0, files: 0 };
  const resuming = p.mode === 'resume';
  const merging = p.mode === 'merge';
  return (
    <div className="building">
      <div className="hd">
        <span className="pill">
          <i className="ld" />
          {w.state === 'queued' ? 'queued' : merging ? 'merging' : resuming ? 'catching up' : 'reducing'} {w.label}
        </span>
        <span className="mono">{p.pct}%</span>
      </div>
      <div className="bar">
        <i style={{ width: `${Math.max(2, Math.min(100, p.pct))}%` }} />
      </div>
      <p className="paths">
        {w.state === 'error'
          ? `rebuild failed: ${w.error}`
          : merging
            ? `combining the saved hours of the ${w.label} window`
            : resuming
            ? `resuming the saved ${w.label} reduction, reading only what the collector wrote since it was built — ${p.records.toLocaleString()} records across ${p.files} file(s)`
            : `these panels carry no time index, so the ${w.label} window is being reduced from the rows — ${p.records.toLocaleString()} records across ${p.files} file(s)`}
      </p>
    </div>
  );
}

export interface PanelProps {
  title: string;
  /** anchor for the tab's section list; defaults to a slug of the title */
  id?: string;
  /** The envelope. Omit only for panels that are not feed-backed (e.g. config). */
  p?: PanelEnvelope | null;
  /** Feed name override when there is no envelope. */
  feed?: string;
  span?: boolean;
  live?: boolean;
  /** Present-but-empty: renders <NoRows> instead of children. */
  empty?: boolean;
  emptyWhat?: string;
  header?: ReactNode;
  basis?: ReactNode;
  /** What an "ask" about this panel reads (components/Ask.tsx): the service's
   *  panel data it is drawn from, or one session. No button without it. */
  ask?: AskRef | null;
  children?: ReactNode;
}

/** "Syscall bytes against disk bytes" -> "syscall-bytes-against-disk-bytes" */
export const slug = (s: string) =>
  'sec-' + s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

export function Panel({
  title,
  id,
  p,
  feed,
  span,
  live,
  empty,
  emptyWhat,
  header,
  basis,
  ask,
  children,
}: PanelProps) {
  const scope = useAskScope();
  const [asking, setAsking] = useState(false);
  const askable = !!ask && canAsk(scope.info);
  const feedName = p?._feed ?? feed ?? '';
  const missing = p != null && p._present === false;
  // Feed-absent outranks window-building: if the collector wrote nothing, no
  // amount of reducing will produce rows, and naming the path is the useful
  // answer. Only when the feed is fine does "still reading" become the story.
  const building = !missing && p?._window && p._window.state !== 'ready'
    ? p._window : null;
  // A FIRST build has nothing to show, so the body says so. A drift RE-build
  // still has the previous reduction, which is valid and older -- it keeps the
  // numbers and wears a pill, rather than blanking a panel that was fine.
  const blank = building != null && !building.has_payload;
  const askBtn = askable && !missing && !blank ? (
    <button
      className={`btn askbtn${asking ? ' on' : ''}`}
      aria-pressed={asking}
      onClick={() => setAsking(!asking)}
      data-tip={`ask ${scope.info?.agent ?? 'the LLM'} about ${
        Array.isArray(ask) ? 'this panel: it is sent this panel’s data and nothing else' : 'this session: it is sent this session’s process tree and nothing else'
      }`}
    >
      {Array.isArray(ask) ? 'ask' : 'ask about this session'}
    </button>
  ) : null;
  return (
    <section className={`panel${span ? ' span2' : ''}`} id={id ?? slug(title)} data-title={title}>
      <header>
        <div>
          {/* No "process tracer · ok" line: which collector fed a panel, and
              whether it is healthy, is the Feeds tab's business. A panel whose
              feed is absent still says so in its body (EmptyState). */}
          <h3>{title}</h3>
        </div>
        <div className="wrapctl" style={{ gap: 6 }}>
          {/* a panel's "ask" sits at the end; a session's leads, so the
              inspector's "close" stays last */}
          {!Array.isArray(ask) && askBtn}
          {header}
          {building && !blank && (
            <span
              className="pill"
              data-tip={`showing the previous ${building.label} reduction while a fresh one is built — ${building.progress.pct}% read`}
            >
              <i className="ld" />
              rebuilding {building.progress.pct}%
            </span>
          )}
          {live && !missing && <StatusPill status="live" text="live" beat />}
          {Array.isArray(ask) && askBtn}
        </div>
      </header>
      {askable && asking && !missing && !blank && (
        <AskBox
          what={ask!}
          title={title}
          threadKey={Array.isArray(ask) ? `${id ?? slug(title)}` : `session:${ask!.session}`}
          onClose={() => setAsking(false)}
        />
      )}
      {p?._waiting ? (
        <div className="none">waiting for the dashboard service…</div>
      ) : missing ? (
        <EmptyState
          feed={feedName}
          paths={p!._paths || []}
          status={p!._status}
          notice={p!._notice}
        />
      ) : blank ? (
        <Building w={building!} />
      ) : empty ? (
        <NoRows what={emptyWhat} />
      ) : (
        children
      )}
      {/* A basis handed over as bare text is a paragraph of method notes; the
          reader asked for none of those under a panel. Only <Basis>, which is
          switched off, may render here. */}
      {!missing && !blank && typeof basis !== 'string' && basis}
    </section>
  );
}

/** The collapsed basis affordance: notes, cpu_basis, scope, dedup rules. */
/* Basis disclosures are switched off: the reader found them noise, not help.
 * Anything a reader needs to read a number right belongs in its tooltip or its
 * pill, beside the number, not in a collapsed list under the panel. The prop and
 * the call sites stay for now so this is one switch, not a dozen file edits. */
const SHOW_BASIS = false;

export function Basis({ items }: { items: [string, unknown][] }) {
  if (!SHOW_BASIS) return null;
  const rows = items.filter(([, v]) => v != null && v !== '' && !(Array.isArray(v) && !v.length));
  if (!rows.length) return null;
  return (
    <details className="basis">
      <summary>basis</summary>
      <div className="body">
        {rows.map(([k, v]) => (
          <div key={k}>
            <b>{k}</b> {typeof v === 'object' ? JSON.stringify(v) : String(v)}
          </div>
        ))}
      </div>
    </details>
  );
}

/** The table behind a chart, folded away until asked for: open, a panel's
 * tables push its chart out of line with the chart beside it. */
export function More({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <details className="more">
      <summary>{label}</summary>
      {children}
    </details>
  );
}

export function Legend({ items }: { items: { k: string; c: string; dashed?: boolean; solid?: boolean }[] }) {
  if (!items.length) return null;
  return (
    <div className="legend">
      {items.map((i) => (
        <span key={i.k + i.c}>
          {i.dashed
            ? <i className="dash" style={{ borderTopColor: i.c }} />
            : <i className={i.solid ? 'solid' : undefined} style={{ background: i.c }} />}
          {i.k}
        </span>
      ))}
    </div>
  );
}
