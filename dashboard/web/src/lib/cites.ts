/* Citations in an Ask LLM answer (rc_dashboard/ask.py: CITE_GUIDE).
 *
 * The model puts the ref of what it took a figure from after the figure,
 * `[b7]`, and the job says what each cited ref was: where it sits in the
 * snapshot (`path`), what the model saw of it (`data`), and the hints that
 * open it (`go`) -- a session, or a moment of the session asked about. Here a
 * ref becomes a place on the page: the tab, the panel, the row to light up,
 * and a link that opens it in this tab or a new one, over the window and
 * filter the question was read over (`view`).
 *
 * A panel is found by its anchor (Panel.tsx: `id ?? slug(title)`), and a row
 * in it by the text that names it -- a user, a host, a command -- so a table
 * needs no wiring to be cited. A row the page does not draw as a row (a
 * chart's bar) lands on its panel; a part no panel draws lands on its tab.
 */
import type { AskCite, AskView, CiteJump, Cls } from '../api/types';
import { CLS } from './classes';
import { parseUrl, urlQuery } from './filters';
import type { UrlState } from './filters';

/** One bracket of refs, as the backend reads them (`ask.CITE_RE`). */
const REF = '[a-z]{1,2}\\d{1,5}';
export const CITE_RE = new RegExp(`\\[(${REF}(?:\\s*[,;]\\s*${REF})*)\\]`, 'g');
const ONE_REF = new RegExp(REF, 'g');

/** The refs of one bracket's text: `b7, c2` -> [b7, c2]. */
export const refsOf = (inner: string): string[] => inner.match(ONE_REF) ?? [];

/** The refs some texts cite, in the order they are first cited: a source's number. */
export function citeOrder(texts: (string | null | undefined)[]): string[] {
  const out: string[] = [];
  for (const t of texts) {
    for (const m of (t ?? '').matchAll(CITE_RE)) {
      for (const r of refsOf(m[1])) if (!out.includes(r)) out.push(r);
    }
  }
  return out;
}

/** A text without its citations: a follow-up question about an answer must
 *  not hand the model refs from a snapshot it no longer has. */
export const stripCites = (s: string) => s.replace(new RegExp(`\\s*${CITE_RE.source}`, 'g'), '');

/* ------------------------------------------------------------- places */

interface Place {
  tab: string;
  /** the panel's title as the page shows it; none: the tab itself */
  panel?: string;
  /** its anchor, when it is not the slug of `panel` */
  anchor?: string;
  /** a title the site words itself (`ui.node_noun`): matched, not slugged */
  titled?: string;
}

/** As App.tsx's TABS names them. */
const TAB_LBL: Record<string, string> = {
  live: 'Live',
  overview: 'Overview',
  agents: 'Agents',
  trajectories: 'Trajectories',
  resources: 'Resources',
  risk: 'Risk & Warning',
  slurm: 'Slurm',
  internal: 'Internal',
};

const INSPECTOR = 'Session inspector — process tree';

/** Where each part of a snapshot is drawn: the longest matching prefix of a
 *  ref's path wins. The risk sections are the site's own (titled by its rules),
 *  so their rows are found on the tab rather than in a named panel. */
const PLACES: [string, Place][] = [
  ['panels.tool_mix.tools', { tab: 'agents', panel: 'Top tools', anchor: 'sec-head-of-the-tool-distribution' }],
  ['panels.tool_mix.head', { tab: 'agents', panel: 'Top tools', anchor: 'sec-head-of-the-tool-distribution' }],
  ['panels.tool_mix.n_tools', { tab: 'agents', panel: 'Top tools', anchor: 'sec-head-of-the-tool-distribution' }],
  ['panels.tool_mix', { tab: 'agents', panel: 'Tool mix', anchor: 'sec-tool-mix' }],
  ['panels.io_process.by_user', { tab: 'resources', panel: 'I/O by user' }],
  ['panels.io_process.by_tool', { tab: 'resources', panel: 'Reads by tool' }],
  ['panels.io_process', { tab: 'resources', panel: 'I/O distributions' }],
  ['panels.risk.rules', { tab: 'risk', panel: 'Rules in effect' }],
  ['panels.risk.sections', { tab: 'risk', panel: 'Rules in effect' }],
  ['panels.risk.totals', { tab: 'risk', panel: 'Rules in effect' }],
  ['panels.risk', { tab: 'risk' }],
  ['risk_who', { tab: 'risk' }],
  ['panels.resources.worst_stall', { tab: 'resources', panel: 'Worst individual processes' }],
  ['panels.resources.worst_fault', { tab: 'resources', panel: 'Worst individual processes' }],
  ['panels.resources.worst_wait', { tab: 'resources', panel: 'Worst individual processes' }],
  ['panels.resources.stall_by_tool', { tab: 'resources', panel: 'Faults, delays, and D-state' }],
  ['panels.resources.impossible_waits', { tab: 'resources', panel: 'Faults, delays, and D-state' }],
  ['panels.resources', { tab: 'resources', panel: 'Per-process distributions', anchor: 'sec-per-event-distributions' }],
  ['panels.sandbox.attempts', { tab: 'internal', panel: 'Sandbox wrapper calls, by outcome' }],
  ['panels.sandbox.tool_calls', { tab: 'internal', panel: 'Tool calls — did they run namespace-isolated?' }],
  ['panels.sandbox.launch_modes', { tab: 'internal', panel: 'Approval at launch — an independent axis' }],
  ['panels.sandbox.approval_by_class_events', { tab: 'internal', panel: 'Approval at launch — an independent axis' }],
  ['panels.sandbox.approval_by_class_sessions', { tab: 'internal', panel: 'Approval at launch — an independent axis' }],
  ['panels.sandbox.ancestry_matrix', { tab: 'internal', panel: 'Ancestry × kind × verdict' }],
  ['panels.sandbox.sandbox_src', { tab: 'internal', panel: 'Where the per-process verdict came from' }],
  ['panels.sandbox.sandbox_detail', { tab: 'internal', panel: 'Where the per-process verdict came from' }],
  ['panels.sandbox', { tab: 'internal', panel: 'Namespace isolation — per process and per session' }],
  ['panels.slurm.sessions', { tab: 'slurm', panel: 'Sessions at the scheduler' }],
  ['panels.slurm.rejections', { tab: 'slurm', panel: 'Submissions the scheduler refused' }],
  ['panels.slurm', { tab: 'slurm', panel: 'How each class uses the scheduler' }],
  ['panels.cascades', { tab: 'agents', panel: 'Agents and their subagents', anchor: 'sec-agent-trees' }],
  ['panels.workspace', { tab: 'agents', panel: 'Agents and their subagents', anchor: 'sec-agent-trees' }],
  ['panels.node', { tab: 'live' }],
  ['live_now.users', { tab: 'live', panel: 'Active users', anchor: 'live-users' }],
  ['live_now.hosts', { tab: 'live', panel: 'Login nodes right now', titled: 'right now' }],
  ['live_now.submits', { tab: 'slurm', panel: 'Latest submissions' }],
  ['live_now', { tab: 'live' }],
  ['facets.users', { tab: 'live', panel: 'Active users', anchor: 'live-users' }],
  ['facets', { tab: 'overview' }],
  ['sessions', { tab: 'trajectories', panel: 'Sessions', anchor: 'sec-ranked-sessions' }],
  // one session's digest (ask.session_digest): the inspector it was asked from
  ['session', { tab: 'trajectories', panel: INSPECTOR }],
  ['tools', { tab: 'trajectories', panel: INSPECTOR }],
  ['purposes', { tab: 'trajectories', panel: INSPECTOR }],
  ['landmarks', { tab: 'trajectories', panel: INSPECTOR }],
  ['tree', { tab: 'trajectories', panel: INSPECTOR }],
  ['read', { tab: 'trajectories', panel: INSPECTOR }],
];

/** Panel.tsx's anchor for a title. */
const slug = (s: string) => 'sec-' + s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

function placeOf(path: (string | number)[]): [Place, (string | number)[]] | null {
  const dotted = path.join('.');
  let best: [string, Place] | null = null;
  for (const e of PLACES) {
    const [k] = e;
    if ((dotted === k || dotted.startsWith(k + '.')) && (!best || k.length > best[0].length)) best = e;
  }
  return best ? [best[1], path.slice(best[0].split('.').length)] : null;
}

/* ------------------------------------------------------------ targets */

/** Where a citation goes, and how it is found once the page is there. */
export interface Arrival {
  /** what to set in the URL: the tab, and the window and filter it was read over */
  patch: Partial<UrlState>;
  anchor?: string;
  titled?: string;
  /** the texts that name its row, the most telling first */
  find: string[];
  /** a moment of this session's tree */
  sid?: string;
  jump?: CiteJump;
}

export interface Target {
  /** the tab and the panel: "Risk & Warning › Rules in effect" */
  where: string;
  /** what it is: "rm -rf against a shared path" */
  label: string;
  /** null: the page draws it nowhere (the question's own view) */
  arrival: Arrival | null;
}

const rec = (d: unknown): Record<string, unknown> =>
  d && typeof d === 'object' && !Array.isArray(d) ? (d as Record<string, unknown>) : {};

/** The fields that name a row, the most telling first. */
const NAME_KEYS = ['command', 'pattern', 'tool', 'user', 'host', 'wrapper', 'agent_type', 'b', 'label',
  'title', 'ancestry', 'why', 'root', 'id'];

function names(d: Record<string, unknown>): string[] {
  const out: string[] = [];
  const add = (s: string) => {
    if (s && !out.includes(s)) out.push(s);
  };
  for (const k of NAME_KEYS) {
    const v = d[k];
    if ((typeof v === 'string' && v.trim()) || typeof v === 'number') {
      const s = String(v).trim();
      add(s);
      // a host is often shown by its short name
      if (k === 'host' && s.includes('.')) add(s.split('.')[0]);
      // the model's copy of every string is masked, the page's labels are not:
      // what comes before the mask still names the row
      const cut = s.indexOf('[REDACTED]');
      if (cut >= 6) add(s.slice(0, cut).trim());
    }
  }
  return out;
}

const SID = /^[^|\s]+\|\d{4}-\d{2}-\d{2}\|[a-z]+\|[^|\s]+$/;

/** A session row's sid: a hint, its own field, or the scheduler's session. */
function sidOf(c: AskCite): string | null {
  const d = rec(c.data);
  for (const v of [c.go?.sid, d.sid, d.session]) if (typeof v === 'string' && SID.test(v)) return v;
  return null;
}

const human = (k: string | number) => String(k).replace(/_/g, ' ');

/** The window, filter, and classes a question was read over, as URL state. */
function viewPatch(v: AskView | null | undefined): Partial<UrlState> {
  if (!v) return {};
  const p: Partial<UrlState> = { win: v.window_min ?? null, users: v.users ?? [], atypes: v.atypes ?? [] };
  const cls = (v.cls ?? []).filter((c): c is Cls => (CLS as string[]).includes(c));
  if (cls.length) p.cls = cls;
  return p;
}

/** Where one citation goes. */
export function target(c: AskCite, view: AskView | null | undefined): Target {
  const got = placeOf(c.path);
  const d = rec(c.data);
  const line = typeof c.data === 'string' ? c.data : null;
  const sid = sidOf(c);
  const jump = c.go?.jump;
  if (!got) {
    return { where: 'the question', label: c.path.map(human).join(' › ') || 'its view', arrival: null };
  }
  const [place, rest] = got;
  const tabLbl = TAB_LBL[place.tab] ?? place.tab;
  const where = place.panel ? `${tabLbl} › ${place.panel}` : tabLbl;
  const base = viewPatch(view);
  const nm = names(d);

  // a moment of the session asked about. The read it is in is the jump's to
  // find (TrajectoryInspector pages to it), so the page on screen is kept.
  if (jump && view?.sid) {
    const mark =
      !line && c.path[0] === 'landmarks'
        ? `${human(c.path[1])}${d.cmd ? ` · ${String(d.cmd)}` : ''}`
        : null;
    return {
      where,
      label: mark ?? (line ? line.slice(0, 90) : rest.length ? human(rest[0]) : 'process'),
      arrival: {
        patch: { ...base, tab: 'trajectories', traj: view.sid, tuser: null },
        find: [],
        sid: view.sid,
        jump,
      },
    };
  }
  // a session: it opens in the inspector
  if (sid) {
    const who = typeof d.user === 'string' ? `${d.user}’s ` : '';
    const kind = String(d.agent_type ?? d.actor3 ?? d.cls ?? '').trim();
    return {
      where: `${TAB_LBL.trajectories} › ${INSPECTOR}`,
      label: `${who}${kind ? kind + ' ' : ''}session`,
      arrival: {
        patch: { ...base, tab: 'trajectories', traj: sid, tfrom: null, tuser: null },
        anchor: slug(INSPECTOR),
        find: [],
      },
    };
  }
  // the session asked about, as a whole
  if (place.panel === INSPECTOR) {
    return {
      where,
      label: rest.length ? human(rest[0]) : human(c.path[0]),
      arrival: view?.sid
        ? { patch: { ...base, tab: 'trajectories', traj: view.sid, tuser: null }, anchor: slug(INSPECTOR), find: [] }
        : null,
    };
  }
  // the part of the panel it sits in: `poll` alone does not say whose
  const part = rest.filter((k) => typeof k === 'string' && k !== 'rows').map(human).join(' › ');
  const label = nm[0] ? (part ? `${nm[0]} · ${part}` : nm[0]) : part || place.panel || tabLbl;
  return {
    where,
    label,
    arrival: {
      patch: { ...base, tab: place.tab },
      anchor: place.titled ? undefined : place.panel ? (place.anchor ?? slug(place.panel)) : undefined,
      titled: place.titled,
      find: nm,
    },
  };
}

/** The link that opens a citation: in a new tab, it arrives by its hash. */
export function hrefOf(a: Arrival | null): string | undefined {
  if (!a) return undefined;
  const q = urlQuery({ ...parseUrl(location.search), ...a.patch });
  // the query already holds the patch: the hash says only how to arrive
  const { anchor, titled, find, sid, jump } = a;
  const h = encodeURIComponent(JSON.stringify({ patch: {}, anchor, titled, find, sid, jump }));
  return `${location.pathname}${q ? '?' + q : ''}#cite=${h}`;
}

/* ------------------------------------------------------------ arriving */

type Jumper = (j: CiteJump, what: string) => void;
const jumpers = new Map<string, Jumper>();

/** The open inspector of `sid` takes its citations' jumps; returns the undo. */
export function registerJumper(sid: string, f: Jumper): () => void {
  jumpers.set(sid, f);
  return () => {
    if (jumpers.get(sid) === f) jumpers.delete(sid);
  };
}

/** A cell's text without the disclosure mark a row that opens leads with. */
const cellText = (c: Element) => (c.textContent ?? '').replace(/^[\s▸▾►▼›]+/, '').trim();

/** The row of `root` the texts name: a cell that says exactly one of them
 *  beats a row that mentions it in passing, and the first text beats the rest.
 *  Never a line of an answer or its sources, which name the same things. */
function findRow(root: ParentNode, needles: string[]): HTMLElement | null {
  const rows = Array.from(root.querySelectorAll<HTMLElement>('tr, li, [role="row"]')).filter(
    (r) => !r.closest('thead, .chat, .askbox, .cite-src') && r.offsetParent !== null,
  );
  for (const n of needles) {
    let best: HTMLElement | null = null;
    for (const r of rows) {
      const t = r.textContent ?? '';
      if (!t.includes(n)) continue;
      if (Array.from(r.children).some((c) => cellText(c) === n)) return r;
      if (!best || t.length < (best.textContent ?? '').length) best = r;
    }
    if (best) return best;
  }
  return null;
}

function panelOf(a: Arrival): HTMLElement | null {
  if (a.anchor) return document.getElementById(a.anchor);
  if (a.titled) {
    const want = a.titled.toLowerCase();
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('section.panel[data-title]'))) {
      if ((el.dataset.title ?? '').toLowerCase().endsWith(want)) return el;
    }
  }
  return null;
}

const motion = (): ScrollBehavior =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';

function light(el: HTMLElement, center: boolean) {
  el.scrollIntoView({ block: center ? 'center' : 'start', behavior: motion() });
  el.classList.remove('cite-hit');
  // a reflow between, so a second visit flashes again
  void el.offsetWidth;
  el.classList.add('cite-hit');
  window.setTimeout(() => el.classList.remove('cite-hit'), 2600);
}

/** Once the page is on its tab: go to the citation's row, or its panel, or
 *  the moment of the session. The tab draws after its data, so this looks
 *  again for a while; a row still missing by then lands on its panel. */
export function arrive(a: Arrival, what: string) {
  const t0 = performance.now();
  const tick = () => {
    const late = performance.now() - t0;
    if (a.jump && a.sid) {
      const j = jumpers.get(a.sid);
      if (j) {
        j(a.jump, what);
        return;
      }
    } else {
      const panel = panelOf(a);
      const scope = panel ?? (a.anchor || a.titled ? null : document.querySelector('main'));
      const row = scope && a.find.length ? findRow(scope, a.find) : null;
      if (row) {
        light(row, true);
        return;
      }
      if (panel && (!a.find.length || late > 2500)) {
        light(panel, false);
        return;
      }
      // a row the tab does not draw: the tab, from its top
      if (!a.anchor && !a.titled && late > 2500) {
        window.scrollTo({ top: 0, behavior: motion() });
        return;
      }
    }
    if (late < 10000) window.setTimeout(tick, 120);
    else if (!a.jump) window.scrollTo({ top: 0, behavior: motion() });
  };
  tick();
}

/** Go to a citation in this tab: the URL first (Back returns to the answer),
 *  then the row. */
export function go(a: Arrival, what: string) {
  const q = urlQuery({ ...parseUrl(location.search), ...a.patch });
  const url = `${location.pathname}${q ? '?' + q : ''}`;
  if (url !== location.pathname + location.search) {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
    // the page draws the tab it is going to first: until then the rows on
    // screen are the ones it is leaving
    requestAnimationFrame(() => requestAnimationFrame(() => arrive(a, what)));
    return;
  }
  arrive(a, what);
}

/** A page opened from a citation's link arrives at it, once. */
export function arriveFromHash() {
  const h = location.hash;
  if (!h.startsWith('#cite=')) return;
  history.replaceState(null, '', location.pathname + location.search);
  try {
    const a = JSON.parse(decodeURIComponent(h.slice('#cite='.length))) as Arrival;
    if (a && typeof a === 'object' && Array.isArray(a.find)) arrive(a, 'the cited source');
  } catch {
    /* a hand-edited link: the page as it opened */
  }
}
