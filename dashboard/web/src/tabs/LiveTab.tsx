/* Live tier.
 *
 * Websocket-driven, with nothing invented between frames. The rate chart plots
 * `null` bins as gaps: 5 minutes in which the collector wrote no record are a hole
 * in the line, because a 0 there would be a claim that the cluster was idle.
 */
import { Fragment, memo, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { Cls, FeedReport, FeedsResponse, LiveEvent, LiveHost, LiveResponse, Trajectories } from '../api/types';
import type { LiveState } from '../api/useLive';
import { Fluid, Lines } from '../charts/primitives';
import type { LinePt, LineSeries } from '../charts/primitives';
import { clockLabel, fullClock, tickTarget, timeTicks } from '../charts/scale';
import { Basis, EmptyState, Legend, NoRows, Panel } from '../components/Panel';
import { AGENT_TYPE_LBL, CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { Segmented } from '../components/ClassFilter';
import type { Who } from '../lib/filters';
import { DASH, dur, fint, fmt } from '../lib/format';
import { pcol } from '../lib/purposes';
import { ActiveUsers } from './LiveUsers';
import { ByUser, distinct } from '../components/ByUser';
import { WindowPlates } from '../components/WindowPlates';
import { DayByDay } from './DayByDay';
import { Attention } from './Attention';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { useNodeWords } from '../lib/nodes';
import type { Panels } from '../lib/panels';

/* ------------------------------------------------------------- helpers */

/* `depth` is the collector's count of process generations between a process
 * and its nearest agent ancestor: its depth in that agent's process tree. */
const DEPTH_TIP =
  "depth in the agent's process tree:\n\n" +
  '0 = the agent process itself\n1 = started directly by the agent (e.g. a tool-call shell)\n' +
  '2 = started by one of those (e.g. the command that shell ran)\nand so on down the tree\n\n' +
  "— = no agent above it: a human's own process";

function depthLabel(d: number | null | undefined): string {
  if (d == null) return DASH;
  return d === 0 ? '0 (agent)' : String(d);
}

/** No live frame yet: one empty list, so a memo keyed on the bins holds. */
const NO_BINS: LiveResponse['rate'] = [];

/** The rate chart's bucket: the service's per-minute bins summed into this. */
const RATE_BIN_S = 300;
const RATE_LBL = '5 min';

/** The newest bucket that actually carried a value. */
function latestPt(pts: LinePt[]): number | null {
  for (let i = pts.length - 1; i >= 0; i--) {
    const v = pts[i].y;
    if (v != null) return v;
  }
  return null;
}

/* `roots` is the count of live agent and IDE session trees on the host: the
 * word a reader meets is "sessions" (roadmap note 14). */
const HOST_COLS: [key: string, lbl: string, kind: 'int' | 'num' | 'pct', gloss?: string][] = [
  ['roots', 'sessions', 'int', 'sessions_now'],
  ['procs', 'running', 'int', 'running'],
  ['events', 'exited', 'int', 'exited'],
  ['rss_gb', 'memory GB', 'num', 'rss'],
  ['cpu_pct', 'CPU', 'pct'],
  ['load_1', 'load₁', 'num'],
  ['d_state', 'D-state', 'int', 'dstate'],
  ['users', 'users', 'int', 'user'],
  ['tcp_open_ext', 'ext TCP', 'int', 'exttcp'],
];

function hostCell(h: LiveHost, key: string, kind: string) {
  const v = h[key];
  if (typeof v !== 'number') return DASH;
  if (kind === 'pct') return fmt(v) + '%';
  if (kind === 'num') return fmt(v);
  if (key === 'd_state' && v > 0) return <span style={{ color: 'var(--crit)' }}>{fint(v)}</span>;
  return fint(v);
}

/** Per-class split for one host, if the payload carries one. */
// editors, which the IDE class holds; every other type is an agent's
const EDITOR_TYPES = new Set(['vscode', 'cursor', 'windsurf']);

/** A host's running processes split by class, or by agent and editor type,
 * read right after their total (the column before it). */
function HostBreakdown({ h, cls, by }: { h: LiveHost; cls: Cls[]; by: 'class' | 'type' }) {
  const sep = <span className="sep">{' · '}</span>;
  if (by === 'class') {
    const bc = hostByClass(h);
    if (!bc) return <span className="nm">not reported per class</span>;
    return (
      <span className="rle">
        {cls.map((c, i) => (
          <span key={c} data-tip={`${CLS_LBL[c] ?? c}: processes running now`}>
            {i ? sep : null}
            <i className="dot" style={{ background: CLS_COL[c], marginRight: 4 }} />
            <b>{c in bc ? fint(bc[c]) : DASH}</b>
          </span>
        ))}
      </span>
    );
  }
  const bt = (h.by_type ?? null) as Record<string, number> | null;
  if (!bt || !Object.keys(bt).length) return <span className="nm">not reported per type</span>;
  const total = typeof h.procs === 'number' ? h.procs : null;
  const typed = Object.values(bt).reduce((a, n) => a + n, 0);
  return (
    <span className="rle">
      {Object.entries(bt)
        .sort((a, b) => b[1] - a[1])
        .map(([t, n], i) => (
          <span key={t}>
            {i ? sep : null}
            <i className="dot" style={{ background: CLS_COL[EDITOR_TYPES.has(t) ? 'ide' : 'agent'], marginRight: 4 }} />
            {AGENT_TYPE_LBL[t] ?? t} <b>{fint(n)}</b>
          </span>
        ))}
      {total != null && total > typed && (
        <span data-tip="processes with no agent or IDE above them: people’s own shells, and the rest">
          {sep}
          <span className="nm">no type</span> <b>{fint(total - typed)}</b>
        </span>
      )}
    </span>
  );
}

function hostByClass(h: LiveHost): Partial<Record<string, number>> | null {
  const b = (h.by_class ?? h.classes ?? h.events_by_class) as unknown;
  if (b && typeof b === 'object' && !Array.isArray(b)) {
    const out: Record<string, number> = {};
    for (const c of CLS) {
      const v = (b as Record<string, unknown>)[c];
      if (typeof v === 'number') out[c] = v;
    }
    if (Object.keys(out).length) return out;
  }
  const out: Record<string, number> = {};
  for (const c of CLS) {
    const v = h[`roots_${c.replace('-', '_')}`] ?? h[`events_${c.replace('-', '_')}`];
    if (typeof v === 'number') out[c] = v;
  }
  return Object.keys(out).length ? out : null;
}

/* --------------------------------------------------------------- the tab */

// It reads the live frame, so it is drawn with each one; it is not drawn for renders of App that change nothing it shows.
export const LiveTab = memo(function LiveTab({
  live,
  P,
  traj,
  cls,
  who,
  feeds,
  goto,
  openTraj,
  openUserTraj,
  userCols,
  whoUses,
}: {
  live: LiveState;
  /** the window's reduced panels: the plates, Needs attention, Who uses what */
  P: Panels;
  traj: Trajectories | null;
  cls: Cls[];
  /** The page's user and agent-type filter. The live tier is one shared rolling
   *  count, not rebuilt per filter, so only rows that name a user narrow. */
  who?: Who;
  feeds: FeedsResponse | null;
  goto: (tab: string) => void;
  /** open a session, or a user's sessions, on the Sessions tab */
  openTraj: (sid: string) => void;
  openUserTraj: (user: string) => void;
  /** the service's default Active users columns (`ui.user_columns`) */
  userCols?: string[] | null;
  /** "Who uses what", the old Overview tab's table: each class's share of the
   *  window in every unit, drawn under the rate chart (App.tsx) */
  whoUses?: ReactNode;
}) {
  const nodes = useNodeWords();
  const L = live.live;
  const rate = L?.rate ?? NO_BINS;

  /* The live feed report, for the staleness line on the rate panel. */
  const liveFeed: FeedReport | null = useMemo(() => {
    if (!feeds) return null;
    const vals = Object.values(feeds);
    return (
      vals.find((f) => f.tier === 'process' && f.required) ??
      vals.find((f) => f.cadence === 'event') ??
      vals.find((f) => f.required) ??
      vals[0] ??
      null
    );
  }, [feeds]);

  // Every class's points in one pass over the bins -- a week is 10,080 of them
  // -- made again only when the frame or the classes shown change. The service
  // bins by the minute; the chart sums them into RATE_BIN_S buckets on local
  // wall-clock multiples, so a 24 h line is 288 points, not 1,440 jittery ones.
  // A bucket whose minutes are all holes stays a hole; in one with any record a
  // class's holes add nothing. Only whole buckets are drawn: the newest row is
  // the minute still filling, and the oldest bucket can start before the window,
  // so either edge would always read low.
  const series: LineSeries[] = useMemo(() => {
    const pts: LinePt[][] = cls.map(() => []);
    const binS = L?.window?.bin_s ?? 60;
    const per = Math.max(1, Math.round(RATE_BIN_S / binS));
    const tz = new Date().getTimezoneOffset() * 60;
    const lastE = rate.length ? Number(rate[rate.length - 1].epoch) : NaN;
    let bx = NaN;
    let seen = 0;
    let acc: (number | null)[] = cls.map(() => null);
    const flush = () => {
      // x is the bucket's epoch, not its index: the window slides every minute,
      // and a zoom held in indices would drift off the times picked.
      if (Number.isFinite(bx) && seen === per && bx + per * binS <= lastE) {
        for (let k = 0; k < cls.length; k++) pts[k].push({ x: bx, y: acc[k] });
      }
    };
    for (const r of rate) {
      const x = Number(r.epoch);
      if (!Number.isFinite(x)) continue;
      const b = Math.floor((x - tz) / (per * binS)) * per * binS + tz;
      if (b !== bx) {
        flush();
        bx = b;
        seen = 0;
        acc = cls.map(() => null);
      }
      seen++;
      for (let k = 0; k < cls.length; k++) {
        const v = r[cls[k]];
        if (typeof v === 'number') acc[k] = (acc[k] ?? 0) + v;
      }
    }
    flush();
    return cls.map((c, k) => {
      const last = c === 'agent' ? latestPt(pts[k]) : null;
      return {
        lbl: CLS_LBL[c],
        c: CLS_COL[c],
        area: c === 'agent',
        mark: c === 'agent' ? (last != null ? fint(last) + '/' + RATE_LBL : null) : null,
        pts: pts[k],
      };
    });
  }, [rate, cls, L?.window?.bin_s]);

  /* X ticks that stay legible at ANY window, and at any zoom into it: a round
   * interval that lands ~10 ticks across what is on screen, on LOCAL wall-clock
   * multiples of it (`timeTicks`), so a label reads `14:00` and not `14:07`.
   * Fewer where the chart is drawn narrow (`tickTarget`), so none overprint. */
  const xticks = (W: number) => (a: number, b: number) => timeTicks(a, b, tickTarget(W, 10));

  // One row per account, from the live reducer's roster: a person who runs
  // an agent, a shell, and an IDE is one user, not three.
  const U = L?.users;

  // sortable by any column (roadmap note 27); by host until then
  const hostSort = useSort<string>('host', 'asc');
  const hostCols = HOST_COLS.filter((c) => (L?.hosts ?? []).some((h) => typeof h[c[0]] === 'number'));
  const hosts = hostSort.sort(L?.hosts ?? [], (h, k) =>
    k === 'host' ? h.host : typeof h[k] === 'number' ? (h[k] as number) : null);
  // what a host's running processes are, read right after their count
  const [hostBy, setHostBy] = useState<'class' | 'type'>('class');

  const envelope = L
    ? null
    : ({
        _feed: liveFeed?.name ?? 'live',
        _status: (liveFeed?.status ?? 'missing') as FeedReport['status'],
        _present: false,
        _paths: liveFeed?.resolved?.length ? liveFeed.resolved : (liveFeed?.configured ?? []),
        _notice: liveFeed?.notice ?? 'nothing has arrived on the live stream yet',
      } as const);

  // what the service has flagged, first: it reads the window's panels and the
  // feeds, not the live tier, so it is here with the live tier missing too
  const attention = (
    <>
      <div className="grid wide">
        <Attention P={P} cls={cls} feeds={feeds} />
      </div>
      <div className="grid-gap" />
    </>
  );

  if (envelope) {
    return (
      <>
        {attention}
        <div className="grid wide">
          <Panel title="Live stream" span feed={envelope._feed}>
            <EmptyState
              feed={envelope._feed}
              paths={[...envelope._paths]}
              status={envelope._status}
              notice={envelope._notice}
            />
          </Panel>
        </div>
        {/* "Who uses what" is read from the window's panels, not the live tier:
            with the live tier missing it still has its numbers, as it did on
            the Overview tab it came from */}
        {whoUses && (
          <>
            <div className="grid-gap" />
            {whoUses}
          </>
        )}
      </>
    );
  }

  const windowMin = L?.window?.minutes ?? rate.length;
  // The service's own name for the span, so the chart title, the plate and the
  // picker cannot disagree about what is on screen.
  const windowLbl = L?.window?.label ?? `${fint(windowMin)} min`;

  return (
    <>
      {attention}
      {/* One row of plates, labeled with the span it counts. The minute and
          the last 10 minutes are the rate chart's newest point, Active users
          and the login node table. */}
      <div className="rowlabel">
        {windowLbl === 'in window' ? 'the window' : <>last <b>{windowLbl}</b></>}
      </div>
      <WindowPlates P={P} cls={cls} traj={traj} />

      <div className="grid wide">
        <Panel
          title={`Processes exited per 5 minutes · last ${windowLbl}`}
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!rate.length}
          emptyWhat="the live window holds no bins"
          basis={
            <Basis
              items={[
                ['window', L?.window ? JSON.stringify(L.window) : null],
                ['transport', live.transport],
                ['bins', 'the service\'s per-minute bins summed into 5-minute buckets on the clock; ' +
                  'the bucket still filling, and a partial one at the window\'s start, are not drawn'],
                ['gaps', 'a bucket with no record at all is null and is drawn as a hole in the line; ' +
                  'in a bucket with records, a class with none is 0'],
                ['backfill', L?.backfill ? JSON.stringify(L.backfill) : null],
              ]}
            />
          }
        >
          <Fluid natural={1060}>
            {(W) => (
              <Lines
                series={series}
                W={W}
                H={230}
                ymin={0}
                xticks={xticks(W)}
                xfmt={clockLabel}
                xtip={fullClock}
                minSpan={3 * Math.max(RATE_BIN_S, L?.window?.bin_s ?? 60)}
                ylabel={`exited / ${RATE_LBL}`}
                xlabel={`last ${windowLbl}`}
                xname="at"
                yname={`exited/${RATE_LBL}`}
              />
            )}
          </Fluid>
          <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        {/* what happened, then who did it: the window's shares, in every unit */}
        {whoUses && <div className="span2">{whoUses}</div>}

        {/* the same question over days: every day the service holds */}
        <DayByDay P={P} cls={cls} who={who ?? null} />

        <ActiveUsers
          U={U}
          cls={cls}
          windowMin={L?.window?.requested_minutes ?? windowMin ?? null}
          windowLbl={`last ${windowLbl}`}
          P={P}
          defaultCols={userCols}
          pageUsers={who?.users}
          pageTypes={who?.atypes}
          openTraj={openTraj}
          openUserTraj={openUserTraj}
          goto={goto}
          events={L?.events}
          tailDepth={L?.events_tail_depth ?? null}
          hosts={(L?.hosts ?? []).map((h) => h.host)}
        />

        <Panel
          title={`${nodes.Many} right now`}
          ask={[{ panel: 'live', keys: ['hosts', 'now'] }]}
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!hosts.length}
          emptyWhat={`no ${nodes.one} reported in the window`}
          header={
            <Segmented
              label="running, by"
              value={hostBy}
              onChange={setHostBy}
              options={[
                { k: 'class', lbl: 'class', tip: 'running processes split by class: agent, human, IDE' },
                { k: 'type', lbl: 'agent type', tip: 'running processes split by the agent or IDE they belong to; a person’s own shell has no type' },
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <SortTh s={hostSort} k="host" first="asc" className="pin">{nodes.one}</SortTh>
                  {hostCols.map((c) => (
                    <Fragment key={c[0]}>
                      <SortTh s={hostSort} k={c[0]} num gloss={c[3]}>
                        {c[1]}
                      </SortTh>
                      {c[0] === 'procs' && <th className="host-by">{hostBy === 'class' ? 'by class' : 'by agent type'}</th>}
                    </Fragment>
                  ))}
                </tr>
              </thead>
              <tbody>
                {hosts.map((h) => {
                  return (
                    <tr key={h.host}>
                      <td className="mono pin">{h.host}</td>
                      {hostCols.map((c) => (
                        <Fragment key={c[0]}>
                          <td className="num">{hostCell(h, c[0], c[2])}</td>
                          {c[0] === 'procs' && (
                            <td className="host-by">
                              <HostBreakdown h={h} cls={cls} by={hostBy} />
                            </td>
                          )}
                        </Fragment>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>



      </div>
    </>
  );
});

/* Shared by the live stream and the /api/events explorer, so a row looks the
 * same however it was fetched. Class is tagged as TEXT, not colour: crimson is
 * reserved for agent identity, so purpose owns the colour here. */
export function EventTable({ rows }: { rows: LiveEvent[] }) {
  if (!rows.length) return <NoRows />;
  const failed = (e: LiveEvent) => e.signal != null || (e.exit_code != null && e.exit_code !== 0);
  return (
    <ByUser
      rows={rows}
      user={(e) => e.user}
      noun="process"
      // both callers hold it in a `.stream`, which is already the scroll box
      scroll={false}
      cols={[
        {
          h: 'class',
          cell: (rs) => distinct(rs, (e) => CLS_LBL[e.actor3 ?? ''] ?? e.actor3),
          sort: (rs) => distinct(rs, (e) => CLS_LBL[e.actor3 ?? ''] ?? e.actor3),
        },
        {
          h: 'commands',
          cell: (rs) => <span className="mono">{distinct(rs, (e) => e.comm ?? e.tool, 4)}</span>,
          sort: (rs) => distinct(rs, (e) => e.comm ?? e.tool, 4),
        },
        {
          h: 'CPU',
          num: true,
          cell: (rs) => dur(rs.reduce((a, e) => a + (e.cpu_s ?? 0), 0)),
          sort: (rs) => rs.reduce((a, e) => a + (e.cpu_s ?? 0), 0),
        },
        {
          h: 'failed',
          num: true,
          tip: 'exited non-zero or on a signal',
          cell: (rs) => (rs.some(failed) ? fint(rs.filter(failed).length) : DASH),
          sort: (rs) => rs.filter(failed).length,
        },
        {
          h: 'latest',
          cell: (rs) => <span className="mono nm">{rs.map((e) => e.ts ?? '').sort().pop() || DASH}</span>,
          sort: (rs) => rs.map((e) => e.ts ?? '').sort().pop() || null,
        },
      ]}
      sub={(rs, withUser) => <EventRows rows={rs} withUser={withUser} />}
    />
  );
}

type EvKey = 'at' | 'user' | 'class' | 'pid' | 'host' | 'depth' | 'cmd' | 'ran' | 'cpu' | 'exit';
const evVal = (e: LiveEvent, k: EvKey) =>
  k === 'at' ? e.epoch ?? e.ts : k === 'user' ? e.user : k === 'class' ? e.actor3 : k === 'pid' ? e.pid : k === 'host' ? e.host
  : k === 'depth' ? e.depth : k === 'cmd' ? e.comm ?? e.tool : k === 'ran' ? e.duration_s : k === 'cpu' ? e.cpu_s
  : e.signal != null ? 1000 + e.signal : e.exit_code;

/** One user's processes, as rows; every user's, with a user column. */
function EventRows({ rows, withUser }: { rows: LiveEvent[]; withUser?: boolean }) {
  const nodes = useNodeWords();
  const s = useSort<EvKey>('at');
  return (
    <table>
      <thead>
        <tr>
          <SortTh s={s} k="at">at</SortTh>
          {withUser && <SortTh s={s} k="user" first="asc">user</SortTh>}
          <SortTh s={s} k="class" first="asc">class</SortTh>
          <SortTh s={s} k="pid" num>pid</SortTh>
          <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
          <SortTh s={s} k="depth" num tip={DEPTH_TIP}>tree depth</SortTh>
          <SortTh s={s} k="cmd" first="asc">command</SortTh>
          <SortTh s={s} k="ran" num>ran</SortTh>
          <SortTh s={s} k="cpu" num>CPU</SortTh>
          <SortTh s={s} k="exit" num>exit</SortTh>
        </tr>
      </thead>
      <tbody>
        {s.sort(rows, evVal).map((e, i) => {
          const comm = e.comm ?? e.tool ?? '';
          const args = e.args ?? '';
          const tail = args.startsWith(comm) ? args.slice(comm.length) : args ? ' ' + args : '';
          return (
            <tr key={`${e.epoch ?? e.ts ?? ''}-${i}`}>
              <td className="mono" style={{ color: 'var(--muted)' }}>
                {e.ts ?? DASH}
              </td>
              {withUser && <td className="mono">{e.user ?? DASH}</td>}
              <td
                style={{ color: CLS_COL[e.actor3 ?? ''] ?? 'var(--muted)' }}
                data-tip={e.agent_type ? `agent type: ${AGENT_TYPE_LBL[e.agent_type] ?? e.agent_type}` : undefined}
              >
                {CLS_LBL[e.actor3 ?? ''] ?? e.actor3 ?? 'unlabeled'}
              </td>
              <td
                className="num mono"
                style={{ fontSize: '12px' }}
                data-tip={e.ppid != null ? `parent pid ${e.ppid}` : undefined}
              >
                {e.pid ?? DASH}
              </td>
              <td className="mono" style={{ color: 'var(--muted)' }}>
                {(e.host ?? '').replace('login', '') || DASH}
              </td>
              <td className="num" data-tip={DEPTH_TIP}>
                {depthLabel(e.depth)}
              </td>
              {/* the cell is cut to one line, so the hover shows the whole
                  command (masked as served) and nothing else */}
              <td className="arg" data-tip={args || comm || undefined} data-tip-wide>
                <span className="cmd" style={{ color: pcol(e.purpose) }}>
                  {comm || DASH}
                </span>
                <span style={{ color: 'var(--ink-2)' }}>{tail}</span>
              </td>
              <td className="num">{dur(e.duration_s)}</td>
              <td className="num">
                {e.cpu_s == null ? DASH : e.cpu_s < 0.01 ? '<0.01s' : e.cpu_s.toFixed(2) + 's'}
              </td>
              <td className="num">
                {e.signal ? (
                  <span style={{ color: 'var(--warn)' }}>sig{e.signal}</span>
                ) : e.exit_code == null ? (
                  DASH
                ) : e.exit_code ? (
                  <span style={{ color: 'var(--crit)' }}>{e.exit_code}</span>
                ) : (
                  '0'
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
