/* Axis maths, ported from the archived template. */
import { zdate, zinstant } from '../lib/zone.ts';

/** Round ticks from 0 up to at least `max`, about `n` steps. `integer`: a count
 * axis, whose steps are whole numbers -- a gridline at 0.5 trees marks nothing,
 * and labelled with a rounded count it reads as a value it is not. Each tick is
 * `i * step`, not a running sum, so 0.1 + 0.2 never makes 0.30000000000000004. */
export function niceTicks(max: number, n?: number, integer = false): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / (n || 4);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const steps = [1, 2, 2.5, 5, 10].map((m) => m * mag).filter((s) => !integer || Number.isInteger(s));
  const step = integer && raw <= 1 ? 1 : steps.find((s) => s >= raw) || 10 * mag;
  const at = (i: number) => +(i * step).toPrecision(12);
  const out: number[] = [];
  for (let i = 0; at(i) <= max * 1.0001; i++) out.push(at(i));
  if (out[out.length - 1] < max) out.push(at(out.length));
  return out;
}

/** The fewest decimals, at most `max`, that write `s` (above 0) exactly; null
 * when more would be needed. A rounding to 0 never writes it: a step of 1e-6
 * is within the tolerance of 0, and was written as 0 at every tick. */
function decimals(s: number, max: number): number | null {
  for (let d = 0; d <= max; d++) {
    const r = Math.round(s * 10 ** d);
    if (r !== 0 && Math.abs(s * 10 ** d - r) <= 1e-6) return d;
  }
  return null;
}

/** The labels of one axis, written alike: one unit (k, M, B) and one number of
 * decimals for every tick, the fewest that write its step exactly (25 → none,
 * 2.5 → one, 0.25 → two, 0.0005 → four). Each tick written by itself (`fmt`)
 * gave one axis `0.00 · 25.0 · 100`, or `0.00 · 5,000 · 10k`. The baseline is
 * `0`. Ticks only: a value read off a bar or a hover keeps its own precision.
 *
 * Every label stays within six characters, the room the charts' left margin
 * gives it (an svg cuts what passes its edge: `0.000025` would show as
 * `000025`). A step finer than 0.0001 would need more, so that axis is written
 * in powers of ten instead, its mantissas alike: `2.5e-4 · 5.0e-4 · 1.0e-3`.
 * That bound is for the ticks the charts ask `niceTicks` for (`n` = 4): with
 * more steps, a 2.5 step reaches 12.5, and the mantissas take two decimals
 * (`1.25e-4`, seven characters). */
export function axisFmt(ticks: number[]): (v: number) => string {
  const top = ticks.reduce((a, t) => Math.max(a, Math.abs(t)), 0);
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : top || 1;
  const [div, unit] = top >= 1e9 ? [1e9, 'B'] : top >= 1e6 ? [1e6, 'M'] : top >= 1e4 ? [1e3, 'k'] : [1, ''];
  const d = decimals(step / div, 4);
  if (d === null) {
    const mantissa = (t: number) => t / 10 ** Math.floor(Math.log10(Math.abs(t)));
    const k = Math.max(0, ...ticks.filter((t) => t !== 0).map((t) => decimals(mantissa(t), 2) ?? 2));
    return (v: number) => (v === 0 ? '0' : v.toExponential(k));
  }
  return (v: number) => {
    if (v === 0) return '0';
    const x = v / div;
    // under 10,000 and whole, with the thousands separator `fmt` writes too
    return (d || unit ? x.toFixed(d) : Math.round(x).toLocaleString()) + unit;
  };
}

/* Time axis in epoch seconds: ticks on the page zone's wall-clock multiples of a round
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
  // stepped on the page zone's wall clock (lib/zone.ts), not the browser's
  const d = zdate(x0 * 1000);
  d.setUTCSeconds(0, 0);
  if (step >= 1440) d.setUTCHours(0, 0);
  else d.setUTCHours(0, Math.ceil((d.getUTCHours() * 60 + d.getUTCMinutes()) / step) * step);
  const out: number[] = [];
  for (let e = zinstant(d) / 1000; e <= x1; e = zinstant(d) / 1000) {
    // A wall time the zone springs over is no tick: it reads as the time past the
    // change (`zinstant`), which the stepping reaches as itself, so each tick is
    // later than the one before.
    if (e >= x0 && zdate(e * 1000).getTime() === d.getTime()) out.push(e);
    if (step >= 1440) d.setUTCDate(d.getUTCDate() + step / 1440);
    else d.setUTCMinutes(d.getUTCMinutes() + step);
  }
  return out;
}

/** How many time ticks a chart `W` wide carries legibly: one per 64px of plot
 * (W less the axis margins), never more than `max` -- the count the chart was
 * drawn with at its natural width -- and never fewer than three. A wide chart
 * keeps its ticks; a narrow one thins them rather than overprinting them. */
export function tickTarget(W: number, max: number, margins = 60): number {
  return Math.max(3, Math.min(max, Math.floor((W - margins) / 64)));
}

/** A tick label: the date at midnight, the clock otherwise. */
export function clockLabel(epoch: number): string {
  const d = zdate(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return d.getUTCHours() === 0 && d.getUTCMinutes() === 0
    ? `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`
    : `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

/** A hover or range label: the date as well, since a multi-day axis repeats
 * every clock reading. */
export function fullClock(epoch: number): string {
  const d = zdate(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}
