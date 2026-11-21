/* One whole, split into its parts: a 100%-stacked bar.
 *
 * Who uses what draws each unit's whole as one of these, so the split reads at
 * a glance and the lead can be followed from unit to unit. A stacked bar alone
 * loses the smallest part -- the IDE's 0.3% of the processes is a sliver no one
 * can read -- so it never stands alone: every part's share and amount are
 * written out beside it (OverviewTab's class columns), and here a part above
 * zero keeps at least a sliver, with its name, share and amount on hover. A
 * part at zero draws nothing.
 */
import { pctLabel } from './ShareCell';

export interface SplitPart {
  k: string;
  /** the part's name, as hover and the bar's accessible name say it */
  lbl: string;
  /** the part's amount, in the unit's own terms; its width is its share of the sum */
  v: number;
  /** the amount as the page writes it */
  amt: string;
  /** its fill: the class's colour */
  c: string;
  /** a counted part that is not one of the classes shown -- `unlabeled`, or a
   *  class the chips hide -- drawn as a neutral, hatched part */
  muted?: boolean;
  /** a line hover adds, under the share */
  note?: string;
}

export function SplitBar({ parts, what }: { parts: SplitPart[]; what: string }) {
  const drawn = parts.filter((p) => p.v > 0);
  const tot = drawn.reduce((a, p) => a + p.v, 0);
  if (!tot) return null;
  const pctOf = (p: SplitPart) => pctLabel((100 * p.v) / tot);
  return (
    <div className="split-bar" role="img" aria-label={`${what}: ${drawn.map((p) => `${p.lbl} ${pctOf(p)}`).join(', ')}`}>
      {drawn.map((p) => (
        <i
          key={p.k}
          className={p.muted ? 'seg muted' : 'seg'}
          style={{ flexGrow: p.v, background: p.muted ? undefined : p.c }}
          data-tip={[`${p.lbl}: ${pctOf(p)} of ${what} · ${p.amt}`, p.note].filter(Boolean).join('\n')}
        />
      ))}
    </div>
  );
}
