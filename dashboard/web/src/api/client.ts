/* Same-origin HTTP client for the rc_dashboard API.
 *
 * There is no mock branch in here on purpose: in development the Vite dev server
 * answers `/api/*` from `web/mock/` (see `web/mock/plugin.ts`), so the code path
 * exercised in dev is byte-for-byte the production path. Nothing under `src/`
 * ever synthesises a value.
 */
import type {
  ConfigTree,
  EventsResponse,
  FeedsResponse,
  LiveResponse,
  PanelsResponse,
  TrajRank,
  Trajectories,
} from './types';

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

export const getConfig = (s?: AbortSignal) => get<ConfigTree>('/config', { signal: s });
export const getFeeds = (s?: AbortSignal) => get<FeedsResponse>('/feeds', { signal: s });
export const getPanels = (s?: AbortSignal) => get<PanelsResponse>('/panels', { signal: s });
export const getLive = (s?: AbortSignal) => get<LiveResponse>('/live', { signal: s });

export const getTrajectories = (rank: TrajRank, s?: AbortSignal) =>
  get<Trajectories>(`/trajectories?rank=${encodeURIComponent(rank)}`, { signal: s });

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
