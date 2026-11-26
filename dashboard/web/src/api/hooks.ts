/* Data hooks. One `Async<T>` shape everywhere so the shell can render a
 * consistent loading / error state without every tab reinventing it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  CollectorsResponse,
  ConfigTree,
  FeedsResponse,
  PanelsResponse,
  StatusDoc,
  TrajOrder,
  TrajRank,
  Trajectories,
  TrajectoryDetail,
  TrendResponse,
  UserPortrait,
  UsersResponse,
  WindowsResponse,
} from './types';
import {
  getCollectors,
  getConfig,
  getFeeds,
  getPanels,
  getStatus,
  getTrajectories,
  getTrajectory,
  getTrend,
  getUser,
  getUsers,
  getWindows,
} from './client';
import { whoQuery } from '../lib/filters';
import type { Who } from '../lib/filters';

export interface Async<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

function useFetched<T>(fn: (s?: AbortSignal) => Promise<T>, deps: unknown[]): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const ac = new AbortController();
    let live = true;
    setLoading(true);
    fnRef
      .current(ac.signal)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!live || ac.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
      ac.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // One object for as long as nothing in it changes, so a memoized component
  // handed it is not re-rendered by every render of its parent.
  return useMemo(() => ({ data, error, loading, reload }), [data, error, loading, reload]);
}

export const useConfig = (): Async<ConfigTree> => useFetched(getConfig, []);

/** Each collector's state, re-read `everyMs` after each answer while the
 *  caller is mounted and the page is on screen. On a fixed beat, a reply
 *  slower than the beat was cancelled by the next request and never arrived. */
export function useCollectors(everyMs = 20000): Async<CollectorsResponse> {
  const a = useFetched(getCollectors, []);
  useRefresh(a, everyMs);
  return a;
}

/** Is the collection working (`/api/status`), for Summary: asked once when it
 *  opens and re-read `everyMs` after each answer while it is on screen. A
 *  collector's heartbeat is a minute apart, so a minute loses nothing. */
export function useStatus(everyMs: number | null = 60000): Async<StatusDoc> {
  const a = useFetched(getStatus, []);
  useRefresh(a, everyMs);
  return a;
}

/* The window is a dependency, not a filter applied after the fetch: the
 * historical tier is REDUCED per window server-side, so changing it is a new
 * request, and `buildNonce` re-requests when that build finishes. The user and
 * agent-type filter is the same kind of thing -- its own reduced view -- and is
 * a dependency by its query string, so a new array with the same values is not
 * a new request.
 *
 * The Network bytes panel's endpoint names are looked up in the background
 * after the service first lists the addresses (rc_dashboard/endpoints.py), so
 * while some are pending the panels are read again, later each time, and only
 * a few times: a name still missing after that waits for the next build. Not
 * while the page is hidden: each is the whole of /api/panels again. */
const NAME_REREADS_MS = [3000, 6000, 12000, 24000, 48000];

export function usePanels(
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
): Async<PanelsResponse> {
  const view = `${win}|${who ? whoQuery(who) : ''}`;
  const a = useFetched((s) => getPanels(win, s, who), [win, buildNonce, view]);
  const { data, loading, reload } = a;
  const visible = useVisible();
  const pending = (data?.panels?.io_process?.endpoints as { pending?: number } | undefined)?.pending ?? 0;
  const rereads = useRef({ view, n: 0 });
  useEffect(() => {
    const r = rereads.current;
    if (r.view !== view || !pending) rereads.current = { view, n: 0 };
    const n = rereads.current.n;
    // not while a read is out: its answer may hold the names, or be another view's
    if (!pending || loading || !visible || n >= NAME_REREADS_MS.length) return;
    const t = setTimeout(() => {
      rereads.current.n = n + 1;
      reload();
    }, NAME_REREADS_MS[n]);
    return () => clearTimeout(t);
  }, [data, loading, pending, reload, view, visible]);
  return a;
}

/** Whether the page is on screen: a hidden tab polls nothing. */
function useVisible(): boolean {
  const [v, setV] = useState(() => document.visibilityState !== 'hidden');
  useEffect(() => {
    const on = () => setV(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return v;
}

/* `liveMs` re-reads the ranked sessions that long after each answer, while
 * the page is on screen: the service re-ranks them from the live feed
 * (`reducers.trajectories.live_s`), so a running session's counts move and
 * it climbs the recency order as it works. Timed from the last answer, not
 * on a fixed beat, so a slow reply is never cut off by the next request. */
export function useTrajectories(
  rank: TrajRank,
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
  liveMs: number | null = null,
): Async<Trajectories> {
  const a = useFetched((s) => getTrajectories(rank, win, s, who), [
    rank,
    win,
    buildNonce,
    who ? whoQuery(who) : '',
  ]);
  const visible = useVisible();
  const { reload, loading, data, error } = a;
  useEffect(() => {
    if (!liveMs || !visible || loading) return;
    const t = window.setTimeout(reload, liveMs);
    return () => clearTimeout(t);
  }, [liveMs, visible, loading, reload, data, error]);
  return a;
}

/** `reload` that long after each answer, while the page is on screen: timed
 *  from the last answer, as `useTrajectories` times its re-reads. */
function useRefresh<T>(a: Async<T>, everyMs: number | null): void {
  const visible = useVisible();
  const { reload, loading, data, error } = a;
  useEffect(() => {
    if (!everyMs || !visible || loading) return;
    const t = window.setTimeout(reload, everyMs);
    return () => clearTimeout(t);
  }, [everyMs, visible, loading, reload, data, error]);
}

/** The Users tab's roster: every user with an agent session in the window,
 *  narrowed by the page's filter as the panels are. Re-read every `everyMs`
 *  while it is on screen, so "running now" and the counts follow the feed. */
export function useUsers(
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
  everyMs: number | null = null,
): Async<UsersResponse> {
  const a = useFetched((s) => getUsers(win, s, who), [win, buildNonce, who ? whoQuery(who) : '']);
  useRefresh(a, everyMs);
  return a;
}

/** The Trend tab's roll-up: `days` back in `by` buckets, read again every
 *  `everyMs` while on screen (the table is written a minute at a time). */
export function useTrend(days: number, by: string, everyMs: number | null = null): Async<TrendResponse> {
  const a = useFetched((s) => getTrend(days, by, s), [days, by]);
  useRefresh(a, everyMs);
  return a;
}

/** One user's portrait; `null` data, and no request, when no user is open. */
export function useUserPortrait(
  name: string | null,
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
  everyMs: number | null = null,
): Async<UserPortrait | null> {
  const a = useFetched((s) => (name ? getUser(name, win, s, who) : Promise.resolve(null)), [
    name,
    win,
    buildNonce,
    who ? whoQuery(who) : '',
  ]);
  useRefresh(a, name ? everyMs : null);
  return a;
}

/** The inspector's process tree; `null` data, and no request, when nothing is
 *  selected. Not re-read on every window rebuild: the read is the expensive
 *  part, and the service keeps recent trees for ten minutes -- `to` (now)
 *  asks for a fresh read past that. */
export const useTrajectory = (
  sid: string | null,
  win: number | null,
  from: number | null,
  to: number | null = null,
  order: TrajOrder = 'oldest',
): Async<TrajectoryDetail | null> =>
  useFetched((s) => (sid ? getTrajectory(sid, win, from, s, to, order) : Promise.resolve(null)), [
    sid,
    win,
    from,
    to,
    order,
  ]);

/** `/api/feeds` also arrives over the websocket; `seed` lets the WS overwrite it. */
export function useFeeds(): Async<FeedsResponse> & { merge: (f: FeedsResponse) => void } {
  const base = useFetched(getFeeds, []);
  const [override, setOverride] = useState<FeedsResponse | null>(null);
  const merge = useCallback((f: FeedsResponse) => setOverride(f), []);
  return useMemo(() => ({ ...base, data: override ?? base.data, merge }), [base, override, merge]);
}

/** The offered windows and the retention ceiling, seeded over REST.
 *
 * The websocket supersedes this with live build progress; the REST read exists
 * so the picker is populated before the socket is up, and stays populated if it
 * never comes up at all.
 */
export const useWindows = (): Async<WindowsResponse> => useFetched(getWindows, []);
