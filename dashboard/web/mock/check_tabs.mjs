/* A self-check that the Sessions tab's parts are where they went: its list of every session and the
 * inspector beside it at the foot of the Agents tab, and a user's own sessions opening under their
 * portrait on Users (`src/App.tsx`, `src/tabs/OpenSession.tsx`). Every way to a session -- a saved
 * `?tab=trajectories` link, a citation, the popped-out tree's way back -- lands on one of the two, and
 * a session open on one tab does not open again on the other.
 *
 *   npm run check:tabs          (node --test mock/check_tabs.mjs)
 *
 * Like check_lookup.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The modules are loaded with Vite's loader (mock/ssr.mjs). App is not drawn (it
 * needs a browser and the service), so its wiring is read as text.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { renderer } from './ssr.mjs';

let ui;
before(async () => {
  ui = await renderer('tabs');
});
after(() => ui?.close());

const source = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8');
const SID = 'login1|2026-11-20|agent|apid:4242';

test('the rail has no Sessions entry, and a saved link to it opens the Agents tab with what it named', async () => {
  const app = source('App.tsx');
  const tabs = /const TABS: Tab\[\] = \[([\s\S]*?)\n\];/.exec(app)?.[1] ?? '';
  assert.ok(tabs, 'App.tsx keeps its TABS');
  // on develop: { k: 'trajectories', lbl: 'Sessions', ... }, a stop of its own in the rail
  assert.doesNotMatch(tabs, /k: 'trajectories'/);
  assert.doesNotMatch(tabs, /lbl: 'Sessions'/);
  // "go to" still finds the Agents tab by the old names
  assert.match(tabs, /k: 'agents',[^\n]*also: '[^']*\bsessions\b[^']*\btrajectories\b/);
  const { parseUrl } = await ui.load('lib/filters.ts');
  const u = parseUrl(`?tab=trajectories&traj=${encodeURIComponent(SID)}&tuser=alice&torder=newest&rank=cpu_s`);
  assert.equal(u.tab, 'agents');
  assert.equal(u.traj, SID, 'the session it opened is open');
  assert.equal(u.tuser, 'alice', "a link to one user's sessions narrows the list to them");
  assert.equal(u.torder, 'newest');
  assert.equal(u.rank, 'cpu_s');
  // it lands on the list, at the foot of the tab, and one that opens a session on its inspector; so does a link to the Agents tab
  // that opens a session, or narrows the list to one user's
  assert.match(app, /const LANDS_ON: Record<string, string> = table\(\{[^}]*trajectories: SESSIONS_SEC \}\);/);
  assert.match(app, /const SESSIONS_SEC = 'sec-ranked-sessions';\nconst INSPECTOR_SEC = 'sec-session-inspector';/);
  const lands = /function landsOn\([^)]*\): string \| null \{([\s\S]*?)\n\}/.exec(app)?.[1] ?? '';
  assert.match(lands, /^\s*if \(url\.tab === 'agents' && url\.traj\) return INSPECTOR_SEC;\s*if \(LANDS_ON\[opened\]\) return LANDS_ON\[opened\];\s*return url\.tab === 'agents' && url\.tuser \? SESSIONS_SEC : null;\s*$/);
  assert.match(source('tabs/TrajectoriesTab.tsx'), /id="sec-ranked-sessions"/);
  assert.match(source('tabs/TrajectoryInspector.tsx'), /id="sec-session-inspector"/);
});

test('the Agents tab ends in the sessions list and its inspector, and the list is re-read while that tab is open', () => {
  const app = source('App.tsx');
  assert.doesNotMatch(app, /case 'trajectories':/);
  const agents = /case 'agents':\s*return \(([\s\S]*?)\n {8}\);/.exec(app)?.[1] ?? '';
  const [a, s] = [agents.indexOf('<AgentsTab '), agents.indexOf('<TrajectoriesTab')];
  assert.ok(a >= 0 && s > a, 'the agents first, then every session');
  // on develop: `url.tab === 'trajectories'`, which no tab is now, and the list would never be read again
  assert.match(app, /url\.tab === 'agents' \? TRAJ_LIVE_MS : null/);
  // split off with the tab's code, its stylesheet kept where the Sessions tab put it in the page's one sheet
  assert.match(app, /const TrajectoriesTab = lazyTab\(\(\) => import\('\.\/tabs\/TrajectoriesTab'\)/);
  assert.match(app, /import '\.\/tabs\/trajectories\.css';\nimport '\.\/tabs\/agents\.css';\nimport '\.\/tabs\/users\.css';/);
});

test('a session opens on the Agents tab from anywhere else, and in place under a portrait on Users', () => {
  const app = source('App.tsx');
  assert.match(app, /\(sid: string\) => \{\s*setUrl\(\{ tab: 'agents', traj: sid, tfrom: null, tuser: null \}, true\);\s*setLand\(\{ tab: 'agents', sec: INSPECTOR_SEC \}\);/);
  // a user's sessions are their portrait's ...
  assert.match(app, /setToSessions\(u\);\s*setUrl\(\{ tab: 'users', portrait: u, traj: null, tfrom: null \}, true\);/);
  // ... and every one of them, of every class, the Agents tab's list narrowed to them
  assert.match(app, /setUrl\(\{ tab: 'agents', tuser: u, traj: null, tfrom: null \}, true\);\s*setLand\(\{ tab: 'agents', sec: SESSIONS_SEC \}\);/);
  // opening or closing a portrait closes the session open under the last one
  assert.match(app, /setUrl\(\{ portrait: u, traj: null, tfrom: null \}, true\);/);
  const users = source('tabs/UsersTab.tsx');
  // on develop: a row's "process tree →" and a task's left for the Sessions tab
  assert.doesNotMatch(users, /\bopenTraj\b|\bopenUserTraj\b/);
  assert.match(users, /<Sessions p=\{p\} sel=\{sel\} open=\{openHere\} \/>/);
  assert.match(users, /<Tasks p=\{p\} row=\{row\} sel=\{sel\} open=\{openHere\} \/>/);
  assert.match(users, /\{sel && \(\s*<div className="span2 ur-session" ref=\{insRef\}>\s*<OpenSession/);
  // the one it opened is marked, in the shade a picked row has
  assert.match(source('tabs/users.css'), /table\.ur-sesslist tr\.on td,\ntable\.ur-tasklist tr\.on td \{/);
});

test('one thing at a time scrolls to a session: the page landing on it, or the inspector as it opens', () => {
  const app = source('App.tsx');
  // A landing waits for what is drawn above its part -- the panels and the sessions -- and lets go when the reader scrolls first
  // or goes to another tab. Gone to sooner, at the foot of the Agents tab, it was pushed down by what arrived above it.
  assert.match(app, /const settled = \(panels\.data != null \|\| panels\.error != null\) && \(traj\.data != null \|\| traj\.error != null\);/);
  const effect = /useEffect\(\(\) => \{\s*if \(!land\) return;([\s\S]*?)\}, \[land, tab\.k, goSec, settled\]\);/.exec(app)?.[1] ?? '';
  assert.match(effect, /if \(land\.tab !== tab\.k\) \{\s*setLand\(null\);/);
  assert.match(effect, /addEventListener\('wheel', byHand/);
  assert.match(effect, /if \(!settled\) return done;/);
  assert.match(effect, /goSec\(land\.sec, 'auto'\)/);
  // While the page is to land on the inspector, the inspector does not scroll itself as it opens: a smooth scroll of its own,
  // still running when the page jumped, carried the page some 150 px past it (on develop it always scrolled itself)
  assert.match(app, /landing=\{land\?\.sec === INSPECTOR_SEC\}/);
  assert.match(source('tabs/TrajectoriesTab.tsx'), /scrollOnOpen=\{!landing\}/);
  assert.match(source('tabs/OpenSession.tsx'), /scrollOnOpen=\{scrollOnOpen\}/);
  assert.match(source('tabs/TrajectoryInspector.tsx'), /if \(scrollOnOpen\) ref\.current\?\.scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\);/);
  // under a portrait, one opened with the page is scrolled to by the portrait's arrival, which waits for the roster above it
  const users = source('tabs/UsersTab.tsx');
  assert.match(users, /scrollOnOpen=\{arrived\}/);
  assert.match(users, /const arrived = \(!!p \|\| !!q\.error\) && above;/);
  assert.match(users, /above=\{U != null \|\| users\.error != null\}/);
  assert.match(users, /const to = insRef\.current \?\? \(toSessions \? document\.getElementById\('portrait-sessions'\) : null\) \?\? ref\.current;/);
  // and it is a screen tall from the first, so that opening it can bring its top up before its read has made the page long enough
  assert.match(source('tabs/users.css'), /\.ur-session \{\s*min-height: calc\(100vh - var\(--controls-h\)\);\s*\}/);
});

test('another tab closes the session open on this one, unless it opens one itself', async () => {
  const { parseUrl, patched, urlQuery } = await ui.load('lib/filters.ts');
  const open = parseUrl(`?tab=users&portrait=alice&traj=${encodeURIComponent(SID)}&tfrom=100`);
  // on develop: the session stayed in the link, and opened again at the foot of the Agents tab, scrolling it down
  const off = patched(open, { tab: 'agents' });
  assert.equal(off.traj, null);
  assert.equal(off.tfrom, null);
  assert.equal(off.portrait, 'alice', "the portrait stays the Users tab's");
  assert.doesNotMatch(urlQuery(off), /traj=|tfrom=/);
  // the tab it is open on keeps it, and a patch that opens one opens that one
  assert.equal(patched(open, { win: 60 }).traj, SID);
  assert.equal(patched(open, { tab: 'users' }).traj, SID);
  assert.equal(patched(open, { tab: 'agents', traj: 'other' }).traj, 'other');
  // the page's one way to write its URL, and a citation's, go through it
  assert.match(source('lib/useUrlState.ts'), /const next: UrlState = patched\(cur\.current, patch\);/);
  assert.match(source('lib/cites.ts'), /const q = urlQuery\(patched\(parseUrl\(location\.search\), a\.patch\)\);/);
  assert.match(source('lib/cites.ts'), /const q = urlQuery\(patched\(from, at\.patch\)\);/);
});

test('a citation of a session opens it beside the Agents tab’s list, and one open under a portrait is cited there', async () => {
  const C = await ui.load('lib/cites.ts');
  const view = { window_min: 60, users: [], atypes: [], cls: null, sid: SID };
  // the list of sessions
  const list = C.target({ path: ['sessions', 'rows', 0], data: { user: 'alice' }, go: null }, view);
  assert.equal(list.arrival.patch.tab, 'agents');
  assert.equal(list.arrival.anchor, 'sec-ranked-sessions');
  assert.match(list.where, /^Agents › Sessions$/);
  // the session asked about, as a whole
  const whole = C.target({ path: ['session', 'gist'], data: 'x', go: null }, view);
  assert.equal(whole.arrival.patch.tab, 'agents');
  assert.equal(whole.arrival.patch.traj, SID);
  // an agent's row that is a session opens the session, its panel a link of its own
  const other = 'login1|2026-11-20|agent|apid:7';
  const row = C.target({ path: ['panels', 'cascades', 'trees', 0], data: { sid: other, user: 'bob' }, go: null }, view);
  assert.equal(row.arrival.patch.tab, 'agents');
  assert.equal(row.arrival.patch.traj, other);
  // on develop: "Sessions › Session inspector"
  assert.equal(row.where, 'Agents › Session inspector');
  assert.ok(row.also, 'the panel it was read from');
  // a session open on this page, under a portrait on Users as beside the list, is cited where it is open
  assert.match(source('lib/cites.ts'), /a\.patch\.traj != null && a\.patch\.traj === from\.traj \? \{ \.\.\.a, patch: \{ \.\.\.a\.patch, tab: from\.tab \} \}/);
});

test("the popped-out tree's way back opens its session on the Agents tab", async () => {
  const { dashboardHref } = await ui.load('lib/popout.ts');
  const had = globalThis.window;
  globalThis.window = { location: { pathname: '/' } };
  try {
    const href = dashboardHref({ sid: SID, win: 60, from: 100, hide: true, typos: true, order: 'newest' });
    const p = new URLSearchParams(href.split('?')[1]);
    // on develop: tab=trajectories, a tab that is gone
    assert.equal(p.get('tab'), 'agents');
    assert.equal(p.get('traj'), SID);
    assert.equal(p.get('tfrom'), '100');
    assert.equal(p.get('torder'), 'newest');
  } finally {
    globalThis.window = had;
  }
});
