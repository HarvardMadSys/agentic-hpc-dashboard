/* Live tier.
 *
 * Websocket-driven, with nothing invented between frames. The rate chart plots
 * `null` bins as gaps: a minute in which the collector wrote no record is a hole
 * in the line, because a 0 there would be a claim that the cluster was idle.
 */
import { Fragment, useMemo, useState } from 'react';
import type { Cls, FeedReport, FeedsResponse, LiveEvent, LiveHost, LiveResponse, Trajectories } from '../api/types';
import type { LiveState } from '../api/useLive';
import { Lines } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { clockLabel, fullClock, timeTicks } from '../charts/scale';
import { Basis, EmptyState, Legend, NoRows, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { AGENT_TYPE_LBL, CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { Segmented } from '../components/ClassFilter';
import type { Who } from '../lib/filters';
import { DASH, dur, fint, fmt, pc } from '../lib/format';
import { pcol } from '../lib/purposes';
import { ActiveUsers, scrollToUsers } from './LiveUsers';
import { ByUser, distinct } from '../components/ByUser';
import { WindowPlates } from '../components/WindowPlates';
import { WindowCharts } from './LiveCharts';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { GLOSS } from '../lib/glossary';
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

/** The newest bin that actually carried a value for this class. */
function latest(rate: LiveResponse['rate'], c: string): number | null {
  for (let i = rate.length - 1; i >= 0; i--) {
    const v = rate[i][c];
    if (typeof v === 'number') return v;
  }
  return null;
}

function nowNum(now: LiveResponse['now'], keys: string[]): number | null {
  for (const k of keys) {
    const v = now?.[k];
    if (typeof v === 'number') return v;
  }
  return null;
}

/** Per-class value out of `now`, trying `<key>_<cls>` then `<key>.<cls>` maps. */
function nowByClass(now: LiveResponse['now'], key: string): Partial<Record<string, number | null>> {
  const out: Partial<Record<string, number | null>> = {};
  const nested = now?.[key] as unknown;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    for (const c of CLS) {
      const v = (nested as Record<string, unknown>)[c];
      if (typeof v === 'number') out[c] = v;
    }
    if (Object.keys(out).length) return out;
  }
  for (const c of CLS) {
    const flat = c.replace('-', '_');
    const v = nowNum(now, [`${key}_${flat}`, `${key}_${c}`]);
    if (v != null) out[c] = v;
  }
  return out;
}

/* `roots` is the count of live agent and IDE session trees on the host: the
 * word a reader meets is "sessions" (roadmap note 14). */
const capFirst = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

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
        <span data-tip="processes with no agent or editor above them: people’s own shells, and the rest">
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

export function LiveTab({
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
  nodeNoun,
}: {
  live: LiveState;
  /** the window's reduced panels: the "last 24h" plates and every time series */
  P: Panels;
  traj: Trajectories | null;
  cls: Cls[];
  /** The page's user and agent-type filter. The live tier is one shared rolling
   *  count, not rebuilt per filter, so only rows that name a user narrow. */
  who?: Who;
  feeds: FeedsResponse | null;
  goto: (tab: string) => void;
  /** open a session's trajectory, or a user's sessions, on the Trajectories tab */
  openTraj: (sid: string) => void;
  openUserTraj: (user: string) => void;
  /** the service's default Active users columns (`ui.user_columns`) */
  userCols?: string[] | null;
  /** what the watched hosts are called (`ui.node_noun`): "login nodes" */
  nodeNoun?: string | null;
}) {
  const L = live.live;
  const rate = L?.rate ?? [];
  const now = L?.now ?? {};

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

  const series: LineSeries[] = cls.map((c) => ({
    lbl: CLS_LBL[c],
    c: CLS_COL[c],
    area: c === 'agent',
    mark: c === 'agent' ? (latest(rate, c) != null ? fint(latest(rate, c)) + '/min' : null) : null,
    // x is the bin's epoch, not its index: the window slides a bin every
    // minute, and a zoom held in indices would drift off the minutes picked.
    pts: rate
      .filter((r) => Number.isFinite(Number(r.epoch)))
      .map((r) => ({ x: Number(r.epoch), y: typeof r[c] === 'number' ? (r[c] as number) : null })),
  }));

  /* X ticks that stay legible at ANY window, and at any zoom into it: a round
   * interval that lands ~10 ticks across what is on screen, on LOCAL wall-clock
   * multiples of it (`timeTicks`), so a label reads `14:00` and not `14:07`. */
  const xticks = (a: number, b: number) => timeTicks(a, b, 10);

  const perMin = Object.fromEntries(CLS.map((c) => [c, latest(rate, c)]));
  const perMinAll = CLS.reduce<number | null>((a, c) => {
    const v = perMin[c];
    return v == null ? a : (a ?? 0) + v;
  }, null);

  const roots = nowByClass(now, 'roots');
  const rootsTot = nowNum(now, ['agent_roots', 'roots_total', 'roots']) ??
    (Object.keys(roots).length
      ? CLS.reduce((a, c) => a + (roots[c] ?? 0), 0)
      : null);
  const procs = nowByClass(now, 'procs');
  const procsTot = nowNum(now, ['procs_total', 'agent_procs', 'procs']) ??
    (Object.keys(procs).length ? CLS.reduce((a, c) => a + (procs[c] ?? 0), 0) : null);
  // One row per account, from the live reducer's roster: a person who runs
  // an agent, a shell, and an IDE is one user, not three.
  const U = L?.users;
  const usersBy = Object.fromEntries(CLS.filter((c) => U?.by_class[c] != null).map((c) => [c, U!.by_class[c]!]));

  // sortable by any column (roadmap note 27); the service's order until then
  const hostSort = useSort<string>();
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
        _notice: liveFeed?.notice ?? 'no live payload has arrived yet',
      } as const);

  if (envelope) {
    return (
      <div className="grid wide">
        <Panel title="Live tier" span feed={envelope._feed}>
          <EmptyState
            feed={envelope._feed}
            paths={[...envelope._paths]}
            status={envelope._status}
            notice={envelope._notice}
          />
        </Panel>
      </div>
    );
  }

  const windowMin = L?.window?.minutes ?? rate.length;
  // The service's own name for the span, so the chart title, the plate and the
  // picker cannot disagree about what is on screen.
  const windowLbl = L?.window?.label ?? `${fint(windowMin)} min`;

  return (
    <>
      {/* Two rows, each saying which span it counts: the same word ("sessions",
          "users") means a different span in each (roadmap note 1). */}
      <div className="rowlabel">right now</div>
      <div className="plates three">
        <Plate
          k="processes exited / min"
          total={perMinAll}
          stripe="var(--s1)"
          by={perMin}
          tip={
            GLOSS.exited + '\n\n' +
            'counted over the last minute that has fully elapsed: the minute in progress is ' +
            'still filling, so counting it would always read low.'
          }
          note={
            <>
              in the last full minute
              {perMinAll != null && perMin.agent != null
                ? ` · ${pc((100 * perMin.agent) / (perMinAll || 1))} agent`
                : ''}
            </>
          }
        />
        <Plate
          k="sessions · last 10 min"
          total={rootsTot}
          stripe="var(--s4)"
          by={roots}
          tip={GLOSS.sessions_now + ". A person's own shell has no such session, so the human slot is blank, not 0."}
          note={
            <>
              {procsTot != null ? `${fint(procsTot)} processes running` : 'running processes not reported'}
              {nowNum(now, ['rss_gb']) != null ? ` · ${fmt(nowNum(now, ['rss_gb']))} GB resident` : ''}
              {/* Counted from the live agent-session roots' own launch argv
                  (reducers/live.py), not the collector's `autonomous`, which
                  never saw --permission-mode bypassPermissions. Null when no
                  root was seen, so "none seen" never reads as "none bypassed". */}
              {nowNum(now, ['autonomous_roots']) != null
                ? ` · ${fint(nowNum(now, ['autonomous_roots']))} of ${fint(
                    nowNum(now, ['live_agent_sessions']),
                  )} running agent sessions launched with a bypass flag`
                : ''}
            </>
          }
        />
        <Plate
          k={`active users · ${windowLbl === 'in window' ? 'in the window' : 'last ' + windowLbl}`}
          total={U?.total ?? null}
          stripe="var(--s5)"
          by={usersBy}
          tip={
            'distinct accounts that ran something in the window or have an agent or IDE session running now. ' +
            'one person in several classes is one user; the class row counts the users in each.'
          }
          onClick={scrollToUsers}
          linkTip="see who, and what each one is running"
          note={
            <>
              {U ? `${fint(U.with_live_agent)} with an agent running now · ` : ''}
              <span className="plate-go">see who {'→'}</span>
            </>
          }
        />
      </div>

      <div className="rowlabel">
        {windowLbl === 'in window' ? 'the window' : <>last <b>{windowLbl}</b></>}
      </div>
      <WindowPlates P={P} cls={cls} traj={traj} />

      <div className="grid wide">
        <Panel
          title={`Processes exited per minute · last ${windowLbl}`}
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
                ['gaps', 'a minute with no record is null and is drawn as a hole in the line'],
                ['backfill', L?.backfill ? JSON.stringify(L.backfill) : null],
              ]}
            />
          }
        >
          <Lines
            series={series}
            W={1060}
            H={230}
            ymin={0}
            xticks={xticks}
            xfmt={clockLabel}
            xtip={fullClock}
            minSpan={3 * (L?.window?.bin_s ?? 60)}
            ylabel="exited / min"
            xlabel={`last ${windowLbl}`}
            xname="at"
            yname="exited/min"
          />
          <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        <WindowCharts P={P} cls={cls} />

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
        />

        <Panel
          title={`${capFirst(nodeNoun?.trim() || 'login nodes')} right now`}
          ask={[{ panel: 'live', keys: ['hosts', 'now'] }]}
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!hosts.length}
          emptyWhat="no host reported in the window"
          header={
            <Segmented
              label="running, by"
              value={hostBy}
              onChange={setHostBy}
              options={[
                { k: 'class', lbl: 'class', tip: 'running processes split by class: agents, people, and IDEs' },
                { k: 'type', lbl: 'agent type', tip: 'running processes split by the agent or editor they belong to; a person’s own shell has no type' },
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <SortTh s={hostSort} k="host" first="asc">host</SortTh>
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
                      <td className="mono">{h.host}</td>
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
}

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
      sub={(rs) => <EventRows rows={rs} />}
    />
  );
}

type EvKey = 'at' | 'class' | 'pid' | 'host' | 'depth' | 'cmd' | 'ran' | 'cpu' | 'exit';
const evVal = (e: LiveEvent, k: EvKey) =>
  k === 'at' ? e.epoch ?? e.ts : k === 'class' ? e.actor3 : k === 'pid' ? e.pid : k === 'host' ? e.host
  : k === 'depth' ? e.depth : k === 'cmd' ? e.comm ?? e.tool : k === 'ran' ? e.duration_s : k === 'cpu' ? e.cpu_s
  : e.signal != null ? 1000 + e.signal : e.exit_code;

/** One user's processes, as rows. */
function EventRows({ rows }: { rows: LiveEvent[] }) {
  const s = useSort<EvKey>();
  return (
    <table>
      <thead>
        <tr>
          <SortTh s={s} k="at">at</SortTh>
          <SortTh s={s} k="class" first="asc">class</SortTh>
          <SortTh s={s} k="pid" num>pid</SortTh>
          <SortTh s={s} k="host" first="asc">host</SortTh>
          <SortTh s={s} k="depth" num tip={DEPTH_TIP}>tree depth</SortTh>
          <SortTh s={s} k="cmd" first="asc">command</SortTh>
          <SortTh s={s} k="ran" num>ran</SortTh>
          <SortTh s={s} k="cpu" num>cpu</SortTh>
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
              <td
                style={{ color: CLS_COL[e.actor3 ?? ''] ?? 'var(--muted)' }}
                data-tip={e.agent_type ? `agent type: ${e.agent_type}` : undefined}
              >
                {CLS_LBL[e.actor3 ?? ''] ?? e.actor3 ?? 'unlabeled'}
              </td>
              <td
                className="num mono"
                style={{ fontSize: '10.5px' }}
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
