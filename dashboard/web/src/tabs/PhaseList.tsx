/* The phase list: the inspector's chart in words, one row per phase.
 *
 * It is the chart's own block (`row.phases`, the same one the session row draws), so the list and the chart
 * never disagree. A row is a chip in the phase's colour, its name, when it began and ended, how long it
 * was, and what it was, in words (lib/phases.ts `phaseWhat`):
 *
 * - a GUESS is named `Editing (guess)`, in the ink as every name is, with a chip hatched in the guess colour
 *   (the colour as text is 3.46:1), and its detail is the reason: no command shows an edit there, so it is
 *   read from a pause
 * - IDLE has an empty dashed chip, as the chart has an empty dashed gap: no new call began in it. A call the agent
 *   moved past may still run through it, and its detail names the first of them (`phaseWhat`)
 * - an OPEN phase, one a call in which is still running, says so at the end of its detail: `a call in it is
 *   still running`, or `a call in it was still running when the record ended` on a pinned clock, then the
 *   programs seen under it, `, seen through sleep and ugrep` (R56, R65)
 * - a session running now ends in its LIVE EDGE, which is not a phase and so is not a row to click: what the
 *   per-minute census shows running (an empty dashed chip where it shows nothing, a dotted one where it
 *   cannot say), and where that was read from
 *
 * Clicking a phase picks it, as a click on its name on the chart does: the chart zooms to it and the tree
 * opens at its first call.
 *
 * A session with more calls than its log holds says from when its phases are incomplete, under the rows.
 *
 * A row of a phase (`PhaseRow`) and the live edge (`EdgeRow`) are exported, so that the list of tasks (TaskList.tsx), which
 * has each task open into its phases, draws them the same way.
 *
 * Not memoized, and read in the render of a page that repeats with every live frame: the live edge's name is
 * worded by `clockPinned()`, a plain flag that frame sets, as is an open phase's, and its end by
 * `serverNow()`.
 */
import type { Phase, PhaseBlock, TrajRunning } from '../api/types';
import { serverNow } from '../lib/clock';
import { clock, dur } from '../lib/format';
import { cutNote, edgeLabel, edgeWhat, phaseCol, phaseLbl, phaseWhat, zoomTip } from '../lib/phases';

type Look = 'fill' | 'guess' | 'idle' | 'unknown';

/** A phase's chip, as the chart draws its band: a fill, a hatch with a dashed outline, or an empty
 *  dashed outline for idle (`phaseCol` is `none`, and a fill would draw it as work). */
function Chip({ look, fill }: { look: Look; fill?: string }) {
  return <span className={`pl-chip ${look}`} style={look === 'fill' ? { background: fill } : undefined} aria-hidden="true" />;
}

/** One phase, as a row to click: its chip, name, when it began and ended, how long it was, and what it was. Picking it is
 *  the caller's: `onPick` is told which. */
export function PhaseRow({ p, onPick }: { p: Phase; onPick: (p: Phase) => void }) {
  const col = phaseCol(p.kind);
  const look: Look = p.kind === 'edit_guess' ? 'guess' : col === 'none' ? 'idle' : 'fill';
  return (
    <button type="button" className="pl-row" onClick={() => onPick(p)} data-tip={zoomTip(p.t0)}>
      <Chip look={look} fill={col} />
      <span className={`pl-name ${look}`}>{phaseLbl(p.kind)}</span>
      <span className="pl-when">{`${clock(p.t0)}–${clock(p.t1)}`}</span>
      <span className="pl-dur">{dur(p.t1 - p.t0)}</span>
      <span className="pl-what">{phaseWhat(p)}</span>
    </button>
  );
}

/** The live edge of a session running now, after its last phase: what the per-minute census shows running, from the last
 *  phase's end to now. Not a phase and not a row to click. Nothing where the session is not running, or has no phase. */
export function EdgeRow({
  block,
  now,
}: {
  block: PhaseBlock;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
}) {
  const list = block.list;
  const last = list.length ? list[list.length - 1] : null;
  const edge = last && now && (now.state === 'running' || now.state === 'idle' || now.state === 'unknown') ? now : null;
  if (!edge) return null;
  const from = last?.t1 ?? 0;
  const to = Math.max(serverNow() / 1000, from);
  // the edge is drawn as the chart draws it: filled in what is running, an empty dashed outline where the census
  // shows nothing beyond what was up already (a proxy for idle), and a dotted one where it cannot say
  const fill = edge.state === 'running' ? phaseCol(edge.cat) : 'none';
  const look: Look = edge.state === 'unknown' ? 'unknown' : fill === 'none' ? 'idle' : 'fill';
  return (
    <div className="pl-row edge" data-kind="edge">
      <Chip look={look} fill={fill} />
      <span className="pl-name edge">{edgeLabel(edge)}</span>
      <span className="pl-when">{`${clock(from)}–${clock(to)}`}</span>
      <span className="pl-dur">{dur(to - from)}</span>
      <span className="pl-what">{edgeWhat(edge)}</span>
    </div>
  );
}

export function PhaseList({
  block,
  now,
  onPick,
}: {
  block: PhaseBlock;
  /** what the per-minute census shows running at the live edge of a session running now */
  now?: TrajRunning['now'] | null;
  onPick: (p: Phase) => void;
}) {
  const note = cutNote(block);
  return (
    <div className="pl" role="group" aria-label="the phases, in order">
      {block.list.map((p, i) => (
        <PhaseRow key={i} p={p} onPick={onPick} />
      ))}
      <EdgeRow block={block} now={now} />
      {note && <div className="pl-note">{note}</div>}
    </div>
  );
}
