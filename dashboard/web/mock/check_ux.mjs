/* A self-check for what a first-time reader meets: an instance with no data says so instead of
 * looking healthy; a figure nobody measured is never a 0; one judgment per concept (the scheduler's
 * rules flag a session on Live as on HPC); every time in one zone; a class told by its word as well as
 * its colour; and a session titled by whose it is.
 *
 *   npm run check:ux            (node --test mock/check_ux.mjs)
 *
 * Like check_shares.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The parts are rendered (mock/ssr.mjs) on the fixture's numbers and on hand-made ones.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { renderer, textOf } from './ssr.mjs';

const api = JSON.parse(readFileSync(fileURLToPath(new URL('./api.json', import.meta.url)), 'utf8'));
const FIX = api.panels.panels;
const ALL = ['agent', 'human', 'ide'];

let ui;
before(async () => {
  ui = await renderer('ux');
});
after(() => ui?.close());

const env = (body) => ({ _feed: 'ebpf', _status: 'ok', _present: true, _paths: [], _notice: null, ...body });
const feed = (name, status, extra = {}) => ({
  name, tier: name, label: name, cadence: null, configured: [`/x/${name}`], resolved: status === 'missing' ? [] : [`/x/${name}`],
  missing: status === 'missing' ? [`/x/${name}`] : [], origin: null, env: null, cli: null, exists: status !== 'missing',
  status, required: name === 'ebpf', n_files: 0, bytes: 0, days: [], hosts: [], newest_record_ts: null, lag_s: null,
  lag: null, max_age_s: 300, rows: 0, notice: null, backfill: null, ...extra,
});

test('a risk panel not present counts nothing: the plate says not checked, never 0 commands', async () => {
  // the service's envelope for a feed with no records still carries the empty kinds
  const P = { ...FIX, risk: env({ _present: false, _status: 'empty', dangerous: { rows: [] }, secrets: { rows: [] } }) };
  const html = await ui.render('components/WindowPlates.tsx', 'WindowPlates', { P, cls: ALL, traj: null });
  const risky = textOf(html.split('<div class="plate').find((p) => p.includes('risky actions')) ?? '');
  // on develop: "0 commands that should not run here · 0 credentials on a command line"
  assert.doesNotMatch(risky, /\b0 commands/);
  assert.doesNotMatch(risky, /\b0 credentials/);
  assert.match(risky, /not checked: no records to check yet/);
});

test('each class swatch on a plate carries its word, and the headline says whose it is', async () => {
  const html = await ui.render('components/WindowPlates.tsx', 'WindowPlates', { P: FIX, cls: ALL, traj: null });
  const first = html.split('<div class="plate').find((x) => x.includes('processes exited')) ?? '';
  for (const w of ['agent', 'human', 'IDE']) assert.match(first, new RegExp(`<span class="cl">${w}</span>`));
  assert.match(first, /<small class="whose"> agent<\/small>/);
  // the label names the measure: no "agent processes exited" over a row that counts all three classes
  assert.match(textOf('<div class="plate' + first), /^processes exited/);
});

test('polling is counted, not flagged: Needs attention flags only a session that broke a scheduler rule', async () => {
  const slurm = (agent) =>
    env({
      by_class: { agent: { polling_sessions: 5, polling_h: 1, rejected: 2, rejected_pct: 17, ...agent } },
      rejections: [],
      abnormal: { rules: [{ id: 'fast_polling', gap_s: 30, min_s: 1800 }, { id: 'refused', min: 3 }], sessions: 0, served: 0 },
    });
  const quiet = textOf(await ui.render('tabs/Attention.tsx', 'Attention', { P: { ...FIX, slurm: slurm({ abnormal_sessions: 0, abnormal_by: { fast_polling: 0, refused: 0 } }) }, cls: ALL, feeds: null }));
  // on develop: "5 agent sessions polled the queue" above an HPC table that said no session broke a rule
  assert.doesNotMatch(quiet, /polled the queue/);
  assert.doesNotMatch(quiet, /refused by the scheduler/);
  assert.match(quiet, /checked: [^·]*the scheduler/);
  const broke = textOf(await ui.render('tabs/Attention.tsx', 'Attention', { P: { ...FIX, slurm: slurm({ abnormal_sessions: 2, abnormal_by: { fast_polling: 2, refused: 0 } }) }, cls: ALL, feeds: null }));
  assert.match(broke, /2 agent sessions broke a rule at the scheduler/);
  assert.match(broke, /polling the queue fast ×2/);
});

test('green only for a full check: with a source not checked, nothing flagged says it is about part of the page', async () => {
  const P = { ...FIX };
  delete P.risk;
  delete P.slurm;
  const html = await ui.render('tabs/Attention.tsx', 'Attention', { P, cls: ALL, feeds: null });
  if (!/class="pill stale"/.test(html)) {
    assert.doesNotMatch(html, /class="pill ok">nothing flagged</);
    assert.match(textOf(html), /nothing flagged in what was checked/);
  }
});

test('a required feed that is there but empty is flagged, not left under an all-clear', async () => {
  const feeds = { ebpf: feed('ebpf', 'empty'), ebpf_node: feed('ebpf_node', 'empty') };
  const text = textOf(await ui.render('tabs/Attention.tsx', 'Attention', { P: FIX, cls: ALL, feeds }));
  assert.match(text, /ebpf has no records/);
});

test('no feed holds a record: the setup screen, which names every folder it looked in', async () => {
  const S = await ui.load('components/SetupScreen.tsx');
  const none = {
    ebpf: feed('ebpf', 'missing', { candidates: [{ path: '/scratch/ebpfm', state: 'missing', note: null }, { path: '/var/log/ebpfm', state: 'denied', note: 'you: rc; folder root:ebpfm 0750' }] }),
    ebpf_node: feed('ebpf_node', 'empty'),
  };
  assert.equal(S.noData(none), true);
  assert.equal(S.noData({ ...none, ebpf: feed('ebpf', 'ok', { n_files: 3 }) }), false, 'one feed with records: the tabs');
  assert.equal(S.noData({ ...none, ebpf: feed('ebpf', 'empty', { synthetic_hosts: ['demo-login01'] }) }), false, 'the demo is data');
  assert.equal(S.noData(null), false, 'no report yet is not no data');
  const text = textOf(await ui.render('components/SetupScreen.tsx', 'SetupScreen', { feeds: none, goFeeds: () => {} }));
  assert.match(text, /No data yet/);
  assert.match(text, /\/scratch\/ebpfm — no such folder/);
  assert.match(text, /\/var\/log\/ebpfm — permission denied \(you: rc; folder root:ebpfm 0750\)/);
  assert.match(text, /rc-dashboard check-feeds/);
  // the shell gives way to it, and no green light shines over it
  const app = readFileSync(fileURLToPath(new URL('../src/App.tsx', import.meta.url)), 'utf8');
  // ... every tab but Feeds, where it looked, and Trend, whose roll-up table outlives the
  // day-files it was made from
  assert.match(app, /const setup = empty && tab\.k !== 'feeds' && tab\.k !== 'trend';/);
  assert.match(app, /if \(setup\) return <SetupScreen/);
  assert.match(app, /live\.transport === 'ws' && !empty \? '' : ' off'/);
});

test('the popped-out tree reads times in the service’s zone, as the dashboard does', async () => {
  // main.tsx renders TreePopout in place of App for a pop-out URL: the zone is set by a
  // hook both call, and each draws again once it is known
  const src = (f) => readFileSync(fileURLToPath(new URL('../src/' + f, import.meta.url)), 'utf8');
  for (const f of ['App.tsx', 'tabs/TreePopout.tsx']) {
    assert.match(src(f), /const tzKey = useServiceZone\(config\.data/, f);
  }
  assert.match(src('tabs/TreePopout.tsx'), /<div className="tp" key=\{tzKey\}>/);
  assert.match(src('main.tsx'), /<TreePopout \/>/);
});

test('a password in a hash file is a password: the rail offers to sign out', async () => {
  const A = await ui.load('lib/auth.ts');
  const auth = (hash, file) => ({ server: { auth: { password_hash: { _value: hash }, password_hash_file: { _value: file } } } });
  assert.equal(A.asksPassword(auth('(hidden)', '')), true, 'the hash, inline');
  assert.equal(A.asksPassword(auth('', '/etc/rc-dashboard/password.hash')), true, 'the hash file, as init and the kits set it');
  assert.equal(A.asksPassword(auth('', '')), false, 'an open page has no one to sign out');
  assert.equal(A.asksPassword(null), false, 'no config yet');
  const app = readFileSync(fileURLToPath(new URL('../src/App.tsx', import.meta.url)), 'utf8');
  assert.match(app, /const passwordSet = asksPassword\(config\.data/);
});

test('every time on the page is in the service’s zone, named in the head', async () => {
  const Z = await ui.load('lib/zone.ts');
  const t = Date.UTC(2026, 10, 23, 22, 36, 5); // 22:36:05 UTC, 17:36:05 in New York (EST)
  Z.setZone({ name: 'America/New_York', abbrev: 'EST', utc_offset_s: -18000 });
  assert.equal(Z.hms(t), '17:36:05');
  assert.equal(Z.monthDay(t), '11-23');
  assert.equal(Z.offsetAt(t), -18000);
  assert.equal(Z.zinstant(Z.zdate(t)), Math.floor(t / 1000) * 1000, 'a wall clock and back');
  assert.equal(Z.zoneLabel().short, 'EST');
  assert.match(Z.zoneLabel().tip, /America\/New_York/);
  // daylight saving: July in New York is UTC−4
  assert.equal(Z.offsetAt(Date.UTC(2026, 6, 1, 12)), -14400);
  // only an offset known: that offset
  Z.setZone({ name: null, abbrev: null, utc_offset_s: 19800 });
  assert.equal(Z.hms(t), '04:06:05');
  const F = await ui.load('lib/format.ts');
  assert.equal(F.clock(t / 1000), '04:06:05', 'clock() reads the same zone');
  Z.setZone(null);
});

test('a session is titled by whose it is and what ran it, its pid under the title', async () => {
  const S = await ui.load('lib/sessions.ts');
  const a = S.sessionTitle({ sid: 'demo-login02|2026-11-23|ide|apid:680003', agent_type: 'vscode', user: 'wen' });
  const b = S.sessionTitle({ sid: 'demo-login02|2026-11-23|ide|apid:680003', agent_type: 'vscode', user: 'xin' });
  assert.equal(a.title, 'wen · VS Code');
  assert.equal(a.detail, 'pid 680003');
  assert.notEqual(a.title, b.title, 'two users’ sessions read apart');
  assert.equal(S.sessionTitle({ sid: 'h|2026-11-23|human|sess:163', user: 'tim' }).title, 'tim · login session');
});

test('Feeds says a failed copy is a copy, and which code each collector and the service are', () => {
  // FeedsTab fetches its own collectors, which SSR cannot, so its wiring is read as text
  const feeds = readFileSync(fileURLToPath(new URL('../src/tabs/FeedsTab.tsx', import.meta.url)), 'utf8');
  // on develop: a node whose copy failed read "silent: killed, crashed, or the host is down"
  assert.match(feeds, /\{h\.transport && \(\s*h\.transport\.ok \?/);
  assert.match(feeds, /copy failed\{h\.transport\.last_ok_age_s != null/);
  assert.match(feeds, /h\.running\.collector_version \? ` · \$\{h\.running\.collector_version\}` : ''/);
  assert.match(feeds, /rc-dashboard \{version\}/);
  const types = readFileSync(fileURLToPath(new URL('../src/api/types.ts', import.meta.url)), 'utf8');
  assert.match(types, /export interface CollectorTransport \{\s*kind: 'rsync' \| 'stream';\s*ok: boolean;\s*last_ok_age_s: number \| null;\s*error: string \| null;\s*\}/);
  assert.match(types, /candidates\?: FeedCandidate\[\];/);
});
