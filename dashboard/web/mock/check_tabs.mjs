/* A self-check that the rail's six stops hold what they say and every way to a session lands: the
 * Sessions tab, a sub-tab of the Agents entry, with its list of every session and the inspector beside
 * it, and a user's own sessions opening under their portrait on Users (`src/App.tsx`,
 * `src/tabs/OpenSession.tsx`). Every way to a session -- a saved `?tab=trajectories` or `?tab=agents&traj=`
 * link, a citation, the popped-out tree's way back -- lands on one of the two, and a session open on one
 * tab does not open again on the other. Ask LLM is a drawer beside the open tab, not a stop; Internal is
 * listed only where the site asks.
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

test('six stops in the rail, Sessions a sub-tab of Agents, and a saved link to the old tab opens it', async () => {
  const app = source('App.tsx');
  const tabs = /const TABS: Tab\[\] = \[([\s\S]*?)\n\];/.exec(app)?.[1] ?? '';
  assert.ok(tabs, 'App.tsx keeps its TABS');
  assert.match(tabs, /k: 'agents',[^\n]*parent: 'agents'/);
  assert.match(tabs, /k: 'sessions', lbl: 'Sessions',[^\n]*parent: 'agents',[^\n]*also: '[^']*\btrajectories\b/);
  for (const [k, parent] of [['security', 'risk'], ['risk', 'risk'], ['resources', 'usage'], ['slurm', 'usage'], ['trend', 'usage'], ['feeds', 'system'], ['internal', 'system']]) {
    assert.match(tabs, new RegExp(`k: '${k}',[^\\n]*parent: '${parent}'`), `${k} under ${parent}`);
  }
  // the stops: Live and Users, then the four parents; Ask is not one
  const parents = /const PARENTS: Record<Parent, [^>]*> = \{([\s\S]*?)\n\};/.exec(app)?.[1] ?? '';
  for (const k of ['agents', 'risk', 'usage', 'system']) assert.match(parents, new RegExp(`\\b${k}: \\{ lbl: '`));
  assert.match(app, /if \(t\.k === 'chat' \|\| \(t\.k === 'internal' && !showInternal && tab\.k !== 'internal'\)\) continue;/);
  const { parseUrl } = await ui.load('lib/filters.ts');
  const u = parseUrl(`?tab=trajectories&traj=${encodeURIComponent(SID)}&tuser=alice&torder=newest&rank=cpu_s`);
  assert.equal(u.tab, 'sessions');
  assert.equal(u.traj, SID, 'the session it opened is open');
  assert.equal(u.tuser, 'alice', "a link to one user's sessions narrows the list to them");
  assert.equal(u.torder, 'newest');
  assert.equal(u.rank, 'cpu_s');
  // a link to the Agents tab that opened a session, or one user's list, was a link to its sessions
  assert.equal(parseUrl(`?tab=agents&traj=${encodeURIComponent(SID)}`).tab, 'sessions');
  assert.equal(parseUrl('?tab=agents&tuser=alice').tab, 'sessions');
  assert.equal(parseUrl('?tab=agents').tab, 'agents');
  assert.equal(parseUrl('?tab=cascades').tab, 'agents');
  // a session open lands on its inspector
  const lands = /function landsOn\([^)]*\): string \| null \{([\s\S]*?)\n\}/.exec(app)?.[1] ?? '';
  assert.match(lands, /if \(url\.tab === 'sessions' && url\.traj\) return INSPECTOR_SEC;/);
  assert.match(source('tabs/TrajectoriesTab.tsx'), /id="sec-ranked-sessions"/);
  assert.match(source('tabs/TrajectoryInspector.tsx'), /id="sec-session-inspector"/);
});

test('the Sessions tab draws the list and its inspector, re-read while it is open; Agents draws the agents alone', () => {
  const app = source('App.tsx');
  assert.match(app, /case 'agents':\s*return <AgentsTab P=\{P\} cls=\{cls\} \/>;/);
  const sessions = /case 'sessions':\s*return \(([\s\S]*?)\n {8}\);/.exec(app)?.[1] ?? '';
  assert.ok(sessions.includes('<TrajectoriesTab'), 'the Sessions tab is the list and the inspector');
  assert.match(app, /url\.tab === 'sessions' \? TRAJ_LIVE_MS : null/);
  // split off with the tab's code, its stylesheet kept where the Sessions tab put it in the page's one sheet
  assert.match(app, /const TrajectoriesTab = lazyTab\(\(\) => import\('\.\/tabs\/TrajectoriesTab'\)/);
  assert.match(app, /import '\.\/tabs\/trajectories\.css';\nimport '\.\/tabs\/agents\.css';\nimport '\.\/tabs\/users\.css';/);
});

test('with nothing picked, the first row is shown beside the list, and held while it stays listed', () => {
  // on develop: an empty "pick a session from the list" frame, and one for the agents
  const traj = source('tabs/TrajectoriesTab.tsx');
  assert.match(traj, /else if \(!auto\.current \|\| !drawn\.some\(\(r\) => r\.sid === auto\.current\)\) auto\.current = drawn\[0\]\?\.sid \?\? null;/);
  assert.match(traj, /const cur = sel \?\? auto\.current;/);
  assert.match(traj, /scrollOnOpen=\{!landing && sel != null\}/);
  const agents = source('tabs/AgentsTab.tsx');
  assert.match(agents, /const cur = sel \?\? \(auto\.current \? \{ entry: auto\.current, agent: null \} : null\);/);
});

test('Ask LLM opens beside the open tab, only where a site turned it on; its old link opens it', () => {
  const app = source('App.tsx');
  assert.match(app, /const askOn = askInfo\?\.enabled === true;/);
  assert.match(app, /useState<boolean>\(\(\) => OPENED_ON === 'chat'\)/);
  assert.match(app, /\{askOn && askOpen && \(\s*<AskDrawer onClose=\{closeAsk\}>/);
  assert.doesNotMatch(app, /case 'chat':/);
});

test('a session opens on the Sessions tab from anywhere else, and in place under a portrait on Users', () => {
  const app = source('App.tsx');
  assert.match(app, /\(sid: string\) => \{\s*setUrl\(\{ tab: 'sessions', traj: sid, tfrom: null, tuser: null \}, true\);\s*setLand\(\{ tab: 'sessions', sec: INSPECTOR_SEC \}\);/);
  // a user's sessions are their portrait's ...
  assert.match(app, /setToSessions\(u\);\s*setUrl\(\{ tab: 'users', portrait: u, traj: null, tfrom: null \}, true\);/);
  // ... and every one of them, of every class, the Sessions tab's list narrowed to them
  assert.match(app, /setUrl\(\{ tab: 'sessions', tuser: u, traj: null, tfrom: null \}, true\);\s*setLand\(\{ tab: 'sessions', sec: SESSIONS_SEC \}\);/);
  // opening or closing a portrait closes the session open under the last one
  assert.match(app, /setUrl\(\{ portrait: u, traj: null, tfrom: null \}, true\);/);
  const users = source('tabs/UsersTab.tsx');
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
  // or goes to another tab.
  assert.match(app, /const settled = \(panels\.data != null \|\| panels\.error != null\) && \(traj\.data != null \|\| traj\.error != null\);/);
  const effect = /useEffect\(\(\) => \{\s*if \(!land\) return;([\s\S]*?)\}, \[land, tab\.k, goSec, settled\]\);/.exec(app)?.[1] ?? '';
  assert.match(effect, /if \(land\.tab !== tab\.k\) \{\s*setLand\(null\);/);
  assert.match(effect, /addEventListener\('wheel', byHand/);
  assert.match(effect, /if \(!settled\) return done;/);
  assert.match(effect, /goSec\(land\.sec, 'auto'\)/);
  // While the page is to land on the inspector, the inspector does not scroll itself as it opens
  assert.match(app, /landing=\{land\?\.sec === INSPECTOR_SEC\}/);
  assert.match(source('tabs/OpenSession.tsx'), /scrollOnOpen=\{scrollOnOpen\}/);
  assert.match(source('tabs/TrajectoryInspector.tsx'), /if \(scrollOnOpen\) ref\.current\?\.scrollIntoView\(\{ behavior: 'smooth', block: 'start' \}\);/);
  // under a portrait, one opened with the page is scrolled to by the portrait's arrival, which waits for the roster above it
  const users = source('tabs/UsersTab.tsx');
  assert.match(users, /scrollOnOpen=\{arrived\}/);
  assert.match(users, /const arrived = \(!!p \|\| !!q\.error\) && above;/);
  assert.match(users, /above=\{U != null \|\| users\.error != null\}/);
  assert.match(users, /const to = insRef\.current \?\? \(toSessions \? document\.getElementById\('portrait-sessions'\) : null\) \?\? ref\.current;/);
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

test('a citation of a session opens it beside the Sessions tab’s list, and one open under a portrait is cited there', async () => {
  const C = await ui.load('lib/cites.ts');
  const view = { window_min: 60, users: [], atypes: [], cls: null, sid: SID };
  // the list of sessions: the tab's own name, not "Sessions" twice
  const list = C.target({ path: ['sessions', 'rows', 0], data: { user: 'alice' }, go: null }, view);
  assert.equal(list.arrival.patch.tab, 'sessions');
  assert.equal(list.arrival.anchor, 'sec-ranked-sessions');
  assert.match(list.where, /^Agents › Sessions$/);
  // the session asked about, as a whole
  const whole = C.target({ path: ['session', 'gist'], data: 'x', go: null }, view);
  assert.equal(whole.arrival.patch.tab, 'sessions');
  assert.equal(whole.arrival.patch.traj, SID);
  // an agent's row that is a session opens the session, its panel a link of its own
  const other = 'login1|2026-11-20|agent|apid:7';
  const row = C.target({ path: ['panels', 'cascades', 'trees', 0], data: { sid: other, user: 'bob' }, go: null }, view);
  assert.equal(row.arrival.patch.tab, 'sessions');
  assert.equal(row.arrival.patch.traj, other);
  assert.equal(row.where, 'Agents › Sessions › Session inspector');
  assert.ok(row.also, 'the panel it was read from');
  // a session open on this page, under a portrait on Users as beside the list, is cited where it is open
  assert.match(source('lib/cites.ts'), /a\.patch\.traj != null && a\.patch\.traj === from\.traj \? \{ \.\.\.a, patch: \{ \.\.\.a\.patch, tab: from\.tab \} \}/);
});

test("the popped-out tree's way back opens its session on the Sessions tab", async () => {
  const { dashboardHref } = await ui.load('lib/popout.ts');
  const had = globalThis.window;
  globalThis.window = { location: { pathname: '/' } };
  try {
    const href = dashboardHref({ sid: SID, win: 60, from: 100, hide: true, typos: true, order: 'newest' });
    const p = new URLSearchParams(href.split('?')[1]);
    assert.equal(p.get('tab'), 'sessions');
    assert.equal(p.get('traj'), SID);
    assert.equal(p.get('tfrom'), '100');
    assert.equal(p.get('torder'), 'newest');
  } finally {
    globalThis.window = had;
  }
});
