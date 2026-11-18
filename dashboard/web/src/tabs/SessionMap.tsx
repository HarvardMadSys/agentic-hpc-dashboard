/* What an agent did over its whole session: its tasks, and the phases under them, on the session's own clock.
 *
 * It answers the first question a reader has of an agent's session, before any process is read. The inspector's
 * chart (TrajectoryNav.tsx) is the read's: process starts on the axis of the records one read reached, and a read
 * stops at its budget, so for a long session that chart holds the first hour or two and the rest is unread. The
 * tasks and phases are the row's block, which is the whole session's whatever the read reached, so they are drawn
 * here at full width: from the session's first record, or its first phase where that is earlier, to its last
 * phase's end, and on to now for a session running now. That is the axis the session row's band is drawn on
 * (`bandAxis`, lib/phases.ts), so the row and this map agree, and nothing in the map comes from the read.
 *
 * Two rows on one clock, captioned in a left gutter as the inspector's chart captions its lanes:
 * - TASKS, where the block has any: a block for each task, with its number and its label where both fit, else its
 *   label, else its number, never a label cut short. The list under the map numbers and labels the tasks alike. A
 *   lone stretch is no task: a dashed outline with no word. The space between two blocks is the gap the list's gap
 *   line names.
 * - PHASES: a band for each phase, in the tint the inspector's chart gives it (`.tn-band`) and with a rule of its
 *   full colour along its foot, so neighbours that tint alike still read apart; named short where the name fits. A
 *   guess, and a guessed slice of any phase, is hatched; idle is an empty dashed outline, since a fill would draw it
 *   as work; and after the last phase of a session running now comes its live edge, as the row's band ends.
 * Under them, the clock, and a key to the colours of the phases on it.
 *
 * A hover on a block or a band says what it was (`taskTip`, `phaseTip`) and what a click does (`zoomTip`). A click
 * picks it as its row in the list does: the inspector's chart zooms to it and the tree opens at its first call,
 * reading the page that holds it where the read does not. Every pick here is a button in that list, so the map
 * takes no tab stop of its own and is an image to a screen reader, named by its phases in words.
 */
import { useId } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { Phase, PhaseBlock, Task, TrajRunning } from '../api/types';
import { Term } from '../components/Term';
import { clock } from '../lib/format';
import {
  GAP,
  GUTTER,
  LABEL_CHAR_W,
  LABEL_PAD,
  LEVELS,
  bandAxis,
  edgeLabel,
  edgeSpan,
  edgeTip,
  labelFits,
  onAxis,
  phaseCol,
  phaseLegend,
  phaseShort,
  phaseTip,
  placeBand,
  plotW,
  sequenceLine,
  sliceTip,
  zoomTip,
} from '../lib/phases';
import type { LegendEntry } from '../lib/phases';
import { taskBlocks, taskLabel, taskNumbers, taskSequence, taskTip } from '../lib/tasks';
import { useClockPinned, useNow } from '../lib/useNow';
import { ticks, useWidth } from './TrajectoryNav';

/** The rows, in px: the task lane, the space under it, the phase band and the rule along its foot, then the clock. */
const TASK_H = 22;
const LANE_GAP = 5;
const BAND_H = 26;
const FOOT = 3;
const TICK_GAP = 4;
const TICK_H = 13;
/** A block or band narrower than this has no outline worth drawing: it is a single line. */
const THIN = 3;
/** The width the map is drawn at before its box is measured. */
const FALLBACK_W = 900;

/** The chip of a phase in the key, drawn as its band is: a tint with the rule along its foot, a hatch in the editing
 *  colour with a dashed outline for a guess, and an empty dashed outline for idle. */
const KEY_W = 16;
const KEY_H = 12;
function KeyChip({ look, fill }: Pick<LegendEntry, 'look' | 'fill'>) {
  const hatch = 'mk' + useId().replace(/[^\w-]/g, '');
  const outline = (stroke: string) => (
    <rect x={0.5} y={0.5} width={KEY_W - 1} height={KEY_H - 1} fill="none" stroke={stroke} strokeDasharray="3 2" />
  );
  return (
    <svg className="sm-key" width={KEY_W} height={KEY_H} viewBox={`0 0 ${KEY_W} ${KEY_H}`} aria-hidden="true">
      {look === 'fill' && (
        <>
          <rect className="tn-band" width={KEY_W} height={KEY_H} fill={fill} />
          <rect y={KEY_H - FOOT} width={KEY_W} height={FOOT} fill={fill} />
        </>
      )}
      {look === 'guess' && (
        <>
          <defs>
            <pattern id={hatch} width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <rect width="4" height="4" fill="var(--surface)" />
              <line x1="0" y1="0" x2="0" y2="4" stroke="var(--s5)" strokeWidth="1.6" />
            </pattern>
          </defs>
          <rect width={KEY_W} height={KEY_H} fill={`url(#${hatch})`} />
          {outline('var(--s5)')}
        </>
      )}
      {look === 'idle' && outline('var(--muted)')}
    </svg>
  );
}

export function SessionMap({
  block,
  first,
  now,
  onTask,
  onPhase,
}: {
  /** the row's block, with at least one phase */
  block: PhaseBlock;
  /** the row's first record, epoch seconds: the map starts there, or at the first phase where that is earlier */
  first?: number | null;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
  onTask: (t: Task) => void;
  onPhase: (p: Phase) => void;
}) {
  // the edge's name and an open phase's or task's words read the clock's pin
  useClockPinned();
  const hatch = 'sm' + useId().replace(/[^\w-]/g, '');
  const list = block.list;
  const tasks = block.tasks ?? [];
  // a session running now runs on to now, which is read again with every live frame (lib/useNow.ts)
  const at = useNow(!!now);
  const nowS = now ? at / 1000 : null;
  const [box, boxW] = useWidth(true, FALLBACK_W);
  const axis = bandAxis(list, first, nowS);
  if (!axis) return null;
  const W = plotW(boxW);
  const [v0, v1] = axis;
  const span = Math.max(v1 - v0, 1e-6);
  const X = (t: number) => ((t - v0) / span) * W;
  const place = (a: number, b: number, gap = 0) => placeBand(a, b, axis, W, gap);

  const lane = tasks.length > 0;
  const bandY = lane ? TASK_H + LANE_GAP : 0;
  const bandFoot = bandY + BAND_H;
  const H = bandFoot + TICK_GAP + TICK_H;

  /** An outline of a stretch of a row: inset half its stroke, so the svg does not cut it, and one too thin for that a
   *  single line. It takes the pointer over all of its inside, so a click anywhere in it picks what it stands for. */
  const outline = (key: string, x: number, w: number, y: number, h: number, stroke: string, dash: string): ReactNode =>
    w < THIN ? (
      <g key={key}>
        <rect x={x} y={y} width={w} height={h} fill="transparent" pointerEvents="all" />
        <line x1={x + w / 2} x2={x + w / 2} y1={y + 0.5} y2={y + h - 0.5} stroke={stroke} strokeDasharray={dash} />
      </g>
    ) : (
      <rect key={key} x={x + 0.5} y={y + 0.5} width={w - 1} height={h - 1} fill="transparent" stroke={stroke} strokeDasharray={dash} pointerEvents="all" />
    );

  // The task lane: a block for each task or lone stretch on the axis, placed as the bands under it are (`taskBlocks`).
  const nums = taskNumbers(tasks);
  const taskMarks: ReactNode[] = [];
  for (const { i, x, w } of lane ? taskBlocks(tasks, axis, W) : []) {
    const t = tasks[i];
    const lone = t.kind === 'lone';
    const label = taskLabel(t);
    const num = nums[i];
    // its number and its label where both fit, as the list shows them, else the label, else the number; a lone stretch says nothing
    const both = num != null && labelFits(`${num} ${label}`, w);
    const say = lone ? null : both ? { num, label } : labelFits(label, w) ? { num: null, label } : num != null && labelFits(String(num), w) ? { num, label: '' } : null;
    taskMarks.push(
      <g key={`t${i}`} className="sm-pick" data-tip={`${taskTip(t, block)}\n${zoomTip(t.t0)}`} onClick={() => onTask(t)}>
        {lone ? (
          outline(`o${i}`, x, w, 0, TASK_H, 'var(--muted)', '3 2')
        ) : w < THIN ? (
          <rect className="tn-task sliver" x={x} y={0} width={w} height={TASK_H} />
        ) : (
          <rect className="tn-task" x={x + 0.5} y={0.5} width={w - 1} height={TASK_H - 1} rx={2} />
        )}
        {say && (
          <text className="ph-lbl tn-task-lbl" x={x + LABEL_PAD / 2} y={TASK_H / 2} dy="0.35em">
            {say.num != null && <tspan className="sm-num">{say.num}</tspan>}
            {say.label && <tspan dx={say.num != null ? LABEL_CHAR_W : 0}>{say.label}</tspan>}
          </text>
        )}
        <rect className="sm-hl" x={x} y={0} width={Math.max(w, 1)} height={TASK_H} />
      </g>,
    );
  }

  // The phase band: a band for each phase on the axis, named where its name fits.
  const bandMarks: ReactNode[] = [];
  const name = (key: string, x: number, w: number, text: string, ink: string, halo: boolean): ReactNode =>
    labelFits(text, w) ? (
      <text
        key={key}
        className={halo ? 'ph-lbl ph-halo' : 'ph-lbl'}
        x={x + LABEL_PAD / 2}
        y={bandY + (BAND_H - FOOT) / 2}
        dy="0.35em"
        fill={ink}
        stroke={halo ? 'var(--surface)' : undefined}
      >
        {text}
      </text>
    ) : null;
  list.forEach((p, i) => {
    if (!onAxis(p.t0, p.t1, v0, v1)) return;
    const col = phaseCol(p.kind);
    const [x, w] = place(p.t0, p.t1, GAP);
    const guess = p.kind === 'edit_guess';
    const idle = col === 'none';
    // any phase can hold guessed slices, each hatched where it lies (R22), and a name over one keeps a halo
    const slices = (p.slices ?? []).filter(([a, b]) => onAxis(a, b, v0, v1));
    const text = phaseShort(p);
    const lx = x + LABEL_PAD / 2;
    const lw = text.length * LABEL_CHAR_W;
    const halo =
      guess ||
      slices.some(([a, b]) => {
        const [sx, sw] = place(a, b);
        return sx < lx + lw && sx + sw > lx;
      });
    bandMarks.push(
      <g key={`p${i}`} className="sm-pick" data-kind={p.kind} data-tip={`${phaseTip(p)}\n${zoomTip(p.t0)}`} onClick={() => onPhase(p)}>
        {guess ? (
          <>
            <rect x={x} y={bandY} width={w} height={BAND_H} fill={`url(#${hatch})`} />
            {outline(`o${i}`, x, w, bandY, BAND_H, 'var(--s5)', '3 2')}
          </>
        ) : idle ? (
          outline(`o${i}`, x, w, bandY, BAND_H, 'var(--muted)', '3 2')
        ) : (
          <>
            <rect className="tn-band" x={x} y={bandY} width={w} height={BAND_H} fill={col} />
            <rect x={x} y={bandFoot - FOOT} width={w} height={FOOT} fill={col} />
          </>
        )}
        {slices.map(([a, b], j) => {
          const [sx, sw] = place(a, b);
          return (
            <rect key={j} x={sx} y={bandY} width={sw} height={BAND_H - (guess || idle ? 0 : FOOT)} fill={`url(#${hatch})`} data-tip={sliceTip(p, a, b)} />
          );
        })}
        {name(`n${i}`, x, w, text, idle ? 'var(--ink-2)' : 'var(--ink)', halo)}
        <rect className="sm-hl" x={x} y={bandY} width={Math.max(w, 1)} height={BAND_H} />
      </g>,
    );
  });

  // The live edge of a session running now, after the last phase, as the row's band ends: not a phase, and no pick.
  const e = now && (now.state === 'running' || now.state === 'idle' || now.state === 'unknown') && nowS != null ? edgeSpan(list, nowS, v0, v1) : null;
  if (now && e) {
    const [x, w] = place(e.a, e.b, GAP);
    const col = now.state === 'running' ? phaseCol(now.cat) : 'none';
    bandMarks.push(
      <g key="edge" data-kind="edge" data-tip={edgeTip(now, e.from, e.to)}>
        {now.state === 'unknown' ? (
          outline('oe', x, w, bandY, BAND_H, 'var(--muted)', '1 2')
        ) : col === 'none' ? (
          outline('oe', x, w, bandY, BAND_H, 'var(--muted)', '3 2')
        ) : (
          <>
            <rect className="tn-band" x={x} y={bandY} width={w} height={BAND_H} fill={col} />
            <rect x={x} y={bandFoot - FOOT} width={w} height={FOOT} fill={col} />
          </>
        )}
        {name('ne', x, w, edgeLabel(now).split(' · ')[0], col === 'none' ? 'var(--ink-2)' : 'var(--ink)', false)}
      </g>,
    );
  }

  const xt = ticks(v0, v1, W / 100);
  const key = phaseLegend(list, axis);
  const levels: [string, number, number][] = [
    ...(lane ? ([['tasks', 0, TASK_H]] as [string, number, number][]) : []),
    ['phases', bandY, BAND_H],
  ];
  const tasksLine = taskSequence(block);
  return (
    <div className="sm" ref={box} style={{ '--tn-gut': `${GUTTER}px` } as CSSProperties}>
      <div className="tn-plot">
        {levels.map(([level, top, h]) => (
          // placed as a share of the drawing's height, which follows its width, so a caption stays beside its row at any width
          <div key={level} className="tn-cap tn-lane-cap" style={{ top: `${(top / H) * 100}%`, height: `${(h / H) * 100}%` }} aria-hidden="true">
            <span className="eyebrow">
              <Term k={level}>{LEVELS[level as keyof typeof LEVELS]}</Term>
            </span>
          </div>
        ))}
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="tn-svg sm-svg"
          role="img"
          aria-label={`the session from ${clock(v0)} to ${clock(v1)}: ${tasksLine ? `tasks ${tasksLine}; ` : ''}phases ${sequenceLine(list, now)}`}
        >
          <defs>
            <pattern id={hatch} width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <rect width="4" height="4" fill="var(--surface)" />
              <line x1="0" y1="0" x2="0" y2="4" stroke="var(--s5)" strokeWidth="1.6" />
            </pattern>
          </defs>
          {taskMarks}
          {bandMarks}
          <line className="base-line" x1={0} x2={W} y1={bandFoot + 0.5} y2={bandFoot + 0.5} />
          {xt.map(({ t, lbl }) => {
            const x = X(t);
            const anchor = x > W - 24 ? 'end' : x < 24 ? 'start' : 'middle';
            return (
              <g key={t}>
                <line className="sm-tick" x1={x} x2={x} y1={bandFoot} y2={bandFoot + TICK_GAP} />
                <text className="tick" x={x} y={H - 1} textAnchor={anchor}>
                  {lbl}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      {key.length > 0 && (
        <div className="tn-legend sm-legend" role="list" aria-label="what the colours of the phases mean">
          {key.map((k) => (
            <span key={`${k.look} ${k.name}`} className="tn-legend-item" role="listitem">
              <KeyChip look={k.look} fill={k.fill} />
              {k.name}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
