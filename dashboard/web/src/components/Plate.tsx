/* Stat plate: a total, and the per-class breakdown underneath it.
 *
 * Every plate carries ALL THREE classes. A single headline number with no class
 * split is the shape that let "agent" absorb VS Code, so the breakdown row is a
 * required prop, not an option.
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
}: {
  k: string;
  total: number | null | undefined;
  stripe?: string;
  /** per-class values; a class absent from the map renders as a dash, not 0 */
  by: Partial<Record<string, number | null>>;
  note?: React.ReactNode;
  /** hover text for the label: how this number is counted, and what it overcounts */
  tip?: string;
  fmtFn?: (n: number | null | undefined) => string;
  unit?: string;
  classes?: Cls[];
}) {
  return (
    <div className="plate" style={{ '--stripe': stripe } as React.CSSProperties}>
      <div className="k" data-tip={tip}>
        {k}
      </div>
      <div className="v">
        {total == null ? DASH : fmtFn(total)}
        {unit && total != null && <small> {unit}</small>}
      </div>
      <div className="cls3">
        {classes.map((c) => (
          <span key={c} data-tip={CLS_LBL[c]}>
            <i style={{ background: CLS_COL[c] }} />
            <b>{c in by ? fmtFn(by[c]) : DASH}</b>
          </span>
        ))}
      </div>
      {note && <div className="n">{note}</div>}
    </div>
  );
}
