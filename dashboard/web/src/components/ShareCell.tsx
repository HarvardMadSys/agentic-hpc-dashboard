/* One share on its own 0-100% track, with its number printed beside it.
 *
 * Replaces the 100%-stacked bar wherever classes or states are compared: on a
 * stacked bar a 0.3% IDE share is a sliver no one can see or hover, and the
 * smallest class is usually the one a reader is checking. Here every value
 * keeps its own row of pixels -- at least a hairline when it is above zero --
 * and the percentage and count are always written out.
 */
import { DASH } from '../lib/format';

export function pctLabel(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return DASH;
  if (p === 0) return '0%';
  if (p < 0.1) return '<0.1%';
  if (p < 10) return `${p.toFixed(1)}%`;
  if (p > 99.9 && p < 100) return '>99.9%';
  return `${Math.round(p)}%`;
}

export function ShareCell({ pct, sub, c }: { pct: number | null | undefined; sub?: string; c: string }) {
  return (
    <div className="share-cell">
      <span className="share-track">
        {pct != null && pct > 0 && <i style={{ width: `max(2px, ${Math.min(100, pct)}%)`, background: c }} />}
      </span>
      <b>{pctLabel(pct)}</b>
      {sub != null && <span className="share-sub">{sub}</span>}
    </div>
  );
}
