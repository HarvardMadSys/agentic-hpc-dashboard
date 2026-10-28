/* The inspector's navigator: find a moment of the session, then go there.
 *
 * Two tracks above the process tree, one gesture vocabulary with every chart
 * on this page (charts/primitives.tsx: wheel or pinch zooms around the cursor,
 * a drag pans once zoomed, a double-click or `reset zoom` goes back):
 *
 * - the SESSION track is the session's whole span, first record to last, with
 *   the stretch this read covers drawn solid and the rest hatched as not read.
 *   A long session is read in pages (the read stops at a budget); clicking an
 *   unread stretch reads the page that starts there.
 * - the STRIP is the read's own axis, the one the tree's timeline column uses:
 *   process starts per time bin, stacked by purpose. Zooming it zooms the
 *   tree's timeline column with it and marks the rows that ran in range;
 *   clicking a moment opens the tree at the first process started at or after
 *   it. The bins come from every record the read matched, itemised or only
 *   counted (`nav` in rc_dashboard/proctree.py), so a poll loop the tree shows
 *   as one counted row still shows here as the activity it was.
 *
 * Failures are ticks under the strip, red for a signal and pink for a non-zero
 * exit -- the timeline bars' own two colours. The quick jumps below go to the
 * landmarks the read found; what they landed on, and whether it is the process
 * itself or the row that counts it, is said on the line under them.
 */
import { useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import type { TrajNav, TrajectoryDetail } from '../api/types';
import { ZOOM_HINT, ZoomMap, ZoomNote, svgAt, useWheel, wheelFactor, wheelPan } from '../charts/primitives';
import { DASH, clock, dur, fint } from '../lib/format';
import { PURP, pcol, plbl } from '../lib/purposes';

/** Purposes outside the eight colour slots stack as one `other`, as they are coloured. */
const SLOT = new Set(PURP.map(([k]) => k));
const ORDER = PURP.map(([k]) => k);
const slotOf = (p: string) => (SLOT.has(p) ? p : 'other');

/** Tick steps, seconds: a label every ~100 px. */
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

function ticks(v0: number, v1: number, room: number): { t: number; lbl: string }[] {
  const step = STEPS.find((s) => (v1 - v0) / s <= room) ?? STEPS[STEPS.length - 1];
  // On the local clock: an hour tick is a local hour, whatever the offset.
  const off = -new Date(v0 * 1000).getTimezoneOffset() * 60;
  const out: { t: number; lbl: string }[] = [];
  for (let k = Math.ceil((v0 + off) / step); k * step - off <= v1 && out.length < 40; k++) {
    const t = k * step - off;
    out.push({ t, lbl: step >= 60 ? clock(t).slice(0, 5) : clock(t) });
  }
  return out;
}

const SPAN_MIN = 1; // seconds: the narrowest zoom, whatever the bin width

/** Served bins regrouped into display bins at least ~3 px wide across `w` px,
 *  a whole number of served bins each, so a display bin is still a count of
 *  starts over a stated width; stacked by purpose slot. */
function regroup(den: NonNullable<TrajNav['density']>, purposes: string[], v0: number, v1: number, w: number) {
  const k = Math.max(1, Math.ceil(Math.max(v1 - v0, 1e-6) / den.w / (w / 3)));
  const dw = den.w * k;
  const per = new Map<number, Map<string, number>>();
  for (const [i, p, c] of den.bins) {
    const t = den.lo + i * den.w;
    if (t + den.w < v0 || t > v1) continue;
    const j = Math.floor(i / k);
    const m = per.get(j) ?? new Map<string, number>();
    const key = slotOf(purposes[p] ?? 'other');
    m.set(key, (m.get(key) ?? 0) + c);
    per.set(j, m);
  }
  let top = 0;
  for (const m of per.values()) {
    let s = 0;
    for (const c of m.values()) s += c;
    top = Math.max(top, s);
  }
  return { per, dw, top };
}

/** Stacked bars of `regroup`'s bins, on the x scale `X`, standing on `base`. */
function stackBars(
  g: ReturnType<typeof regroup>,
  lo: number,
  X: (t: number) => number,
  base: number,
  ph: number,
  W: number,
  min = 0.8,
): ReactNode[] {
  const bars: ReactNode[] = [];
  if (g.top <= 0) return bars;
  for (const [j, m] of g.per) {
    const t0 = lo + j * g.dw;
    const x0 = Math.max(0, X(t0));
    const x1 = Math.min(W, X(t0 + g.dw));
    if (x1 <= x0) continue;
    const w = Math.max(min, x1 - x0 - (x1 - x0 > 3 ? 0.6 : 0));
    let y = base;
    for (const key of ORDER) {
      const c = m.get(key);
      if (!c) continue;
      const h = Math.max(min, (ph * c) / g.top);
      y -= h;
      bars.push(<rect key={`${j}-${key}`} x={x0} y={y} width={w} height={h} fill={pcol(key)} />);
    }
  }
  return bars;
}

export interface Quick {
  key: string;
  lbl: string;
  tip: string;
  run?: () => void;
}

/* ------------------------------------------------------------ session track */

function SessionTrack({
  d,
  axis,
  view,
  onPick,
  W,
}: {
  d: TrajectoryDetail;
  axis: [number, number];
  view: [number, number] | null;
  onPick: (t: number) => void;
  W: number;
}) {
  const hatch = 'tn' + useId().replace(/[^\w-]/g, '');
  const s = d.scan;
  const first = d.span?.first ?? axis[0];
  const last = d.span?.last ?? axis[1];
  const S0 = Math.min(first, axis[0], s?.from ?? axis[0]);
  const S1 = Math.max(last, axis[1]);
  const whole = Math.max(S1 - S0, 1e-6);
  // What this read covers: from where it began to where it stopped, or to the
  // session's end when it did not stop.
  const r0 = s?.from ?? axis[0];
  const r1 = s?.stopped && s.stopped_at != null ? s.stopped_at : S1;
  const H = 14;
  const X = (t: number) => ((Math.min(S1, Math.max(S0, t)) - S0) / whole) * W;
  const v = view ?? axis;
  const [hx, setHx] = useState<number | null>(null);
  const tAt = (x: number) => S0 + (Math.min(W, Math.max(0, x)) / W) * whole;
  const partial = X(r0) > 0.5 || X(r1) < W - 0.5;
  const hoverT = hx != null ? tAt(hx) : null;
  const unread = hoverT != null && (hoverT < r0 - 0.5 || hoverT > r1 + 0.5);
  const tip =
    hoverT == null
      ? undefined
      : unread
        ? `${clock(hoverT)} · not in this read: click to read the page that starts here`
        : `${clock(hoverT)} · click to open the tree at the first process started at or after it`;
  return (
    <div className="tn-track">
      <div className="tn-cap">
        <span className="eyebrow">session</span>
        <span>
          {`${clock(S0)} → ${clock(S1)}`}
          {partial ? (
            <>
              {' · this read '}
              <b>{`${clock(r0)} → ${clock(r1)}`}</b>
              {' · hatched: not read'}
            </>
          ) : (
            ' · read whole'
          )}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        preserveAspectRatio="none"
        className="tn-svg"
        role="img"
        aria-label={`the session from ${clock(S0)} to ${clock(S1)}; this read covers ${clock(r0)} to ${clock(r1)}`}
        onPointerMove={(e) => setHx(svgAt(e.currentTarget, e, W, H)?.x ?? null)}
        onPointerLeave={() => setHx(null)}
        onClick={(e) => {
          const p = svgAt(e.currentTarget, e, W, H);
          if (p) onPick(tAt(p.x));
        }}
        data-tip={tip}
        style={{ cursor: 'pointer' }}
      >
        <defs>
          <pattern id={hatch} width="5" height="5" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <rect width="5" height="5" fill="var(--surface)" />
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--edge)" strokeWidth="2" />
          </pattern>
        </defs>
        <rect x={0} y={2} width={W} height={H - 4} fill={`url(#${hatch})`} />
        <rect x={X(r0)} y={2} width={Math.max(1.5, X(r1) - X(r0))} height={H - 4} className="tn-read" />
        <rect x={X(v[0])} y={0.75} width={Math.max(2, X(v[1]) - X(v[0]))} height={H - 1.5} className="tn-view" />
        {hx != null && <line x1={hx} x2={hx} y1={0} y2={H} className="tn-hover" />}
      </svg>
    </div>
  );
}

/* ---------------------------------------------------------------- the strip */

function Strip({
  nav,
  axis,
  view,
  setView,
  cursor,
  onPick,
  onFail,
  inView,
  W,
}: {
  nav: TrajNav;
  axis: [number, number];
  view: [number, number] | null;
  setView: (v: [number, number] | null) => void;
  cursor: number | null;
  onPick: (t: number) => void;
  onFail: (i: number) => void;
  /** processes drawn in the tree that ran in the zoomed range */
  inView: number | null;
  W: number;
}) {
  const H = 62;
  const TOP = 13;
  const PH = 32;
  const BASE = TOP + PH;
  const den = nav.density;
  const [a0, a1] = axis;
  const full = Math.max(a1 - a0, 1e-6);
  const zoomed = view != null && view[1] > a0 && view[0] < a1;
  const v0 = zoomed ? Math.max(a0, view[0]) : a0;
  const v1 = zoomed ? Math.min(a1, view[1]) : a1;
  const span = Math.max(v1 - v0, 1e-6);
  const X = (t: number) => ((t - v0) / span) * W;
  const T = (x: number) => v0 + (Math.min(W, Math.max(0, x)) / W) * span;
  const fit = (lo: number, hi: number): [number, number] | null => {
    const s = hi - lo;
    if (s >= full) return null;
    const l = Math.min(Math.max(lo, a0), a1 - s);
    return [l, l + s];
  };
  const zoomAt = (at: number, f: number) => {
    if (!zoomed && f >= 1) return;
    const ns = Math.min(full, Math.max(Math.min(SPAN_MIN, full), span * f));
    const lo = at - ((at - v0) * ns) / span;
    setView(fit(lo, lo + ns));
  };
  const wheel = useWheel((w, svg) => {
    const p = svgAt(svg, w, W, H);
    if (!p) return;
    const slide = wheelPan(w);
    if (slide) {
      if (zoomed) {
        const d = ((slide * p.k) / W) * span;
        setView(fit(v0 + d, v1 + d));
      }
      return;
    }
    zoomAt(T(p.x), wheelFactor(w));
  });

  const shown = useMemo(
    () => (den && den.bins.length ? regroup(den, nav.purposes, v0, v1, W) : null),
    [den, nav.purposes, v0, v1, W],
  );
  // the whole axis, for the minimap under a zoomed strip
  const whole = useMemo(
    () => (zoomed && den && den.bins.length ? regroup(den, nav.purposes, a0, a1, W) : null),
    [zoomed, den, nav.purposes, a0, a1, W],
  );

  const [hx, setHx] = useState<number | null>(null);
  const [kc, setKc] = useState<number | null>(null); // the keyboard's cursor, a time
  const grab = useRef<{ cx: number; sx: number; v0: number; moved: boolean; fail: number | null } | null>(null);
  const [panning, setPanning] = useState(false);
  const handlers = {
    onPointerDown: (e: ReactPointerEvent<SVGGElement>) => {
      if (e.button !== 0) return;
      const p = svgAt(e.currentTarget, e, W, H);
      if (!p) return;
      const f = (e.target as Element).getAttribute?.('data-fail');
      grab.current = { cx: e.clientX, sx: p.x, v0, moved: false, fail: f != null ? Number(f) : null };
      if (zoomed && e.pointerType !== 'touch') e.currentTarget.setPointerCapture(e.pointerId);
    },
    onPointerMove: (e: ReactPointerEvent<SVGGElement>) => {
      const p = svgAt(e.currentTarget, e, W, H);
      setHx(p ? p.x : null);
      const g = grab.current;
      if (!g || !p) return;
      if (Math.abs(e.clientX - g.cx) > 3) g.moved = true;
      if (!zoomed || !g.moved) return;
      setPanning(true);
      const d = -((p.x - g.sx) / W) * span;
      setView(fit(g.v0 + d, g.v0 + span + d));
    },
    onPointerUp: (e: ReactPointerEvent<SVGGElement>) => {
      const g = grab.current;
      grab.current = null;
      setPanning(false);
      if (!g || g.moved) return;
      if (g.fail != null) {
        onFail(g.fail);
        return;
      }
      const p = svgAt(e.currentTarget, e, W, H);
      if (p) onPick(T(p.x));
    },
    onPointerCancel: () => {
      grab.current = null;
      setPanning(false);
    },
    onPointerLeave: () => setHx(null),
  };
  const onKey = (e: ReactKeyboardEvent<SVGGElement>) => {
    const at = kc ?? cursor ?? (v0 + v1) / 2;
    const stepT = span / 40;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const t = Math.min(a1, Math.max(a0, at + (e.key === 'ArrowRight' ? stepT : -stepT)));
      setKc(t);
      if (zoomed && (t < v0 || t > v1)) setView(fit(t - span / 2, t + span / 2));
    } else if (e.key === 'Enter' || e.key === ' ') {
      onPick(at);
    } else if (e.key === '+' || e.key === '=' || e.key === '-') {
      zoomAt(at, e.key === '-' ? 1.6 : 1 / 1.6);
    } else if (e.key === '0' || e.key === 'Escape') {
      setView(null);
    } else return;
    e.preventDefault();
  };

  const bars: ReactNode[] = shown && den ? stackBars(shown, den.lo, X, BASE, PH, W) : [];
  const fails = nav.failures.items;
  const ticksOut: ReactNode[] = [];
  fails.forEach((f, i) => {
    const t = f[0];
    if (t == null || t < v0 || t > v1) return;
    ticksOut.push(
      <rect
        key={i}
        x={X(t) - 1}
        y={BASE + 2}
        width={2.4}
        height={6}
        className={f[5] != null ? 'tn-fail sig' : 'tn-fail'}
        data-fail={i}
        data-tip={`${clock(t)} · ${f[3] ?? '?'} · ${f[5] != null ? `signal ${f[5]}` : `exit ${f[4]}`} · click to open it`}
      />,
    );
  });

  // What the pointer is over, for the tooltip on the plot.
  let tip = ZOOM_HINT;
  if (hx != null && den) {
    const t = T(hx);
    let n = 0;
    const by: string[] = [];
    if (shown) {
      const j = Math.floor((t - den.lo) / shown.dw);
      const m = shown.per.get(j);
      if (m) {
        for (const key of ORDER) {
          const c = m.get(key);
          if (c) {
            n += c;
            by.push(`${plbl(key)} ${fint(c)}`);
          }
        }
      }
    }
    tip = `${clock(t)} · ${fint(n)} started in this ${dur(shown?.dw ?? den.w)}${
      by.length ? ` (${by.join(', ')})` : ''
    }\nclick: open the tree at the first process started at or after it\n${ZOOM_HINT}`;
  }
  const xt = ticks(v0, v1, W / 100);
  const kx = kc != null && kc >= v0 && kc <= v1 ? X(kc) : null;
  const cx = cursor != null && cursor >= v0 && cursor <= v1 ? X(cursor) : null;
  return (
    <>
      <ZoomNote
        zoomed={zoomed}
        text={`${clock(v0)} – ${clock(v1)} (${dur(v1 - v0)}) · drag to pan${
          inView != null ? ` · ${fint(inView)} drawn process${inView === 1 ? '' : 'es'} ran in it` : ''
        }`}
        hint="scroll to zoom · click to open the tree there"
        reset={() => setView(null)}
      />
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        height={H}
        className="tn-svg strip"
        role="img"
        aria-label={`process starts per time bin from ${clock(v0)} to ${clock(v1)}`}
      >
        <text className="tick" x={2} y={9}>
          {shown && shown.top > 0
            ? `process starts per ${dur(shown.dw)} · tallest ${fint(shown.top)}`
            : den
              ? 'no process started in this range'
              : 'no start times in this read'}
          {den && den.before > 0 ? ` · ${fint(den.before)} started before the axis` : ''}
        </text>
        <line className="base-line" x1={0} x2={W} y1={BASE} y2={BASE} />
        <g
          ref={wheel}
          className="zoomable"
          data-tip={tip}
          tabIndex={0}
          role="slider"
          aria-label="session time: arrow keys move, Enter opens the tree there, + and − zoom, 0 resets"
          aria-valuemin={a0}
          aria-valuemax={a1}
          aria-valuenow={kc ?? cursor ?? v0}
          aria-valuetext={clock(kc ?? cursor ?? v0)}
          style={{ cursor: zoomed ? (panning ? 'grabbing' : 'grab') : 'pointer', outline: 'none' }}
          onDoubleClick={() => setView(null)}
          onKeyDown={onKey}
          onBlur={() => setKc(null)}
          {...handlers}
        >
          <rect x={0} y={0} width={W} height={BASE + 10} fill="transparent" />
          {bars}
          {ticksOut}
          {cx != null && <line className="tn-cursor" x1={cx} x2={cx} y1={TOP - 2} y2={BASE + 8} />}
          {kx != null && <line className="tn-key" x1={kx} x2={kx} y1={TOP - 2} y2={BASE + 8} />}
          {hx != null && <line className="tn-hover" x1={hx} x2={hx} y1={TOP} y2={BASE} />}
        </g>
        {xt.map(({ t, lbl }) => {
          const x = X(t);
          const anchor = x > W - 24 ? 'end' : x < 24 ? 'start' : 'middle';
          return (
            <text key={t} className="tick" x={x} y={H - 1} textAnchor={anchor}>
              {lbl}
            </text>
          );
        })}
      </svg>
      {zoomed && (
        <ZoomMap
          W={W}
          H={26}
          lo={a0}
          hi={a1}
          a={v0}
          b={v1}
          minSpan={Math.min(SPAN_MIN, full)}
          onSet={(lo, hi) => setView(fit(lo, hi))}
          onReset={() => setView(null)}
          className="tn-svg"
          label={`the read from ${clock(a0)} to ${clock(a1)}, with ${clock(v0)} to ${clock(v1)} in view`}
          draw={(MX, MH) => (whole && den ? stackBars(whole, den.lo, MX, MH - 2, MH - 5, W, 0.5) : null)}
        />
      )}
    </>
  );
}

/* ---------------------------------------------------------------- navigator */

export function TrajectoryNav({
  d,
  axis,
  view,
  setView,
  cursor,
  onPick,
  onFail,
  quick,
  status,
  inView,
}: {
  d: TrajectoryDetail;
  /** the read's own axis: the tree's timeline when not zoomed */
  axis: [number, number];
  view: [number, number] | null;
  setView: (v: [number, number] | null) => void;
  cursor: number | null;
  onPick: (t: number) => void;
  onFail: (i: number) => void;
  quick: Quick[];
  status: ReactNode;
  inView: number | null;
}) {
  const nav = d.nav;
  const W = 1120;
  return (
    <div className="tn">
      <SessionTrack d={d} axis={axis} view={view} onPick={onPick} W={W} />
      {nav ? (
        <Strip
          nav={nav}
          axis={axis}
          view={view}
          setView={setView}
          cursor={cursor}
          onPick={onPick}
          onFail={onFail}
          inView={inView}
          W={W}
        />
      ) : (
        <div className="nm">{`no navigator in this payload ${DASH} the service predates it`}</div>
      )}
      <div className="tn-quick" role="toolbar" aria-label="jump to">
        {quick.map((q) => (
          // aria-disabled, not disabled: a disabled button gets no pointer
          // events, and its tooltip is what says why there is nothing to go to
          <button
            key={q.key}
            className="btn"
            aria-disabled={!q.run}
            onClick={q.run}
            data-tip={q.tip}
          >
            {q.lbl}
          </button>
        ))}
      </div>
      <div className="tn-status" aria-live="polite">
        {status}
      </div>
    </div>
  );
}
