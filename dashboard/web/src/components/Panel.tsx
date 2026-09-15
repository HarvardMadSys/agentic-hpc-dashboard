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
import type { FeedStatus, PanelEnvelope } from '../api/types';

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
  return (
    <section className={`panel${span ? ' span2' : ''}`}>
      <header>
        <div>
          <h3>{title}</h3>
          {feedName && (
            <div className="src">
              {feedName}
              {p?._status ? ' · ' + p._status : ''}
            </div>
          )}
        </div>
        <div className="wrapctl" style={{ gap: 6 }}>
          {header}
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
      ) : empty ? (
        <NoRows what={emptyWhat} />
      ) : (
        children
      )}
      {!missing && basis}
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
