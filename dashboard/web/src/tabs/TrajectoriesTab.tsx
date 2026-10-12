/* Trajectories.
 *
 * The rank switcher only REORDERS: the events column and the cpu column are
 * both always present, because ranking by one and showing only that one is how
 * a poll loop gets mistaken for work (and vice versa).
 *
 * The plates describe every session in the window (`summary`), not the listed
 * rows, and still say how much one user dominates, in the sessions plate's
 * note: the busiest sessions are often one user's campaign, not a pattern.
 *
 * Every row opens the inspector (TrajectoryInspector.tsx): the session as the
 * process tree its summary compresses. Which session is open lives in the URL
 * (`?traj=`), so an inspection is a link like every other view here.
 *
 * In the tool-sequence table a ribbon zooms along the sequence, and a ribbon
 * run or a tool token opens the inspector AT that run: the row carries when
 * each run began (`purpose_t`, `tool_t`), and the inspector goes to the
 * process that run's first record came from.
 */
import { useState } from 'react';
import type { Async } from '../api/hooks';
import type { Cls, TrajRank, TrajRow, TrajSummary, Trajectories } from '../api/types';
import { TRAJ_RANKS } from '../api/types';
import { Ribbon } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { Segmented } from '../components/ClassFilter';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { sessionName } from '../lib/sessions';
import { ByUser, distinct } from '../components/ByUser';
import { DASH, dur, fint, fmt, mb, pc } from '../lib/format';
import { pcol, plbl, purposeLegend } from '../lib/purposes';
import { absent } from '../lib/panels';
import { AP_LABEL, AP_SHORT, SB_LABEL, label } from '../lib/sandbox';
import { ToolTokens, TrajectoryInspector, VIA_TIP, runJump, runTip } from './TrajectoryInspector';
import type { RunJump } from './TrajectoryInspector';
import './trajectories.css';

const RANK_LBL: Record<TrajRank, string> = {
  events: 'processes',
  cpu_s: 'CPU',
  distinct_tools: 'distinct tools',
  chain_runs: 'chain runs',
};

/** `host|2026-10-11|agent|apid:561705` as `10-11` and `apid:561705`. */
export function TrajectoriesTab({
  traj,
  rank,
  setRank,
  cls,
  win,
  sel,
  select,
  tfrom,
  setTfrom,
}: {
  traj: Async<Trajectories>;
  rank: TrajRank;
  setRank: (r: TrajRank) => void;
  cls: Cls[];
  /** the window as the URL has it: the inspector reads the same one */
  win: number | null;
  /** the sid open in the inspector, or null */
  sel: string | null;
  select: (sid: string | null) => void;
  tfrom: number | null;
  setTfrom: (t: number | null) => void;
}) {
  const t = traj.data;
  // Not answered yet is waiting, not a missing feed -- a busy service, or a
  // filtered view asked for the first time, can take a while to reply.
  const env = t ?? ((traj.error
    ? absent('ebpfm', traj.error)
    : { ...absent('ebpfm', 'waiting for the dashboard service…'), _waiting: true }) as unknown as Trajectories);
  const rows = t?.rows ?? {};
  const order = t?.ranks?.[rank] ?? [];
  const listed: TrajRow[] = order
    .map((sid) => rows[sid])
    .filter((r): r is TrajRow => !!r)
    .filter((r) => !r.actor3 || cls.includes(r.actor3 as Cls));

  // A run picked in the tool-sequence table, for the inspector to go to. It
  // names its session, so an inspector opened some other way never takes it.
  const [jump, setJump] = useState<{ sid: string; j: RunJump } | null>(null);
  const open = (sid: string | null) => {
    setJump(null);
    select(sid);
  };
  const pick = (r: TrajRow, kind: 'purpose' | 'tool', i: number) => {
    setJump((p) => ({ sid: r.sid, j: runJump(r, kind, i, (p?.j.n ?? 0) + 1) }));
    // the session already open keeps its read, and the page it is on
    if (sel !== r.sid) select(r.sid);
  };

  // A row is a button for the inspector: click anywhere on it, or tab to its
  // session id and press Enter.
  const rowProps = (sid: string) => ({
    className: `tj-row${sel === sid ? ' sel' : ''}`,
    onClick: () => open(sid),
    'aria-selected': sel === sid,
  });

  const sum = t?.summary ?? {};
  const agent = cls.includes('agent') ? sum.agent : undefined;
  const shownCls = cls.filter((c) => c in sum);
  const byCls = (f: (d: TrajSummary) => number | null) =>
    Object.fromEntries(shownCls.map((c) => [c, f(sum[c])]));

  return (
    <>
      {/* Every session in the window, not the listed few, and a probe is not a
          session: see `summary` in reducers/trajectories.py. */}
      <div className="plates">
        <Plate
          k="agent sessions in window"
          total={agent?.sessions ?? null}
          stripe="var(--s1)"
          by={byCls((d) => d.sessions)}
          tip={
            'a session is one agent process and everything it ran (for agents), or one ' +
            'login / terminal (for people and IDEs).\n\n' +
            'an agent-type process with no agent above it -- a script checking ' +
            '`codex --version` every minute -- is not a session and is counted apart.'
          }
          note={
            agent ? (
              <>
                {fint(agent.users)} agent user{agent.users === 1 ? '' : 's'}
                {agent.top_user_share_pct != null
                  ? ` · the busiest makes ${pc(agent.top_user_share_pct)} of agent processes`
                  : ''}
                {agent.probes ? ` · ${fint(agent.probes)} one-off probes set aside` : ''}
              </>
            ) : undefined
          }
        />
        <Plate
          k="processes per agent session · median"
          total={agent?.calls_p50 ?? null}
          stripe="var(--s4)"
          by={byCls((d) => d.calls_p50)}
          note={
            agent?.calls_p50 != null
              ? `p90 ${fint(agent.calls_p90)} · the busiest ran ${fint(agent.calls_max)}`
              : undefined
          }
        />
        <Plate
          k="agent session length · median"
          total={agent?.wall_p50_s ?? null}
          stripe="var(--s6)"
          by={byCls((d) => d.wall_p50_s)}
          fmtFn={(n) => dur(n)}
          tip={
            'first to last process INSIDE the window: in a 1 h window no session reads ' +
            'longer than an hour, and one idle at its end reads shorter than it was open.'
          }
          note={
            agent?.longest
              ? `longest ${dur(agent.longest.wall_s)} · ${agent.longest.user ?? '?'}, ${
                  agent.longest.agent_type ?? 'agent'
                }, ${fint(agent.longest.tool_calls)} processes`
              : undefined
          }
        />
        <Plate
          k="agent processes spent waiting"
          total={agent?.wait_pct ?? null}
          stripe="var(--s2)"
          by={byCls((d) => d.wait_pct)}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip={
            'processes whose only job is to wait on something else: polling the ' +
            'process table (ps, pgrep, top, sleep loops) or the scheduler (squeue, ' +
            'sacct, scontrol, watch).'
          }
          note={agent ? `of ${fint(agent.tool_calls)} processes in real agent sessions` : undefined}
        />
      </div>

      <div className="grid wide">
        {sel && (
          <TrajectoryInspector
            key={sel}
            sid={sel}
            row={rows[sel] ?? null}
            win={win}
            from={tfrom}
            setFrom={setTfrom}
            onClose={() => open(null)}
            jump={jump?.sid === sel ? jump.j : null}
          />
        )}
        <Panel
          title="Ranked sessions"
          p={env}
          span
          empty={!listed.length}
          emptyWhat={
            Object.keys(rows).length
              ? 'no session at this rank matches the class filter'
              : 'the trajectory buffer is empty'
          }
          header={
            <Segmented
              label="rank by"
              value={rank}
              options={TRAJ_RANKS.map((r) => ({
                k: r,
                lbl: RANK_LBL[r],
                tip: `reorder by ${RANK_LBL[r]} — every column stays visible`,
              }))}
              onChange={setRank}
            />
          }
          basis={
            <Basis
              items={[
                ['rank_meaning', t?.rank_meaning],
                ['basis', t?.basis],
                ['window', t?.window],
                ['buffer', t?.buffer],
                ['ribbon', 'segment width is the run length; colour is the purpose run. Zoomed, the widths are shares of the calls in view, which the line above it states'],
                ['runs', 'a ribbon run or a tool token opens the inspector at the process its first record came from: the record time, then its purpose or tool, pick it'],
              ]}
            />
          }
        >
          <ByUser
            rows={listed}
            user={(r) => r.user ?? (r.users ?? [])[0]}
            noun="session"
            open
            cols={[
              { h: 'classes', cell: (rs) => distinct(rs, (r) => CLS_LBL[r.actor3 ?? 'unlabeled'] ?? r.actor3) },
              { h: 'processes', num: true, cell: (rs) => fint(rs.reduce((a, r) => a + (r.n_events ?? 0), 0)) },
              { h: 'cpu s', num: true, cell: (rs) => fmt(rs.reduce((a, r) => a + (r.cpu_s ?? 0), 0)) },
              { h: 'peak RSS', num: true, cell: (rs) => mb(Math.max(0, ...rs.map((r) => r.peak_rss_mb ?? 0)) || null) },
              { h: 'read', num: true, cell: (rs) => mb(rs.reduce((a, r) => a + (r.io_rd_mb ?? 0), 0)) },
              { h: 'written', num: true, cell: (rs) => mb(rs.reduce((a, r) => a + (r.io_wr_mb ?? 0), 0)) },
            ]}
            sub={(rs) => (
              <div className="scroller">
                <table className="tj-table">
                  <thead>
                    <tr>
                      <th>session</th>
                      <th className="num" data-tip="collector session keys folded into this session, and how the least certain one was attributed">
                        keys
                      </th>
                      <th>class</th>
                      <th>host</th>
                      {/* both always visible: the switcher reorders, it never hides */}
                      <th className="num" data-tip="processes that exited in the session within the window">processes</th>
                      <th className="num">cpu s</th>
                      <th className="num">tools</th>
                      <th className="num">chains</th>
                      <th className="num">wall</th>
                      <th className="num">peak RSS</th>
                      <th className="num" data-tip="bytes read from disk (block layer)">read</th>
                      <th className="num" data-tip="bytes written to disk (block layer)">written</th>
                      <th>sandbox</th>
                      <th>approval</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rs.map((r) => (
                      <tr key={r.sid} {...rowProps(r.sid)}>
                        <td
                          className="mono"
                          data-tip={`${r.sid}\nkey_type ${r.key_type}${
                            r.agent_type ? '\nagent_type ' + r.agent_type : ''
                          }\nclick to open its process tree`}
                        >
                          {/* host and class have their own columns; the date and
                              the key are what tell two rows apart */}
                          <button className="tj-open" onClick={(e) => { e.stopPropagation(); open(r.sid); }}>
                            <span className="tj-date">{sessionName(r).date}</span>
                            {sessionName(r).key}
                          </button>
                        </td>
                        <td
                          className="num"
                          data-tip={r.owner_via ? `${r.owner_via}: ${VIA_TIP[r.owner_via]}` : undefined}
                        >
                          {fint(r.fragments ?? 1)}
                          {r.owner_via && r.owner_via !== 'self' && r.owner_via !== 'ancestor' && (
                            <div className="sub">{r.owner_via.replace(/_/g, ' ')}</div>
                          )}
                        </td>
                        <td>
                          <span
                            className="dot"
                            style={{
                              background: CLS_COL[r.actor3 ?? 'unlabeled'],
                              marginRight: 5,
                            }}
                          />
                          {CLS_LBL[r.actor3 ?? 'unlabeled'] ?? r.actor3}
                        </td>
                        <td className="mono" style={{ color: 'var(--muted)' }}>
                          {(r.host || '').replace('login', '') || DASH}
                        </td>
                        <td
                          className="num"
                          style={rank === 'events' ? { fontWeight: 600 } : undefined}
                        >
                          {fint(r.n_events)}
                        </td>
                        <td className="num" style={rank === 'cpu_s' ? { fontWeight: 600 } : undefined}>
                          {fmt(r.cpu_s)}
                        </td>
                        <td
                          className="num"
                          style={rank === 'distinct_tools' ? { fontWeight: 600 } : undefined}
                        >
                          {fint(r.distinct_tools)}
                        </td>
                        <td
                          className="num"
                          style={rank === 'chain_runs' ? { fontWeight: 600 } : undefined}
                        >
                          {fint(r.chain_runs)}
                        </td>
                        <td className="num">{dur(r.wall_s)}</td>
                        <td className="num">{mb(r.peak_rss_mb)}</td>
                        <td className="num">{mb(r.io_rd_mb)}</td>
                        <td className="num">{mb(r.io_wr_mb)}</td>
                        <td>
                          <span className="sev low" data-tip="the session's first known verdict">
                            {label(SB_LABEL, r.sandbox ?? 'unknown')}
                          </span>
                        </td>
                        <td>
                          <span
                            className={`sev ${
                              r.approval === 'bypassed' || r.autonomous
                                ? 'HIGH'
                                : r.approval === 'auto'
                                  ? 'med'
                                  : 'low'
                            }`}
                            data-tip={`${label(AP_LABEL, r.approval ?? 'unknown')}\nread off the session root's own record`}
                          >
                            {label(AP_SHORT, r.approval ?? 'unknown')}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          />
        </Panel>

        <Panel
          title="Tool sequence — ribbon and tokens"
          p={env}
          span
          empty={!listed.length}
          emptyWhat="no session to draw"
        >
          <ByUser
            rows={listed.slice(0, 20)}
            user={(r) => r.user ?? (r.users ?? [])[0]}
            noun="session"
            open
            cols={[
              { h: 'processes', num: true, cell: (rs) => fint(rs.reduce((a, r) => a + (r.n_events ?? 0), 0)) },
              { h: 'cpu s', num: true, cell: (rs) => fmt(rs.reduce((a, r) => a + (r.cpu_s ?? 0), 0)) },
            ]}
            sub={(rs) => (
              <div className="scroller">
                <table className="tj-table">
                  <thead>
                    <tr>
                      <th>session</th>
                      <th className="num" data-tip="processes that exited in the session within the window">processes</th>
                      <th className="num">cpu s</th>
                      <th style={{ minWidth: 330 }}>purpose over the sequence</th>
                      <th className="wrap" style={{ minWidth: 300 }}>tool runs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rs.map((r) => (
                      <tr key={r.sid} {...rowProps(r.sid)}>
                        <td className="mono" style={{ whiteSpace: 'nowrap' }} data-tip={r.sid}>
                          <button className="tj-open" onClick={(e) => { e.stopPropagation(); open(r.sid); }}>
                            <span className="tj-date">{sessionName(r).date}</span>
                            {sessionName(r).key}
                          </button>
                        </td>
                        <td className="num">{fint(r.n_events)}</td>
                        <td className="num">{fmt(r.cpu_s)}</td>
                        <td>
                          {r.purpose_rle?.length ? (
                            <Ribbon
                              rle={r.purpose_rle}
                              colour={pcol}
                              label={(k, n, i) =>
                                `${plbl(k)} ×${fint(n)} · run ${fint(i + 1)}\n${runTip(r.purpose_t?.[i], 'open the inspector')}`
                              }
                              W={320}
                              H={13}
                              zoom
                              note={
                                r.purpose_rle.reduce((a, [, k]) => a + k, 0) < r.n_events
                                  ? `(the first ${fint(r.purpose_rle.length)} runs of ${fint(r.n_events)})`
                                  : undefined
                              }
                              name="purpose runs"
                              onPick={(i) => pick(r, 'purpose', i)}
                            />
                          ) : (
                            <span className="nm">no purpose run-lengths</span>
                          )}
                        </td>
                        <td className="wrap">
                          {r.tool_rle?.length ? (
                            <ToolTokens
                              rle={r.tool_rle}
                              truncated={r.chain_truncated}
                              times={r.tool_t}
                              onPick={(i) => pick(r, 'tool', i)}
                            />
                          ) : (
                            <span className="nm">no tool run-lengths</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          />
          <Legend items={purposeLegend()} />
        </Panel>

      </div>
    </>
  );
}
