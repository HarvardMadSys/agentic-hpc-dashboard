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
  EventsResponse,
  FeedsResponse,
  LiveResponse,
  LiveUserDetail,
  AskContext,
  AskInfo,
  AskJob,
  LiveUsers,
  PanelsResponse,
  TrajRank,
  Trajectories,
  TrajectoryDetail,
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

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const url = API_BASE + path;
  const r = await fetch(url, { cache: 'no-store', ...init });
  if (!r.ok) {
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
export const getLive = (minutes?: number | null, s?: AbortSignal) =>
  get<LiveResponse>(`/live${win(minutes)}`, { signal: s });

export const getLiveUsers = (q: string, minutes?: number | null, s?: AbortSignal, running?: string | null) =>
  get<LiveUsers>(
    `/live/users?q=${encodeURIComponent(q)}&limit=500${running ? `&running=${encodeURIComponent(running)}` : ''}${win(minutes, '&')}`,
    { signal: s },
  );
export const getAskInfo = (s?: AbortSignal) => get<AskInfo>('/ask', { signal: s });
export const getAskJob = (id: string, s?: AbortSignal) =>
  get<AskJob>(`/ask/${encodeURIComponent(id)}`, { signal: s });
/** Start a question; the answer is polled with getAskJob. `context` narrows it
 *  to one panel or one session; `w`, the page's filter, rides in the query
 *  string as on /panels, so a panel's question reads the view on screen. */
export async function postAsk(
  question: string,
  minutes: number | null | undefined,
  history: { q: string; a: string }[],
  context?: AskContext,
  w?: Who | null,
): Promise<AskJob> {
  const q = who(w).replace(/^&/, '');
  const r = await fetch(API_BASE + '/ask' + (q ? '?' + q : ''), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ question, window_min: minutes ?? null, history, context: context ?? null }),
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = ((await r.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* not JSON: the status is the message */
    }
    throw new ApiError(API_BASE + '/ask', r.status, detail);
  }
  return (await r.json()) as AskJob;
}

export const getLiveUser = (name: string, minutes?: number | null, s?: AbortSignal, n = 25) =>
  get<LiveUserDetail>(`/live/user?name=${encodeURIComponent(name)}&n=${n}${win(minutes, '&')}`, { signal: s });

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
 * a fresh read instead of the one the service keeps. */
export const getTrajectory = (
  sid: string,
  minutes?: number | null,
  from?: number | null,
  s?: AbortSignal,
  to?: number | null,
) =>
  get<TrajectoryDetail>(
    `/trajectory?sid=${encodeURIComponent(sid)}${win(minutes, '&')}${
      from != null ? `&from=${encodeURIComponent(from)}` : ''
    }${to != null ? `&to=${encodeURIComponent(to)}` : ''}`,
    { signal: s },
  );

export function getEvents(query: string, cursor?: number | string | null, s?: AbortSignal) {
  const q = cursor ? `${query}${query ? '&' : ''}cursor=${encodeURIComponent(cursor)}` : query;
  return get<EventsResponse>(`/events${q ? '?' + q : ''}`, { signal: s });
}

/** The export URL. Identical query string to the events request by construction. */
export const exportUrl = (query: string) => `${API_BASE}/export${query ? '?' + query : ''}`;

/** `ws://`/`wss://` peer of the API origin. */
export function wsUrl(): string {
  const explicit = import.meta.env.VITE_WS_URL as string | undefined;
  if (explicit) return explicit;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}
