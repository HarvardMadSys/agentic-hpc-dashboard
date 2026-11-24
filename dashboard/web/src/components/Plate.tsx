/* Stat plate: a total, and the per-class breakdown underneath it.
 *
 * A plate carries all three classes unless its question only has an agent
 * answer (the Users tab's: agent users, agent sessions and how they were launched). Then
 * `by` is left out and the row goes with it; a row of dashes for the classes
 * the question does not apply to is noise, not a breakdown.
 *
 * A plate has no colour of its own. The series hues mean agent, human and IDE
 * everywhere else on the page, so a stripe in one of them on an unrelated
 * number reads as a class it is not. The one accent a plate takes is `alarm`:
 * a red edge for a count that is a problem when it is above zero.
 *
 * A class is never told by its colour alone: crimson and green, agent and
 * human, are one olive brown to a red-green colour-blind reader. Each swatch
 * in the row carries its class's word, and a plate with a row says whose its
 * headline is (`head`, the agent's: the row is for scale), so the label above
 * names only the measure.
 */
import { CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint } from '../lib/format';
import type { Cls } from '../api/types';

export function Plate({
  k,
  total,
  alarm = false,
  by,
  note,
  tip,
  fmtFn = fint,
  unit,
  classes = CLS,
  onClick,
  linkTip,
  head = by ? 'agent' : undefined,
}: {
  k: string;
  total: number | null | undefined;
  /** whose the headline is, said beside it; by default the agent's on a plate
   *  with a class row, and nothing on a plate about agents alone */
  head?: Cls;
  /** a red edge: this count is a problem, and it is above zero */
  alarm?: boolean;
  /** per-class values; a class absent from the map renders as a dash, not 0.
   *  Omitted: the plate is about agents alone, and shows no class row. */
  by?: Partial<Record<string, number | null>>;
  note?: React.ReactNode;
  /** hover text for the label: how this number is counted, and what it overcounts */
  tip?: string;
  fmtFn?: (n: number | null | undefined) => string;
  unit?: string;
  classes?: Cls[];
  /** the plate opens the section that breaks it down */
  onClick?: () => void;
  linkTip?: string;
}) {
  return (
    <div
      className={`plate${onClick ? ' link' : ''}${alarm ? ' alarm' : ''}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === 'Enter' && onClick() : undefined}
      data-tip={onClick ? linkTip : undefined}
    >
      <div className="k" data-tip={tip}>
        {k}
      </div>
      {/* under the label, in one piece: the plates of a row share the label's
          height (styles.css), so their numbers start on one line */}
      <div className="under">
        <div className="v">
          {total == null ? DASH : fmtFn(total)}
          {unit && total != null && <small> {unit}</small>}
          {head && total != null && <small className="whose"> {CLS_LBL[head]}</small>}
        </div>
        {by && (
          <div className="cls3">
            {classes.map((c) => (
              <span key={c}>
                <i style={{ background: CLS_COL[c] }} aria-hidden="true" />
                <span className="cl">{CLS_LBL[c]}</span>
                <b>{c in by ? fmtFn(by[c]) : DASH}</b>
              </span>
            ))}
          </div>
        )}
        {note && <div className="n">{note}</div>}
      </div>
    </div>
  );
}
