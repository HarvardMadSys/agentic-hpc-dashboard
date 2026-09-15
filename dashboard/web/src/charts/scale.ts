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

export function logTicks(a: number, b: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(Math.max(a, 1e-9))); e <= Math.ceil(Math.log10(b)); e++) {
    const v = Math.pow(10, e);
    if (v >= a * 0.999 && v <= b * 1.001) out.push(v);
  }
  return out.length ? out : [a, b];
}

/** Largest finite value in a set of possibly-null numbers; 0 when all absent. */
export function maxOf(vals: (number | null | undefined)[]): number {
  let m = 0;
  for (const v of vals) if (v != null && Number.isFinite(v) && v > m) m = v;
  return m;
}
