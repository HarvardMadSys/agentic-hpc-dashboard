/* Filter state, and its one canonical serialisation.
 *
 * The filter bar, the `/api/events` request and the `/api/export` download all
 * read the SAME query string produced here, so the downloaded JSONL is exactly
 * the view on screen. `tab` and `rank` ride along in the URL too so a reload
 * restores what the user was looking at, but they are stripped before the
 * query reaches the API.
 *
 * The user and agent-type filter (`Who`) is page-wide like the class filter,
 * but it is not applied to numbers the page already has: the service builds a
 * filtered view the way it builds a window, so it travels with the panels and
 * trajectories requests as well as with the events query.
 */
import { CLS } from './classes';
import { serverNow } from './clock';
import type { Cls, TrajRank } from '../api/types';

/** Scalar filter params, in the order the backend documents them. */
export const SCALAR_KEYS = [
  'from',
  'to',
  'host',
  'tool',
  'purpose',
  'bucket',
  'session_key',
  'sandbox',
  'approval',
  'depth_min',
  'depth_max',
  'cpu_min',
  'duration_min',
  'exit_code',
  'signal',
  'q',
] as const;
export type ScalarKey = (typeof SCALAR_KEYS)[number];

/** UI-only keys: they live in the URL, never in an API query.
 *
 * Load-bearing rather than documentation, which is the point. An earlier
 * version of this list was a bare comment that `urlQuery` did not consult, so
 * it drifted out of sync and was removed as dead; reinstating it that way would
 * only set up the same drift again. `urlQuery` now serialises exactly these
 * keys, and `UI_OMIT_AT` below is a mapped type over them, so adding a key here
 * without deciding what value omits it is a compile error.
 */
export const UI_KEYS = ['tab', 'rank', 'win', 'traj', 'tfrom'] as const;
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
  scalars: Partial<Record<ScalarKey, string>>;
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
   *  link like every other view. */
  traj: string | null;
  /** Where the inspector's read starts, epoch seconds: set by "continue" after
   *  a read stopped at its budget, so the continued view is linkable too. */
  tfrom: number | null;
}

/** The value at which each UI key is left out of the URL, so a default-valued
 *  view produces a clean query string instead of `?tab=live&rank=events`. */
const UI_OMIT_AT: { readonly [K in UiKey]: UrlState[K] } = {
  tab: 'live',
  rank: 'events',
  win: null,
  traj: null,
  tfrom: null,
};

const RANKS = new Set(['events', 'cpu_s', 'distinct_tools', 'chain_runs']);

/** Renamed tabs: old key -> current key. */
const TAB_ALIAS: Record<string, string> = { cascades: 'agents' };

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
  const scalars: Partial<Record<ScalarKey, string>> = {};
  for (const k of SCALAR_KEYS) {
    const v = p.get(k);
    if (v != null && v !== '') scalars[k] = v;
  }
  const rank = p.get('rank');
  const win = Number(p.get('win'));
  const tfrom = Number(p.get('tfrom'));
  return {
    // At least one class is always selected: an empty selection would silently
    // render an all-zero dashboard, which reads as "no activity".
    cls: raw.length ? raw : [...CLS],
    scalars,
    // `user` and `agent_type` were the search bar's own fields before they
    // became page-wide, so a saved search link still narrows to the same people.
    users: many(p, 'user'),
    atypes: [...new Set(many(p, 'atype', 'agent_type').map((t) => (UNTAGGED_ALIAS.has(t) ? UNTAGGED : t)))],
    // `cascades` is the Agents tab's old name: a link saved before the rename
    // still opens it.
    tab: TAB_ALIAS[p.get('tab') ?? ''] ?? (p.get('tab') || 'live'),
    rank: (rank && RANKS.has(rank) ? rank : 'events') as TrajRank,
    // Nonsense in the query string falls back to the default rather than being
    // clamped to a number nobody asked for. The backend clamps what it is sent.
    win: Number.isFinite(win) && win > 0 ? Math.floor(win) : null,
    traj: p.get('traj') || null,
    tfrom: Number.isFinite(tfrom) && tfrom > 0 ? tfrom : null,
  };
}

/** `user` and `agent_type` repeated, in `/api/events`' vocabulary, sorted so
 *  one filter is one string whichever order its values were picked in. */
export function whoQuery(w: Who): string {
  const p = new URLSearchParams();
  for (const u of [...w.users].sort()) p.append('user', u);
  for (const t of [...w.atypes].sort()) p.append('agent_type', t);
  return p.toString();
}

export const whoActive = (w: Who): boolean => w.users.length + w.atypes.length > 0;

/** The one client-side test, for the live tier the service does not rebuild
 *  per filter: the same rule the service applies to a filtered view's records. */
export function whoMatch(w: Who, user: string | null | undefined, atype: string | null | undefined): boolean {
  if (w.users.length && !(user != null && w.users.includes(user))) return false;
  if (w.atypes.length && !w.atypes.includes(atype ? atype : UNTAGGED)) return false;
  return true;
}

/** The API query: `class` repeated, scalars in order, the user and agent-type
 *  filter, no UI keys. */
export function apiQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const c of f.cls) p.append('class', c);
  for (const k of SCALAR_KEYS) {
    const v = f.scalars[k];
    if (v != null && v !== '') p.append(k, v);
  }
  const w = whoQuery(f);
  return [p.toString(), w].filter(Boolean).join('&');
}

/** The browser query: the API query plus the UI keys.
 *
 * `apiQuery` iterates SCALAR_KEYS and UI_KEYS is disjoint from it, which is how
 * a UI key is kept out of the request the backend sees.
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

/** `from`/`to` for the events + export query, pinned to the selected window.
 *
 * The download has to be exactly the view, and the view is now window-bounded,
 * so a window that the user narrowed must reach `/api/export` too. An explicit
 * `from`/`to` typed into the filter bar still wins: it is the more specific
 * statement of intent.
 */
export function windowBounds(s: UrlState, minutes: number | null): Filters {
  if (minutes == null || s.scalars.from || s.scalars.to) return s;
  const to = new Date(serverNow());
  const from = new Date(to.getTime() - minutes * 60_000);
  const stamp = (d: Date) =>
    new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
      .toISOString()
      .slice(0, 19)
      .replace('T', ' ');
  return { ...s, scalars: { ...s.scalars, from: stamp(from), to: stamp(to) } };
}

export function countActiveScalars(f: Pick<Filters, 'cls' | 'scalars'>): number {
  return SCALAR_KEYS.reduce((a, k) => a + (f.scalars[k] ? 1 : 0), 0);
}
