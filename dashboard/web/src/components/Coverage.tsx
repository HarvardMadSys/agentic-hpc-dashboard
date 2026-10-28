/* Coverage is part of the number.
 *
 * Wherever a payload carries `coverage_pct`, it is rendered next to the value.
 * An accumulator or quantile with `n: 0` / `coverage_pct: 0` means NOT MEASURED
 * and renders as the words `not measured` -- never as `0`, which would read as
 * "measured, and it was zero".
 */
import type { Accum, Quant } from '../api/types';
import { DASH, fmt, notMeasured, pc } from '../lib/format';
import type { Sorter } from '../lib/useSort';
import { SortTh } from './SortTh';

export function Coverage({ pct, n }: { pct: number | null | undefined; n?: number | null }) {
  if (pct == null) return null;
  const cls = pct <= 0 ? 'none' : pct < 50 ? 'low' : '';
  return (
    <span className={`cov ${cls}`} data-tip={n != null ? `${fmt(n)} processes carried the field` : undefined}>
      cov {pc(pct)}
    </span>
  );
}

/** An `{total, n, coverage_pct, mean}` accumulator. */
export function AccumCell({ a, unit }: { a: Accum | null | undefined; unit?: string }) {
  if (notMeasured(a)) return <span className="nm">not measured</span>;
  return (
    <>
      <span>
        {fmt(a!.total)}
        {unit ? ' ' + unit : ''}
      </span>
      <Coverage pct={a!.coverage_pct} n={a!.n} />
    </>
  );
}

/** The quantile spine, as text. `sampled` is surfaced because it bounds the tail. */
export function QuantCells({ q }: { q: Quant | null | undefined }) {
  // An all-zero metric reads as "not measured", with the reason on hover: a
  // column of 0.00 would otherwise be indistinguishable from a real finding.
  if (q?.all_zero)
    return (
      <>
        <td className="num" title={q.suspect ?? undefined}>
          <span className="nm">not measured</span>
        </td>
        <td className="num">{DASH}</td>
        <td className="num">{DASH}</td>
        <td className="num">{DASH}</td>
        <td className="num">
          <Coverage pct={q.coverage_pct} n={q.n} />
        </td>
      </>
    );
  if (notMeasured(q))
    return (
      <>
        <td className="num">
          <span className="nm">not measured</span>
        </td>
        <td className="num">{DASH}</td>
        <td className="num">{DASH}</td>
        <td className="num">{DASH}</td>
        <td className="num">{DASH}</td>
      </>
    );
  const v = q!;
  return (
    <>
      <td className="num">{fmt(v.p50)}</td>
      <td className="num">{fmt(v.p90)}</td>
      <td className="num">{fmt(v.p99)}</td>
      <td className="num">{fmt(v.max)}</td>
      <td className="num">
        <Coverage pct={v.coverage_pct} n={v.n} />
        {v.sampled && (
          <span className="cov" data-tip={`quantiles from a sample of ${fmt(v.sample_n)} processes`}>
            {' '}
            sampled
          </span>
        )}
      </td>
    </>
  );
}

export const QUANT_HEAD = (
  <>
    <th className="num">p50</th>
    <th className="num">p90</th>
    <th className="num">p99</th>
    <th className="num">max</th>
    <th className="num">coverage</th>
  </>
);

/** The same headers, sorting their table (roadmap note 27); `quantVal` reads
 * a row's value for one. A metric that was never measured sorts last. */
export function QuantHead({ s }: { s: Sorter<string> }) {
  return (
    <>
      <SortTh s={s} k="p50" num>p50</SortTh>
      <SortTh s={s} k="p90" num>p90</SortTh>
      <SortTh s={s} k="p99" num>p99</SortTh>
      <SortTh s={s} k="max" num>max</SortTh>
      <SortTh s={s} k="coverage_pct" num>coverage</SortTh>
    </>
  );
}
export const quantVal = (q: Quant | null | undefined, k: string): number | null =>
  !q || (q.all_zero && k !== 'coverage_pct') ? null
  : ((q as unknown as Record<string, number | null>)[k] ?? null);
