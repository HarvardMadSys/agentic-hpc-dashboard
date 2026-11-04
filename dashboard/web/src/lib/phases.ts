/* Phases: what an agent was doing, read from the commands it ran
 * (`rc_dashboard/reducers/phase_segment.py`), in the words the page uses for
 * them: a session row's band and line, and the inspector's chart (the phases behind
 * its process starts) and list.
 *
 * Labels, colours, and sentences are all here so the row and the inspector say
 * the same thing. The colours are the existing series slots, never a new hue;
 * idle has no fill, because no new call began in it and a fill would draw it as
 * work. A call the agent moved past may still run through it, and then its line
 * says so. A guess says it is one wherever it is named.
 */
import type { Phase, PhaseBlock, PhaseHousekeeping, PhaseKind, PhasesUnavailable, TrajRunning } from '../api/types';
import { clockPinned } from './clock';
import { clock, dur, fint } from './format';

export const PHASES: [kind: PhaseKind, label: string, short: string, colour: string][] = [
  ['explore', 'Exploring', 'explore', 'var(--s3)'],
  ['edit', 'Editing', 'edit', 'var(--s5)'],
  ['edit_guess', 'Editing (guess)', 'edit?', 'var(--s5)'],
  ['build', 'Building', 'build', 'var(--s2)'],
  ['test', 'Testing', 'test', 'var(--s8)'],
  ['loop', 'Edit–test loop', 'edit–test loop', 'var(--s8)'],
  ['run', 'Running', 'run', 'var(--s6)'],
  ['slurm', 'Slurm jobs', 'Slurm', 'var(--s4)'],
  ['wait', 'Waiting', 'waiting', 'var(--s1)'],
  ['git', 'Git', 'git', 'var(--s7)'],
  ['idle', 'Idle', 'idle', 'none'],
  ['unknown', 'Commands not recorded', 'not recorded', 'var(--sg)'],
];

const BY_KIND = new Map<string, (typeof PHASES)[number]>(PHASES.map((r) => [r[0], r]));

/** Fill for a phase: its slot, `none` for idle, and the grey for a kind this page does not know. */
export const phaseCol = (kind: string | null | undefined): string => BY_KIND.get(kind ?? '')?.[3] ?? 'var(--sg)';

/** Its name: `Editing (guess)` for a guess, and the kind itself for one this page does not know. */
export const phaseLbl = (kind: string | null | undefined): string =>
  BY_KIND.get(kind ?? '')?.[1] ?? (kind || 'Commands not recorded');

/** Its short name, as a band's segment or the line of phases says it: `edit?`, and `edit–test loop ×4`. */
export function phaseShort(p: Phase): string {
  const s = BY_KIND.get(p.kind)?.[2] ?? p.kind;
  return p.kind === 'loop' && p.rounds != null ? `${s} ×${fint(p.rounds)}` : s;
}

/** A phase block, as against `{unavailable}` or nothing at all. */
export function isBlock(x: PhaseBlock | PhasesUnavailable | null | undefined): x is PhaseBlock {
  return x != null && Array.isArray((x as PhaseBlock).list);
}

/* ------------------------------------------------------------ small words */

/** One line: a masked preview can run over several. */
const oneLine = (s: string): string => s.replace(/\s+/g, ' ').trim();

/** `a`, `a and b`, `a, b, and c`: the serial comma. */
const andList = (xs: string[]): string =>
  xs.length < 3 ? xs.join(' and ') : `${xs.slice(0, -1).join(', ')}, and ${xs[xs.length - 1]}`;

const calls = (n: number): string => `${fint(n)} ${n === 1 ? 'call' : 'calls'}`;

/** `Read x` as `read x`: a label that follows a count or a time. */
export const lowerFirst = (s: string): string => s.charAt(0).toLowerCase() + s.slice(1);

/** One command of a phase's `top`: `rg -n foo ×14` for one that ran more than once, and `ls` for one that ran once. Its masked
 *  preview is on one line, whatever it held. A phase's line (`phaseWhat`) joins them, and the list of tasks gives each its own row. */
export const commandLine = ([cmd, n]: [string, number]): string => (n > 1 ? `${oneLine(cmd)} ×${fint(n)}` : oneLine(cmd));

/** `rg -n foo ×14, ls`: what ran most, with a count where it ran more than once. */
const commands = (top: [string, number][]): string => top.map(commandLine).join(', ');

/** Characters of a command named in a sentence (R69). */
const SHORT = 40;
/** A leading assignment: a `NAME=value` word, or a `NAME=value;` or `NAME=value &&` statement. Its value
 *  is quoted, a `$( )` or a backquote that may hold spaces, or bare. */
const ASSIGN =
  /^[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`|[^\s;&|"'`])*\s*(?:;|&&)?\s*/;
/** A leading `cd DIR &&` or `cd DIR;`: where a command runs, not what it runs. */
const CD = /^cd\s+(?:"[^"]*"|'[^']*'|[^\s;&|"'])+\s*(?:;|&&)\s*/;
/** A word that ends a command's arguments: what follows is another command's. */
const ENDS = new Set(['|', '||', '&&', ';', '&']);
/** An argument that is a path: not an option or a redirection, and holding a `/`. */
const isPath = (w: string): boolean => !/^(?:-|\d*[<>&]|[<>])/.test(w) && w.includes('/');

/** A command as a sentence names it, a wait's pace or a loop's runs: its leading assignments and `cd DIR
 *  &&` passed over, then cut at 40 characters on a word boundary, with `…` when it is cut (R69). A poll
 *  that sets `T=/tmp/…` first is named by what it runs. When the cut would leave the program alone, its
 *  script names it: the program, then `…/` and the basename of its first argument that is a path, as
 *  `bash …/watch_t2i.sh`; with none, `bash…`. The list's `top` keeps whole previews. */
export function shortCmd(cmd: string): string {
  let s = oneLine(cmd);
  for (let m = ASSIGN.exec(s) ?? CD.exec(s); m && m[0].length < s.length; m = ASSIGN.exec(s) ?? CD.exec(s)) {
    s = s.slice(m[0].length);
  }
  if (s.length <= SHORT) return s;
  const head = s.slice(0, SHORT + 1);
  const at = head.lastIndexOf(' ');
  const cut = (at > 0 ? head.slice(0, at) : s.slice(0, SHORT)).trimEnd();
  const words = s.split(' ');
  if (cut === words[0]) {
    const end = words.findIndex((w, i) => i > 0 && ENDS.has(w));
    const path = words.slice(1, end < 0 ? undefined : end).find(isPath);
    const base = path?.replace(/^["']+|["';]+$/g, '').replace(/\/+$/, '').split('/').pop();
    if (base) return `${words[0]} …/${base}`;
  }
  return `${cut}…`;
}

/** `every_s` as a reader says a pace: `2 min`, `30 s`, never `2.0 min`. */
const pace = (s: number): string => dur(s).replace(/\.0(?= )/, '');

/** The command a loop or a wait is named for. `top` ranks every call in the phase, and a loop holds the
 *  reading and git between its runs, so a repeated `git status` can outrank the test it loops: the
 *  command that ran `n` times (the loop's `rounds`, the wait's `times`) is the one, and failing that,
 *  the one that ran most. Null when no command was recorded. */
const namedBy = (top: [string, number][], n: number | null): string | null => {
  const hit = top.find(([, c]) => c === n) ?? top[0];
  return hit ? shortCmd(hit[0]) : null;
};

/** `table[key]` for a key the table holds, and never one off `Object.prototype`: keys come from the service. */
const lookup = (table: Record<string, string>, key: string): string | undefined =>
  Object.hasOwn(table, key) ? table[key] : undefined;

/** The category names a mixed phase is made of, as the phase labels say them. */
const MIX_WORD: Record<string, string> = {
  explore: 'exploring',
  edit: 'editing',
  build: 'building',
  test: 'testing',
  run: 'running',
  slurm: 'Slurm',
  wait: 'waiting',
  git: 'git',
  unknown: 'commands not recorded',
};

/** `exploring, with some running and building`: said when no category holds half the calls. */
function mixText(mix: [string, number][]): string {
  if (mix.length < 2) return '';
  const word = (c: string) => lookup(MIX_WORD, c) ?? c;
  return `${word(mix[0][0])}, with some ${andList(mix.slice(1).map(([c]) => word(c)))}`;
}

const WAITS_ON: Record<string, string> = {
  slurm: 'Slurm',
  process: 'a process',
  output: 'a file or its output',
  sleep: 'a fixed sleep',
  command: 'something else',
};

const IDLE_WHAT = "no tool calls: the user's turn, or the agent thinking";

/** What a gap says of a call the agent had moved past that was still running as the gap began:
 *  `linktest.sh still running since 13:05:44`. A task's gap names the call (`TaskGap.alive`): by the script it ran or
 *  what else the service names it for, and else by the programs seen under it, for a call still running. Where it has
 *  no name, as an idle phase's call has none, it is the command named as a sentence names one (`shortCmd`). A call
 *  still running has no preview of its own (R65), and with no name either it is `a call still running since 13:05:44`.
 *  It says when the call began, and not that it is running now: it may have ended inside the gap, and no end is said.
 *  An idle phase's detail (`phaseWhat`) and a task's gap line (`gapLine`, tasks.ts) say it alike. */
export function aliveClause([preview, since, name]: [string | null, number, (string | null)?]): string {
  const what = (name ? oneLine(name) : '') || (preview ? shortCmd(preview) : '');
  return `${what || 'a call'} still running since ${clock(since)}`;
}

/** Calls that ran past their phase, as many as `phaseWhat` names before it counts the rest. */
const RAN_PAST_NAMED = 3;

/* --------------------------------------------------- a phase, in words */

/** A phase's detail line in the inspector's list.
 *
 *  - idle: `no tool calls: the user's turn, or the agent thinking`. Where a call the agent moved past was still
 *    running as the gap began, the first of them follows: ` · linktest still running since 13:05:44` (`aliveClause`)
 *  - a guess: its reason. Keyed on `kind`, never on `guess` being there: a phase that holds
 *    guessed slices carries their reason too, and is not a guess
 *  - a loop: `6 runs of pytest -q · 3 pauses read as edits (guess)`
 *  - a wait: `waiting on Slurm · squeue every 2 min, 33 times`
 *  - any other: `22 calls · exploring, with some running · rg -n foo ×9, ls -la ×6`
 *
 *  Then, for any phase holding guessed slices that is not a loop, how many and why; for each call that
 *  ran past its phase `train.py kept running for 2.7 h` (never one still running, R65); and for a phase
 *  with a call still running, `a call in it is still running, seen through sleep and ugrep`. */
export function phaseWhat(p: Phase): string {
  if (p.kind === 'idle') {
    const alive = p.alive?.[0];
    return alive ? `${IDLE_WHAT} · ${aliveClause(alive)}` : IDLE_WHAT;
  }
  if (p.kind === 'edit_guess') return oneLine(p.guess ?? '') || 'a pause read as editing (guess)';
  const top = p.top ?? [];
  const segs: string[] = [];
  const general = () => {
    segs.push(calls(p.n));
    const mix = mixText(p.mix ?? []);
    if (mix) segs.push(mix);
    if (top.length) segs.push(commands(top));
  };
  if (p.kind === 'loop') {
    const runs = p.rounds != null ? `${fint(p.rounds)} ${p.rounds === 1 ? 'run' : 'runs'}` : calls(p.n);
    const cmd = namedBy(top, p.rounds);
    segs.push(cmd ? `${runs} of ${cmd}` : runs);
  } else if (p.kind === 'wait' && p.waits_on != null) {
    segs.push(`waiting on ${lookup(WAITS_ON, p.waits_on) ?? p.waits_on}`);
    if (p.every_s != null && p.times != null) {
      // R65: the command the service says the pace is set by; a call still running is in `times`, and
      // never in `top`
      const cmd = (p.every_cmd ? shortCmd(p.every_cmd) : null) ?? namedBy(top, p.times) ?? 'a command';
      segs.push(`${cmd} every ${pace(p.every_s)}, ${fint(p.times)} ${p.times === 1 ? 'time' : 'times'}`);
    } else {
      general();
    }
  } else {
    general();
  }
  const cuts = p.slices?.length ?? 0;
  if (cuts > 0) {
    const n = `${fint(cuts)} ${cuts === 1 ? 'pause read as an edit' : 'pauses read as edits'} (guess)`;
    segs.push(p.kind !== 'loop' && p.guess ? `${n}: ${oneLine(p.guess)}` : n);
  }
  const past = [...(p.ran_past ?? [])].sort((a, b) => b[1] - a[1]);
  for (const [cmd, s] of past.slice(0, RAN_PAST_NAMED)) {
    segs.push(`${cmd ? oneLine(cmd) : 'a call'} kept running for ${dur(s)}`);
  }
  if (past.length > RAN_PAST_NAMED) segs.push(`${fint(past.length - RAN_PAST_NAMED)} more kept running`);
  const open = phaseOpen(p);
  if (open) segs.push(open);
  return segs.join(' · ');
}

/** What says that a call in a phase or a task is still running: `a call in it is still running` on a clock that goes
 *  on, and `a call in it was still running when the record ended` on a pinned one, which will not grow: chosen as the
 *  edge's `not known yet` and `not known` are. An open phase says it (`phaseOpen`), and so does an open task, in its
 *  tooltip (`taskTip`, tasks.ts). */
export function openClause(): string {
  return clockPinned() ? 'a call in it was still running when the record ended' : 'a call in it is still running';
}

/** What an open phase adds to its line and its tooltip: `openClause`, a call in it still running, seen through its
 *  children (R56), never drawn as finished. Then the programs seen under it, `, seen through sleep and ugrep`: its own
 *  command line is not recorded until it exits, and its first child's text is no command of its own (R65). Empty for a
 *  phase that is not open. */
export function phaseOpen(p: Phase): string {
  if (!p.open) return '';
  const seen = p.seen ?? [];
  return seen.length ? `${openClause()}, seen through ${andList(seen.map(oneLine))}` : openClause();
}

/** What says that a session's call log was cut, so that what lies after the cut is not all there: `incomplete from
 *  14:03:22: the call log was full`, from when the first call the log left out began (`cut_at`), and `incomplete: the
 *  call log was full` where the block has no time for it. The list of phases says it under its rows (PhaseList), and a
 *  task the cut may have taken calls of says it in its tooltip (`taskTip`, tasks.ts). */
export function incompleteFrom(cutAt: number | null | undefined): string {
  return `incomplete${cutAt != null ? ` from ${clock(cutAt)}` : ''}: the call log was full`;
}

/** The note under the list of phases for a session whose call log was full: `phases are incomplete from 14:03:22: the call
 *  log was full, so 12 later calls are counted but in no phase` (`incompleteFrom` has the first half, and a task's tooltip
 *  says it too). Where the block has no time for the cut, the calls are not said to be later ones. Null where nothing was
 *  cut. */
export function cutNote(block: Pick<PhaseBlock, 'cut' | 'cut_at'>): string | null {
  const n = block.cut;
  if (!(n > 0)) return null;
  const left = `${fint(n)} ${block.cut_at != null ? 'later ' : ''}${n === 1 ? 'call is' : 'calls are'}`;
  return `phases are ${incompleteFrom(block.cut_at)}, so ${left} counted but in no phase`;
}

/* ------------------------------------------------ a phase, on a chart or a band */

/** What a hover on a phase says: `Exploring · 09:02:11–09:14:02 · 12 min`, and for an open one
 *  ` · a call in it is still running, seen through sleep` (`… was still running when the record ended`
 *  on a pinned clock; `phaseOpen`). Idle and a guess add what they are, which their look alone does not
 *  say. The session row's band and the inspector's chart both say it. */
export function phaseTip(p: Phase): string {
  const open = phaseOpen(p);
  const head = `${phaseLbl(p.kind)} · ${clock(p.t0)}–${clock(p.t1)} · ${dur(p.t1 - p.t0)}${open ? ` · ${open}` : ''}`;
  return p.kind === 'idle' || p.kind === 'edit_guess' ? `${head}\n${phaseWhat(p)}` : head;
}

/** A guessed slice of a phase: named as a guess, with the reason the phase gives for it. */
export function sliceTip(p: Phase, a: number, b: number): string {
  const head = `${phaseLbl('edit_guess')} · ${clock(a)}–${clock(b)} · ${dur(b - a)}`;
  const why = oneLine(p.guess ?? '');
  return why ? `${head}\n${why}` : head;
}

/** What a hover says a click on a phase or a task does: `click: zoom to it, and open the tree at the first process started at
 *  or after 11:03:32`, `t0` being where it starts. The chart says it over a phase's name and over a task's block, and the
 *  list of phases says it on each of its rows. */
export const zoomTip = (t0: number): string =>
  `click: zoom to it, and open the tree at the first process started at or after ${clock(t0)}`;

/** The index of the phase that holds time `t`, or -1 where none does: a phase holds from its start up to its
 *  end, and the last one its end as well. Phases come in time order. The tasks above them hold a time the same
 *  way (`taskAt`), so any stretches that come in time order will do. */
export function phaseAt(list: Pick<Phase, 't0' | 't1'>[], t: number): number {
  let at = -1;
  for (let i = 0; i < list.length && list[i].t0 <= t; i++) at = i;
  if (at < 0) return -1;
  const p = list[at];
  return t < p.t1 || (at === list.length - 1 && t <= p.t1) ? at : -1;
}

/** The guessed slice of `p` that holds `t`, or null. */
export function sliceAt(p: Phase, t: number): [number, number] | null {
  return (p.slices ?? []).find(([a, b]) => t >= a && t <= b) ?? null;
}

/** Whether the stretch `[a, b]` falls on the axis `[v0, v1]`: it overlaps it, and a stretch of no length counts
 *  where it sits. One rule for what a chart draws on an axis, and for what its legend and its note say about the
 *  axis, so they cannot disagree. */
export const onAxis = (a: number, b: number, v0: number, v1: number): boolean =>
  (b > v0 && a < v1) || (a === b && a >= v0 && a <= v1);

/** The words that explain the hatching: a guess, and what a guessed pause in any phase is. */
export const GUESS_LEGEND = 'hatched = a guess: no command shows it';

/** One entry of a chart's legend: a swatch, and what it stands for. */
export interface LegendEntry {
  /** a band's tint (`fill`), a guess's hatch, or idle's empty dashed outline */
  look: 'fill' | 'guess' | 'idle';
  /** the colour of a tint, as `phaseCol` gives it to the band; `none` for idle */
  fill: string;
  /** `Testing, edit–test loop`, `idle`, or for the hatch its own words (`GUESS_LEGEND`) */
  name: string;
}

/** The legend of a chart's colours: the kinds whose bands fall on the read's axis and no others (R77), in the colour
 *  table's order (explore, edit, build, test, run, Slurm, wait, git, not recorded, and idle last), each named as
 *  `phaseLbl` names it and coloured as `phaseCol` gives it to its band, so the legend and the bands cannot drift.
 *
 *  `axis` is the one the chart draws before any zoom, the read's: not the whole block, which can run past the read
 *  and name a kind with no band to show for it, and not the zoom's view, so the legend changes only when the read
 *  pages.
 *
 *  Two kinds that share a colour share a swatch and both names, the second in lower case as it continues
 *  the first (`Testing, edit–test loop`). A guess is hatched in the editing colour, not tinted, so it has
 *  a swatch of its own, at the editing colour's place, when a guessed phase or a guessed slice of a phase falls
 *  on the axis. Idle has an empty dashed swatch named `idle`, as its bands are named. A kind this page does not
 *  know is drawn in the grey of `unknown`, and is named with it. Empty where no phase falls on the axis. */
export function phaseLegend(list: Phase[], axis: [number, number]): LegendEntry[] {
  const [v0, v1] = axis;
  const here = list.filter((p) => onAxis(p.t0, p.t1, v0, v1));
  const kinds = new Set<string>(here.map((p) => p.kind));
  const guessed = kinds.has('edit_guess') || here.some((p) => (p.slices ?? []).some(([a, b]) => onAxis(a, b, v0, v1)));
  const items: { look: LegendEntry['look']; fill: string; names: string[] }[] = [];
  const byFill = new Map<string, string[]>();
  const tint = (fill: string, label: string) => {
    const names = byFill.get(fill);
    if (names) names.push(label);
    else {
      const e = { look: 'fill' as const, fill, names: [label] };
      byFill.set(fill, e.names);
      items.push(e);
    }
  };
  for (const [kind, label, , fill] of PHASES) {
    if (kind === 'idle') continue;
    if (kind === 'edit_guess') {
      if (guessed) items.push({ look: 'guess', fill, names: [GUESS_LEGEND] });
    } else if (kinds.has(kind)) tint(fill, label);
  }
  for (const kind of kinds) if (!BY_KIND.has(kind)) tint(phaseCol(kind), phaseLbl(kind));
  if (kinds.has('idle')) items.push({ look: 'idle', fill: 'none', names: ['idle'] });
  return items.map((e) => ({ look: e.look, fill: e.fill, name: e.names.map((n, i) => (i ? lowerFirst(n) : n)).join(', ') }));
}

/** The line that says where the phases the read's axis does not hold are, for a chart that cannot draw them (R78):
 *  `phases after 21:11:18 are in the list below`, `phases before 17:10:56 are in the list below`, or
 *  `phases before 17:10:56 and after 21:11:18 are in the list below`, the times as the session caption has them.
 *  A phase is outside where it does not fall on the axis (`onAxis`): before it if it ends at or before the axis's
 *  start, after it if it begins at or after the axis's end. Empty where the read holds every phase. */
export function phasesOutside(list: Phase[], axis: [number, number]): string {
  const [v0, v1] = axis;
  const out = list.filter((p) => !onAxis(p.t0, p.t1, v0, v1));
  const before = out.some((p) => p.t1 <= v0);
  const after = out.some((p) => p.t0 >= v1);
  if (before && after) return `phases before ${clock(v0)} and after ${clock(v1)} are in the list below`;
  if (after) return `phases after ${clock(v1)} are in the list below`;
  if (before) return `phases before ${clock(v0)} are in the list below`;
  return '';
}

/** What the page calls the three levels of a session: the tasks, the phases under them, and the processes, which are the
 *  bars the phases are tinted behind. The inspector's chart captions its lanes with them. */
export const LEVELS = { tasks: 'Tasks', phases: 'Phases', processes: 'Processes' } as const;
export type Level = keyof typeof LEVELS;

/** The levels the chart captions in its gutter, top to bottom: `tasks` where it has a lane of them, then `phases` where it
 *  draws phases, and `processes`, which the bars are, always. A block with no tasks has no lane, and so no Tasks caption,
 *  and keeps the gutter. A chart that draws no phases is the strip of process starts alone, and has only the last. */
export function chartLevels(hasTasks: boolean, hasPhases: boolean): Level[] {
  if (!hasPhases) return ['processes'];
  return hasTasks ? ['tasks', 'phases', 'processes'] : ['phases', 'processes'];
}

/** A letter of a level's caption is this wide, in px: the caption style (`.tn-cap .eyebrow`) is IBM Plex Mono at 9.5 px, a
 *  letter of 0.6 em, with its letters spaced by 0.08 em, which is 6.46 px, rounded up. */
export const CAP_CHAR_W = 6.5;
/** The room between the widest caption and the tracks that start after it, in px. */
export const GUTTER_PAD = 10;
/** The left gutter that stands beside the inspector's session track, chart, and zoom map, and that they all start after, in
 *  px: the widest caption (`Processes`) and its room. The mockup's label column is 72 px and its 8 px of gap. */
export const GUTTER = Math.ceil(Math.max(...Object.values(LEVELS).map((name) => name.length)) * CAP_CHAR_W) + GUTTER_PAD;
/** The width the chart is drawn at in a box `boxW` px wide: what the box leaves beside the gutter, and never under a pixel,
 *  so a box narrower than the gutter does not give the drawing a negative width. */
export const plotW = (boxW: number): number => Math.max(boxW - GUTTER, 1);

/** A band's label is left out where it does not fit, never cut: IBM Plex Mono at 10 px is about 6 px a
 *  character (`.ph-lbl`), with a little room either side. */
export const LABEL_CHAR_W = 6.1;
export const LABEL_PAD = 8;
export const labelFits = (label: string, w: number): boolean =>
  label.length > 0 && w >= label.length * LABEL_CHAR_W + LABEL_PAD;

/** A band is never drawn thinner than this, so a short stretch is still there. */
export const MIN_W = 1;
/** Neighbouring bands wider than `GAP_OVER` px are parted by `GAP` px, as a Ribbon's runs are, so two of one colour
 *  (a test and the loop after it) are still two. */
export const GAP = 0.6;
export const GAP_OVER = 3;

/** Where a band is drawn: the left edge and the width, in px, of the stretch `[a, b]` on the axis `[v0, v1]` drawn
 *  `W` px wide. It is cut at the axis's edges, never thinner than `MIN_W`, and kept inside the axis, so a stretch of no
 *  length is a pixel where it sits, and one wholly outside is a pixel against the edge it lies beyond. Given `gap`
 *  (`GAP`), a stretch wider than `GAP_OVER` is shortened by it, which parts it from the next. One rule for the session
 *  row's ribbon (PhaseBand), the task lane (`taskBlocks`), and the inspector's chart (TrajectoryNav), so that the bands
 *  of one picture line up. */
export function placeBand(a: number, b: number, axis: [number, number], W: number, gap = 0): [x: number, w: number] {
  const [v0, v1] = axis;
  const span = Math.max(v1 - v0, 1e-6);
  const X = (t: number) => ((t - v0) / span) * W;
  const xa = X(Math.max(a, v0));
  const raw = X(Math.min(b, v1)) - xa;
  const w = Math.max(MIN_W, raw - (gap && raw > GAP_OVER ? gap : 0));
  return [Math.min(xa, Math.max(0, W - w)), w];
}

/* ----------------------------------------------------------- the live edge */

/** The edge's short name: `running now`, `idle`, or what the census cannot say. An unknown edge is
 *  `not known yet` on a clock that goes on, and `not known` on a pinned one: a frozen capture will not
 *  grow. Empty where the service sent no edge at all. */
function edgeShort(now: TrajRunning['now'] | null | undefined): string {
  switch (now?.state) {
    case 'running':
      return 'running now';
    case 'idle':
      return 'idle';
    case 'unknown':
      return clockPinned() ? 'not known' : 'not known yet';
    default:
      return '';
  }
}

/** The live edge's name: `running now · python, srun`, `idle`, `not known yet`, or `not known`. The
 *  commands are named, not counted: the census counts processes, which a reader would take for calls. A
 *  process the collector could not name (`?`) is `unnamed` (R71). */
export function edgeLabel(now: TrajRunning['now'] | null | undefined): string {
  if (now?.state !== 'running') return edgeShort(now);
  const cmds = (now.cmds ?? []).map(([c]) => (c === '?' ? 'unnamed' : c)).join(', ');
  return cmds ? `running now · ${cmds}` : 'running now';
}

/** What stands behind the edge's name, for the list or a tooltip: where a running call was read from,
 *  and that an idle one is a proxy. Empty for an unknown edge, whose name already says so. */
export function edgeWhat(now: TrajRunning['now'] | null | undefined): string {
  if (now?.state === 'running') {
    return now.at != null
      ? `from the per-minute census, at ${clock(now.at).slice(0, 5)}`
      : 'from the per-minute census';
  }
  if (now?.state === 'idle') return 'idle · nothing new running since the session began (per-minute census)';
  return '';
}

/** What a hover on the live edge says: its name, when it began and where it ends, and how long, then what
 *  stands behind it: `running now · python, srun · 11:08:20–11:33:20 · 25 min`. */
export function edgeTip(now: TrajRunning['now'] | null | undefined, from: number, to: number): string {
  const head = `${edgeLabel(now)} · ${clock(from)}–${clock(to)} · ${dur(to - from)}`;
  const why = edgeWhat(now);
  return why ? `${head}\n${why}` : head;
}

/** Where the live edge lies on a view `[v0, v1]`. It runs from the last phase's end to `nowS`, and like every
 *  phase it is cut at the view's edges. Returns the whole stretch and the part in view, or null when the list
 *  is empty or none of it is in view: a chart whose axis ends before it (the read ends at its newest record,
 *  which is where the last phase ends) has no room for it, and the list under it says it. */
export function edgeSpan(
  list: Phase[],
  nowS: number,
  v0: number,
  v1: number,
): { from: number; to: number; a: number; b: number } | null {
  if (!list.length) return null;
  const from = list[list.length - 1].t1;
  const to = Math.max(nowS, from);
  const a = Math.max(from, v0);
  const b = Math.min(to, v1);
  return b > a ? { from, to, a, b } : null;
}

/* ---------------------------------------------------------- the whole row */

/** Phases this many or fewer are all named in a sequence line; more, and the middle is elided. */
const SEQ_NAMED = 9;
/** …and then the first and last this many are kept, around a `…`. */
const SEQ_ENDS = 4;
/** A phase this share of the session's span or more has its duration after its name. */
const SEQ_LONG = 0.25;

/** The phases in order, as one line: `explore → edit? → edit–test loop ×4 1.2 h → git → running now`.
 *
 *  A phase that is a quarter of the session or more says how long. Past nine phases it names the first
 *  four and the last four around a `…`. The live edge ends it: `running now`, `idle`, or `not known yet`
 *  (`not known` on a pinned clock). The session's span is the list's own, first start to last end. */
export function sequenceLine(list: Phase[], now?: TrajRunning['now'] | null): string {
  const span = list.length ? list[list.length - 1].t1 - list[0].t0 : 0;
  const name = (p: Phase): string => {
    const d = p.t1 - p.t0;
    return span > 0 && d >= span * SEQ_LONG ? `${phaseShort(p)} ${dur(d)}` : phaseShort(p);
  };
  const parts =
    list.length > SEQ_NAMED
      ? [...list.slice(0, SEQ_ENDS).map(name), '…', ...list.slice(-SEQ_ENDS).map(name)]
      : list.map(name);
  const edge = edgeShort(now);
  if (edge) parts.push(edge);
  return parts.join(' → ');
}

/* ------------------------------------------------------------ housekeeping */

/** The housekeeping shape of commands under a process that never exited (`phase_segment.NEVER_EXITED`). It
 *  can be among a block's `shapes`, and is kept out of the two the housekeeping line names. */
export const HK_NEVER_EXITED = 'under a process that never exited';

/** The housekeeping line's text, under the chart: `housekeeping, not the model: 214 commands (git, realpath)`.
 *
 *  Commands under a process that never exited are not known to be the agent's own, so they are not
 *  counted as "not the model": they follow apart, as `· 12 more under a process that never exited`.
 *  Their count is the block's own `never_exited`, not a look in `shapes`, which holds only the top five
 *  and may not list that shape at all (R43). */
export function housekeepingText(hk: PhaseHousekeeping): string {
  const extra = hk.never_exited;
  const own = Math.max(0, hk.n - extra);
  const top = hk.shapes
    .filter(([s]) => s !== HK_NEVER_EXITED)
    .slice(0, 2)
    .map(([s]) => s);
  let text = `housekeeping, not the model: ${fint(own)} ${own === 1 ? 'command' : 'commands'}`;
  if (top.length) text += ` (${top.join(', ')})`;
  if (extra > 0) text += ` · ${fint(extra)} more under a process that never exited`;
  return text;
}
