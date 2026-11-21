/* A self-check for the HPC tab's Abnormal Slurm activity (`src/tabs/SlurmTab.tsx`).
 *
 *   npm run check:hpc        (and `npm run check`, which runs every check_*.mjs)
 *
 * Like check_panels.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The fixture leaves the Slurm panel out (it is how the page is tried without
 * one), so this builds one in the service's shape: two agent sessions and a person that broke a
 * rule, and a person who broke none. The service judges every session and holds every threshold;
 * the page words the rules from the thresholds it is sent, gives each session its own numbers as
 * the evidence, and never says "none" of a panel it has no body for.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { renderer, textOf } from './ssr.mjs';

const api = JSON.parse(readFileSync(fileURLToPath(new URL('./api.json', import.meta.url)), 'utf8'));
const FIX = api.panels.panels;
const ALL = ['agent', 'human', 'ide'];
const T = 1791518400;

let ui;
let S;
before(async () => {
  ui = await renderer('hpc');
  S = await ui.load('tabs/SlurmTab.tsx');
});
after(() => ui?.close());

const RULES = [
  { id: 'polling', gap_s: 180, min_s: 600 },
  { id: 'query_burst', min: 30 },
  { id: 'submit_burst', min: 20 },
  { id: 'refused', min: 3 },
  { id: 'cancels', min: 10 },
  { id: 'failed_runs', min: 5 },
];
const NONE_BROKEN = Object.fromEntries(RULES.map((r) => [r.id, 0]));

const row = (o) => ({
  cls: 'agent', user: 'ada', host: 'login01', agent_type: 'claude_code', session: 'login01:100', keys: 1,
  fold: null, first: T, last: T + 3600, jobs: 0, outcomes: {}, rejected: 0, dry_runs: 0, tools: {},
  peak_per_min: 0, shapes: 0, shapes_capped: false, flags: {}, argv_req_pct: null, partitions: {}, gpus: 0,
  gpu_jobs: 0, gpu_h: null, gpu_h_known: 0, queries: 0, queries_by: {}, queries_per_job: null, polls: 0,
  poll_gap_s: null, loop: null, queries_unplaced: 0, queries_peak_per_min: 0, cancels: 0, runs: 0,
  runs_failed: 0, fails: [], args: null, abnormal: [], ...o,
});
const klass = (o) => ({
  sessions: 1, users: 1, jobs: 1, asked: 1, rejected: 0, rejected_pct: 0, dry_runs: 0, steps: 0, queries: 1,
  queries_by: {}, queries_per_job: 1, polls: 1, polling_sessions: 0, polling_h: 0, cancels: 0, runs: 0,
  runs_failed: 0, flags_pct: {}, argv_req_pct: null, partitions: {}, gpus: 0, gpu_jobs: 0, gpu_h: null,
  gpu_h_known: 0, time_limit_s: null, peak_per_min: 1, abnormal_sessions: 0, abnormal_by: NONE_BROKEN, ...o,
});

const ADA = row({
  jobs: 1, queries: 7200, queries_peak_per_min: 24, polls: 3600,
  loop: { from: T, to: T + 18000, polls: 3600, span_s: 18000, every_s: 5, polling: true }, abnormal: ['polling'],
});
const GRACE = row({
  user: 'grace', agent_type: 'codex', session: 'login02:200', host: 'login02', jobs: 875, peak_per_min: 112,
  abnormal: ['submit_burst'],
});
const BOB = row({
  cls: 'human', user: 'bob', agent_type: null, session: null, rejected: 14,
  fails: [{ why: 'sbatch: error: QOSMaxSubmitJobPerUserLimit', n: 14 }], abnormal: ['refused'],
});
const ERIN = row({ cls: 'human', user: 'erin', agent_type: null, session: null, jobs: 40, queries: 30 });

const slurm = (o = {}) => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null,
  by_class: {
    agent: klass({ sessions: 2, abnormal_sessions: 2, abnormal_by: { ...NONE_BROKEN, polling: 1, submit_burst: 1 } }),
    human: klass({ sessions: 2, abnormal_sessions: 1, abnormal_by: { ...NONE_BROKEN, refused: 1 } }),
  },
  // the busiest two, then the abnormal ones past them
  sessions: [ERIN, ADA, GRACE, BOB], sessions_total: 10, sessions_busiest: 2,
  abnormal: { rules: RULES, sessions: 3, served: 3 },
  series: { bin_s: 600, t: [], by_class: {} }, rejections: [], basis: {},
  ...o,
});

const tab = async (P, cls = ALL) =>
  textOf(await ui.render('tabs/SlurmTab.tsx', 'SlurmTab', { P, live: null, cls }));
/** the Abnormal Slurm activity panel's text: from its title to the next panel's */
const abnormalOf = (text) => {
  const a = text.indexOf('Abnormal Slurm activity');
  const b = text.indexOf('Sessions at the scheduler');
  assert.ok(a >= 0 && b > a, 'the abnormal panel comes before the sessions');
  return text.slice(a, b);
};

const failed = (why = 'division by zero') => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null, _error: why,
});
const firstBuild = {
  minutes: 1440, label: '24h', state: 'building', error: null, has_payload: false,
  progress: { pct: 40, records: 1000, bytes: 0, total_bytes: 0, files: 2, elapsed_s: 3 },
  built_at: null, built_age_s: null, covers_from: null, covers_to: null, drift_s: null,
  drift_budget_s: 0, live_records: 0, replay_dropped: 0,
};

test('every rule is worded from the threshold it was sent with', () => {
  for (const r of RULES) {
    const w = S.ruleWords(r);
    assert.ok(w.lbl && w.when, r.id);
  }
  assert.deepEqual(S.ruleWords(RULES[0]), {
    lbl: 'polling the queue', when: 'checks at most 3.0 min apart, for 10 min or more',
  });
  assert.equal(S.ruleWords({ id: 'submit_burst', min: 50 }).when, '50 or more jobs in one minute; an array is one submission');
});

test('the rules with each class\'s count, then each session that broke one, with its own numbers', async () => {
  const text = abnormalOf(await tab({ ...FIX, slurm: slurm() }));
  assert.match(text, /3 sessions/);
  assert.match(text, /polling the queue · checks at most 3\.0 min apart, for 10 min or more 1 0/);
  assert.match(text, /refused again and again · 3 or more submissions refused 0 1/);
  assert.match(text, /polled the queue every 5\.0 s for 5\.0 h, 3,600 checks/);
  assert.match(text, /112 jobs submitted in one minute/);
  assert.match(text, /14 submissions refused · most: sbatch: error: QOSMaxSubmitJobPerUserLimit/);
  // a session that broke no rule is not listed here (it is in the sessions table)
  assert.doesNotMatch(text, /erin/);
});

test('the sessions table says what was sent: the busiest, then the others that broke a rule', async () => {
  const text = await tab({ ...FIX, slurm: slurm() });
  assert.match(text, /4 sessions · the service sends the busiest 2 of 10, and the 2 others that broke a rule/);
  // what the service sent is every class's, whatever the chips show
  const people = await tab({ ...FIX, slurm: slurm() }, ['human']);
  assert.match(people, /2 sessions · the service sends the busiest 2 of 10, and the 2 others that broke a rule/);
});

test('none only of a panel the service reduced and judged, and only of the classes shown', async () => {
  const none = abnormalOf(await tab({
    ...FIX, slurm: slurm({ sessions: [ERIN], abnormal: { rules: RULES, sessions: 0, served: 0 } }),
  }));
  assert.match(none, /none · no session broke a rule in the window/);
  const hidden = abnormalOf(await tab({ ...FIX, slurm: slurm() }, ['ide']));
  assert.match(hidden, /none · no session of the classes shown broke a rule/);
  // the counts it was sent are past what it shows
  const cut = abnormalOf(await tab({ ...FIX, slurm: slurm({ abnormal: { rules: RULES, sessions: 5, served: 3 } }) }));
  assert.match(cut, /5 sessions broke a rule; the service sends 3 of them/);
});

test('waited for, not reduced, still read or not judged: never none', async () => {
  const NONE = /no session broke a rule|no session of the classes/;
  const waiting = abnormalOf(await tab({}));
  assert.match(waiting, /waiting for the dashboard service/);
  assert.doesNotMatch(waiting, NONE);
  const unreduced = abnormalOf(await tab({ ...FIX, slurm: failed() }));
  assert.match(unreduced, /the service could not reduce this panel: division by zero/);
  assert.doesNotMatch(unreduced, NONE);
  const building = abnormalOf(await tab({ ...FIX, slurm: { ...failed(), _error: undefined, _window: firstBuild } }));
  assert.match(building, /reducing 24h/);
  assert.doesNotMatch(building, NONE);
  // a service older than the page sends the panel without the rules
  const older = slurm();
  delete older.abnormal;
  const old = abnormalOf(await tab({ ...FIX, slurm: older }));
  assert.match(old, /not returned/);
  assert.doesNotMatch(old, NONE);
});

test('a minute of queue checks past the service\'s cap is a floor', () => {
  const r = { ...ADA, queries_peak_per_min: 40, queries_unplaced: 12, abnormal: ['query_burst'] };
  assert.equal(S.evidence(r, 'query_burst'), 'at least 40 queue checks in one minute');
  assert.equal(S.evidence({ ...r, queries_unplaced: 0 }, 'query_burst'), '40 queue checks in one minute');
});
