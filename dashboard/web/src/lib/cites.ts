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
import { AGENT_TYPE_LBL, CLS } from './classes';
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
  /** what to set in the URL: the tab, the window it was read over, and the
   *  filter that narrows the page to what it is about */
  patch: Partial<UrlState>;
  anchor?: string;
  titled?: string;
  /** the texts that name its row, the most telling first */
  find: string[];
  /** the agents it is (`host:pid`): an agent's row is found by these */
  keys?: string[];
  /** its rows are drawn from a view the filter builds: wait for them */
  waits?: boolean;
  /** a moment of this session's tree */
  sid?: string;
  jump?: CiteJump;
}

export interface Target {
  /** the tab and the panel: "Risk & Warning › Rules in effect" */
  where: string;
  /** what it is: "rm -rf against a shared path" */
  label: string;
  /** what a click narrows the page to: "marthen", "the human class"; null: nothing */
  narrows: string | null;
  /** null: the page draws it nowhere (the question's own view) */
  arrival: Arrival | null;
  /** an agent's part opens its trajectory; the panel it was read from is this */
  also?: { where: string; arrival: Arrival; narrows: string | null };
}

const rec = (d: unknown): Record<string, unknown> =>
  d && typeof d === 'object' && !Array.isArray(d) ? (d as Record<string, unknown>) : {};

/** The fields that name a row, the most telling first; a folder by the path
 *  the page shows it as. */
const NAME_KEYS = ['command', 'pattern', 'tool', 'user', 'host', 'wrapper', 'agent_type', 'b', 'label',
  'title', 'ancestry', 'why', 'root', 'id', 'shown', 'path'];

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

const isCls = (k: unknown): k is Cls => typeof k === 'string' && (CLS as string[]).includes(k);

/** The page filter that narrows to what a citation is about, so a click shows
 *  it rather than only scrolling to it: its one user, its one agent type, its
 *  one class. A row of many users narrows to none of them, and an agent is
 *  narrowed by its user, not its type -- a Claude Code's tree holds the Codex
 *  it started, which a type would cut out. */
function narrowing(c: AskCite, d: Record<string, unknown>): { patch: Partial<UrlState>; said: string[] } {
  const patch: Partial<UrlState> = {};
  const said: string[] = [];
  const keys = c.path.filter((k): k is string => typeof k === 'string');
  let user = typeof d.user === 'string' && d.user ? d.user : null;
  if (!user && Array.isArray(d.users) && d.users.length === 1 && typeof d.users[0] === 'string') user = d.users[0];
  // a "who ran it" count is keyed by user: one user is that user
  if (!user && keys[keys.length - 1] === 'users' && Object.keys(d).length === 1) user = Object.keys(d)[0];
  if (user) {
    patch.users = [user];
    said.push(user);
  }
  const ti = keys.indexOf('by_agent_type');
  const atype = ti >= 0 ? keys[ti + 1] : keys[0] === 'facets' && keys[1] === 'agent_types' ? d.agent_type : null;
  if (!user && typeof atype === 'string' && atype) {
    patch.atypes = [atype];
    said.push(atype);
  }
  // the class a part of a panel is about (`by_class.human`), or a row's own
  const cls = [...keys].reverse().find(isCls) ?? (isCls(d.actor3) ? d.actor3 : isCls(d.cls) ? d.cls : null);
  if (cls) {
    patch.cls = [cls];
    said.push(`the ${cls === 'ide' ? 'IDE' : cls} class`);
  }
  return { patch, said };
}

/** The agents a row is: its own (`host`, `pid`), or a tree's, by its root
 *  process (`root.pid`). */
function agentKeys(d: Record<string, unknown>): string[] | undefined {
  const host = typeof d.host === 'string' ? d.host : null;
  if (!host) return undefined;
  const root = typeof d.root === 'number' ? d.root : rec(d.root).pid;
  const out = [d.pid, root].filter((p): p is number => typeof p === 'number').map((p) => `${host}:${p}`);
  return out.length ? out : undefined;
}

/** Where one citation goes. */
export function target(c: AskCite, view: AskView | null | undefined): Target {
  const got = placeOf(c.path);
  const d = rec(c.data);
  const line = typeof c.data === 'string' ? c.data : null;
  const sid = sidOf(c);
  const jump = c.go?.jump;
  if (!got) {
    return { where: 'the question', label: c.path.map(human).join(' › ') || 'its view', narrows: null, arrival: null };
  }
  const [place, rest] = got;
  const tabLbl = TAB_LBL[place.tab] ?? place.tab;
  // an answer about one session is read inside that session's inspector
  const here = place.panel === INSPECTOR && !!view?.sid;
  const where = here ? 'this session' : place.panel ? `${tabLbl} › ${place.panel}` : tabLbl;
  const base = viewPatch(view);
  const nm = names(d);
  // whom it is: its own fields, or for a part of a row that names no one (a
  // folder an agent read), the row's (`of`)
  const owner = rec(c.of);
  const who: Record<string, unknown> = { ...owner, ...d };
  // the part of the panel it sits in: `poll` alone does not say whose
  const part = rest.filter((k) => typeof k === 'string' && k !== 'rows').map(human).join(' › ');
  const label = nm[0] ? (part ? `${nm[0]} · ${part}` : nm[0]) : part || place.panel || tabLbl;
  const { patch, said } = narrowing(c, who);
  const panelArrival: Arrival = {
    patch: { ...base, ...patch, tab: place.tab },
    anchor: place.titled ? undefined : place.panel ? (place.anchor ?? slug(place.panel)) : undefined,
    titled: place.titled,
    find: nm,
    keys: agentKeys(who),
  };

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
      narrows: null,
      arrival: {
        patch: { ...base, tab: 'trajectories', traj: view.sid, tuser: null },
        find: [],
        sid: view.sid,
        jump,
      },
    };
  }
  // a session, an agent, or a part of one (a folder an agent read): it opens
  // as the session's trajectory, which is it and nothing else; the panel it
  // was read from, filtered to it, is a link of its own
  if (sid) {
    const whose = typeof who.user === 'string' ? `${who.user}’s ` : '';
    const at = typeof who.agent_type === 'string' ? who.agent_type : null;
    const kind = (at ? AGENT_TYPE_LBL[at] ?? at : String(who.actor3 ?? who.cls ?? '')).trim();
    const sess = `${whose}${kind ? kind + ' ' : ''}session`;
    const isSession = place.tab === 'trajectories';
    return {
      where: sid === view?.sid ? 'this session' : `${TAB_LBL.trajectories} › ${INSPECTOR}`,
      // a part of it by its own name; the agent or the session itself as the session
      label: c.of ? `${nm[0] ?? part} in ${sess}` : sess,
      narrows: null,
      arrival: {
        patch: { ...base, tab: 'trajectories', traj: sid, tfrom: null, tuser: null },
        anchor: slug(INSPECTOR),
        find: [],
      },
      also: isSession ? undefined : { where, arrival: panelArrival, narrows: said.length ? said.join(', ') : null },
    };
  }
  // the session asked about, as a whole
  if (place.panel === INSPECTOR) {
    return {
      where,
      label: c.path.filter((k) => typeof k === 'string').map(human).join(' › '),
      narrows: null,
      arrival: view?.sid
        ? { patch: { ...base, tab: 'trajectories', traj: view.sid, tuser: null }, anchor: slug(INSPECTOR), find: [] }
        : null,
    };
  }
  return {
    where,
    label: c.of && typeof owner.user === 'string' ? `${label} · ${owner.user}’s` : label,
    narrows: said.length ? said.join(', ') : null,
    arrival: panelArrival,
  };
}

const sameList = (a: readonly string[] | undefined, b: readonly string[] | undefined) =>
  (a ?? []).length === (b ?? []).length && (a ?? []).every((x) => (b ?? []).includes(x));

/** Whether going to `a` from the page `from` asks for a view the service may
 *  still have to build -- another filter or window -- whose rows come later. */
function builds(a: Arrival, from: UrlState): boolean {
  const p = a.patch;
  return (
    (p.users !== undefined && !sameList(p.users, from.users)) ||
    (p.atypes !== undefined && !sameList(p.atypes, from.atypes)) ||
    (p.win !== undefined && p.win !== from.win)
  );
}

/** The link that opens a citation: in a new tab, it arrives by its hash. */
export function hrefOf(a: Arrival | null, what: string): string | undefined {
  if (!a) return undefined;
  const q = urlQuery({ ...parseUrl(location.search), ...a.patch });
  // the query already holds the patch: the hash says only how to arrive; a
  // new page builds its filtered view from nothing, so its rows are waited for
  const { anchor, titled, find, keys, sid, jump } = a;
  const waits = !!(a.patch.users?.length || a.patch.atypes?.length);
  const h = encodeURIComponent(JSON.stringify({ patch: { tab: a.patch.tab }, anchor, titled, find, keys, waits, sid, jump, what }));
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

/** The element the citation is: an agent's row by its keys (the agent's own
 *  row before a fold that holds it), else the row its texts name. */
function targetIn(scope: ParentNode, a: Arrival): HTMLElement | null {
  for (const k of a.keys ?? []) {
    const all = Array.from(scope.querySelectorAll<HTMLElement>(`[data-keys~="${k.replace(/["\\]/g, '\\$&')}"]`))
      .filter((e) => e.offsetParent !== null);
    const own = all.find((e) => !e.classList.contains('fold')) ?? all[0];
    if (own) return own;
  }
  return a.find.length ? findRow(scope, a.find) : null;
}

/** Open the row a citation landed on, so it shows what it is: an agent's row
 *  draws its tree, a row that folds out lists what it counts. One already
 *  open stays open. */
function openRow(el: HTMLElement) {
  if (el.matches('button.al-item:not(.on):not(.fold), tr.xrow:not(.open), tr.u-row:not(.open)')) el.click();
}

/** In the tree an agent's row drew, the cited agent's own box: picked, so
 *  its blast radius opens beside the tree. The tree draws after the row
 *  opens, so this looks again for a moment; an agent folded away in it
 *  ("+3 more") leaves the tree as it is. */
function pickNode(keys: string[], tries = 0) {
  for (const k of keys) {
    const el = document.querySelector<HTMLElement>(`.tnode[data-id="${k.replace(/["\\]/g, '\\$&')}"]`);
    if (el) {
      if (!el.classList.contains('on')) el.click();
      return;
    }
  }
  if (tries < 15) window.setTimeout(() => pickNode(keys, tries + 1), 150);
}

/** A fold of look-alike agents holding the one cited: opened, it draws the
 *  agent's own row, which is looked for next. */
function openFold(el: HTMLElement): boolean {
  if (!el.matches('button.al-item.fold')) return false;
  if (el.getAttribute('aria-expanded') !== 'true') el.click();
  return true;
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

/** Once the page is on its tab: go to the citation's row and open it, or to
 *  its panel, or to the moment of the session. The tab draws after its data,
 *  so this looks again for a while -- for minutes when the filter it set has
 *  a view to build, showing the panel meanwhile -- and stops when the reader
 *  goes to another tab. A row still missing by then leaves the panel lit. */
export function arrive(a: Arrival, what: string) {
  const t0 = performance.now();
  const tab = a.patch.tab;
  const patience = a.waits ? 300000 : 10000;
  let shown = false;
  const tick = () => {
    if (tab && (parseUrl(location.search).tab || 'live') !== tab) return;
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
      const row = scope ? targetIn(scope, a) : null;
      if (row) {
        if (openFold(row)) {
          window.setTimeout(tick, 150);
          return;
        }
        // opened first: the page redraws the row's classes as it opens, and
        // would take the light off with them
        openRow(row);
        requestAnimationFrame(() => requestAnimationFrame(() => light(row, true)));
        if (a.keys?.length && row.matches('button.al-item')) pickNode(a.keys);
        return;
      }
      const sought = a.find.length > 0 || (a.keys?.length ?? 0) > 0;
      if (panel && !shown && (!sought || late > 2500)) {
        light(panel, false);
        shown = true;
        if (!sought) return;
      }
      // a row the tab does not draw: the tab, from its top
      if (!a.anchor && !a.titled && late > 2500 && !a.waits) {
        window.scrollTo({ top: 0, behavior: motion() });
        return;
      }
    }
    if (late < patience) window.setTimeout(tick, late < 10000 ? 120 : 500);
    else if (!a.jump && !shown) window.scrollTo({ top: 0, behavior: motion() });
  };
  tick();
}

/** Go to a citation in this tab: the URL first (Back returns to the answer),
 *  then the row. */
export function go(a: Arrival, what: string) {
  const from = parseUrl(location.search);
  const q = urlQuery({ ...from, ...a.patch });
  const url = `${location.pathname}${q ? '?' + q : ''}`;
  const b = { ...a, waits: builds(a, from) };
  if (url !== location.pathname + location.search) {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
    // the page draws the tab it is going to first: until then the rows on
    // screen are the ones it is leaving
    requestAnimationFrame(() => requestAnimationFrame(() => arrive(b, what)));
    return;
  }
  arrive(b, what);
}

/** A page opened from a citation's link arrives at it, once. */
export function arriveFromHash() {
  const h = location.hash;
  if (!h.startsWith('#cite=')) return;
  history.replaceState(null, '', location.pathname + location.search);
  try {
    const a = JSON.parse(decodeURIComponent(h.slice('#cite='.length))) as Arrival & { what?: unknown };
    if (a && typeof a === 'object' && Array.isArray(a.find)) {
      arrive(a, typeof a.what === 'string' && a.what ? a.what : 'the cited source');
    }
  } catch {
    /* a hand-edited link: the page as it opened */
  }
}
