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
import type { ReactNode } from 'react';
import type { FeedStatus, PanelEnvelope, WindowStatus } from '../api/types';
import { feedLabel } from '../lib/feeds';

export function StatusPill({
  status,
  text,
  beat,
}: {
  status: FeedStatus | 'live';
  text?: string;
  beat?: boolean;
}) {
  const cls = status === 'live' ? (beat ? '' : 'ok') : status;
  return (
    <span className={`pill ${cls}`}>
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
  return (
    <div className="building">
      <div className="hd">
        <span className="pill">
          <i className="ld" />
          {w.state === 'queued' ? 'queued' : 'reducing'} {w.label}
        </span>
        <span className="mono">{p.pct}%</span>
      </div>
      <div className="bar">
        <i style={{ width: `${Math.max(2, Math.min(100, p.pct))}%` }} />
      </div>
      <p className="paths">
        {w.state === 'error'
          ? `rebuild failed: ${w.error}`
          : `these panels carry no time index, so the ${w.label} window is being reduced from the rows — ${p.records.toLocaleString()} records across ${p.files} file(s)`}
      </p>
    </div>
  );
}

export interface PanelProps {
  title: string;
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
  children?: ReactNode;
}

export function Panel({
  title,
  p,
  feed,
  span,
  live,
  empty,
  emptyWhat,
  header,
  basis,
  children,
}: PanelProps) {
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
  return (
    <section className={`panel${span ? ' span2' : ''}`}>
      <header>
        <div>
          <h3>{title}</h3>
          {feedName && (
            <div className="src" data-tip={`feed key: ${feedName}`}>
              {feedLabel(feedName)}
              {p?._status ? ' · ' + p._status : ''}
            </div>
          )}
        </div>
        <div className="wrapctl" style={{ gap: 6 }}>
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
        </div>
      </header>
      {missing ? (
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
      {!missing && !blank && basis}
    </section>
  );
}

/** The collapsed basis affordance: notes, cpu_basis, scope, dedup rules. */
export function Basis({ items }: { items: [string, unknown][] }) {
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

export function Legend({ items }: { items: { k: string; c: string }[] }) {
  if (!items.length) return null;
  return (
    <div className="legend">
      {items.map((i) => (
        <span key={i.k + i.c}>
          <i style={{ background: i.c }} />
          {i.k}
        </span>
      ))}
    </div>
  );
}
