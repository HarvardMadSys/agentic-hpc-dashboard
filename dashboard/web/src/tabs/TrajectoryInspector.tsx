/* The trajectory inspector: one ranked session, opened as its process tree.
 *
 * The row's own summary -- the purpose ribbon and the tool tokens -- stays on
 * top, because it is the session at a glance. Below it is what the summary
 * compresses: every process, nested under whoever started it, in start order.
 *
 * The tree is a re-read of the day-files, so it says what it read and where it
 * stopped. A read that hit a budget says so above the tree, not in a tooltip:
 * a tree that covers the first forty minutes of a four-hour session and looks
 * complete is the kind of picture this dashboard exists not to draw.
 *
 * Between the two sits the navigator (TrajectoryNav.tsx), and every way of
 * picking a moment -- a click on its strip, a quick jump, a ribbon run or a
 * tool token here or in the ranked table -- becomes one `Pending` jump that
 * this component carries out: read the page holding it if this read does not
 * (the `tfrom` the "continue" button sets), open the rows above the one it
 * lands on, scroll the tree to it and light it up, and say on the line under
 * the quick jumps what it landed on -- the process itself, or the row that
 * counts it when it is counted rather than drawn.
 */
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { sessionName } from '../lib/sessions';
import type { ReactNode } from 'react';
import { useTrajectory } from '../api/hooks';
import type { OwnerVia, ProcNode, TrajRow, TrajectoryDetail } from '../api/types';
import { PROC_LINKS } from '../api/types';
import { Ribbon } from '../charts/primitives';
import {
  ProcTree,
  LINK_LBL,
  LINK_TIP,
  allOpen,
  ancestors,
  defaultOpen,
  locate,
  spots,
} from '../components/ProcTree';
import type { Found, JumpReq } from '../components/ProcTree';
import { DEFAULT_ROUTINE, ProcDiagram, hiddenCount } from '../components/ProcDiagram';
import { Segmented } from '../components/ClassFilter';
import { Basis, Legend, Panel } from '../components/Panel';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { serverNow } from '../lib/clock';
import { DASH, bytes, clock, dur, fint, fmt } from '../lib/format';
import { pcol, plbl, purposeLegend } from '../lib/purposes';
import { AP_LABEL, SB_LABEL, label } from '../lib/sandbox';
import { TrajectoryNav } from './TrajectoryNav';
import type { Quick } from './TrajectoryNav';
import './trajectories.css';

/** A moment to go to, from wherever it was picked. */
export interface Pending extends JumpReq {
  /** what it is, for the line that says where the jump landed */
  what: string;
  /** a range to zoom the strip and the timeline to as well */
  zoom?: [number, number];
  /** its index in the read's failures, for prev / next */
  fail?: number;
  /** set once the page holding `t` was asked for: a jump reads one page at most */
  paged?: boolean;
  /** the read it was asked from: it waits until a different read is in */
  stamp?: string;
  /** once the page is read, go to its first failure at or after `t` instead */
  then?: 'first-failure';
}

/** A ribbon run or a tool token picked in the ranked table, for the inspector
 *  to go to once it has read the tree; `t` null: the run's start is unknown. */
export type RunJump = Omit<Pending, 't' | 'by'> & { t: number | null; n: number };

/** The jump to run `i` of a row's purpose ribbon or tool tokens. Its time is
 *  when the run's first record was written, which is when that process
 *  exited; the purpose or the tool says which of the processes exiting then. */
export function runJump(r: TrajRow, kind: 'purpose' | 'tool', i: number, n: number): RunJump {
  const rle = kind === 'purpose' ? r.purpose_rle : r.tool_rle;
  const ts = kind === 'purpose' ? r.purpose_t : r.tool_t;
  const [k, cnt] = rle[i] ?? ['?', 0];
  return {
    t: ts?.[i] ?? null,
    n,
    ...(kind === 'purpose' ? { purpose: k } : { tool: k }),
    what: `${kind === 'purpose' ? plbl(k) : k} ×${fint(cnt)}, run ${fint(i + 1)} of the ${
      kind === 'purpose' ? 'purpose ribbon' : 'tool runs'
    }`,
  };
}

/** The tooltip of a pickable run: where it goes, or why it cannot. */
export function runTip(t: number | null | undefined, where: string): string {
  return t != null
    ? `click: ${where} at ${clock(t)}, where this run began`
    : `this run's start time is unknown: opens ${where === 'open the tree' ? 'the tree' : 'the inspector'} without jumping`;
}

/* What each attribution means, for the keys column and the fold table. */
export const VIA_TIP: Record<OwnerVia, string> = {
  self: 'the key process is itself the agent',
  ancestor: 'walked up the process tree to the nearest agent',
  parent: 'the parent of a tool-call shell, embedded tool or Codex sandbox, never itself seen',
  tree_root: 'fell back to the top-most agent the collector recorded for the tree',
  root: 'not an agent, with nothing above it to fold into',
  unknown: 'the key process was never seen in the window, so it is left as its own session',
};

/** `pgrep x120 > git > rg x3 ...` -- the tool tokens as text, bounded. With
 *  `onPick`, each token is a button for the run it stands for. */
export function ToolTokens({
  rle,
  truncated,
  max = 14,
  onPick,
  times,
  where = 'open the inspector',
}: {
  rle: [string, number][];
  truncated: boolean;
  max?: number;
  onPick?: (i: number) => void;
  /** when each run began, for the tooltip */
  times?: (number | null)[];
  where?: string;
}) {
  const head = rle.slice(0, max);
  return (
    <span className="rle">
      {head.map(([t, n], i) => (
        <span key={i}>
          {i > 0 && <span className="sep">{' › '}</span>}
          {onPick ? (
            <button
              className="tok"
              onClick={(e) => {
                e.stopPropagation();
                onPick(i);
              }}
              data-tip={`${t} ×${fint(n)} · run ${fint(i + 1)}\n${runTip(times?.[i], where)}`}
            >
              <b>{t}</b>
              {n > 1 && <span> {'×'}{n}</span>}
            </button>
          ) : (
            <>
              <b>{t}</b>
              {n > 1 && <span> {'×'}{n}</span>}
            </>
          )}
        </span>
      ))}
      {(rle.length > max || truncated) && (
        <span className="sep" data-tip={truncated ? 'the backend truncated this chain' : `${rle.length - max} more runs`}>
          {' …'}
        </span>
      )}
    </span>
  );
}

/* The seconds a read has been running: real elapsed time, not a progress
 * estimate -- the service cannot know how far through a session it is. */
function useElapsed(on: boolean): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!on) return;
    const t0 = Date.now();
    setT(0);
    const iv = setInterval(() => setT(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(iv);
  }, [on]);
  return t;
}

function Facts({
  row,
  d,
  onRun,
}: {
  row: TrajRow | null;
  d: TrajectoryDetail | null;
  onRun: (kind: 'purpose' | 'tool', i: number) => void;
}) {
  const r = row ?? d?.row ?? null;
  if (!r) return null;
  const calls = r.purpose_rle?.reduce((a, [, k]) => a + k, 0) ?? 0;
  const cls = r.actor3 ?? 'unlabeled';
  return (
    <>
      <div className="ins-id">
        <span>
          <span className="dot" style={{ background: CLS_COL[cls] ?? 'var(--sg)', marginRight: 5 }} />
          {CLS_LBL[cls] ?? cls}
        </span>
        {r.agent_type && <span className="mono">{r.agent_type}</span>}
        <span className="mono">
          {r.user ?? DASH}@{r.host ?? DASH}
        </span>
        {r.agent_pid && <span className="mono">agent pid {r.agent_pid}</span>}
        <span className="sid" data-tip={r.sid}>{sessionName(r).key}</span>
      </div>
      <div className="ins-facts">
        <span>
          events <b>{fint(r.n_events)}</b>
        </span>
        <span>
          cpu <b>{fmt(r.cpu_s)}</b> s
        </span>
        <span>
          wall <b>{dur(r.wall_s)}</b>
        </span>
        <span>
          tools <b>{fint(r.distinct_tools)}</b>
        </span>
        <span data-tip={r.owner_via ? `${r.owner_via}: ${VIA_TIP[r.owner_via]}` : undefined}>
          keys <b>{fint(r.fragments ?? 1)}</b>
          {r.owner_via && r.owner_via !== 'self' && r.owner_via !== 'ancestor' ? ` ${r.owner_via}` : ''}
        </span>
        <span>
          sandbox <b>{label(SB_LABEL, r.sandbox ?? 'unknown')}</b>
        </span>
        <span>
          approval <b>{label(AP_LABEL, r.approval ?? 'unknown')}</b>
        </span>
      </div>
      <div className="ins-summary">
        <span className="eyebrow">purpose</span>
        {r.purpose_rle?.length ? (
          <div className="ins-ribbon">
            <Ribbon
              rle={r.purpose_rle}
              colour={pcol}
              label={(k, n, i) =>
                `${plbl(k)} ×${fint(n)} · run ${fint(i + 1)}\n${runTip(r.purpose_t?.[i], 'open the tree')}`
              }
              W={640}
              H={12}
              zoom
              note={calls < r.n_events ? `(the first ${fint(r.purpose_rle.length)} runs of ${fint(r.n_events)} calls)` : undefined}
              name="purpose runs"
              onPick={(i) => onRun('purpose', i)}
            />
          </div>
        ) : (
          <span className="nm">no purpose run-lengths</span>
        )}
        <span className="eyebrow">tools</span>
        {r.tool_rle?.length ? (
          <ToolTokens
            rle={r.tool_rle}
            truncated={r.chain_truncated}
            max={24}
            times={r.tool_t}
            where="open the tree"
            onPick={(i) => onRun('tool', i)}
          />
        ) : (
          <span className="nm">no tool run-lengths</span>
        )}
      </div>
      <Legend items={purposeLegend()} />
    </>
  );
}

function ReadLine({
  d,
  from,
  setFrom,
  reread,
}: {
  d: TrajectoryDetail;
  from: number | null;
  setFrom: (t: number | null) => void;
  reread: () => void;
}) {
  const s = d.scan;
  const b = d.bounds;
  if (!s || !b) return null;
  const links = PROC_LINKS.filter((k) => k !== 'ppid' && d.links?.[k]);
  return (
    <div className="ins-read">
      <div>
        {`re-read ${bytes(s.bytes)} of ${fint(s.files)} day-file${s.files === 1 ? '' : 's'} in ${s.elapsed_s.toFixed(1)} s · `}
        <b>{fint(s.records)}</b>
        {` processes, ${clock(s.first)} → ${clock(s.last)}`}
        {b.stubs > 0 && (
          <span data-tip="parents the records name that left no exit record in this read: the agent still running, or a shell that exited outside the span">
            {` · ${fint(b.stubs)} parent${b.stubs === 1 ? '' : 's'} with no exit in the read`}
          </span>
        )}
        {d.cached && (
          <span data-tip="the service keeps the trees it read recently, so going back to a session is instant; a session still running may have done more since">
            {` · kept from a read ${dur(Math.max(0, serverNow() / 1000 - s.computed_at))} ago · `}
            <button className="linkish" onClick={reread}>
              read again
            </button>
          </span>
        )}
      </div>
      {links.length > 0 && (
        <div style={{ color: 'var(--muted)' }}>
          {'parents placed on weaker evidence than their own ppid: '}
          {links.map((k, i) => (
            <span key={k} data-tip={LINK_TIP[k]}>
              {i > 0 ? ' · ' : ''}
              {`${LINK_LBL[k]} ${fint(d.links?.[k] ?? 0)}`}
            </span>
          ))}
        </div>
      )}
      {s.stopped && (
        <div className="ins-bound">
          <span>
            {`the read stopped at ${clock(s.stopped_at)}, at its ${
              s.stopped === 'records' ? `${fint(b.max_records)}-record` : `${bytes(b.max_bytes)}`
            } budget: this tree covers ${clock(s.first)}–${clock(s.stopped_at)} of a session that ran ${clock(
              d.span?.first,
            )}–${clock(d.span?.last)}`}
          </span>
          {s.stopped_at != null && (
            <button className="btn primary" onClick={() => setFrom(s.stopped_at)}>
              {`continue from ${clock(s.stopped_at)} →`}
            </button>
          )}
        </div>
      )}
      {from != null && (
        <div className="ins-bound soft">
          <span>{`reading from ${clock(from)}, past an earlier read's budget; what ran before is not in this tree`}</span>
          <button className="btn" onClick={() => setFrom(null)}>
            ← from the session's start
          </button>
        </div>
      )}
      {b.counted > 0 && (
        <div className="ins-bound soft">
          {`${fint(b.counted)} processes are counted under their parent rather than drawn (⋯ rows): past ${fint(
            b.per_parent,
          )} childless processes of one command under one parent, or ${fint(b.hold)} in all`}
        </div>
      )}
      {b.elided > 0 && (
        <div className="ins-bound soft">
          {`${fint(b.elided)} processes are past the ${fint(b.nodes)}-row budget, allotted breadth first: counted in ⋯ rows at the deepest levels`}
        </div>
      )}
    </div>
  );
}

/** A failure's exit, as the tree's exit column says it. */
const failWhat = (f: [number | null, number | null, number | null, string | null, number | null, number | null]) =>
  `${f[3] ?? '?'} · ${f[5] != null ? `signal ${f[5]}` : `exit ${f[4]}`}`;

/** What a jump landed on, when it is not the process itself. */
function landing(w: Pending, f: Found): string {
  if (f.kind === 'agg') return ' · counted under its parent, not drawn: opened at the ⋯ row that counts it';
  if (f.kind === 'more') return ' · past the rows the inspector serves: opened at the ⋯ row that counts it';
  if (f.kind === 'rest') return " · one of a group's members past the first few: opened at the group's count";
  if (!f.exact && (w.pid != null || w.purpose != null || w.tool != null))
    return ' · that process is not drawn here: opened at the nearest row';
  return '';
}

export function TrajectoryInspector({
  sid,
  row,
  win,
  from,
  setFrom,
  onClose,
  jump,
  routine,
}: {
  sid: string;
  /** the ranked row, when the ranking holds it: shown while the tree is read */
  row: TrajRow | null;
  win: number | null;
  from: number | null;
  setFrom: (t: number | null) => void;
  onClose: () => void;
  /** a run picked in the ranked table; `n` changes with every pick */
  jump?: RunJump | null;
  /** the site's routine commands, hidden from the tree on request (`ui.routine_commands`) */
  routine?: string[] | null;
}) {
  // "read again": a bound of now is a different read from the one the service
  // kept, and is that read's own key there. Cleared by paging, which reads anew.
  const [to, setTo] = useState<number | null>(null);
  const page = (t: number | null) => {
    setTo(null);
    setFrom(t);
  };
  const q = useTrajectory(sid, win, from, to);
  const d = q.data && q.data.sid === sid && !q.loading ? q.data : null;
  const elapsed = useElapsed(q.loading);
  const tree: ProcNode | null = d?.found ? (d.tree ?? null) : null;
  const [open, setOpen] = useState<Set<string>>(() => new Set(['r']));
  // The tree view (roadmap notes 38 and 39): boxes, opened from the top, with
  // the routine taken out unless asked for. The table keeps every jump.
  const [mode, setMode] = useState<'tree' | 'table'>('tree');
  const [hide, setHide] = useState(true);
  const [dopen, setDopen] = useState<Set<string>>(() => new Set(['r']));
  const [dall, setDall] = useState<Set<string>>(() => new Set());
  const rules = routine?.length ? routine : DEFAULT_ROUTINE;
  const [view, setView] = useState<[number, number] | null>(null);
  // The tree follows the zoom a beat behind the strip: a fully expanded tree is
  // thousands of rows, and the strip must not wait for them on every frame.
  const treeView = useDeferredValue(view);
  const [flash, setFlash] = useState<{ id: string; n: number } | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [failIdx, setFailIdx] = useState<number | null>(null);
  const [note, setNote] = useState<ReactNode>(null);
  const [want, setWant] = useState<Pending | null>(null);
  const stampKey = `${sid}|${d?.scan?.computed_at ?? ''}`;
  useEffect(() => {
    if (tree) setOpen(defaultOpen(tree));
    setDopen(new Set(['r']));
    setDall(new Set());
    // a new read is a new axis and new rows: nothing from the last one applies
    setView(null);
    setFlash(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stampKey]);

  // a jump lands on a row of the table: show the table when one is asked for
  useEffect(() => {
    if (want) setMode('table');
  }, [want]);

  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [sid]);

  // A run picked in the ranked table.
  useEffect(() => {
    if (!jump) return;
    if (jump.t == null) {
      setNote(`${jump.what}: its start time is unknown, so the tree is opened without jumping`);
      return;
    }
    setWant({ ...jump, t: jump.t, by: 'end' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump?.n]);

  const index = useMemo(() => (tree ? spots(tree) : []), [tree]);
  const axis: [number, number] | null =
    d?.timeline?.lo != null && d.timeline.hi != null && d.timeline.hi >= d.timeline.lo
      ? [d.timeline.lo, d.timeline.hi]
      : null;
  const fitView = (v: [number, number]): [number, number] | null => {
    if (!axis) return null;
    const s = v[1] - v[0];
    if (s >= axis[1] - axis[0]) return null;
    const lo = Math.min(Math.max(v[0], axis[0]), axis[1] - s);
    return [lo, lo + s];
  };

  // Carry out a jump once there is a tree to land in. Declared after the
  // effect that resets the rows for a new read, so on the read a jump paged
  // in, the rows are reset first and the jump's opened on top of them.
  useEffect(() => {
    if (!want || !d?.found || !tree || q.loading || want.stamp === stampKey) return;
    const s = d.scan;
    if (!want.paged && s) {
      const past = !!s.stopped && s.stopped_at != null && want.t > s.stopped_at;
      const before = from != null && want.t < s.from - 1;
      if (past || before) {
        setWant({ ...want, paged: true, stamp: stampKey });
        setNote(`${want.what}: ${clock(want.t)} is not in this read, so the page that starts there is read`);
        const first = d.span?.first;
        page(before && first != null && want.t <= first ? null : Math.floor(want.t));
        return;
      }
    }
    let w = want;
    if (w.then === 'first-failure') {
      const items = d.nav?.failures.items ?? [];
      const i = items.findIndex((f) => f[0] != null && f[0] >= want.t - 1);
      if (i < 0) {
        setWant(null);
        setNote(`no failure in the read from ${clock(want.t)}`);
        return;
      }
      w = failReq(i) ?? w;
    }
    setWant(null);
    const found = locate(index, w);
    if (!found) {
      setNote(`${w.what}: nothing in the tree to land on`);
      return;
    }
    setOpen((o) => {
      const next = new Set(o);
      for (const id of ancestors(found.id)) next.add(id);
      return next;
    });
    setFlash((f) => ({ id: found.id, n: (f?.n ?? 0) + 1 }));
    setCursor(w.t);
    setFailIdx(w.fail ?? null);
    if (w.zoom) setView(fitView(w.zoom));
    else
      setView((v) => (v && (w.t < v[0] || w.t > v[1]) ? fitView([w.t - (v[1] - v[0]) / 2, w.t + (v[1] - v[0]) / 2]) : v));
    setNote(
      <>
        <b>{`→ ${clock(w.t)}`}</b>
        {` · ${w.what}${landing(w, found)}`}
      </>,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want, d, tree, q.loading]);

  const nav = d?.nav ?? null;
  const fl = nav?.failures ?? null;
  const failReq = (i: number): Pending | null => {
    const f = fl?.items[i];
    if (!f || f[0] == null || !fl) return null;
    return {
      t: f[0],
      by: 'start',
      pid: f[2],
      fail: i,
      what: `failure ${fint(i + 1)} of ${fint(fl.n)}${
        fl.listed < fl.n ? ` (the first ${fint(fl.listed)} are listed)` : ''
      } · ${failWhat(f)}`,
    };
  };
  const go = (p: Pending | null) => (p ? () => setWant(p) : undefined);

  // prev / next step through the failures from the last one gone to, or from
  // the last moment gone to when that was not a failure.
  const items = fl?.items ?? [];
  let nextI: number | null = null;
  let prevI: number | null = null;
  if (failIdx != null) {
    nextI = failIdx + 1 < items.length ? failIdx + 1 : null;
    prevI = failIdx > 0 ? failIdx - 1 : null;
  } else if (cursor != null) {
    const n = items.findIndex((f) => f[0] != null && f[0] > cursor + 5e-4);
    nextI = n >= 0 ? n : null;
    for (let i = items.length - 1; i >= 0; i--) {
      const t = items[i][0];
      if (t != null && t < cursor - 5e-4) {
        prevI = i;
        break;
      }
    }
  } else if (items.length) nextI = 0;
  const s = d?.scan;
  const nextPage =
    nextI == null && s?.stopped && s.stopped_at != null && (!fl || fl.listed >= fl.n)
      ? s.stopped_at
      : null;
  const quick: Quick[] = nav
    ? [
        {
          key: 'tool',
          lbl: 'first tool call',
          run: go(
            nav.first_tool?.t != null
              ? {
                  t: nav.first_tool.t,
                  by: 'start',
                  pid: nav.first_tool.pid,
                  what:
                    nav.first_tool.via === 'tool'
                      ? `first tool call · ${nav.first_tool.cmd ?? '?'}`
                      : `first process · ${nav.first_tool.cmd ?? '?'} (no tool-call shell, sandbox or embedded tool in this read)`,
                }
              : null,
          ),
          tip: 'the first tool-call shell, sandbox wrapper or embedded tool to start in this read; the first process when there is none',
        },
        {
          key: 'busy',
          lbl: 'busiest minute',
          run: go(
            nav.busiest
              ? {
                  t: nav.busiest.t,
                  by: 'start',
                  zoom: [nav.busiest.t, nav.busiest.t + 60],
                  what: `busiest minute · ${fint(nav.busiest.n)} processes started in it`,
                }
              : null,
          ),
          tip: 'the clock minute in which most processes started, counted ones included; zooms to it and opens its first',
        },
        {
          key: 'long',
          lbl: 'longest process',
          run: go(
            nav.longest?.t != null
              ? {
                  t: nav.longest.t,
                  by: 'start',
                  pid: nav.longest.pid,
                  what: `longest process · ${nav.longest.cmd ?? '?'} · ran ${dur(nav.longest.dur)}`,
                }
              : null,
          ),
          tip: 'the process that ran longest in this read, the session root aside',
        },
        {
          key: 'fail',
          lbl: fl?.n ? `first failure (${fint(fl.n)})` : 'first failure',
          run: go(failReq(0)),
          tip: fl?.n
            ? `${fint(fl.n)} exits with a non-zero code or a signal in this read${
                fl.listed < fl.n ? `; the first ${fint(fl.listed)} are listed` : ''
              }`
            : 'no exit with a non-zero code or a signal in this read',
        },
        {
          key: 'prev',
          lbl: '‹ prev failure',
          run: go(prevI != null ? failReq(prevI) : null),
          tip: prevI != null ? `failure ${fint(prevI + 1)}` : 'no earlier failure in this read',
        },
        {
          key: 'next',
          lbl: 'next failure ›',
          run:
            nextI != null
              ? go(failReq(nextI))
              : nextPage != null
                ? go({
                    t: nextPage + 1e-3,
                    by: 'start',
                    then: 'first-failure',
                    what: 'next failure, in the next page',
                  })
                : undefined,
          tip:
            nextI != null
              ? `failure ${fint(nextI + 1)}`
              : nextPage != null
                ? `none later in this read: reads the next page, from ${clock(nextPage)}`
                : fl && fl.listed < fl.n
                  ? `the read lists the first ${fint(fl.listed)} of ${fint(fl.n)} failures`
                  : 'no later failure in this read',
        },
      ]
    : [];

  const inView = useMemo(() => {
    if (!treeView) return null;
    let k = 0;
    for (const { e } of index) {
      if (e.k !== 'p' || e.start == null) continue;
      if (e.start <= treeView[1] && (e.end ?? e.start) >= treeView[0]) k++;
    }
    return k;
  }, [index, treeView]);

  const onRun = (kind: 'purpose' | 'tool', i: number) => {
    const r = row ?? d?.row;
    if (!r) return;
    const j = runJump(r, kind, i, 0);
    if (j.t == null) setNote(`${j.what}: its start time is unknown, so there is nowhere to jump`);
    else setWant({ ...j, t: j.t, by: 'end' });
  };

  const n = row?.n_events ?? null;
  const env = d && d.found !== false ? d : null;

  return (
    <div ref={ref} className="span2" style={{ scrollMarginTop: 12 }}>
      <Panel
        title="Session inspector — process tree"
        p={env}
        feed="ebpf"
        empty={d?.found === false}
        emptyWhat={d?.why ?? 'not in this window'}
        header={
          <div className="ins-tools">
            {tree && (
              <>
                <Segmented
                  value={mode}
                  onChange={setMode}
                  options={[
                    { k: 'tree', lbl: 'tree', tip: 'boxes joined by lines, opened a level at a time' },
                    { k: 'table', lbl: 'table', tip: 'every process as a row, with its timeline; landmarks and the strip jump here' },
                  ]}
                />
                <button className="btn" onClick={() => (mode === 'tree' ? setDopen(allOpen(tree)) : setOpen(allOpen(tree)))}
                  data-tip="open every process the read served">
                  expand all
                </button>
                <button className="btn" onClick={() => (mode === 'tree' ? setDopen(new Set(['r'])) : setOpen(new Set(['r'])))}>
                  collapse
                </button>
              </>
            )}
            <button className="btn" onClick={onClose} aria-label="close the inspector">
              close ×
            </button>
          </div>
        }
        basis={
          d?.found ? (
            <Basis
              items={[
                ...Object.entries(d.basis ?? {}),
                ['scan', d.scan],
                ['bounds', d.bounds],
                ['links', d.links],
                ['timeline', 'bars span start to exit on the read’s own time axis, or on the range zoomed to on the strip above; a dashed bar has no exit in the read, a bar with a left rule started before the axis, one with a right rule ran past it'],
                ['jumps', 'a moment on the strip opens the first process started at or after it; a ribbon run or a tool token opens the process its first record came from, matched by exit time and purpose or tool. A process that is counted rather than drawn opens at the row that counts it'],
              ]}
            />
          ) : undefined
        }
      >
        <Facts row={row} d={d} onRun={onRun} />
        {q.error && <div className="err">{`/api/trajectory: ${q.error}`}</div>}
        {q.loading && (
          <div className="building">
            <div className="hd">
              <span className="pill">
                <i className="ld" />
                reading
              </span>
              <span className="mono">{`${elapsed} s`}</span>
            </div>
            <p className="paths">
              {`reading the session's records${n != null ? ` · ${fint(n)} of them` : ''}`}
              {want ? ` · then going to ${clock(want.t)} (${want.what})` : ''}
            </p>
          </div>
        )}
        {d?.found && <ReadLine d={d} from={from} setFrom={page} reread={() => setTo(Math.ceil(serverNow() / 1000))} />}
        {d?.found && axis && (
          <TrajectoryNav
            d={d}
            axis={axis}
            view={view}
            setView={setView}
            cursor={cursor}
            onPick={(t) =>
              setWant({ t, by: 'start', what: `the first process started at or after ${clock(t)}` })
            }
            onFail={(i) => go(failReq(i))?.()}
            quick={quick}
            status={note ?? <span className="nm">{`pick a moment on the strip, or a landmark ${DASH} the tree opens there`}</span>}
            inView={inView}
          />
        )}
        {tree && mode === 'tree' && (
          <>
            <div className="pd-bar">
              <label data-tip="take out runs of the same command and the site's routine commands (ui.routine_commands), so the unusual processes stand out">
                <input type="checkbox" checked={hide} onChange={(e) => setHide(e.target.checked)} />
                hide routine
              </label>
              {hide && (
                <span className="nm">
                  {`${fint(hiddenCount(tree, rules))} routine processes hidden`}
                </span>
              )}
              <span className="nm">· click a box to open what it started · hover for its command</span>
            </div>
            <ProcDiagram root={tree} open={dopen} setOpen={setDopen} hide={hide} rules={rules} all={dall} setAll={setDall} />
          </>
        )}
        {tree && mode === 'table' && (
          <ProcTree root={tree} open={open} setOpen={setOpen} timeline={d?.timeline} view={treeView} flash={flash} />
        )}
        {tree && (
          <div className="pt-legend">
            <span>
              <span className="pt-tl">
                <i className="pt-bar" style={{ left: 0, width: '100%' }} />
              </span>
              ran, exit 0
            </span>
            <span>
              <span className="pt-tl">
                <i className="pt-bar bad" style={{ left: 0, width: '100%' }} />
              </span>
              non-zero exit
            </span>
            <span>
              <span className="pt-tl">
                <i className="pt-bar sig" style={{ left: 0, width: '100%' }} />
              </span>
              killed by a signal
            </span>
            <span>
              <span className="pt-tl">
                <i className="pt-bar open" style={{ left: 0, width: '100%' }} />
              </span>
              no exit in the read
            </span>
            <span>
              <span className="pt-tl">
                <i className="pt-bar group" style={{ left: 0, width: '100%' }} />
              </span>
              a group or a count
            </span>
            <span className="pt-tag weak">dashed tag</span>
            <span>a parent link weaker than the record's own ppid</span>
          </div>
        )}
      </Panel>
    </div>
  );
}
