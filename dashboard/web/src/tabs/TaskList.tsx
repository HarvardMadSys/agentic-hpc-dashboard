/* The three-level list: the inspector's tasks, in words, and under a task its phases, and under a phase its commands.
 *
 * It is the chart's own block (`row.phases`), the one the session row draws and the lane above the chart draws its tasks
 * from, so the list and the chart never disagree. Where a block has tasks the inspector shows this list; where it has
 * none it shows the phase list (PhaseList.tsx), which is this list's rows with no level above them.
 *
 * - A TASK is a row: a chevron, its number, when it began and ended, its label, and what it holds (`6 phases · 208 calls`).
 *   A quoted label is in quotation marks and says where the agent's words came from (`commit`, `PR #235`, or `log`); a built
 *   label has neither. A task a call in which is still running, or one that may be missing calls the log was too full to
 *   keep, says so under its label, in the words the chart's hover and the phase list use for them (`taskState`).
 * - A GAP of an hour or more, in which the agent waited on nothing, is a muted line before the task it parts from the one
 *   before: `2.1 h with no new calls`, and the call it moved past that was still running, where one was.
 * - A LONE stretch, a few calls hours from other work, is a quiet line of its own, with no number: `3 calls at 10:41:24 ·
 *   read src/app.py`. It is no task, and opens into its phases like one. It is a row to click, so its text is `--ink-2`, which
 *   is quieter than a task's label, and not the gap line's `--muted`, which is too faint for text that takes a click.
 * - Opening a task lists its phases, in the phase list's own rows (`PhaseRow`): the ones its row counts, less the idle phase
 *   of the gap after it, which the gap line says. Opening a phase lists the commands that ran most in it, each as its
 *   masked preview with the calls that ran it, and a button that shows its calls in the tree.
 *
 * Clicking a task's row picks the task, as a click on its block in the chart's lane does: the chart zooms to it and the tree
 * opens at its first call. A click on a phase picks the phase. The chevron opens and closes what is under a row, and a
 * row picked is opened. The task that holds the chart's zoom is open by default, whichever way the chart came to be zoomed
 * on it, and a task the reader has opened or closed stays as they left it, as does a phase. Each is known by where it begins,
 * so it stays so when a session running now is cut again and the tasks around it split or merge. A task picked from the chart
 * is brought into view in the list, which scrolls on its own.
 *
 * A session running now ends in its live edge, and a session with more calls than its log holds says from when it is
 * incomplete, under the rows, both as the phase list does.
 *
 * Not memoized, and read in the render of a page that repeats with every live frame: a call still running is worded by
 * `clockPinned()`, a plain flag that frame sets, and the live edge's end by `serverNow()`.
 */
import { Fragment, useEffect, useRef, useState } from 'react';
import type { Phase, PhaseBlock, Task, TrajRunning } from '../api/types';
import { clock } from '../lib/format';
import { commandLine, cutNote, phaseLbl, zoomTip } from '../lib/phases';
import {
  gapLine,
  loneLine,
  taskInView,
  taskLabel,
  taskNumbers,
  taskPhases,
  taskSource,
  taskState,
  taskWhat,
} from '../lib/tasks';
import { EdgeRow, PhaseRow } from './PhaseList';

/** The button under a phase's commands: what it does is what a click on the phase does. */
const SHOW_CALLS = 'show its calls in the tree';

/** `set` with `k` taken out where it is in, and put in where it is not. */
const flipped = (set: Set<number>, k: number): Set<number> => {
  const next = new Set(set);
  if (!next.delete(k)) next.add(k);
  return next;
};

/** The chevron that opens and closes what is under a row. It is named for the row (`what`), once, and whether it is open is
 *  `aria-expanded`'s to say, so its name does not say it again. */
function Tog({ open, what, onClick }: { open: boolean; what: string; onClick: () => void }) {
  return (
    <button type="button" className="tl-tog" aria-expanded={open} aria-label={what} onClick={onClick}>
      {open ? '▾' : '▸'}
    </button>
  );
}

/** What is under an open phase: the commands that ran most in it (its `top`), a line each, as the masked preview the service
 *  keeps and, where more than one call ran it, how many; the whole preview is in the hover, since a line is cut where it runs
 *  long. Then the button that shows the phase's calls in the tree, which picks the phase as a click on its row does. */
export function PhaseCalls({ p, onPick }: { p: Phase; onPick: (p: Phase) => void }) {
  return (
    <div className="tl-calls">
      {(p.top ?? []).map(([cmd, n], j) => (
        <div key={j} className="tl-cmd" data-tip={cmd} data-tip-wide>
          {commandLine([cmd, n])}
        </div>
      ))}
      <button type="button" className="linkish tl-tree" onClick={() => onPick(p)}>
        {SHOW_CALLS}
      </button>
    </div>
  );
}

export function TaskList({
  block,
  now,
  view,
  onTask,
  onPhase,
}: {
  /** a block that has tasks */
  block: PhaseBlock;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
  /** the chart's zoom, which the task it is on is open for by default; or the stretch it was asked to zoom to and could not, as one as
   *  long as the read; null where it is neither */
  view?: [number, number] | null;
  onTask: (t: Task) => void;
  onPhase: (p: Phase) => void;
}) {
  const tasks = block.tasks ?? [];
  const numbers = taskNumbers(tasks);
  const held = taskInView(tasks, view);
  // What the reader chose for a task, which wins over the default, and the phases they opened. Each is known by where it begins
  // (`t0`), not by its place in the list: a session running now is cut and named again with every read, and a new split or a merge
  // moves the places of the tasks and phases after it, but leaves where each one begins.
  const [chose, setChose] = useState<Map<number, boolean>>(() => new Map());
  const [seen, setSeen] = useState<Set<number>>(() => new Set());
  const box = useRef<HTMLDivElement>(null);

  // A task that comes to hold the zoom is brought into view in the list, which scrolls on its own: the list only, never the page,
  // and only if its row is out of sight, so that a row clicked does not move.
  useEffect(() => {
    const list = box.current;
    const row = held >= 0 ? list?.querySelector<HTMLElement>(`[data-i="${held}"]`) : null;
    if (!list || !row) return;
    const at = row.getBoundingClientRect().top - list.getBoundingClientRect().top;
    if (at < 0 || at + row.offsetHeight > list.clientHeight) list.scrollTop += at - 2;
  }, [held]);

  const note = cutNote(block);
  return (
    <div className="pl tl" role="group" aria-label="the tasks, in order" ref={box}>
      {tasks.map((t, i) => {
        const lone = t.kind === 'lone';
        const open = chose.get(t.t0) ?? i === held;
        const source = taskSource(t);
        const state = taskState(t, block).join(' · ');
        const what = lone ? `the lone stretch at ${clock(t.t0)}` : `task ${numbers[i]}`;
        return (
          <Fragment key={t.t0}>
            {t.gap && <div className="tl-gap">{gapLine(t.gap)}</div>}
            <div className={`tl-row${lone ? ' lone' : ''}${open ? ' open' : ''}`} data-i={i}>
              <Tog open={open} what={what} onClick={() => setChose((m) => new Map(m).set(t.t0, !open))} />
              <button
                type="button"
                className="tl-pick"
                onClick={() => {
                  setChose((m) => new Map(m).set(t.t0, true));
                  onTask(t);
                }}
                data-tip={zoomTip(t.t0)}
              >
                {lone ? (
                  <span className="tl-line">{loneLine(t)}</span>
                ) : (
                  <>
                    <span className="tl-num">{numbers[i]}</span>
                    <span className="tl-when">{`${clock(t.t0)}–${clock(t.t1)}`}</span>
                    <span className="tl-label">
                      {taskLabel(t)}
                      {source && <span className="tl-src">{` · ${source}`}</span>}
                    </span>
                    <span className="tl-what">{taskWhat(t, block)}</span>
                  </>
                )}
                {state && <span className="tl-state">{state}</span>}
              </button>
            </div>
            {open && (
              <div className="tl-phases" role="group" aria-label={`the phases of ${what}`}>
                {taskPhases(t, block).map((p) => {
                  const opened = seen.has(p.t0);
                  return (
                    <Fragment key={p.t0}>
                      <div className="tl-ph">
                        {p.n > 0 ? (
                          <Tog open={opened} what={`${phaseLbl(p.kind)} ${clock(p.t0)}`} onClick={() => setSeen((s) => flipped(s, p.t0))} />
                        ) : (
                          <span className="tl-tog none" aria-hidden="true" />
                        )}
                        <PhaseRow p={p} onPick={onPhase} />
                      </div>
                      {opened && <PhaseCalls p={p} onPick={onPhase} />}
                    </Fragment>
                  );
                })}
              </div>
            )}
          </Fragment>
        );
      })}
      <EdgeRow block={block} now={now} />
      {note && <div className="pl-note">{note}</div>}
    </div>
  );
}
