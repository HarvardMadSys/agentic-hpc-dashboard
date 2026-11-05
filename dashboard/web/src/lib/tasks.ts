/* Tasks: what an agent was working on, above the fine phases (`rc_dashboard/reducers/phase_tasks.py`), in the
 * words the page uses for them: the inspector's task lane and list, and a session row's line.
 *
 * Labels, copy, and the lane's geometry are all here, so the lane, the list, and the row say the same thing. A
 * label the service took from the agent's own words is a quote, and is shown as one: in quotation marks, with its
 * source (`commit`, `PR #235`, or `log`) where there is room for it. A label built from the names in the task's
 * commands shows neither, so a reader never takes it for something the agent wrote.
 */
import type { Phase, PhaseBlock, PhaseDay, Task, TaskGap } from '../api/types';
import { clock, dur, fint } from './format';
import { GAP, aliveClause, incompleteFrom, labelFits, lowerFirst, onAxis, openClause, phaseAt, placeBand } from './phases';

/** `3 phases`, `1 phase`: a count in the page's own format, and the word that fits it. */
const plural = (n: number, one: string, many: string): string => `${fint(n)} ${n === 1 ? one : many}`;

/** The basis line above the inspector's chart: what the tasks, and the phases under them, are read from. It is the
 *  service's own `basis` for a block (`phase_segment.BASIS`) under the name of the level it opens with, and no other
 *  file of the page spells it. Its first word is a glossary term (`tasks`). */
export const TASKS_BASIS = 'tasks · read from the commands the agent ran and the words it wrote in them';

/* ------------------------------------------------------------ labels */

/** Where a task's label came from, as the page names it: `commit`, `log`, `PR #235` (`PR` alone for a pull request
 *  with no number yet), or null for a label built from names. A source this page does not know is still the agent's
 *  words, and is named as it came. */
export function taskSource(t: Task): string | null {
  if (t.source === 'pr') return t.ref ? `PR ${t.ref}` : 'PR';
  return t.source ?? null;
}

/** A task's label: `“Fix the loader”` for a quote, in the quotation marks that say these are the agent's words, and
 *  the label as it is for one built from names. A lone stretch is labelled by the same rules. */
export function taskLabel(t: Task): string {
  return taskSource(t) === null ? t.label : `“${t.label}”`;
}

/* --------------------------------------------------------- the list */

/** The phases a task opens into: its range of the block's list, less the idle phase of the cutting gap after it. The
 *  range holds that phase, since the tasks tile the phases in order, but the page says that gap once, as the gap line
 *  that begins the next task (`gapLine`). An idle phase inside a task is one of its phases, and so is one at the end
 *  of the last task, which no gap line follows. The next task is the one whose range begins where this one ends, so a
 *  copy of a task opens into the same phases as the task does. */
export function taskPhases(t: Task, b: PhaseBlock): Phase[] {
  const [first, end] = t.phases;
  const own = b.list.slice(first, end);
  const next = b.tasks?.find((x) => x.phases[0] === end);
  return next?.gap && own[own.length - 1]?.kind === 'idle' ? own.slice(0, -1) : own;
}

/** What a task's row says after its label: `6 phases · 208 calls`, and `1 phase · 1 call`. */
export function taskWhat(t: Task, b: PhaseBlock): string {
  return `${plural(taskPhases(t, b).length, 'phase', 'phases')} · ${plural(t.n, 'call', 'calls')}`;
}

/** The number each of the tasks has, which the lane and the list both show: its place among the tasks, from 1. A lone stretch
 *  is no task, so it has none (null) and takes none from the tasks after it. One function, so that the two cannot number
 *  differently. */
export function taskNumbers(tasks: Pick<Task, 'kind'>[]): (number | null)[] {
  let nth = 0;
  return tasks.map((t) => (t.kind === 'task' ? ++nth : null));
}

/** The index of the task or lone stretch that holds the chart's zoom, which the list opens by default, or -1. It is the one the
 *  view `[v0, v1]` covers most, and the earlier of two it covers alike. A view of no zoom (null) holds none, and nor does one
 *  that lies in a gap, or only touches the edge of a task. A task of no time is held by a view that includes it, which is what a
 *  click on it makes: the chart's narrowest zoom, beginning at its start. */
export function taskInView(tasks: Pick<Task, 't0' | 't1'>[], view: [number, number] | null | undefined): number {
  if (!view) return -1;
  const [v0, v1] = view;
  let held = -1;
  let most = -1;
  tasks.forEach((t, i) => {
    const covered = Math.min(t.t1, v1) - Math.max(t.t0, v0);
    const holds = t.t1 > t.t0 ? covered > 0 : t.t0 >= v0 && t.t0 <= v1;
    if (holds && covered > most) {
      held = i;
      most = covered;
    }
  });
  return held;
}

/** What a task says of itself beyond its label and what it holds, a line each: that a call in it is still running, where it is
 *  open (`openClause`), and that the call log was full, where the cut may have taken calls of it, from when the block was
 *  cut (`incompleteFrom`, `cut_at`). Empty for a task that is neither. The running line comes first. The task's hover has
 *  them on lines of their own (`taskTip`) and its row in the list has them as a muted part of it. */
export function taskState(t: Task, b: Pick<PhaseBlock, 'cut_at'>): string[] {
  const out: string[] = [];
  if (t.open) out.push(openClause());
  if (t.incomplete) out.push(incompleteFrom(b.cut_at));
  return out;
}

/** The muted line for the gap between two tasks: `2.1 h with no new calls`. Where a call the agent moved past was
 *  still running as the gap began, the first of them follows, by the name the service gives it, else by its command:
 *  `2.1 h with no new calls · linktest.sh still running since 13:05:44`, or `· a call still running since …` for one
 *  with neither (`aliveClause`). */
export function gapLine(g: TaskGap): string {
  const head = `${dur(g.s)} with no new calls`;
  return g.alive.length ? `${head} · ${aliveClause(g.alive[0])}` : head;
}

/** The line for a lone stretch: `1 call at 16:46:03 · read harness/x.py`, and `3 calls at …`. A quote keeps its
 *  capital, inside its quotation marks, as it was written. The list draws it as a row to click, in `--ink-2`, and not
 *  muted as the gap line is. */
export function loneLine(t: Task): string {
  return `${plural(t.n, 'call', 'calls')} at ${clock(t.t0)} · ${lowerFirst(taskLabel(t))}`;
}

/* ------------------------------------------------------- the row line */

/** A session row's line: the labels of its first `keep` tasks in order, joined by ` → `, and a count of the rest, as
 *  `A → B → 3 more`. A quoted task is in its quotation marks, and its source, which has no room here, is left out.
 *  A lone stretch is no task, and is neither named nor counted. Empty where the block has no tasks. */
export function taskSequence(b: PhaseBlock, keep = 2): string {
  const named = (b.tasks ?? []).filter((t) => t.kind === 'task');
  const parts = named.slice(0, keep).map(taskLabel);
  if (named.length > keep) parts.push(`${fint(named.length - keep)} more`);
  return parts.join(' → ');
}

/* ----------------------------------------------------------- the lane */

/** What a hover on a task's block in the lane says: its label, with where its words came from where it is a quote, and
 *  then what it holds, as the list says it: `“Fix the loader” · commit`, then `6 phases · 208 calls`. A lone stretch
 *  says its own line (`loneLine`). Last, on a line each, what it says of a call still running in it and of a cut call log,
 *  in the words the list of phases uses for them (`taskState`). */
export function taskTip(t: Task, b: PhaseBlock): string {
  const source = taskSource(t);
  const head = t.kind === 'lone' ? loneLine(t) : `${taskLabel(t)}${source ? ` · ${source}` : ''}\n${taskWhat(t, b)}`;
  return [head, ...taskState(t, b)].join('\n');
}

/** What the chart's accessible name says of the task lane, after the times: `, with 3 tasks above them`. It counts the
 *  tasks and not the lone stretches, which are no task, as the row's line does (`taskSequence`), and is empty where the
 *  block has none, as a block of the first version has none. */
export function laneClause(tasks: Task[] | undefined): string {
  const n = (tasks ?? []).filter((t) => t.kind === 'task').length;
  return n ? `, with ${plural(n, 'task', 'tasks')} above them` : '';
}

/** One block of the lane above the chart: a task or a lone stretch. */
export interface LaneBlock {
  /** which of the tasks it stands for: its index in `tasks` */
  i: number;
  /** its left edge and its width, in px */
  x: number;
  w: number;
  /** what it says. A task's is its label where that fits, else its number among the tasks where that fits, else
   *  nothing. A lone stretch has no number and says nothing */
  text: string;
}

/** The lane's blocks on the axis `[v0, v1]` drawn `W` px wide: a block for each task or lone stretch that falls on it
 *  (`onAxis`), placed as the chart's bands are (`placeBand`, with the gap that parts neighbours), so that the lane
 *  lines up with the bands under it. A task's text is its label where the label fits (`labelFits`: never cut), else
 *  its number where only that does, else nothing; a quoted label is in its quotation marks. Tasks are numbered by their
 *  place among the tasks, the ones off the axis counted too, and a lone stretch is no task, so it takes no number and
 *  gives none up (`taskNumbers`, which the list numbers its tasks by too). */
export function taskBlocks(tasks: Task[], axis: [number, number], W: number): LaneBlock[] {
  const [v0, v1] = axis;
  const out: LaneBlock[] = [];
  const nums = taskNumbers(tasks);
  tasks.forEach((t, i) => {
    if (!onAxis(t.t0, t.t1, v0, v1)) return;
    const [x, w] = placeBand(t.t0, t.t1, axis, W, GAP);
    const label = taskLabel(t);
    const num = String(nums[i]);
    out.push({ i, x, w, text: t.kind !== 'task' ? '' : labelFits(label, w) ? label : labelFits(num, w) ? num : '' });
  });
  return out;
}

/** The index in `tasks` of the task or lone stretch whose block holds pixel `x` of the lane (`taskBlocks`), or -1: the last
 *  such block, since a later one is drawn over an earlier one and takes the click. The pointer is asked of the blocks, in the
 *  pixels the lane draws them in, and not of the time under it (`taskAt`), because a block is never under `MIN_W` wide: a
 *  task or lone stretch of no time to speak of (a lone stretch of one call) is a pixel, and answers over all of it, as it
 *  takes the click over all of it. By time it would answer over a part of that pixel, or none. */
export function taskAtX(blocks: LaneBlock[], x: number): number {
  for (let k = blocks.length - 1; k >= 0; k--) {
    if (x >= blocks[k].x && x < blocks[k].x + blocks[k].w) return blocks[k].i;
  }
  return -1;
}

/** The index of the task or lone stretch that holds time `t`, or -1 where none does: from its start up to its end,
 *  and the last one its end as well, as `phaseAt` holds a phase. A gap between tasks holds none. The lane's hover does not
 *  ask it, since a block can be wider than its time (`taskAtX`). */
export const taskAt = (tasks: Task[], t: number): number => phaseAt(tasks, t);

/* ---------------------------------------------------------------- the days */

/** The row a pick must go to, where its task or phase is held by another day's row of the same session than the one open: that
 *  row's sid, else null. A session that ran past midnight is two rows that serve one block, and each task and phase of it says
 *  which row holds it (`sid`: the row of the day it begins on). An agent of one day has no sid on them, nor does a block of the
 *  first version, and picks nothing away. */
export function otherDay(item: { sid?: string | null }, open: string): string | null {
  return item.sid && item.sid !== open ? item.sid : null;
}

/** Where the window of a payload begins, in epoch seconds on the service's clock: its length in `minutes` back from `nowMs`, the
 *  service's present (`serverNow`). Null where the payload does not say how long it is. */
export function windowStart(minutes: number | null | undefined, nowMs: number): number | null {
  return typeof minutes === 'number' && Number.isFinite(minutes) && minutes > 0 ? nowMs / 1000 - minutes * 60 : null;
}

/** Whether the window holds a day row of a session: its last record is at or after the window's start (`from`), which is how the
 *  service picks the rows a window ranks. The table lists only the rows that rank highest, so a day row the window holds may be one the
 *  table does not list, and it opens all the same. With no start known, the window is not known to hold it. */
export function dayInWindow(day: Pick<PhaseDay, 'last'>, from: number | null): boolean {
  return from != null && day.last >= from;
}
