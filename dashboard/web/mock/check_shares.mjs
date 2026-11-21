/* A self-check for each class's share of a quantity (`src/lib/shares.ts`) and the panels that print one.
 *
 *   npm run check:shares        (node --test mock/check_shares.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The class chips hide a class; they must never change another class's share: a
 * share is out of every class the panel counted, `unlabeled` included, and a class whose value is not
 * known leaves no share at all. The panels are rendered (mock/ssr.mjs) on the fixture's numbers and on
 * hand-made ones.
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
  ui = await renderer('shares');
});
after(() => ui?.close());

/** An envelope the service would send, around `body`. */
const env = (body) => ({ _feed: 'ebpf', _status: 'ok', _present: true, _paths: [], _notice: null, ...body });

/** The share labels of the table row whose first cell says `unit`, in column order. */
function rowShares(html, unit) {
  const row = html.split('<tr').find((r) => r.includes(`>${unit}<`));
  assert.ok(row, `a row for ${unit}`);
  return [...row.matchAll(/<div class="share-cell">.*?<b>([^<]*)<\/b>/g)].map((m) => m[1]);
}

test('the whole is every class the panel counted, and a class not known leaves no share', async () => {
  const S = await ui.load('lib/shares.ts');
  const by = { agent: 500, human: 100, ide: 100, unlabeled: 300 };
  const w = S.wholeOf(Object.keys(by), (c) => by[c]);
  assert.deepEqual(w, { tot: 1000, unknown: [] });
  assert.equal(S.shareOf(500, w), 50);
  assert.equal(S.shareOf(0, w), 0);
  assert.equal(S.shareOf(null, w), null);
  const holed = S.wholeOf(['agent', 'human', 'ide'], (c) => (c === 'ide' ? null : 100));
  assert.deepEqual(holed, { tot: 200, unknown: ['ide'] });
  assert.equal(S.shareOf(100, holed), null, 'a share of a whole that is missing a class is no share');
  assert.match(S.noShare(holed), /^no share: IDE not measured/);
  assert.equal(S.noShare(w), null);
  assert.equal(S.shareOf(5, S.wholeOf([], () => 0)), null, 'nothing counted: no share');
  assert.deepEqual(S.countedIn({ agent: 1 }, null, { agent: 2, unlabeled: 3 }), ['agent', 'unlabeled']);
});

test('Who uses what: turning the IDE off changes no other class’s share of the processes', async () => {
  const draw = (cls) => ui.render('tabs/OverviewTab.tsx', 'OverviewTab', { P: FIX, cls, traj: null });
  const all = rowShares(await draw(ALL), 'processes exited');
  // 67.2%, 5.4% and 27.4%, as the page writes them
  assert.deepEqual(all, ['67%', '5.4%', '27%']);
  // on develop: 93% and 7.4%, the IDE's 27.4% handed to the other two
  assert.deepEqual(rowShares(await draw(['agent', 'human']), 'processes exited'), ['67%', '5.4%']);
  // on develop: 100%
  assert.deepEqual(rowShares(await draw(['agent']), 'processes exited'), ['67%']);
});

test('Who uses what: a class with no reading of a unit leaves the unit no share, and says why', async () => {
  // the fixture's human class has no network reading (n 0): the agent's and the IDE's bytes are
  // not shares of a whole without it
  const html = await ui.render('tabs/OverviewTab.tsx', 'OverviewTab', { P: FIX, cls: ALL, traj: null });
  assert.deepEqual(rowShares(html, 'network sent'), ['—', '—', '—']);
  assert.match(html, /no share: human not measured/);
  // and the units every class has a reading of keep theirs
  assert.deepEqual(rowShares(html, 'disk read'), ['69%', '8.0%', '23%']);
});

/** Which class cells of the row whose first cell says `unit` are marked as its largest share, in column order. */
function rowLeads(html, unit) {
  const row = html.split('<tr').find((r) => r.includes(`>${unit}<`));
  assert.ok(row, `a row for ${unit}`);
  return [...row.matchAll(/<td class="clsv( lead)?"/g)].map((m) => Boolean(m[1]));
}

/** The parts of the stacked bar in the row whose first cell says `unit`: each one's class, width and whether it is muted. */
function rowSplit(html, unit) {
  const row = html.split('<tr').find((r) => r.includes(`>${unit}<`));
  assert.ok(row, `a row for ${unit}`);
  return [...row.matchAll(/<i class="seg( muted)?" style="flex-grow:([\d.e+]+)[^"]*" data-tip="([^:"]*):/g)]
    .map((m) => ({ lbl: m[3], v: Number(m[2]), muted: Boolean(m[1]) }));
}

test('Who uses what: each unit’s largest share is marked, out of every class counted, whatever the chips show', async () => {
  const draw = (cls, P = FIX) => ui.render('tabs/OverviewTab.tsx', 'OverviewTab', { P, cls, traj: null });
  const html = await draw(ALL);
  assert.deepEqual(rowLeads(html, 'processes exited'), [true, false, false]);
  // the lead changes hands: the human class holds most of the peak memory
  assert.deepEqual(rowLeads(html, 'peak memory'), [false, true, false]);
  // a unit with no known whole has no lead
  assert.deepEqual(rowLeads(html, 'network sent'), [false, false, false]);
  // the agent off: no other class is marked in its place
  assert.deepEqual(rowLeads(await draw(['human', 'ide']), 'processes exited'), [false, false]);
  // `unlabeled` is part of the whole: when it holds the most, no class leads
  const by = { ...FIX.tool_mix.by_class, unlabeled: { calls: 9e6, cpu_s: 0, rows: [] } };
  const P = { ...FIX, tool_mix: env({ ...FIX.tool_mix, by_class: by }) };
  assert.deepEqual(rowLeads(await draw(ALL, P), 'processes exited'), [false, false, false]);
});

test('Who uses what: a unit’s stacked bar is its whole, whatever the chips show', async () => {
  const draw = (cls, P = FIX) => ui.render('tabs/OverviewTab.tsx', 'OverviewTab', { P, cls, traj: null });
  const calls = FIX.tool_mix.by_class;
  const want = [
    { lbl: 'agent', v: calls.agent.calls, muted: false },
    { lbl: 'human', v: calls.human.calls, muted: false },
    { lbl: 'IDE', v: calls.ide.calls, muted: false },
  ];
  assert.deepEqual(rowSplit(await draw(ALL), 'processes exited'), want);
  // the IDE off: its part stays, as no class, rather than handed to the other two
  assert.deepEqual(rowSplit(await draw(['agent', 'human']), 'processes exited'), [want[0], want[1], { ...want[2], muted: true }]);
  // hover gives each part its share of the whole
  assert.match(await draw(ALL), /data-tip="agent: 67% of processes exited · 4,812,446"/);
  // `unlabeled` is a part of its own, in its place after the classes
  const by = { ...calls, unlabeled: { calls: 1000, cpu_s: 0, rows: [] } };
  const P = { ...FIX, tool_mix: env({ ...FIX.tool_mix, by_class: by }) };
  assert.deepEqual(rowSplit(await draw(ALL, P), 'processes exited').map((p) => p.lbl), ['agent', 'human', 'IDE', 'unlabeled']);
  // no known whole, no bar: the reason where it would be
  const html = await draw(ALL);
  assert.deepEqual(rowSplit(html, 'network sent'), []);
  const net = html.split('<tr').find((r) => r.includes('>network sent<'));
  assert.match(textOf(net), /no share: human not measured/);
});

/** The fixture's panels with a tool mix of `calls` per class and a risk panel holding `risk`. */
function plates(calls, risk) {
  const by = Object.fromEntries(Object.entries(calls).map(([c, n]) => [c, { calls: n, cpu_s: null, rows: [] }]));
  return { ...FIX, tool_mix: env({ ...FIX.tool_mix, by_class: by, by_type: {} }), risk: env(risk ?? {}) };
}
const platesText = async (P, cls) =>
  textOf(await ui.render('components/WindowPlates.tsx', 'WindowPlates', { P, cls, traj: null }));

test('the agent’s share of every process is out of every class, `unlabeled` too, whatever the chips show', async () => {
  const P = plates({ agent: 500, human: 100, ide: 100, unlabeled: 300 });
  // on develop: 71% (unlabeled never counted), 100% (only agent), 250% (agent off)
  assert.match(await platesText(P, ALL), /50% of every process on the/);
  assert.match(await platesText(P, ['agent']), /50% of every process on the/);
  const off = await platesText(P, ['human', 'ide']);
  assert.doesNotMatch(off, /of every process/, 'the agent class filtered out: its plate says nothing of it');
  assert.doesNotMatch(off, /250%/);
});

test('the risky-actions plate has no per-class zeros for kinds this cluster does not count', async () => {
  const html = await ui.render('components/WindowPlates.tsx', 'WindowPlates', { P: plates({ agent: 5 }, {}), cls: ALL, traj: null });
  const risky = html.split('<div class="plate').find((p) => p.includes('risky agent actions'));
  assert.ok(risky);
  // on develop: a class row of 0 · 0 · 0 under a total of —
  assert.doesNotMatch(risky, /class="cls3"/);
  assert.match(textOf(risky), /no command or credential section on this cluster/);
  const counted = await ui.render('components/WindowPlates.tsx', 'WindowPlates', {
    P: plates({ agent: 5 }, { dangerous: { rows: [{ id: 'rm', classes: { agent: 2, human: 1 } }] } }),
    cls: ALL,
    traj: null,
  });
  const row = counted.split('<div class="plate').find((p) => p.includes('risky agent actions'));
  assert.match(row, /class="cls3"/, 'a counted kind keeps its class row');
});

test('Tool mix, by class: a row’s shares are of every class, whatever the chips show', async () => {
  const draw = async (cls) => textOf(await ui.render('tabs/ToolPanels.tsx', 'ToolPanels', { P: FIX, cls }));
  const tm = FIX.tool_mix.by_class;
  const poll = (c) => tm[c].rows.find((r) => r.b === 'poll/probe').calls;
  const want = (100 * poll('agent')) / (poll('agent') + poll('human') + poll('ide'));
  const pct = want < 10 ? want.toFixed(1) : String(Math.round(want));
  const row = (t) => t.split('poll/probe')[1].split('slurm-submit')[0];
  assert.match(row(await draw(ALL)), new RegExp(`\\b${pct}%`));
  assert.match(row(await draw(['agent', 'human'])), new RegExp(`\\b${pct}%`), 'the IDE off: the agent’s share is the same');
});

test('the Disk plates: the agent’s share of all disk reads is the same whatever the chips show', async () => {
  const draw = async (cls) =>
    textOf(await ui.render('tabs/RiskIoTab.tsx', 'RiskIoTab', { P: FIX, cls, part: 'io', plates: true }));
  const by = FIX.io_process.by_class;
  const want = Math.round((100 * by.agent.rd_mb) / (by.agent.rd_mb + by.human.rd_mb + by.ide.rd_mb));
  assert.match(await draw(ALL), new RegExp(`${want}% of all disk reads`));
  assert.match(await draw(['agent']), new RegExp(`${want}% of all disk reads`));
});

/** The cells of the first row reading `state` after `from` in some markup: the share-cell's number, or the count. */
function stateRow(html, from, state) {
  const row = html.slice(html.indexOf(from)).split('<tr').find((r) => r.includes(`>${state}<`));
  assert.ok(row, `a row for ${state} after ${from}`);
  return [...row.matchAll(/<td[^>]*>(.*?)<\/td>/g)].slice(1).map(([, td]) => {
    const share = td.match(/<b>([^<]*)<\/b>/);
    return share ? share[1] : textOf(td);
  });
}

test('the Sandbox tables: a state is a share of the judged ones, as the plates above them say', async () => {
  const html = await ui.render('tabs/SandboxTab.tsx', 'SandboxTab', { P: FIX, cls: ALL });
  const s = FIX.sandbox.approval_by_class_sessions.agent;
  assert.equal(s.bypassed + s.auto + s.supervised, 17);
  // the plate: 2 of the 17 sessions whose launch flags are readable, 11.8%
  assert.match(textOf(html), /agent sessions launched with a bypass flag \? 12% of 17/);
  // on develop the table under it said 10% (2 of 20)
  assert.deepEqual(stateRow(html, 'Approval at launch', 'bypass flag'), ['12%']);
  // tool calls: 41 of 778 judged (on develop 5.2%, of 785)
  assert.deepEqual(stateRow(html, 'did they run sandboxed', 'sandboxed'), ['5.3%']);
  // sessions: 1 of the 10 whose tool calls were judged (on develop 5.0%, of 20)
  assert.deepEqual(stateRow(html, 'per session', 'all tool calls sandboxed'), ['10%']);
});

test('the Sandbox tables: unknown, no tool calls and exited-before-checked are counts, with the coverage beside the class', async () => {
  const html = await ui.render('tabs/SandboxTab.tsx', 'SandboxTab', { P: FIX, cls: ALL });
  // on develop: shares of every state, 5.0% and 45%
  assert.deepEqual(stateRow(html, 'Approval at launch', 'unknown'), ['1']);
  assert.deepEqual(stateRow(html, 'per session', 'no tool calls seen'), ['9']);
  assert.deepEqual(stateRow(html, 'per process', 'exited before it was checked'), ['412', '5', '0']);
  // 17 of 20 sessions judged, under the class's name (its tip's ? and its sort arrow between them)
  assert.match(textOf(html.slice(html.indexOf('Approval at launch'))), /agent[^a-z]*20 sessions cov 85%/);
});
