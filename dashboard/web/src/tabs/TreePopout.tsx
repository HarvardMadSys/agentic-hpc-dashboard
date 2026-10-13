/* A session's process tree in a tab of its own, for reading a big tree the
 * way a model graph is read in Netron. The whole window is the canvas: a drag
 * pans it, the wheel scrolls it, Ctrl+wheel or a pinch zooms it. A click on a
 * box shows everything the read knows about that process in a panel beside
 * it; the caret, a double-click, or Enter opens what it started.
 *
 * It is the inspector's tree, read the same way (`/api/trajectory` with the
 * same session, window, and start), drawn from the same boxes
 * (ProcDiagram.tsx) on the same layout (TreeDiagram.tsx `treeGeometry`), so a
 * box here is the box there and has the same id. What it adds is room: find,
 * a minimap, and export as SVG or PNG. The inspector's "pop out" opens it
 * (`?pop=tree&traj=<sid>`, lib/popout.ts).
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as RPointerEvent, ReactNode } from 'react';
import type { ProcCount, ProcGroup, ProcNode } from '../api/types';
import { useConfig, useTrajectory } from '../api/hooks';
import {
  Box,
  DEFAULT_ROUTINE,
  boxText,
  hiddenCount,
  procNodes,
  reveal,
} from '../components/ProcDiagram';
import type { Item, PNode } from '../components/ProcDiagram';
import { LINK_LBL, LINK_TIP, ROLE_TAG, allOpen, sigName, spots } from '../components/ProcTree';
import { treeGeometry } from '../components/TreeDiagram';
import type { TreeGeo } from '../components/TreeDiagram';
import { TooltipLayer } from '../components/Tooltip';
import { serverNow } from '../lib/clock';
import { DASH, clock, dur, fint, fmt } from '../lib/format';
import { dashboardHref, readTreePop } from '../lib/popout';
import { pcol, plbl } from '../lib/purposes';
import { sessionName } from '../lib/sessions';
import './trajectories.css';
import './treepop.css';

/* the inline diagram's sizes, so a box reads the same in both */
const NW = 206;
const NH = 80;
const GX = 14;
const GY = 26;
const K_MIN = 0.02;
const K_MAX = 4;

interface View {
  x: number;
  y: number;
  k: number;
}

const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v));

/** Date and time, for the details panel: a read can span days. */
function stamp(t: number | null | undefined): string {
  if (t == null) return DASH;
  const d = new Date(t * 1000);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${clock(t)}`;
}

/** What find looks through: everything a box or its details name. */
function textOf(e: Item): string {
  if (e.k === 'p')
    return [e.cmd, e.label, e.eff, e.cmdline, e.args, e.pid != null ? `pid ${e.pid}` : null, e.cwd, e.agent_type]
      .filter(Boolean)
      .join('\n')
      .toLowerCase();
  if (e.k === 'g') return [...e.motif, ...e.samples.map((s) => s.cmdline ?? s.args ?? '')].join('\n').toLowerCase();
  if (e.k === 'agg' || e.k === 'more') return e.cmds.map(([c]) => c).join('\n').toLowerCase();
  return '';
}

/* ---------------------------------------------------------------- export */

const xml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function clipText(s: string, n: number): string {
  return s.length > n ? s.slice(0, Math.max(1, n - 1)) + '…' : s;
}

/** The tree as drawn, as a standalone SVG: the boxes' text from `boxText`, the
 *  page's colours resolved, so the file reads the same anywhere. */
function treeSvg(geo: TreeGeo<PNode['data']>, open: Set<string>): { svg: string; w: number; h: number } {
  const st = getComputedStyle(document.documentElement);
  const c = (v: string) => v.replace(/var\((--[\w-]+)\)/g, (_, n: string) => st.getPropertyValue(n).trim() || '#999');
  const P = 16;
  const w = Math.ceil(geo.W + 2 * P);
  const h = Math.ceil(geo.H + 2 * P);
  const out: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" ` +
      `font-family="IBM Plex Mono, ui-monospace, Menlo, monospace">`,
    `<rect width="${w}" height="${h}" fill="${c('var(--page)')}"/>`,
    `<g transform="translate(${P},${P})">`,
    `<g fill="none" stroke="${c('var(--edge)')}" stroke-width="1.2">${geo.edges.map((e) => `<path d="${e.d}"/>`).join('')}</g>`,
  ];
  const ink = c('var(--ink)');
  const ink2 = c('var(--ink-2)');
  const muted = c('var(--muted)');
  const crit = c('var(--crit)');
  for (const b of geo.boxes) {
    const t = boxText(b.n.data.e, open.has(b.n.id), b.n.data.kids, b.n.id === 'r');
    const bg = t.kind === 'g' ? c('var(--surface-2)') : c('var(--surface)');
    const rule = t.color ? c(t.color) : t.kind === 'more' ? c('var(--rule)') : c('var(--edge)');
    out.push(`<g transform="translate(${b.x.toFixed(1)},${b.y.toFixed(1)})">`);
    out.push(`<rect width="${NW}" height="${NH}" rx="6" fill="${bg}" stroke="${c('var(--rule)')}"/>`);
    if (t.bad) out.push(`<rect width="${NW}" height="${NH}" rx="6" fill="${crit}" fill-opacity="0.06"/>`);
    if (t.kind === 'g')
      out.push(`<rect x="0.5" y="3" width="1" height="${NH - 6}" fill="${rule}"/><rect x="2.5" y="3" width="1" height="${NH - 6}" fill="${rule}"/>`);
    else out.push(`<rect x="0.5" y="3" width="3" height="${NH - 6}" fill="${rule}"/>`);
    // a mono glyph is about 0.6 em wide: cut each line to what fits
    const room = NW - 20 - (t.caret ? t.caret.length * 6 + 6 : 0);
    const title = clipText(t.title, Math.floor((room - (t.type ? t.type.length * 5.7 + 10 : 0)) / 6.9));
    out.push(
      `<text x="10" y="17" font-size="11.5" font-weight="${t.kind === 'more' ? 400 : 600}" fill="${t.kind === 'more' ? muted : ink}">` +
        `${xml(title)}${t.type ? `<tspan fill="${ink2}" font-size="9.5" font-weight="400">  ${xml(t.type)}</tspan>` : ''}</text>`,
    );
    if (t.caret) out.push(`<text x="${NW - 8}" y="16" font-size="10" text-anchor="end" fill="${muted}">${xml(t.caret)}</text>`);
    t.lines.forEach((pieces, i) => {
      let left = Math.floor((NW - 18) / 6.3);
      const spans: string[] = [];
      for (const [s, bad] of pieces) {
        if (!s || left <= 0) continue;
        const cut = clipText(s, left);
        left -= cut.length;
        spans.push(`<tspan fill="${bad ? crit : t.kind === 'more' ? muted : ink2}">${xml(cut)}</tspan>`);
      }
      out.push(`<text x="10" y="${34 + i * 16}" font-size="10.5" xml:space="preserve">${spans.join('')}</text>`);
    });
    out.push('</g>');
  }
  out.push('</g></svg>');
  return { svg: out.join(''), w, h };
}

function download(blob: Blob, name: string) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/** The SVG drawn into a canvas, twice the size where that fits. A tree too big
 *  for one image is drawn smaller: the returned scale says how much. */
async function svgPng(svg: string, w: number, h: number): Promise<{ blob: Blob | null; scale: number }> {
  const scale = Math.min(2, 16000 / w, 16000 / h, Math.sqrt(120e6 / (w * h)));
  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
  try {
    const img = new Image();
    await new Promise((res, rej) => {
      img.onload = res;
      img.onerror = rej;
      img.src = url;
    });
    const cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.floor(w * scale));
    cv.height = Math.max(1, Math.floor(h * scale));
    const ctx = cv.getContext('2d');
    if (!ctx) return { blob: null, scale };
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, w, h);
    return { blob: await new Promise<Blob | null>((res) => cv.toBlob(res, 'image/png')), scale };
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* --------------------------------------------------------------- minimap */

const MM_W = 200;
const MM_H = 140;

/** The whole tree small, with the part on screen boxed: a click or a drag on
 *  it moves the view there. */
function TreeMinimap({
  geo,
  view,
  size,
  sel,
  hits,
  go,
}: {
  geo: TreeGeo<PNode['data']>;
  view: View;
  size: { w: number; h: number };
  sel: string | null;
  hits: Set<string>;
  /** centre the view on this point of the canvas */
  go: (x: number, y: number) => void;
}) {
  const m = Math.min(MM_W / geo.W, MM_H / geo.H);
  const w = Math.max(24, geo.W * m);
  const h = Math.max(16, geo.H * m);
  const marks = useMemo(
    () =>
      geo.boxes.map((b) => {
        const e = b.n.data.e;
        const on = b.n.id === sel;
        const hit = hits.has(b.n.id);
        return (
          <rect
            key={b.n.id}
            x={b.x * m}
            y={b.y * m}
            width={Math.max(1, NW * m)}
            height={Math.max(1, NH * m)}
            style={{
              fill: on ? 'var(--crimson)' : hit ? 'var(--warn)' : e.k === 'p' ? pcol(e.purpose) : 'var(--edge)',
              opacity: on || hit ? 1 : 0.7,
            }}
          />
        );
      }),
    [geo, m, sel, hits],
  );
  const down = useRef(false);
  const at = (ev: RPointerEvent<SVGSVGElement>) => {
    const r = ev.currentTarget.getBoundingClientRect();
    go((ev.clientX - r.left) / m, (ev.clientY - r.top) / m);
  };
  return (
    <div className="tp-mini" data-tip="the whole tree: click or drag to move the view">
      <svg
        width={w}
        height={h}
        onPointerDown={(ev) => {
          ev.stopPropagation();
          down.current = true;
          ev.currentTarget.setPointerCapture(ev.pointerId);
          at(ev);
        }}
        onPointerMove={(ev) => down.current && at(ev)}
        onPointerUp={() => (down.current = false)}
        onPointerCancel={() => (down.current = false)}
      >
        {marks}
        <rect
          className="vp"
          x={(-view.x / view.k) * m}
          y={(-view.y / view.k) * m}
          width={(size.w / view.k) * m}
          height={(size.h / view.k) * m}
        />
      </svg>
    </div>
  );
}

/* --------------------------------------------------------------- details */

function Pair({ k, tip, children }: { k: string; tip?: string; children: ReactNode }) {
  return (
    <>
      <dt data-tip={tip}>{k}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Cmd({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="tp-cmd">
      <pre>{text}</pre>
      <button
        className="btn"
        onClick={() => {
          navigator.clipboard?.writeText(text).then(
            () => {
              setDone(true);
              setTimeout(() => setDone(false), 1200);
            },
            () => undefined,
          );
        }}
      >
        {done ? 'copied' : 'copy'}
      </button>
    </div>
  );
}

function ProcDetails({ e }: { e: ProcNode }) {
  const alive = e.ev == null && e.start != null && e.end != null;
  return (
    <>
      <dl>
        <Pair k="pid">{e.pid ?? DASH}</Pair>
        <Pair k="parent pid">{e.ppid ?? DASH}</Pair>
        {e.user && <Pair k="user">{e.user}</Pair>}
        <Pair k="started">{stamp(e.start)}</Pair>
        <Pair k={e.ev === 'exit' ? 'exited' : e.ev === 'truncated' ? 'alive at stop' : 'last seen'}>{stamp(e.end)}</Pair>
        <Pair k="took">{e.dur != null ? dur(e.dur) : alive ? `≥ ${dur((e.end as number) - (e.start as number))}` : DASH}</Pair>
        <Pair k="own CPU" tip="the process's own cpu_s">
          {e.ev ? (e.cpu != null ? `${fmt(e.cpu)} s` : 'not measured') : DASH}
        </Pair>
        <Pair k="Σ CPU" tip="its own cpu_s plus everything below it">
          {e.sub_cpu != null ? `${fmt(e.sub_cpu)} s` : DASH}
        </Pair>
        <Pair k="processes" tip="in its subtree, itself included">{fint(e.n)}</Pair>
        {e.cpu_nm ? <Pair k="CPU not measured">{`${fint(e.cpu_nm)} below it`}</Pair> : null}
        <Pair k="exit">
          {e.ev === 'truncated' ? (
            'alive when the collector stopped'
          ) : e.ev !== 'exit' ? (
            'no exit record in this read'
          ) : e.signal != null ? (
            <span className="bad">{`${sigName(e.signal)}${e.core ? ', core dumped' : ''}`}</span>
          ) : e.exit_code != null ? (
            <span className={e.exit_code ? 'bad' : undefined}>{e.exit_code}</span>
          ) : (
            DASH
          )}
        </Pair>
        {e.cwd && <Pair k="working dir">{e.cwd}</Pair>}
        {e.link && (
          <Pair k="parent link" tip={LINK_TIP[e.link]}>
            {LINK_LBL[e.link]}
            {e.gap ? ` · ${e.gap} level${e.gap === 1 ? '' : 's'} unrecorded` : ''}
          </Pair>
        )}
        {e.orphan && <Pair k="reparented">its record names init: it outlived its parent</Pair>}
      </dl>
      {e.cmdline && (
        <>
          <div className="tp-lbl">what it ran</div>
          <Cmd text={e.cmdline} />
        </>
      )}
      {e.args && e.args !== e.cmdline && (
        <>
          <div className="tp-lbl">argv</div>
          <Cmd text={e.args} />
        </>
      )}
    </>
  );
}

function GroupDetails({ g }: { g: ProcGroup }) {
  const exits = Object.entries(g.exits ?? {}).sort((a, b) => b[1] - a[1]);
  const sigs = Object.entries(g.signals ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <dl>
        <Pair k="runs in a row">{fint(g.count)}</Pair>
        {g.period > 1 && <Pair k="cycle">{`${g.period} steps, repeated ${fint(g.reps)} times`}</Pair>}
        <Pair k="processes" tip="in the runs and below them">{fint(g.n)}</Pair>
        <Pair k="first started">{stamp(g.start)}</Pair>
        <Pair k="last exited">{stamp(g.end)}</Pair>
        <Pair k="span">{g.start != null && g.end != null ? dur(g.end - g.start) : DASH}</Pair>
        <Pair k="longest run">{g.dur_max != null ? dur(g.dur_max) : DASH}</Pair>
        <Pair k="own CPU">{g.cpu != null ? `${fmt(g.cpu)} s` : DASH}</Pair>
        <Pair k="Σ CPU">{g.sub_cpu != null ? `${fmt(g.sub_cpu)} s` : DASH}</Pair>
        <Pair k="exits">
          {exits.length ? exits.map(([c, n]) => (
            <span key={c} className={c !== '0' ? 'bad' : undefined}>{`${c} ×${fint(n)} `}</span>
          )) : DASH}
        </Pair>
        {sigs.length > 0 && (
          <Pair k="signals">
            <span className="bad">{sigs.map(([s, n]) => `${sigName(s)} ×${fint(n)}`).join(', ')}</span>
          </Pair>
        )}
        {g.alive ? <Pair k="alive at stop">{fint(g.alive)}</Pair> : null}
        <Pair k="drawn" tip="the first members are boxes of their own; the rest are counted in the group's totals">
          {`${fint(g.items.length)} of ${fint(g.items.length + g.more)}`}
        </Pair>
      </dl>
      <div className="tp-lbl">{g.distinct > 1 ? `${fint(g.distinct)} distinct command lines, most common` : 'command line'}</div>
      {g.samples.map((s, i) => (
        <div key={i} className="tp-sample">
          <span className="mono">{`${fint(s.n)} ×`}</span>
          <Cmd text={s.cmdline ?? s.args ?? ''} />
        </div>
      ))}
    </>
  );
}

function CountDetails({ c }: { c: ProcCount }) {
  return (
    <>
      <p className="tp-why">
        {c.k === 'agg'
          ? "Past the inspector's hold budget: counted with their CPU and time span under this parent, but not drawn one by one."
          : 'Past the number of rows the inspector serves, which it allots breadth first: counted here, the deepest levels first to go.'}
      </p>
      <dl>
        <Pair k="processes">{fint(c.n)}</Pair>
        {c.count != null && <Pair k="sibling rows">{fint(c.count)}</Pair>}
        <Pair k="first started">{stamp(c.start)}</Pair>
        <Pair k="last exited">{stamp(c.end)}</Pair>
        <Pair k="Σ CPU">{c.sub_cpu != null ? `${fmt(c.sub_cpu)} s` : DASH}</Pair>
        {c.cpu_nm ? <Pair k="CPU not measured">{fint(c.cpu_nm)}</Pair> : null}
      </dl>
      <div className="tp-lbl">commands</div>
      <dl>
        {c.cmds.map(([k, n]) => (
          <Pair key={k} k={`×${fint(n)}`}>
            {k}
          </Pair>
        ))}
      </dl>
    </>
  );
}

function Details({
  n,
  open,
  toggle,
  find,
  close,
}: {
  n: PNode;
  open: boolean;
  toggle: () => void;
  find: (s: string) => void;
  close: () => void;
}) {
  const e = n.data.e;
  const kids = n.data.kids;
  const head =
    e.k === 'p' ? (e.cmd ?? `pid ${e.pid ?? '?'}`) : e.k === 'g' ? `${e.motif.join(' › ')} ×${fint(e.reps)}` : boxText(e, false, 0, false).title;
  const findWhat = e.k === 'p' ? (e.eff ?? e.cmd) : e.k === 'g' ? e.motif[0] : null;
  return (
    <aside className="tp-side" aria-label="the selected box">
      <div className="tp-side-h">
        <span className="eyebrow">{e.k === 'p' ? 'process' : e.k === 'g' ? 'runs of one shape' : 'counted'}</span>
        <button className="btn" onClick={close} aria-label="close the details">
          ×
        </button>
      </div>
      <div className="tp-side-t mono">{head}</div>
      {e.k === 'p' && (
        <div className="tp-tags">
          {e.agent_type && <span className="pt-tag type">{e.agent_type}</span>}
          {e.role && ROLE_TAG[e.role] && <span className="pt-tag">{ROLE_TAG[e.role]}</span>}
          {e.eff && e.eff !== e.cmd && <span className="pt-tag">{`runs ${e.eff}`}</span>}
          <span className="tp-purpose">
            <i style={{ background: pcol(e.purpose) }} />
            {plbl(e.purpose)}
          </span>
        </div>
      )}
      <div className="tp-acts">
        {kids > 0 && (
          <button className="btn" onClick={toggle}>
            {open ? 'close what it started' : `open what it started (${fint(kids)})`}
          </button>
        )}
        {findWhat && (
          <button className="btn" onClick={() => find(findWhat)} data-tip="find every box that names this command">
            find {findWhat}
          </button>
        )}
      </div>
      {e.k === 'p' && <ProcDetails e={e} />}
      {e.k === 'g' && <GroupDetails g={e} />}
      {(e.k === 'agg' || e.k === 'more') && <CountDetails c={e} />}
      {e.k === 'rest' && (
        <p className="tp-why">{`${fint(e.more)} more members of the group above: counted in its totals, not drawn one by one.`}</p>
      )}
    </aside>
  );
}

/* ------------------------------------------------------------------ page */

export default function TreePopout() {
  const [u] = useState(() => readTreePop(window.location.search) ?? { sid: null, win: null, from: null, hide: true });
  const [to, setTo] = useState<number | null>(null);
  const q = useTrajectory(u.sid, u.win, u.from, to);
  const config = useConfig();
  const routine = (config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.routine_commands?._value as
    | string[]
    | undefined;
  const rules = useMemo(() => (routine?.length ? routine : DEFAULT_ROUTINE), [routine]);
  const d = q.data && q.data.sid === u.sid ? q.data : null;
  const tree: ProcNode | null = d?.found ? (d.tree ?? null) : null;

  const [hide, setHide] = useState(u.hide);
  const [open, setOpen] = useState<Set<string>>(() => new Set(['r']));
  const [all, setAll] = useState<Set<string>>(() => new Set());
  const [sel, setSel] = useState<string | null>(null);
  const [note, setNote] = useState<ReactNode>(null);
  const [mini, setMini] = useState(true);
  const [query, setQuery] = useState('');
  const [hi, setHi] = useState(-1);

  const name = u.sid ? sessionName({ sid: u.sid, key_type: d?.row?.key_type, agent_type: d?.row?.agent_type }) : null;
  useEffect(() => {
    document.title = name ? `${name.key} · process tree` : 'Process tree';
  }, [name?.key]); // eslint-disable-line react-hooks/exhaustive-deps

  const nodes = useMemo(() => (tree ? procNodes(tree, open, hide, rules, all) : []), [tree, open, hide, rules, all]);
  const geo = useMemo(() => treeGeometry(nodes, NW, NH, GX, GY), [nodes]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  // find: through every served entry, drawn or not
  const index = useMemo(() => (tree ? spots(tree).map((s) => ({ id: s.id, t: textOf(s.e) })) : []), [tree]);
  const matches = useMemo(() => {
    const s = query.trim().toLowerCase();
    return s ? index.filter((x) => x.t.includes(s)).map((x) => x.id) : [];
  }, [index, query]);
  const hits = useMemo(() => new Set(matches), [matches]);

  /* ---- the view */
  const vp = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 0, h: 0 });
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const viewRef = useRef(view);
  viewRef.current = view;
  useLayoutEffect(() => {
    const el = vp.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const zoomAt = useCallback((f: number, px?: number, py?: number) => {
    setView((v) => {
      const k = clamp(v.k * f, K_MIN, K_MAX);
      const cx = px ?? sizeRef.current.w / 2;
      const cy = py ?? sizeRef.current.h / 2;
      return { k, x: cx - (cx - v.x) * (k / v.k), y: cy - (cy - v.y) * (k / v.k) };
    });
  }, []);
  const fit = () => {
    const { w, h } = sizeRef.current;
    const k = clamp(Math.min((w - 48) / geo.W, (h - 48) / geo.H, 1), K_MIN, 1);
    setView({ k, x: (w - geo.W * k) / 2, y: Math.max(24, (h - geo.H * k) / 2) });
  };
  const centreOn = (x: number, y: number, k?: number) =>
    setView((v) => {
      const kk = k ?? v.k;
      return { k: kk, x: sizeRef.current.w / 2 - x * kk, y: sizeRef.current.h / 2 - y * kk };
    });

  // After a re-layout: keep a box that was opened where it was on screen, or
  // centre the one find or the keys went to. On a new read: the root, at the top.
  const pin = useRef<{ id: string; sx: number; sy: number } | null>(null);
  const want = useRef<{ id: string; zoom: boolean } | null>(null);
  const home = useRef(true);
  const [tick, setTick] = useState(0);
  const stampKey = `${d?.sid ?? ''}|${d?.scan?.computed_at ?? ''}`;
  useEffect(() => {
    setOpen(new Set(['r']));
    setAll(new Set());
    setSel(null);
    home.current = true;
  }, [stampKey]);
  useLayoutEffect(() => {
    const { w } = sizeRef.current;
    if (home.current && geo.boxes.length && w) {
      home.current = false;
      const r = geo.at.get('r');
      if (r) setView({ k: 1, x: w / 2 - (r.x + NW / 2), y: 24 });
      return;
    }
    const p = pin.current;
    pin.current = null;
    const b = p ? geo.at.get(p.id) : undefined;
    if (p && b) setView((v) => ({ ...v, x: p.sx - b.x * v.k, y: p.sy - b.y * v.k }));
    const wnt = want.current;
    const c = wnt ? geo.at.get(wnt.id) : undefined;
    if (wnt && c) {
      want.current = null;
      const v = viewRef.current;
      const sx = v.x + (c.x + NW / 2) * v.k;
      const sy = v.y + (c.y + NH / 2) * v.k;
      const inView = sx > 40 && sx < sizeRef.current.w - 40 && sy > 30 && sy < sizeRef.current.h - 30;
      if (wnt.zoom || !inView) centreOn(c.x + NW / 2, c.y + NH / 2, wnt.zoom && v.k < 0.6 ? 1 : undefined);
    }
  }, [geo, tick, size.w > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---- acting on boxes */
  const toggle = (id: string) => {
    const n = byId.get(id);
    if (!n) return;
    const keep = n.data.e.k === 'cap' ? n.data.e.parent : id;
    const b = geo.at.get(keep);
    if (b) pin.current = { id: keep, sx: view.x + b.x * view.k, sy: view.y + b.y * view.k };
    if (n.data.e.k === 'cap') {
      setAll(new Set([...all, n.data.e.parent]));
      return;
    }
    if (!n.data.kids) return;
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };
  const pick = (id: string) => {
    const n = byId.get(id);
    if (n?.data.e.k === 'cap') toggle(id);
    else setSel(id);
  };
  /** Draw box `id` -- opening what is above it, and showing the routine when
   *  it is routine -- select it, and bring it into view. */
  const show = (id: string, zoom: boolean) => {
    if (!tree) return;
    const r = reveal(tree, id, hide, rules);
    if (r.hidden) {
      setHide(false);
      setNote('it is routine, so hide routine is off');
    } else setNote(null);
    if (r.open.some((x) => !open.has(x))) setOpen(new Set([...open, ...r.open]));
    if (r.all.some((x) => !all.has(x))) setAll(new Set([...all, ...r.all]));
    setSel(id);
    want.current = { id, zoom };
    setTick((t) => t + 1);
  };
  const step = (dir: 1 | -1) => {
    if (!matches.length) return;
    const i = hi < 0 || hi >= matches.length ? (dir > 0 ? 0 : matches.length - 1) : (hi + dir + matches.length) % matches.length;
    setHi(i);
    show(matches[i], true);
  };
  const move = (key: string) => {
    if (!sel || !byId.has(sel)) {
      if (byId.has('r')) show('r', false);
      return;
    }
    const n = byId.get(sel)!;
    if (key === 'ArrowUp') {
      if (n.parent) show(n.parent, false);
      return;
    }
    if (key === 'ArrowDown') {
      const first = nodes.find((x) => x.parent === sel);
      if (first) show(first.id, false);
      else if (n.data.kids) toggle(sel);
      return;
    }
    const sibs = nodes.filter((x) => x.parent === n.parent);
    const i = sibs.findIndex((x) => x.id === sel) + (key === 'ArrowRight' ? 1 : -1);
    if (i >= 0 && i < sibs.length) show(sibs[i].id, false);
  };

  // handlers the memoised boxes and the window listeners call: always the latest
  const act = useRef({ toggle, pick, move, zoomAt, fit, step });
  act.current = { toggle, pick, move, zoomAt, fit, step };

  /* ---- pointer and wheel */
  const drag = useRef<{ id: number; x0: number; y0: number; vx: number; vy: number; moved: boolean } | null>(null);
  const dragged = useRef(false);
  const [panning, setPanning] = useState(false);
  useEffect(() => {
    const el = vp.current;
    if (!el) return;
    const h = (e: WheelEvent) => {
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      const r = el.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        act.current.zoomAt(Math.exp(-e.deltaY * unit * 0.0025), e.clientX - r.left, e.clientY - r.top);
        return;
      }
      let dx = e.deltaX * unit;
      let dy = e.deltaY * unit;
      if (e.shiftKey && !dx) {
        dx = dy;
        dy = 0;
      }
      setView((v) => ({ ...v, x: v.x - dx, y: v.y - dy }));
    };
    el.addEventListener('wheel', h, { passive: false });
    return () => el.removeEventListener('wheel', h);
  }, []);

  const findRef = useRef<HTMLInputElement>(null);
  const selRef = useRef(sel);
  selRef.current = sel;
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const t = e.target as Element | null;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        findRef.current?.focus();
        findRef.current?.select();
        return;
      }
      if (t?.closest?.('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey) return;
      const onButton = !!t?.closest?.('button, a');
      const a = act.current;
      switch (e.key) {
        case '+':
        case '=':
          a.zoomAt(1.25);
          break;
        case '-':
        case '_':
          a.zoomAt(0.8);
          break;
        case '0':
          a.zoomAt(1 / viewRef.current.k);
          break;
        case 'f':
          a.fit();
          break;
        case 'Escape':
          setSel(null);
          break;
        case 'Enter':
        case ' ':
          if (onButton || !selRef.current) return;
          a.toggle(selRef.current);
          break;
        case 'ArrowUp':
        case 'ArrowDown':
        case 'ArrowLeft':
        case 'ArrowRight':
          a.move(e.key);
          break;
        default:
          return;
      }
      e.preventDefault();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  /* ---- the canvas: rebuilt when the boxes change, never for a pan or a zoom */
  const canvas = useMemo(
    () => (
      <div className="tdiag-canvas" style={{ width: geo.W, height: geo.H }}>
        <svg width={geo.W} height={geo.H} className="tdiag-edges" aria-hidden="true">
          {geo.edges.map((e) => (
            <path key={e.id} d={e.d} />
          ))}
        </svg>
        {geo.boxes.map((b) => {
          const id = b.n.id;
          return (
            <div
              key={id}
              className={`tnode click${sel === id ? ' on' : ''}${hits.has(id) ? ' hit' : ''}`}
              style={{ left: b.x, top: b.y, width: NW, height: NH }}
              onClick={() => !dragged.current && act.current.pick(id)}
              onDoubleClick={() => act.current.toggle(id)}
            >
              <Box
                e={b.n.data.e}
                open={open.has(id)}
                kids={b.n.data.kids}
                root={id === 'r'}
                onCaret={() => !dragged.current && act.current.toggle(id)}
              />
            </div>
          );
        })}
      </div>
    ),
    [geo, sel, hits, open],
  );

  const selNode = sel ? (byId.get(sel) ?? null) : null;
  const scan = d?.scan;
  const grid = 20 * view.k;
  const status = q.error
    ? `/api/trajectory: ${q.error}`
    : !u.sid
      ? 'no session in the link: open the tree from the inspector’s “pop out”'
      : d?.found === false
        ? `this session is not in the read: ${d.why ?? 'not in this window'}`
        : !tree
          ? 'reading the session’s records…'
          : null;

  const exportSvg = () => {
    const { svg } = treeSvg(geo, open);
    download(new Blob([svg], { type: 'image/svg+xml' }), `${fileBase()}.svg`);
  };
  const exportPng = async () => {
    const { svg, w, h } = treeSvg(geo, open);
    setNote('drawing the PNG…');
    try {
      const { blob, scale } = await svgPng(svg, w, h);
      if (!blob) {
        setNote('the browser could not draw the PNG; the SVG export has the same tree');
        return;
      }
      download(blob, `${fileBase()}.png`);
      setNote(scale < 1 ? `the tree is too big for one image at full size: the PNG is at ${Math.round(scale * 100)}%, the SVG has it whole` : null);
    } catch {
      setNote('the browser could not draw the PNG; the SVG export has the same tree');
    }
  };
  const fileBase = () => `tree-${(name?.key ?? u.sid ?? 'session').replace(/[^\w.-]+/g, '-').replace(/-+/g, '-')}`;

  return (
    <div className="tp">
      <header className="tp-head">
        <div className="tp-title">
          <span className="eyebrow">process tree</span>
          <h1>{name ? name.key : (u.sid ?? 'no session')}</h1>
          {name?.date && <span className="tp-sub">{name.date}</span>}
          {d?.row && <span className="tp-sub">{`${d.row.user} · ${d.row.host}`}</span>}
          {scan && (
            <span className="tp-sub" data-tip={scan.stopped ? `the read stopped at its ${scan.stopped} budget` : undefined}>
              {`read ${stamp(scan.first ?? scan.from)} → ${stamp(scan.last ?? scan.to)} · ${fint(scan.records)} records${scan.stopped ? ' · stopped at its budget' : ''}`}
            </span>
          )}
          <span className="tp-sp" />
          <button className="btn" onClick={() => setTo(Math.ceil(serverNow() / 1000))} disabled={q.loading} data-tip="read the session again, up to now">
            {q.loading && d ? 'reading…' : 'read again'}
          </button>
          <a className="btn" href={dashboardHref(u)} data-tip="this session in the dashboard's inspector">
            open in the dashboard
          </a>
        </div>
        <div className="tp-tools">
          <span className="tp-grp">
            <label data-tip="take out runs of the same command and the site's routine commands (ui.routine_commands), so the unusual processes stand out">
              <input type="checkbox" checked={hide} onChange={(e) => setHide(e.target.checked)} />
              hide routine
            </label>
            {hide && tree && <span className="nm">{`${fint(hiddenCount(tree, rules))} hidden`}</span>}
          </span>
          <span className="tp-grp">
            <button className="btn" onClick={() => tree && setOpen(allOpen(tree))} disabled={!tree} data-tip="open every process the read served">
              expand all
            </button>
            <button className="btn" onClick={() => setOpen(new Set(['r']))} disabled={!tree}>
              collapse
            </button>
          </span>
          <span className="tp-grp tp-find">
            <input
              ref={findRef}
              type="search"
              placeholder="find a command, pid, or path"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHi(-1);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  step(e.shiftKey ? -1 : 1);
                } else if (e.key === 'Escape') {
                  setQuery('');
                  (e.target as HTMLInputElement).blur();
                }
              }}
              aria-label="find"
            />
            <button className="btn" onClick={() => step(-1)} disabled={!matches.length} aria-label="previous match">
              ‹
            </button>
            <button className="btn" onClick={() => step(1)} disabled={!matches.length} aria-label="next match">
              ›
            </button>
            <span className="nm">
              {query.trim() ? (matches.length ? `${hi >= 0 ? fint(hi + 1) : DASH} of ${fint(matches.length)}` : 'no match') : ''}
            </span>
          </span>
          <span className="tp-grp" data-tip={'drag to pan · wheel to scroll · Ctrl+wheel or pinch to zoom\n+ and − zoom · 0 is 100% · f fits the tree\nclick a box for its details · Enter, a double-click or its caret opens it\narrows move between boxes · Ctrl+F finds'}>
            <button className="btn" onClick={() => zoomAt(0.8)} aria-label="zoom out">
              −
            </button>
            <span className="tp-k mono">{`${Math.round(view.k * 100)}%`}</span>
            <button className="btn" onClick={() => zoomAt(1.25)} aria-label="zoom in">
              +
            </button>
            <button className="btn" onClick={fit} disabled={!tree}>
              fit
            </button>
            <button className="btn" onClick={() => zoomAt(1 / view.k)}>
              100%
            </button>
          </span>
          <span className="tp-grp">
            <label>
              <input type="checkbox" checked={mini} onChange={(e) => setMini(e.target.checked)} />
              minimap
            </label>
          </span>
          <span className="tp-grp">
            <button className="btn" onClick={exportSvg} disabled={!tree} data-tip="the tree as drawn now, as an SVG file">
              SVG
            </button>
            <button className="btn" onClick={exportPng} disabled={!tree} data-tip="the tree as drawn now, as a PNG image">
              PNG
            </button>
          </span>
          {note && <span className="tp-note">{note}</span>}
        </div>
      </header>
      <div className="tp-body">
        <div
          ref={vp}
          className={`tp-view${panning ? ' drag' : ''}`}
          style={grid >= 6 ? { backgroundSize: `${grid}px ${grid}px`, backgroundPosition: `${view.x}px ${view.y}px` } : { backgroundImage: 'none' }}
          onPointerDown={(e) => {
            if ((e.button !== 0 && e.button !== 1) || (e.target as Element).closest('button, input, a, .tp-mini')) return;
            if (e.button === 1) e.preventDefault();
            drag.current = { id: e.pointerId, x0: e.clientX, y0: e.clientY, vx: view.x, vy: view.y, moved: false };
            dragged.current = false;
          }}
          onPointerMove={(e) => {
            const g = drag.current;
            if (!g || g.id !== e.pointerId) return;
            const dx = e.clientX - g.x0;
            const dy = e.clientY - g.y0;
            if (!g.moved) {
              if (Math.hypot(dx, dy) < 4) return;
              g.moved = true;
              dragged.current = true;
              setPanning(true);
              vp.current?.setPointerCapture(e.pointerId);
            }
            setView((v) => ({ ...v, x: g.vx + dx, y: g.vy + dy }));
          }}
          onPointerUp={(e) => {
            const g = drag.current;
            if (!g || g.id !== e.pointerId) return;
            drag.current = null;
            if (g.moved) {
              setPanning(false);
              if (vp.current?.hasPointerCapture(e.pointerId)) vp.current.releasePointerCapture(e.pointerId);
            }
          }}
          onPointerCancel={() => {
            drag.current = null;
            setPanning(false);
          }}
          onClick={(e) => {
            if (!dragged.current && !(e.target as Element).closest('.tnode, .tp-mini')) setSel(null);
          }}
        >
          <div className="tp-xf" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
            {canvas}
          </div>
          {status && <div className="tp-msg">{status}</div>}
          {mini && tree && geo.boxes.length > 0 && (
            <TreeMinimap geo={geo} view={view} size={size} sel={sel} hits={hits} go={(x, y) => centreOn(x, y)} />
          )}
        </div>
        {selNode && (
          <Details
            n={selNode}
            open={open.has(selNode.id)}
            toggle={() => toggle(selNode.id)}
            find={(s) => {
              setQuery(s);
              setHi(-1);
            }}
            close={() => setSel(null)}
          />
        )}
      </div>
      <TooltipLayer />
    </div>
  );
}
