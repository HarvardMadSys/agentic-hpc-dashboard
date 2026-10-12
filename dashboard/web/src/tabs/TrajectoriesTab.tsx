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
import { Panel } from '../components/Panel';
import { Segmented } from '../components/ClassFilter';
import { Plate } from '../components/Plate';
import { CLS_COL } from '../lib/classes';
import { sessionName } from '../lib/sessions';
import { DASH, dur, fint, pc } from '../lib/format';
import { pcol, plbl } from '../lib/purposes';
import { absent } from '../lib/panels';
import { TrajectoryInspector } from './TrajectoryInspector';
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
  tuser = null,
  clearUser = () => {},
  routine = null,
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
  /** open on this user's sessions (from Active users' "trajectories →") */
  tuser?: string | null;
  clearUser?: () => void;
  /** the site's routine commands, which the tree can hide */
  routine?: string[] | null;
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
  const shownList = tuser ? listed.filter((r) => (r.user ?? (r.users ?? [])[0]) === tuser) : listed;
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

      {/* The ranked list and the inspector, side by side (roadmap note 39.2):
          pick a session on the left, its process tree opens on the right. */}
      <div className="tj-md">
        <Panel
          title="Sessions"
          id="sec-ranked-sessions"
          ask={[{ panel: 'trajectories', rank }]}
          p={env}
          empty={!listed.length}
          emptyWhat={
            Object.keys(rows).length
              ? 'no session at this rank matches the class filter'
              : 'the trajectory buffer is empty'
          }
        >
          {/* in the body, not the header: the column is too narrow for both */}
          <div className="tj-rank">
            <Segmented
              label="rank by"
              wrap
              value={rank}
              options={TRAJ_RANKS.map((r) => ({
                k: r,
                lbl: RANK_LBL[r],
                tip: `the busiest sessions by ${RANK_LBL[r]} first`,
              }))}
              onChange={setRank}
            />
          </div>
          {tuser && (
            <div className="tj-filter">
              {`${shownList.length ? '' : 'no ranked session for '}`}
              <b className="mono">{tuser}</b>
              {shownList.length ? '’s sessions' : ' in this window'}
              {' · '}
              <button className="linkbtn" onClick={() => clearUser()}>show everyone</button>
            </div>
          )}
          <div className="tj-list">
            {(tuser && shownList.length ? shownList : listed).map((r) => {
              const nm = sessionName(r);
              return (
                <button
                  key={r.sid}
                  className={`tj-item${sel === r.sid ? ' on' : ''}`}
                  style={{ borderLeftColor: sel === r.sid ? undefined : CLS_COL[r.actor3 ?? 'unlabeled'] }}
                  onClick={() => open(r.sid)}
                  data-tip={`${r.sid}\nclick to open its process tree`}
                >
                  <span className="tj-item-h">
                    <b>{nm.key}</b>
                    <span className="tj-date">{nm.date}</span>
                  </span>
                  <span className="tj-item-s">
                    {(r.user ?? (r.users ?? [])[0]) ?? DASH} · {fint(r.n_events)} processes · {dur(r.cpu_s)} CPU · {dur(r.wall_s)}
                  </span>
                  {r.purpose_rle?.length ? (
                    <Ribbon rle={r.purpose_rle} colour={pcol} label={(k, n) => `${plbl(k)} ×${fint(n)}`} W={290} H={7} />
                  ) : null}
                </button>
              );
            })}
          </div>
        </Panel>

        <div>
          {sel ? (
            <TrajectoryInspector
              key={sel}
              sid={sel}
              row={rows[sel] ?? null}
              win={win}
              from={tfrom}
              setFrom={setTfrom}
              onClose={() => open(null)}
              jump={jump?.sid === sel ? jump.j : null}
              routine={routine}
            />
          ) : (
            <div className="tj-empty">pick a session on the left: its process tree opens here</div>
          )}
        </div>
      </div>
    </>
  );
}
