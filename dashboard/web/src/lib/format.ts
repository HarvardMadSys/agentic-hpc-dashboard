/* Formatters.
 *
 * The invariant that matters: `null`/`undefined` renders as an em dash, NEVER as
 * 0. "Zero is not missing" is a rendering rule as much as a layout rule, so the
 * numeric formatters refuse to invent a zero for an absent value.
 */
import type { Accum, Quant } from '../api/types';

export const DASH = '—';

const nil = (n: unknown): boolean => n == null || (typeof n === 'number' && !Number.isFinite(n));

export function fmt(n: number | null | undefined, d?: number): string {
  if (nil(n)) return DASH;
  const v = n as number;
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(d ?? 2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(d ?? 2) + 'M';
  if (a >= 1e4) return Math.round(v / 1e3) + 'k';
  if (a >= 100) return Math.round(v).toLocaleString();
  if (a >= 10) return v.toFixed(d ?? 1);
  return v.toFixed(d ?? (a < 1 ? 2 : 1));
}

export function fint(n: number | null | undefined): string {
  return nil(n) ? DASH : Math.round(n as number).toLocaleString();
}

export function pc(n: number | null | undefined, d?: number): string {
  if (nil(n)) return DASH;
  const v = n as number;
  return (d != null ? v.toFixed(d) : v < 10 ? v.toFixed(1) : String(Math.round(v))) + '%';
}

export function dur(s: number | null | undefined): string {
  if (nil(s)) return DASH;
  const v = s as number;
  if (v < 1) return (v * 1000).toFixed(0) + ' ms';
  if (v < 90) return v.toFixed(v < 10 ? 1 : 0) + ' s';
  if (v < 5400) return (v / 60).toFixed(v < 600 ? 1 : 0) + ' min';
  if (v < 172800) return (v / 3600).toFixed(v < 36000 ? 1 : 0) + ' h';
  return (v / 86400).toFixed(1) + ' d';
}

export function bytes(b: number | null | undefined): string {
  if (nil(b)) return DASH;
  const v = b as number;
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < u.length - 1) {
    x /= 1024;
    i++;
  }
  return (i === 0 ? String(Math.round(x)) : x.toFixed(x < 10 ? 1 : 0)) + ' ' + u[i];
}

export function mb(v: number | null | undefined): string {
  if (nil(v)) return DASH;
  const n = v as number;
  if (Math.abs(n) >= 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + ' TB';
  if (Math.abs(n) >= 1024) return (n / 1024).toFixed(n / 1024 < 10 ? 2 : 1) + ' GB';
  return fmt(n) + ' MB';
}

/** Short relative age. */
export function ago(ms: number | null | undefined): string {
  if (nil(ms)) return DASH;
  const v = ms as number;
  if (v < 1000) return 'just now';
  if (v < 60000) return Math.round(v / 1000) + ' s ago';
  if (v < 3600000) return Math.round(v / 60000) + ' min ago';
  if (v < 86400000) return (v / 3600000).toFixed(1) + ' h ago';
  return (v / 86400000).toFixed(1) + ' d ago';
}

export function lagStr(lag_s: number | null | undefined): string {
  if (nil(lag_s)) return DASH;
  return dur(lag_s as number);
}

/* ------------------------------------------------------- coverage-aware */

/** True when an accumulator/quantile means "not measured", not "measured zero". */
export function notMeasured(o: Accum | Quant | null | undefined): boolean {
  if (!o) return true;
  return (o.n ?? 0) === 0;
}

/** `hh:mm:ss` from an epoch-seconds value, local time. */
export function clock(epoch: number | null | undefined): string {
  if (nil(epoch)) return DASH;
  return new Date((epoch as number) * 1000).toTimeString().slice(0, 8);
}
