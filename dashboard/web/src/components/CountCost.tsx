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
  countLabel = 'share of calls',
  costLabel = 'share of CPU seconds',
  countAxis = '% of calls',
  costAxis = '% of CPU seconds',
  pctAxis = true,
  labelTop = true,
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
}) {
  const f = pctAxis
    ? { fmtVal: (v: number) => v.toFixed(1) + '%', tickFmt: (t: number) => t + '%' }
    : {};
  const costGroups: BarGroup[] = groups.map((g) => ({
    k: g.k,
    k2: g.k2,
    v: Object.fromEntries(series.map((s) => [s.k, g.v['cpu:' + s.k] ?? null])),
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
        {...f}
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
        {...f}
      />
      <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
    </>
  );
}
