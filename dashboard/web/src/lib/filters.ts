/* Filter state, and its one canonical serialisation.
 *
 * The filter bar, the `/api/events` request and the `/api/export` download all
 * read the SAME query string produced here, so the downloaded JSONL is exactly
 * the view on screen. `tab` and `rank` ride along in the URL too so a reload
 * restores what the user was looking at, but they are stripped before the
 * query reaches the API.
 */
import { CLS } from './classes';
import type { Cls, TrajRank } from '../api/types';

/** Scalar filter params, in the order the backend documents them. */
export const SCALAR_KEYS = [
  'from',
  'to',
  'agent_type',
  'user',
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

/** UI-only keys: they live in the URL, never in an API query. */
export const UI_KEYS = ['tab', 'rank', 'win'] as const;

export interface Filters {
  /** `class` is repeatable; at least one class is always selected. */
  cls: Cls[];
  scalars: Partial<Record<ScalarKey, string>>;
}

export const EMPTY_FILTERS: Filters = { cls: [...CLS], scalars: {} };

export interface UrlState extends Filters {
  tab: string;
  rank: TrajRank;
  /** Selected window in minutes. `null` means "whatever the service defaults
   *  to" -- deliberately not resolved to a number here, so a link shared before
   *  an operator changed `live.window_min` follows the new default instead of
   *  pinning the old one. */
  win: number | null;
}

const RANKS = new Set(['events', 'cpu_s', 'distinct_tools', 'chain_runs']);

export function parseUrl(search: string): UrlState {
  const p = new URLSearchParams(search);
  const raw = p.getAll('class').filter((c): c is Cls => (CLS as string[]).includes(c));
  const scalars: Partial<Record<ScalarKey, string>> = {};
  for (const k of SCALAR_KEYS) {
    const v = p.get(k);
    if (v != null && v !== '') scalars[k] = v;
  }
  const rank = p.get('rank');
  const win = Number(p.get('win'));
  return {
    // At least one class is always selected: an empty selection would silently
    // render an all-zero dashboard, which reads as "no activity".
    cls: raw.length ? raw : [...CLS],
    scalars,
    tab: p.get('tab') || 'live',
    rank: (rank && RANKS.has(rank) ? rank : 'events') as TrajRank,
    // Nonsense in the query string falls back to the default rather than being
    // clamped to a number nobody asked for. The backend clamps what it is sent.
    win: Number.isFinite(win) && win > 0 ? Math.floor(win) : null,
  };
}

/** The API query: `class` repeated, scalars in order, no UI keys. */
export function apiQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const c of f.cls) p.append('class', c);
  for (const k of SCALAR_KEYS) {
    const v = f.scalars[k];
    if (v != null && v !== '') p.append(k, v);
  }
  return p.toString();
}

/** The browser query: the API query plus the UI keys. */
export function urlQuery(s: UrlState): string {
  const p = new URLSearchParams(apiQuery(s));
  if (s.tab && s.tab !== 'live') p.append('tab', s.tab);
  if (s.rank && s.rank !== 'events') p.append('rank', s.rank);
  if (s.win != null) p.append('win', String(s.win));
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
  const to = new Date();
  const from = new Date(to.getTime() - minutes * 60_000);
  const stamp = (d: Date) =>
    new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
      .toISOString()
      .slice(0, 19)
      .replace('T', ' ');
  return { ...s, scalars: { ...s.scalars, from: stamp(from), to: stamp(to) } };
}

export function countActiveScalars(f: Filters): number {
  return SCALAR_KEYS.reduce((a, k) => a + (f.scalars[k] ? 1 : 0), 0);
}

/** True when the class selection is not the full three-class set. */
export function classFiltered(f: Filters): boolean {
  return f.cls.length !== CLS.length;
}
