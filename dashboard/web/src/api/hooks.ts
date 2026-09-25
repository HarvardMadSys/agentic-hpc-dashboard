/* Data hooks. One `Async<T>` shape everywhere so the shell can render a
 * consistent loading / error state without every tab reinventing it.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ConfigTree,
  FeedsResponse,
  PanelsResponse,
  TrajRank,
  Trajectories,
  WindowsResponse,
} from './types';
import { getConfig, getFeeds, getPanels, getTrajectories, getWindows } from './client';

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

/* The window is a dependency, not a filter applied after the fetch: the
 * historical tier is REDUCED per window server-side, so changing it is a new
 * request, and `buildNonce` re-requests when that build finishes. */
export const usePanels = (win: number | null, buildNonce = 0): Async<PanelsResponse> =>
  useFetched((s) => getPanels(win, s), [win, buildNonce]);

export const useTrajectories = (
  rank: TrajRank,
  win: number | null,
  buildNonce = 0,
): Async<Trajectories> =>
  useFetched((s) => getTrajectories(rank, win, s), [rank, win, buildNonce]);

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
