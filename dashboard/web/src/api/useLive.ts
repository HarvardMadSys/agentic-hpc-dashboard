/* The live tier: `WS /ws` with a polling fallback.
 *
 * The socket is authoritative. If it cannot be established (no websocket route,
 * a proxy that drops upgrades, the dev mock server) the hook degrades to polling
 * `GET /api/live` and says so through `transport`, because a frozen chart that
 * looks live is worse than a chart labelled `polling`.
 *
 * Nothing here interpolates. Between frames the series simply does not move --
 * the retired viewer manufactured events between polls and that is the specific
 * behaviour this replaces.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedsResponse, LiveResponse, WsFrame } from './types';
import { getLive, wsUrl } from './client';

export type Transport = 'connecting' | 'ws' | 'polling' | 'down';

const POLL_MS = 15000;
const WS_RETRY_MS = [1000, 2000, 5000, 10000, 20000];
/** After this many failed socket attempts, stop retrying and just poll. */
const WS_GIVE_UP = 4;

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
  refresh: () => void;
}

export function useLive(): LiveState {
  const [live, setLive] = useState<LiveResponse | null>(null);
  const [feeds, setFeeds] = useState<FeedsResponse | null>(null);
  const [backfill, setBackfill] = useState<unknown>(null);
  const [transport, setTransport] = useState<Transport>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [rebuildNonce, setRebuildNonce] = useState(0);

  const sock = useRef<WebSocket | null>(null);
  const timer = useRef<number | null>(null);
  const attempts = useRef(0);
  const dead = useRef(false);

  const accept = useCallback((l: LiveResponse) => {
    setLive(l);
    setReceivedAt(Date.now());
    setError(null);
  }, []);

  const poll = useCallback(async () => {
    try {
      accept(await getLive());
      setTransport((t) => (t === 'ws' ? t : 'polling'));
    } catch (e) {
      setTransport('down');
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [accept]);

  /* one-shot manual refresh, used by the header button */
  const refresh = useCallback(() => {
    void poll();
  }, [poll]);

  useEffect(() => {
    dead.current = false;

    const clear = () => {
      if (timer.current != null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const startPolling = () => {
      clear();
      const loop = async () => {
        if (dead.current) return;
        await poll();
        if (!dead.current) timer.current = window.setTimeout(loop, POLL_MS);
      };
      void loop();
    };

    const connect = () => {
      if (dead.current) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        startPolling();
        return;
      }
      sock.current = ws;

      ws.onopen = () => {
        attempts.current = 0;
        clear();
        setTransport('ws');
        setError(null);
        // The socket may only push deltas; seed the view once from the REST
        // snapshot so a quiet cluster is not an empty dashboard.
        void poll();
      };

      ws.onmessage = (ev) => {
        let frame: WsFrame;
        try {
          frame = JSON.parse(ev.data as string) as WsFrame;
        } catch {
          return; // a malformed frame is dropped, not guessed at
        }
        switch (frame.type) {
          case 'live':
            accept(frame.payload);
            break;
          case 'feeds':
            setFeeds(frame.payload);
            break;
          case 'rebuilt':
            setRebuildNonce((n) => n + 1);
            break;
          case 'backfill':
            setBackfill(frame.payload);
            break;
        }
      };

      ws.onclose = () => {
        sock.current = null;
        if (dead.current) return;
        attempts.current += 1;
        if (attempts.current > WS_GIVE_UP) {
          startPolling();
          return;
        }
        setTransport('connecting');
        const wait = WS_RETRY_MS[Math.min(attempts.current - 1, WS_RETRY_MS.length - 1)];
        timer.current = window.setTimeout(connect, wait);
      };

      ws.onerror = () => {
        /* onclose always follows; the retry/backoff decision lives there. */
      };
    };

    connect();
    // Seed immediately so the first paint is data, not a spinner, even if the
    // socket handshake is slow.
    void poll();

    return () => {
      dead.current = true;
      clear();
      const s = sock.current;
      sock.current = null;
      if (s) {
        s.onclose = null;
        s.close();
      }
    };
  }, [accept, poll]);

  return { live, transport, error, receivedAt, rebuildNonce, feeds, backfill, refresh };
}
