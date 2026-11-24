/* Filter state, and its one canonical serialisation.
 *
 * The page-wide filters ride in the URL with the view -- the tab, the rank,
 * the window, the open session -- so a reload restores what the user was
 * looking at; the view's keys are stripped before a query reaches the API.
 *
 * The user and agent-type filter (`Who`) is page-wide like the class filter,
 * but it is not applied to numbers the page already has: the service builds a
 * filtered view the way it builds a window, so it travels with the panels and
 * trajectories requests.
 */
import { CLS } from './classes';
import type { Cls, TrajOrder, TrajRank } from '../api/types';
import { TRAJ_RANKS } from '../api/types';
import { table } from './lookup';

/** UI-only keys: they live in the URL, never in an API query.
 *
 * Load-bearing rather than documentation, which is the point. An earlier
 * version of this list was a bare comment that `urlQuery` did not consult, so
 * it drifted out of sync and was removed as dead; reinstating it that way would
 * only set up the same drift again. `urlQuery` now serialises exactly these
 * keys, and `UI_OMIT_AT` below is a mapped type over them, so adding a key here
 * without deciding what value omits it is a compile error.
 */
export const UI_KEYS = ['tab', 'rank', 'win', 'traj', 'tfrom', 'tuser', 'torder', 'portrait'] as const;
export type UiKey = (typeof UI_KEYS)[number];

/** The page-wide user and agent-type filter; empty lists are everyone. */
export interface Who {
  users: string[];
  /** Collector agent-type names, `none` for an untagged process. */
  atypes: string[];
}

export interface Filters extends Who {
  /** `class` is repeatable; at least one class is always selected. */
  cls: Cls[];
}

export interface UrlState extends Filters {
  tab: string;
  rank: TrajRank;
  /** Selected window in minutes. `null` means "whatever the service defaults
   *  to" -- deliberately not resolved to a number here, so a link shared before
   *  an operator changed `live.window_min` follows the new default instead of
   *  pinning the old one. */
  win: number | null;
  /** The trajectory open in the inspector (its sid), so an inspection is a
   *  link like every other view: on the Agents tab, beside its list of every
   *  session, or on the Users tab, under a portrait. It is the tab's it was
   *  opened on, so going to another tab closes it (`patched`). */
  traj: string | null;
  /** Where the inspector's read starts, epoch seconds: set by "continue" after
   *  a read stopped at its budget, so the continued view is linkable too. */
  tfrom: number | null;
  /** A user the Agents tab's list of sessions is narrowed to, from a
   *  portrait's "every session" on the Users tab (or a link to the old
   *  Sessions tab, from Active users' "sessions →", roadmap note 7): a view,
   *  not a filter, so nothing is rebuilt for it. */
  tuser: string | null;
  /** Which end of a session the inspector lists first and draws in full. It
   *  stays as other sessions are opened: it is how this reader reads them. */
  torder: TrajOrder;
  /** The user whose portrait the Users tab has open: a view, not a filter, so
   *  nothing is rebuilt for it, and a portrait is a link like every other view. */
  portrait: string | null;
}

/** The value at which each UI key is left out of the URL, so a default-valued
 *  view produces a clean query string instead of `?tab=live&rank=recent`. */
const UI_OMIT_AT: { readonly [K in UiKey]: UrlState[K] } = {
  tab: 'live',
  rank: 'recent',
  win: null,
  traj: null,
  tfrom: null,
  tuser: null,
  torder: 'oldest',
  portrait: null,
};

// A saved link ranked by a ranking since removed (`chain_runs`) opens on the default.
const RANKS = new Set<string>(TRAJ_RANKS);

/** Renamed tabs: old key -> current key. */
const TAB_ALIAS: Record<string, string> = table({
  cascades: 'agents',
  // the Sessions tab's old key: a sub-tab of the Agents entry again (`sessions`), its list and inspector
  trajectories: 'sessions',
  io: 'resources',
  // the sandbox panels were Internal's, and are Security's confinement half now
  sandbox: 'security',
  // Overview's one table is a section of Live now; App.tsx lands such a link on it
  overview: 'live',
  // HPC is the Slurm tab, out from under the login nodes: its key stays
  hpc: 'slurm',
});

/** How a filter asks for the processes the collector gave no agent type. */
export const UNTAGGED = 'none';
const UNTAGGED_ALIAS = new Set(['none', 'None', 'null', 'untagged']);

/** Every value of a repeatable key, comma lists split, blanks and repeats dropped. */
function many(p: URLSearchParams, ...keys: string[]): string[] {
  const out: string[] = [];
  for (const k of keys)
    for (const v of p.getAll(k))
      for (const x of v.split(','))
        if (x && !out.includes(x)) out.push(x);
  return out;
}

export function parseUrl(search: string): UrlState {
  const p = new URLSearchParams(search);
  const raw = p
    .getAll('class')
    // a link saved before the IDE class was named says human-vscode
    .map((c) => (c === 'human-vscode' ? 'ide' : c))
    .filter((c): c is Cls => (CLS as string[]).includes(c));
  const rank = p.get('rank');
  const win = Number(p.get('win'));
  const tfrom = Number(p.get('tfrom'));
  return {
    // At least one class is always selected: an empty selection would silently
    // render an all-zero dashboard, which reads as "no activity".
    cls: raw.length ? raw : [...CLS],
    // `user` and `agent_type` were the search bar's own fields before they
    // became page-wide, so a saved search link still narrows to the same people.
    users: many(p, 'user'),
    atypes: [...new Set(many(p, 'atype', 'agent_type').map((t) => (UNTAGGED_ALIAS.has(t) ? UNTAGGED : t)))],
    // `cascades` is the Agents tab's old name: a link saved before the rename
    // still opens it.
    // A link to the Agents tab that opened a session, or narrowed the list to
    // one user's, was a link to its sessions, the tab's last part then: it
    // opens the Sessions sub-tab they are now.
    tab: ((t) => (t === 'agents' && (p.get('traj') || p.get('tuser')) ? 'sessions' : t))(
      TAB_ALIAS[p.get('tab') ?? ''] ?? (p.get('tab') || 'live'),
    ),
    rank: (rank && RANKS.has(rank) ? rank : 'recent') as TrajRank,
    // Nonsense in the query string falls back to the default rather than being
    // clamped to a number nobody asked for. The backend clamps what it is sent.
    win: Number.isFinite(win) && win > 0 ? Math.floor(win) : null,
    traj: p.get('traj') || null,
    tfrom: Number.isFinite(tfrom) && tfrom > 0 ? tfrom : null,
    tuser: p.get('tuser') || null,
    torder: p.get('torder') === 'newest' ? 'newest' : 'oldest',
    portrait: p.get('portrait') || null,
  };
}

/** The state with `patch` applied. A session is open on the tab it was opened
 *  on -- the Sessions tab, or a portrait on Users -- so a patch that goes to
 *  another tab closes it (`traj`, and where its read starts), unless the patch
 *  opens one there itself: one left open would open again on a tab that did not
 *  ask for it, and scroll that tab down to it. */
export function patched(s: UrlState, patch: Partial<UrlState>): UrlState {
  const next: UrlState = { ...s, ...patch };
  if (patch.tab !== undefined && patch.tab !== s.tab && patch.traj === undefined) {
    next.traj = null;
    next.tfrom = null;
  }
  return next;
}

/** `user` and `agent_type` repeated, in the service's filter vocabulary
 *  (`export.parse_filters`), sorted so
 *  one filter is one string whichever order its values were picked in. */
export function whoQuery(w: Who): string {
  const p = new URLSearchParams();
  for (const u of [...w.users].sort()) p.append('user', u);
  for (const t of [...w.atypes].sort()) p.append('agent_type', t);
  return p.toString();
}

/** The one client-side test, for the live tier the service does not rebuild
 *  per filter: the same rule the service applies to a filtered view's records. */
export function whoMatch(w: Who, user: string | null | undefined, atype: string | null | undefined): boolean {
  if (w.users.length && !(user != null && w.users.includes(user))) return false;
  if (w.atypes.length && !w.atypes.includes(atype ? atype : UNTAGGED)) return false;
  return true;
}

/** The API query: `class` repeated, and the user and agent-type filter; no UI
 *  keys. */
export function apiQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const c of f.cls) p.append('class', c);
  const w = whoQuery(f);
  return [p.toString(), w].filter(Boolean).join('&');
}

/** The browser query: the API query plus the UI keys.
 *
 * `apiQuery` writes no UI key, which is how a UI key is kept out of the
 * request the backend sees.
 */
export function urlQuery(s: UrlState): string {
  // The agent types go in as `atype`: `agent_type` is read too, for old links.
  const p = new URLSearchParams(apiQuery({ ...s, atypes: [] }));
  for (const t of s.atypes) p.append('atype', t);
  for (const k of UI_KEYS) {
    const v = s[k];
    if (v != null && v !== '' && v !== UI_OMIT_AT[k]) p.append(k, String(v));
  }
  return p.toString();
}
