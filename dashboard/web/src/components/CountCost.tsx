/* Count and cost, side by side -- the single most load-bearing component here.
 *
 * Any categorical breakdown goes through this: it renders TWO charts, one
 * event-weighted and one CPU-weighted, each with its own single axis. There is
 * deliberately no prop that would put them on one pair of axes, because a dual
 * axis is what lets `poll/probe` at ~29% of calls and ~0% of CPU look like one
 * quantity.
 */
import { GroupBars } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Legend } from './Panel';

export function CountCost({
  groups,
  series,
  W = 1060,
  H = 230,
  maxBar = 26,
  countLabel = 'share of processes',
  costLabel = 'share of CPU seconds',
  countAxis = '% of processes',
  costAxis = '% of CPU seconds',
  pctAxis = true,
  labelTop = true,
  countFmt,
  costFmt,
}: {
  /** One group per category; `v` holds `${cls}` -> count and `cpu:${cls}` -> cost. */
  groups: BarGroup[];
  series: BarSeries[];
  W?: number;
  H?: number;
  maxBar?: number;
  countLabel?: string;
  costLabel?: string;
  countAxis?: string;
  costAxis?: string;
  pctAxis?: boolean;
  /** print each bar's value above it. On by default: a share near 0 draws a bar
   *  a pixel tall, and hover is not a reasonable way to read a number. */
  labelTop?: boolean;
  /** how each chart writes a value and a tick when the axes are not shares */
  countFmt?: (v: number) => string;
  costFmt?: (v: number) => string;
}) {
  const f = pctAxis
    ? { fmtVal: (v: number) => v.toFixed(1) + '%', tickFmt: (t: number) => t + '%' }
    : {};
  const fCount = !pctAxis && countFmt ? { fmtVal: countFmt, tickFmt: countFmt } : f;
  const fCost = !pctAxis && costFmt ? { fmtVal: costFmt, tickFmt: costFmt } : f;
  const costGroups: BarGroup[] = groups.map((g) => ({
    k: g.k,
    k2: g.k2,
    v: Object.fromEntries(series.map((s) => [s.k, g.v['cpu:' + s.k] ?? null])),
    // a group's `tips` carry the count tip under `<series>` and the cost tip
    // under `cpu:<series>`, as its values do
    tips: g.tips
      ? Object.fromEntries(series.map((s) => [s.k, g.tips?.['cpu:' + s.k] ?? ''])
          .filter(([, t]) => t))
      : undefined,
  }));
  return (
    <>
      <div className="eyebrow">{countLabel}</div>
      <GroupBars
        groups={groups}
        series={series}
        W={W}
        H={H}
        maxBar={maxBar}
        ylabel={countAxis}
        labelTop={labelTop}
        {...fCount}
      />
      <div className="eyebrow" style={{ marginTop: 6 }}>
        {costLabel}
      </div>
      <GroupBars
        groups={costGroups}
        series={series}
        W={W}
        H={H}
        maxBar={maxBar}
        ylabel={costAxis}
        labelTop={labelTop}
        {...fCost}
      />
      <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
    </>
  );
}
