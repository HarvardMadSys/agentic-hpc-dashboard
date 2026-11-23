/* The Live tab's rate chart: the service's bins of processes exited, summed
 * into the chart's bucket.
 *
 * The service bins exits by the minute (`live.rate`, `RollingBuckets.series`).
 * A long window sums them into 5-minute buckets, so a day is 288 points and not
 * 1,440 jittery ones. A window shorter than a dozen of those plots the
 * service's own bins: at the 5-minute floor no whole 5-minute bucket fits, and
 * the chart drew nothing at all.
 *
 * Pure, so mock/check_charts.mjs runs it as written.
 */
import type { RateRow } from '../api/types';
import type { LinePt } from '../charts/primitives';

/** The wide bucket, seconds, and how many of them a window must hold to use it. */
const WIDE_S = 300;
const MIN_BUCKETS = 12;

/** The rate chart's bucket, seconds, for a window of `windowMin` minutes over
 *  the service's `binS`-second bins: 5 minutes once the window holds a dozen
 *  of them (an hour is 12), the service's own bin below that. */
export function rateBucketS(windowMin: number | null | undefined, binS: number): number {
  const wide = Math.max(binS, WIDE_S);
  return windowMin != null && windowMin * 60 >= MIN_BUCKETS * wide ? wide : binS;
}

/** A bucket in words: the title's (`per 5 minutes`, `per minute`), the axis's
 *  (`5 min`, `min`), and the one an empty chart is short of (`5 minutes`, `minute`). */
export function rateWords(s: number): { per: string; unit: string; one: string } {
  if (s % 60) return { per: `per ${s} seconds`, unit: `${s} s`, one: `${s} seconds` };
  const m = s / 60;
  return m === 1
    ? { per: 'per minute', unit: 'min', one: 'minute' }
    : { per: `per ${m} minutes`, unit: `${m} min`, one: `${m} minutes` };
}

/** Each class's points: the bins summed into `bucketS`-second buckets on local
 *  wall-clock multiples (`tz`, the zone's offset in seconds, as
 *  `getTimezoneOffset() * 60` gives it). A bucket whose bins are all holes
 *  stays a hole; in one with any record, a class's holes add nothing. Only
 *  whole buckets are drawn: the newest bin is the minute still filling, and the
 *  oldest bucket can start before the window, so either edge would read low. */
export function rateSeries(
  rate: readonly RateRow[],
  cls: readonly string[],
  binS: number,
  bucketS: number,
  tz: number,
): LinePt[][] {
  const per = Math.max(1, Math.round(bucketS / binS));
  const w = per * binS;
  const pts: LinePt[][] = cls.map(() => []);
  const lastE = rate.length ? Number(rate[rate.length - 1].epoch) : NaN;
  let bx = NaN;
  let seen = 0;
  let acc: (number | null)[] = cls.map(() => null);
  const flush = () => {
    // x is the bucket's epoch, not its index: the window slides every minute,
    // and a zoom held in indices would drift off the times picked.
    if (Number.isFinite(bx) && seen === per && bx + w <= lastE) {
      for (let k = 0; k < cls.length; k++) pts[k].push({ x: bx, y: acc[k] });
    }
  };
  for (const r of rate) {
    const x = Number(r.epoch);
    if (!Number.isFinite(x)) continue;
    const b = Math.floor((x - tz) / w) * w + tz;
    if (b !== bx) {
      flush();
      bx = b;
      seen = 0;
      acc = cls.map(() => null);
    }
    seen++;
    for (let k = 0; k < cls.length; k++) {
      const v = r[cls[k]];
      if (typeof v === 'number') acc[k] = (acc[k] ?? 0) + v;
    }
  }
  flush();
  return pts;
}
