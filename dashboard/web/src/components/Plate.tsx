/* Stat plate: a total, and the per-class breakdown underneath it.
 *
 * A plate carries all three classes unless its question only has an agent
 * answer (the Sandbox tab: whether an agent's tool calls were isolated). Then
 * `by` is left out and the row goes with it; a row of dashes for the classes
 * the question does not apply to is noise, not a breakdown.
 */
import { CLS, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint } from '../lib/format';
import type { Cls } from '../api/types';

export function Plate({
  k,
  total,
  stripe = 'var(--s1)',
  by,
  note,
  tip,
  fmtFn = fint,
  unit,
  classes = CLS,
  onClick,
  linkTip,
}: {
  k: string;
  total: number | null | undefined;
  stripe?: string;
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
      className={`plate${onClick ? ' link' : ''}`}
      style={{ '--stripe': stripe } as React.CSSProperties}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === 'Enter' && onClick() : undefined}
      data-tip={onClick ? linkTip : undefined}
    >
      <div className="k" data-tip={tip}>
        {k}
      </div>
      <div className="v">
        {total == null ? DASH : fmtFn(total)}
        {unit && total != null && <small> {unit}</small>}
      </div>
      {by && (
        <div className="cls3">
          {classes.map((c) => (
            <span key={c} data-tip={CLS_LBL[c]}>
              <i style={{ background: CLS_COL[c] }} />
              <b>{c in by ? fmtFn(by[c]) : DASH}</b>
            </span>
          ))}
        </div>
      )}
      {note && <div className="n">{note}</div>}
    </div>
  );
}
