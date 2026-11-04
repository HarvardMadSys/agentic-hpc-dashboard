/* The phase band: what an agent was doing, one segment per phase along a time axis.
 *
 * It is a session row's ribbon (tabs/TrajectoriesTab.tsx). The inspector draws the same phases behind the
 * process starts of its chart (tabs/TrajectoryNav.tsx), in the same colours, hatching, and words
 * (lib/phases.ts), so a row and the inspector say the same thing. How a segment reads:
 *
 * - SOLID, in the phase's colour (lib/phases.ts): commands the agent ran.
 * - HATCHED in the editing colour: a guess. No command shows an edit there; a
 *   pause is read as one. An `edit_guess` phase is hatched with a dashed outline,
 *   and so is every guessed slice of ANY phase (a loop's pauses, a run's first
 *   stretch), drawn where it lies over that phase's own fill: a guessed pause is
 *   never drawn solid.
 * - an EMPTY dashed outline: idle. No new call began, though a call the agent moved past may still run through it,
 *   and a fill would draw it as work.
 * - the live edge, after the last phase, when `now` says what the per-minute
 *   census shows: filled in the colour of what is running, an empty dashed outline
 *   where it shows nothing beyond what was already up (idle, which is a proxy),
 *   and an empty DOTTED one where it cannot say.
 * - a TICK, a hairline across the band, where a task or a lone stretch after the first
 *   begins (`tasks`; the line under the band names the tasks). It stands at the later
 *   one's first call, so across a cutting gap it is at the end of the gap's idle band, and
 *   not where the task before ends. It is 1 px wide with crisp edges, in the ink of the
 *   inspector's task lane, so it reads apart from the pale part between two bands
 *   (`GAP`), and it takes no pointer, so a band's hover is its own (`.ph-tick`,
 *   tabs/trajectories.css).
 *
 * Nothing is interpolated. A segment is exactly a phase's own span, never thinner
 * than a pixel so a short one is still there, and its tooltip gives the name, the
 * times, and the length. A tick is at the time the service gives a task.
 */
import { useId } from 'react';
import type { ReactNode } from 'react';
import type { Phase, Task, TrajRunning } from '../api/types';
import { GAP, edgeTip, phaseCol, phaseTip, placeBand, sequenceLine, sliceTip } from '../lib/phases';

type Look = 'fill' | 'guess' | 'idle' | 'unknown';

interface Seg {
  key: string;
  look: Look;
  /** the fill of a solid segment */
  fill: string;
  x: number;
  w: number;
  tip: string;
  /** which phase of `list` it stands for; none for the live edge */
  at: number | null;
  /** guessed slices of it, drawn over it */
  over: { x: number; w: number; tip: string }[];
}

export interface PhaseBandProps {
  list: Phase[];
  /** the session's tasks and lone stretches, in time order: a tick marks where each one after the first begins */
  tasks?: Task[];
  /** the axis, epoch seconds: a phase outside it is not drawn, and one across its edge is cut at it */
  t0: number;
  t1: number;
  W: number;
  H: number;
  /** what the census shows running at the live edge: the last segment, after the last phase */
  now?: TrajRunning['now'] | null;
  /** where that segment ends, epoch seconds; the axis's end when left out */
  nowAt?: number;
}

export function PhaseBand({ list, tasks, t0, t1, W, H, now, nowAt }: PhaseBandProps) {
  const hatch = 'ph' + useId().replace(/[^\w-]/g, '');
  const axis: [number, number] = [t0, t1];
  /** A stretch's left edge and width in px, cut at the axis and kept inside the band (`placeBand`). */
  const place = (a: number, b: number, gap = 0) => placeBand(a, b, axis, W, gap);
  /** Whether a stretch is on the axis at all. A zero-length one counts where it sits. */
  const on = (a: number, b: number) => (b > t0 && a < t1) || (a === b && a >= t0 && a <= t1);

  const segs: Seg[] = [];
  list.forEach((p, i) => {
    if (!on(p.t0, p.t1)) return;
    const col = phaseCol(p.kind);
    const [x, w] = place(p.t0, p.t1, GAP);
    segs.push({
      key: `p${i}`,
      look: p.kind === 'edit_guess' ? 'guess' : col === 'none' ? 'idle' : 'fill',
      fill: col,
      x,
      w,
      tip: phaseTip(p),
      at: i,
      // R22: any phase can hold guessed slices, and each is hatched where it lies
      over: (p.slices ?? [])
        .filter(([a, b]) => on(a, b))
        .map(([a, b]) => {
          const [sx, sw] = place(a, b);
          return { x: sx, w: sw, tip: sliceTip(p, a, b) };
        }),
    });
  });
  if (now && (now.state === 'running' || now.state === 'idle' || now.state === 'unknown')) {
    const from = list.length ? list[list.length - 1].t1 : t0;
    const to = Math.max(nowAt ?? t1, from);
    const col = now.state === 'running' ? phaseCol(now.cat) : 'none';
    const [x, w] = place(from, to, GAP);
    segs.push({
      key: 'edge',
      look: now.state === 'unknown' ? 'unknown' : col === 'none' ? 'idle' : 'fill',
      fill: col,
      x,
      w,
      tip: edgeTip(now, from, to),
      at: null,
      over: [],
    });
  }

  /** Where each task or lone stretch after the first begins, as an x in px. A boundary at the axis's edge is the edge,
   *  and one off the axis is not on the band: neither has a tick. */
  const ticks = (tasks ?? [])
    .slice(1)
    .filter((s) => s.t0 > t0 && s.t0 < t1)
    .map((s) => place(s.t0, s.t0)[0]);

  /** The segment's mark. An outline is inset half its stroke, which is centred on the edge it follows and
   *  would otherwise be cut by the svg; one too thin for that is a single dashed line. */
  const mark = (s: Seg): ReactNode => {
    if (s.look === 'fill') return <rect x={s.x} y={0} width={s.w} height={H} fill={s.fill} />;
    const guess = s.look === 'guess';
    const fill = guess ? `url(#${hatch})` : 'none';
    const stroke = guess ? 'var(--s5)' : 'var(--muted)';
    const dash = s.look === 'unknown' ? '1 2' : '3 2';
    return s.w < 3 ? (
      <>
        <rect x={s.x} y={0} width={s.w} height={H} fill={fill} pointerEvents="all" />
        <line x1={s.x + s.w / 2} x2={s.x + s.w / 2} y1={0.5} y2={H - 0.5} stroke={stroke} strokeDasharray={dash} />
      </>
    ) : (
      <rect
        x={s.x + 0.5}
        y={0.5}
        width={s.w - 1}
        height={H - 1}
        fill={fill}
        stroke={stroke}
        strokeDasharray={dash}
        pointerEvents="all"
      />
    );
  };

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      role="img"
      aria-label={`phases: ${sequenceLine(list, now)}`}
      preserveAspectRatio="xMidYMin meet"
    >
      <defs>
        <pattern id={hatch} width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="4" height="4" fill="var(--surface)" />
          <line x1="0" y1="0" x2="0" y2="4" stroke="var(--s5)" strokeWidth="1.6" />
        </pattern>
      </defs>
      <g>
        {segs.map((s) => (
          <g key={s.key} data-kind={s.at != null ? list[s.at].kind : 'edge'} data-tip={s.tip}>
            {mark(s)}
            {s.over.map((o, j) => (
              <rect key={j} x={o.x} y={0} width={o.w} height={H} fill={`url(#${hatch})`} data-tip={o.tip} />
            ))}
          </g>
        ))}
      </g>
      {ticks.map((x, i) => (
        <line key={i} className="ph-tick" x1={x} x2={x} y1={0} y2={H} />
      ))}
    </svg>
  );
}
