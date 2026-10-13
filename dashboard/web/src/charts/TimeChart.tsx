/* A time series on the window's wall-clock axis, shared by every tab that
 * draws one (Live's charts and picker, Resources, I/O).
 *
 * Bins on a shared axis: `perMin` turns a per-bin SUM into a rate, so the scale
 * reads the same at every window; a sampled level is left as it is. Charts that
 * take the same `view` zoom together, so a spike can be read across exits, CPU,
 * and memory at once. A zoom stops at three bins: finer than that, the chart
 * would imply a resolution the bins never had.
 */
import { Lines } from './primitives';
import type { LineSeries } from './primitives';
import { clockLabel, fullClock, timeTicks } from './scale';
import { Legend } from '../components/Panel';

/* Tools are not classes, so their lines skip the class hues (crimson is the
 * agent's alone); everything past the named tools is the grey tail. */
// Ordered so no two neighbours are close: --s5 next to --s4 is violet on purple.
export const TOOL_COL = ['var(--s4)', 'var(--s6)', 'var(--s5)', 'var(--s8)', 'var(--s7)', 'var(--s2)'];

export function binLabel(binS: number): string {
  return binS % 3600 === 0 ? `${binS / 3600} h` : `${binS / 60} min`;
}

export interface TimeLine {
  lbl: string;
  c: string;
  v: (number | null)[] | undefined;
  dashed?: boolean;
  /** a point opens the commands behind its bin, which starts at `t` */
  onPick?: (t: number) => void;
}

export const anyValue = (lines: TimeLine[]) => lines.some((l) => l.v?.some((x) => x != null));

export type View = [number, number] | null;

/** Bins on a shared wall-clock axis. `perMin` turns a per-bin SUM into a rate,
 * so the scale reads the same at every window; a sampled level is left as is.
 * Every chart on the tab takes the same `view`, so a zoom into one is a zoom
 * into all of them, and a spike can be read across exits, CPU and memory at
 * once. It stops at three bins: finer than that, the chart would be implying a
 * resolution the bins never had. */
export function TimeChart({
  s,
  lines,
  ylabel,
  yname,
  perMin = false,
  scale = 1,
  view,
  onView,
  yfmt,
  ymax,
  legend,
}: {
  /** any binned series: only its clock (`t`, `bin_s`) is read here */
  s: { t: number[]; bin_s: number };
  lines: TimeLine[];
  ylabel: string;
  yname: string;
  perMin?: boolean;
  scale?: number;
  view: View;
  onView: (v: View) => void;
  yfmt?: (v: number) => string;
  ymax?: number;
  /** a legend other than one swatch per line, e.g. classes plus a line-style key */
  legend?: { k: string; c: string; dashed?: boolean; solid?: boolean }[];
}) {
  const k = scale * (perMin ? 60 / s.bin_s : 1);
  const x0 = s.t[0];
  const x1 = s.t[s.t.length - 1];
  const series: LineSeries[] = lines.map((l) => ({
    lbl: l.lbl,
    c: l.c,
    dashed: l.dashed,
    onPick: l.onPick,
    pts: s.t.map((x, i) => {
      const v = l.v?.[i];
      return { x, y: v == null ? null : v * k };
    }),
  }));
  return (
    <>
      <Lines
        series={series}
        W={520}
        H={210}
        ymin={0}
        ymax={ymax}
        yfmt={yfmt}
        xmin={x0}
        xmax={x1 > x0 ? x1 : x0 + s.bin_s}
        xticks={(a, b) => timeTicks(a, b)}
        xfmt={clockLabel}
        xtip={fullClock}
        ylabel={ylabel}
        xname="at"
        yname={yname}
        view={view}
        onView={onView}
        minSpan={3 * s.bin_s}
      />
      <Legend items={legend ?? lines.map((l) => ({ k: l.lbl, c: l.c, dashed: l.dashed }))} />
    </>
  );
}

