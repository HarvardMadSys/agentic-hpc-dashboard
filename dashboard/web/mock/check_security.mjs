/* A self-check for the Security tab (`src/tabs/SecurityTab.tsx`).
 *
 *   node --test mock/check_security.mjs        (and `npm run check`, which runs every check_*.mjs)
 *
 * Like check_panels.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The tab is rendered (mock/ssr.mjs) on the fixture, whole and with one panel
 * swapped for each kind of nothing. Every check is listed with what it found, so the one mistake it
 * must never make is to say "none" -- or 0 -- of a panel that holds no body: waited for, still being
 * read, not reduced, not returned, or with the agent class filtered out.
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
let S;
before(async () => {
  ui = await renderer('security');
  S = await ui.load('tabs/SecurityTab.tsx');
});
after(() => ui?.close());

const failed = (why = 'division by zero') => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null, _error: why,
});
const firstBuild = {
  minutes: 1440, label: '24h', state: 'building', error: null, has_payload: false,
  progress: { pct: 40, records: 1000, bytes: 0, total_bytes: 0, files: 2, elapsed_s: 3 },
  built_at: null, built_age_s: null, covers_from: null, covers_to: null, drift_s: null,
  drift_budget_s: 0, live_records: 0, replay_dropped: 0,
};
/** A reduced risk panel with one credentials section, as the service sends it. */
const risk = (rows) => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null,
  sections: [
    { id: 'commands', kind: 'commands', title: 'Commands that should not run here', rules: [] },
    { id: 'credentials', kind: 'credentials', title: 'Credentials on a command line', rules: ['openai_key'] },
  ],
  secrets: { rows, args_scanned: 41102, args_truncated: 0, truncation_pct: 0 },
});
const KEY_ROW = {
  id: 'openai_key', pattern: 'OpenAI/Anthropic-style key literal', severity: 'HIGH', n: 3, users: 2,
  tools: ['curl'], classes: { agent: 2, human: 1 },
};

const tab = async (P, cls = ALL) => textOf(await ui.render('tabs/SecurityTab.tsx', 'SecurityTab', { P, cls }));
/** the checks panel alone: the Sandbox panels under it have checks of their own (check_panels.mjs) */
const checksText = async (P, cls = ALL) => textOf(await ui.render('tabs/SecurityTab.tsx', 'SecurityChecks', { P, cls }));
const byKey = (checks) => Object.fromEntries(checks.map((c) => [c.key, c]));
/** "none", a check made, or a 0 count: what a check with no body must never say */
const NONE = /\bNo agent\b|none on an agent|(?:^|\s)0 agent/;

test('on the fixture, each check says what it found, and the one with no panel says it was not checked', () => {
  const c = byKey(S.securityChecks(FIX, ALL));
  assert.equal(c['launch:bypassed'].state, 'flag');
  assert.match(c['launch:bypassed'].what, /^2 agent sessions launched with a bypass flag$/);
  // the share of the sessions it can judge, as the approval table under it: pc() rounds a share of
  // 10% or more to a whole one
  assert.match(c['launch:bypassed'].note, /^12% of the 17 agent sessions whose launch flags are readable · it acts without asking$/);
  assert.equal(c['launch:auto'].state, 'flag');
  assert.match(c['launch:auto'].what, /^7 agent sessions launched in auto mode$/);
  assert.equal(c['sandbox:not'].state, 'flag');
  assert.match(c['sandbox:not'].what, /^708 agent tool calls not sandboxed$/);
  // the same share as the tool-call table under it: sandboxed of the known, a failed start counted as not
  assert.match(c['sandbox:not'].note, /^5\.3% of the 778 agent tool calls whose outcome is known ran sandboxed · 6 agent sessions had none sandboxed$/);
  assert.equal(c['sandbox:failed'].state, 'flag');
  assert.match(c['sandbox:failed'].what, /^29 agent sandboxes failed to start$/);
  assert.match(c['sandbox:failed'].note, /^of 70 sandbox wrapper calls/);
  // the fixture sends no risk panel: not checked, and never none
  assert.equal(c.cred.state, 'unchecked');
  assert.match(c.cred.note, /the risk panel was not returned/);
  assert.equal(c['reach:outside'].state, 'flag');
  assert.match(c['reach:outside'].what, /^1 agent wrote outside its start folder$/);
  assert.match(c['reach:outside'].note, /^of 4 agents · into another user’s home 1, shared storage 1, temp 1 · paths read from the text of their commands$/);
  assert.equal(c['reach:home'].state, 'flag');
  assert.match(c['reach:home'].note, /1 wrote or deleted there$/);
});

test('every link into the Security tab lands on a panel the tab draws', async () => {
  const html = await ui.render('tabs/SecurityTab.tsx', 'SecurityTab', { P: FIX, cls: ALL });
  const into = S.securityChecks(FIX, ALL).filter((c) => c.to?.patch.tab === 'security');
  assert.ok(into.length >= 4);
  for (const c of into) assert.match(html, new RegExp(`id="${c.to.anchor}"`), `${c.key} -> ${c.to.anchor}`);
});

test('the writers are every agent the service counts as writing outside, the places the most alarming first', () => {
  const ws = FIX.cascades.workspace;
  const writers = S.writersOf(ws);
  assert.equal(writers.length, ws.summary.with_outside_writes);
  const places = S.placesOf(writers);
  assert.deepEqual(places.map((p) => p.kind), ["other user's home", 'shared storage', 'temp']);
  assert.deepEqual(places.map((p) => p.written), [1, 2, 4]);
  // a place the agent only read, or ran in, is not a place it wrote
  assert.ok(!places.some((p) => p.kind === 'filesystem root' || p.kind === 'cache & tooling'));
});

test('waiting for the service, nothing is none: every check is not checked', async () => {
  const checks = S.securityChecks({}, ALL);
  assert.ok(checks.length >= 4);
  for (const c of checks) {
    assert.equal(c.state, 'unchecked', c.key);
    assert.match(c.note, /waiting for the dashboard service/, c.key);
  }
  const text = await checksText({});
  assert.doesNotMatch(text, NONE);
  assert.match(text, /4 not checked/);
  assert.doesNotMatch(text, /nothing flagged/);
});

test('a sandbox panel not reduced, or still being read, is not checked, never none', async () => {
  for (const [sandbox, why] of [
    [failed(), /the service could not reduce the sandbox panel: division by zero/],
    [{ ...FIX.sandbox, _window: firstBuild }, /the window is still being read \(40%\)/],
  ]) {
    const c = byKey(S.securityChecks({ ...FIX, sandbox }, ALL));
    for (const k of ['launch', 'sandbox']) {
      assert.equal(c[k].state, 'unchecked', k);
      assert.match(c[k].note, why, k);
    }
    assert.equal(c['launch:bypassed'], undefined);
    assert.equal(c['sandbox:failed'], undefined);
    const text = await checksText({ ...FIX, sandbox });
    assert.doesNotMatch(text, /No agent session launched|No agent tool call|No agent sandbox/);
    assert.match(text, /not checked: Approval at launch: not checked/);
  }
});

test('with the agent class filtered out, the agent checks say they were not applied', async () => {
  const checks = S.securityChecks({ ...FIX, risk: risk([KEY_ROW]) }, ['human', 'ide']);
  for (const c of checks) {
    assert.equal(c.state, 'unchecked', c.key);
    assert.equal(c.note, 'the agent class is filtered out', c.key);
  }
  assert.doesNotMatch(await checksText(FIX, ['human', 'ide']), NONE);
  // the writes panel, which the filter empties, says why
  assert.match(await tab(FIX, ['human', 'ide']), /none · the agent class is filtered out/);
});

test('credentials: the agent’s count leads, every class beside it, and none is said only of a reduced panel', () => {
  const hit = byKey(S.securityChecks({ ...FIX, risk: risk([KEY_ROW]) }, ALL))['cred:credentials'];
  assert.equal(hit.state, 'flag');
  // each rule counts its own matches: a sum of them is never a count of command lines
  assert.equal(hit.what, 'Credentials on a command line: 2 matches on agent command lines');
  assert.deepEqual(hit.by, { agent: 2, human: 1, ide: 0 });
  assert.match(hit.note, /^OpenAI\/Anthropic-style key literal ×2 · a line can match more than one pattern · the values are never shown · 41,102 command lines scanned$/);
  assert.equal(hit.to.patch.tab, 'risk');
  assert.equal(hit.to.anchor, 'sec-credentials-on-a-command-line');
  const none = byKey(S.securityChecks({ ...FIX, risk: risk([]) }, ALL))['cred:credentials'];
  assert.equal(none.state, 'clear');
  assert.equal(none.what, 'Credentials on a command line: none on an agent’s');
  // a reduced risk panel whose profile counts no credential rule: not checked, and why
  const off = byKey(S.securityChecks({ ...FIX, risk: { ...risk([]), sections: [] } }, ALL)).cred;
  assert.equal(off.state, 'unchecked');
  assert.match(off.note, /no credentials section/);
  const unreduced = byKey(S.securityChecks({ ...FIX, risk: failed() }, ALL)).cred;
  assert.equal(unreduced.state, 'unchecked');
  assert.match(unreduced.note, /could not reduce the risk panel/);
});

test('the writes panel: none only of agent trees that were read, and why when they were not', async () => {
  const none = await tab({ ...FIX, cascades: { ...FIX.cascades, workspace: { ...FIX.cascades.workspace, agents: [] } } });
  assert.match(none, /none · no agent wrote outside its start folder in this window/);
  const unreduced = await tab({ ...FIX, cascades: failed() });
  assert.match(unreduced, /the service could not reduce this panel: division by zero/);
  assert.doesNotMatch(unreduced, /no agent wrote outside/);
  const bare = await tab({ ...FIX, cascades: { ...FIX.cascades, workspace: null } });
  assert.match(bare, /not returned/);
  assert.doesNotMatch(bare, /no agent wrote outside/);
  const shown = await tab(FIX);
  assert.match(shown, /1 agent of 4 wrote or deleted outside the folder it was started in/);
  assert.match(shown, /another user’s home/);
  // the most alarming path first, whatever order the payload lists them in: the fixture's lists
  // the shared-storage writes before the one into another user's home
  const home = shown.indexOf('~aparker/shared-data/labels.csv');
  const scratch = shown.indexOf('/n/holyscratch01/lab/thomwg11/era5/cache.zarr');
  assert.ok(home > 0 && scratch > 0 && home < scratch, `${home} < ${scratch}`);
});
