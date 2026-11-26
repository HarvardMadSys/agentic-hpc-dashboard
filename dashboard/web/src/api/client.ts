/* Same-origin HTTP client for the rc_dashboard API.
 *
 * There is no mock branch in here on purpose: in development the Vite dev server
 * answers `/api/*` from `web/mock/` (see `web/mock/plugin.ts`), so the code path
 * exercised in dev is byte-for-byte the production path. Nothing under `src/`
 * ever synthesises a value.
 */
import type {
  CollectorsResponse,
  ConfigTree,
  FeedsResponse,
  LiveResponse,
  LiveUserDetail,
  IoCmdJob,
  IoSort,
  LiveUsers,
  PanelsResponse,
  TrajOrder,
  TrajRank,
  Trajectories,
  TrajectoryDetail,
  TrendResponse,
  UserPortrait,
  UsersResponse,
  WindowsResponse,
} from './types';
import { whoQuery } from '../lib/filters';
import type { Who } from '../lib/filters';

/** Override for a non-root mount or a split dev backend. */
export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) || '/api';

export class ApiError extends Error {
  status: number;
  url: string;
  constructor(url: string, status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
  }
}

/** The service asks for its password (rc_dashboard/auth.py), and this page's
 *  session has run out or was signed out elsewhere: back to the sign-in form,
 *  which returns to this very URL. Once per page, however many requests fail. */
let signingIn = false;
function signInAgain(status: number) {
  if (status !== 401 || signingIn) return;
  signingIn = true;
  const back = location.pathname + location.search + location.hash;
  location.assign('/login?next=' + encodeURIComponent(back));
}

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const url = API_BASE + path;
  const r = await fetch(url, { cache: 'no-store', ...init });
  if (!r.ok) {
    signInAgain(r.status);
    let detail = r.statusText;
    try {
      detail = (await r.text()).slice(0, 400) || detail;
    } catch {
      /* body already consumed or unreadable; the status is the message */
    }
    throw new ApiError(url, r.status, `${r.status} ${detail}`);
  }
  return (await r.json()) as T;
}

/** `?window_min=N`, or nothing at all when the caller has no opinion.
 *
 * Omitted rather than sent as the resolved default: the service decides what
 * "default" means, so a page that never touched the picker follows
 * `live.window_min` instead of pinning whatever it happened to read first.
 */
function win(minutes: number | null | undefined, sep = '?'): string {
  return minutes == null ? '' : `${sep}window_min=${encodeURIComponent(minutes)}`;
}

export const getConfig = (s?: AbortSignal) => get<ConfigTree>('/config', { signal: s });
export const getFeeds = (s?: AbortSignal) => get<FeedsResponse>('/feeds', { signal: s });
export const getCollectors = (s?: AbortSignal) => get<CollectorsResponse>('/collectors', { signal: s });
export const getWindows = (s?: AbortSignal) => get<WindowsResponse>('/windows', { signal: s });

/** `&user=..&agent_type=..`, or nothing when the filter is empty -- so an
 *  unfiltered page asks for exactly what it asked for before filters existed. */
function who(w: Who | null | undefined): string {
  const q = w ? whoQuery(w) : '';
  return q ? '&' + q : '';
}

/** `who` narrows the panels to some users or agent types: a view of its own,
 *  which the service builds like a window. */
export const getPanels = (minutes?: number | null, s?: AbortSignal, w?: Who | null) => {
  const q = (win(minutes, '') + who(w)).replace(/^&/, '');
  return get<PanelsResponse>(`/panels${q ? '?' + q : ''}`, { signal: s });
};
/** The Trend tab: the roll-up table's last `days` days in `by` buckets. */
export const getTrend = (days: number, by: string, s?: AbortSignal) =>
  get<TrendResponse>(`/trend?days=${encodeURIComponent(days)}&by=${encodeURIComponent(by)}`, { signal: s });
export const getLive = (minutes?: number | null, s?: AbortSignal) =>
  get<LiveResponse>(`/live${win(minutes)}`, { signal: s });

export const getLiveUsers = (q: string, minutes?: number | null, s?: AbortSignal, running?: string | null) =>
  get<LiveUsers>(
    `/live/users?q=${encodeURIComponent(q)}&limit=500${running ? `&running=${encodeURIComponent(running)}` : ''}${win(minutes, '&')}`,
    { signal: s },
  );
export const getLiveUser = (name: string, minutes?: number | null, s?: AbortSignal, n = 25) =>
  get<LiveUserDetail>(`/live/user?name=${encodeURIComponent(name)}&n=${n}${win(minutes, '&')}`, { signal: s });

/** Every user with an agent session in the window, and how each works with
 *  their agents (`rc_dashboard/portraits.py`); narrowed by the page's filter. */
export const getUsers = (minutes?: number | null, s?: AbortSignal, w?: Who | null) => {
  const q = (win(minutes, '') + who(w)).replace(/^&/, '');
  return get<UsersResponse>(`/users${q ? '?' + q : ''}`, { signal: s });
};

/** One user's portrait. `name` is what the page shows: an alias, when names are anonymized. */
export const getUser = (name: string, minutes?: number | null, s?: AbortSignal, w?: Who | null) =>
  get<UserPortrait>(`/user?name=${encodeURIComponent(name)}${win(minutes, '&')}${who(w)}`, { signal: s });

export const getTrajectories = (
  rank: TrajRank,
  minutes?: number | null,
  s?: AbortSignal,
  w?: Who | null,
) =>
  get<Trajectories>(
    `/trajectories?rank=${encodeURIComponent(rank)}${win(minutes, '&')}${who(w)}`,
    { signal: s },
  );

/** One session as a process tree. A re-read of the day-files server-side, so
 * it costs seconds on a big session; `from` continues past a read that
 * stopped at its budget, and `to` bounds it -- the page sends "now" to ask for
 * a fresh read instead of the one the service keeps. `order` newest asks for
 * the newest end listed first and drawn in full; oldest is the default, and
 * is left off the URL. */
export const getTrajectory = (
  sid: string,
  minutes?: number | null,
  from?: number | null,
  s?: AbortSignal,
  to?: number | null,
  order: TrajOrder = 'oldest',
) =>
  get<TrajectoryDetail>(
    `/trajectory?sid=${encodeURIComponent(sid)}${win(minutes, '&')}${
      from != null ? `&from=${encodeURIComponent(from)}` : ''
    }${to != null ? `&to=${encodeURIComponent(to)}` : ''}${order === 'newest' ? '&order=newest' : ''}`,
    { signal: s },
  );

/** `ws://`/`wss://` peer of the API origin. */
export function wsUrl(): string {
  const explicit = import.meta.env.VITE_WS_URL as string | undefined;
  if (explicit) return explicit;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

/** Start the lookup of every command behind an I/O number, or find the one
 *  already reading or done for the same question (rc_dashboard/iocmds.py). */
export function startIoCmds(params: URLSearchParams, signal?: AbortSignal): Promise<IoCmdJob> {
  return get<IoCmdJob>(`/io/commands?${params.toString()}`, { signal });
}

/** A lookup's progress, and once it is done, one sorted page of its commands. */
export function getIoCmds(
  id: string,
  page: { offset: number; limit: number; sort: IoSort; dir: 'asc' | 'desc' },
  signal?: AbortSignal,
): Promise<IoCmdJob> {
  const q = new URLSearchParams({
    offset: String(page.offset), limit: String(page.limit), sort: page.sort, dir: page.dir,
  });
  return get<IoCmdJob>(`/io/commands/${encodeURIComponent(id)}?${q.toString()}`, { signal });
}
