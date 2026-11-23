/* Live tier.
 *
 * Websocket-driven, with nothing invented between frames. The rate chart plots
 * `null` bins as gaps: 5 minutes in which the collector wrote no record are a hole
 * in the line, because a 0 there would be a claim that the cluster was idle.
 */
import { Fragment, memo, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { Cls, FeedReport, FeedsResponse, LiveHost, LiveResponse, ToolMix, Trajectories } from '../api/types';
import type { LiveState } from '../api/useLive';
import { Fluid, Lines } from '../charts/primitives';
import type { LinePt, LineSeries } from '../charts/primitives';
import { clockLabel, fullClock, tickTarget, timeTicks } from '../charts/scale';
import { EmptyState, Legend, Panel } from '../components/Panel';
import { AGENT_TYPE_LBL, CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { Segmented } from '../components/ClassFilter';
import type { Who } from '../lib/filters';
import { DASH, fint, fmt } from '../lib/format';
import { ActiveUsers } from './LiveUsers';
import { WindowPlates } from '../components/WindowPlates';
import { Attention } from './Attention';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { useNodeWords } from '../lib/nodes';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { offsetAt } from '../lib/zone';

/* ------------------------------------------------------------- helpers */

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
 * word a reader meets is "sessions" (roadmap note 14). A node's load and CPU
 * are the node tier's, on Login node health (tabs/NodeHealth.tsx), so this
 * table has no column for them. */
const HOST_COLS: [key: string, lbl: string, kind: 'int' | 'num', gloss?: string][] = [
  ['roots', 'sessions', 'int', 'sessions_now'],
  ['procs', 'running', 'int', 'running'],
  ['rss_gb', 'memory GB', 'num', 'rss'],
  ['d_state', 'D-state', 'int', 'dstate'],
  ['users', 'users', 'int', 'user'],
  ['tcp_open_ext', 'ext TCP', 'int', 'exttcp'],
];

function hostCell(h: LiveHost, key: string, kind: string) {
  const v = h[key];
  if (typeof v !== 'number') return DASH;
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
  /** open a session on the Agents tab, or a user's sessions in their portrait on Users */
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
    // seconds west of UTC in the page's zone (lib/zone.ts), as getTimezoneOffset counts
    const tz = -offsetAt(Date.now());
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
        // the class's word at the line's end: told apart without its colour
        tag: CLS_LBL[c],
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
  // The plates' span, read off their own panel's envelope and not off the
  // picker: while a window builds the two differ, and the label must say what
  // the plates under it counted (web/README.md, "The window control").
  const platesWin = pick<ToolMix>(P, 'tool_mix', 'ebpfm')._window?.label;

  return (
    <>
      {attention}
      {/* One row of headline plates over the page's window, which the window
          control at the top picks for the whole tab: Who uses what, the rate
          chart and every chart below count the same span. */}
      <div className="rowlabel">{platesWin ? <>last <b>{platesWin}</b></> : 'the window'}</div>
      <WindowPlates P={P} cls={cls} traj={traj} />

      <div className="grid wide">
        <Panel
          title={`Processes exited per 5 minutes · last ${windowLbl}`}
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!rate.length}
          emptyWhat="the live window holds no bins"
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
          ask={[{ panel: 'live', keys: ['hosts'] }]}
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
