/* The live tier: `WS /ws` with a polling fallback.
 *
 * The socket is authoritative. If it cannot be established (no websocket route,
 * a proxy that drops upgrades, the dev mock server) the hook degrades to polling
 * and says so through `transport`, because a frozen chart that looks live is
 * worse than a chart labelled `polling`. Polling reads what the socket would
 * have sent: `/api/live`, and `/api/windows` and `/api/feeds` on the same beat,
 * so a finished build still reaches the panels (`buildNonce`) and the feeds
 * report stays current. The socket is tried again meanwhile, for as long as the
 * page is open (lib/backoff.ts): at once when the browser comes back online or
 * the tab is shown again, else after a wait that doubles up to 30 s. Only a
 * frame resets that wait: a proxy that accepts the upgrade and closes it at once
 * would otherwise be knocked on every second. An open socket that says nothing
 * for QUIET_MS is closed and replaced, and the page stops saying `live`.
 *
 * Nothing here interpolates. Between frames the series simply does not move --
 * the retired viewer manufactured events between polls and that is the specific
 * behaviour this replaces.
 *
 * A hidden page draws nothing, so it renders nothing: while the tab is in the
 * background each kind of frame is kept, the newest only, and applied when the
 * page is shown again -- one render, not one a second for as long as it was
 * away. The service's clock is still taken from every live frame as it comes.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FeedsResponse, LiveResponse, WindowsResponse, WsFrame } from './types';
import { getFeeds, getLive, getWindows, wsUrl } from './client';
import { setClockPinned, syncServerClock } from '../lib/clock';
import { POLL_AFTER, QUIET_MS, readyKey, retryDelay, spreadOf } from '../lib/backoff';

export type Transport = 'connecting' | 'ws' | 'polling' | 'down';

const POLL_MS = 15000;

export interface LiveState {
  live: LiveResponse | null;
  transport: Transport;
  error: string | null;
  /** epoch ms of the last accepted frame or poll, for the staleness readout. */
  receivedAt: number | null;
  /** monotonically increasing: bumps when the backend says it rebuilt panels. */
  rebuildNonce: number;
  feeds: FeedsResponse | null;
  backfill: unknown;
  /** The window registry: what is offered, and what is building right now. */
  windows: WindowsResponse | null;
  /** Bumps when the window this page is viewing finishes a (re)build. */
  buildNonce: number;
  /** Read the live frame, the windows and the feeds again, now. */
  refresh: () => void;
}

export function useLive(windowMin: number | null): LiveState {
  const [live, setLive] = useState<LiveResponse | null>(null);
  const [feeds, setFeeds] = useState<FeedsResponse | null>(null);
  const [backfill, setBackfill] = useState<unknown>(null);
  const [windows, setWindows] = useState<WindowsResponse | null>(null);
  const [transport, setTransport] = useState<Transport>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [rebuildNonce, setRebuildNonce] = useState(0);
  const [buildNonce, setBuildNonce] = useState(0);

  const sock = useRef<WebSocket | null>(null);
  /** failed socket attempts since the last frame */
  const attempts = useRef(0);
  const dead = useRef(false);
  /* The window lives in a ref as well as in state so `poll` stays referentially
   * stable. If it were a dependency the whole socket would tear down and
   * reconnect on every window change -- losing the transport, the backoff count
   * and the feeds frame to redraw a chart the server can just re-send. */
  const win = useRef(windowMin);
  win.current = windowMin;
  /* The set of ready windows and their build stamps, as last seen. Panels are
   * re-fetched only when this changes -- progress frames must not trigger one. */
  const builtKey = useRef<string>('');
  /* Frames that came while the page was hidden: the newest of each kind, with
   * when it came, in the order the newest ones came. */
  const held = useRef(new Map<WsFrame['type'], { f: WsFrame; at: number }>());
  /* The REST read that seeds the page is made once per mount (StrictMode runs
   * the effect twice and keeps refs), and once more on each reconnect: a
   * socket that dropped may have missed frames. */
  const seeded = useRef(false);
  const reseed = useRef(false);

  /** A frame's effect on the page's state. */
  const apply = useCallback((frame: WsFrame, at: number) => {
    switch (frame.type) {
      case 'live': {
        const l = frame.payload;
        // Every frame says whether the clock is pinned; one that does not (an older
        // service) is the wall clock, which does grow.
        setClockPinned(l.clock?.pinned === true);
        setLive(l);
        setReceivedAt(at);
        setError(null);
        break;
      }
      case 'feeds':
        setFeeds(frame.payload);
        break;
      case 'rebuilt':
        setRebuildNonce((n) => n + 1);
        break;
      case 'backfill':
        setBackfill(frame.payload);
        break;
      case 'windows': {
        setWindows(frame.payload);
        // Re-fetch panels when a READY window's reducers are swapped in. Keyed on
        // the whole ready set rather than on this page's own window, because
        // `win` is null whenever the page is on the service default and
        // would never match a held window's minutes -- the first build to
        // finish would then never reach the page. Progress frames arrive
        // every couple of seconds and must NOT trigger a fetch, which is
        // why only `state === 'ready'` rows are in the key. `installed_at`,
        // not just `built_at`: a window resumed from a saved build keeps that
        // build's stamp, so across a service restart `built_at` alone would
        // match the key from before it and the page would never re-fetch.
        // A poll's `/api/windows` answer comes this way too.
        const key = readyKey(frame.payload.held);
        if (key && key !== builtKey.current) {
          builtKey.current = key;
          setBuildNonce((n) => n + 1);
        }
        break;
      }
    }
  }, []);

  /** A frame as it arrives: applied now, or kept for when the page is shown. */
  const take = useCallback(
    (frame: WsFrame) => {
      // the offset to the service's clock is right only when taken as the frame comes
      if (frame.type === 'live') syncServerClock(frame.payload.clock?.now);
      const at = Date.now();
      if (document.visibilityState === 'hidden') {
        held.current.delete(frame.type);
        held.current.set(frame.type, { f: frame, at });
        return;
      }
      apply(frame, at);
    },
    [apply],
  );

  // Shown again: what came while hidden, in one render.
  useEffect(() => {
    const shown = () => {
      if (document.visibilityState === 'hidden' || !held.current.size) return;
      const frames = [...held.current.values()];
      held.current.clear();
      for (const { f, at } of frames) apply(f, at);
    };
    document.addEventListener('visibilitychange', shown);
    return () => document.removeEventListener('visibilitychange', shown);
  }, [apply]);

  /** `/api/live`; with `all`, `/api/windows` and `/api/feeds` too, as the frames
   *  the socket would have sent. A failed read says `down` unless the socket is up. */
  const poll = useCallback(async (all = false) => {
    const more = all ? Promise.allSettled([getWindows(), getFeeds()]) : null;
    try {
      take({ type: 'live', payload: await getLive(win.current) });
      setTransport((t) => (t === 'ws' ? t : 'polling'));
    } catch (e) {
      setTransport((t) => (t === 'ws' ? t : 'down'));
      setError(e instanceof Error ? e.message : String(e));
    }
    if (more) {
      const [w, f] = await more;
      if (dead.current) return;
      if (w.status === 'fulfilled') take({ type: 'windows', payload: w.value });
      if (f.status === 'fulfilled') take({ type: 'feeds', payload: f.value });
    }
  }, [take]);

  /* one-shot manual refresh, used by the header button: everything the socket sends */
  const refresh = useCallback(() => {
    void poll(true);
  }, [poll]);

  useEffect(() => {
    dead.current = false;
    // what spreads this page's waits from another's: when it opened
    const seed = typeof performance !== 'undefined' ? performance.timeOrigin : 0;
    let retry: number | null = null;
    let quiet: number | null = null;
    let next: number | null = null;
    let polling = false;

    const stopRetry = () => {
      if (retry != null) window.clearTimeout(retry);
      retry = null;
    };
    const stopQuiet = () => {
      if (quiet != null) window.clearTimeout(quiet);
      quiet = null;
    };
    const stopPolling = () => {
      polling = false;
      if (next != null) window.clearTimeout(next);
      next = null;
    };
    const startPolling = () => {
      if (polling) return;
      polling = true;
      const loop = async () => {
        next = null;
        if (dead.current || !polling) return;
        await poll(true);
        if (!dead.current && polling) next = window.setTimeout(loop, POLL_MS);
      };
      void loop();
    };
    /** a socket let go: no handler of it runs again */
    const drop = (ws: WebSocket) => {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    };

    /** The socket closed, would not open, or went quiet: poll once it has
     *  failed POLL_AFTER times in a row, and try it again after a wait. */
    const lost = () => {
      sock.current = null;
      stopQuiet();
      if (dead.current) return;
      reseed.current = true;
      attempts.current += 1;
      if (attempts.current >= POLL_AFTER) startPolling();
      else if (!polling) setTransport('connecting');
      stopRetry();
      retry = window.setTimeout(connect, retryDelay(attempts.current, spreadOf(seed, attempts.current)));
    };

    const connect = () => {
      retry = null;
      if (dead.current || sock.current) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        lost();
        return;
      }
      sock.current = ws;
      let heard = false;
      // re-armed by every frame: a socket that says nothing for QUIET_MS is dead
      const watch = () => {
        stopQuiet();
        quiet = window.setTimeout(() => {
          quiet = null;
          if (sock.current !== ws) return;
          drop(ws);
          lost();
        }, QUIET_MS);
      };

      ws.onopen = () => {
        watch();
        // Tell the server which window this reader is on before anything else:
        // it sends one `live` frame per DISTINCT window, and until it hears
        // otherwise this socket is counted on the service default.
        if (win.current != null) {
          try {
            ws.send(JSON.stringify({ type: 'window', minutes: win.current }));
          } catch {
            /* the frame is re-sent by the window effect once the socket settles */
          }
        }
      };

      ws.onmessage = (ev) => {
        watch();
        if (!heard) {
          // The socket works: its first frame, not its opening, starts the
          // backoff over and ends the polling.
          heard = true;
          attempts.current = 0;
          stopPolling();
          setTransport('ws');
          setError(null);
          // The socket may only push deltas; the view was seeded from the REST
          // snapshot as the page mounted, so a quiet cluster is not an empty
          // dashboard. A socket that came back after dropping seeds it again.
          if (reseed.current) {
            reseed.current = false;
            void poll();
          }
        }
        let frame: WsFrame;
        try {
          frame = JSON.parse(ev.data as string) as WsFrame;
        } catch {
          return; // a malformed frame is dropped, not guessed at
        }
        switch (frame.type) {
          case 'live':
          case 'feeds':
          case 'rebuilt':
          case 'backfill':
          case 'windows':
            take(frame);
            break;
        }
      };

      ws.onclose = () => {
        if (sock.current !== ws) return;
        drop(ws);
        lost();
      };

      ws.onerror = () => {
        /* onclose always follows; the retry/backoff decision lives there. */
      };
    };

    /** Try the socket now, rather than at the end of its wait: the browser is
     *  back online, or the tab is on screen again. Not while one is open or
     *  opening. */
    const now = () => {
      if (dead.current || sock.current) return;
      stopRetry();
      connect();
    };
    const shown = () => {
      if (document.visibilityState !== 'hidden') now();
    };
    window.addEventListener('online', now);
    document.addEventListener('visibilitychange', shown);

    connect();
    // Seed immediately so the first paint is data, not a spinner, even if the
    // socket handshake is slow. Once: the socket's opening and the window
    // effect below used to read /api/live again, three reads at every start.
    if (!seeded.current) {
      seeded.current = true;
      void poll();
    }

    return () => {
      dead.current = true;
      stopRetry();
      stopQuiet();
      stopPolling();
      window.removeEventListener('online', now);
      document.removeEventListener('visibilitychange', shown);
      const s = sock.current;
      sock.current = null;
      if (s) drop(s);
    };
  }, [poll, take]);

  /* A window change re-uses the open socket. The server answers with a frame
   * for the new window immediately, so the chart never keeps showing the old
   * span while the picker already reads the new one. Not on mount: the seed
   * above reads the window the page opened on, and the socket names it to the
   * server as it opens. */
  const shownWin = useRef(windowMin);
  useEffect(() => {
    if (shownWin.current === windowMin) return;
    shownWin.current = windowMin;
    const s = sock.current;
    if (s && s.readyState === WebSocket.OPEN) {
      try {
        s.send(JSON.stringify({ type: 'window', minutes: windowMin }));
        return;
      } catch {
        /* fall through to the REST read */
      }
    }
    void poll();
  }, [windowMin, poll]);

  // one object for as long as nothing in it changes: a memoized tab handed it
  // is drawn again for a new frame, not for every render of App
  return useMemo(
    () => ({ live, transport, error, receivedAt, rebuildNonce, feeds, backfill, windows, buildNonce, refresh }),
    [live, transport, error, receivedAt, rebuildNonce, feeds, backfill, windows, buildNonce, refresh],
  );
}
