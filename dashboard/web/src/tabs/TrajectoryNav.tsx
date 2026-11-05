/* The inspector's navigator: find a moment of the session, then go there.
 *
 * Two charts above the process tree, one gesture vocabulary with every chart
 * on this page (charts/primitives.tsx: wheel or pinch zooms around the cursor,
 * a drag pans once zoomed, a double-click or `reset zoom` goes back):
 *
 * - the SESSION track is the session's whole span, first record to last, with
 *   the stretch this read covers drawn solid and the rest hatched as not read.
 *   A long session is read in pages (the read stops at a budget); clicking an
 *   unread stretch reads the page that starts there.
 * - the CHART is the read's own axis, the one the tree's timeline column uses:
 *   process starts per time bin, stacked by purpose. Zooming it zooms the
 *   tree's timeline column with it and marks the rows that ran in range;
 *   clicking a moment opens the tree at the first process started at or after
 *   it. The bins come from every record the read matched, itemised or only
 *   counted (`nav` in rc_dashboard/proctree.py), so a poll loop the tree shows
 *   as one counted row still shows here as the activity it was.
 *
 * For an agent whose commands are read (lib/phases.ts) the chart also says what
 * the agent was doing, in the same picture and on the same axis. Each phase is a
 * band as tall as the bars, tinted in its colour behind them and named at its top
 * where the name fits. A guess is hatched, a guessed slice of any phase is hatched
 * where it lies, and idle has no tint but a dashed outline. A session running now
 * ends in its live edge where the view reaches it. The agent's own housekeeping
 * commands are a thin row of ticks under the bars, and under the chart's axis a
 * legend names the tints: a swatch for each kind of phase whose bands fall on the
 * read's axis, and for the hatching, drawn as the bands are. It is the read's, not
 * the zoom's, so it changes only when the read pages, and under it a line says where
 * the phases the read does not hold are (in the list below). One hover line crosses all of
 * the chart, and its tooltip names the phase under it and counts the bar. Clicking a
 * phase's name picks the phase, as a row of the list under the navigator does
 * (`onPhase`, from the inspector): the chart zooms to it and the tree opens at its
 * first call. Clicking anywhere else picks the moment. The chart is the read's, so
 * a long session read in pages draws its phases a page at a time; the session
 * track above it and the list below it cover the whole session.
 *
 * Above the bands, where the block has tasks (lib/tasks.ts), is a lane of them on the same axis. Each task is a
 * block, from its own start to its own end, in the colours of a selected row of the ranked table (not the crimson,
 * which is the wait phase's): named by its label where that fits, else numbered by its place among the tasks, else
 * bare. A lone stretch is no task, and is a dashed outline with no word. A block's tooltip names the task and says
 * what it holds (a lone stretch says its own line), and clicking one picks the task (`onTask`, from the inspector):
 * the chart zooms to it, as it does to a phase. The lane is placed by the rule the bands are (`placeBand`), so a
 * block lines up with the bands under it.
 *
 * The three tracks -- the session track above, this chart, and the zoom map under it -- all start after one left gutter,
 * as the mockup draws its label column (`GUTTER`, lib/phases.ts), so their axes line up and no caption is over a band
 * or a bar. The gutter holds the captions of the levels, in the page's names for them and the caption row's own style:
 * Tasks beside the lane, Phases beside the row of the phases' names, and Processes beside the bars. The Phases caption is
 * the glossary's `phases` term, as the basis line's first word is its `tasks`. A block with no
 * tasks has no Tasks caption and keeps the gutter, so the chart does not move between rows. The gutter is outside the
 * drawing: the chart is drawn as wide as it leaves, and stands beside it, so every x the chart works out (a hover, a
 * click, a drag, a tick) is in the drawing's own units, as it always was.
 *
 * Failures are ticks under the bars, red for a signal and pink for a non-zero
 * exit -- the timeline bars' own two colours. The quick jumps below go to the
 * landmarks the read found; what they landed on, and whether it is the process
 * itself or the row that counts it, is said on the line under them.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
  RefObject,
} from 'react';
import type { Phase, PhaseBlock, PhaseHousekeeping, Task, TrajNav, TrajRunning, TrajectoryDetail } from '../api/types';
import { ZOOM_HINT, ZoomMap, ZoomNote, svgAt, useWheel, wheelFactor, wheelPan } from '../charts/primitives';
import { Term } from '../components/Term';
import { serverNow } from '../lib/clock';
import { DASH, clock, dur, fint } from '../lib/format';
import {
  GAP,
  GUTTER,
  LABEL_CHAR_W,
  LABEL_PAD,
  LEVELS,
  chartLevels,
  edgeLabel,
  edgeSpan,
  edgeTip,
  housekeepingText,
  labelFits,
  onAxis,
  phaseAt,
  phaseCol,
  phaseLegend,
  phaseShort,
  phaseTip,
  phasesOutside,
  placeBand,
  plotW,
  sequenceLine,
  sliceAt,
  sliceTip,
  zoomTip,
} from '../lib/phases';
import type { LegendEntry, Level } from '../lib/phases';
import { PURP, pcol, plbl } from '../lib/purposes';
import { TASKS_BASIS, laneClause, taskAtX, taskBlocks, taskTip } from '../lib/tasks';

/** Purposes outside the eight colour slots stack as one `other`, as they are coloured. */
const SLOT = new Set(PURP.map(([k]) => k));
const ORDER = PURP.map(([k]) => k);
const slotOf = (p: string) => (SLOT.has(p) ? p : 'other');

/** Tick steps, seconds: a label every ~100 px. */
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

function ticks(v0: number, v1: number, room: number): { t: number; lbl: string }[] {
  const step = STEPS.find((s) => (v1 - v0) / s <= room) ?? STEPS[STEPS.length - 1];
  // On the local clock: an hour tick is a local hour, whatever the offset.
  const off = -new Date(v0 * 1000).getTimezoneOffset() * 60;
  const out: { t: number; lbl: string }[] = [];
  for (let k = Math.ceil((v0 + off) / step); k * step - off <= v1 && out.length < 40; k++) {
    const t = k * step - off;
    out.push({ t, lbl: step >= 60 ? clock(t).slice(0, 5) : clock(t) });
  }
  return out;
}

export const SPAN_MIN = 1; // seconds: the narrowest zoom, whatever the bin width

/** The width the bars are binned for, and the width the session track and a chart with no phases are drawn at.
 *  The bins are the same at every width the chart is shown at: a chart with phases is drawn at its box's own
 *  pixel width, so that its names are 10 px, but its bars are regrouped as for this. */
const BIN_W = 1120;

/** Served bins regrouped into display bins at least ~3 px wide across `w` px,
 *  a whole number of served bins each, so a display bin is still a count of
 *  starts over a stated width; stacked by purpose slot. */
function regroup(den: NonNullable<TrajNav['density']>, purposes: string[], v0: number, v1: number, w: number) {
  const k = Math.max(1, Math.ceil(Math.max(v1 - v0, 1e-6) / den.w / (w / 3)));
  const dw = den.w * k;
  const per = new Map<number, Map<string, number>>();
  for (const [i, p, c] of den.bins) {
    const t = den.lo + i * den.w;
    if (t + den.w < v0 || t > v1) continue;
    const j = Math.floor(i / k);
    const m = per.get(j) ?? new Map<string, number>();
    const key = slotOf(purposes[p] ?? 'other');
    m.set(key, (m.get(key) ?? 0) + c);
    per.set(j, m);
  }
  let top = 0;
  for (const m of per.values()) {
    let s = 0;
    for (const c of m.values()) s += c;
    top = Math.max(top, s);
  }
  return { per, dw, top };
}

/** Stacked bars of `regroup`'s bins, on the x scale `X`, standing on `base`. */
function stackBars(
  g: ReturnType<typeof regroup>,
  lo: number,
  X: (t: number) => number,
  base: number,
  ph: number,
  W: number,
  min = 0.8,
): ReactNode[] {
  const bars: ReactNode[] = [];
  if (g.top <= 0) return bars;
  for (const [j, m] of g.per) {
    const t0 = lo + j * g.dw;
    const x0 = Math.max(0, X(t0));
    const x1 = Math.min(W, X(t0 + g.dw));
    if (x1 <= x0) continue;
    const w = Math.max(min, x1 - x0 - (x1 - x0 > 3 ? 0.6 : 0));
    let y = base;
    for (const key of ORDER) {
      const c = m.get(key);
      if (!c) continue;
      const h = Math.max(min, (ph * c) / g.top);
      y -= h;
      bars.push(<rect key={`${j}-${key}`} x={x0} y={y} width={w} height={h} fill={pcol(key)} />);
    }
  }
  return bars;
}

export interface Quick {
  key: string;
  lbl: string;
  tip: string;
  run?: () => void;
}

/** A box's own width in px as it is laid out, kept as the box is resized; `fallback` before it is first measured. */
function useWidth(on: boolean, fallback: number): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!on || !el) return;
    const read = () => {
      const n = Math.round(el.clientWidth);
      if (n > 0) setW(n);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [on]);
  return [ref, w];
}

/* ------------------------------------------------------------ session track */

/** The session track's axis, `[S0, S1]`: the session's first record to its last, taking in where this read began
 *  and the read's own axis. `cover` is a stretch it must also reach, so that nothing the session's own record
 *  holds is cut: the phases' span (a call is stamped by when it exited, and a phase by when it began, so the first
 *  can begin before the first record), and for a session running now, on to now. */
export function sessionAxis(
  d: TrajectoryDetail,
  axis: [number, number],
  cover?: [number, number] | null,
): [number, number] {
  const first = d.span?.first ?? axis[0];
  const last = d.span?.last ?? axis[1];
  return [
    Math.min(first, axis[0], d.scan?.from ?? axis[0], cover?.[0] ?? Infinity),
    Math.max(last, axis[1], cover?.[1] ?? -Infinity),
  ];
}

function SessionTrack({
  d,
  axis,
  S,
  view,
  onPick,
  W,
}: {
  d: TrajectoryDetail;
  axis: [number, number];
  /** the session's axis: `sessionAxis` */
  S: [number, number];
  view: [number, number] | null;
  onPick: (t: number) => void;
  W: number;
}) {
  const hatch = 'tn' + useId().replace(/[^\w-]/g, '');
  const s = d.scan;
  const [S0, S1] = S;
  const whole = Math.max(S1 - S0, 1e-6);
  // What this read covers: from where it began to where it stopped, or to the
  // session's end when it did not stop.
  const r0 = s?.from ?? axis[0];
  const r1 = s?.stopped && s.stopped_at != null ? s.stopped_at : S1;
  const H = 14;
  const X = (t: number) => ((Math.min(S1, Math.max(S0, t)) - S0) / whole) * W;
  const v = view ?? axis;
  const [hx, setHx] = useState<number | null>(null);
  const tAt = (x: number) => S0 + (Math.min(W, Math.max(0, x)) / W) * whole;
  const partial = X(r0) > 0.5 || X(r1) < W - 0.5;
  const hoverT = hx != null ? tAt(hx) : null;
  const unread = hoverT != null && (hoverT < r0 - 0.5 || hoverT > r1 + 0.5);
  const tip =
    hoverT == null
      ? undefined
      : unread
        ? `${clock(hoverT)} · not in this read: click to read the page that starts here`
        : `${clock(hoverT)} · click to open the tree at the first process started at or after it`;
  return (
    <div className="tn-track">
      <div className="tn-cap">
        <span className="eyebrow">session</span>
        <span>
          {`${clock(S0)} → ${clock(S1)}`}
          {partial ? (
            <>
              {' · this read '}
              <b>{`${clock(r0)} → ${clock(r1)}`}</b>
              {' · hatched: not read'}
            </>
          ) : (
            ' · read whole'
          )}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        preserveAspectRatio="none"
        className="tn-svg"
        role="img"
        aria-label={`the session from ${clock(S0)} to ${clock(S1)}; this read covers ${clock(r0)} to ${clock(r1)}`}
        onPointerMove={(e) => setHx(svgAt(e.currentTarget, e, W, H)?.x ?? null)}
        onPointerLeave={() => setHx(null)}
        onClick={(e) => {
          const p = svgAt(e.currentTarget, e, W, H);
          if (p) onPick(tAt(p.x));
        }}
        data-tip={tip}
        style={{ cursor: 'pointer' }}
      >
        <defs>
          <pattern id={hatch} width="5" height="5" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="5" height="5" fill="var(--surface)" />
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--edge)" strokeWidth="2" />
          </pattern>
        </defs>
        <rect x={0} y={2} width={W} height={H - 4} fill={`url(#${hatch})`} />
        <rect x={X(r0)} y={2} width={Math.max(1.5, X(r1) - X(r0))} height={H - 4} className="tn-read" />
        <rect x={X(v[0])} y={0.75} width={Math.max(2, X(v[1]) - X(v[0]))} height={H - 1.5} className="tn-view" />
        {hx != null && <line x1={hx} x2={hx} y1={0} y2={H} className="tn-hover" />}
      </svg>
    </div>
  );
}

/* ---------------------------------------------------------------- the chart */

/** The basis line above the chart (`TASKS_BASIS`), which covers the tasks and the phases under them. Its first word is
 *  the glossary's `tasks`. */
function BasisRow() {
  const at = TASKS_BASIS.indexOf(' · ');
  return (
    <div className="tn-cap tn-basis">
      <span>
        <Term k="tasks">{TASKS_BASIS.slice(0, at)}</Term>
        {TASKS_BASIS.slice(at)}
      </span>
    </div>
  );
}

/** A legend's swatch, drawn as the chart draws what it stands for. A tint is a rect of the band's own class, so
 *  its colour and its opacity are the band's (`phaseCol`, `.tn-band`); a guess is hatched in the editing colour
 *  with a dashed outline; idle is an empty dashed outline, since a fill would draw it as work. */
const SWATCH_W = 14;
const SWATCH_H = 12;
function Swatch({ look, fill }: Pick<LegendEntry, 'look' | 'fill'>) {
  const hatch = 'tg' + useId().replace(/[^\w-]/g, '');
  const outline = (stroke: string) => (
    <rect x={0.5} y={0.5} width={SWATCH_W - 1} height={SWATCH_H - 1} fill="none" stroke={stroke} strokeDasharray="3 2" />
  );
  return (
    <svg className="tn-swatch" width={SWATCH_W} height={SWATCH_H} viewBox={`0 0 ${SWATCH_W} ${SWATCH_H}`} aria-hidden="true">
      {look === 'fill' && <rect className="tn-band" width={SWATCH_W} height={SWATCH_H} fill={fill} />}
      {look === 'guess' && (
        <>
          <defs>
            <pattern id={hatch} width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <rect width="4" height="4" fill="var(--surface)" />
              <line x1="0" y1="0" x2="0" y2="4" stroke="var(--s5)" strokeWidth="1.6" />
            </pattern>
          </defs>
          <rect width={SWATCH_W} height={SWATCH_H} fill={`url(#${hatch})`} />
          {outline('var(--s5)')}
        </>
      )}
      {look === 'idle' && outline('var(--muted)')}
    </svg>
  );
}

/** What the chart's colours stand for, under its axis: a swatch and a name for each kind of phase whose bands fall on
 *  the read's axis (`phaseLegend`, which names and colours them as the bands are, and takes the axis the chart
 *  draws before any zoom, so it changes only when the read pages), and for the hatching. Nothing where no phase
 *  falls on the axis. */
function PhaseLegend({ list, axis }: { list: Phase[]; axis: [number, number] }) {
  const entries = phaseLegend(list, axis);
  if (!entries.length) return null;
  return (
    <div className="tn-legend" role="list" aria-label="what the colours behind the bars mean">
      {entries.map((e) => (
        <span key={`${e.look} ${e.name}`} className="tn-legend-item" role="listitem">
          <Swatch look={e.look} fill={e.fill} />
          {e.name}
        </span>
      ))}
    </div>
  );
}

/** What a hover on the housekeeping row says: the minute it is over, or what a tick is, and the commands that
 *  have no minute and so no tick. */
function hkTip(hk: PhaseHousekeeping, t: number): string {
  const m = hk.bins.find(([b]) => t >= b && t < b + 60);
  const what = m
    ? `${fint(m[1])} ${m[1] === 1 ? 'command' : 'commands'} of its own began in this minute`
    : 'each tick is a minute in which the agent ran a command of its own';
  return hk.cut > 0 ? `${what}\n${fint(hk.cut)} of the ${fint(hk.n)} commands have no minute, so they have no tick` : what;
}

/** The chart's rows, top to bottom, in the units it is drawn in. A chart with no phases is the strip as it was:
 *  the caption in its first row, the bars, the failures, and the ticks. One with phases has, where the session has
 *  tasks, a lane of them at the top, then a row of names at the top of its bands (where it has any), the bars, a thin
 *  row of the agent's own commands, the failures, and the ticks, and its caption is under it. */
interface Rows {
  H: number;
  /** the task lane at the top: its height, 0 where the session has no tasks */
  task: number;
  /** where the bands begin, under the lane and the space below it: 0 where there is no lane */
  top: number;
  /** the label row at the top of the bands: 0 where there are none */
  lbl: number;
  PH: number;
  BASE: number;
  /** the housekeeping row: its top and height */
  hk: [number, number] | null;
  failY: number;
  /** what the pointer's line spans, and what a cursor's does */
  hover: [number, number];
  cursor: [number, number];
  /** how far down the chart answers the pointer */
  hit: number;
}

/** The task lane's height, and the space between it and the bands. */
const TASK_H = 18;
const TASK_GAP = 3;
const LBL_H = 16;
const BARS_H = 34;
const HK_H = 5;
const FAIL_H = 6;

function rowsOf(merged: boolean, bands: boolean, lane: boolean): Rows {
  if (!merged) {
    const BASE = 45;
    return { H: 62, task: 0, top: 0, lbl: 0, PH: 32, BASE, hk: null, failY: BASE + 2, hover: [13, BASE], cursor: [11, BASE + 8], hit: BASE + 10 };
  }
  const task = lane ? TASK_H : 0;
  const top = lane ? TASK_H + TASK_GAP : 0;
  const lbl = bands ? LBL_H : 0;
  const BASE = top + lbl + BARS_H;
  const failY = BASE + 11;
  const bottom = failY + FAIL_H;
  return { H: BASE + 30, task, top, lbl, PH: BARS_H, BASE, hk: [BASE + 3, HK_H], failY, hover: [0, bottom], cursor: [0, bottom], hit: bottom + 2 };
}

/** A band narrower than this has no outline worth drawing: it is a single dashed line. */
const THIN = 3;
/** The live edge is left out of a chart where less than this much of it is in view. */
const EDGE_MIN_PX = 3;

function Strip({
  nav,
  axis,
  view,
  setView,
  cursor,
  onPick,
  onFail,
  inView,
  W,
  phases,
  now,
  nowS,
  onPhase,
  onTask,
}: {
  nav: TrajNav;
  axis: [number, number];
  view: [number, number] | null;
  setView: (v: [number, number] | null) => void;
  cursor: number | null;
  onPick: (t: number) => void;
  onFail: (i: number) => void;
  /** processes drawn in the tree that ran in the zoomed range */
  inView: number | null;
  /** the width it is drawn at, beside the gutter: its box's own, less the gutter, for a chart with phases, and `BIN_W` for one
   *  without */
  W: number;
  /** what the agent was doing, where its commands are read: the bands behind the bars, and the task lane above them */
  phases?: PhaseBlock;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
  /** the service's present, epoch seconds, where `now` says the session runs */
  nowS?: number | null;
  /** a phase picked by its name */
  onPhase?: (p: Phase) => void;
  /** a task or lone stretch picked by its block in the lane */
  onTask?: (t: Task) => void;
}) {
  const merged = !!phases;
  const list = phases?.list ?? [];
  // a block of the first version has no tasks, and no lane
  const tasks = phases?.tasks ?? [];
  const bands = list.length > 0;
  const R = rowsOf(merged, bands, bands && tasks.length > 0);
  const { H, BASE, PH } = R;
  const bandH = BASE - R.top;
  const hatch = 'ts' + useId().replace(/[^\w-]/g, '');
  const den = nav.density;
  const [a0, a1] = axis;
  const full = Math.max(a1 - a0, 1e-6);
  const zoomed = view != null && view[1] > a0 && view[0] < a1;
  const v0 = zoomed ? Math.max(a0, view[0]) : a0;
  const v1 = zoomed ? Math.min(a1, view[1]) : a1;
  const span = Math.max(v1 - v0, 1e-6);
  const X = (t: number) => ((t - v0) / span) * W;
  const T = (x: number) => v0 + (Math.min(W, Math.max(0, x)) / W) * span;
  const fit = (lo: number, hi: number): [number, number] | null => {
    const s = hi - lo;
    if (s >= full) return null;
    const l = Math.min(Math.max(lo, a0), a1 - s);
    return [l, l + s];
  };
  const zoomAt = (at: number, f: number) => {
    if (!zoomed && f >= 1) return;
    const ns = Math.min(full, Math.max(Math.min(SPAN_MIN, full), span * f));
    const lo = at - ((at - v0) * ns) / span;
    setView(fit(lo, lo + ns));
  };
  const wheel = useWheel((w, svg) => {
    const p = svgAt(svg, w, W, H);
    if (!p) return;
    const slide = wheelPan(w);
    if (slide) {
      if (zoomed) {
        const d = ((slide * p.k) / W) * span;
        setView(fit(v0 + d, v1 + d));
      }
      return;
    }
    zoomAt(T(p.x), wheelFactor(w));
  });

  const shown = useMemo(
    () => (den && den.bins.length ? regroup(den, nav.purposes, v0, v1, BIN_W) : null),
    [den, nav.purposes, v0, v1],
  );
  // the whole axis, for the minimap under a zoomed strip
  const whole = useMemo(
    () => (zoomed && den && den.bins.length ? regroup(den, nav.purposes, a0, a1, BIN_W) : null),
    [zoomed, den, nav.purposes, a0, a1],
  );

  const [hx, setHx] = useState<number | null>(null);
  const [hy, setHy] = useState<number | null>(null);
  const [kc, setKc] = useState<number | null>(null); // the keyboard's cursor, a time
  const grab = useRef<{
    cx: number;
    sx: number;
    v0: number;
    moved: boolean;
    fail: number | null;
    phase: number | null;
    task: number | null;
  } | null>(null);
  const [panning, setPanning] = useState(false);
  const handlers = {
    onPointerDown: (e: ReactPointerEvent<SVGGElement>) => {
      if (e.button !== 0) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      const target = e.target as Element;
      const f = target.getAttribute?.('data-fail');
      const ph = target.getAttribute?.('data-phase');
      const tk = target.getAttribute?.('data-task');
      grab.current = {
        cx: e.clientX,
        sx: p.x,
        v0,
        moved: false,
        fail: f != null ? Number(f) : null,
        phase: ph != null ? Number(ph) : null,
        task: tk != null ? Number(tk) : null,
      };
      if (zoomed && e.pointerType !== 'touch') e.currentTarget.setPointerCapture(e.pointerId);
    },
    onPointerMove: (e: ReactPointerEvent<SVGGElement>) => {
      const p = svgAt(e.currentTarget, e, W, H);
      setHx(p ? p.x : null);
      setHy(p ? p.y : null);
      const g = grab.current;
      if (!g || !p) return;
      if (Math.abs(e.clientX - g.cx) > 3) g.moved = true;
      if (!zoomed || !g.moved) return;
      setPanning(true);
      const d = -((p.x - g.sx) / W) * span;
      setView(fit(g.v0 + d, g.v0 + span + d));
    },
    onPointerUp: (e: ReactPointerEvent<SVGGElement>) => {
      const g = grab.current;
      grab.current = null;
      setPanning(false);
      if (!g || g.moved) return;
      if (g.fail != null) {
        onFail(g.fail);
        return;
      }
      if (g.phase != null && list[g.phase]) {
        onPhase?.(list[g.phase]);
        return;
      }
      if (g.task != null && tasks[g.task]) {
        onTask?.(tasks[g.task]);
        return;
      }
      const p = svgAt(e.currentTarget, e, W, H);
      if (p) onPick(T(p.x));
    },
    onPointerCancel: () => {
      grab.current = null;
      setPanning(false);
    },
    onPointerLeave: () => {
      setHx(null);
      setHy(null);
    },
  };
  const onKey = (e: ReactKeyboardEvent<SVGGElement>) => {
    const at = kc ?? cursor ?? (v0 + v1) / 2;
    const stepT = span / 40;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const t = Math.min(a1, Math.max(a0, at + (e.key === 'ArrowRight' ? stepT : -stepT)));
      setKc(t);
      if (zoomed && (t < v0 || t > v1)) setView(fit(t - span / 2, t + span / 2));
    } else if (e.key === 'Enter' || e.key === ' ') {
      onPick(at);
    } else if (e.key === '+' || e.key === '=' || e.key === '-') {
      zoomAt(at, e.key === '-' ? 1.6 : 1 / 1.6);
    } else if (e.key === '0' || e.key === 'Escape') {
      setView(null);
    } else return;
    e.preventDefault();
  };

  const bars: ReactNode[] = shown && den ? stackBars(shown, den.lo, X, BASE, PH, W) : [];
  const fails = nav.failures.items;
  const ticksOut: ReactNode[] = [];
  fails.forEach((f, i) => {
    const t = f[0];
    if (t == null || t < v0 || t > v1) return;
    ticksOut.push(
      <rect
        key={i}
        x={X(t) - 1}
        y={R.failY}
        width={2.4}
        height={FAIL_H}
        className={f[5] != null ? 'tn-fail sig' : 'tn-fail'}
        data-fail={i}
        data-tip={`${clock(t)} · ${f[3] ?? '?'} · ${f[5] != null ? `signal ${f[5]}` : `exit ${f[4]}`} · click to open it`}
      />,
    );
  });

  // The agent's own commands, a tick for each minute one began in: one path, since a week of minutes is some
  // ten thousand of them.
  const hkPath = useMemo(() => {
    if (!phases) return '';
    const tw = Math.max(1.5, (60 / span) * W);
    let d = '';
    for (const [t] of phases.housekeeping.bins) {
      if (t + 60 <= v0 || t >= v1) continue;
      const x = Math.min(W - tw, Math.max(0, X(t)));
      d += `M${x.toFixed(1)} 0h${tw.toFixed(1)}v${HK_H}h${(-tw).toFixed(1)}z`;
    }
    return d;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phases, v0, v1, W, span]);

  // The task lane's blocks on this view, worked out once: they are drawn, they take the click (a hit rect each), and they are
  // what the pointer is over (`taskAtX`), so that the hover and the click agree about a block as narrow as a pixel.
  const laneBlocks = R.task ? taskBlocks(tasks, [v0, v1], W) : [];
  // The captions of the levels, in the gutter beside the row each names, centred on it: the task lane, the row of the phases'
  // names, and the bars. The bars stand on the baseline and are `PH` tall, under the names, in a chart with phases or without.
  // These rows are in the chart's own units, which are px only where it is drawn at its box's width, as the chart with phases is.
  // The chart with no phases is drawn in `BIN_W` units and scaled to its plot. The svg keeps its aspect ratio, so its height is `H`
  // times the plot's width over `W`, and a row's share of `H` is the same share of that height. So a caption's box is its row's share
  // of the plot's height, which puts it at its row at whatever width the plot is drawn.
  const levels = chartLevels(R.task > 0, bands);
  const capBox: Record<Level, [top: number, height: number]> = {
    tasks: [0, R.task],
    phases: [R.top, R.lbl],
    processes: [BASE - PH, PH],
  };

  // The phases behind the bars: a tinted band each, as tall as the bars and the names above them, with a guessed
  // phase or slice hatched and idle an outline. Read here, in a render the page repeats with every live frame, as
  // the pinned clock the edge's name reads is.
  const under: ReactNode[] = [];
  const names: ReactNode[] = [];
  const picks: ReactNode[] = [];
  let edge: ReturnType<typeof edgeSpan> = null;
  if (merged && bands) {
    /** A stretch's left edge and width in px, cut at the view and kept inside the chart (`placeBand`). */
    const place = (a: number, b: number, gap = 0): [x: number, w: number] => placeBand(a, b, [v0, v1], W, gap);
    const visible = (a: number, b: number) => onAxis(a, b, v0, v1);
    /** A stretch's outline, inset half its stroke; one too thin for that is a single line. It is as tall as the bands, or
     *  runs from `y0` to `y1`. */
    const outline = (key: string, x: number, w: number, stroke: string, dash: string, y0 = R.top, y1 = BASE): ReactNode =>
      w < THIN ? (
        <line key={key} x1={x + w / 2} x2={x + w / 2} y1={y0 + 0.5} y2={y1 - 0.5} stroke={stroke} strokeDasharray={dash} />
      ) : (
        <rect key={key} x={x + 0.5} y={y0 + 0.5} width={w - 1} height={y1 - y0 - 1} fill="none" stroke={stroke} strokeDasharray={dash} />
      );
    /** A name at the top of a band, where it fits; with a halo where the band is hatched under it. */
    const name = (key: string, x: number, w: number, text: string, ink: string, over: { x: number; w: number }[], hatched: boolean) => {
      if (!R.lbl || !labelFits(text, w)) return;
      const lx = x + LABEL_PAD / 2;
      const lw = text.length * LABEL_CHAR_W;
      const halo = hatched || over.some((o) => o.x < lx + lw && o.x + o.w > lx);
      names.push(
        <text
          key={key}
          className={halo ? 'ph-lbl ph-halo' : 'ph-lbl'}
          x={lx}
          y={R.top + R.lbl / 2}
          dy="0.35em"
          fill={ink}
          stroke={halo ? 'var(--surface)' : undefined}
        >
          {text}
        </text>,
      );
    };
    // The task lane above the bands: a block for each task or lone stretch on the view (`laneBlocks`), from its own start to
    // its own end, with its label where that fits, else its number. A lone stretch is an outline alone, and says nothing. A
    // block picks its task, whatever it says.
    for (const { i, x, w, text } of laneBlocks) {
      if (tasks[i].kind === 'lone') {
        under.push(outline(`ot${i}`, x, w, 'var(--muted)', '3 2', 0, R.task));
      } else if (w < THIN) {
        under.push(<rect key={`bt${i}`} className="tn-task sliver" x={x} y={0} width={w} height={R.task} />);
      } else {
        under.push(<rect key={`bt${i}`} className="tn-task" x={x + 0.5} y={0.5} width={w - 1} height={R.task - 1} rx={2} />);
      }
      if (text) {
        names.push(
          <text key={`nt${i}`} className="ph-lbl tn-task-lbl" x={x + w / 2} y={R.task / 2} dy="0.35em" textAnchor="middle">
            {text}
          </text>,
        );
      }
      picks.push(
        <rect key={`kt${i}`} data-task={i} x={x} y={0} width={w} height={R.task} fill="transparent" style={{ cursor: 'pointer' }} />,
      );
    }
    list.forEach((p, i) => {
      if (!visible(p.t0, p.t1)) return;
      const col = phaseCol(p.kind);
      const [x, w] = place(p.t0, p.t1, GAP);
      const guess = p.kind === 'edit_guess';
      if (guess) {
        under.push(<rect key={`b${i}`} x={x} y={R.top} width={w} height={bandH} fill={`url(#${hatch})`} />);
        under.push(outline(`o${i}`, x, w, 'var(--s5)', '3 2'));
      } else if (col === 'none') {
        under.push(outline(`o${i}`, x, w, 'var(--muted)', '3 2'));
      } else {
        under.push(<rect key={`b${i}`} className="tn-band" x={x} y={R.top} width={w} height={bandH} fill={col} />);
      }
      // R22: any phase can hold guessed slices, and each is hatched where it lies
      const slices = (p.slices ?? [])
        .filter(([a, b]) => visible(a, b))
        .map(([a, b]) => {
          const [sx, sw] = place(a, b);
          return { x: sx, w: sw };
        });
      slices.forEach((s, j) =>
        under.push(<rect key={`s${i}-${j}`} x={s.x} y={R.top} width={s.w} height={bandH} fill={`url(#${hatch})`} />),
      );
      name(`n${i}`, x, w, phaseShort(p), col === 'none' ? 'var(--ink-2)' : 'var(--ink)', slices, guess);
      if (R.lbl) {
        picks.push(
          <rect key={`k${i}`} data-phase={i} x={x} y={R.top} width={w} height={R.lbl} fill="transparent" style={{ cursor: 'pointer' }} />,
        );
      }
    });
    // the live edge, where the view reaches into it
    if (now && (now.state === 'running' || now.state === 'idle' || now.state === 'unknown') && nowS != null) {
      edge = edgeSpan(list, nowS, v0, v1);
      if (edge) {
        const [x, w] = place(edge.a, edge.b, GAP);
        if (w < EDGE_MIN_PX) edge = null;
        else {
          const col = now.state === 'running' ? phaseCol(now.cat) : 'none';
          if (now.state === 'unknown') under.push(outline('oe', x, w, 'var(--muted)', '1 2'));
          else if (col === 'none') under.push(outline('oe', x, w, 'var(--muted)', '3 2'));
          else under.push(<rect key="be" className="tn-band" x={x} y={R.top} width={w} height={bandH} fill={col} />);
          name('ne', x, w, edgeLabel(now).split(' · ')[0], now.state === 'running' && col !== 'none' ? 'var(--ink)' : 'var(--ink-2)', [], false);
        }
      }
    }
  }

  // What the pointer is over, for the tooltip on the plot: over a task's block, the task and what a click does there;
  // elsewhere the phase under the line, the bar's count, and what a click does there. The block is the one drawn under the
  // pointer, which is the one a click picks, and not the task at that moment: a block is a pixel at the least.
  const ti = phases && R.task > 0 && hx != null && hy != null && hy < R.task ? taskAtX(laneBlocks, hx) : -1;
  let tip = ZOOM_HINT;
  if (phases && ti >= 0) {
    tip = [taskTip(tasks[ti], phases), zoomTip(tasks[ti].t0), ZOOM_HINT].join('\n');
  } else if (hx != null && (den || merged)) {
    const t = T(hx);
    const lines: string[] = [];
    const pi = bands ? phaseAt(list, t) : -1;
    if (pi >= 0) {
      lines.push(phaseTip(list[pi]));
      const sl = sliceAt(list[pi], t);
      if (sl) lines.push(sliceTip(list[pi], sl[0], sl[1]));
    } else if (edge && now && t >= edge.a && t <= edge.b) {
      lines.push(edgeTip(now, edge.from, edge.to));
    }
    if (den) {
      let n = 0;
      const by: string[] = [];
      if (shown) {
        const j = Math.floor((t - den.lo) / shown.dw);
        const m = shown.per.get(j);
        if (m) {
          for (const key of ORDER) {
            const c = m.get(key);
            if (c) {
              n += c;
              by.push(`${plbl(key)} ${fint(c)}`);
            }
          }
        }
      }
      lines.push(`${clock(t)} · ${fint(n)} started in this ${dur(shown?.dw ?? den.w)}${by.length ? ` (${by.join(', ')})` : ''}`);
    } else {
      lines.push(clock(t));
    }
    if (phases && R.hk && hy != null && hy >= R.hk[0] - 1 && hy <= R.hk[0] + R.hk[1] + 1) lines.push(hkTip(phases.housekeeping, t));
    lines.push(
      pi >= 0 && R.lbl > 0 && hy != null && hy >= R.top && hy < R.top + R.lbl
        ? zoomTip(list[pi].t0)
        : 'click: open the tree at the first process started at or after it',
    );
    lines.push(ZOOM_HINT);
    tip = lines.join('\n');
  }
  const xt = ticks(v0, v1, W / 100);
  const kx = kc != null && kc >= v0 && kc <= v1 ? X(kc) : null;
  const cx = cursor != null && cursor >= v0 && cursor <= v1 ? X(cursor) : null;
  // phases the read's axis does not hold are in the list under the navigator, and the chart says so (R78)
  const outside = phases ? phasesOutside(list, axis) : '';
  const caption =
    (shown && shown.top > 0
      ? `process starts per ${dur(shown.dw)} · tallest ${fint(shown.top)}`
      : den
        ? 'no process started in this range'
        : 'no start times in this read') + (den && den.before > 0 ? ` · ${fint(den.before)} started before the axis` : '');
  return (
    <>
      <ZoomNote
        zoomed={zoomed}
        text={`${clock(v0)} – ${clock(v1)} (${dur(v1 - v0)}) · drag to pan${
          inView != null ? ` · ${fint(inView)} drawn process${inView === 1 ? '' : 'es'} ran in it` : ''
        }`}
        hint="scroll to zoom · click to open the tree there"
        reset={() => setView(null)}
      />
      {merged && <BasisRow />}
      <div className="tn-plot">
        {levels.map((level) => (
          <div
            key={level}
            className="tn-cap tn-lane-cap"
            style={{ top: `${(capBox[level][0] / H) * 100}%`, height: `${(capBox[level][1] / H) * 100}%` }}
            aria-hidden="true"
          >
            {/* the phases are explained where they are named, as the basis line explains the tasks */}
            <span className="eyebrow">{level === 'phases' ? <Term k="phases">{LEVELS[level]}</Term> : LEVELS[level]}</span>
          </div>
        ))}
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="tn-svg strip"
          role="img"
          aria-label={
            merged && bands
              ? `phases and process starts per time bin from ${clock(v0)} to ${clock(v1)}${laneClause(tasks)}: ${sequenceLine(list, now)}`
              : `process starts per time bin from ${clock(v0)} to ${clock(v1)}`
          }
        >
          {merged && (
            <defs>
              <pattern id={hatch} width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
                <rect width="4" height="4" fill="var(--surface)" />
                <line x1="0" y1="0" x2="0" y2="4" stroke="var(--s5)" strokeWidth="1.6" />
              </pattern>
            </defs>
          )}
          {!merged && (
            <text className="tick" x={2} y={9}>
              {caption}
            </text>
          )}
          {under}
          <line className="base-line" x1={0} x2={W} y1={BASE} y2={BASE} />
          {names}
          <g
            ref={wheel}
            className="zoomable"
            data-tip={tip}
            tabIndex={0}
            role="slider"
            aria-label="session time: arrow keys move, Enter opens the tree there, + and − zoom, 0 resets"
            aria-valuemin={a0}
            aria-valuemax={a1}
            aria-valuenow={kc ?? cursor ?? v0}
            aria-valuetext={clock(kc ?? cursor ?? v0)}
            style={{ cursor: zoomed ? (panning ? 'grabbing' : 'grab') : 'pointer', outline: 'none' }}
            onDoubleClick={() => setView(null)}
            onKeyDown={onKey}
            onBlur={() => setKc(null)}
            {...handlers}
          >
            <rect x={0} y={0} width={W} height={R.hit} fill="transparent" />
            {picks}
            {bars}
            {R.hk && (
              <g transform={`translate(0 ${R.hk[0]})`}>
                <rect x={0} y={0} width={W} height={R.hk[1]} className="tn-hk-bg" />
                <path d={hkPath} className="tn-hk-tick" />
              </g>
            )}
            {ticksOut}
            {cx != null && <line className="tn-cursor" x1={cx} x2={cx} y1={R.cursor[0]} y2={R.cursor[1]} />}
            {kx != null && <line className="tn-key" x1={kx} x2={kx} y1={R.cursor[0]} y2={R.cursor[1]} />}
            {hx != null && <line className="tn-hover" x1={hx} x2={hx} y1={R.hover[0]} y2={R.hover[1]} />}
          </g>
          {xt.map(({ t, lbl }) => {
            const x = X(t);
            const anchor = x > W - 24 ? 'end' : x < 24 ? 'start' : 'middle';
            return (
              <text key={t} className="tick" x={x} y={H - 1} textAnchor={anchor}>
                {lbl}
              </text>
            );
          })}
        </svg>
      </div>
      {phases && <PhaseLegend list={list} axis={axis} />}
      {outside && <div className="tn-note">{outside}</div>}
      {merged && <div className="tn-note">{caption}</div>}
      {phases && <div className="tn-note tn-hk-text">{housekeepingText(phases.housekeeping)}</div>}
      {zoomed && (
        <ZoomMap
          W={BIN_W}
          H={26}
          lo={a0}
          hi={a1}
          a={v0}
          b={v1}
          minSpan={Math.min(SPAN_MIN, full)}
          onSet={(lo, hi) => setView(fit(lo, hi))}
          onReset={() => setView(null)}
          className="tn-svg"
          label={`the read from ${clock(a0)} to ${clock(a1)}, with ${clock(v0)} to ${clock(v1)} in view`}
          draw={(MX, MH) => (whole && den ? stackBars(whole, den.lo, MX, MH - 2, MH - 5, BIN_W, 0.5) : null)}
        />
      )}
    </>
  );
}

/* ---------------------------------------------------------------- navigator */

export function TrajectoryNav({
  d,
  axis,
  view,
  setView,
  cursor,
  onPick,
  onFail,
  quick,
  status,
  inView,
  phases,
  now,
  onPhase,
  onTask,
}: {
  d: TrajectoryDetail;
  /** the read's own axis: the tree's timeline when not zoomed */
  axis: [number, number];
  view: [number, number] | null;
  setView: (v: [number, number] | null) => void;
  cursor: number | null;
  onPick: (t: number) => void;
  onFail: (i: number) => void;
  quick: Quick[];
  status: ReactNode;
  inView: number | null;
  /** what the agent was doing, where its commands are read: the bands behind the chart's bars, and the task lane
   *  above them */
  phases?: PhaseBlock;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
  /** a phase picked by its name on the chart */
  onPhase?: (p: Phase) => void;
  /** a task or lone stretch picked by its block in the chart's lane */
  onTask?: (t: Task) => void;
}) {
  const nav = d.nav;
  const W = BIN_W;
  // The session track reaches the phases' span and, for a session running now, now, which the live edge runs on
  // to. Read here, in a render the page repeats with every live frame, as the pinned clock the edge's name reads is.
  const list = phases?.list ?? [];
  const nowS = list.length && now ? serverNow() / 1000 : null;
  const S = sessionAxis(d, axis, list.length ? [list[0].t0, Math.max(list[list.length - 1].t1, nowS ?? -Infinity)] : null);
  // A chart with phases is drawn at its box's own pixel width, so that its names are 10 px whatever the box, less the gutter
  // that its captions stand in, which the three tracks start after (the stylesheet reads it from `--tn-gut`).
  const [box, boxW] = useWidth(!!phases && !!nav, W);
  return (
    <div className="tn" style={{ '--tn-gut': `${GUTTER}px` } as CSSProperties}>
      <SessionTrack d={d} axis={axis} S={S} view={view} onPick={onPick} W={W} />
      {nav ? (
        phases ? (
          <div className="tn-strip" ref={box}>
            <Strip
              nav={nav}
              axis={axis}
              view={view}
              setView={setView}
              cursor={cursor}
              onPick={onPick}
              onFail={onFail}
              inView={inView}
              W={plotW(boxW)}
              phases={phases}
              now={now ?? null}
              nowS={nowS}
              onPhase={onPhase}
              onTask={onTask}
            />
          </div>
        ) : (
          <Strip
            nav={nav}
            axis={axis}
            view={view}
            setView={setView}
            cursor={cursor}
            onPick={onPick}
            onFail={onFail}
            inView={inView}
            W={W}
          />
        )
      ) : (
        <div className="nm">{`no navigator in this payload ${DASH} the service predates it`}</div>
      )}
      <div className="tn-quick" role="toolbar" aria-label="jump to">
        {quick.map((q) => (
          // aria-disabled, not disabled: a disabled button gets no pointer
          // events, and its tooltip is what says why there is nothing to go to
          <button
            key={q.key}
            className="btn"
            aria-disabled={!q.run}
            onClick={q.run}
            data-tip={q.tip}
          >
            {q.lbl}
          </button>
        ))}
      </div>
      <div className="tn-status" aria-live="polite">
        {status}
      </div>
    </div>
  );
}
