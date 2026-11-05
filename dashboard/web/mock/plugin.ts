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
  trajectory_detail?: Record<string, unknown>;
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

/* ------------------------------------------------------------- windowing
 *
 * The mock slices the same way the service does -- the fixture holds one bin
 * per minute, so a window is its last N rows -- because the point of this
 * plugin is that dev exercises the shipping code path. A picker that only
 * worked against the real backend would be untested until deploy.
 */
const MOCK_RETENTION_MIN = 1440; // the fixture holds 1440 one-minute bins
const MOCK_PRESETS = [60, 360, 720, 1440, 4320, 10080];
const MIN_WINDOW_MIN = 5;

function label(m: number): string {
  if (m % 1440 === 0 && m >= 2880) return `${m / 1440}d`;
  if (m % 60 === 0) return `${m / 60}h`;
  return `${m}m`;
}

function presets() {
  return MOCK_PRESETS.filter((m) => m >= MIN_WINDOW_MIN && m <= MOCK_RETENTION_MIN).map((m) => ({
    minutes: m,
    label: label(m),
  }));
}

/** Resolve `window_min` exactly as the backend does: clamp, and SAY so. */
function resolveWindow(q: URLSearchParams) {
  const raw = q.get('window_min');
  const def = Math.min(1440, MOCK_RETENTION_MIN);
  const want = raw == null || raw === '' ? NaN : Number(raw);
  if (!Number.isFinite(want)) {
    return { minutes: def, requested: null, clamped: false, note: null,
             retention_minutes: MOCK_RETENTION_MIN, label: label(def) };
  }
  const n = Math.floor(want);
  if (n < MIN_WINDOW_MIN) {
    return { minutes: MIN_WINDOW_MIN, requested: n, clamped: true,
             note: `below the ${MIN_WINDOW_MIN}-minute floor`,
             retention_minutes: MOCK_RETENTION_MIN, label: label(MIN_WINDOW_MIN) };
  }
  if (n > MOCK_RETENTION_MIN) {
    return { minutes: MOCK_RETENTION_MIN, requested: n, clamped: true,
             note: `beyond retention (live.retention_min=${MOCK_RETENTION_MIN}); the process never held those bins`,
             retention_minutes: MOCK_RETENTION_MIN, label: label(MOCK_RETENTION_MIN) };
  }
  return { minutes: n, requested: n, clamped: false, note: null,
           retention_minutes: MOCK_RETENTION_MIN, label: label(n) };
}

/* ------------------------------------------------------ the inspector's read */

// A hand-built payload, shifted as it comes: its shape is the service's, which `src/api/types.ts` types.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

/** Every `start` and `end` of a tree, `delta` seconds on. */
function shiftTimes(v: Json, delta: number): Json {
  if (Array.isArray(v)) return v.map((x) => shiftTimes(x, delta));
  if (v && typeof v === 'object') {
    const out: Json = {};
    for (const [k, x] of Object.entries(v)) {
      out[k] = (k === 'start' || k === 'end') && typeof x === 'number' ? x + delta : shiftTimes(x, delta);
    }
    return out;
  }
  return v;
}

/** The navigator's times, `delta` seconds on. */
function shiftNav(nav: Json, delta: number): Json {
  const on = (t: number | null) => (t == null ? null : t + delta);
  const mark = (m: Json) => m && { ...m, t: on(m.t), end: on(m.end) };
  return {
    ...nav,
    density: nav.density && { ...nav.density, lo: nav.density.lo + delta },
    busiest: nav.busiest && { ...nav.busiest, t: nav.busiest.t + delta },
    first_tool: mark(nav.first_tool),
    longest: mark(nav.longest),
    failures: {
      ...nav.failures,
      items: nav.failures.items.map((f: Json[]) => [on(f[0]), on(f[1]), ...f.slice(2)]),
    },
  };
}

/** The one hand-built tree, read as the service reads a session: from its first record, or from `from` where the
 *  inspector pages, for as long as the tree runs (the service's budget) or until the session's last record. The
 *  session's own span is the row's, so the session track, the chart's phases, and the list agree about when it ran,
 *  and a long session stops at its budget with the rest unread. A short one leaves the tree's later processes
 *  past the read's end, off the axis. */
function alignDetail(d: Json, row: Json, from: number | null): Json {
  const lo: number = d.timeline.lo;
  const hi: number = d.timeline.hi;
  const first: number = row.first ?? lo;
  const last: number = row.last ?? hi;
  const start = from != null ? Math.min(Math.max(first, from), last) : first;
  const end = Math.min(last, start + (hi - lo));
  const stopped = end < last - 1;
  const delta = start - lo;
  return {
    ...d,
    tree: shiftTimes(d.tree, delta),
    nav: d.nav && shiftNav(d.nav, delta),
    span: { first, last },
    scan: {
      ...d.scan,
      from: start,
      first: start,
      last: end,
      to: end + 1,
      stopped: stopped ? 'records' : null,
      stopped_at: stopped ? end : null,
      // a page is a read of its own: the inspector waits for one whose stamp differs
      computed_at: d.scan.computed_at + (from != null ? (Math.floor(from) % 997) + 1 : 0),
    },
    timeline: { lo: start, hi: end },
  };
}

function windowStatus(minutes: number) {
  const now = new Date();
  const stamp = (d: Date) => d.toISOString().slice(0, 19).replace('T', ' ');
  return {
    minutes, label: label(minutes), state: 'ready' as const, error: null,
    has_payload: true,
    progress: { pct: 100, records: 0, bytes: 0, total_bytes: 0, files: 0, elapsed_s: 0 },
    built_at: stamp(now), built_age_s: 0,
    covers_from: stamp(new Date(now.getTime() - minutes * 60_000)),
    covers_to: stamp(now),
    drift_s: 0, drift_budget_s: minutes * 60 * 0.25,
    live_records: 0, replay_dropped: 0,
  };
}

/* The server's present, as every live frame says it (rc_dashboard/clock.py), for
 * the page's own "now" (src/lib/clock.ts) to follow.
 *
 * The fixture's trajectory rows -- a session's first and last record, its
 * phases -- all sit at the fixture's own `now_epoch`, while `freshenLive` stamps
 * the live tab onto the wall clock. A page that followed the wall clock would read
 * a running session as days into its live edge, so the frame carries the
 * fixture's present and the page aligns to it, as it would to a real service's.
 * It stands still, as a capture's clock does: one that ran would leave the
 * fixture's rows further behind it with every minute the dev server stays up.
 *
 * `RC_MOCK_PINNED=1` says so the way a frozen capture does (`pinned`, `as_of`),
 * to see what the page says of a clock that will not move. */
function fixtureClock(fx: Fixture) {
  const pinned = process.env.RC_MOCK_PINNED === '1';
  return {
    pinned,
    now: fx.now_epoch,
    as_of: pinned ? new Date(fx.now_epoch * 1000).toISOString().slice(0, 19).replace('T', ' ') : null,
    source: "mock/api.json: the fixture's own present",
    behind_wall_s: Math.floor(Date.now() / 1000) - fx.now_epoch,
  };
}

/* Re-stamp the fixture's fixed clock onto the wall clock, so the live tab shows
 * a window that ends now. Values are untouched -- only the timestamps move. The
 * frame's `clock` is the exception: it is the fixture's own (above), which the
 * page's "now" follows. */
function freshenLive(fx: Fixture, minutes = MOCK_RETENTION_MIN) {
  const nowEpoch = Math.floor(Date.now() / 1000);
  const shift = nowEpoch - fx.now_epoch;
  const live = fx.live as unknown as {
    rate: Record<string, number | null>[];
    events: Record<string, unknown>[];
    window: Record<string, unknown>;
    [k: string]: unknown;
  };
  const span = Math.min(minutes, live.rate.length);
  const wide = minutes > 1440;
  const rate = live.rate.slice(-span).map((r) => {
    const epoch = Number(r.epoch) + shift;
    const d = new Date(epoch * 1000);
    return {
      ...r,
      epoch,
      // The real backend switches to a dated label past a day, for the same
      // reason: `14:03` repeated seven times is not an axis.
      t: wide
        ? `${d.toISOString().slice(5, 10)} ${d.toTimeString().slice(0, 5)}`
        : d.toTimeString().slice(0, 5),
    };
  });
  const cutoff = nowEpoch - span * 60;
  const events = live.events
    .map((e) => {
      const epoch = Number(e.epoch) + shift;
      return { ...e, epoch, ts: new Date(epoch * 1000).toTimeString().slice(0, 8) };
    })
    .filter((e) => Number(e.epoch) >= cutoff);
  const stamp = (s: number) => new Date(s * 1000).toISOString().slice(0, 19).replace('T', ' ');
  return {
    ...live,
    rate,
    events,
    clock: fixtureClock(fx),
    window: {
      ...(live.window || {}),
      minutes: span,
      bin_s: 60,
      label: label(span),
      from: stamp(cutoff),
      to: stamp(nowEpoch),
      retention_minutes: MOCK_RETENTION_MIN,
      retained_from: stamp(nowEpoch - live.rate.length * 60),
      retained_minutes: live.rate.length,
    },
  };
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

/** Decode whole client text frames out of `buf`, returning the leftover bytes.
 *
 * The client sends exactly one kind of message -- which window it is viewing --
 * and the plugin has to act on it, or dev mode would keep pushing the default
 * span while the picker read something else: a bug that only ever shows up
 * against the real backend. Client frames are always masked (RFC 6455 §5.3).
 */
function readClientFrames(buf: Buffer): { texts: string[]; rest: Buffer } {
  const texts: string[] = [];
  let off = 0;
  for (;;) {
    if (buf.length - off < 2) break;
    const opcode = buf[off] & 0x0f;
    const masked = (buf[off + 1] & 0x80) !== 0;
    let len = buf[off + 1] & 0x7f;
    let p = off + 2;
    if (len === 126) {
      if (buf.length - p < 2) break;
      len = buf.readUInt16BE(p);
      p += 2;
    } else if (len === 127) {
      if (buf.length - p < 8) break;
      len = Number(buf.readBigUInt64BE(p));
      p += 8;
    }
    let mask: Buffer | null = null;
    if (masked) {
      if (buf.length - p < 4) break;
      mask = buf.subarray(p, p + 4);
      p += 4;
    }
    if (buf.length - p < len) break; // frame still arriving: wait for more
    const body = Buffer.from(buf.subarray(p, p + len));
    if (mask) for (let i = 0; i < body.length; i++) body[i] ^= mask[i % 4];
    off = p + len;
    if (opcode === 0x1) texts.push(body.toString('utf8'));
  }
  // Copied, not a view: the leftover is a partial frame of a few bytes at most,
  // and a subarray would keep the whole received chunk alive behind it.
  return { texts, rest: Buffer.from(buf.subarray(off)) };
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

        const win = resolveWindow(q);
        switch (u.pathname) {
          case '/api/config':
            return send(fx.config);
          case '/api/feeds':
            return send(fx.feeds);
          case '/api/windows':
            return send({
              held: [windowStatus(win.minutes)],
              max_windows: 2,
              queued: [],
              presets: presets(),
              retention_minutes: MOCK_RETENTION_MIN,
              default_minutes: Math.min(1440, MOCK_RETENTION_MIN),
              min_minutes: MIN_WINDOW_MIN,
            });
          case '/api/panels': {
            const st = windowStatus(win.minutes);
            const src = fx.panels as { panels: Record<string, unknown>; [k: string]: unknown };
            const panels = Object.fromEntries(
              Object.entries(src.panels || {}).map(([k, v]) => [
                k,
                // The node tier is a snapshot of now, not a window aggregate.
                k === 'node' ? v : { ...(v as object), _window: st },
              ]),
            );
            return send({ ...src, panels, window: win, window_status: st });
          }
          case '/api/live':
            return send({ ...freshenLive(fx, win.minutes),
                          window: { ...freshenLive(fx, win.minutes).window,
                                    requested_minutes: win.requested,
                                    clamped: win.clamped, note: win.note } });
          case '/api/trajectories': {
            const rank = q.get('rank') || 'events';
            return send({ ...fx.trajectories, rank_by: rank,
                          _window: windowStatus(win.minutes) });
          }
          case '/api/trajectory': {
            // One hand-built tree, served for any session the fixture ranks, so
            // every row of the dev page opens something: laid on the row's own
            // span, and paged by `from`, as the service reads a session.
            const sid = q.get('sid') || '';
            const rows = (fx.trajectories as { rows?: Record<string, unknown> }).rows ?? {};
            const st = windowStatus(win.minutes);
            if (!fx.trajectory_detail || !(sid in rows)) {
              return send({ sid, found: false, why: `no session with this id in the ${win.label} window`,
                            _window: st, window: win });
            }
            const asked = q.get('from');
            const from = asked != null && Number.isFinite(Number(asked)) ? Number(asked) : null;
            return send({ ...alignDetail(fx.trajectory_detail, rows[sid], from), sid, row: rows[sid],
                          _window: st, window: win });
          }
          case '/api/events': {
            const all = filterEvents(freshenLive(fx, win.minutes).events, q);
            const PAGE = 100;
            const start = Number(q.get('cursor') || 0);
            const rows = all.slice(start, start + PAGE);
            return send({
              rows,
              next_cursor: start + PAGE < all.length ? String(start + PAGE) : null,
              total_scanned: freshenLive(fx, win.minutes).events.length,
              truncated: all.length > 380,
            });
          }
          case '/api/export': {
            const all = filterEvents(freshenLive(fx, win.minutes).events, q);
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

        // Which window this socket is on. The real service tracks exactly this,
        // per client, and sends one `live` frame per distinct window.
        let minutes = Math.min(1440, MOCK_RETENTION_MIN);

        const push = () => {
          if (socket.destroyed) return;
          const fx = load();
          socket.write(
            frame(JSON.stringify({ type: 'live', payload: freshenLive(fx, minutes) })),
          );
          socket.write(frame(JSON.stringify({ type: 'feeds', payload: fx.feeds })));
          socket.write(
            frame(
              JSON.stringify({
                type: 'windows',
                payload: {
                  held: [windowStatus(minutes)],
                  max_windows: 2,
                  queued: [],
                  presets: presets(),
                  retention_minutes: MOCK_RETENTION_MIN,
                  default_minutes: Math.min(1440, MOCK_RETENTION_MIN),
                  min_minutes: MIN_WINDOW_MIN,
                },
              }),
            ),
          );
        };
        push();
        const iv = setInterval(push, 10_000);
        socket.on('close', () => clearInterval(iv));
        socket.on('error', () => clearInterval(iv));

        // Explicitly the general Buffer type: `Buffer.alloc` narrows to
        // Buffer<ArrayBuffer>, which the reader's leftover does not satisfy.
        let carry: Buffer = Buffer.alloc(0);
        socket.on('data', (chunk: Buffer) => {
          carry = Buffer.concat([carry, chunk]);
          const { texts, rest } = readClientFrames(carry);
          carry = rest;
          for (const t of texts) {
            let msg: { type?: string; minutes?: unknown };
            try {
              msg = JSON.parse(t) as { type?: string; minutes?: unknown };
            } catch {
              continue; // a keepalive ping, or a frame we do not speak
            }
            if (msg?.type !== 'window') continue;
            const want = resolveWindow(
              new URLSearchParams({ window_min: String(msg.minutes ?? '') }),
            ).minutes;
            if (want === minutes) continue;
            minutes = want;
            push(); // answer at once: the chart must not lag the picker
          }
        });
      });

      server.config.logger.info('  ⤷  rc-mock-api: /api/* and /ws served from mock/api.json');
    },
  };
}
