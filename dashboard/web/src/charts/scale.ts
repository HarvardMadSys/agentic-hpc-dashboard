/* Axis maths, ported from the archived template. */

export function niceTicks(max: number, n?: number): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / (n || 4);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
  const out: number[] = [];
  for (let v = 0; v <= max * 1.0001; v += step) out.push(v);
  if (out[out.length - 1] < max) out.push(out[out.length - 1] + step);
  return out;
}

/* Minutes between time-axis ticks, smallest first. */
const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080];

/** Epoch-second ticks across [from, to], at most `max` of them, on LOCAL
 *  wall-clock multiples of a round step -- `14:00`, never `14:07`. Built from
 *  local date fields, so a DST change or a half-hour timezone still ticks on
 *  the hour. */
export function timeTicks(from: number, to: number, max = 6): number[] {
  if (!(to > from)) return [];
  const span = (to - from) / 60;
  const step = TIME_STEPS.find((s) => span / s <= max) ?? TIME_STEPS[TIME_STEPS.length - 1];
  const d0 = new Date(from * 1000);
  const out: number[] = [];
  for (let k = 0; ; k++) {
    const t = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate(), 0, k * step).getTime() / 1000;
    if (t > to) break;
    if (t >= from) out.push(t);
  }
  return out;
}

/** Local `HH:MM`, or `MM-DD HH:MM` once a span is wider than a day -- the same
 *  rule `buckets.RollingBuckets.series` labels its rows by. */
export function timeFmt(spanS: number): (epoch: number) => string {
  return (epoch) => {
    const d = new Date(epoch * 1000);
    const p = (n: number) => String(n).padStart(2, '0');
    const hm = p(d.getHours()) + ':' + p(d.getMinutes());
    return spanS > 86400 ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm;
  };
}
