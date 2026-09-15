/* SVG chart primitives, ported from dashboard/archive/agent_dashboard.template.html.
 *
 * Hand-rolled rather than library-backed for two reasons that are constraints
 * here, not preferences:
 *   - every chart has exactly ONE value axis, and there is no code path that can
 *     add a second one;
 *   - the colours are the CSS custom properties verbatim, so no library theme
 *     can substitute a hue.
 *
 * Sizing follows the template's one portable idiom: natural width/height
 * attributes plus a viewBox, scaled by CSS (`max-width:100%; height:auto`).
 */
import type { ReactNode } from 'react';
import { niceTicks } from './scale';
import { fmt } from '../lib/format';

export interface SvgProps {
  w: number;
  h: number;
  children: ReactNode;
  label?: string;
}

export function Svg({ w, h, children, label }: SvgProps) {
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      width={w}
      height={h}
      role="img"
      aria-label={label}
      preserveAspectRatio="xMidYMin meet"
    >
      {children}
    </svg>
  );
}

function YGrid({ x0, x1, y, lbl }: { x0: number; x1: number; y: number; lbl: string }) {
  return (
    <>
      <line className="grid-line" x1={x0} x2={x1} y1={y} y2={y} />
      <text className="tick" x={x0 - 6} y={y + 3} textAnchor="end">
        {lbl}
      </text>
    </>
  );
}

/* ------------------------------------------------------------------ hbars */

export interface HBarItem {
  k: string;
  v: number | null;
  c?: string;
  lbl?: string;
  tip?: string;
}

export function HBars({
  items,
  W = 560,
  labelW = 132,
  valW = 54,
  rowH = 22,
  color = 'var(--s1)',
  max,
  unit,
}: {
  items: HBarItem[];
  W?: number;
  labelW?: number;
  valW?: number;
  rowH?: number;
  color?: string;
  max?: number;
  unit?: string;
}) {
  if (!items.length) return null;
  const gap = 5;
  const h = items.length * rowH + 6;
  const top = max || Math.max(...items.map((d) => d.v ?? 0), 1);
  const bw = W - labelW - valW;
  return (
    <Svg w={W} h={h}>
      {items.map((d, i) => {
        const y = i * rowH + 3;
        const bh = rowH - gap;
        // `null` is absent: draw no bar at all, and label it with a dash.
        const absent = d.v == null;
        const len = absent ? 0 : Math.max((d.v as number) > 0 ? 2 : 0, (bw * (d.v as number)) / top);
        return (
          <g key={d.k + '-' + i}>
            <text
              className="vlbl"
              x={labelW - 8}
              y={y + bh / 2 + 3.5}
              textAnchor="end"
              fill="var(--ink-2)"
            >
              {d.k}
            </text>
            {!absent && (
              <rect
                x={labelW}
                y={y}
                width={len}
                height={bh}
                rx={2}
                fill={d.c || color}
                data-tip={d.tip || `${d.k}: ${fmt(d.v)}${unit ? ' ' + unit : ''}`}
              />
            )}
            <text className="vlbl" x={labelW + len + 7} y={y + bh / 2 + 3.5}>
              {d.lbl != null ? d.lbl : fmt(d.v)}
            </text>
          </g>
        );
      })}
    </Svg>
  );
}

/* -------------------------------------------------------------- groupBars */

export interface BarGroup {
  k: string;
  k2?: string;
  v: Record<string, number | null>;
}
export interface BarSeries {
  k: string;
  lbl: string;
  c: string;
}

/** Grouped vertical bars on ONE value axis. There is no second-axis option. */
export function GroupBars({
  groups,
  series,
  W = 560,
  H = 200,
  maxBar = 34,
  ylabel,
  fmtVal,
  tickFmt,
  labelTop,
  max,
}: {
  groups: BarGroup[];
  series: BarSeries[];
  W?: number;
  H?: number;
  maxBar?: number;
  ylabel?: string;
  fmtVal?: (v: number) => string;
  tickFmt?: (v: number) => string;
  labelTop?: boolean;
  max?: number;
}) {
  if (!groups.length || !series.length) return null;
  const L = 44;
  const R = 8;
  const TOP = ylabel ? 20 : 10;
  const B = groups.some((g) => g.k2) ? 44 : 34;
  const pw = W - L - R;
  const ph = H - TOP - B;
  const dataMax =
    max || Math.max(...groups.flatMap((g) => series.map((s) => g.v[s.k] ?? 0)), 1);
  const ticks = niceTicks(dataMax, 4);
  const topV = ticks[ticks.length - 1];
  const y = (v: number) => TOP + ph - (ph * v) / topV;
  const gw = pw / groups.length;
  const bw = Math.min(maxBar, (gw - 14) / series.length);
  const F = (v: number) => (fmtVal ? fmtVal(v) : fmt(v));
  return (
    <Svg w={W} h={H} label={ylabel}>
      {ticks.map((t) => (
        <YGrid key={t} x0={L} x1={W - R} y={y(t)} lbl={tickFmt ? tickFmt(t) : fmt(t)} />
      ))}
      {groups.map((g, gi) => {
        const x0 = L + gi * gw + (gw - bw * series.length - 2 * (series.length - 1)) / 2;
        return (
          <g key={g.k + '-' + gi}>
            {series.map((se, si) => {
              const raw = g.v[se.k];
              if (raw == null) return null; // absent, not zero: no bar drawn
              const x = x0 + si * (bw + 2);
              const h = Math.max(raw > 0 ? 1.5 : 0, y(0) - y(raw));
              return (
                <rect
                  key={se.k}
                  x={x}
                  y={y(raw)}
                  width={bw}
                  height={h}
                  rx={2}
                  fill={se.c}
                  data-tip={`${g.k} · ${se.lbl}: ${F(raw)}`}
                />
              );
            })}
            {labelTop &&
              series.map((se, si) => {
                const raw = g.v[se.k];
                if (raw == null || raw <= 0) return null;
                return (
                  <text
                    key={'l' + se.k}
                    className="vlbl"
                    x={x0 + si * (bw + 2) + bw / 2}
                    y={y(raw) - 4}
                    textAnchor="middle"
                  >
                    {F(raw)}
                  </text>
                );
              })}
            <text className="tick" x={L + gi * gw + gw / 2} y={TOP + ph + 15} textAnchor="middle">
              {g.k}
            </text>
            {g.k2 && (
              <text
                className="tick"
                x={L + gi * gw + gw / 2}
                y={TOP + ph + 27}
                textAnchor="middle"
                fill="var(--muted)"
              >
                {g.k2}
              </text>
            )}
          </g>
        );
      })}
      <line className="base-line" x1={L} x2={W - R} y1={y(0)} y2={y(0)} />
      {ylabel && (
        <text className="axlbl" x={0} y={9}>
          {ylabel}
        </text>
      )}
    </Svg>
  );
}

/* --------------------------------------------------------------- stackRow */

export interface StackSeg {
  k: string;
  v: number;
  c: string;
}

/** One 100%-stacked row, 2px surface gaps, selective in-bar labels. */
export function StackRow({
  segs,
  W = 560,
  H = 30,
}: {
  segs: StackSeg[];
  W?: number;
  H?: number;
}) {
  const tot = segs.reduce((a, d) => a + (d.v || 0), 0);
  if (!(tot > 0)) return null;
  let x = 0;
  const out: ReactNode[] = [];
  segs.forEach((d, i) => {
    const w = (W * (d.v || 0)) / tot;
    if (w <= 0) return;
    const pctv = (100 * d.v) / tot;
    out.push(
      <rect
        key={'r' + d.k + i}
        x={x}
        y={0}
        width={Math.max(0, w - 2)}
        height={H}
        rx={1.5}
        fill={d.c}
        data-tip={`${d.k}: ${fmt(d.v)} (${pctv.toFixed(1)}%)`}
      />,
    );
    if (w > 44)
      out.push(
        <text
          key={'t' + d.k + i}
          x={x + (w - 2) / 2}
          y={H / 2 + 3.5}
          textAnchor="middle"
          style={{ fontSize: '10.5px', fontFamily: "'IBM Plex Mono',monospace", fill: 'var(--surface)' }}
        >
          {pctv.toFixed(w > 62 ? 1 : 0)}%
        </text>,
      );
    x += w;
  });
  return (
    <Svg w={W} h={H}>
      {out}
    </Svg>
  );
}

/* ------------------------------------------------------------------ lines */

export interface LinePt {
  x: number;
  y: number | null;
}
export interface LineSeries {
  lbl: string;
  c: string;
  pts: LinePt[];
  area?: boolean;
  mark?: string | null;
  dashed?: boolean;
}

/** Multi-line chart on ONE value axis.
 *
 * A `null` y is a GAP. The path is emitted as separate subpaths around it, so a
 * minute with no records reads as a hole in the line -- never as a measured 0,
 * and never as a straight interpolation across the hole. */
export function Lines({
  series,
  W = 560,
  H = 210,
  L = 48,
  ymin,
  ymax,
  xmin,
  xmax,
  xticks,
  xfmt,
  yfmt,
  ylabel,
  xlabel,
  xname,
  yname,
  ylog,
  xlog,
}: {
  series: LineSeries[];
  W?: number;
  H?: number;
  L?: number;
  ymin?: number;
  ymax?: number;
  xmin?: number;
  xmax?: number;
  xticks?: number[];
  xfmt?: (v: number) => string;
  yfmt?: (v: number) => string;
  ylabel?: string;
  xlabel?: string;
  xname?: string;
  yname?: string;
  ylog?: boolean;
  xlog?: boolean;
}) {
  const finite = series.flatMap((s) => s.pts.filter((p) => p.y != null));
  if (!series.length) return null;
  const R = 12;
  const TOP = ylabel ? 22 : 12;
  const B = 34;
  const pw = W - L - R;
  const ph = H - TOP - B;
  const xs = series.flatMap((s) => s.pts.map((p) => p.x));
  const ys = finite.map((p) => p.y as number);
  const x0 = xmin != null ? xmin : xs.length ? Math.min(...xs) : 0;
  const x1 = xmax != null ? xmax : xs.length ? Math.max(...xs) : 1;
  const y1 = ymax != null ? ymax : ys.length ? Math.max(...ys) : 1;
  const y0 = ymin != null ? ymin : ylog ? (ys.length ? Math.min(...ys) : 1e-3) : 0;
  const lx = (v: number) => (xlog ? Math.log10(Math.max(v, x0 || 1e-9)) : v);
  const ly = (v: number) => (ylog ? Math.log10(Math.max(v, y0 || 1e-9)) : v);
  const X = (v: number) => L + (pw * (lx(v) - lx(x0))) / (lx(x1) - lx(x0) || 1);
  const Y = (v: number) => TOP + ph - (ph * (ly(v) - ly(y0))) / (ly(y1) - ly(y0) || 1);
  const yt = niceTicks(y1, 4).filter((t) => t >= y0);
  const xt = xticks || niceTicks(x1, 5).filter((t) => t >= x0);
  const F = (v: number) => (yfmt ? yfmt(v) : fmt(v));
  const XF = (v: number) => (xfmt ? xfmt(v) : fmt(v));

  /** Split into runs of consecutive present points -- one subpath per run. */
  const runs = (pts: LinePt[]): LinePt[][] => {
    const out: LinePt[][] = [];
    let cur: LinePt[] = [];
    for (const p of pts) {
      if (p.y == null) {
        if (cur.length) out.push(cur);
        cur = [];
      } else cur.push(p);
    }
    if (cur.length) out.push(cur);
    return out;
  };

  return (
    <Svg w={W} h={H} label={ylabel}>
      {yt.map((t) => (
        <YGrid key={'y' + t} x0={L} x1={W - R} y={Y(t)} lbl={F(t)} />
      ))}
      {xt.map((t, i) => (
        <text key={'x' + i} className="tick" x={X(t)} y={TOP + ph + 15} textAnchor="middle">
          {XF(t)}
        </text>
      ))}
      {series.map((se) => {
        const rs = runs(se.pts);
        return (
          <g key={se.lbl}>
            {rs.map((run, ri) => {
              const d = run
                .map((p, i) => (i ? 'L' : 'M') + X(p.x).toFixed(1) + ' ' + Y(p.y as number).toFixed(1))
                .join(' ');
              return (
                <g key={ri}>
                  {se.area && run.length > 1 && (
                    <path
                      d={`${d} L ${X(run[run.length - 1].x).toFixed(1)} ${Y(y0).toFixed(1)} L ${X(
                        run[0].x,
                      ).toFixed(1)} ${Y(y0).toFixed(1)} Z`}
                      fill={se.c}
                      opacity={0.1}
                    />
                  )}
                  {run.length > 1 ? (
                    <path
                      d={d}
                      fill="none"
                      stroke={se.c}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeDasharray={se.dashed ? '4 3' : undefined}
                    />
                  ) : (
                    /* an isolated sample between two gaps: a dot, not a line */
                    <circle cx={X(run[0].x)} cy={Y(run[0].y as number)} r={2} fill={se.c} />
                  )}
                </g>
              );
            })}
            {se.pts.map((p, i) => {
              const every = Math.max(1, Math.round(se.pts.length / 26));
              if (i % every || p.y == null) return null;
              return (
                <circle
                  key={'h' + i}
                  cx={X(p.x).toFixed(1)}
                  cy={Y(p.y).toFixed(1)}
                  r={8}
                  fill="transparent"
                  data-tip={`${se.lbl} · ${xname || 'x'} ${XF(p.x)} → ${
                    yname || 'y'
                  } ${F(p.y)}`}
                />
              );
            })}
            {se.mark &&
              (() => {
                const last = [...se.pts].reverse().find((p) => p.y != null);
                if (!last) return null;
                return (
                  <g>
                    <circle
                      cx={X(last.x).toFixed(1)}
                      cy={Y(last.y as number).toFixed(1)}
                      r={3.5}
                      fill={se.c}
                      stroke="var(--surface)"
                      strokeWidth={2}
                    />
                    <text
                      className="vlbl"
                      x={X(last.x) - 6}
                      y={Y(last.y as number) - 9}
                      textAnchor="end"
                    >
                      {se.mark}
                    </text>
                  </g>
                );
              })()}
          </g>
        );
      })}
      <line className="base-line" x1={L} x2={W - R} y1={Y(y0)} y2={Y(y0)} />
      {ylabel && (
        <text className="axlbl" x={0} y={9}>
          {ylabel}
        </text>
      )}
      {xlabel && (
        <text className="axlbl" x={W - R} y={H - 4} textAnchor="end">
          {xlabel}
        </text>
      )}
    </Svg>
  );
}

/* ----------------------------------------------------------------- ribbon */

/** Proportional-width run-length ribbon.
 *
 * Each segment's width is its run length as a share of the sequence, so the
 * ribbon shows how much of the trajectory a run actually occupied -- a 120-deep
 * `pgrep` loop dominates the ribbon the way it dominated the session. */
export function Ribbon({
  rle,
  colour,
  label,
  W = 420,
  H = 13,
}: {
  rle: [string, number][];
  colour: (k: string) => string;
  label?: (k: string, n: number, i: number) => string;
  W?: number;
  H?: number;
}) {
  const tot = rle.reduce((a, [, n]) => a + (n || 0), 0);
  if (!(tot > 0)) return null;
  let x = 0;
  const out: ReactNode[] = [];
  rle.forEach(([k, n], i) => {
    const w = (W * (n || 0)) / tot;
    if (w <= 0) return;
    out.push(
      <rect
        key={i}
        x={x.toFixed(2)}
        y={0}
        width={Math.max(w - (w > 3 ? 0.6 : 0), 0.4).toFixed(2)}
        height={H}
        fill={colour(k)}
        data-tip={label ? label(k, n, i) : `${k} ×${n}`}
      />,
    );
    x += w;
  });
  return (
    <Svg w={W} h={H}>
      {out}
    </Svg>
  );
}
