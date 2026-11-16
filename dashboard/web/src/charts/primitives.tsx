/* SVG chart primitives, ported from the retired demo template
 * (git history: dashboard/archive/agent_dashboard.template.html).
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
 * CSS only ever shrinks a chart, so a time series sits in a <Fluid> box, which
 * measures its container and draws the chart at that width, at 1:1. In the
 * narrow layout the other charts do the same in a box narrower than their
 * natural width (`useFit`), so their labels keep their size on a phone.
 */
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
  RefObject,
} from 'react';
import { niceTicks } from './scale';
import { fint, fmt } from '../lib/format';
import { useNarrow } from '../lib/useNarrow';

export interface SvgProps {
  w: number;
  h: number;
  children: ReactNode;
  label?: string;
  /** the ref `useFit` measures the chart's box through */
  fit?: RefObject<SVGSVGElement | null>;
}

export function Svg({ w, h, children, label, fit }: SvgProps) {
  return (
    <svg
      ref={fit}
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

/** The width a chart has to fill, measured off its container, so that it is
 * drawn at 1:1 and a 10px label stays 10px. Scaled by CSS alone, a 520-wide
 * chart in a full-width panel stayed 520 wide beside half a panel of nothing,
 * and a 1060-wide one on a phone was shrunk to labels 3px tall. Until the
 * first measurement, and wherever none can be taken, the natural width
 * stands. Below `min` the chart is drawn at `min` and CSS shrinks it, as
 * before: a sliver of a box makes no readable plot at 1:1 either. The height
 * stays the chart's own, so a resize never moves the page under the reader. */
export function useFluidWidth(natural: number, min = 300) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState<number | null>(null);
  // before paint, so the first frame is already at the measured width
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const read = () => {
      const cw = Math.floor(el.clientWidth);
      if (cw > 0) setW(Math.max(min, cw));
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [min]);
  return { ref, W: w ?? natural };
}

/** A box as wide as its container, which hands that width to the chart in it. */
export function Fluid({
  natural,
  min,
  children,
}: {
  natural: number;
  min?: number;
  children: (W: number) => ReactNode;
}) {
  const { ref, W } = useFluidWidth(natural, min);
  return (
    <div ref={ref} className="fluid">
      {children(W)}
    </div>
  );
}

/** The narrowest a chart is drawn; in a box narrower still, it scales down. */
const FIT_MIN = 240;

/** The width to draw a chart at, given its natural width `W`, and the ref to
 * put on its `<svg>`.
 *
 * Scaled by its viewBox into a box much narrower than `W` -- a 1060-wide chart
 * on a phone -- a chart prints its 10 px labels at 3 px. So in the narrow
 * layout (`useNarrow`), in a box narrower than `W`, it is drawn at the box's
 * width instead, every label at its real size, down to `FIT_MIN`. In a box as
 * wide as `W` or wider, and anywhere in the wide layout, where a chart is
 * scaled down a little at most, it is drawn at `W` as it always was. The box is
 * the svg's parent, watched as it resizes; a chart that drew nothing has none
 * until it draws. */
export function useFit(W: number): [RefObject<SVGSVGElement | null>, number] {
  const narrow = useNarrow();
  const ref = useRef<SVGSVGElement | null>(null);
  const [room, setRoom] = useState<number | null>(null);
  const seen = useRef<{ box: Element; ro: ResizeObserver } | null>(null);
  useLayoutEffect(() => {
    const box = ref.current?.parentElement ?? null;
    if (box === (seen.current?.box ?? null) || typeof ResizeObserver === 'undefined') return;
    seen.current?.ro.disconnect();
    seen.current = null;
    if (!box) return;
    const read = () => {
      const cs = getComputedStyle(box);
      const n = Math.floor(box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight));
      // a box that is not laid out (display: none) says nothing about its width
      if (n > 0) setRoom(n);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(box);
    seen.current = { box, ro };
  });
  useLayoutEffect(
    () => () => {
      seen.current?.ro.disconnect();
      seen.current = null;
    },
    [],
  );
  return [ref, narrow && room != null && room < W ? Math.max(FIT_MIN, room) : W];
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

/* ------------------------------------------------------------------- zoom */

/* Scroll to zoom, on every chart whose scale can be magnified without changing
 * what a mark means. The gesture is a map's: the wheel (or a trackpad pinch)
 * zooms around the cursor, a drag pans once zoomed in, and a double-click or
 * `reset zoom` above the chart goes back to the whole. What zooms is decided
 * chart by chart:
 *   - a LINE chart zooms its x range, and y refits to what is in view, from 0;
 *   - a BAR chart zooms only its value scale. The baseline stays at 0, because
 *     a bar's length from 0 IS the measurement; a bar past the top is drawn
 *     cut, with its true value still printed beside it;
 *   - a 100%-stacked row does not zoom at all. Each mark there is a share of a
 *     whole, and a magnified part is no longer that;
 *   - a run-length ribbon zooms its SEQUENCE axis when asked to (`Ribbon`'s
 *     `zoom`): magnified, a run's width is its share of the calls in view, and
 *     the line above says which calls those are, so it is never read as a
 *     share of the whole.
 * A category axis does not zoom either: a subset of categories is a filter.
 *
 * The helpers below are exported for the charts outside this file that follow
 * the same gesture (the trajectory inspector's time strip).
 *
 * A page of charts must still scroll as a page, and a chart must never scroll
 * it. So a wheel GESTURE is latched, as a browser latches one to a nested
 * scroller: it belongs to whatever the cursor was over when it began -- a
 * zoomable chart, or the page -- until the wheel has been quiet for
 * LATCH_GAP_MS. A page scroll passing over a chart keeps scrolling the page,
 * trackpad momentum included; a gesture begun on a chart only ever zooms it,
 * even at its limits, and never leaks into the page. */

export const ZOOM_HINT = 'scroll to zoom · drag to pan · double-click to reset';
const LATCH_GAP_MS = 200;
/** Wheel input past this many CSS px in one frame is clamped: a fast flick
 * should zoom a lot, not jump straight to the end. */
const MAX_WHEEL_PX = 400;

const zoomables = new WeakSet<Element>();
let latch: Element | 'page' | null = null;
let lastWheel = -Infinity;
let watching = false;
function watchWheel() {
  if (watching || typeof document === 'undefined') return;
  watching = true;
  // Capture phase: decided before any chart's own listener runs.
  document.addEventListener(
    'wheel',
    (e) => {
      const now = performance.now();
      if (now - lastWheel > LATCH_GAP_MS) {
        const svg = e.target instanceof Element ? e.target.closest('svg') : null;
        latch = svg && zoomables.has(svg) ? svg : 'page';
      }
      lastWheel = now;
    },
    { capture: true, passive: true },
  );
}

/** One frame's worth of wheel input, in CSS px. */
export interface Wheel {
  dx: number;
  dy: number;
  pinch: boolean;
  shift: boolean;
  clientX: number;
  clientY: number;
}

/** Above 1 zooms out, below 1 zooms in: about 16% a mouse-wheel notch. A
 * trackpad pinch arrives as ctrl+wheel with small deltas, so it is scaled up. */
export function wheelFactor(w: Wheel): number {
  const dy = Math.max(-MAX_WHEEL_PX, Math.min(MAX_WHEEL_PX, w.dy));
  return Math.pow(2, dy * (w.pinch ? 0.01 : 0.0025));
}

/** Sideways travel in CSS px: a trackpad swipe, or shift+wheel. */
export function wheelPan(w: Wheel): number {
  return Math.abs(w.dx) > Math.abs(w.dy) ? w.dx : w.shift ? w.dy : 0;
}

/** Where a pointer is in a chart's own SVG units; `k` is SVG units per CSS px. */
export function svgAt(el: Element, e: { clientX: number; clientY: number }, W: number, H: number) {
  const svg = el instanceof SVGSVGElement ? el : (el as SVGGraphicsElement).ownerSVGElement;
  const r = svg?.getBoundingClientRect();
  if (!r || !r.width || !r.height) return null;
  return { x: ((e.clientX - r.left) * W) / r.width, y: ((e.clientY - r.top) * H) / r.height, k: W / r.width };
}

/** Wheel zoom for the chart around the returned ref: the whole SVG takes part,
 * axes included. A non-passive listener, because React's is passive and could
 * not stop the page scrolling; input is summed and applied once per animation
 * frame, so a trackpad's 120 events a second cost 60 renders, not 120. */
export function useWheel(handler: ((w: Wheel, svg: SVGSVGElement) => void) | null) {
  const [el, setEl] = useState<SVGGElement | null>(null);
  const fn = useRef(handler);
  useLayoutEffect(() => {
    fn.current = handler;
  });
  useEffect(() => {
    const svg = el?.ownerSVGElement;
    if (!svg) return;
    watchWheel();
    zoomables.add(svg);
    let acc: Wheel | null = null;
    let raf = 0;
    const flush = () => {
      raf = 0;
      const w = acc;
      acc = null;
      if (w && fn.current) fn.current(w, svg);
    };
    const on = (e: WheelEvent) => {
      if (latch !== svg || !fn.current) return;
      e.preventDefault();
      const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      acc ??= { dx: 0, dy: 0, pinch: false, shift: false, clientX: 0, clientY: 0 };
      acc.dx += e.deltaX * k;
      acc.dy += e.deltaY * k;
      acc.pinch ||= e.ctrlKey;
      acc.shift ||= e.shiftKey;
      acc.clientX = e.clientX;
      acc.clientY = e.clientY;
      if (!raf) raf = requestAnimationFrame(flush);
    };
    svg.addEventListener('wheel', on, { passive: false });
    return () => {
      svg.removeEventListener('wheel', on);
      zoomables.delete(svg);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [el]);
  return setEl;
}

/** A bar chart's wheel: scale its top between the smallest bar and the largest. */
function useCeilingWheel(
  zoom: boolean,
  vals: (number | null)[],
  full: number,
  top: number,
  setCeil: (c: number | null) => void,
) {
  const pos = vals.filter((v): v is number => v != null && v > 0);
  const floor = Math.min(full, pos.length ? Math.min(...pos) : full);
  return useWheel(
    zoom
      ? (w) => {
          if (wheelPan(w)) return;
          const next = Math.min(full, Math.max(floor, top * wheelFactor(w)));
          setCeil(next >= full ? null : next);
        }
      : null,
  );
}

/** The line above a zoomable chart. Always there, so the chart never moves
 * when a zoom begins: a hint while the pointer is on the chart, and once
 * zoomed, what is in view and the way back. */
export function ZoomNote({
  zoomed,
  text,
  hint,
  reset,
}: {
  zoomed: boolean;
  text: string;
  hint: string;
  reset: () => void;
}) {
  return (
    <div className={zoomed ? 'zoomnote on' : 'zoomnote'}>
      {zoomed ? (
        <>
          <span>zoomed to {text}</span>
          <button type="button" onClick={reset}>
            reset zoom
          </button>
        </>
      ) : (
        <span className="hint">{hint}</span>
      )}
    </div>
  );
}

/** The whole range under a zoomed chart, small, with the part in view boxed
 * -- the chart's minimap. Drag the box to pan, drag either edge to widen or
 * narrow it, click outside it to go there, double-click to see it all again.
 * Shown only while zoomed: unzoomed, the chart above already is the whole.
 *
 * It works in the chart's axis space (`lo`-`hi`, `a`-`b`; log space on a log
 * axis), on the same `W` and plot margins as the chart, so it lines up under
 * it. `draw` paints the overview -- the chart's own lines, or a strip's bars --
 * given the minimap's x scale and its height; `onSet` gets every new range,
 * already inside the data and no narrower than `minSpan`. */
export function ZoomMap({
  W,
  L = 0,
  R = 0,
  H = 30,
  lo,
  hi,
  a,
  b,
  minSpan,
  onSet,
  onReset,
  draw,
  label,
  className,
}: {
  W: number;
  L?: number;
  R?: number;
  H?: number;
  lo: number;
  hi: number;
  a: number;
  b: number;
  minSpan: number;
  onSet: (a: number, b: number) => void;
  onReset: () => void;
  draw: (X: (v: number) => number, H: number) => ReactNode;
  label?: string;
  /** sizing of the chart above it, when that is not the plain `svg` rule */
  className?: string;
}) {
  const pw = W - L - R;
  const full = Math.max(hi - lo, 1e-9);
  const X = (v: number) => L + ((v - lo) / full) * pw;
  const V = (x: number) => lo + ((Math.min(W - R, Math.max(L, x)) - L) / pw) * full;
  const clampTo = (v: number, p: number, q: number) => Math.min(q, Math.max(p, v));
  const grab = useRef<{ mode: 'move' | 'l' | 'r'; sv: number; a: number; b: number } | null>(null);
  const [mode, setMode] = useState<'move' | 'l' | 'r' | null>(null);
  const xa = X(a);
  const xb = X(b);
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      preserveAspectRatio="xMidYMin meet"
      className={`zmap${className ? ' ' + className : ''}${mode ? ' ' + mode : ''}`}
      role="img"
      aria-label={label ?? 'the whole range, with the part in view boxed'}
      data-tip="the whole range: drag the box to pan, an edge to resize, click elsewhere to go there, double-click to see it all"
      onDoubleClick={onReset}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        const p = svgAt(e.currentTarget, e, W, H);
        if (!p) return;
        const edge = 6 * p.k;
        let na = a;
        let nb = b;
        let m: 'move' | 'l' | 'r';
        if (Math.abs(p.x - xa) <= edge && p.x <= (xa + xb) / 2) m = 'l';
        else if (Math.abs(p.x - xb) <= edge) m = 'r';
        else if (p.x > xa && p.x < xb) m = 'move';
        else {
          // outside the box: the box goes there, and the drag carries on from it
          const s = b - a;
          na = clampTo(V(p.x) - s / 2, lo, hi - s);
          nb = na + s;
          onSet(na, nb);
          m = 'move';
        }
        e.currentTarget.setPointerCapture(e.pointerId);
        grab.current = { mode: m, sv: V(p.x), a: na, b: nb };
        setMode(m);
      }}
      onPointerMove={(e) => {
        const g = grab.current;
        const p = g && svgAt(e.currentTarget, e, W, H);
        if (!g || !p) return;
        const d = V(p.x) - g.sv;
        if (g.mode === 'move') {
          const s = g.b - g.a;
          const na = clampTo(g.a + d, lo, hi - s);
          onSet(na, na + s);
        } else if (g.mode === 'l') onSet(clampTo(g.a + d, lo, g.b - minSpan), g.b);
        else onSet(g.a, clampTo(g.b + d, g.a + minSpan, hi));
      }}
      onPointerUp={() => {
        grab.current = null;
        setMode(null);
      }}
      onPointerCancel={() => {
        grab.current = null;
        setMode(null);
      }}
    >
      <rect className="zmap-bg" x={L} y={0.5} width={pw} height={H - 1} />
      {draw(X, H)}
      <rect className="zmap-out" x={L} y={0.5} width={Math.max(0, xa - L)} height={H - 1} />
      <rect className="zmap-out" x={xb} y={0.5} width={Math.max(0, W - R - xb)} height={H - 1} />
      <rect className="zmap-win" x={xa} y={0.75} width={Math.max(2, xb - xa)} height={H - 1.5} />
      <rect className="zmap-h" x={xa - 2} y={H / 2 - 6} width={4} height={12} rx={1.5} />
      <rect className="zmap-h" x={xb - 2} y={H / 2 - 6} width={4} height={12} rx={1.5} />
      <rect className="zmap-edge" x={xa - 5} y={0} width={10} height={H} />
      <rect className="zmap-edge" x={xb - 5} y={0} width={10} height={H} />
    </svg>
  );
}

/** Two slanted strokes across a bar: it runs on past the zoomed ceiling. */
function Cut({ d }: { d: string }) {
  return <path className="zoom-cut" d={d} />;
}

/* ------------------------------------------------------------------ hbars */

export interface HBarItem {
  k: string;
  v: number | null;
  c?: string;
  lbl?: string;
  tip?: string;
  /** the bar and its value open something: the commands behind it */
  onPick?: () => void;
}

/** Advance width of `.vlbl` (IBM Plex Mono at 10.5px). */
const VLBL_CH = 6.3;
/** Advance width of `.tick` (IBM Plex Mono at 10px). */
const TICK_CH = 6;
/** The room a fitted HBars keeps for its bars, at most its natural room. */
const HBAR_MIN = 72;

export function HBars({
  items,
  W: natW = 560,
  labelW: natLabelW = 132,
  valW: natValW = 54,
  rowH = 22,
  color = 'var(--s1)',
  max,
  unit,
  zoom = true,
}: {
  items: HBarItem[];
  W?: number;
  labelW?: number;
  valW?: number;
  rowH?: number;
  color?: string;
  max?: number;
  unit?: string;
  /** scroll over the chart to magnify the short bars; the baseline stays 0 */
  zoom?: boolean;
}) {
  const [ceil, setCeil] = useState<number | null>(null);
  const [fitRef, fitW] = useFit(natW);
  const gap = 5;
  const h = items.length * rowH + 6;
  const full = max || Math.max(...items.map((d) => d.v ?? 0), 1);
  const top = ceil ?? full;
  const wheel = useCeilingWheel(zoom, items.map((d) => d.v), full, top, setCeil);
  if (!items.length) return null;
  const u = unit ? ' ' + unit : '';
  // Drawn narrower than asked (`useFit`), the label and value columns give up
  // the room they had beyond their own words before the bars give up any.
  const words = (ss: string[]) => Math.ceil(Math.max(...ss.map((s) => s.length)) * VLBL_CH);
  const fitted = fitW < natW;
  const labelW = fitted ? Math.min(natLabelW, words(items.map((d) => d.k)) + 12) : natLabelW;
  const valW = fitted
    ? Math.min(natValW, words(items.map((d) => (d.lbl != null ? d.lbl : fmt(d.v)))) + 13)
    : natValW;
  // Where the words still leave the bars too little room to differ -- long
  // tool names on a small phone -- the chart is drawn as wide as the bars need,
  // up to `natW`, and scaled into its box a little: smaller words, rather than
  // every bar the same 2 px.
  const W = fitted
    ? Math.min(natW, Math.max(fitW, labelW + valW + Math.max(0, Math.min(natW - natLabelW - natValW, HBAR_MIN))))
    : natW;
  const bw = W - labelW - valW;
  return (
    <>
      {zoom && (
        <ZoomNote
          zoomed={ceil != null}
          text={`0 – ${fmt(top)}${u}`}
          hint="scroll to zoom the scale"
          reset={() => setCeil(null)}
        />
      )}
      <Svg w={W} h={h} fit={fitRef}>
        <g
          ref={wheel}
          className={zoom ? 'zoomable' : undefined}
          onDoubleClick={() => setCeil(null)}
        >
          {zoom && (
            <rect x={0} y={0} width={W} height={h} fill="transparent">
              <title>{ZOOM_HINT}</title>
            </rect>
          )}
          {items.map((d, i) => {
            const y = i * rowH + 3;
            const bh = rowH - gap;
            // `null` is absent: draw no bar at all, and label it with a dash.
            const absent = d.v == null;
            const v = d.v as number;
            const len = absent ? 0 : Math.max(v > 0 ? 2 : 0, Math.min(bw, (bw * v) / top));
            const cut = !absent && v > top;
            const e = labelW + len;
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
                    className={d.onPick ? 'hpick' : undefined}
                    onClick={d.onPick}
                    data-tip={(d.tip || `${d.k}: ${fmt(d.v)}${u}`) + (d.onPick ? '\nclick: every command behind it' : '')}
                  />
                )}
                {cut && (
                  <Cut
                    d={`M${e - 13} ${y + bh + 1} L${e - 8} ${y - 1} M${e - 8} ${y + bh + 1} L${e - 3} ${y - 1}`}
                  />
                )}
                <text className={`vlbl${d.onPick ? ' hpick' : ''}`} x={e + 7} y={y + bh / 2 + 3.5} onClick={d.onPick}>
                  {d.lbl != null ? d.lbl : fmt(d.v)}
                </text>
              </g>
            );
          })}
        </g>
      </Svg>
    </>
  );
}

/* -------------------------------------------------------------- groupBars */

export interface BarGroup {
  k: string;
  k2?: string;
  v: Record<string, number | null>;
  /** a hover text per series key, in place of the default `group · series: value` */
  tips?: Record<string, string>;
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
  W: natW = 560,
  H = 200,
  maxBar = 34,
  ylabel,
  fmtVal,
  tickFmt,
  labelTop,
  max,
  zoom = true,
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
  /** scroll over the plot to magnify the short bars; the baseline stays 0 */
  zoom?: boolean;
}) {
  const [ceil, setCeil] = useState<number | null>(null);
  const [fitRef, W] = useFit(natW);
  const L = 44;
  const R = 8;
  const TOP = ylabel ? 20 : 10;
  const B = groups.some((g) => g.k2) ? 44 : 34;
  const pw = W - L - R;
  const ph = H - TOP - B;
  const dataMax =
    max || Math.max(...groups.flatMap((g) => series.map((s) => g.v[s.k] ?? 0)), 1);
  const ticks = niceTicks(ceil ?? dataMax, 4);
  const topV = ticks[ticks.length - 1];
  const y = (v: number) => TOP + ph - (ph * Math.min(v, topV)) / topV;
  const wheel = useCeilingWheel(
    zoom,
    groups.flatMap((g) => series.map((se) => g.v[se.k] ?? null)),
    dataMax,
    ceil ?? dataMax,
    setCeil,
  );
  if (!groups.length || !series.length) return null;
  const gw = pw / groups.length;
  const bw = Math.min(maxBar, (gw - 14) / series.length);
  const F = (v: number) => (fmtVal ? fmtVal(v) : fmt(v));
  const CH = 6.3; // advance width of `.vlbl` (IBM Plex Mono at 10.5px)
  const PAD = 5;  // a label that fills its slot edge to edge still collides
  return (
    <>
      {zoom && (
        <ZoomNote
          zoomed={ceil != null}
          text={`0 – ${F(topV)}`}
          hint="scroll to zoom the scale"
          reset={() => setCeil(null)}
        />
      )}
      <Svg w={W} h={H} label={ylabel} fit={fitRef}>
        {ticks.map((t) => (
          <YGrid key={t} x0={L} x1={W - R} y={y(t)} lbl={tickFmt ? tickFmt(t) : fmt(t)} />
        ))}
        <g
          ref={wheel}
          className={zoom ? 'zoomable' : undefined}
          onDoubleClick={() => setCeil(null)}
        >
          {zoom && (
            <rect x={L} y={TOP} width={pw} height={ph} fill="transparent">
              <title>{ZOOM_HINT}</title>
            </rect>
          )}
          {groups.map((g, gi) => {
            // Only the bars that exist, centred under the group's label: a group
            // holding one series (a class's own bar) sits in the middle, not in
            // the slot it would have had beside the others.
            const present = series.filter((se) => g.v[se.k] != null);
            const drawn = present.length;
            const slots = Math.max(1, drawn);
            const x0 = L + gi * gw + (gw - bw * slots - 2 * (slots - 1)) / 2;
            const slot = (se: BarSeries) => Math.max(0, present.indexOf(se));
            return (
              <g key={g.k + '-' + gi}>
                {series.map((se) => {
                  const raw = g.v[se.k];
                  if (raw == null) return null; // absent, not zero: no bar drawn
                  const x = x0 + slot(se) * (bw + 2);
                  const h = Math.max(raw > 0 ? 1.5 : 0, y(0) - y(raw));
                  return (
                    <g key={se.k}>
                      <rect
                        x={x}
                        y={y(raw)}
                        width={bw}
                        height={h}
                        rx={2}
                        fill={se.c}
                        data-tip={g.tips?.[se.k] ?? `${g.k} · ${se.lbl}: ${F(raw)}`}
                      />
                      {raw > topV && (
                        <Cut
                          d={`M${x - 1} ${TOP + 13} L${x + bw + 1} ${TOP + 8} M${x - 1} ${TOP + 18} L${
                            x + bw + 1
                          } ${TOP + 13}`}
                        />
                      )}
                    </g>
                  );
                })}
                {labelTop &&
                  series.map((se) => {
                    const raw = g.v[se.k];
                    if (raw == null) return null; // absent has no bar to label
                    const si = slot(se);
                    // A measured 0 IS labelled: a bar one pixel tall and a bar that
                    // is genuinely absent look identical at this size, and the
                    // number is the only thing that tells them apart. Slots too
                    // narrow for the text at full size step down rather than drop
                    // the label -- the short bars are exactly the ones that are
                    // unreadable without it.
                    const txt = F(raw);
                    // Room is bounded by the next LABELLED bar, not by the bar
                    // itself: a group holding a single series can spread across the
                    // whole group and stay full size.
                    const room = drawn > 1 ? bw + 2 : gw - 4;
                    const narrow = txt.length * CH + PAD > room;
                    // Short bars of similar height put their labels at the same y,
                    // where a stepped-down font still reads as one run-on number
                    // ("0.4%0.6%1.1%"). Lifting the middle of each triple splits
                    // them without moving any label off its own bar.
                    const dy = narrow && si % 2 === 1 ? -9 : 0;
                    return (
                      <text
                        key={'l' + se.k}
                        className={narrow ? 'vlbl sm' : 'vlbl'}
                        x={x0 + si * (bw + 2) + bw / 2}
                        y={y(raw) - 4 + dy}
                        textAnchor="middle"
                      >
                        {txt}
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
        </g>
        <line className="base-line" x1={L} x2={W - R} y1={y(0)} y2={y(0)} />
        {ylabel && (
          <text className="axlbl" x={0} y={9}>
            {ylabel}
          </text>
        )}
      </Svg>
    </>
  );
}

/* --------------------------------------------------------------- stackRow */

export interface StackSeg {
  k: string;
  v: number;
  c: string;
}

/** One 100%-stacked row, 2px surface gaps, selective in-bar labels.
 *
 * Not zoomable, on purpose: each segment is a share of the row, and a
 * magnified segment would no longer read as a share of anything. */
export function StackRow({
  segs,
  W: natW = 560,
  H = 30,
}: {
  segs: StackSeg[];
  W?: number;
  H?: number;
}) {
  const [fitRef, W] = useFit(natW);
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
    <Svg w={W} h={H} fit={fitRef}>
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
  /** a point opens something: the commands behind that bin */
  onPick?: (x: number) => void;
  lbl: string;
  c: string;
  pts: LinePt[];
  area?: boolean;
  mark?: string | null;
  dashed?: boolean;
}

/** The x scale, axis value to SVG units: linear, or log10 on a log axis. The
 *  parts that do not depend on `v` are worked out once, the same arithmetic. */
function xScale(L: number, pw: number, x0: number, x1: number, xlog?: boolean) {
  const lx = (v: number) => (xlog ? Math.log10(Math.max(v, x0 || 1e-9)) : v);
  const a = lx(x0);
  const den = lx(x1) - a || 1;
  return (v: number) => L + (pw * (lx(v) - a)) / den;
}

/** The y scale, likewise, from the plot's top down. */
function yScale(TOP: number, ph: number, y0: number, y1: number, ylog?: boolean) {
  const ly = (v: number) => (ylog ? Math.log10(Math.max(v, y0 || 1e-9)) : v);
  const b = ly(y0);
  const den = ly(y1) - b || 1;
  return (v: number) => TOP + ph - (ph * (ly(v) - b)) / den;
}

/** Multi-line chart on ONE value axis.
 *
 * A `null` y is a GAP. The path is emitted as separate subpaths around it, so a
 * minute with no records reads as a hole in the line -- never as a measured 0,
 * and never as a straight interpolation across the hole.
 *
 * Zoom is on the x range only (scroll to zoom, drag to pan: see `useWheel`).
 * It is held here, or shared through `view`/`onView` when several charts must
 * stay on one axis. The y scale refits to the points in view, so a quiet hour
 * is not flattened by a spike outside it; `minSpan` stops a zoom finer than
 * the data's own bins. */
export function Lines({
  series,
  W: natW = 560,
  H = 210,
  L = 48,
  ymin,
  ymax,
  xmin,
  xmax,
  xticks,
  xfmt,
  xtip,
  yfmt,
  ylabel,
  xlabel,
  xname,
  yname,
  ylog,
  xlog,
  zoom = true,
  view,
  onView,
  minSpan = 0,
}: {
  series: LineSeries[];
  W?: number;
  H?: number;
  L?: number;
  ymin?: number;
  ymax?: number;
  xmin?: number;
  xmax?: number;
  /** fixed ticks, or ticks for whatever x range is in view */
  xticks?: number[] | ((x0: number, x1: number) => number[]);
  xfmt?: (v: number) => string;
  /** the x in a hover tip, when a tick label alone would be ambiguous there */
  xtip?: (v: number) => string;
  yfmt?: (v: number) => string;
  ylabel?: string;
  xlabel?: string;
  xname?: string;
  yname?: string;
  ylog?: boolean;
  xlog?: boolean;
  zoom?: boolean;
  /** a zoom shared with other charts: the x range in view (null = all) */
  view?: [number, number] | null;
  onView?: (v: [number, number] | null) => void;
  /** the narrowest x span a zoom may reach */
  minSpan?: number;
}) {
  const clip = 'lc' + useId().replace(/[^\w-]/g, '');
  const [own, setOwn] = useState<[number, number] | null>(null);
  const [fitRef, W] = useFit(natW);
  const R = 12;
  const TOP = ylabel ? 22 : 12;
  const B = 34;
  const pw = W - L - R;
  const ph = H - TOP - B;
  // The x extent, by a loop: spread into Math.min, a week of minutes is 30,000
  // arguments on every render (and past ~100k, a stack overflow). Math.min
  // pairwise is Math.min of them all, NaN and -0 included.
  let nx = 0;
  let xlo = Infinity;
  let xhi = -Infinity;
  if (xmin == null || xmax == null) {
    for (const s of series) {
      for (const p of s.pts) {
        xlo = Math.min(xlo, p.x);
        xhi = Math.max(xhi, p.x);
        nx++;
      }
    }
  }
  const fx0 = xmin != null ? xmin : nx ? xlo : 0;
  const fx1 = xmax != null ? xmax : nx ? xhi : 1;
  const want = view !== undefined ? view : own;
  const setView = onView ?? setOwn;
  // A shared view can outlive the data it was taken on (a rebuilt window
  // slides past it); one that no longer overlaps is ignored, not drawn empty.
  const zoomed = zoom && want != null && want[1] > fx0 && want[0] < fx1;
  const x0 = zoomed ? Math.max(fx0, want![0]) : fx0;
  const x1 = zoomed ? Math.min(fx1, want![1]) : fx1;

  // Zoom and pan work in axis space -- log space on a log axis -- so the
  // point under the cursor stays under it either way.
  const T = (v: number) => (xlog ? Math.log10(Math.max(v, 1e-9)) : v);
  const Ti = (t: number) => (xlog ? 10 ** t : t);
  const full = T(fx1) - T(fx0);
  /** A range slid back inside the data rather than past its edge; null is all of it. */
  const fit = (a: number, b: number): [number, number] | null => {
    if (b - a >= full) return null;
    const lo = Math.min(Math.max(a, T(fx0)), T(fx1) - (b - a));
    return [Ti(lo), Ti(lo + (b - a))];
  };
  const wheel = useWheel(
    zoom
      ? (w, svg) => {
          const p = svgAt(svg, w, W, H);
          if (!p) return;
          const a = T(x0);
          const span = T(x1) - a;
          const slide = wheelPan(w);
          if (slide) {
            if (!zoomed) return;
            const d = ((slide * p.k) / pw) * span;
            setView(fit(a + d, a + span + d));
            return;
          }
          const f = wheelFactor(w);
          if (!zoomed && f >= 1) return;
          const ns = Math.min(full, Math.max(xlog || !minSpan ? full / 1e4 : minSpan, span * f));
          // the x under the cursor stays under it; off the plot, the nearer edge does
          const px = Math.min(W - R, Math.max(L, p.x));
          const at = a + ((px - L) / pw) * span;
          const lo = at - ((at - a) * ns) / span;
          setView(fit(lo, lo + ns));
        }
      : null,
  );
  // A drag pans, once there is somewhere to pan to.
  const grab = useRef<{ sx: number; a: number; span: number } | null>(null);
  const [panning, setPanning] = useState(false);
  const panHandlers = {
    onPointerDown: (e: ReactPointerEvent<SVGGElement>) => {
      if (!zoomed || e.pointerType === 'touch' || e.button !== 0) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      e.currentTarget.setPointerCapture(e.pointerId);
      grab.current = { sx: p.x, a: T(x0), span: T(x1) - T(x0) };
      setPanning(true);
    },
    onPointerMove: (e: ReactPointerEvent<SVGGElement>) => {
      const g = grab.current;
      const p = g && svgAt(e.currentTarget, e, W, H);
      if (!g || !p) return;
      const d = -((p.x - g.sx) / pw) * g.span;
      setView(fit(g.a + d, g.a + g.span + d));
    },
    onPointerUp: () => {
      grab.current = null;
      setPanning(false);
    },
    onPointerCancel: () => {
      grab.current = null;
      setPanning(false);
    },
  };
  const inView = (p: LinePt) => p.x >= x0 && p.x <= x1;
  // The y range of what is in view, by a loop for the same reason as x's.
  let ny = 0;
  let ylo = Infinity;
  let yhi = -Infinity;
  for (const s of series) {
    for (const p of s.pts) {
      if (p.y == null || !inView(p)) continue;
      ylo = Math.min(ylo, p.y);
      yhi = Math.max(yhi, p.y);
      ny++;
    }
  }
  const top = ny ? yhi : 1;
  // Topped at the last round tick: at the bare data max, the tick just above
  // it would be drawn over the axis label.
  const topTicks = ymax != null || ylog ? null : niceTicks(top, 4);
  const y1 = ymax != null ? ymax : ylog ? top : topTicks![topTicks!.length - 1];
  const y0 = ymin != null ? ymin : ylog ? (ny ? ylo : 1e-3) : 0;
  const X = xScale(L, pw, x0, x1, xlog);
  const Y = yScale(TOP, ph, y0, y1, ylog);
  const F = (v: number) => (yfmt ? yfmt(v) : fmt(v));
  const XF = (v: number) => (xfmt ? xfmt(v) : fmt(v));
  const XT = (v: number) => (xtip ? xtip(v) : XF(v));

  // Every series' lines, area, hover targets and end mark. A week of minutes is
  // 10,080 points a series: built once per change of what they depend on, each
  // path's `d` in one pass, and only the hover circles that are drawn visited.
  const marks = useMemo(() => {
    const X = xScale(L, pw, x0, x1, xlog);
    const Y = yScale(TOP, ph, y0, y1, ylog);
    const F = (v: number) => (yfmt ? yfmt(v) : fmt(v));
    const XF = (v: number) => (xfmt ? xfmt(v) : fmt(v));
    const XT = (v: number) => (xtip ? xtip(v) : XF(v));
    const inView = (p: LinePt) => p.x >= x0 && p.x <= x1;
    /** In view, plus one neighbour each side, so a line runs on to the edge. */
    const drawn = (pts: LinePt[]): LinePt[] => {
      if (!zoomed) return pts;
      let a = 0;
      while (a < pts.length && pts[a].x < x0) a++;
      let b = pts.length - 1;
      while (b >= 0 && pts[b].x > x1) b--;
      return pts.slice(Math.max(0, a - 1), Math.min(pts.length, b + 2));
    };
    return series.map((se) => {
      const pts = drawn(se.pts);
      const seen = pts.filter(inView);
      // One subpath per run of consecutive present points: a null is a gap.
      const runEls: ReactNode[] = [];
      for (let i = 0, ri = 0; i < pts.length; ) {
        if (pts[i].y == null) {
          i++;
          continue;
        }
        let d = '';
        let j = i;
        for (; j < pts.length && pts[j].y != null; j++) {
          d += (j > i ? ' L' : 'M') + X(pts[j].x).toFixed(1) + ' ' + Y(pts[j].y as number).toFixed(1);
        }
        const first = pts[i];
        const last = pts[j - 1];
        const n = j - i;
        runEls.push(
          <g key={ri++}>
            {se.area && n > 1 && (
              <path
                d={`${d} L ${X(last.x).toFixed(1)} ${Y(y0).toFixed(1)} L ${X(first.x).toFixed(1)} ${Y(y0).toFixed(1)} Z`}
                fill={se.c}
                opacity={0.1}
              />
            )}
            {n > 1 ? (
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
              <circle cx={X(first.x)} cy={Y(first.y as number)} r={2} fill={se.c} />
            )}
          </g>,
        );
        i = j;
      }
      // a hover target every `every` points in view, about 26 across
      const every = Math.max(1, Math.round(seen.length / 26));
      const hover: ReactNode[] = [];
      for (let i = 0; i < seen.length; i += every) {
        const p = seen[i];
        if (p.y == null) continue;
        const y = p.y;
        hover.push(
          <circle
            key={'h' + i}
            cx={X(p.x).toFixed(1)}
            cy={Y(y).toFixed(1)}
            r={8}
            fill="transparent"
            className={se.onPick ? 'hpick' : undefined}
            onClick={se.onPick ? () => se.onPick!(p.x) : undefined}
            data-tip={`${se.lbl} · ${xname || 'x'} ${XT(p.x)} → ${yname || 'y'} ${F(y)}${
              se.onPick ? '\nclick: every command behind it' : ''}`}
          />,
        );
      }
      return (
        <g key={se.lbl}>
          <g clipPath={zoomed ? `url(#${clip})` : undefined}>{runEls}</g>
          {hover}
          {se.mark &&
            (() => {
              let last: LinePt | undefined;
              for (let i = seen.length - 1; i >= 0; i--) {
                if (seen[i].y != null) {
                  last = seen[i];
                  break;
                }
              }
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
    });
  }, [series, zoomed, clip, L, pw, ph, TOP, x0, x1, y0, y1, xlog, ylog, xname, yname, xfmt, xtip, yfmt]);

  if (!series.length) return null;

  const yt = niceTicks(y1, 4).filter((t) => t >= y0);
  const xt = (
    typeof xticks === 'function' ? xticks(x0, x1) : xticks || niceTicks(x1, 5).filter((t) => t >= x0)
  ).filter((t) => t >= x0 && t <= x1);
  // Ticks whose labels would run into each other -- a chart drawn narrow, on a
  // phone -- are thinned from the left, so every label printed is whole.
  const xtShown: number[] = [];
  for (let i = 0, lastX = -Infinity, lastHalf = 0; i < xt.length; i++) {
    const x = X(xt[i]);
    const half = (XF(xt[i]).length * TICK_CH) / 2;
    if (x - lastX < lastHalf + half + 8) continue;
    xtShown.push(xt[i]);
    lastX = x;
    lastHalf = half;
  }

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
    <>
      {zoom && (
        <ZoomNote
          zoomed={zoomed}
          text={`${XT(x0)} – ${XT(x1)} · drag to pan`}
          hint="scroll to zoom"
          reset={() => setView(null)}
        />
      )}
      <Svg w={W} h={H} label={ylabel} fit={fitRef}>
        {zoomed && (
          <defs>
            <clipPath id={clip}>
              <rect x={L} y={TOP - 4} width={pw} height={ph + 8} />
            </clipPath>
          </defs>
        )}
        {yt.map((t) => (
          <YGrid key={'y' + t} x0={L} x1={W - R} y={Y(t)} lbl={F(t)} />
        ))}
        {xtShown.map((t, i) => {
          // a label centred on a tick at the plot's edge would be cut in half
          const x = X(t);
          const anchor = x > W - R - 18 ? 'end' : x < L + 18 ? 'start' : 'middle';
          return (
            <text key={'x' + i} className="tick" x={x} y={TOP + ph + 15} textAnchor={anchor}>
              {XF(t)}
            </text>
          );
        })}
        <g
          ref={wheel}
          className={zoom ? 'zoomable' : undefined}
          style={zoomed ? { cursor: panning ? 'grabbing' : 'grab' } : undefined}
          onDoubleClick={() => setView(null)}
          {...(zoom ? panHandlers : {})}
        >
          {zoom && (
            <rect x={L} y={TOP} width={pw} height={ph} fill="transparent">
              <title>{ZOOM_HINT}</title>
            </rect>
          )}
          {marks}
        </g>
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
      {zoomed && (
        <ZoomMap
          W={W}
          L={L}
          R={R}
          lo={T(fx0)}
          hi={T(fx1)}
          a={T(x0)}
          b={T(x1)}
          minSpan={xlog || !minSpan ? full / 1e4 : minSpan}
          onSet={(a, b) => setView(fit(a, b))}
          onReset={() => setView(null)}
          label={`the whole x range, ${XT(fx0)} to ${XT(fx1)}, with ${XT(x0)} to ${XT(x1)} in view`}
          draw={(MX, MH) => {
            // every line over the whole range, on a y scale of its own
            let n = 0;
            let lo = Infinity;
            let hi = -Infinity;
            for (const se of series) {
              for (const p of se.pts) {
                if (p.y == null) continue;
                lo = Math.min(lo, p.y);
                hi = Math.max(hi, p.y);
                n++;
              }
            }
            const mtop = n ? hi : 1;
            const mbot = ylog ? (n ? lo : 1e-3) : (ymin ?? 0);
            const my = (v: number) => (ylog ? Math.log10(Math.max(v, mbot || 1e-9)) : v);
            const MY = (v: number) => MH - 3 - ((MH - 6) * (my(v) - my(mbot))) / (my(mtop) - my(mbot) || 1);
            return series.map((se) =>
              runs(se.pts).map((run, ri) =>
                run.length > 1 ? (
                  <path
                    key={`${se.lbl}-${ri}`}
                    d={run.map((p, i) => (i ? 'L' : 'M') + MX(T(p.x)).toFixed(1) + ' ' + MY(p.y as number).toFixed(1)).join(' ')}
                    fill="none"
                    stroke={se.c}
                    strokeWidth={1}
                    strokeDasharray={se.dashed ? '3 2' : undefined}
                  />
                ) : (
                  <circle key={`${se.lbl}-${ri}`} cx={MX(T(run[0].x))} cy={MY(run[0].y as number)} r={1} fill={se.c} />
                ),
              ),
            );
          }}
        />
      )}
    </>
  );
}

/* ----------------------------------------------------------------- ribbon */

/** Proportional-width run-length ribbon.
 *
 * Each segment's width is its run length as a share of the sequence, so the
 * ribbon shows how much of the trajectory a run actually occupied -- a 120-deep
 * `pgrep` loop dominates the ribbon the way it dominated the session. That is
 * the unzoomed ribbon, and why it zooms only when asked (`zoom`): a magnified
 * run is no longer a share of the whole. Zoomed, the axis is still the
 * sequence -- calls a–b, never a clock range -- widths are shares of the calls
 * in view, and the line above the ribbon says which calls those are, the same
 * reserved line every zoomable chart here keeps, so nothing jumps.
 *
 * `onPick(i)` makes the runs pickable: a click that is not a drag, or Enter on
 * the focused ribbon after the arrow keys have moved through its runs
 * (+ / − zoom around that run, 0 resets). */
export function Ribbon({
  rle,
  colour,
  label,
  W: natW = 420,
  H = 13,
  zoom = false,
  onPick,
  note,
  name = 'runs',
}: {
  rle: [string, number][];
  colour: (k: string) => string;
  label?: (k: string, n: number, i: number) => string;
  W?: number;
  H?: number;
  /** scroll to magnify a stretch of the sequence, drag to pan it */
  zoom?: boolean;
  onPick?: (i: number) => void;
  /** appended to the zoom line: what the sequence is a part of */
  note?: string;
  /** what the runs are, for the keyboard label */
  name?: string;
}) {
  const [view, setView] = useState<[number, number] | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [panning, setPanning] = useState(false);
  const [fitRef, W] = useFit(natW);
  const grab = useRef<{ cx: number; sx: number; a: number; moved: boolean } | null>(null);
  const cum: number[] = [];
  let tot = 0;
  for (const [, n] of rle) {
    cum.push(tot);
    tot += n || 0;
  }
  const zoomed = zoom && view != null;
  const a = zoomed ? view[0] : 0;
  const b = zoomed ? view[1] : tot;
  const span = Math.max(b - a, 1e-9);
  // Never narrower than a few calls: past that a "run" is one rectangle wide.
  const minSpan = Math.min(tot, 4);
  const fit = (lo: number, hi: number): [number, number] | null => {
    const s = hi - lo;
    if (s >= tot) return null;
    const l = Math.min(Math.max(lo, 0), tot - s);
    return [l, l + s];
  };
  const zoomAt = (at: number, f: number) => {
    if (!zoomed && f >= 1) return;
    const ns = Math.min(tot, Math.max(minSpan, span * f));
    const lo = at - ((at - a) * ns) / span;
    setView(fit(lo, lo + ns));
  };
  const wheel = useWheel(
    zoom && tot > 1
      ? (w, svg) => {
          const p = svgAt(svg, w, W, H);
          if (!p) return;
          const slide = wheelPan(w);
          if (slide) {
            if (zoomed) setView(fit(a + ((slide * p.k) / W) * span, b + ((slide * p.k) / W) * span));
            return;
          }
          zoomAt(a + (Math.min(W, Math.max(0, p.x)) / W) * span, wheelFactor(w));
        }
      : null,
  );
  if (!(tot > 0)) return null;
  /** The run holding call position `x`. */
  const runAt = (x: number) => {
    let lo = 0;
    let hi = rle.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (cum[mid] <= x) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  };
  const firstInView = () => runAt(a + 1e-9);
  const keep = (i: number) => {
    // a run moved to with the keys is brought into view, the span kept
    const c0 = cum[i];
    const c1 = c0 + (rle[i][1] || 0);
    if (zoomed && (c1 <= a || c0 >= b)) setView(fit(c0 - span / 2, c0 + span / 2));
  };
  const onKey = (e: ReactKeyboardEvent<SVGGElement>) => {
    const cur = focus ?? firstInView();
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const i = Math.min(rle.length - 1, Math.max(0, cur + (e.key === 'ArrowRight' ? 1 : -1)));
      setFocus(i);
      keep(i);
    } else if ((e.key === 'Enter' || e.key === ' ') && onPick) {
      onPick(cur);
    } else if (zoom && (e.key === '+' || e.key === '=' || e.key === '-')) {
      zoomAt(cum[cur] + (rle[cur][1] || 0) / 2, e.key === '-' ? 1.6 : 1 / 1.6);
    } else if (zoom && (e.key === '0' || e.key === 'Escape')) {
      setView(null);
    } else return;
    e.preventDefault();
    e.stopPropagation();
  };
  const handlers = {
    onPointerDown: (e: ReactPointerEvent<SVGGElement>) => {
      if (e.button !== 0) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      grab.current = { cx: e.clientX, sx: p.x, a, moved: false };
      if (zoomed && e.pointerType !== 'touch') e.currentTarget.setPointerCapture(e.pointerId);
    },
    onPointerMove: (e: ReactPointerEvent<SVGGElement>) => {
      const g = grab.current;
      if (!g) return;
      if (Math.abs(e.clientX - g.cx) > 3) g.moved = true;
      if (!zoomed || !g.moved) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      setPanning(true);
      const d = -((p.x - g.sx) / W) * span;
      setView(fit(g.a + d, g.a + span + d));
    },
    onPointerUp: (e: ReactPointerEvent<SVGGElement>) => {
      const g = grab.current;
      grab.current = null;
      setPanning(false);
      if (!g || g.moved || !onPick) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      const i = runAt(a + (Math.min(W, Math.max(0, p.x)) / W) * span);
      setFocus(i);
      onPick(i);
    },
    onPointerCancel: () => {
      grab.current = null;
      setPanning(false);
    },
  };
  const out: ReactNode[] = [];
  rle.forEach(([k, n], i) => {
    const c0 = Math.max(cum[i], a);
    const c1 = Math.min(cum[i] + (n || 0), b);
    if (c1 <= c0) return;
    const x = ((c0 - a) / span) * W;
    const w = ((c1 - c0) / span) * W;
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
  });
  let ring: ReactNode = null;
  if (focus != null && focus < rle.length) {
    const c0 = Math.max(cum[focus], a);
    const c1 = Math.min(cum[focus] + (rle[focus][1] || 0), b);
    if (c1 > c0) {
      const x = ((c0 - a) / span) * W;
      ring = (
        <rect
          className="ribbon-focus"
          x={x.toFixed(2)}
          y={0.75}
          width={Math.max(((c1 - c0) / span) * W, 2).toFixed(2)}
          height={H - 1.5}
        />
      );
    }
  }
  const lo = Math.floor(a) + 1;
  const hi = Math.max(lo, Math.ceil(b - 1e-9));
  const interactive = zoom || !!onPick;
  return (
    <>
      {zoom && (
        <ZoomNote
          zoomed={zoomed}
          text={`calls ${fint(lo)}–${fint(hi)} of ${fint(tot)}${note ? ` ${note}` : ''}`}
          hint={onPick ? 'scroll to zoom · click a run to open it' : 'scroll to zoom'}
          reset={() => setView(null)}
        />
      )}
      <Svg w={W} h={H} fit={fitRef}>
        <g
          ref={zoom ? wheel : undefined}
          className={[zoom ? 'zoomable' : '', onPick ? 'pickable' : ''].join(' ').trim() || undefined}
          style={zoomed ? { cursor: panning ? 'grabbing' : 'grab' } : onPick ? { cursor: 'pointer' } : undefined}
          tabIndex={interactive ? 0 : undefined}
          role={interactive ? 'group' : undefined}
          aria-label={
            interactive
              ? `${fint(rle.length)} ${name}: arrow keys move between them${onPick ? ', Enter opens one' : ''}${
                  zoom ? ', + and − zoom, 0 resets' : ''
                }`
              : undefined
          }
          onKeyDown={interactive ? onKey : undefined}
          onBlur={() => setFocus(null)}
          onDoubleClick={zoom ? () => setView(null) : undefined}
          // A ribbon sits inside clickable rows: its own click is its own.
          onClick={interactive ? (e) => e.stopPropagation() : undefined}
          {...(interactive ? handlers : {})}
        >
          {out}
          {ring}
        </g>
      </Svg>
    </>
  );
}

