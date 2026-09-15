/* Dev-only mock API.
 *
 * This answers `/api/*` and `/ws` from `mock/api.json` inside the Vite dev
 * server, so the app under development exercises the SAME code path it uses in
 * production: `fetch('/api/panels')` and `new WebSocket('/ws')`, over HTTP, with
 * real status codes. There is no mock branch in `src/` and nothing here is
 * bundled -- this module is only ever loaded by `vite.config.ts` in Node.
 *
 * Enabled by default in `vite dev`; set `RC_MOCK=0` (or `--mode real`) to proxy
 * `/api` and `/ws` to a running backend instead.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Duplex } from 'node:stream';
import type { Plugin } from 'vite';

const HERE = fileURLToPath(new URL('.', import.meta.url));

interface Fixture {
  now_epoch: number;
  config: unknown;
  feeds: Record<string, unknown>;
  panels: unknown;
  live: { rate: Record<string, number | null>[]; events: Record<string, unknown>[]; [k: string]: unknown };
  trajectories: Record<string, unknown>;
}

function load(): Fixture {
  return JSON.parse(readFileSync(join(HERE, 'api.json'), 'utf8')) as Fixture;
}

/* ------------------------------------------------------- event filtering */

const NUM_GE: Record<string, string> = {
  depth_min: 'depth',
  cpu_min: 'cpu_s',
  duration_min: 'duration_s',
};
const NUM_LE: Record<string, string> = { depth_max: 'depth' };
const EQ: Record<string, string> = {
  agent_type: 'agent_type',
  user: 'user',
  host: 'host',
  tool: 'tool',
  purpose: 'purpose',
  bucket: 'bucket',
  session_key: 'session_key',
  sandbox: 'sandbox',
  approval: 'approval',
  exit_code: 'exit_code',
  signal: 'signal',
};

/** The same filter semantics for /api/events and /api/export, from one place --
 *  which is exactly the property the real backend has to have too. */
function filterEvents(rows: Record<string, unknown>[], q: URLSearchParams) {
  const classes = q.getAll('class');
  const from = q.get('from');
  const to = q.get('to');
  const text = (q.get('q') || '').toLowerCase();
  return rows.filter((r) => {
    if (classes.length && !classes.includes(String(r.actor3))) return false;
    for (const [k, f] of Object.entries(EQ)) {
      const v = q.get(k);
      if (v != null && v !== '' && String(r[f] ?? '') !== v) return false;
    }
    for (const [k, f] of Object.entries(NUM_GE)) {
      const v = q.get(k);
      if (v != null && v !== '' && !(Number(r[f]) >= Number(v))) return false;
    }
    for (const [k, f] of Object.entries(NUM_LE)) {
      const v = q.get(k);
      if (v != null && v !== '' && !(Number(r[f]) <= Number(v))) return false;
    }
    if (from && String(r.ts ?? '') < from) return false;
    if (to && String(r.ts ?? '') > to) return false;
    if (text) {
      const hay = `${r.comm ?? ''} ${r.args ?? ''} ${r.user ?? ''} ${r.host ?? ''}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });
}

/* Re-stamp the fixture's fixed clock onto the wall clock, so the live tab shows
 * a window that ends now. Values are untouched -- only the timestamps move. */
function freshenLive(fx: Fixture) {
  const nowEpoch = Math.floor(Date.now() / 1000);
  const shift = nowEpoch - fx.now_epoch;
  const live = fx.live as unknown as {
    rate: Record<string, number | null>[];
    events: Record<string, unknown>[];
    window: Record<string, unknown>;
    [k: string]: unknown;
  };
  const rate = live.rate.map((r) => {
    const epoch = Number(r.epoch) + shift;
    return { ...r, epoch, t: new Date(epoch * 1000).toTimeString().slice(0, 5) };
  });
  const events = live.events.map((e) => {
    const epoch = Number(e.epoch) + shift;
    return { ...e, epoch, ts: new Date(epoch * 1000).toTimeString().slice(0, 8) };
  });
  return { ...live, rate, events };
}

/* -------------------------------------------------------------- websocket */

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

/** Minimal RFC 6455 text frame (payloads here are < 64 KiB... and over). */
function frame(text: string): Buffer {
  const body = Buffer.from(text, 'utf8');
  let head: Buffer;
  if (body.length < 126) {
    head = Buffer.from([0x81, body.length]);
  } else if (body.length < 65_536) {
    head = Buffer.alloc(4);
    head[0] = 0x81;
    head[1] = 126;
    head.writeUInt16BE(body.length, 2);
  } else {
    head = Buffer.alloc(10);
    head[0] = 0x81;
    head[1] = 127;
    head.writeBigUInt64BE(BigInt(body.length), 2);
  }
  return Buffer.concat([head, body]);
}

export function mockApi(): Plugin {
  return {
    name: 'rc-mock-api',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        const raw = req.url || '';
        if (!raw.startsWith('/api/')) return next();
        const u = new URL(raw, 'http://localhost');
        const q = u.searchParams;
        const fx = load(); // re-read per request: edit the fixture, reload the page
        const send = (obj: unknown, code = 200) => {
          const body = JSON.stringify(obj);
          res.statusCode = code;
          res.setHeader('content-type', 'application/json; charset=utf-8');
          res.setHeader('cache-control', 'no-store');
          res.end(body);
        };

        switch (u.pathname) {
          case '/api/config':
            return send(fx.config);
          case '/api/feeds':
            return send(fx.feeds);
          case '/api/panels':
            return send(fx.panels);
          case '/api/live':
            return send(freshenLive(fx));
          case '/api/trajectories': {
            const rank = q.get('rank') || 'events';
            return send({ ...fx.trajectories, rank_by: rank });
          }
          case '/api/events': {
            const all = filterEvents(freshenLive(fx).events, q);
            const PAGE = 100;
            const start = Number(q.get('cursor') || 0);
            const rows = all.slice(start, start + PAGE);
            return send({
              rows,
              next_cursor: start + PAGE < all.length ? String(start + PAGE) : null,
              total_scanned: freshenLive(fx).events.length,
              truncated: all.length > 380,
            });
          }
          case '/api/export': {
            const all = filterEvents(freshenLive(fx).events, q);
            res.statusCode = 200;
            res.setHeader('content-type', 'application/x-ndjson; charset=utf-8');
            res.setHeader('content-disposition', 'attachment; filename="events.jsonl"');
            for (const r of all) res.write(JSON.stringify(r) + '\n');
            return res.end();
          }
          default:
            return send({ error: `no mock route for ${u.pathname}` }, 404);
        }
      });

      /* A real websocket, not a stub: the client's socket path is what ships. */
      server.httpServer?.on('upgrade', (req: IncomingMessage, socket: Duplex, head: Buffer) => {
        if (!req.url || new URL(req.url, 'http://localhost').pathname !== '/ws') return;
        void head;
        const key = req.headers['sec-websocket-key'];
        if (typeof key !== 'string') {
          socket.destroy();
          return;
        }
        const accept = createHash('sha1').update(key + WS_GUID).digest('base64');
        socket.write(
          'HTTP/1.1 101 Switching Protocols\r\n' +
            'Upgrade: websocket\r\n' +
            'Connection: Upgrade\r\n' +
            `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
        );

        const push = () => {
          if (socket.destroyed) return;
          const fx = load();
          socket.write(frame(JSON.stringify({ type: 'live', payload: freshenLive(fx) })));
          socket.write(frame(JSON.stringify({ type: 'feeds', payload: fx.feeds })));
        };
        push();
        const iv = setInterval(push, 10_000);
        socket.on('close', () => clearInterval(iv));
        socket.on('error', () => clearInterval(iv));
        // Client frames are masked; we never read them, so drain to keep the
        // socket from back-pressuring.
        socket.resume();
      });

      server.config.logger.info('  ⤷  rc-mock-api: /api/* and /ws served from mock/api.json');
    },
  };
}
