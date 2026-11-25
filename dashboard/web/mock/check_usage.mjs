/* A self-check that the page counts the tabs readers open as the service takes them
 * (`src/lib/usage.ts`, rc_dashboard/usage.py): a tab's key and a count, nothing sent
 * before /api/config says the site counts, nothing where it says not, and App wired to
 * count each tab it opens.
 *
 *   node --test mock/check_usage.mjs
 *
 * The module is loaded with Vite's loader (mock/ssr.mjs); `fetch` is a stand-in that
 * keeps what would be posted.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { renderer } from './ssr.mjs';

let ui;
let U;
const posted = [];
before(async () => {
  globalThis.fetch = async (url, init) => {
    posted.push({ url, init });
    return { ok: true, status: 204 };
  };
  ui = await renderer('usage');
  U = await ui.load('lib/usage.ts');
});
after(() => ui?.close());

test('nothing goes before the site says it counts, and nothing where it says not', () => {
  U.countTab('live');
  U.flush();
  assert.equal(posted.length, 0, 'not known yet: kept, not sent');
  U.setCounting(false);
  U.countTab('users');
  U.flush();
  assert.equal(posted.length, 0);
  assert.equal(U.takePending(), null, 'off drops what was gathered, and gathers nothing');
});

test('a tab key and a count, posted as the service takes them', () => {
  U.setCounting(true);
  for (const k of ['live', 'live', 'risk', 'Bad Key', '../x', '']) U.countTab(k);
  U.flush();
  assert.equal(posted.length, 1);
  const { url, init } = posted[0];
  assert.equal(url, '/api/usage');
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['content-type'], 'application/json');
  assert.equal(init.keepalive, true);
  assert.deepEqual(JSON.parse(init.body), { views: { 'tab:live': 2, 'tab:risk': 1 } });
  U.flush();
  assert.equal(posted.length, 1, 'nothing gathered since: no post');
});

test('a count stops at what the service takes in one post', () => {
  for (let i = 0; i < 1500; i++) U.countTab('feeds');
  assert.deepEqual(U.takePending(), { 'tab:feeds': 1000 });
});

test('App counts each tab it opens, once the config says the site counts', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /countTab\(tab\.k\)/);
  assert.match(app, /setCounting\(countViews\)/);
  assert.match(app, /count_views/);
});
