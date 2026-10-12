/* One session's processes as a tree: who started what, when, for how long.
 *
 * The tree is the backend's (`rc_dashboard/proctree.py`); this only lays it
 * out. Three things it is careful about:
 *
 * - A parent link is shown as the evidence it is. Most rows hang from their
 *   own record's ppid; a row placed by the process index, by the collector's
 *   depth, or by the fold alone carries a dashed tag that says so, because a
 *   tree that draws an inference like a measurement is a fabricated tree.
 * - A process with no exit record in the read (the agent itself, still
 *   running) has no exit code and no cpu: those cells are a dash, never 0.
 * - Counted is not drawn. A group's members past the first few, the processes
 *   past the hold budget, and the rows past the served budget are rows of
 *   their own with their counts, so the totals up the tree still add up.
 *
 * The page navigates it by time (TrajectoryNav.tsx). `locate` finds the row a
 * moment lands on -- the first process started at or after it, or, when that
 * process is only counted, the row that counts it -- and `ancestors` the rows
 * to open to show it. A zoomed time range re-scales the timeline column and
 * marks the rows that ran inside it.
 */
import { memo, useLayoutEffect, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';
import type { ProcCount, ProcEntry, ProcGroup, ProcLink, ProcNode } from '../api/types';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, clock, dur, fint, fmt } from '../lib/format';

/** Group members past the itemised few: in the group's totals, not drawn. */
interface Rest {
  k: 'rest';
  more: number;
  of: ProcGroup;
}
type Item = ProcEntry | Rest;

export interface Row {
  id: string;
  depth: number;
  /** per ancestor level below the root: whether its line continues past this row */
  guides: boolean[];
  last: boolean;
  e: Item;
  kids: number;
}

const ROLE_TAG: Record<string, string> = {
  'tool-shell': 'tool call',
  sandbox: 'sandbox',
  'embedded-tool': 'embedded',
  probe: 'probe',
  helper: 'helper',
  infra: 'infra',
  client: 'client',
  ide: 'ide',
};

export const LINK_LBL: Record<ProcLink, string> = {
  ppid: 'its own ppid',
  index: 'parent from index',
  depth: 'placed by depth',
  fold: 'no process link',
};

export const LINK_TIP: Record<ProcLink, string> = {
  ppid: "its own record names this parent, and the parent's exit came after it",
  index:
    "its parent left no exit record in this read; the window's process index (or the agent's residency ticks) names it",
  depth:
    "no record read names its parent; the collector's depth below its session key places it there",
  fold: 'nothing read links it to a process here; it hangs from the agent because the session fold put its key there',
};

const SIG: Record<string, string> = {
  '1': 'SIGHUP',
  '2': 'SIGINT',
  '3': 'SIGQUIT',
  '6': 'SIGABRT',
  '7': 'SIGBUS',
  '9': 'SIGKILL',
  '11': 'SIGSEGV',
  '13': 'SIGPIPE',
  '14': 'SIGALRM',
  '15': 'SIGTERM',
};
const sigName = (s: number | string) => SIG[String(s)] ?? `signal ${s}`;

function children(e: Item): Item[] {
  if (e.k === 'p') return e.kids ?? [];
  if (e.k === 'g') return e.more > 0 ? [...e.items, { k: 'rest', more: e.more, of: e }] : e.items;
  return [];
}

export function flatten(root: ProcNode, open: Set<string>): Row[] {
  const out: Row[] = [];
  const walk = (e: Item, id: string, depth: number, guides: boolean[], last: boolean) => {
    const ch = children(e);
    out.push({ id, depth, guides, last, e, kids: ch.length });
    if (!ch.length || !open.has(id)) return;
    const g = depth === 0 ? [] : [...guides, !last];
    ch.forEach((c, i) => walk(c, `${id}.${i}`, depth + 1, g, i === ch.length - 1));
  };
  walk(root, 'r', 0, [], true);
  return out;
}

/* ---------------------------------------------------------- finding a time */

/** Every served entry with the id `flatten` gives it, open or not, in order. */
export interface Spot {
  id: string;
  e: Item;
}

export function spots(root: ProcNode): Spot[] {
  const out: Spot[] = [];
  const walk = (e: Item, id: string) => {
    out.push({ id, e });
    children(e).forEach((c, i) => walk(c, `${id}.${i}`));
  };
  walk(root, 'r');
  return out;
}

/** What to find. `by` says what `t` is of the process looked for: when it
 *  started (a moment on the strip, a quick jump) or when it exited (a ribbon
 *  run, whose time is its first record's, and a record is written at exit). */
export interface JumpReq {
  t: number;
  by: 'start' | 'end';
  /** the process itself, when the navigator named it */
  pid?: number | null;
  /** a ribbon run's: the purpose, or the tool, its first record had */
  purpose?: string;
  tool?: string;
}

export interface Found {
  id: string;
  /** the process itself, rather than the row nearest it */
  exact: boolean;
  kind: 'p' | 'rest' | 'agg' | 'more';
}

/** The span of times a counted row's processes `by`, and how many there are. */
function spanOf(e: Item, by: 'start' | 'end'): [number | null, number | null, number] {
  if (e.k === 'rest') {
    const g = e.of;
    const last = g.items[g.items.length - 1];
    const a = (last ? (by === 'start' ? last.start : last.end) : null) ?? g.start;
    const b = by === 'start' ? (g.last ?? g.end) : g.end;
    return [a, b, e.more];
  }
  if (e.k === 'agg' || e.k === 'more') return [e.start, e.end, e.n];
  return [null, null, 0];
}

/** The row a moment lands on.
 *
 * The process itself when it is drawn: by pid and time for a process the
 * navigator named, by exit time and purpose or tool for a ribbon run. Else the
 * first drawn process to start (or exit) at or after `t` -- unless a counted
 * row is likelier to hold it: a row counting `n` processes over a span that
 * contains `t` is scored by the wait to its next one were they spread evenly,
 * half the span over `n`, so a dense poll loop wins over a row a minute away
 * and a sparse one does not. Ties go to the earlier row in the tree, so a
 * tool-call shell comes before the command it ran.
 */
export function locate(list: Spot[], q: JumpReq): Found | null {
  const EPS = 0.0015;
  const key = (e: ProcNode) => (q.by === 'end' ? e.end : e.start);
  if (q.pid != null || q.purpose != null || q.tool != null) {
    for (const s of list) {
      const e = s.e;
      if (e.k !== 'p') continue;
      const v = key(e);
      if (v == null || Math.abs(v - q.t) > EPS) continue;
      if (q.pid != null && e.pid !== q.pid) continue;
      if (q.purpose != null && (e.purpose ?? 'other') !== q.purpose) continue;
      if (q.tool != null && (e.eff ?? e.cmd ?? '(empty)') !== q.tool) continue;
      return { id: s.id, exact: true, kind: 'p' };
    }
  }
  let best: Found | null = null;
  let score = Infinity;
  let last: Spot | null = null;
  for (const s of list) {
    const e = s.e;
    let sc: number | null = null;
    if (e.k === 'p') {
      const v = key(e);
      if (v != null && v >= q.t - EPS) sc = Math.max(0, v - q.t);
      if (e.start != null && (last == null || (last.e as ProcNode).start! <= e.start)) last = s;
    } else if (e.k === 'rest' || e.k === 'agg' || e.k === 'more') {
      const [a, b, n] = spanOf(e, q.by);
      if (a != null && b != null) {
        if (q.t <= a) sc = a - q.t;
        else if (q.t <= b) sc = (b - a) / Math.max(1, n) / 2;
      }
    }
    if (sc != null && sc < score) {
      score = sc;
      best = { id: s.id, exact: false, kind: e.k === 'p' ? 'p' : (e.k as Found['kind']) };
    }
  }
  if (best) return best;
  return last ? { id: last.id, exact: false, kind: 'p' } : null;
}

/** The rows to open so that row `id` is drawn. */
export function ancestors(id: string): string[] {
  const parts = id.split('.');
  return parts.slice(1).map((_, i) => parts.slice(0, i + 1).join('.'));
}

/** Every expandable row: what "expand all" opens. */
export function allOpen(root: ProcNode): Set<string> {
  const open = new Set<string>();
  const walk = (e: Item, id: string) => {
    const ch = children(e);
    if (!ch.length) return;
    open.add(id);
    ch.forEach((c, i) => walk(c, `${id}.${i}`));
  };
  walk(root, 'r');
  return open;
}

/** Open level by level while the page stays readable; groups stay closed,
 *  because the count IS the point of a group. The root's own children are
 *  always shown -- they are the trajectory -- and `extra` more rows are spent
 *  opening the first of them, so the nesting is visible without a click. */
export function defaultOpen(root: ProcNode, extra = 160): Set<string> {
  const open = new Set(['r']);
  let rows = 1 + children(root).length;
  const budget = Math.max(220, rows + extra);
  let level: [Item, string][] = children(root).map((c, i) => [c, `r.${i}`]);
  while (level.length) {
    const next: [Item, string][] = [];
    for (const [e, id] of level) {
      if (e.k !== 'p') continue;
      const ch = children(e);
      // A wide node opened by default would bury its siblings: one click away
      // is better than a screen of its children between the rows around it.
      if (!ch.length || ch.length > 24 || rows + ch.length > budget) continue;
      open.add(id);
      rows += ch.length;
      ch.forEach((c, i) => next.push([c, `${id}.${i}`]));
    }
    level = next;
  }
  return open;
}

/* ------------------------------------------------------------------ cells */

const shortPath = (p: string) => {
  const parts = p.replace(/^\/home\/[^/]+/, '~').split('/');
  return parts.length > 3 ? '…/' + parts.slice(-2).join('/') : parts.join('/');
};

/** hh:mm:ss, with the date when it is not the read's own day. */
function when(t: number | null | undefined, lo: number | null): string {
  if (t == null) return DASH;
  if (lo != null && Math.abs(t - lo) > 20 * 3600) {
    const d = new Date(t * 1000);
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${clock(t).slice(0, 5)}`;
  }
  return clock(t);
}

function stamp(t: number | null | undefined): string {
  if (t == null) return DASH;
  const d = new Date(t * 1000);
  return `${d.toLocaleDateString()} ${clock(t)}`;
}

function procTip(n: ProcNode): string {
  return [
    `pid ${n.pid ?? DASH}${n.ppid != null ? ' · ppid ' + n.ppid : ''}${n.user ? ' · ' + n.user : ''}`,
    n.cmdline ? `ran  ${n.cmdline}` : null,
    n.args ? `argv ${n.args}` : null,
    n.cwd ? `cwd  ${n.cwd}` : null,
    n.role ? `role ${n.role}` : null,
    n.purpose ? `purpose ${n.purpose}` : null,
    `started ${stamp(n.start)}`,
    n.ev === 'exit'
      ? `exited ${stamp(n.end)}`
      : n.ev === 'truncated'
        ? `alive when the collector stopped, ${stamp(n.end)}`
        : `no exit record in this read${n.end != null ? '; last seen ' + stamp(n.end) : ''}`,
    n.link ? `parent: ${LINK_TIP[n.link]}` : null,
    n.gap ? `${n.gap} level(s) between it and that parent were never recorded` : null,
    n.orphan ? 'its record names init: it outlived its parent and was reparented' : null,
    n.cpu_nm ? `cpu not measured on ${fint(n.cpu_nm)} process(es) below` : null,
  ]
    .filter(Boolean)
    .join('\n');
}

function exitCell(n: ProcNode): ReactNode {
  if (n.ev === 'truncated') return <span className="pt-x ok">alive</span>;
  if (n.ev !== 'exit') return <span className="pt-x ok">{DASH}</span>;
  if (n.signal != null)
    return (
      <span className="sev HIGH">
        {sigName(n.signal)}
        {n.core ? ' core' : ''}
      </span>
    );
  if (n.exit_code == null) return <span className="pt-x ok">{DASH}</span>;
  if (n.exit_code === 0) return <span className="pt-x ok">0</span>;
  return <span className="sev med">{n.exit_code}</span>;
}

function groupExit(g: ProcGroup): ReactNode {
  const sigs = Object.entries(g.signals ?? {}).sort((a, b) => b[1] - a[1]);
  const bad = Object.entries(g.exits ?? {})
    .filter(([c]) => c !== '0')
    .sort((a, b) => b[1] - a[1]);
  if (sigs.length) return <span className="sev HIGH">{`${sigName(sigs[0][0])} ×${fint(sigs[0][1])}`}</span>;
  if (bad.length) return <span className="sev med">{`${bad[0][0]} ×${fint(bad[0][1])}`}</span>;
  if (g.exits?.['0']) return <span className="pt-x ok">0</span>;
  return <span className="pt-x ok">{DASH}</span>;
}

function groupExitTip(g: ProcGroup): string {
  const lines = [
    ...Object.entries(g.exits ?? {}).map(([c, k]) => `exit ${c}: ${fint(k)}`),
    ...Object.entries(g.signals ?? {}).map(([s, k]) => `${sigName(s)}: ${fint(k)}`),
  ];
  if (g.alive) lines.push(`alive at collector stop: ${fint(g.alive)}`);
  return lines.join('\n');
}

interface Span {
  lo: number;
  hi: number;
}

function Bar({ t0, t1, cls, tl, tip }: { t0: number | null; t1: number | null; cls: string; tl: Span | null; tip?: string }) {
  if (!tl || (t0 == null && t1 == null)) return null;
  const w = Math.max(tl.hi - tl.lo, 1e-6);
  const a = t0 ?? tl.lo;
  const b = t1 ?? tl.hi;
  const left = Math.max(0, Math.min(1, (a - tl.lo) / w));
  const right = Math.max(left, Math.min(1, (b - tl.lo) / w));
  const clip = a < tl.lo - 0.5;
  const clipR = b > tl.hi + 0.5;
  return (
    <i
      className={`pt-bar ${cls}${clip ? ' clip' : ''}${clipR ? ' clipr' : ''}`}
      style={{ left: `${(left * 100).toFixed(3)}%`, width: `${((right - left) * 100).toFixed(3)}%` }}
      data-tip={tip}
    />
  );
}

/* -------------------------------------------------------------------- row */

/** When a row's processes ran, for marking the rows inside a zoomed range. */
function ranOf(e: Item): [number | null, number | null] {
  if (e.k === 'p') return [e.start, e.end ?? e.start];
  if (e.k === 'rest') return [spanOf(e, 'start')[0], e.of.end];
  return [e.start, e.end];
}

const RowView = memo(function RowView({
  r,
  open,
  toggle,
  tl,
  zoomed,
  flash,
}: {
  r: Row;
  open: boolean;
  toggle: (id: string) => void;
  tl: Span | null;
  /** a zoomed range is on: mark whether this row ran inside it */
  zoomed: boolean;
  flash: boolean;
}) {
  const e = r.e;
  const lo = tl?.lo ?? null;
  const tog = r.kids ? (
    <button
      className="pt-tog"
      aria-label={`${open ? 'collapse' : 'expand'} ${r.kids} row${r.kids === 1 ? '' : 's'}`}
    >
      {open ? '▾' : '▸'}
    </button>
  ) : (
    <span className="pt-tog leaf">·</span>
  );

  let body: ReactNode;
  let tip: string | undefined;
  let cells: ReactNode[];
  let rowCls = '';

  if (e.k === 'p') {
    const isAgent = e.role === 'agent' || r.depth === 0;
    const text = e.cmdline ?? (e.args && e.args !== e.cmd ? e.args : null);
    tip = procTip(e);
    rowCls = r.depth === 0 ? ' root' : '';
    body = (
      <>
        {isAgent && e.actor3 && (
          <span className="dot" style={{ background: CLS_COL[e.actor3] ?? 'var(--sg)' }} data-tip={CLS_LBL[e.actor3] ?? e.actor3} />
        )}
        {isAgent && e.agent_type && <span className="pt-tag type">{e.agent_type}</span>}
        {e.role && ROLE_TAG[e.role] && <span className="pt-tag">{ROLE_TAG[e.role]}</span>}
        <span className={`pt-cmd${e.cmd ? '' : ' anon'}`}>{e.cmd ?? `pid ${e.pid ?? '?'}`}</span>
        {e.eff && <span className="pt-arg">{`→ ${e.eff}`}</span>}
        {e.ev == null && r.depth >= 0 && e.pid != null && (
          <span className="pt-tag noexit" data-tip={tip}>
            no exit in read
          </span>
        )}
        {e.ev === 'truncated' && <span className="pt-tag noexit">alive at collector stop</span>}
        {e.link && e.link !== 'ppid' && (
          <span className="pt-tag weak" data-tip={LINK_TIP[e.link]}>
            {LINK_LBL[e.link]}
            {e.gap ? ` · ${e.gap} level${e.gap === 1 ? '' : 's'} unrecorded` : ''}
          </span>
        )}
        {e.orphan && (
          <span className="pt-tag weak" data-tip="its record names init: it outlived its parent and was reparented">
            reparented
          </span>
        )}
        {e.cwd && r.depth > 0 && (
          <span className="pt-tag" data-tip={`cwd ${e.cwd}`}>
            {`cwd ${shortPath(e.cwd)}`}
          </span>
        )}
        {text && <span className="pt-arg">{text}</span>}
      </>
    );
    const alive = e.ev == null && e.start != null && e.end != null;
    cells = [
      when(e.start, lo),
      <span key="d" className={alive ? 'dim' : undefined} data-tip={alive ? 'no exit: alive at least this long' : undefined}>
        {e.dur != null ? dur(e.dur) : alive ? `≥ ${dur((e.end as number) - (e.start as number))}` : DASH}
      </span>,
      e.ev ? fmt(e.cpu) : DASH,
      fmt(e.sub_cpu),
      fint(e.n),
      exitCell(e),
    ];
    const barCls =
      e.ev == null ? 'open' : e.signal != null ? 'sig' : e.exit_code ? 'bad' : '';
    cells.push(<Bar key="b" t0={e.start} t1={e.ev == null ? (e.end ?? null) : e.end} cls={barCls} tl={tl} />);
  } else if (e.k === 'g') {
    const sample = e.samples[0];
    const sampleText = sample ? sample.cmdline ?? sample.args : null;
    tip = [
      `${fint(e.count)} consecutive siblings of one shape${e.period > 1 ? ` — a ${e.period}-step cycle repeated ${fint(e.reps)} times` : ''}`,
      `${fint(e.n)} processes in them and below them`,
      e.dur_max != null ? `longest single run ${dur(e.dur_max)}` : null,
      e.distinct > 1 ? `${fint(e.distinct)} distinct command lines, most common:` : 'command line:',
      ...e.samples.map((s) => `  ${fint(s.n)} × ${s.cmdline ?? s.args ?? ''}`),
    ]
      .filter(Boolean)
      .join('\n');
    body = (
      <>
        <span className="pt-count">{`×${fint(e.reps)}`}</span>
        {/* `›` is parent › child in a label, so a cycle's steps are listed with commas */}
        <span className="pt-cmd">{e.period > 1 ? `↻ ${e.motif.join(', ')}` : e.motif[0]}</span>
        {e.distinct > 1 && <span className="pt-tag">{`${fint(e.distinct)} distinct`}</span>}
        {sampleText && <span className="pt-arg">{sampleText}</span>}
      </>
    );
    cells = [
      when(e.start, lo),
      <span key="d" data-tip="first start to last exit of the run">
        {e.start != null && e.end != null ? dur(e.end - e.start) : DASH}
      </span>,
      fmt(e.cpu),
      fmt(e.sub_cpu),
      fint(e.n),
      <span key="x" data-tip={groupExitTip(e)}>
        {groupExit(e)}
      </span>,
      <Bar key="b" t0={e.start} t1={e.end} cls="group" tl={tl} />,
    ];
  } else if (e.k === 'rest') {
    rowCls = ' count';
    body = <span>{`… ${fint(e.more)} more like these, in the group's totals`}</span>;
    tip = 'members of the group past the first few: counted in the group row above, not drawn one by one';
    cells = ['', '', '', '', '', '', ''];
  } else {
    const c = e as ProcCount;
    rowCls = ' count';
    const cmds = c.cmds
      .slice(0, 4)
      .map(([k, n]) => `${k} ×${fint(n)}`)
      .join(', ');
    body =
      c.k === 'agg' ? (
        <span>{`⋯ ${fint(c.n)} process${c.n === 1 ? '' : 'es'} counted, not itemised · ${cmds}`}</span>
      ) : (
        <span>{`⋯ ${fint(c.count ?? 0)} more sibling${c.count === 1 ? '' : 's'} past the row budget (${fint(c.n)} processes) · ${cmds}`}</span>
      );
    tip =
      c.k === 'agg'
        ? "past the inspector's hold budget: counted with their CPU and time span under this parent, but not drawn one by one"
        : 'past the number of rows the inspector serves, which it allots breadth first: counted here, deepest levels first to go';
    cells = [
      when(c.start, lo),
      <span key="d">{c.start != null && c.end != null ? dur(c.end - c.start) : DASH}</span>,
      DASH,
      fmt(c.sub_cpu),
      fint(c.n),
      '',
      <Bar key="b" t0={c.start} t1={c.end} cls="group" tl={tl} />,
    ];
  }

  if (zoomed && tl) {
    const [a, b] = ranOf(e);
    const inside = a != null && a <= tl.hi && (b ?? a) >= tl.lo;
    rowCls += inside ? ' inz' : ' outz';
  }
  if (flash) rowCls += ' flash';
  return (
    <div
      className={`pt-row${rowCls}`}
      role="treeitem"
      aria-level={r.depth + 1}
      aria-expanded={r.kids ? open : undefined}
      aria-current={flash ? 'location' : undefined}
      data-row={r.id}
    >
      <div className="pt-tree">
        {r.guides.map((on, i) => (
          <span key={i} className={`pt-g${on ? ' on' : ''}`} />
        ))}
        {r.depth > 0 && <span className={`pt-j${r.last ? ' last' : ''}`} />}
        <div
          className="pt-body"
          data-tip={tip}
          onClick={r.kids ? () => toggle(r.id) : undefined}
          style={r.kids ? { cursor: 'pointer' } : undefined}
        >
          {tog}
          {body}
        </div>
      </div>
      {cells.slice(0, 6).map((c, i) => (
        <div key={i} className={`num${i === 0 || i === 4 ? ' dim' : ''}`}>
          {c}
        </div>
      ))}
      <div className="pt-tl">{cells[6]}</div>
    </div>
  );
});

export function ProcTree({
  root,
  open,
  setOpen,
  timeline,
  view,
  flash,
}: {
  root: ProcNode;
  open: Set<string>;
  setOpen: (s: Set<string>) => void;
  timeline: { lo: number | null; hi: number | null } | null | undefined;
  /** a zoomed time range: the timeline column is drawn on it, not the read's */
  view?: [number, number] | null;
  /** the row a jump landed on, and a count that changes on every jump */
  flash?: { id: string; n: number } | null;
}) {
  const rows = useMemo(() => flatten(root, open), [root, open]);
  const full =
    timeline?.lo != null && timeline?.hi != null && timeline.hi >= timeline.lo
      ? { lo: timeline.lo, hi: timeline.hi }
      : null;
  const tl = view ? { lo: view[0], hi: view[1] } : full;
  const toggle = (id: string) => {
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };
  // A jump scrolls the tree, not the page, to the row it landed on: the strip
  // that was clicked stays where it was. The page moves only when the tree
  // itself is out of sight.
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el || !flash) return;
    const row = el.querySelector(`[data-row="${flash.id}"]`);
    if (!row) return;
    const r = row.getBoundingClientRect();
    const b = el.getBoundingClientRect();
    el.scrollTop += r.top - b.top - el.clientHeight / 2 + r.height / 2;
    if (b.bottom < 0 || b.top > window.innerHeight) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [flash]);
  return (
    <div className="pt" role="tree" aria-label="process tree" ref={box}>
      <div className="pt-in">
        <div className="pt-head">
          <div>process · what it ran</div>
          <div className="num">started</div>
          <div className="num">took</div>
          <div className="num" data-tip="the process's own cpu_s">
            cpu s
          </div>
          <div className="num" data-tip="own cpu_s plus everything below it; child_cpu_s is never added">
            Σ cpu s
          </div>
          <div className="num" data-tip="processes in the subtree, itself included">
            procs
          </div>
          <div className="num">exit</div>
          <div className={`pt-tlhead${view ? ' zoomed' : ''}`} data-tip={view ? 'zoomed: the bars are drawn on the range picked on the strip above' : undefined}>
            <span>{tl ? clock(tl.lo) : 'timeline'}</span>
            <span>{tl ? clock(tl.hi) : ''}</span>
          </div>
        </div>
        {rows.map((r) => {
          const on = flash?.id === r.id;
          return (
            <RowView
              // remounted on each jump to it, so the highlight plays again
              key={on ? `${r.id}#${flash?.n}` : r.id}
              r={r}
              open={open.has(r.id)}
              toggle={toggle}
              tl={tl}
              zoomed={!!view}
              flash={on}
            />
          );
        })}
      </div>
    </div>
  );
}
