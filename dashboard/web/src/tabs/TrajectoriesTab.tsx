/* Trajectories: the list of every session in the window, and the inspector beside it.
 *
 * It was the Sessions tab, and is the Agents tab's last part now (App.tsx), its key `trajectories` an
 * alias that opens it there; a user's own sessions are listed, and open, in their portrait on the Users
 * tab, which opens this list narrowed to them (`tuser`) for every one of them.
 *
 * The rank switcher only REORDERS: the events column and the cpu column are
 * both always present, because ranking by one and showing only that one is how
 * a poll loop gets mistaken for work (and vice versa). Any other ranking adds
 * its own quantity as a line of its own.
 *
 * The list is live while it is on screen: re-read every few seconds
 * (App.tsx), a session whose agent is running says so, and a row the re-read
 * moves slides from where it was to where it is, so the eye can follow a
 * session climbing the order; one that climbed, or arrived, flashes.
 *
 * The plates describe every session in the window (`summary`), not the listed
 * rows, and still say how much one user dominates, in the sessions plate's
 * note: the busiest sessions are often one user's campaign, not a pattern.
 *
 * Every row opens the inspector (TrajectoryInspector.tsx, hosted by
 * OpenSession.tsx): the session as the process tree its summary compresses.
 * Which session is open lives in the URL (`?traj=`), so an inspection is a
 * link like every other view here. A task or phase picked in it that the
 * other day's row of the session holds opens that row (OpenSession.tsx).
 *
 * Every row's commands are read, an agent's and a person's or an editor's alike, and its row says what kind of session it was, a
 * chip beside where it was driven from (components/CategoryChip.tsx): the use that
 * holds half its work, `mixed`, or what it had none of, with its summary on hover.
 * It says what the agent was doing too
 * (components/PhaseBand.tsx): a thin band of its phases stands where the purpose
 * ribbon did, with a tick on it where each task or lone stretch after the first
 * begins, and the line under it names the tasks (`A → B → 3 more`), or where the
 * block has none, gives the order the phases came in: two lines at most, and all
 * of it in its hover. The stat line counts tool calls, not processes -- the
 * process count moves to the row's tooltip, and to the processes ranking's own
 * line. A running session's band ends in what the per-minute census shows at its
 * live edge. An agent type whose commands are not recognized keeps the purpose
 * ribbon and says so under it; people and IDEs keep today's row exactly, with no
 * note, because no phase was ever on offer for them.
 */
import { memo, useCallback, useLayoutEffect, useRef } from 'react';
import type { RefObject } from 'react';
import type { Async } from '../api/hooks';
import type { Cls, TrajOrder, TrajRank, TrajRow, TrajSummary, Trajectories } from '../api/types';
import { TRAJ_RANKS } from '../api/types';
import { Ribbon } from '../charts/primitives';
import { Panel } from '../components/Panel';
import { Icon } from '../components/Icon';
import { PhaseBand } from '../components/PhaseBand';
import { Segmented } from '../components/ClassFilter';
import { Plate } from '../components/Plate';
import { AGENT_TYPE_LBL, CLS_COL } from '../lib/classes';
import { sessionTitle } from '../lib/sessions';
import { DASH, ago, dur, fint, mb, pc } from '../lib/format';
import { useClockPinned, useNow } from '../lib/useNow';
import { bandAxis, calls, driverOf, edgeLabel, isBlock, phasesUnread, runningUnread, sequenceLine } from '../lib/phases';
import { pcol, plbl } from '../lib/purposes';
import { taskSequence } from '../lib/tasks';
import { surfaceSplit } from '../lib/surfaces';
import { absent, winLabel } from '../lib/panels';
import { SurfaceChip } from '../components/SurfaceChip';
import { CategoryChip } from '../components/CategoryChip';
import { OpenSession } from './OpenSession';
import './trajectories.css';

const RANK_LBL: Record<TrajRank, string> = {
  recent: 'recency',
  events: 'processes',
  cpu_s: 'CPU',
  peak_rss_mb: 'memory',
  io_mb: 'disk I/O',
  net_mb: 'network',
  distinct_tools: 'distinct tools',
};
const RANK_TIP: Record<TrajRank, string> = {
  recent: 'the session with the newest process first',
  events: 'the sessions that ran the most processes first',
  cpu_s: 'the sessions that used the most CPU first',
  peak_rss_mb: 'the sessions whose largest process held the most memory first, not the session added up',
  io_mb: 'the sessions that read and wrote the most bytes on disk first',
  net_mb: 'the sessions that sent and received the most bytes over the network first',
  distinct_tools: 'the sessions that ran the most different tools first',
};

/** Whether the row's stat line counts calls, not processes: its phases are read, as every row's are (an agent's tool
 *  calls, or the commands its terminals ran). A null count, from a service that read agents alone, is not counted (R34),
 *  which is not none, so such a row keeps its processes. */
const countsCalls = (r: TrajRow): boolean => isBlock(r.phases) && r.n_calls != null;

/** What the ranking adds to a row, where the row does not already say it. */
function rankLine(r: TrajRow, rank: TrajRank, now: number): string | null {
  switch (rank) {
    case 'events':
      // the stat line counts tool calls on a row with phases (R3), so the processes ranking says its own quantity
      return countsCalls(r) ? `${fint(r.n_events)} processes` : null;
    case 'peak_rss_mb':
      return `${mb(r.peak_rss_mb)} peak memory`;
    case 'io_mb':
      return `${mb(r.io_rd_mb)} disk read · ${mb(r.io_wr_mb)} written`;
    case 'net_mb':
      return `${mb(r.net_tx_mb)} sent · ${mb(r.net_rx_mb)} received`;
    case 'distinct_tools':
      return `${fint(r.distinct_tools)} distinct tools`;
    case 'recent':
      return r.last != null ? `last process ${ago(Math.max(0, now - r.last * 1000))}` : null;
    default:
      return null;
  }
}

function liveTip(r: TrajRow): string {
  const run = r.running;
  if (!run) return '';
  const parts = ['its agent is running now'];
  if (run.n_procs != null) {
    parts.push(
      `${fint(run.n_procs)} process${run.n_procs === 1 ? '' : 'es'} up in its tree` +
        (run.rss_mb != null ? `, holding ${mb(run.rss_mb)}` : ''),
    );
  } else {
    parts.push('it runs inside another program’s tree, so its own process count is not known');
  }
  return parts.join('\n');
}

/** The purpose ribbon's size, which the phase band stands in for. */
const BAND_W = 290;
const BAND_H = 7;

/** The ribbon of a row.
 *
 *  A row whose commands are read, which every row's are, gets its phases: a band, and under it a line. The line names its tasks
 *  (`taskSequence`: the first two, and a count of the rest) where its block has any, and ticks on the band
 *  mark where each task or lone stretch after the first begins. A block with none, as one of the first
 *  version, keeps the order the phases came in, in words (`sequenceLine`). Either line is two lines at most,
 *  with an ellipsis, so that two long quotes do not make a tall row, and all of it is its hover. An agent
 *  over several days is a row to a day, and each serves the one block, so each draws the whole session's
 *  band and says its tasks.
 *  The band runs from the session's first record to its last phase's end, and a running session's on to now
 *  (`nowS`, on the service's clock), where the census's word on what is running ends it; a session running now
 *  whose first call has not ended is that edge alone (`runningUnread`). A row with no phase that is not running
 *  keeps the purpose of each process, run by run. A row an older service did not read (it read Claude Code and
 *  Codex alone, and sent the rest `{unavailable}`) says so under an AGENT, and a person or an IDE row says
 *  nothing (R41).
 *
 *  Drawn by `SessionItem`, which renders again with every live frame where the band runs on to now, and whenever
 *  the clock's pin flips: an unknown edge is worded by `clockPinned()`, a plain flag that frame sets (lib/useNow.ts). */
function RowRibbon({ r, nowS }: { r: TrajRow; nowS: number }) {
  const ph = r.phases;
  const block = isBlock(ph) ? ph : null;
  // the session's own axis (`bandAxis`): from its first record or first phase to its last call, and on to now while it runs
  const axis = block ? bandAxis(block.list, r.first, r.running?.now ? nowS : null) : null;
  const edge = r.running?.now ?? null;
  // a session running now whose first call has not ended has no phase yet: its band is its live edge alone, from its
  // first record (`runningUnread`), and its line says what runs
  if (block && axis && (block.list.length || runningUnread(block, edge))) {
    const { list } = block;
    // its tasks, and where the block names none, its phases: `taskSequence` is empty for a block with no task
    // with no phase yet, the edge's own name: `running now · python, srun`
    const line = taskSequence(block) || (list.length ? sequenceLine(list, edge) : edgeLabel(edge));
    return (
      <>
        <PhaseBand
          list={list}
          tasks={block.tasks}
          // from the first record or the first phase, whichever is earlier, to the last call's end, and on to now for a
          // session running now: a phase is never cut and a call never runs off the band
          t0={axis[0]}
          t1={axis[1]}
          W={BAND_W}
          H={BAND_H}
          now={edge}
          nowAt={nowS}
          driver={driverOf(block)}
        />
        {/* clamped to two lines (`.tj-seq`), so two long quotes make no tall row, and all of it is in its hover, a wide one */}
        <span className="tj-seq" data-tip={line} data-tip-wide>
          {line}
        </span>
      </>
    );
  }
  const why = phasesUnread(r);
  return (
    <>
      {r.purpose_rle?.length ? (
        <Ribbon rle={r.purpose_rle} colour={pcol} label={(k, n) => `${plbl(k)} ×${fint(n)}`} W={BAND_W} H={BAND_H} />
      ) : null}
      {why && <span className="tj-note">{why}</span>}
    </>
  );
}

/** A row of the list: a session, and a click to open it. It renders again with each live frame only where it says
 *  something against now -- "last process 2 min ago" when ranked by recency, or a running session's band, which runs
 *  on to now -- and whenever the clock's pin flips, which words an unknown edge (lib/useNow.ts). */
const SessionItem = memo(function SessionItem({
  r,
  rank,
  on,
  onOpen,
}: {
  r: TrajRow;
  rank: TrajRank;
  /** it is the one open in the inspector */
  on: boolean;
  onOpen: (sid: string) => void;
}) {
  const ph = r.phases;
  // a band that runs on to now, a session with no phase yet included (`runningUnread`)
  const edge = isBlock(ph) && r.running?.now != null;
  const now = useNow(rank === 'recent' || edge);
  useClockPinned();
  // titled by whose it is and what ran it; the collector's key (a pid) goes to the line under it
  const nm = sessionTitle(r);
  const extra = rankLine(r, rank, now);
  // a row whose commands are read is counted in its calls, tool calls or commands by who ran it (`driver`); a row of a
  // service that did not read it, in processes
  const counted = countsCalls(r);
  const count = counted && isBlock(ph) ? calls(r.n_calls ?? 0, driverOf(ph)) : `${fint(r.n_events)} processes`;
  return (
    <button
      data-sid={r.sid}
      className={`tj-item${on ? ' on' : ''}`}
      style={{ borderLeftColor: on ? undefined : CLS_COL[r.actor3 ?? 'unlabeled'] }}
      onClick={() => onOpen(r.sid)}
      data-tip={`${r.sid}${counted ? `\n${fint(r.n_events)} processes` : ''}\nclick to open its process tree`}
    >
      <span className="tj-item-h">
        <b>{nm.title}</b>
        <span className="tj-date">{nm.date}</span>
        <SurfaceChip s={r.surface} detail={r.surface_detail} />
        {/* no chip for a session whose calls are running and none has ended: `no tool calls` would be wrong */}
        <CategoryChip of={isBlock(ph) && !runningUnread(ph, r.running?.now) ? ph : null} />
        {r.running && (
          <span className="tj-live" data-tip={liveTip(r)}>
            <i className="ld" />
            live
            {r.running.n_procs != null ? ` · ${fint(r.running.n_procs)} running` : ''}
          </span>
        )}
      </span>
      <span className="tj-item-s">
        {nm.detail ? `${nm.detail} · ` : ''}{count} · {dur(r.cpu_s)} CPU · {dur(r.wall_s)}
      </span>
      {extra && <span className="tj-item-m">{extra}</span>}
      <RowRibbon r={r} nowS={now / 1000} />
    </button>
  );
});

const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

/** Slide each `[data-sid]` row of `box` from where it was to where it is now,
 *  and flash one that climbed or arrived. Positions are layout offsets, which a
 *  transform does not change and a scroll does not move. A new `reset`
 *  (another ranking, filter, or window) is a new list: nothing slides. Read
 *  when the rows' `order` changes, or `reset` does -- not after every render:
 *  each read is a layout of the whole list. */
function useFlip(box: RefObject<HTMLElement | null>, reset: string, order: string) {
  const pos = useRef<Map<string, number>>(new Map());
  const was = useRef(reset);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const items = Array.from(el.querySelectorAll<HTMLElement>('[data-sid]'));
    const prev = pos.current;
    const next = new Map<string, number>();
    for (const it of items) next.set(it.dataset.sid ?? '', it.offsetTop);
    pos.current = next;
    const same = was.current === reset;
    was.current = reset;
    if (!same || !prev.size || reducedMotion()) return;
    const fresh = items.filter((it) => !prev.has(it.dataset.sid ?? ''));
    if (fresh.length > items.length / 2) return; // a different list, not a re-read
    for (const it of items) {
      const before = prev.get(it.dataset.sid ?? '');
      const dy = before == null ? 0 : before - it.offsetTop;
      if (before != null && Math.abs(dy) >= 1 && it.animate) {
        for (const a of it.getAnimations()) a.cancel();
        it.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], {
          duration: 650,
          easing: 'cubic-bezier(.2,.75,.25,1)',
        });
      }
      if (before == null || dy > 0) {
        it.classList.remove('tj-up');
        void it.offsetWidth; // restart the flash
        it.classList.add('tj-up');
      }
    }
  }, [box, reset, order]);
}

/** `host|2026-10-11|agent|apid:561705` as `10-11` and `apid:561705`. */
export const TrajectoriesTab = memo(function TrajectoriesTab({
  traj,
  rank,
  setRank,
  cls,
  win,
  sel,
  select,
  tfrom,
  setTfrom,
  torder = 'oldest',
  setTorder = () => {},
  tuser = null,
  clearUser = () => {},
  routine = null,
  landing = false,
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
  /** which end of a session the inspector lists first and draws in full */
  torder?: TrajOrder;
  setTorder?: (o: TrajOrder) => void;
  /** narrowed to this user's sessions (a portrait's "every session" on the Users tab, or a link to the old Sessions tab) */
  tuser?: string | null;
  clearUser?: () => void;
  /** the site's routine commands, which the inspector can hide */
  routine?: string[] | null;
  /** the page is about to go to the inspector itself (App.tsx `land`), so it does not scroll itself as it opens */
  landing?: boolean;
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

  const listRef = useRef<HTMLDivElement>(null);
  // The list's rows (memoized) are handed one function for good, which opens
  // through the `select` of the latest render.
  const latestSelect = useRef(select);
  useLayoutEffect(() => {
    latestSelect.current = select;
  });
  const openRow = useCallback((sid: string) => latestSelect.current(sid), []);
  const shownList = tuser ? listed.filter((r) => (r.user ?? (r.users ?? [])[0]) === tuser) : listed;
  // the rows drawn, in their order: a row that moves slides there (`useFlip`)
  const drawn = tuser && shownList.length ? shownList : listed;
  // Nothing picked: the list's first session is open beside it, so the
  // inspector never stands empty asking for a pick; picking one is still the
  // reader's (`sel`, in the URL), and this only shows until they do. It is held
  // while it stays listed: the list is read again every few seconds, and a new
  // session at its top must not swap the tree under the reader.
  const auto = useRef<string | null>(null);
  if (sel) auto.current = null;
  else if (!auto.current || !drawn.some((r) => r.sid === auto.current)) auto.current = drawn[0]?.sid ?? null;
  const cur = sel ?? auto.current;
  useFlip(listRef, `${rank}|${win}|${tuser ?? ''}|${cls.join(',')}`, drawn.map((r) => r.sid).join('\n'));
  const sum = t?.summary ?? {};
  const agent = cls.includes('agent') ? sum.agent : undefined;
  const split = surfaceSplit(agent?.by_surface);
  const shownCls = cls.filter((c) => c in sum);
  const byCls = (f: (d: TrajSummary) => number | null) =>
    Object.fromEntries(shownCls.map((c) => [c, f(sum[c])]));

  return (
    <>
      {/* Every session in the window, not the listed few, and a probe is not a
          session: see `summary` in reducers/trajectories.py. */}
      <div className="plates">
        <Plate
          k={`sessions · ${winLabel(t)}`}
          total={agent?.sessions ?? null}
          by={byCls((d) => d.sessions)}
          tip={
            'an agent and everything it ran, or, for the human and IDE classes, one login or terminal; ' +
            'a one-off probe, such as a script checking `codex --version`, is not one'
          }
          note={
            agent ? (
              <>
                {fint(agent.users)} agent user{agent.users === 1 ? '' : 's'}
                {agent.top_user_share_pct != null
                  ? ` · the busiest makes ${pc(agent.top_user_share_pct)} of agent processes`
                  : ''}
                {agent.probes ? ` · ${fint(agent.probes)} one-off probes set aside` : ''}
                {split && (
                  <>
                    <br />
                    <span data-tip={split.tip}>{split.text}</span>
                  </>
                )}
              </>
            ) : undefined
          }
        />
        <Plate
          k="processes per session · median"
          total={agent?.calls_p50 ?? null}
          by={byCls((d) => d.calls_p50)}
          note={
            agent?.calls_p50 != null
              ? `p90 ${fint(agent.calls_p90)} · the busiest ran ${fint(agent.calls_max)}`
              : undefined
          }
        />
        <Plate
          k="session length · median"
          total={agent?.wall_p50_s ?? null}
          by={byCls((d) => d.wall_p50_s)}
          fmtFn={(n) => dur(n)}
          tip="first to last process INSIDE the window: never longer than the window, and an idle end is not counted"
          note={
            agent?.longest
              ? `longest ${dur(agent.longest.wall_s)} · ${agent.longest.user ?? '?'}, ${
                  AGENT_TYPE_LBL[agent.longest.agent_type ?? ''] ?? agent.longest.agent_type ?? 'agent'
                }, ${fint(agent.longest.tool_calls)} processes`
              : undefined
          }
        />
        <Plate
          k="processes spent waiting"
          total={agent?.wait_pct ?? null}
          by={byCls((d) => d.wait_pct)}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip="processes that only wait: ps, pgrep, top, sleep loops, squeue, sacct, scontrol, watch"
          note={agent ? `of ${fint(agent.tool_calls)} processes in real agent sessions` : undefined}
        />
      </div>

      {/* The ranked list and the inspector, side by side (roadmap note 39.2):
          pick a session on the left, its process tree opens on the right. */}
      <div className="tj-md">
        <Panel
          title="Sessions"
          id="sec-ranked-sessions"
          p={env}
          empty={!listed.length}
          emptyWhat={
            Object.keys(rows).length
              ? 'no session at this rank matches the class filter'
              : 'no session was read in this window'
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
                tip: RANK_TIP[r],
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
          <div className="tj-list" ref={listRef}>
            {drawn.map((r) => (
              <SessionItem key={r.sid} r={r} rank={rank} on={cur === r.sid} onOpen={openRow} />
            ))}
          </div>
        </Panel>

        <div>
          {cur ? (
            <OpenSession
              sel={cur}
              row={rows[cur] ?? null}
              win={win}
              windowMin={t?.window?.minutes}
              select={select}
              tfrom={tfrom}
              setTfrom={setTfrom}
              torder={torder}
              setTorder={setTorder}
              routine={routine}
              // shown by default, not opened: the page stays where it is
              scrollOnOpen={!landing && sel != null}
            />
          ) : (
            <div className="tj-empty">
              <Icon name="pointLeft" size={20} />
              <span>pick a session from the list: its process tree opens here</span>
            </div>
          )}
        </div>
      </div>
    </>
  );
});
