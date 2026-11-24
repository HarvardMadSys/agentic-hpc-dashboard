/* A self-check for the kinds of nothing a panel can be (`src/components/Panel.tsx`, `src/lib/panels.ts`).
 *
 *   npm run check:panels        (node --test mock/check_panels.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. A panel whose reducer raised arrives as `{_error: "..."}` inside its envelope
 * (`windows.materialize`); a panel still waited for, or building, has no body yet. None of those is
 * "none", and none of them is a 0. The tabs are rendered (mock/ssr.mjs) on the fixture with one panel
 * swapped for such an envelope.
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
  ui = await renderer('panels');
});
after(() => ui?.close());

/** The envelope the service sends for a panel whose reducer raised: its feed's status, and the error. */
const failed = (why = 'division by zero') => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null, _error: why,
});
/** A window whose first build is still running: nothing to show yet. */
const firstBuild = {
  minutes: 1440, label: '24h', state: 'building', error: null, has_payload: false,
  progress: { pct: 40, records: 1000, bytes: 0, total_bytes: 0, files: 2, elapsed_s: 3 },
  built_at: null, built_age_s: null, covers_from: null, covers_to: null, drift_s: null,
  drift_budget_s: 0, live_records: 0, replay_dropped: 0,
};
const NOT_REDUCED = /the service could not reduce this panel: division by zero/;

test('pick hands on a panel whose reducer raised as not present, its error kept, and the same object each time', async () => {
  const L = await ui.load('lib/panels.ts');
  const P = { slurm: failed() };
  const a = L.pick(P, 'slurm', 'ebpfm');
  assert.equal(a._present, false, 'on develop it was present, and its plates read 0');
  assert.equal(a._error, 'division by zero');
  assert.equal(L.pick(P, 'slurm', 'ebpfm'), a, 'one object for one envelope, so a memo holds');
  assert.equal(L.hasBody(a), false);
  assert.equal(L.hasBody(FIX.tool_mix), true);
  assert.equal(L.hasBody(L.pick({}, 'x', 'ebpfm')), false, 'waiting');
  assert.equal(L.hasBody({ ...FIX.tool_mix, _window: firstBuild }), false, 'a first build');
  assert.equal(L.hasBody({ ...FIX.tool_mix, _window: { ...firstBuild, has_payload: true } }), true, 'a rebuild keeps its body');
});

test('<Panel> says the service could not reduce it, and neither "none" nor a missing feed', async () => {
  const html = await ui.render('components/Panel.tsx', 'Panel', {
    title: 'Sessions', p: failed(), empty: true, emptyWhat: 'no session was read in this window', children: 'rows',
  });
  assert.match(textOf(html), NOT_REDUCED);
  assert.doesNotMatch(html, /no session was read|rows|no data from|no configured path/);
});

test('the Slurm plates read a dash, not 0, when the Slurm panel could not be reduced', async () => {
  const text = textOf(await ui.render('tabs/SlurmTab.tsx', 'SlurmTab', { P: { ...FIX, slurm: failed() }, live: null, cls: ALL }));
  for (const k of ['jobs submitted', 'sessions polling the queue', 'submissions refused']) {
    const after = text.split(k)[1];
    assert.ok(after, k);
    // on develop: 0, the plate's number for a panel with no body
    assert.match(after, /^[^0-9—]*—/, `${k}: ${after.slice(0, 40)}`);
  }
  assert.match(text, NOT_REDUCED);
});

test('Agents says the cascades could not be reduced, not that no agent ran', async () => {
  const text = textOf(await ui.render('tabs/AgentsTab.tsx', 'AgentsTab', { P: { ...FIX, cascades: failed() }, cls: ALL }));
  // on develop: "none · no agent process in this window", and no panel to say why
  assert.doesNotMatch(text, /no agent process in this window/);
  assert.match(text, NOT_REDUCED);
});

test('Sessions says the sessions could not be reduced, not that none was read', async () => {
  const traj = { data: failed(), error: null, loading: false, reload: () => {} };
  const text = textOf(await ui.render('tabs/TrajectoriesTab.tsx', 'TrajectoriesTab', {
    traj, rank: 'recent', setRank: () => {}, cls: ALL, win: null, sel: null, select: () => {}, tfrom: null, setTfrom: () => {},
  }));
  assert.doesNotMatch(text, /no session was read in this window/);
  assert.match(text, NOT_REDUCED);
});

test('Agents waits with the service, and shows a first build, rather than saying none', async () => {
  const waiting = textOf(await ui.render('tabs/AgentsTab.tsx', 'AgentsTab', { P: {}, cls: ALL }));
  // on develop: "none · no agent process in this window" before the service answered
  assert.doesNotMatch(waiting, /no agent process/);
  assert.match(waiting, /waiting for the dashboard service/);
  const building = textOf(await ui.render('tabs/AgentsTab.tsx', 'AgentsTab', {
    P: { ...FIX, cascades: { ...FIX.cascades, trees: [], _window: firstBuild } }, cls: ALL,
  }));
  assert.doesNotMatch(building, /no agent process/);
  assert.match(building, /reducing 24h/);
  // a window that was read and holds no agent says so
  const none = textOf(await ui.render('tabs/AgentsTab.tsx', 'AgentsTab', {
    P: { ...FIX, cascades: { ...FIX.cascades, trees: [] } }, cls: ALL,
  }));
  assert.match(none, /none · no agent process in this window/);
});

test('Risks says the risk panel is waited for, building, not reduced or not sent, rather than that it has no sections', async () => {
  const risks = async (P) => textOf(await ui.render('tabs/RiskIoTab.tsx', 'RiskIoTab', { P, cls: ALL, part: 'risk' }));
  const NO_SECTIONS = /lists no sections/;
  // on develop each of these read "No risk sections · none · this cluster's risk profile lists no sections"
  const waiting = await risks({});
  assert.doesNotMatch(waiting, NO_SECTIONS);
  assert.match(waiting, /waiting for the dashboard service/);
  const failedRisk = await risks({ ...FIX, risk: failed() });
  assert.doesNotMatch(failedRisk, NO_SECTIONS);
  assert.match(failedRisk, NOT_REDUCED);
  const building = await risks({ ...FIX, risk: { ...failed(), _error: undefined, _window: firstBuild } });
  assert.doesNotMatch(building, NO_SECTIONS);
  assert.match(building, /reducing 24h/);
  // the fixture sends no risk panel at all
  const unsent = await risks(FIX);
  assert.doesNotMatch(unsent, NO_SECTIONS);
  assert.match(unsent, /not returned/);
  // a risk panel the service reduced, whose profile lists no section, says so
  const none = await risks({ ...FIX, risk: { ...failed(), _error: undefined, sections: [] } });
  assert.match(none, /none · this cluster's risk profile lists no sections/);
});
