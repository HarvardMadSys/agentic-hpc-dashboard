/* A self-check for the live connection (`src/api/useLive.ts`, `src/lib/backoff.ts`).
 *
 *   npm run check:live          (node --test mock/check_live.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The hook's own code runs, as written: node loads `useLive.ts` with `react`
 * resolved to mock/fake_react.mjs (its hooks, outside a renderer) and `./client` to
 * mock/fake_client.mjs (every read logged), and the test plays the socket, the clock, the network
 * and the tab. A socket that fails is tried again for as long as the page is open; while it is
 * down the page polls what the socket would have sent; only a frame resets the wait; a socket that
 * goes quiet is replaced; and the refresh button asks for everything again.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { register } from 'node:module';
import { afterEach, beforeEach, test } from 'node:test';

const here = (f) => new URL(f, import.meta.url).href;
register(
  'data:text/javascript,' +
    encodeURIComponent(`
      const REACT = ${JSON.stringify(here('./fake_react.mjs'))};
      const CLIENT = ${JSON.stringify(here('./fake_client.mjs'))};
      export async function resolve(s, c, next) {
        if (s === 'react') return { url: REACT, shortCircuit: true };
        if (s === './client' && c.parentURL && c.parentURL.endsWith('/src/api/useLive.ts')) return { url: CLIENT, shortCircuit: true };
        try { return await next(s, c); }
        catch (e) { if (e.code === 'ERR_MODULE_NOT_FOUND' && s.startsWith('.')) return next(s + '.ts', c); throw e; }
      }`),
  import.meta.url,
);

/* ------------------------------------------------------------ the world the hook runs in */

/** A clock the test moves: the hook's timers run when it passes them, and nothing else does. */
const clock = { now: 0, q: [], id: 0 };
const setT = (f, ms = 0) => {
  const id = ++clock.id;
  clock.q.push({ id, at: clock.now + Math.max(0, ms), f });
  return id;
};
const clearT = (id) => {
  clock.q = clock.q.filter((t) => t.id !== id);
};
/** An event target the test can fire: `fire('online')`. */
function target(o) {
  const by = new Map();
  o.addEventListener = (k, f) => {
    if (!by.has(k)) by.set(k, new Set());
    by.get(k).add(f);
  };
  o.removeEventListener = (k, f) => by.get(k)?.delete(f);
  o.fire = (k) => [...(by.get(k) ?? [])].forEach((f) => f({ type: k }));
  return o;
}
globalThis.window = target({ setTimeout: setT, clearTimeout: clearT });
globalThis.document = target({ visibilityState: 'visible' });

/** The page's socket, played by the test: `open()`, `frame(f)`, `drop()` (it closes, or never opened). */
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    globalThis.__live.sockets.push(this);
  }
  send(t) {
    this.sent.push(t);
  }
  close() {
    this.closed = true;
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  frame(f) {
    this.onmessage?.({ data: JSON.stringify(f) });
  }
  drop() {
    this.readyState = 3;
    this.onerror?.({});
    this.onclose?.({});
  }
}
Object.defineProperty(globalThis, 'WebSocket', { value: FakeSocket, configurable: true, writable: true });

const LIVE = { clock: { now: 1_700_000_000, pinned: false }, window: { minutes: 60, label: '1h' }, rate: [], events: [] };
const FEEDS = { ebpf: { name: 'ebpf', status: 'ok' } };
const windowsOf = (built) => ({
  held: [{ minutes: 60, state: 'ready', built_at: built, installed_at: 1 }],
  max_windows: 4, queued: [], presets: [], retention_minutes: 10080, default_minutes: 60, min_minutes: 5,
});

const { mount } = await import('./fake_react.mjs');
const { useLive } = await import('../src/api/useLive.ts');

let L;
let h;
beforeEach(() => {
  clock.now = 0;
  clock.q = [];
  document.visibilityState = 'visible';
  let built = 'b1';
  L = globalThis.__live = {
    reads: [], sockets: [], down: false,
    live: () => LIVE, windows: () => windowsOf(built), feeds: () => FEEDS,
    rebuild: (b) => (built = b),
  };
});
afterEach(() => h?.unmount());

/** Promise chains run, and the hook renders what they set. */
async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  h.flush();
}
/** Move the clock on by `ms`, running each timer as it comes due. */
async function advance(ms) {
  const end = clock.now + ms;
  for (;;) {
    clock.q.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = clock.q[0];
    if (!t || t.at > end) break;
    clock.q.shift();
    clock.now = t.at;
    t.f();
    await settle();
  }
  clock.now = end;
  await settle();
}
async function start(win = null) {
  h = mount(useLive, win);
  await settle();
  return h;
}
const last = () => L.sockets[L.sockets.length - 1];
/** The newest socket fails; how long until the page makes the next one, in ms (null: none in 2 min). */
async function failAndWait({ opened = false } = {}) {
  const n = L.sockets.length;
  if (opened) last().open();
  last().drop();
  await settle();
  const t0 = clock.now;
  while (L.sockets.length === n && clock.now - t0 < 120000) await advance(250);
  return L.sockets.length > n ? clock.now - t0 : null;
}

/* ------------------------------------------------------------------------------ the checks */

test('the schedule: doubling from 1 s to 30 s, each wait in its upper half, never random', async () => {
  const B = await import('../src/lib/backoff.ts');
  for (let n = 1; n <= 12; n++) {
    const d = Math.min(30000, 1000 * 2 ** (n - 1));
    assert.equal(B.retryDelay(n, 0), d / 2);
    assert.equal(B.retryDelay(n, 1), d);
    const s = B.spreadOf(1_731_900_000_123.4, n);
    assert.ok(s >= 0 && s < 1);
    const w = B.retryDelay(n, s);
    assert.ok(w >= d / 2 && w <= d);
  }
  assert.equal(B.retryDelay(1000, 1), 30000, 'capped, however many tries');
  assert.equal(B.spreadOf(42, 3), B.spreadOf(42, 3), 'the same page, the same try: the same spread');
  assert.notEqual(B.spreadOf(1_731_900_000_123, 3), B.spreadOf(1_731_900_000_456, 3), 'two pages: apart');
  assert.equal(B.readyKey([{ minutes: 60, state: 'building', built_at: null }]), '');
  assert.equal(B.readyKey([{ minutes: 60, state: 'ready', built_at: 'x', installed_at: 2 }]), '60:x:2');
});

test('a socket that fails is tried again for as long as the page is open', async () => {
  await start();
  for (let i = 1; i <= 12; i++) {
    const waited = await failAndWait();
    // on develop: after the fifth failure (18 s of retries) it only polled, and never tried again
    assert.notEqual(waited, null, `no new socket after failure ${i}`);
    assert.ok(waited <= 30000, `failure ${i}: waited ${waited} ms`);
  }
});

test('while the socket is down the page polls the windows and the feeds too, and a finished build reaches the panels', async () => {
  const hook = await start();
  for (let i = 0; i < 5; i++) await failAndWait();
  assert.equal(hook.value.transport, 'polling');
  // on develop: only /api/live was polled
  assert.ok(L.reads.includes('windows'), `reads: ${L.reads}`);
  assert.ok(L.reads.includes('feeds'), `reads: ${L.reads}`);
  assert.deepEqual(hook.value.feeds, FEEDS);
  const nonce = hook.value.buildNonce;
  L.rebuild('b2');
  await advance(15000);
  assert.equal(hook.value.buildNonce, nonce + 1, 'the build that finished while polling');
  await advance(15000);
  assert.equal(hook.value.buildNonce, nonce + 1, 'and only once');
});

test('only a socket that stays up starts the wait over: one that opens and closes at once backs off', async () => {
  await start();
  const waits = [];
  for (let i = 0; i < 7; i++) waits.push(await failAndWait({ opened: true }));
  // on develop: 1,000 ms every time, as each opening reset the count
  assert.ok(waits[6] >= 8000, `waits: ${waits}`);
  // a socket that speaks, and stays up for a watchdog period, resets it: the next failure is
  // tried again within a second
  last().open();
  last().frame({ type: 'live', payload: LIVE });
  for (let i = 0; i < 3; i++) {
    await advance(30000);
    last().frame({ type: 'feeds', payload: FEEDS }); // the service's 30 s beat
  }
  await settle();
  assert.ok((await failAndWait()) <= 1000);
});

test('a socket that sends its first frames and drops keeps backing off, and the page keeps polling', async () => {
  const hook = await start();
  for (let i = 0; i < 5; i++) await failAndWait();
  assert.equal(hook.value.transport, 'polling');
  const waits = [];
  for (let i = 0; i < 4; i++) {
    // the service's opening frames, then a proxy's timeout or a worker that dies after its hello
    last().open();
    last().frame({ type: 'live', payload: LIVE });
    last().frame({ type: 'windows', payload: windowsOf('b1') });
    await settle();
    assert.equal(hook.value.transport, 'ws', 'live while its frames flow');
    waits.push(await failAndWait());
    // on 8c38a7d: "connecting", the polling stopped by the first frame
    assert.equal(hook.value.transport, 'polling', `after drop ${i + 1}`);
  }
  // on 8c38a7d: 500 to 1,000 ms after every drop, as each first frame reset the count
  assert.ok(waits.every((w) => w >= 8000), `waits: ${waits}`);
  L.reads.length = 0;
  await advance(15000);
  assert.ok(L.reads.includes('windows'), `the polling goes on: ${L.reads}`);
});

test('a socket whose handshake never finishes is given up and tried again, and the page polls meanwhile', async () => {
  const hook = await start();
  for (let i = 1; i <= 5; i++) {
    const n = L.sockets.length;
    const t0 = clock.now;
    // the newest is never opened, closed or failed: a proxy that took the connection and is silent
    while (L.sockets.length === n && clock.now - t0 < 120000) await advance(250);
    // on 8c38a7d: no new socket, ever, and no poll
    assert.ok(L.sockets.length > n, `no new socket after stalled handshake ${i}`);
    assert.equal(L.sockets[n - 1].closed, true, 'the stalled one was let go');
    assert.ok(clock.now - t0 <= 15000 + 30000, `${clock.now - t0} ms`);
  }
  assert.equal(hook.value.transport, 'polling');
  assert.ok(L.reads.includes('windows'), `reads: ${L.reads}`);
  // and while the newest is opening, the browser back online waits for it rather than doubling it
  const n = L.sockets.length;
  window.fire('online');
  assert.equal(L.sockets.length, n);
});

test('a socket that says nothing for 75 s is closed and replaced, and the page stops saying live', async () => {
  const hook = await start();
  const s = last();
  s.open();
  s.frame({ type: 'live', payload: LIVE });
  await settle();
  assert.equal(hook.value.transport, 'ws');
  await advance(74000);
  s.frame({ type: 'feeds', payload: FEEDS }); // the service's 30 s beat: any frame re-arms it
  await advance(74000);
  assert.equal(s.closed, false);
  await advance(2000);
  // on develop: still open, and still "live"
  assert.equal(s.closed, true, 'a socket quiet for 75 s is let go');
  assert.notEqual(hook.value.transport, 'ws');
  await advance(1000);
  assert.equal(L.sockets.length, 2, 'and a new one is made');
});

test('the browser back online, or the tab shown again, tries the socket at once', async () => {
  await start();
  for (let i = 0; i < 6; i++) await failAndWait();
  last().drop(); // the next try is 15 to 30 s away
  await settle();
  const n = L.sockets.length;
  window.fire('online');
  // on develop: nothing until the wait ran out
  assert.equal(L.sockets.length, n + 1, 'online');
  last().drop();
  await settle();
  document.visibilityState = 'hidden';
  document.fire('visibilitychange');
  assert.equal(L.sockets.length, n + 1, 'not while the tab is hidden');
  document.visibilityState = 'visible';
  document.fire('visibilitychange');
  assert.equal(L.sockets.length, n + 2, 'the tab shown again');
  window.fire('online');
  assert.equal(L.sockets.length, n + 2, 'not while one is opening');
});

test('refresh asks for the windows and the feeds as well as the live frame', async () => {
  const hook = await start();
  L.reads.length = 0;
  hook.value.refresh();
  await settle();
  // on develop: ['live']
  assert.deepEqual([...L.reads].sort(), ['feeds', 'live', 'windows']);
});

test("the header's refresh, and the palette's, reload the panels and the sessions too", () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const body = app.match(/const refresh = useCallback\(\(\) => \{([^}]*)\}/);
  // on develop: both called `live.refresh`, which re-read /api/live alone
  assert.ok(body, 'one refresh for the page');
  for (const call of ['refreshLive()', 'reloadPanels()', 'reloadTraj()']) assert.ok(body[1].includes(call), call);
  assert.match(app, /const refreshLive = live\.refresh;/);
  assert.match(app, /const reloadTraj = traj\.reload;/);
  assert.match(app, /<button className="btn" onClick=\{refresh\}[^>]*>\s*<Icon name="refresh"/);
  assert.match(app, /id: 'act:refresh',[^}]*run: refresh,/);
});
