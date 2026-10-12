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

/* Time axis in epoch seconds: ticks on LOCAL wall-clock multiples of a round
 * step, so a label reads `14:00` and not `14:07`. The step is the first that
 * puts at most `target` ticks across the span, at any window. Local rather
 * than epoch-modulo, because a half-hour timezone would otherwise tick on the
 * half hour; every step under a day divides a day, so the alignment survives
 * midnight. */
const TIME_STEPS_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080];

export function timeTicks(x0: number, x1: number, target = 7): number[] {
  if (!(x1 > x0)) return [x0];
  const spanMin = (x1 - x0) / 60;
  const step = TIME_STEPS_MIN.find((s) => spanMin / s <= target) ?? TIME_STEPS_MIN[TIME_STEPS_MIN.length - 1];
  const d = new Date(x0 * 1000);
  d.setSeconds(0, 0);
  if (step >= 1440) d.setHours(0, 0);
  else d.setHours(0, Math.ceil((d.getHours() * 60 + d.getMinutes()) / step) * step);
  const out: number[] = [];
  for (let e = d.getTime() / 1000; e <= x1; e = d.getTime() / 1000) {
    if (e >= x0) out.push(e);
    if (step >= 1440) d.setDate(d.getDate() + step / 1440);
    else d.setMinutes(d.getMinutes() + step);
  }
  return out;
}

/** A tick label: the date at midnight, the clock otherwise. */
export function clockLabel(epoch: number): string {
  const d = new Date(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return d.getHours() === 0 && d.getMinutes() === 0
    ? `${p(d.getMonth() + 1)}-${p(d.getDate())}`
    : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** A hover or range label: the date as well, since a multi-day axis repeats
 * every clock reading. */
export function fullClock(epoch: number): string {
  const d = new Date(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
