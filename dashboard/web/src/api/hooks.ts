/* Data hooks. One `Async<T>` shape everywhere so the shell can render a
 * consistent loading / error state without every tab reinventing it.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  CollectorsResponse,
  ConfigTree,
  FeedsResponse,
  PanelsResponse,
  TrajRank,
  Trajectories,
  TrajectoryDetail,
  WindowsResponse,
} from './types';
import {
  getCollectors,
  getConfig,
  getFeeds,
  getPanels,
  getTrajectories,
  getTrajectory,
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
  return { data, error, loading, reload };
}

export const useConfig = (): Async<ConfigTree> => useFetched(getConfig, []);

/** Each collector's state, re-read every `everyMs` while the caller is mounted. */
export function useCollectors(everyMs = 20000): Async<CollectorsResponse> {
  const a = useFetched(getCollectors, []);
  const { reload } = a;
  useEffect(() => {
    const t = setInterval(reload, everyMs);
    return () => clearInterval(t);
  }, [reload, everyMs]);
  return a;
}

/* The window is a dependency, not a filter applied after the fetch: the
 * historical tier is REDUCED per window server-side, so changing it is a new
 * request, and `buildNonce` re-requests when that build finishes. The user and
 * agent-type filter is the same kind of thing -- its own reduced view -- and is
 * a dependency by its query string, so a new array with the same values is not
 * a new request. */
export const usePanels = (
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
): Async<PanelsResponse> =>
  useFetched((s) => getPanels(win, s, who), [win, buildNonce, who ? whoQuery(who) : '']);

export const useTrajectories = (
  rank: TrajRank,
  win: number | null,
  buildNonce = 0,
  who: Who | null = null,
): Async<Trajectories> =>
  useFetched((s) => getTrajectories(rank, win, s, who), [
    rank,
    win,
    buildNonce,
    who ? whoQuery(who) : '',
  ]);

/** The inspector's process tree; `null` data, and no request, when nothing is
 *  selected. Not re-read on every window rebuild: the read is the expensive
 *  part, and the service keeps recent trees for ten minutes -- `to` (now)
 *  asks for a fresh read past that. */
export const useTrajectory = (
  sid: string | null,
  win: number | null,
  from: number | null,
  to: number | null = null,
): Async<TrajectoryDetail | null> =>
  useFetched((s) => (sid ? getTrajectory(sid, win, from, s, to) : Promise.resolve(null)), [
    sid,
    win,
    from,
    to,
  ]);

/** `/api/feeds` also arrives over the websocket; `seed` lets the WS overwrite it. */
export function useFeeds(): Async<FeedsResponse> & { merge: (f: FeedsResponse) => void } {
  const base = useFetched(getFeeds, []);
  const [override, setOverride] = useState<FeedsResponse | null>(null);
  const merge = useCallback((f: FeedsResponse) => setOverride(f), []);
  return { ...base, data: override ?? base.data, merge };
}

/** The offered windows and the retention ceiling, seeded over REST.
 *
 * The websocket supersedes this with live build progress; the REST read exists
 * so the picker is populated before the socket is up, and stays populated if it
 * never comes up at all.
 */
export const useWindows = (): Async<WindowsResponse> => useFetched(getWindows, []);
