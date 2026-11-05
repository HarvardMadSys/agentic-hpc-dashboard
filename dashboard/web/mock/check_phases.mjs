/* A self-check for `src/lib/phases.ts` and `src/lib/tasks.ts`, and for the glossary's entry on tasks, on the fixture.
 *
 *   npm run check:phases        (node --test mock/check_phases.mjs)
 *
 * The frontend has no unit-test runner, and this is not one: it is not part of
 * `npm run build` or `npm run typecheck`, and nothing under `src/` imports it.
 * It loads the real modules (node strips the types, with no flag from node 23.6
 * and 22.18; this host runs 24.18. The hook below lets the source's own
 * extensionless imports resolve) and holds the copy the plan fixes, word for
 * word, then every phase and every task of every row in `api.json`.
 *
 * Node does not load a component (it strips types, not JSX). Where a check has to see what a component
 * shows, `renderer()` below has Vite's own module loader turn the `.tsx` file into a module and
 * react-dom/server render it to markup, with the fixture's real data as its props. A function of
 * `src/lib/` is tested without that; the wiring of a component that is not rendered is read as text.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { register } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mock, test } from 'node:test';
import { fileURLToPath } from 'node:url';

// `import './format'` is how the source names its neighbours; node wants `./format.ts`.
register(
  'data:text/javascript,' +
    encodeURIComponent(`export async function resolve(s, c, next) {
      try { return await next(s, c); }
      catch (e) { if (e.code === 'ERR_MODULE_NOT_FOUND' && s.startsWith('.')) return next(s + '.ts', c); throw e; }
    }`),
  import.meta.url,
);

const P = await import('../src/lib/phases.ts');
const K = await import('../src/lib/clock.ts');
const F = await import('../src/lib/format.ts');
const G = await import('../src/lib/glossary.ts');
const T = await import('../src/lib/tasks.ts');
const api = JSON.parse(readFileSync(fileURLToPath(new URL('./api.json', import.meta.url)), 'utf8'));

const rows = Object.values(api.trajectories.rows);
const blocks = rows.filter((r) => P.isBlock(r.phases));

/** The page's source (`src/`), and the files of it that spell `phrase`, as paths under it: for a copy that has one place. */
const SRC = new URL('../src/', import.meta.url);
const spelledIn = (phrase) =>
  readdirSync(SRC, { recursive: true })
    .map((f) => f.replaceAll('\\', '/'))
    .filter((f) => /\.tsx?$/.test(f) && readFileSync(new URL(f, SRC), 'utf8').includes(phrase))
    .sort();
/** How many times a file of the page's source calls `name`. */
const callsOf = (file, name) => (readFileSync(new URL(file, SRC), 'utf8').match(new RegExp(`\\b${name}\\(`, 'g')) ?? []).length;
/** A file of the page's source, whole. */
const source = (file) => readFileSync(new URL(file, SRC), 'utf8');

/** The page's own components, rendered to markup: `render('tabs/TaskList.tsx', 'TaskList', props)`. The module loader is Vite's, with no
 *  config of the project's (nor its cache, which is a directory of the system's own), and react is the one in `node_modules`, which
 *  the loader takes from there too. Close it when done: `close()`. */
async function renderer() {
  const { createServer } = await import('vite');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const { createElement } = await import('react');
  const server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    appType: 'custom',
    logLevel: 'error',
    cacheDir: join(tmpdir(), 'rc-dashboard-check-phases'),
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
  });
  return {
    render: async (file, name, props) => renderToStaticMarkup(createElement((await server.ssrLoadModule(`/src/${file}`))[name], props)),
    close: () => server.close(),
  };
}
/** The text of some markup: its tags taken out, and the five entities React escapes put back. */
const textOf = (html) => html.replace(/<[^>]*>/g, '').replaceAll('&quot;', '"').replaceAll('&#x27;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');

/** A phase with every field a service sends, and the ones `o` names. */
const ph = (kind, t0, t1, o = {}) => ({
  kind, t0, t1, n: 1, mix: [], top: [], guess: null, slices: null, rounds: null,
  waits_on: null, every_s: null, times: null, every_cmd: null, open: false, seen: null, ran_past: null,
  alive: null, task: 0, sid: null, ...o,
});

/** A task with every field a service sends, and the ones `o` names. */
const task = (label, t0, t1, o = {}) => ({
  kind: 'task', label, source: null, ref: '', n: 1, t0, t1, names: [], phases: [0, 1],
  open: false, incomplete: false, gap: null, sid: null, ...o,
});

/** Whether two numbers are the same to a thousandth: a stretch's edge is a division, never an exact figure. */
const near = (a, b) => Math.abs(a - b) < 1e-6;

test('the table: labels, shorts, and colours, as the plan fixes them', () => {
  const want = {
    explore: ['Exploring', 'explore', 'var(--s3)'],
    edit: ['Editing', 'edit', 'var(--s5)'],
    edit_guess: ['Editing (guess)', 'edit?', 'var(--s5)'],
    build: ['Building', 'build', 'var(--s2)'],
    test: ['Testing', 'test', 'var(--s8)'],
    loop: ['Edit–test loop', 'edit–test loop', 'var(--s8)'],
    run: ['Running', 'run', 'var(--s6)'],
    slurm: ['Slurm jobs', 'Slurm', 'var(--s4)'],
    wait: ['Waiting', 'waiting', 'var(--s1)'],
    git: ['Git', 'git', 'var(--s7)'],
    idle: ['Idle', 'idle', 'none'],
    unknown: ['Commands not recorded', 'not recorded', 'var(--sg)'],
  };
  assert.deepEqual(P.PHASES.map(([k]) => k), Object.keys(want));
  for (const [k, label, short, colour] of P.PHASES) assert.deepEqual([label, short, colour], want[k]);
  assert.equal(P.phaseLbl('edit_guess'), 'Editing (guess)');
  assert.equal(P.phaseCol('loop'), P.phaseCol('test'));
  assert.equal(P.phaseCol('edit_guess'), P.phaseCol('edit'));
  assert.equal(P.phaseCol('idle'), 'none');
  assert.equal(P.phaseCol('somethingnew'), 'var(--sg)');
  assert.equal(P.phaseCol('constructor'), 'var(--sg)');
  assert.equal(P.phaseLbl('somethingnew'), 'somethingnew');
  assert.equal(P.phaseShort(ph('loop', 0, 1, { rounds: 4 })), 'edit–test loop ×4');
  assert.equal(P.phaseShort(ph('loop', 0, 1)), 'edit–test loop');
  assert.equal(P.phaseShort(ph('edit_guess', 0, 1)), 'edit?');
});

test('isBlock tells a block from {unavailable} and from nothing', () => {
  assert.equal(P.isBlock(undefined), false);
  assert.equal(P.isBlock(null), false);
  assert.equal(P.isBlock({ unavailable: 'phases not available for this agent type' }), false);
  assert.equal(P.isBlock(blocks[0].phases), true);
});

test('phaseWhat: the copy the plan fixes', () => {
  assert.equal(P.phaseWhat(ph('idle', 0, 400, { n: 0 })), "no tool calls: the user's turn, or the agent thinking");
  assert.equal(
    P.phaseWhat(ph('wait', 0, 4000, { n: 33, waits_on: 'slurm', every_s: 120, times: 33, top: [['squeue', 33]] })),
    'waiting on Slurm · squeue every 2 min, 33 times',
  );
  // R25: a loop reads its runs and its guessed pauses; "between them" is never said
  const loop = ph('loop', 0, 900, { n: 9, rounds: 6, top: [['pytest -q', 6]], slices: [[100, 130], [300, 330], [500, 530]], guess: 'a pause between two runs of pytest -q' });
  assert.equal(P.phaseWhat(loop), '6 runs of pytest -q · 3 pauses read as edits (guess)');
  assert.doesNotMatch(P.phaseWhat(loop), /between/);
  // the loop's command is the one that ran `rounds` times, not a git call that ran more often
  const noisy = { ...loop, rounds: 3, top: [['git status --short', 5], ['pytest -q', 3]] };
  assert.equal(P.phaseWhat(noisy), '3 runs of pytest -q · 3 pauses read as edits (guess)');
  assert.equal(P.phaseWhat({ ...noisy, rounds: 4 }), '4 runs of git status --short · 3 pauses read as edits (guess)');
  assert.equal(P.phaseWhat({ ...loop, slices: null, top: [] }), '6 runs');
  assert.equal(P.phaseWhat({ ...loop, slices: [[100, 130]] }), '6 runs of pytest -q · 1 pause read as an edit (guess)');
  // R25: a guess is keyed on its kind
  const guess = ph('edit_guess', 0, 60, { n: 0, guess: 'a pause after reading, before the test run' });
  assert.equal(P.phaseWhat(guess), 'a pause after reading, before the test run');
  const holder = ph('run', 0, 900, { n: 4, top: [['python train.py', 3]], slices: [[0, 60]], guess: 'a pause after reading, before the run' });
  assert.notEqual(P.phaseWhat(holder), holder.guess);
  assert.equal(P.phaseWhat(holder), '4 calls · python train.py ×3 · 1 pause read as an edit (guess): a pause after reading, before the run');
  // R26: a call that ran past, each as `<cmd> kept running for <dur>`
  assert.equal(
    P.phaseWhat(ph('run', 0, 900, { n: 2, top: [['python eval.py', 2]], ran_past: [['train.py', 9600]] })),
    '2 calls · python eval.py ×2 · train.py kept running for 2.7 h',
  );
  assert.match(P.phaseWhat(ph('run', 0, 900, { ran_past: [[null, 90]] })), /a call kept running for 1\.5 min$/);
  const many = ph('run', 0, 900, { ran_past: [['a', 100], ['b', 300], ['c', 200], ['d', 400], ['e', 50]] });
  assert.match(P.phaseWhat(many), /d kept running for 6\.7 min · b kept running[^·]*· c kept running[^·]*· 2 more kept running$/);
  // the rest of a phase's line, and what it never shows
  assert.equal(P.phaseWhat(ph('explore', 0, 1, { n: 19, mix: [['explore', 8], ['run', 6], ['build', 5]], top: [['ls', 5], ['cat x', 1]] })),
    '19 calls · exploring, with some running and building · ls ×5, cat x');
  // a name the tables do not hold is said as it came, and one off Object.prototype is not looked up
  assert.equal(P.phaseWhat(ph('explore', 0, 1, { n: 5, mix: [['explore', 2], ['constructor', 2], ['toString', 1]] })), '5 calls · exploring, with some constructor and toString');
  assert.equal(P.phaseWhat(ph('wait', 0, 1, { n: 1, waits_on: 'constructor' })), 'waiting on constructor · 1 call');
  assert.equal(P.phaseWhat(ph('unknown', 0, 1, { n: 3 })), '3 calls');
  assert.equal(P.phaseWhat(ph('git', 0, 1, { n: 1, top: [['git status\n  --short', 1]] })), '1 call · git status --short');
  assert.equal(P.phaseWhat(ph('wait', 0, 1, { n: 2, waits_on: 'sleep', top: [['sleep 600', 2]] })), 'waiting on a fixed sleep · 2 calls · sleep 600 ×2');
  assert.equal(P.phaseWhat(ph('wait', 0, 1, { n: 2, top: [['sleep 600', 2]] })), '2 calls · sleep 600 ×2');
  assert.equal(P.phaseWhat(ph('wait', 0, 9000, { waits_on: 'process', every_s: 30, times: 1, top: [['pgrep -f x', 1]] })), 'waiting on a process · pgrep -f x every 30 s, 1 time');
  // the polled command is the one that ran `times` times
  assert.equal(
    P.phaseWhat(ph('wait', 0, 4000, { n: 38, waits_on: 'slurm', every_s: 120, times: 33, top: [['sleep 30', 38], ['squeue', 33]] })),
    'waiting on Slurm · squeue every 2 min, 33 times',
  );
  assert.equal(P.phaseWhat(ph('wait', 0, 4000, { n: 33, waits_on: 'slurm', every_s: 90, times: 33, top: [] })), 'waiting on Slurm · a command every 1.5 min, 33 times');
  // R65: the service names the command a pace is set by, which a call still running adds to, though `top`
  // never counts that call
  assert.equal(
    P.phaseWhat(ph('wait', 0, 900, { n: 7, waits_on: 'slurm', every_s: 120, times: 7, every_cmd: 'squeue -u me', top: [['squeue -u me', 6]], open: true, seen: ['squeue', 'sleep'] })),
    'waiting on Slurm · squeue -u me every 2 min, 7 times · a call in it is still running, seen through squeue and sleep',
  );
  assert.equal(
    P.phaseWhat(ph('wait', 0, 900, { n: 6, waits_on: 'sleep', every_s: 120, times: 6, every_cmd: 'sleep', top: [], open: true, seen: ['sleep'] })),
    'waiting on a fixed sleep · sleep every 2 min, 6 times · a call in it is still running, seen through sleep',
  );
});

test('a command in a sentence is named short: assignments passed over, 40 characters on a word (R69)', () => {
  // A real pace on :8092 quoted the whole 220-character preview of a poll that sets T first
  const real =
    'T=/tmp/claude-61007/-n-home01-pkay/5e6f7a8b-d42b-4983-a679-2df21be73543/tasks; timeout 178 bash -c ' +
    '"until [ -s $T/bmqu7rasv.output ]; do sleep 20; done"; cat $T/bmqu7rasv.output; tail -n 3 /n/lab_storage/phi_lab/pkay/el…';
  assert.equal(real.length, 220);
  assert.equal(P.shortCmd(real), 'timeout 178 bash -c "until [ -s…');
  assert.equal(
    P.phaseWhat(ph('wait', 0, 1500, { n: 7, waits_on: 'output', every_s: 192, times: 7, every_cmd: real, top: [[real, 7]] })),
    'waiting on a file or its output · timeout 178 bash -c "until [ -s… every 3.2 min, 7 times',
  );
  // a loop's runs, the same way; the list's `top` keeps the whole preview
  const loop = ph('loop', 0, 900, { n: 6, rounds: 4, top: [['PYTHONPATH=src; pytest -q tests/test_io.py', 4]] });
  assert.equal(P.phaseWhat(loop), '4 runs of pytest -q tests/test_io.py');
  assert.deepEqual(loop.top, [['PYTHONPATH=src; pytest -q tests/test_io.py', 4]]);
  assert.equal(P.shortCmd('pytest -q tests/test_a_rather_long_name.py'), 'pytest -q…');
  assert.equal(P.shortCmd('A=1 B="x y"; C=2 python train.py --epochs 40 --lr 0.001 --batch 64'), 'python train.py --epochs 40 --lr 0.001…');
  // unchanged: a short command, and one that is only an assignment
  assert.equal(P.shortCmd('squeue -u $USER'), 'squeue -u $USER');
  assert.equal(P.shortCmd('pytest -q'), 'pytest -q');
  assert.equal(P.shortCmd('X=1'), 'X=1');
  assert.equal(P.shortCmd('a'.repeat(50)), `${'a'.repeat(40)}…`);
  assert.equal(
    P.phaseWhat(ph('wait', 0, 4000, { n: 33, waits_on: 'slurm', every_s: 120, times: 33, top: [['squeue -u $USER', 33]] })),
    'waiting on Slurm · squeue -u $USER every 2 min, 33 times',
  );
});

test('a sentence names a script, not a program alone, past a leading cd and a substitution (L2)', () => {
  // 1605025's watch-script waits on :8092 read `bash…`: the cut left the program alone
  const watch =
    'bash /tmp/claude-61006/-n-netscratch-alpha-lab-Lab-jdoe/9c8d7e6f-7cf6-4de2-b988-1e59ed74d95a/' +
    'scratchpad/watch_t2i.sh 2>&1 | grep --line-buffered -E "step|loss"';
  assert.equal(P.shortCmd(watch), 'bash …/watch_t2i.sh');
  assert.equal(
    P.phaseWhat(ph('wait', 0, 1800, { n: 26, waits_on: 'output', every_s: 1789, times: 1, every_cmd: watch, top: [[watch, 1]] })),
    'waiting on a file or its output · bash …/watch_t2i.sh every 30 min, 1 time',
  );
  assert.equal(P.shortCmd('python3 /n/netscratch/alpha_lab/Lab/jdoe/scripts/check_jobs_and_logs.py --all'),
    'python3 …/check_jobs_and_logs.py');
  // with no argument that is a path, as before; and a cut that keeps more than the program, as before
  assert.equal(P.shortCmd(`echo ${'x'.repeat(50)}`), 'echo…');
  assert.equal(P.shortCmd('bash -x /n/netscratch/alpha_lab/Everyone/otto/cbm-t2i/tools/watch.sh'), 'bash -x…');
  // a leading `cd DIR &&` or `cd DIR;` is passed over like an assignment
  assert.equal(P.shortCmd('cd /n/lab_storage/phi_lab/pkay/else/10-28_coffee8_exp10 && squeue -u me'), 'squeue -u me');
  assert.equal(P.shortCmd('cd "/n/a b/c"; squeue -u me'), 'squeue -u me');
  assert.equal(P.shortCmd('cd /x'), 'cd /x');
  assert.equal(P.shortCmd('cd /x || exit 1'), 'cd /x || exit 1');
  // an assignment holding a `$( )` with spaces is passed over whole, and so is the `&&` after it
  assert.equal(P.shortCmd('X=$(date +%s); squeue -u me'), 'squeue -u me');
  assert.equal(P.shortCmd('T=$(mktemp -d) && cd $T && pytest -q'), 'pytest -q');
  assert.equal(P.shortCmd('END=$((SECONDS+170)); squeue -u me'), 'squeue -u me');
  assert.equal(P.shortCmd('T=$(date +%H:%M) python x.py'), 'python x.py');
  // unchanged
  assert.equal(P.shortCmd('A="x y" B=2 pytest -q'), 'pytest -q');
  assert.equal(P.shortCmd('X=$(date)'), 'X=$(date)');
});

test('an open phase says a call in it is still running, and what was seen under it (R56, R65)', () => {
  // A call still running is never named by its first child's text: `top` holds the finished one, and the
  // clause names the programs seen under the open one, with the serial comma
  const open = ph('wait', 0, 900, { n: 2, waits_on: 'sleep', top: [['sleep 30', 1]], open: true, seen: ['sleep', 'ugrep'] });
  assert.equal(K.clockPinned(), false);
  assert.equal(P.openClause(), 'a call in it is still running');
  assert.equal(P.phaseOpen(open), 'a call in it is still running, seen through sleep and ugrep');
  assert.equal(P.phaseOpen({ ...open, seen: ['sleep'] }), 'a call in it is still running, seen through sleep');
  assert.equal(P.phaseOpen({ ...open, seen: ['pgrep', 'sleep', 'ugrep'] }), 'a call in it is still running, seen through pgrep, sleep, and ugrep');
  assert.equal(P.phaseOpen({ ...open, seen: null }), 'a call in it is still running');
  assert.equal(P.phaseOpen({ ...open, seen: [] }), 'a call in it is still running');
  assert.equal(P.phaseWhat(open), 'waiting on a fixed sleep · 2 calls · sleep 30 · a call in it is still running, seen through sleep and ugrep');
  assert.equal(P.phaseOpen({ ...open, open: false }), '');
  assert.doesNotMatch(P.phaseWhat({ ...open, open: false }), /still running/);
  // last on the line, after what ran past its phase
  const both = ph('run', 0, 900, { n: 2, top: [['python eval.py', 1]], ran_past: [['nohup x', 600]], open: true, seen: ['python'] });
  assert.match(P.phaseWhat(both), /^2 calls · python eval\.py · nohup x kept running for [^·]+ · a call in it is still running, seen through python$/);
  K.setClockPinned(true);
  try {
    assert.equal(P.openClause(), 'a call in it was still running when the record ended');
    assert.equal(P.phaseOpen(open), 'a call in it was still running when the record ended, seen through sleep and ugrep');
    assert.equal(P.phaseOpen({ ...open, seen: null }), 'a call in it was still running when the record ended');
    assert.equal(
      P.phaseWhat(open),
      'waiting on a fixed sleep · 2 calls · sleep 30 · a call in it was still running when the record ended, seen through sleep and ugrep',
    );
    assert.equal(P.phaseOpen({ ...open, open: false }), '');
  } finally {
    K.setClockPinned(false);
  }
});

test('the live edge: its name, and what stands behind it', () => {
  assert.equal(P.edgeLabel(undefined), '');
  assert.equal(P.edgeWhat(undefined), '');
  const run = { state: 'running', cmds: [['python', 2], ['srun', 1]], cat: 'slurm', at: 1789399985 };
  assert.equal(P.edgeLabel(run), 'running now · python, srun');
  assert.equal(P.edgeLabel({ state: 'running' }), 'running now');
  // R71: the collector names a process it could not read `?`
  assert.equal(P.edgeLabel({ state: 'running', cmds: [['?', 4], ['sleep', 3], ['srun', 3]] }), 'running now · unnamed, sleep, srun');
  assert.equal(P.edgeLabel({ state: 'running', cmds: [['?x', 1]] }), 'running now · ?x');
  assert.match(P.edgeWhat(run), /^from the per-minute census, at \d\d:\d\d$/);
  assert.equal(P.edgeWhat({ state: 'running' }), 'from the per-minute census');
  assert.equal(P.edgeLabel({ state: 'idle', at: 1 }), 'idle');
  assert.equal(P.edgeWhat({ state: 'idle', at: 1 }), 'idle · nothing new running since the session began (per-minute census)');
  assert.equal(K.clockPinned(), false);
  assert.equal(P.edgeLabel({ state: 'unknown' }), 'not known yet');
  assert.equal(P.edgeWhat({ state: 'unknown' }), '');
  K.setClockPinned(true);
  try {
    assert.equal(K.clockPinned(), true);
    assert.equal(P.edgeLabel({ state: 'unknown' }), 'not known');
    assert.equal(P.sequenceLine([ph('run', 0, 10), ph('git', 10, 100)], { state: 'unknown' }), 'run → git 1.5 min → not known');
    assert.equal(P.edgeLabel({ state: 'idle', at: 1 }), 'idle');
  } finally {
    K.setClockPinned(false);
  }
  assert.equal(P.sequenceLine([ph('run', 0, 10), ph('git', 10, 100)], { state: 'unknown' }), 'run → git 1.5 min → not known yet');
});

test('a phase on the chart: its hover, the phase and the slice under a time, a label that fits (R73, R75)', () => {
  const T = '\\d\\d:\\d\\d:\\d\\d';
  // the hover is the row band's own: a name, when, how long, and for an open phase R65's clause
  assert.match(P.phaseTip(ph('explore', 1789400000, 1789400720)), new RegExp(`^Exploring · ${T}–${T} · 12 min$`));
  const open = ph('wait', 1789400000, 1789400900, { open: true, seen: ['sleep', 'ugrep'] });
  assert.match(P.phaseTip(open), new RegExp(`^Waiting · ${T}–${T} · 15 min · a call in it is still running, seen through sleep and ugrep$`));
  assert.doesNotMatch(P.phaseTip({ ...open, open: false }), /still running/);
  // idle and a guess say what they are on a second line, as a hatch or a gap alone does not
  assert.match(P.phaseTip(ph('idle', 0, 400)), /^Idle · .+ · 6\.7 min\nno tool calls: the user's turn, or the agent thinking$/);
  assert.match(P.phaseTip(ph('edit_guess', 0, 100, { guess: 'a pause before the test run' })), /^Editing \(guess\) · .+ · 1\.7 min\na pause before the test run$/);
  // a slice is named a guess, with the reason its phase gives; none given, no second line
  const loop = ph('loop', 0, 1000, { rounds: 4, guess: 'a pause between two runs of pytest', slices: [[100, 130], [400, 440]] });
  assert.match(P.sliceTip(loop, 100, 130), /^Editing \(guess\) · .+ · 30 s\na pause between two runs of pytest$/);
  assert.match(P.sliceTip({ ...loop, guess: null }, 100, 130), /^Editing \(guess\) · [^\n]+ · 30 s$/);
  assert.deepEqual(P.sliceAt(loop, 110), [100, 130]);
  assert.deepEqual(P.sliceAt(loop, 400), [400, 440]);
  assert.equal(P.sliceAt(loop, 200), null);
  assert.equal(P.sliceAt(ph('run', 0, 10), 5), null);

  // the phase under a time: from its start up to its end, a gap holds none, and the last holds its end too
  const list = [ph('explore', 0, 100), ph('edit_guess', 100, 130), ph('run', 130, 400), ph('idle', 450, 900)];
  const at = (t) => P.phaseAt(list, t);
  assert.deepEqual([-1, 0, 99.9, 100, 129.9, 130, 399.9].map(at), [-1, 0, 0, 1, 1, 2, 2]);
  assert.deepEqual([400, 425, 449.9, 450, 900, 900.1].map(at), [-1, -1, -1, 3, 3, -1]);
  assert.equal(P.phaseAt([], 5), -1);
  assert.equal(P.phaseAt([ph('run', 10, 20)], 5), -1);

  // a label is left out where it does not fit, never cut
  assert.equal(P.labelFits('explore', 7 * P.LABEL_CHAR_W + P.LABEL_PAD), true);
  assert.equal(P.labelFits('explore', 7 * P.LABEL_CHAR_W + P.LABEL_PAD - 0.1), false);
  assert.equal(P.labelFits('', 100), false);
  assert.equal(P.labelFits('edit–test loop ×12', 200), true);
  assert.equal(P.labelFits('edit–test loop ×12', 100), false);
});

test('the legend of the chart colours: only the kinds it is given, in the colour table order, from the bands own helpers (item 10)', () => {
  const ALL = [-1e9, 1e9]; // an axis every phase falls on: the legend is the block's
  const names = (list) => P.phaseLegend(list, ALL).map((e) => e.name);
  const at = (...kinds) => kinds.map((k, i) => ph(k, i * 100, i * 100 + 100, k === 'loop' ? { rounds: 3 } : {}));
  const GUESS = 'hatched = a guess: no command shows it';
  assert.equal(P.GUESS_LEGEND, GUESS);

  // every kind: the colour table's order, idle last, a kind each, and a guess hatched at the editing colour's place
  const every = P.PHASES.map(([k]) => k);
  assert.deepEqual(names(at(...every)), [
    'Exploring', 'Editing', GUESS, 'Building', 'Testing, edit–test loop', 'Running', 'Slurm jobs', 'Waiting', 'Git',
    'Commands not recorded', 'idle',
  ]);
  // however the phases come, the legend does not: it is the table's order, not the session's
  assert.deepEqual(names(at('git', 'run', 'explore', 'idle', 'run')), ['Exploring', 'Running', 'Git', 'idle']);
  assert.deepEqual(names(at('unknown', 'idle')), ['Commands not recorded', 'idle']);

  // only what is there
  assert.deepEqual(names(at('explore', 'run')), ['Exploring', 'Running']);
  assert.deepEqual(names(at('loop')), ['Edit–test loop']);
  assert.deepEqual(names(at('test')), ['Testing']);
  assert.deepEqual(names(at('edit')), ['Editing']);
  assert.deepEqual(names([]), []);
  // two kinds of one colour are one swatch with both names, the second continuing the first
  assert.deepEqual(names(at('loop', 'test')), ['Testing, edit–test loop']);
  assert.equal(P.phaseCol('test'), P.phaseCol('loop'));
  assert.deepEqual(P.phaseLegend(at('test', 'loop', 'run'), ALL).map((e) => e.fill), [P.phaseCol('test'), P.phaseCol('run')]);

  // a guess is hatched, not tinted: its own swatch, and Editing is not listed for it. A guessed slice of any other
  // phase is the same hatch, so it is explained too, and a session with no guess has no hatch to explain
  assert.deepEqual(names(at('edit_guess')), [GUESS]);
  assert.deepEqual(names(at('edit', 'edit_guess')), ['Editing', GUESS]);
  const sliced = [ph('run', 0, 100, { slices: [[0, 20]], guess: 'a pause before the run' })];
  assert.deepEqual(names(sliced), [GUESS, 'Running']);
  assert.deepEqual(names([ph('loop', 0, 100, { slices: [] })]), ['Edit–test loop']);
  assert.deepEqual(names([ph('loop', 0, 100, { slices: null })]), ['Edit–test loop']);
  assert.deepEqual(P.phaseLegend(at('edit_guess'), ALL).map((e) => [e.look, e.fill]), [['guess', P.phaseCol('edit_guess')]]);

  // idle: an empty dashed swatch, no colour, named as its bands are named
  assert.deepEqual(P.phaseLegend(at('idle'), ALL), [{ look: 'idle', fill: 'none', name: 'idle' }]);
  assert.equal(P.phaseCol('idle'), 'none');
  assert.equal(P.phaseShort(at('idle')[0]), 'idle');

  // a kind this page does not know is drawn in the grey of `unknown`, so it is named with it, or alone
  assert.deepEqual(names(at('mystery')), ['mystery']);
  assert.deepEqual(names(at('unknown', 'mystery')), ['Commands not recorded, mystery']);
  assert.equal(P.phaseCol('mystery'), P.phaseCol('unknown'));

  // the tint of an entry is the band's: the colour `phaseCol` gives each kind in it
  for (const e of P.phaseLegend(at(...every), ALL)) {
    if (e.look === 'fill') assert.ok(P.PHASES.some(([k, , , c]) => c === e.fill && k !== 'idle'), e.name);
  }
});

test("the legend is the read's: the kinds whose bands fall on the read's axis, and its hatch only for a guess that does (R77)", () => {
  const GUESS = 'hatched = a guess: no command shows it';
  // :8092's 1983854 in little: the read ends before the only edit phase begins, and the block holds a kind with no band
  const list = [ph('run', 0, 100), ph('wait', 100, 400), ph('edit', 500, 600), ph('wait', 600, 800)];
  const names = (axis, l = list) => P.phaseLegend(l, axis).map((e) => e.name);
  assert.deepEqual(names([0, 450]), ['Running', 'Waiting']);              // no Editing swatch: no Editing band on this axis
  assert.deepEqual(names([0, 800]), ['Editing', 'Running', 'Waiting']);   // and on an axis that holds it, there is one
  assert.deepEqual(names([450, 650]), ['Editing', 'Waiting']);            // a phase across an edge of the axis is on it
  assert.deepEqual(names([0, 550]), ['Editing', 'Running', 'Waiting']);
  // a phase that only touches the axis is not on it: nothing of it is drawn
  assert.deepEqual(names([0, 500]), ['Running', 'Waiting']);              // the edit phase begins where the axis ends
  assert.deepEqual(names([600, 800]), ['Waiting']);                       // the edit phase ends where the axis begins
  assert.deepEqual(names([900, 1000]), []);                               // no phase on the axis, no legend
  assert.deepEqual(P.phaseLegend([], [0, 100]), []);
  // a phase of no length is on the axis where it sits
  const point = [ph('git', 300, 300)];
  assert.deepEqual(names([0, 500], point), ['Git']);
  assert.deepEqual(names([400, 500], point), []);
  assert.deepEqual(names([300, 500], point), ['Git']);
  // idle is listed where an idle band is on the axis
  const gap = [ph('run', 0, 100), ph('idle', 100, 400), ph('run', 400, 500)];
  assert.deepEqual(names([0, 90], gap), ['Running']);
  assert.deepEqual(names([0, 200], gap), ['Running', 'idle']);
  assert.deepEqual(names([410, 500], gap), ['Running']);

  // the hatch: a guessed phase on the axis, or a guessed slice that is
  const guess = [ph('run', 0, 100), ph('edit_guess', 100, 130), ph('run', 130, 400)];
  assert.deepEqual(names([0, 90], guess), ['Running']);                   // the guess is past the axis: no hatch to explain
  assert.deepEqual(names([0, 120], guess), [GUESS, 'Running']);
  const sliced = [ph('loop', 0, 1000, { rounds: 4, slices: [[800, 900]], guess: 'a pause between two runs of pytest' })];
  assert.deepEqual(names([0, 500], sliced), ['Edit–test loop']);          // the loop is on the axis, its slice is not
  assert.deepEqual(names([0, 850], sliced), [GUESS, 'Edit–test loop']);
  assert.deepEqual(names([850, 2000], sliced), [GUESS, 'Edit–test loop']);
  assert.deepEqual(names([900, 2000], sliced), ['Edit–test loop']);       // the slice ends where the axis begins
  // two kinds of one colour, one on the axis and one past it, are the one that is on it
  const pair = [ph('test', 0, 100), ph('loop', 200, 300)];
  assert.deepEqual(names([0, 150], pair), ['Testing']);
  assert.deepEqual(names([0, 250], pair), ['Testing, edit–test loop']);
  assert.deepEqual(names([150, 250], pair), ['Edit–test loop']);

  // every row of the fixture: the legend's tints are the colours of the bands on the axis it is given, for the whole
  // session and for each half of it
  for (const r of blocks) {
    const l = r.phases.list;
    if (l.length < 2) continue;
    const lo = l[0].t0 - 1;
    const hi = l[l.length - 1].t1 + 1;
    const mid = (lo + hi) / 2;
    for (const axis of [[lo, hi], [lo, mid], [mid, hi]]) {
      const on = l.filter((p) => P.onAxis(p.t0, p.t1, axis[0], axis[1]));
      const tints = new Set(on.filter((p) => p.kind !== 'idle' && p.kind !== 'edit_guess').map((p) => P.phaseCol(p.kind)));
      const legend = P.phaseLegend(l, axis);
      assert.deepEqual(new Set(legend.filter((e) => e.look === 'fill').map((e) => e.fill)), tints, `${r.sid} on ${axis}`);
      assert.equal(legend.some((e) => e.look === 'idle'), on.some((p) => p.kind === 'idle'), `${r.sid} idle on ${axis}`);
    }
    // what a half names, the whole names too: a smaller axis never adds a swatch
    const whole = new Set(P.phaseLegend(l, [lo, hi]).map((e) => `${e.look} ${e.fill}`));
    for (const axis of [[lo, mid], [mid, hi]]) {
      for (const e of P.phaseLegend(l, axis)) assert.ok(whole.has(`${e.look} ${e.fill}`), `${r.sid}: ${e.name} on ${axis}`);
    }
  }
});

test('the line that says where the phases outside the read are: after, before, both, and none (R78)', () => {
  const T0 = 1789400000;
  const list = [ph('run', T0, T0 + 100), ph('wait', T0 + 100, T0 + 400), ph('edit', T0 + 500, T0 + 600), ph('wait', T0 + 600, T0 + 800)];
  const note = (a, b, l = list) => P.phasesOutside(l, [T0 + a, T0 + b]);
  const at = (t) => F.clock(T0 + t); // the session caption's format
  assert.match(at(450), /^\d\d:\d\d:\d\d$/);

  // after: the read ends before the last phases begin, as :8092's 1983854 does
  assert.equal(note(0, 450), `phases after ${at(450)} are in the list below`);
  assert.equal(note(0, 500), `phases after ${at(500)} are in the list below`);   // one that begins where the axis ends is after it
  // before: the read begins after the first phases end
  assert.equal(note(450, 900), `phases before ${at(450)} are in the list below`);
  assert.equal(note(400, 900), `phases before ${at(400)} are in the list below`);
  assert.equal(note(100, 900), `phases before ${at(100)} are in the list below`); // one that ends where the axis begins is before it
  // both sides, in one line
  assert.equal(note(450, 550), `phases before ${at(450)} and after ${at(550)} are in the list below`);
  assert.equal(note(100, 400), `phases before ${at(100)} and after ${at(400)} are in the list below`);
  // none: every phase is on the axis, or only crosses its edges
  assert.equal(note(0, 800), '');
  assert.equal(note(-50, 900), '');
  assert.equal(note(50, 750), '');
  assert.equal(P.phasesOutside([], [T0, T0 + 100]), '');
  // a phase of no length is outside where it does not sit on the axis
  const point = [ph('git', T0 + 300, T0 + 300)];
  assert.equal(note(0, 500, point), '');
  assert.equal(note(400, 500, point), `phases before ${at(400)} are in the list below`);
  assert.equal(note(0, 200, point), `phases after ${at(200)} are in the list below`);
  // the copy, word for word, in sentence case, with no full stop
  assert.match(note(0, 450), /^phases after \d\d:\d\d:\d\d are in the list below$/);
  assert.match(note(450, 900), /^phases before \d\d:\d\d:\d\d are in the list below$/);
  assert.match(note(450, 550), /^phases before \d\d:\d\d:\d\d and after \d\d:\d\d:\d\d are in the list below$/);

  // the fixture's rows: a read of the whole session holds every phase; the first half of it leaves the rest after
  for (const r of blocks) {
    const l = r.phases.list;
    const lo = l[0].t0 - 1;
    const hi = l[l.length - 1].t1 + 1;
    assert.equal(P.phasesOutside(l, [lo, hi]), '', r.sid);
    if (l.length < 2) continue;
    const cut = l[0].t1; // the read ends where the first phase does
    assert.equal(P.phasesOutside(l, [lo, cut]), `phases after ${F.clock(cut)} are in the list below`, r.sid);
  }
});

test('the live edge on a view: cut at the view as a phase is, and out of view where the axis ends before it (R73)', () => {
  const T = '\\d\\d:\\d\\d:\\d\\d';
  const list = [ph('run', 0, 100), ph('git', 100, 200)];
  // the edge runs from the last phase's end to now
  assert.deepEqual(P.edgeSpan(list, 500, 0, 600), { from: 200, to: 500, a: 200, b: 500 });
  // cut at the view's end, and at its start
  assert.deepEqual(P.edgeSpan(list, 500, 0, 300), { from: 200, to: 500, a: 200, b: 300 });
  assert.deepEqual(P.edgeSpan(list, 500, 250, 450), { from: 200, to: 500, a: 250, b: 450 });
  // an axis that ends where the last phase does (the read ends at its newest record) has no room for it, nor has a
  // view before it or after it; a clock that has not passed the last phase's end gives an edge of no length
  assert.equal(P.edgeSpan(list, 500, 0, 200), null);
  assert.equal(P.edgeSpan(list, 500, 0, 150), null);
  assert.equal(P.edgeSpan(list, 500, 600, 700), null);
  assert.equal(P.edgeSpan(list, 150, 0, 600), null);
  assert.equal(P.edgeSpan([], 500, 0, 600), null);
  // its hover is its name, its span, and what stands behind it, as the row band says it
  const run = { state: 'running', cmds: [['python', 2], ['srun', 1]], cat: 'slurm', at: 1789399985 };
  assert.match(P.edgeTip(run, 1789398800, 1789400000), new RegExp(`^running now · python, srun · ${T}–${T} · 20 min\\nfrom the per-minute census, at \\d\\d:\\d\\d$`));
  assert.match(P.edgeTip({ state: 'idle', at: 1 }, 0, 2700), new RegExp(`^idle · ${T}–${T} · 45 min\\nidle · nothing new running since the session began \\(per-minute census\\)$`));
  assert.match(P.edgeTip({ state: 'unknown' }, 0, 3000), new RegExp(`^not known yet · ${T}–${T} · 50 min$`));
});

test('housekeepingText: commands under a process that never exited are apart from "not the model" (R35, R43)', () => {
  // `never_exited` is the block's own count of them, 0 when none: the shapes list holds only the top five
  const hk = (n, shapes, never_exited = 0) => ({ n, shapes, bins: [], cut: never_exited, never_exited });
  const never = 'under a process that never exited';
  assert.equal(
    P.housekeepingText(hk(226, [['git', 150], ['realpath', 52], [never, 12], ['uname', 5], ['ls', 3]], 12)),
    'housekeeping, not the model: 214 commands (git, realpath) · 12 more under a process that never exited',
  );
  assert.equal(P.housekeepingText(hk(5, [['git', 4], ['ls', 1]])), 'housekeeping, not the model: 5 commands (git, ls)');
  assert.equal(P.housekeepingText(hk(1, [['git', 1]])), 'housekeeping, not the model: 1 command (git)');
  assert.equal(P.housekeepingText(hk(12, [[never, 12]], 12)), 'housekeeping, not the model: 0 commands · 12 more under a process that never exited');
  assert.equal(P.housekeepingText(hk(0, [])), 'housekeeping, not the model: 0 commands');
  // R43: its shape is not among the top five, and the separation holds all the same
  assert.equal(
    P.housekeepingText(hk(226, [['git', 150], ['realpath', 40], ['uname', 15], ['ls', 12], ['other', 9]], 5)),
    'housekeeping, not the model: 221 commands (git, realpath) · 5 more under a process that never exited',
  );
  // and the count that is said is the field's, whatever the shapes list shows of it
  assert.equal(P.housekeepingText(hk(20, [['git', 15], [never, 5]], 5)), 'housekeeping, not the model: 15 commands (git) · 5 more under a process that never exited');
  assert.equal(P.housekeepingText(hk(20, [['git', 15], [never, 5]], 0)), 'housekeeping, not the model: 20 commands (git)');
});

test('sequenceLine: the plan\'s line, its quarter rule, its nine, and its live edge', () => {
  const at = (...spec) => {
    let t = 0;
    return spec.map(([kind, d, o]) => ph(kind, t, (t += d), o));
  };
  const list = at(['explore', 300], ['edit_guess', 60], ['loop', 1500, { rounds: 4 }], ['git', 100], ['run', 800], ['slurm', 100],
    ['wait', 4000, { waits_on: 'slurm' }], ['idle', 400], ['explore', 300]);
  assert.equal(
    P.sequenceLine(list),
    'explore → edit? → edit–test loop ×4 → git → run → Slurm → waiting 67 min → idle → explore',
  );
  // a quarter of the span exactly is long: 2,500 of 10,000
  assert.equal(P.sequenceLine(at(['run', 2500], ['git', 7500])), 'run 42 min → git 2.1 h');
  assert.equal(P.sequenceLine(at(['run', 2499], ['git', 7501])), 'run → git 2.1 h');
  // past nine, the first four, `…`, and the last four
  const ten = at(...['explore', 'edit', 'test', 'git', 'run', 'build', 'wait', 'slurm', 'explore', 'idle'].map((k) => [k, 100]));
  assert.equal(P.sequenceLine(ten), 'explore → edit → test → git → … → waiting → Slurm → explore → idle');
  assert.equal(P.sequenceLine(ten.slice(0, 9)).includes('…'), false);
  assert.equal(P.sequenceLine(ten, { state: 'running', cmds: [['python', 1]] }), 'explore → edit → test → git → … → waiting → Slurm → explore → idle → running now');
  assert.equal(P.sequenceLine(list.slice(0, 2), { state: 'idle', at: 1 }).endsWith(' → idle'), true);
  assert.equal(P.sequenceLine([], undefined), '');
  assert.equal(P.sequenceLine([], { state: 'running' }), 'running now');
  assert.equal(P.sequenceLine([ph('run', 5, 5)]), 'run');
});

test('phaseWhat: an idle gap names the call the agent moved past that was still running as it began', () => {
  const T0 = 1791623144;
  const said = "no tool calls: the user's turn, or the agent thinking";
  const idle = ph('idle', T0, T0 + 7680, { n: 0, alive: [['linktest', T0 - 600]] });
  assert.equal(P.phaseWhat(idle), `${said} · linktest still running since ${F.clock(T0 - 600)}`);
  assert.match(P.phaseWhat(idle), /^no tool calls: the user's turn, or the agent thinking · linktest still running since \d\d:\d\d:\d\d$/);
  // a call still running has no preview of its own, so there is no program to name
  assert.equal(P.phaseWhat({ ...idle, alive: [[null, T0 - 600]] }), `${said} · a call still running since ${F.clock(T0 - 600)}`);
  // up to three come, the earliest first, and the first is the one named; a command is said as a sentence says it (R69)
  assert.equal(P.phaseWhat({ ...idle, alive: [['sleep 600', T0 - 900], ['linktest', T0 - 600], [null, T0 - 300]] }), `${said} · sleep 600 still running since ${F.clock(T0 - 900)}`);
  assert.equal(
    P.phaseWhat({ ...idle, alive: [['python3 /n/netscratch/alpha_lab/Lab/jdoe/scripts/check_jobs_and_logs.py --all', T0]] }),
    `${said} · python3 …/check_jobs_and_logs.py still running since ${F.clock(T0)}`,
  );
  assert.equal(P.phaseWhat({ ...idle, alive: [['  ', T0]] }), `${said} · a call still running since ${F.clock(T0)}`);
  // nothing running as it began, nothing said: the service sends none, and a block of the first version has no field
  assert.equal(P.phaseWhat({ ...idle, alive: null }), said);
  assert.equal(P.phaseWhat({ ...idle, alive: [] }), said);
  assert.equal(P.phaseWhat({ ...idle, alive: undefined }), said);
  // the hover on the gap says it too, on its second line
  assert.equal(P.phaseTip(idle).split('\n')[1], P.phaseWhat(idle));
  // only an idle gap names one
  assert.equal(P.phaseWhat(ph('run', 0, 900, { n: 2, top: [['python eval.py', 2]], alive: [['linktest', 5]] })), '2 calls · python eval.py ×2');
});

test('task copy: labels, sources, the gap and lone lines, and the row line', () => {
  const q = { kind: 'task', label: 'Fix the loader', source: 'commit', ref: '', n: 3, t0: 0, t1: 60, names: [], phases: [0, 1], open: false, incomplete: false, gap: null, sid: null };
  assert.equal(T.taskLabel(q), '“Fix the loader”');
  assert.equal(T.taskSource(q), 'commit');
  assert.equal(T.taskSource({ ...q, source: 'pr', ref: '#235' }), 'PR #235');
  assert.equal(T.taskLabel({ ...q, source: null, label: 'Run b.py' }), 'Run b.py');
  assert.equal(T.gapLine({ s: 7680, alive: [] }), '2.1 h with no new calls');
  assert.match(T.gapLine({ s: 7680, alive: [['linktest', 1791623144, null]] }), /^2\.1 h with no new calls · linktest still running since \d\d:\d\d:\d\d$/);
  assert.match(T.loneLine({ ...q, kind: 'lone', source: null, label: 'Read harness/x.py', n: 1 }), /^1 call at \d\d:\d\d:\d\d · read harness\/x\.py$/);
  const b = { tasks: ['A', 'B', 'C', 'D', 'E'].map((l) => ({ ...q, source: null, label: l })) };
  assert.equal(T.taskSequence(b), 'A → B → 3 more');
});

test('a quoted label shows its source and quotation marks, and a built one shows neither', () => {
  const q = task('Seek-based frame decoding', 0, 60);
  for (const [source, ref, shown] of [['commit', '', 'commit'], ['log', '', 'log'], ['pr', '#235', 'PR #235'], ['pr', '', 'PR']]) {
    const t = { ...q, source, ref };
    assert.equal(T.taskSource(t), shown, `${source} ${ref}`);
    assert.equal(T.taskLabel(t), '“Seek-based frame decoding”', `${source} ${ref}`);
  }
  assert.equal(T.taskSource(q), null);
  assert.equal(T.taskLabel(q), 'Seek-based frame decoding');
  // the agent's words are shown whole and as they were written; a lone stretch is labelled by the same rules
  assert.equal(T.taskLabel({ ...q, source: 'log', label: 'Fold-back cutoff set at 2 per 1,000 (user)' }), '“Fold-back cutoff set at 2 per 1,000 (user)”');
  assert.equal(T.taskLabel({ ...q, kind: 'lone', source: 'commit' }), '“Seek-based frame decoding”');
  assert.equal(T.taskLabel({ ...q, kind: 'lone' }), 'Seek-based frame decoding');
  // a source this page does not know is still the agent's words, so it is a quote, named as it came
  assert.equal(T.taskSource({ ...q, source: 'issue' }), 'issue');
  assert.equal(T.taskLabel({ ...q, source: 'issue' }), '“Seek-based frame decoding”');
});

test('the gap line says how long with no new calls, and names the first call the agent moved past that was running', () => {
  const T0 = 1791623144;
  const at = F.clock(T0);
  assert.equal(T.gapLine({ s: 7680, alive: [] }), '2.1 h with no new calls');
  assert.equal(T.gapLine({ s: 5400, alive: [] }), '1.5 h with no new calls');
  // The name the service gives the call comes first. The hung call of rtx6000a on 2026-11-01 made a folder and changed into
  // it before it ran anything, so its command, said as a sentence says one, was `mkdir -p…`: the name is the script it ran.
  const hung = 'mkdir -p /tmp/claude-1004/-madsys-x/1a2b3c4d/scratchpad/linktest && cd /tmp/claude-1004/-madsys-x/1a2b3c4d/scratchpad/linktest && timeout 600 bash t.sh';
  assert.equal(T.gapLine({ s: 7680, alive: [[hung, T0, 't.sh']] }), `2.1 h with no new calls · t.sh still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [[hung, T0, null]] }), `2.1 h with no new calls · ${P.shortCmd(hung)} still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [['linktest', T0, null]] }), `2.1 h with no new calls · linktest still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [['linktest', T0, '  ']] }), `2.1 h with no new calls · linktest still running since ${at}`);
  // a call still running has no preview of its own: it is named by the programs seen under it, and else is `a call`
  assert.equal(T.gapLine({ s: 7680, alive: [[null, T0, 'pgrep and sleep']] }), `2.1 h with no new calls · pgrep and sleep still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [[null, T0, null]] }), `2.1 h with no new calls · a call still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [['  ', T0, null]] }), `2.1 h with no new calls · a call still running since ${at}`);
  // up to three come, the earliest first, and the first is the one named
  assert.equal(T.gapLine({ s: 7680, alive: [[null, T0, null], ['linktest', T0 + 60, 'linktest'], ['sleep 600', T0 + 90, null]] }), `2.1 h with no new calls · a call still running since ${at}`);
  assert.equal(T.gapLine({ s: 7680, alive: [['linktest', T0, null], [null, T0 + 60, 'sleep']] }), `2.1 h with no new calls · linktest still running since ${at}`);
  // a command with no name is said as a sentence says it (R69)
  assert.equal(
    T.gapLine({ s: 5400, alive: [['python3 /n/netscratch/alpha_lab/Lab/jdoe/scripts/check_jobs_and_logs.py --all', T0, null]] }),
    `1.5 h with no new calls · python3 …/check_jobs_and_logs.py still running since ${at}`,
  );
  // the approved wording, with no end time: the call may have ended inside the gap, and the line says when it began
  assert.doesNotMatch(T.gapLine({ s: 7680, alive: [['linktest', T0, 'linktest']] }), /until|ended|–/);
  // the page's own words for how long
  assert.equal(T.gapLine({ s: 7680, alive: [] }), `${F.dur(7680)} with no new calls`);
  // the fixture's gaps name their calls by the names the service gives them
  const named = blocks.flatMap((r) => r.phases.tasks ?? []).filter((t) => t.gap?.alive.length);
  assert.ok(named.some((t) => t.gap.alive[0][0] === null) && named.some((t) => t.gap.alive[0][0] !== null), 'a call with a preview, and one still running');
  for (const t of named) {
    assert.equal(t.gap.alive[0].length, 3);
    assert.match(T.gapLine(t.gap), new RegExp(` · ${t.gap.alive[0][2]} still running since \\d\\d:\\d\\d:\\d\\d$`));
  }
});

test('a lone line says how many calls, when, and what for: a built label in lower case, a quote as it was written', () => {
  const T0 = 1791623144;
  const at = F.clock(T0);
  const lone = task('Read harness/x.py', T0, T0 + 2, { kind: 'lone', n: 1 });
  assert.equal(T.loneLine(lone), `1 call at ${at} · read harness/x.py`);
  assert.equal(T.loneLine({ ...lone, n: 3 }), `3 calls at ${at} · read harness/x.py`);
  assert.equal(T.loneLine({ ...lone, n: 8, label: 'Commands not recorded' }), `8 calls at ${at} · commands not recorded`);
  // a quote is the agent's own text: its capital stays, inside its quotation marks
  assert.equal(T.loneLine({ ...lone, n: 2, source: 'commit', label: 'Fix the loader' }), `2 calls at ${at} · “Fix the loader”`);
  assert.equal(P.lowerFirst('Read x'), 'read x');
  assert.equal(P.lowerFirst(''), '');
  assert.equal(P.lowerFirst('“Fix”'), '“Fix”');
});

test('the row line: the labels of the first tasks, quoted ones in quotation marks and with no source, and the rest as a count', () => {
  const b = (...tasks) => ({ tasks });
  const A = (l, o = {}) => task(l, 0, 1, o);
  assert.equal(T.taskSequence(b(A('A'))), 'A');
  assert.equal(T.taskSequence(b(A('A'), A('B'))), 'A → B');
  assert.equal(T.taskSequence(b(A('A'), A('B'), A('C'))), 'A → B → 1 more');
  assert.equal(T.taskSequence(b(...'ABCDEFGH'.split('').map((l) => A(l)))), 'A → B → 6 more');
  // a quoted task is in quotation marks, and its source is not on the line
  assert.equal(T.taskSequence(b(A('Fix it', { source: 'commit' }), A('B'), A('C'), A('D', { source: 'pr', ref: '#235' }))), '“Fix it” → B → 2 more');
  // a lone stretch is no task: it is not named and not counted
  assert.equal(T.taskSequence(b(A('A'), A('x', { kind: 'lone' }), A('B'), A('y', { kind: 'lone' }), A('C'))), 'A → B → 1 more');
  assert.equal(T.taskSequence(b(A('x', { kind: 'lone' }), A('A'))), 'A');
  assert.equal(T.taskSequence(b(A('x', { kind: 'lone' }))), '');
  // `keep` says how many are named
  assert.equal(T.taskSequence(b(...'ABCDE'.split('').map((l) => A(l))), 3), 'A → B → C → 2 more');
  assert.equal(T.taskSequence(b(A('A'), A('B')), 1), 'A → 1 more');
  assert.equal(T.taskSequence(b(A('A'), A('B')), 2), 'A → B');
  // a block with no tasks, or of the first version, has no line
  assert.equal(T.taskSequence({ tasks: [] }), '');
  assert.equal(T.taskSequence({}), '');
});

test("the chart's accessible name counts the tasks above the phases, and no lone stretch, and says nothing where there are none", () => {
  const A = (l, o = {}) => task(l, 0, 1, o);
  assert.equal(T.laneClause([A('a'), A('b')]), ', with 2 tasks above them');
  assert.equal(T.laneClause([A('a')]), ', with 1 task above them');
  // a lone stretch is no task, and is not counted, as the row line does not name it
  assert.equal(T.laneClause([A('a'), A('x', { kind: 'lone' }), A('b')]), ', with 2 tasks above them');
  // a block with no tasks, or of the first version, or with only a lone stretch, says nothing of a lane
  assert.equal(T.laneClause([]), '');
  assert.equal(T.laneClause(undefined), '');
  assert.equal(T.laneClause([A('x', { kind: 'lone' })]), '');
  // the page's own count: a thousand has its separator
  assert.equal(T.laneClause(Array.from({ length: 1234 }, (_, i) => A(String(i)))), `, with ${F.fint(1234)} tasks above them`);
  // it reads in the name: the times, then the lane, then the phases in order
  const name = `phases and process starts per time bin from ${F.clock(0)} to ${F.clock(60)}${T.laneClause([A('a'), A('b')])}: explore → run`;
  assert.match(name, /^phases and process starts per time bin from \d\d:\d\d:\d\d to \d\d:\d\d:\d\d, with 2 tasks above them: explore → run$/);
  // every block of the fixture: as many tasks as the row line counts, and a block's lone stretches are in neither
  for (const r of blocks) {
    const n = r.phases.tasks.filter((t) => t.kind === 'task').length;
    assert.equal(T.laneClause(r.phases.tasks), `, with ${n} ${n === 1 ? 'task' : 'tasks'} above them`, r.sid);
  }
});

test('taskWhat: the phases a task opens into, and its calls, singular for one', () => {
  const T0 = 1789400000;
  // A holds an explore, an idle gap inside it, and a run, then the idle phase of the cutting gap before B. The service gives
  // that gap to the task it follows, and the page says it once, as B's gap line. An idle phase inside a task is its own.
  const list = [
    ph('explore', T0, T0 + 100, { n: 9, task: 0 }),
    ph('idle', T0 + 100, T0 + 700, { n: 0, task: 0 }),
    ph('run', T0 + 700, T0 + 800, { n: 3, task: 0 }),
    ph('idle', T0 + 800, T0 + 9000, { n: 0, task: 0, alive: [['linktest', T0 + 5]] }),
    ph('git', T0 + 9000, T0 + 9100, { n: 1, task: 1 }),
  ];
  const A = task('A', T0, T0 + 800, { n: 12, phases: [0, 4] });
  const B = task('B', T0 + 9000, T0 + 9100, { n: 1, phases: [4, 5], gap: { s: 8200, alive: [['linktest', T0 + 5]] } });
  const b = { list, tasks: [A, B] };
  assert.equal(T.taskWhat(A, b), '3 phases · 12 calls');
  assert.equal(T.taskWhat(B, b), '1 phase · 1 call');
  assert.deepEqual(T.taskPhases(A, b).map((p) => p.kind), ['explore', 'idle', 'run']);
  assert.deepEqual(T.taskPhases(B, b).map((p) => p.kind), ['git']);
  // the task after it is the one that begins where its range ends, whichever object it is: a copy of a task, and a block
  // of copies, hold the same, and the gap's idle phase is not listed again beside its gap line
  assert.equal(T.taskWhat({ ...A }, b), '3 phases · 12 calls');
  assert.deepEqual(T.taskPhases({ ...A }, b).map((p) => p.kind), ['explore', 'idle', 'run']);
  const copies = { ...b, tasks: b.tasks.map((t) => ({ ...t })) };
  assert.equal(T.taskWhat(copies.tasks[0], copies), '3 phases · 12 calls');
  assert.equal(T.taskWhat(A, copies), '3 phases · 12 calls');
  assert.equal(T.taskWhat({ ...B }, b), '1 phase · 1 call');
  // the last task keeps its idle tail: nothing follows it to say that gap
  const tail = { list: [ph('run', 0, 100, { n: 2 }), ph('idle', 100, 5000, { n: 0, alive: [[null, 50]] })], tasks: [task('A', 0, 100, { n: 2, phases: [0, 2] })] };
  assert.equal(T.taskWhat(tail.tasks[0], tail), '2 phases · 2 calls');
  // a gap that is not a cutting one is no gap line, so its idle phase stays in its task
  const short = { list: [ph('run', 0, 100, { n: 2 }), ph('idle', 100, 900, { n: 0 }), ph('run', 900, 1000, { n: 3 })], tasks: [task('A', 0, 900, { n: 2, phases: [0, 2] }), task('B', 900, 1000, { n: 3, phases: [2, 3] })] };
  assert.equal(T.taskWhat(short.tasks[0], short), '2 phases · 2 calls');
  // the page's own count: a thousand has its separator
  assert.equal(T.taskWhat(task('A', 0, 1, { n: 1234, phases: [0, 1] }), { list: [ph('run', 0, 1)], tasks: [] }), `1 phase · ${F.fint(1234)} calls`);
  // a task of no phases says so
  assert.equal(T.taskWhat(task('A', 0, 1, { n: 4, phases: [3, 3] }), { list: [] }), '0 phases · 4 calls');
});

test("a task block's hover: its label and source, and what it holds; a lone stretch says its line; then a call still running, and a cut log", () => {
  const T0 = 1789400000;
  const list = [
    ph('explore', T0, T0 + 100, { n: 9, task: 0 }),
    ph('run', T0 + 100, T0 + 200, { n: 3, task: 0 }),
    ph('idle', T0 + 200, T0 + 9000, { n: 0, task: 0 }),
    ph('git', T0 + 9000, T0 + 9100, { n: 1, task: 1 }),
  ];
  const A = task('Fix the loader', T0, T0 + 200, { source: 'commit', n: 12, phases: [0, 3] });
  const B = task('Commit it', T0 + 9000, T0 + 9100, { n: 1, phases: [3, 4], gap: { s: 8800, alive: [] } });
  const b = { list, tasks: [A, B] };
  // a quote is in its quotation marks, with where the words came from; then what it holds, as the list says it, so the
  // idle phase of the cutting gap after it is not counted
  assert.equal(T.taskTip(A, b), '“Fix the loader” · commit\n2 phases · 12 calls');
  assert.equal(T.taskTip({ ...A, source: 'log' }, b), '“Fix the loader” · log\n2 phases · 12 calls');
  assert.equal(T.taskTip({ ...A, source: 'pr', ref: '#235' }, b), '“Fix the loader” · PR #235\n2 phases · 12 calls');
  assert.equal(T.taskTip({ ...A, source: 'pr', ref: '' }, b), '“Fix the loader” · PR\n2 phases · 12 calls');
  assert.equal(T.taskTip({ ...A, source: 'issue' }, b), '“Fix the loader” · issue\n2 phases · 12 calls');
  // a built label is no quote: no quotation marks and no source
  assert.equal(T.taskTip({ ...A, source: null, label: 'Run b.py · out/x' }, b), 'Run b.py · out/x\n2 phases · 12 calls');
  assert.equal(T.taskTip(B, b), 'Commit it\n1 phase · 1 call');
  // a lone stretch says its line, whatever its label is
  const lone = task('Read harness/x.py', T0 + 9000, T0 + 9100, { kind: 'lone', n: 1, phases: [3, 4] });
  assert.equal(T.taskTip(lone, b), `1 call at ${F.clock(T0 + 9000)} · read harness/x.py`);
  const quote = { ...lone, n: 3, source: 'commit', label: 'Fix it' };
  assert.equal(T.taskTip(quote, b), T.loneLine(quote));
  assert.equal(T.taskTip(quote, b), `3 calls at ${F.clock(T0 + 9000)} · “Fix it”`);

  // Last, on a line each: that a call in it is still running, in the words an open phase's line has, and that the call log
  // was full, in the words of the note under the list of phases, from when the block says it was cut.
  const running = P.phaseOpen(ph('run', 0, 1, { open: true }));
  assert.equal(running, 'a call in it is still running');
  const cut = { ...b, cut: 120, cut_at: T0 + 150 };
  const full = `incomplete from ${F.clock(T0 + 150)}: the call log was full`;
  const head = '“Fix the loader” · commit\n2 phases · 12 calls';
  assert.equal(T.taskTip({ ...A, open: true }, b), `${head}\n${running}`);
  assert.equal(T.taskTip({ ...A, incomplete: true }, cut), `${head}\n${full}`);
  assert.equal(T.taskTip({ ...A, source: null, label: 'Run b.py', open: true }, b), `Run b.py\n2 phases · 12 calls\n${running}`);
  // both: still running first, then the cut
  assert.equal(T.taskTip({ ...A, open: true, incomplete: true }, cut), `${head}\n${running}\n${full}`);
  // a block with no time for its cut says so without one
  assert.equal(T.taskTip({ ...A, incomplete: true }, { ...b, cut: 3, cut_at: null }), `${head}\nincomplete: the call log was full`);
  // a lone stretch says them after its line
  assert.equal(T.taskTip({ ...lone, open: true }, b), `${T.loneLine(lone)}\n${running}`);
  assert.equal(T.taskTip({ ...lone, open: true, incomplete: true }, cut), `${T.loneLine(lone)}\n${running}\n${full}`);
  // a task that is neither says neither, though the block was cut
  assert.equal(T.taskTip(A, cut), head);
  assert.equal(T.taskTip(lone, cut), T.loneLine(lone));
  // on a pinned clock, which will not grow, a call in it was still running when the record ended: the open phase's own words
  K.setClockPinned(true);
  try {
    const ended = P.phaseOpen(ph('run', 0, 1, { open: true }));
    assert.equal(ended, 'a call in it was still running when the record ended');
    assert.equal(T.taskTip({ ...A, open: true }, b), `${head}\n${ended}`);
    assert.equal(T.taskTip({ ...A, open: true, incomplete: true }, cut), `${head}\n${ended}\n${full}`);
    assert.equal(T.taskTip({ ...lone, open: true }, b), `${T.loneLine(lone)}\n${ended}`);
  } finally {
    K.setClockPinned(false);
  }

  // every task of every row has one, and it reads: the label and what it holds, or the lone line, and then what the task
  // says of a call still running in it and of the cut, which the fixture has on some task of some row each
  let nOpen = 0;
  let nIncomplete = 0;
  for (const r of blocks) {
    const bl = r.phases;
    for (const t of bl.tasks) {
      const where = `${r.sid} ${t.label}`;
      const tip = T.taskTip(t, bl);
      assert.doesNotMatch(tip, /undefined|null|NaN|\[object/, where);
      const want = t.kind === 'lone' ? [T.loneLine(t)] : [T.taskLabel(t) + (t.source ? ` · ${T.taskSource(t)}` : ''), T.taskWhat(t, bl)];
      if (t.open) want.push(running);
      if (t.incomplete) want.push(P.incompleteFrom(bl.cut_at));
      assert.deepEqual(tip.split('\n'), want, where);
      nOpen += t.open ? 1 : 0;
      nIncomplete += t.incomplete ? 1 : 0;
    }
  }
  assert.ok(nOpen >= 1 && nIncomplete >= 1, `the fixture has an open task and an incomplete one: ${nOpen}, ${nIncomplete}`);
});

test('a cut call log is incomplete from when its first left-out call began, and without a time where the block has none', () => {
  const at = 1789396203.6;
  assert.equal(P.incompleteFrom(at), `incomplete from ${F.clock(at)}: the call log was full`);
  assert.match(P.incompleteFrom(at), /^incomplete from \d\d:\d\d:\d\d: the call log was full$/);
  assert.equal(P.incompleteFrom(null), 'incomplete: the call log was full');
  assert.equal(P.incompleteFrom(undefined), 'incomplete: the call log was full');
  // the start of the epoch is a time, as a missing one is not
  assert.equal(P.incompleteFrom(0), `incomplete from ${F.clock(0)}: the call log was full`);
  // every cut block of the fixture has a time for it
  for (const r of blocks.filter((x) => x.phases.cut > 0)) assert.equal(typeof r.phases.cut_at, 'number', r.sid);
});

test('the note under the phase list for a cut call log: with and without a time for the cut, and for one call and many', () => {
  const at = 1789396203.6;
  const from = F.clock(at);
  // many calls, from a time: the calls the log left out began later than it
  assert.equal(P.cutNote({ cut: 120, cut_at: at }), `phases are incomplete from ${from}: the call log was full, so 120 later calls are counted but in no phase`);
  // one call
  assert.equal(P.cutNote({ cut: 1, cut_at: at }), `phases are incomplete from ${from}: the call log was full, so 1 later call is counted but in no phase`);
  // with no time for the cut, they are not said to be later, and the sentence has no `from`
  assert.equal(P.cutNote({ cut: 120, cut_at: null }), 'phases are incomplete: the call log was full, so 120 calls are counted but in no phase');
  assert.equal(P.cutNote({ cut: 1, cut_at: null }), 'phases are incomplete: the call log was full, so 1 call is counted but in no phase');
  // the page's own count: a thousand has its separator
  assert.equal(P.cutNote({ cut: 1234, cut_at: at }), `phases are incomplete from ${from}: the call log was full, so ${F.fint(1234)} later calls are counted but in no phase`);
  // the start of the epoch is a time, as a missing one is not
  assert.match(P.cutNote({ cut: 2, cut_at: 0 }), /^phases are incomplete from \d\d:\d\d:\d\d: the call log was full, so 2 later calls are counted/);
  // nothing cut, nothing said
  assert.equal(P.cutNote({ cut: 0, cut_at: null }), null);
  assert.equal(P.cutNote({ cut: 0, cut_at: at }), null);
  // a block of the fixture: said where its log was cut, in the words a task's tooltip uses for it, and nowhere else
  for (const r of blocks) {
    const b = r.phases;
    const note = P.cutNote(b);
    if (b.cut > 0) assert.ok(note.startsWith(`phases are ${P.incompleteFrom(b.cut_at)}, so `), `${r.sid}: ${note}`);
    else assert.equal(note, null, r.sid);
  }
  assert.ok(blocks.some((r) => r.phases.cut > 0) && blocks.some((r) => !(r.phases.cut > 0)), 'a block that was cut, and one that was not');
  // the sentence's words are spelled in phases.ts and nowhere else in the page's code (a task's tooltip and the phase list both
  // take them from there), and the phase list calls the function, as it builds nothing of its own
  assert.deepEqual(spelledIn(': the call log was full`'), ['lib/phases.ts']);
  assert.equal(callsOf('tabs/PhaseList.tsx', 'cutNote'), 1);
});

test('the basis line: tasks, then what they and the phases under them are read from, spelled in one place', () => {
  assert.equal(T.TASKS_BASIS, 'tasks · read from the commands the agent ran and the words it wrote in them');
  // it is the service's own basis under the name of the level it opens with, so the two cannot drift
  for (const r of blocks) assert.equal(T.TASKS_BASIS, `tasks · ${r.phases.basis}`, r.sid);
  // the chart says it by this constant, and no other file of the page spells the basis, so the phases-only line is gone
  assert.deepEqual(spelledIn('read from the commands the agent ran'), ['lib/tasks.ts']);
  assert.match(readFileSync(new URL('tabs/TrajectoryNav.tsx', SRC), 'utf8'), /\bTASKS_BASIS\b/);
});

test('what a click on a phase or a task does is said by one function, which the chart and the phase list call', () => {
  const T0 = 1789400000;
  assert.equal(P.zoomTip(T0), `click: zoom to it, and open the tree at the first process started at or after ${F.clock(T0)}`);
  assert.match(P.zoomTip(T0), /^click: zoom to it, and open the tree at the first process started at or after \d\d:\d\d:\d\d$/);
  // no file but phases.ts spells it; the chart's label row and its task lane call it, and so does each row of the phase list
  assert.deepEqual(spelledIn('click: zoom to it'), ['lib/phases.ts']);
  assert.equal(callsOf('tabs/TrajectoryNav.tsx', 'zoomTip'), 2, 'the label row and the lane');
  assert.equal(callsOf('tabs/PhaseList.tsx', 'zoomTip'), 1, 'the rows of the list');
});

test("the glossary explains tasks: where one starts, what names it, that a quote is the agent's own words, and what a lone stretch is", () => {
  const g = G.GLOSS.tasks;
  assert.equal(typeof g, 'string');
  assert.match(g, /^tasks: /, 'it opens with its term, as the other entries do');
  // Where a task can start: after a gap with nothing waited on, when the work moves on, or after a commit. It can, not must: a commit
  // starts one only when the work around it changes, and a series of commits once. The page is not sent `gap_s`, so the copy names its
  // default, as a gap of an hour or more.
  assert.match(g, /A task can start after an hour or more in which the agent waited on nothing/, 'where a task can start: after a gap');
  assert.doesNotMatch(g, /A task starts/, 'never that it always starts there');
  assert.match(g, /when its work moves to something new/, 'where a task starts: new work');
  assert.match(g, /or after a commit or pull request\./, 'where a task starts: after a commit');
  assert.match(g, /A quoted label is the agent’s own words\./, 'a quote is the agent’s own words');
  assert.match(g, /Any other label is a verb and the names its commands used\./, 'what names the others');
  assert.match(g, /A dashed block is a few calls hours from other work\./, 'what a lone stretch is, as it is drawn');
  assert.doesNotMatch(g, /\bR\d+\b/, 'no ruling number');
  // a hover, not a page: no entry stands far above the others
  const longest = Math.max(...Object.entries(G.GLOSS).filter(([k]) => k !== 'tasks').map(([, v]) => v.length));
  assert.ok(g.length <= longest * 1.2, `${g.length} characters, and the longest other entry is ${longest}`);
  // the phases keep their own entry
  assert.match(G.GLOSS.phases, /^phases: /);
});

test('taskAt: the task or lone stretch that holds a time, as phaseAt holds a phase', () => {
  const tasks = [task('A', 100, 200), task('B', 200, 300), task('x', 500, 510, { kind: 'lone' }), task('C', 600, 700)];
  const at = (t) => T.taskAt(tasks, t);
  // from its start up to its end; a gap holds none; the last holds its end as well
  assert.deepEqual([99.9, 100, 199.9, 200, 299.9].map(at), [-1, 0, 0, 1, 1]);
  assert.deepEqual([300, 450, 499.9, 500, 509.9, 510, 599.9].map(at), [-1, -1, -1, 2, 2, -1, -1]);
  assert.deepEqual([600, 650, 700, 700.1].map(at), [3, 3, 3, -1]);
  assert.equal(T.taskAt([], 5), -1);
  assert.equal(T.taskAt([task('A', 10, 20)], 5), -1);
  // the same answer as the phase under it, for stretches that follow one another
  const list = [ph('run', 100, 200), ph('git', 200, 300)];
  for (const t of [50, 100, 150, 200, 250, 300, 350]) assert.equal(T.taskAt(tasks.slice(0, 2), t), P.phaseAt(list, t), `${t}`);
});

test('the tasks are numbered among the tasks, as the lane and the list both say: a lone stretch has no number and takes none', () => {
  const lone = (l) => task(l, 0, 1, { kind: 'lone' });
  const A = (l) => task(l, 0, 1);
  assert.deepEqual(T.taskNumbers([A('a'), A('b'), A('c')]), [1, 2, 3]);
  assert.deepEqual(T.taskNumbers([A('a'), lone('x'), A('b'), lone('y'), A('c')]), [1, null, 2, null, 3]);
  assert.deepEqual(T.taskNumbers([lone('x'), A('a')]), [null, 1]);
  assert.deepEqual(T.taskNumbers([lone('x')]), [null]);
  assert.deepEqual(T.taskNumbers([]), []);
  // the lane says the same: with room for a number and none for a label, each task says its own and a lone stretch says nothing
  const long = 'x'.repeat(40);
  const mixed = [task(long, 0, 100), task(long, 100, 200, { kind: 'lone' }), task(long, 200, 300), task(long, 300, 400), task(long, 400, 500, { kind: 'lone' }), task(long, 500, 600)];
  const said = T.taskNumbers(mixed).map((n) => (n === null ? '' : String(n)));
  assert.deepEqual(said, ['1', '', '2', '3', '', '4']);
  assert.deepEqual(T.taskBlocks(mixed, [0, 600], 600).map((b) => b.text), said);
  // the tasks a view does not show are counted all the same
  assert.deepEqual(T.taskBlocks(mixed, [250, 600], 350).map((b) => b.text), ['2', '3', '', '4']);
  // every block of the fixture: its tasks are 1, 2, and so on in order, and its lone stretches have none
  for (const r of blocks) {
    const tasks = r.phases.tasks;
    const nums = T.taskNumbers(tasks);
    assert.equal(nums.length, tasks.length, r.sid);
    assert.deepEqual(nums.filter((n) => n !== null), tasks.filter((t) => t.kind === 'task').map((_, k) => k + 1), r.sid);
    tasks.forEach((t, i) => assert.equal(nums[i] === null, t.kind === 'lone', `${r.sid} ${i}`));
  }
  // one function: the lane takes its numbers from it (its own declaration is the other call), and so does the list
  assert.equal(callsOf('lib/tasks.ts', 'taskNumbers'), 2, 'the declaration, and the lane');
  assert.equal(callsOf('tabs/TaskList.tsx', 'taskNumbers'), 1, 'the list');
});

test('the task that holds the zoom is the one the view covers most, and none where the chart is not zoomed or the view lies in a gap', () => {
  const tasks = [task('A', 100, 200), task('B', 200, 300), task('x', 500, 500, { kind: 'lone' }), task('C', 600, 700)];
  const at = (v) => T.taskInView(tasks, v);
  // no zoom, no task
  assert.equal(at(null), -1);
  assert.equal(at(undefined), -1);
  // a view that is a task's own span is held by it, though its neighbour begins where it ends
  assert.deepEqual([[100, 200], [200, 300], [600, 700]].map(at), [0, 1, 3]);
  // inside one, and across the whole of one and a part of the next
  assert.deepEqual([[120, 150], [650, 660], [100, 250]].map(at), [0, 3, 0]);
  // across two: the one it covers more of, and the earlier where it covers both alike
  assert.equal(at([150, 260]), 1);
  assert.equal(at([150, 250]), 0);
  assert.equal(at([0, 1000]), 0, 'covering three alike, the first');
  assert.equal(at([650, 5000]), 3);
  // in a gap, before the first, past the last, or touching only the edge of one: none holds it
  assert.deepEqual([[320, 480], [0, 50], [710, 900], [300, 400], [0, 100], [700, 800]].map(at), [-1, -1, -1, -1, -1, -1]);
  // a task of no time, which a view of the chart's narrowest zoom is made on, is held by a view that includes it
  assert.equal(at([500, 501]), 2);
  assert.equal(at([499.5, 500.5]), 2);
  assert.equal(at([400, 499]), -1);
  assert.equal(at([501, 550]), -1);
  // a task of a fraction of a second is covered by the view that holds it
  assert.equal(T.taskInView([task('q', 10, 10.2), task('r', 40, 90)], [10, 11]), 0);
  assert.equal(T.taskInView([], [0, 10]), -1);
  // the view of every task of the fixture on its own span is held by that task, and the whole session by one of them
  for (const r of blocks) {
    const ts = r.phases.tasks;
    ts.forEach((t, i) => {
      const v = [t.t0, Math.max(t.t1, t.t0 + 1)];
      assert.equal(T.taskInView(ts, v), i, `${r.sid} ${t.label}`);
    });
    assert.ok(T.taskInView(ts, [ts[0].t0, ts.at(-1).t1]) >= 0, r.sid);
  }
  // the list asks it of the chart's own zoom
  assert.equal(callsOf('tabs/TaskList.tsx', 'taskInView'), 1);
});

test('what a task says of a call still running in it and of a cut log is one function, which the hover and the list both take', () => {
  const T0 = 1789400000;
  const t = task('Fix the loader', T0, T0 + 200, { source: 'commit', n: 12, phases: [0, 2] });
  const b = { list: [ph('explore', T0, T0 + 100), ph('run', T0 + 100, T0 + 200)], tasks: [t], cut: 5, cut_at: T0 + 150 };
  const running = 'a call in it is still running';
  const full = `incomplete from ${F.clock(T0 + 150)}: the call log was full`;
  assert.deepEqual(T.taskState(t, b), []);
  assert.deepEqual(T.taskState({ ...t, open: true }, b), [running]);
  assert.deepEqual(T.taskState({ ...t, incomplete: true }, b), [full]);
  assert.deepEqual(T.taskState({ ...t, open: true, incomplete: true }, b), [running, full], 'still running first');
  assert.deepEqual(T.taskState({ ...t, incomplete: true }, { ...b, cut_at: null }), ['incomplete: the call log was full']);
  // the words are the open phase's and the phase list's note: nothing new is spelled here
  assert.deepEqual(T.taskState({ ...t, open: true }, b), [P.openClause()]);
  assert.deepEqual(T.taskState({ ...t, incomplete: true }, b), [P.incompleteFrom(b.cut_at)]);
  // on a pinned clock, which will not grow, the call in it was still running when the record ended
  K.setClockPinned(true);
  try {
    assert.deepEqual(T.taskState({ ...t, open: true }, b), ['a call in it was still running when the record ended']);
  } finally {
    K.setClockPinned(false);
  }
  // a lone stretch has them as a task does
  assert.deepEqual(T.taskState({ ...t, kind: 'lone', open: true, incomplete: true }, b), [running, full]);
  // the hover is the label and what it holds, and then these lines: for every task of every row
  for (const r of blocks) {
    const bl = r.phases;
    for (const x of bl.tasks) {
      const lines = T.taskTip(x, bl).split('\n');
      const own = T.taskState(x, bl);
      assert.deepEqual(lines.slice(lines.length - own.length), own, `${r.sid} ${x.label}`);
      assert.equal(lines.length, own.length + (x.kind === 'lone' ? 1 : 2), `${r.sid} ${x.label}`);
    }
  }
  // the list says it with these, and spells no wording of its own
  assert.equal(callsOf('tabs/TaskList.tsx', 'taskState'), 1);
  assert.equal(callsOf('tabs/TaskList.tsx', 'openClause'), 0);
  assert.equal(callsOf('tabs/TaskList.tsx', 'incompleteFrom'), 0);
  assert.equal(callsOf('lib/tasks.ts', 'taskState'), 2, 'the declaration, and the hover');
});

test("a pick is held by another day's row when its task or phase names a row that is not the open one", () => {
  const A = 'boslogin02|2026-09-13|agent|apid:4242';
  const B = 'boslogin02|2026-09-14|agent|apid:4242';
  assert.equal(T.otherDay({ sid: B }, A), B);
  assert.equal(T.otherDay({ sid: A }, A), null);
  assert.equal(T.otherDay({ sid: null }, A), null, 'an agent of one day has no other');
  assert.equal(T.otherDay({}, A), null, 'a block of the first version has none');
  assert.equal(T.otherDay({ sid: '' }, A), null);

  // The fixture's agent over two day rows: its tasks carry two distinct sids, and each is a row of the fixture.
  const pair = blocks.filter((r) => r.phases.days);
  assert.equal(pair.length, 2);
  const ids = new Set(rows.map((r) => r.sid));
  const taskSids = new Set(pair[0].phases.tasks.map((t) => t.sid));
  assert.equal(taskSids.size, 2, `two distinct sids on the tasks: ${[...taskSids]}`);
  for (const s of taskSids) assert.ok(ids.has(s), `${s} is a row of the fixture`);
  assert.deepEqual([...taskSids].sort(), pair.map((r) => r.sid).sort());

  // From either day's row, a task or phase of the other day jumps to that row, and one of this day does not
  for (const [open, other] of [[pair[0].sid, pair[1].sid], [pair[1].sid, pair[0].sid]]) {
    const b = pair[0].phases;
    const away = [...b.tasks, ...b.list].filter((x) => x.sid === other);
    const here = [...b.tasks, ...b.list].filter((x) => x.sid === open);
    assert.ok(away.length >= 2 && here.length >= 2, 'something to go to, and something to stay for');
    for (const x of away) assert.equal(T.otherDay(x, open), other, `${open}: ${x.t0}`);
    for (const x of here) assert.equal(T.otherDay(x, open), null, `${open}: ${x.t0}`);
  }
  // an agent of one day never jumps
  for (const r of blocks.filter((x) => !x.phases.days)) {
    for (const x of [...r.phases.tasks, ...r.phases.list]) assert.equal(T.otherDay(x, r.sid), null, r.sid);
  }
});

test('a command of a phase is said as the phase line says it: its masked preview on one line, and how many calls ran it where more than one', () => {
  assert.equal(P.commandLine(['rg -n foo', 9]), 'rg -n foo ×9');
  assert.equal(P.commandLine(['ls -la', 1]), 'ls -la');
  assert.equal(P.commandLine(['git status\n  --short', 1]), 'git status --short');
  assert.equal(P.commandLine(['sleep 600', 1234]), `sleep 600 ×${F.fint(1234)}`);
  // the phase's own line is these, in order, after its count and its mix
  const top = [['rg -n foo', 9], ['ls -la', 1], ['cat  x\n y', 3]];
  assert.equal(P.phaseWhat(ph('explore', 0, 1, { n: 13, top })), `13 calls · ${top.map(P.commandLine).join(', ')}`);
  // every phase of the fixture: each command of its line is in the line
  for (const r of blocks) {
    for (const p of r.phases.list) for (const c of p.top) assert.ok(P.phaseWhat(p).includes(P.commandLine(c)) || p.kind === 'loop' || p.kind === 'wait', `${r.sid} ${p.kind}: ${c[0]}`);
  }
});

test('the fixture: every phase of every row reads, and nothing is made up', () => {
  assert.ok(blocks.length >= 10);
  const seen = new Set();
  for (const r of blocks) {
    const b = r.phases;
    // a row of one day counts the block's calls; a row of an agent over several days counts its own day's,
    // and the days add up to the block's (the tasks test below)
    if (!b.days) assert.equal(r.n_calls, b.n_calls);
    assert.match(P.housekeepingText(b.housekeeping), /^housekeeping, not the model: \d+ commands?( \(.+\))?( · \d+ more under a process that never exited)?$/);
    // R43: an int, always there; of the unplaced, and said in the housekeeping line when there are any
    const hk = b.housekeeping;
    assert.ok(Number.isInteger(hk.never_exited) && hk.never_exited >= 0 && hk.never_exited <= hk.cut, `${r.sid}: never_exited ${hk.never_exited}`);
    assert.equal(P.housekeepingText(hk).includes('more under a process that never exited'), hk.never_exited > 0);
    for (const p of b.list) {
      seen.add(p.kind);
      const what = P.phaseWhat(p);
      assert.ok(what.length > 0 || p.kind === 'unknown', `${r.sid} ${p.kind}: empty detail`);
      // R56, R65: an open phase, and only one, says a call in it is still running (the fixture's clock goes
      // on), and names what was seen under it; a phase with no open call names nothing seen
      assert.equal(/ · a call in it is still running(, seen through .+)?$/.test(what), p.open === true, `${r.sid} ${p.kind}: ${what}`);
      if (p.open) assert.ok(p.kind !== 'idle' && p.kind !== 'edit_guess', `${r.sid}: an open ${p.kind}`);
      if (p.open) assert.ok(Array.isArray(p.seen), `${r.sid} ${p.kind}: an open phase with no seen`);
      if (!p.open) assert.equal(p.seen, null, `${r.sid} ${p.kind}: seen on a phase with no open call`);
      if (p.every_s == null) assert.equal(p.every_cmd, null, `${r.sid} ${p.kind}: every_cmd with no pace`);
      assert.doesNotMatch(what, /undefined|null|NaN|\[object|—/, `${r.sid} ${p.kind}: ${what}`);
      assert.doesNotMatch(P.phaseShort(p), /undefined|null|NaN/);
      assert.notEqual(P.phaseCol(p.kind), undefined);
      assert.notEqual(P.phaseLbl(p.kind), p.kind, `${p.kind} has no label`);
    }
    // item 10: the legend lists what the bands draw, in a swatch each, and nothing they do not
    const legend = P.phaseLegend(b.list, [b.list[0].t0 - 1, b.list[b.list.length - 1].t1 + 1]);
    const drawn = new Set(b.list.filter((p) => p.kind !== 'idle' && p.kind !== 'edit_guess').map((p) => P.phaseCol(p.kind)));
    assert.deepEqual(new Set(legend.filter((e) => e.look === 'fill').map((e) => e.fill)), drawn, `${r.sid}: the tints`);
    assert.equal(new Set(legend.map((e) => `${e.look} ${e.fill}`)).size, legend.length, `${r.sid}: two swatches alike`);
    assert.equal(legend.some((e) => e.look === 'guess'), b.list.some((p) => p.kind === 'edit_guess' || (p.slices ?? []).length > 0), `${r.sid}: the hatch`);
    assert.equal(legend.some((e) => e.look === 'idle'), b.list.some((p) => p.kind === 'idle'), `${r.sid}: idle`);
    assert.equal(legend.at(-1).look === 'idle', b.list.some((p) => p.kind === 'idle'), `${r.sid}: idle last`);
    const line = P.sequenceLine(b.list, r.running?.now);
    assert.ok(line.split(' → ').length <= 10, line);
    if (b.list.length > 9) assert.ok(line.includes(' → … → '), line);
    const edge = r.running?.now ? P.edgeLabel(r.running.now).split(' · ')[0] : null;
    if (edge) assert.ok(line.endsWith(edge), `${line} should end with ${edge}`);
  }
  for (const [k] of P.PHASES) assert.ok(seen.has(k), `no fixture phase is ${k}`);
  const now = rows.filter((r) => r.running).map((r) => r.running.now?.state);
  assert.deepEqual(now, ['running', 'idle', 'unknown', 'running']);
});

test('the fixture: a phase on a dark slot begins at a guessed slice, so its name lies over hatching', () => {
  // A guessed slice's ground is the surface, with the hatch's lines over it, and a phase's name is drawn over it
  // with a halo so the lines do not run through the letters. A slice that begins a phase (on a dark slot, wait,
  // build, or Slurm, where a name once had light text that vanished there) and reaches past where its name
  // starts is what shows it, at any chart width from 400 px up: 1.5% of the session is 6 px there, and a name
  // starts 4 px in.
  const dark = new Set(['wait', 'build', 'slurm']);
  const hits = blocks.flatMap((r) =>
    r.phases.list
      .filter((p) => dark.has(p.kind) && p.slices?.length && p.slices[0][0] === p.t0)
      .map((p) => ({ r, p, len: p.slices[0][1] - p.slices[0][0], share: (p.slices[0][1] - p.slices[0][0]) / (r.last - r.first) })),
  );
  assert.ok(hits.length >= 1, 'no wait, build, or Slurm phase begins at a guessed slice');
  for (const { r, p, len } of hits) {
    assert.ok(len >= 20 && len <= 300, `${r.sid} ${p.kind}: a guess is 20 s to 5 min, not ${len} s`);
    assert.ok(p.slices[0][1] <= p.t1, `${r.sid} ${p.kind}: the slice runs past its phase`);
    assert.ok(typeof p.guess === 'string' && p.guess.length > 0, `${r.sid} ${p.kind}: a slice with no reason`);
  }
  assert.ok(hits.some(({ share }) => share >= 0.015), 'no such slice is wide enough to reach a label');
});

test('the fixture: commands under a process that never exited, among the top five shapes and past them (R43)', () => {
  // The housekeeping line's separation must not depend on the shape being listed: `shapes` holds the top five only.
  const never = 'under a process that never exited';
  const has = blocks.filter((r) => r.phases.housekeeping.never_exited > 0);
  const listed = has.filter((r) => r.phases.housekeeping.shapes.some(([s]) => s === never));
  const past = has.filter((r) => !r.phases.housekeeping.shapes.some(([s]) => s === never));
  assert.ok(listed.length >= 1, 'no row lists the shape');
  assert.ok(past.length >= 1, 'no row has them past its top five shapes');
  for (const r of listed) {
    const hk = r.phases.housekeeping;
    assert.equal(hk.shapes.find(([s]) => s === never)[1], hk.never_exited, r.sid);
  }
  for (const r of past) {
    const hk = r.phases.housekeeping;
    assert.equal(hk.shapes.length, 5, r.sid);
    assert.ok(P.housekeepingText(hk).endsWith(` · ${hk.never_exited} more under a process that never exited`), r.sid);
    assert.ok(!P.housekeepingText(hk).includes(never + ')'), r.sid);
  }
});

test('the fixture: a row no phases are read for says so, as the service sends it (R41)', () => {
  // Claude Code and Codex are read. Every other row, an agent of another type, a person, and an IDE,
  // carries `{unavailable}` and a null `n_calls`, and none of those is a block.
  const read = new Set(['claude_code', 'codex']);
  for (const r of rows) {
    const reads = r.actor3 === 'agent' && read.has(r.agent_type);
    assert.equal(P.isBlock(r.phases), reads, `${r.sid} (${r.actor3}, ${r.agent_type})`);
    if (reads) continue;
    assert.deepEqual(r.phases, { unavailable: 'phases not available for this agent type' }, r.sid);
    assert.equal(r.n_calls, null, r.sid);
  }
  // the page notes it under an agent of a type that is not read (Copilot here), and the service never says
  // it of Codex, which is read
  assert.ok(rows.some((r) => r.actor3 === 'agent' && r.agent_type === 'copilot' && !P.isBlock(r.phases)));
  assert.ok(rows.every((r) => r.agent_type !== 'codex' || P.isBlock(r.phases)));
  // a person and an IDE (the Cursor row among them) have the same, and the page says nothing under them
  assert.ok(rows.some((r) => r.actor3 === 'human' && !P.isBlock(r.phases)));
  assert.ok(rows.some((r) => r.actor3 === 'ide' && r.agent_type === 'cursor' && !P.isBlock(r.phases)));
});

test("the fixture: tasks tile every block's phases, and every phase names its task", () => {
  const BASIS = 'read from the commands the agent ran and the words it wrote in them';
  const GAP_S = 3600; // `phase_tasks.DEFAULTS.gap_s`: a gap this long with nothing the agent waits on cuts a task
  const unread = /undefined|null|NaN|\[object|—/;
  const ids = new Set(rows.map((r) => r.sid));
  for (const r of blocks) {
    const b = r.phases;
    assert.equal(b.v, 2, r.sid);
    assert.equal(b.basis, BASIS, r.sid);
    assert.ok(Array.isArray(b.tasks) && b.tasks.length >= 1, `${r.sid}: no tasks`);
    assert.ok(typeof b.headline === 'string' || b.headline === null, `${r.sid}: headline ${b.headline}`);
    assert.doesNotMatch(b.headline ?? '', unread, r.sid);
    assert.ok(b.tasks.some((t) => t.kind === 'task'), `${r.sid}: only lone stretches, which the service never sends`);
    const dayIds = b.days ? new Set(b.days.map((d) => d.sid)) : null;

    // every phase says what it is of. `alive` is on an idle gap alone: up to three calls that began before the gap did
    for (const p of b.list) {
      assert.ok(p.alive === null || Array.isArray(p.alive), `${r.sid} ${p.kind}: alive ${p.alive}`);
      if (p.alive) {
        assert.equal(p.kind, 'idle', `${r.sid}: alive on a ${p.kind}`);
        assert.ok(p.alive.length >= 1 && p.alive.length <= 3, `${r.sid}: ${p.alive.length} alive`);
        for (const [cmd, since] of p.alive) {
          assert.ok(cmd === null || (typeof cmd === 'string' && cmd.length > 0), `${r.sid}: alive ${cmd}`);
          assert.ok(since <= p.t0, `${r.sid}: a call that began at ${since}, in a gap from ${p.t0}`);
        }
      }
      assert.ok(Number.isInteger(p.task), `${r.sid}: task ${p.task}`);
      if (dayIds) assert.ok(dayIds.has(p.sid), `${r.sid}: a phase on a day no row holds, ${p.sid}`);
      else assert.equal(p.sid, null, `${r.sid}: a sid on a phase of one day`);
    }

    // the ranges follow one another in order and hold every phase once, and each phase names the task whose range holds it
    let next = 0;
    b.tasks.forEach((t, j) => {
      const [a, z] = t.phases;
      const where = `${r.sid} task ${j} (${t.label})`;
      assert.equal(a, next, `${where}: begins at phase ${a}, not ${next}`);
      assert.ok(z > a && z <= b.list.length, `${where}: range ${t.phases}`);
      next = z;
      for (let i = a; i < z; i++) assert.equal(b.list[i].task, j, `${where}: phase ${i}`);
      const mine = b.list.slice(a, z);
      const before = j ? b.list[a - 1] : null;
      // the cutting gap before it is the idle phase that closes the task before, and only a gap of the hour and more is one
      const cuts = before?.kind === 'idle' && before.t1 - before.t0 >= GAP_S;

      assert.ok(['task', 'lone'].includes(t.kind), `${where}: kind ${t.kind}`);
      assert.equal(t.n, mine.reduce((s, p) => s + p.n, 0), `${where}: calls`);
      assert.ok(mine[0].n > 0 && mine[0].kind !== 'idle' && mine[0].kind !== 'edit_guess', `${where}: begins at a ${mine[0].kind}, which holds no calls`);
      if (t.kind === 'lone') assert.ok(t.n <= 8, `${where}: a lone stretch of ${t.n} calls`);
      assert.equal(t.t0, mine[0].t0, `${where}: t0`);
      assert.ok(t.t0 <= t.t1 && t.t1 <= mine[mine.length - 1].t1, `${where}: ${t.t0} to ${t.t1}`);
      if (j) {
        assert.equal(t.gap !== null, cuts, `${where}: a gap, or none, where a ${before.kind} of ${before.t1 - before.t0} s ends the task before`);
        assert.equal(b.tasks[j - 1].t1, cuts ? before.t0 : t.t0, `${where}: where the task before ends`);
      } else {
        assert.equal(t.gap, null, `${where}: the first task has no gap before it`);
      }
      if (t.gap) {
        assert.ok(near(t.gap.s, before.t1 - before.t0) && t.gap.s >= GAP_S, `${where}: gap ${t.gap.s}`);
        assert.equal(t.t0, before.t1, `${where}: begins where its gap ends`);
        // the calls its idle phase lists, each with the name the gap line calls it by, which a phase has no tag sets to give
        assert.deepEqual(t.gap.alive.map(([preview, since]) => [preview, since]), before.alive ?? [], `${where}: the gap's alive is its idle phase's`);
        for (const a of t.gap.alive) assert.ok(a.length === 3 && (a[2] === null || typeof a[2] === 'string'), `${where}: ${JSON.stringify(a)}`);
      }
      assert.equal(t.open, mine.some((p) => p.open), `${where}: open`);
      assert.equal(t.incomplete, b.cut > 0 && (j === b.tasks.length - 1 || t.t1 > b.cut_at), `${where}: incomplete`);
      assert.ok(t.source === null || ['pr', 'commit', 'log'].includes(t.source), `${where}: source ${t.source}`);
      if (t.source !== 'pr') assert.equal(t.ref, '', `${where}: ref ${t.ref}`);
      assert.ok(typeof t.label === 'string' && t.label.length > 0, `${where}: no label`);
      assert.ok(Array.isArray(t.names) && t.names.length <= 2 && t.names.every((n) => typeof n === 'string' && n.length > 0), `${where}: names ${t.names}`);
      if (dayIds) assert.ok(dayIds.has(t.sid), `${where}: a task on a day no row holds, ${t.sid}`);
      else assert.equal(t.sid, null, `${where}: a sid on a task of one day`);

      // the copy the page says of it reads
      const said = [T.taskLabel(t), T.taskWhat(t, b), t.gap ? T.gapLine(t.gap) : '', t.kind === 'lone' ? T.loneLine(t) : ''];
      for (const s of said) assert.doesNotMatch(s, unread, `${where}: ${s}`);
      assert.match(T.taskWhat(t, b), /^\d[\d,]* phases? · \d[\d,]* calls?$/, where);
      assert.equal(T.taskLabel(t).startsWith('“'), t.source !== null, where);
      if (t.source !== null) assert.match(T.taskSource(t), /^(commit|log|PR( #\d+)?)$/, where);
      if (t.gap) assert.equal(/ still running since \d\d:\d\d:\d\d$/.test(T.gapLine(t.gap)), t.gap.alive.length > 0, `${where}: ${T.gapLine(t.gap)}`);
      if (t.kind === 'lone') assert.match(T.loneLine(t), /^\d+ calls? at \d\d:\d\d:\d\d · .+$/, where);
    });
    assert.equal(next, b.list.length, `${r.sid}: the tasks stop at phase ${next} of ${b.list.length}`);
    assert.equal(b.tasks.reduce((s, t) => s + t.n, 0), b.n_calls - b.cut, `${r.sid}: calls`);
    // A task opens into its range less the idle phase of the cutting gap after it, which is said once, as the next task's
    // gap line. So the phases the tasks open into are the block's, less one for each gap, and a copy of a task opens into the same.
    const opened = b.tasks.reduce((s, t) => s + T.taskPhases(t, b).length, 0);
    assert.equal(opened, b.list.length - b.tasks.filter((t) => t.gap !== null).length, `${r.sid}: the phases the tasks open into`);
    for (const t of b.tasks) assert.deepEqual(T.taskPhases({ ...t }, b), T.taskPhases(t, b), `${r.sid} ${t.label}: a copy of it`);
    // the row's line names the first two tasks, quoted ones in their quotation marks, and counts the rest; no lone stretch is among them
    const named = b.tasks.filter((t) => t.kind === 'task');
    assert.equal(
      T.taskSequence(b),
      [...named.slice(0, 2).map(T.taskLabel), ...(named.length > 2 ? [`${named.length - 2} more`] : [])].join(' → '),
      r.sid,
    );
  }

  // what the page has to draw right is there, on some row or other
  const all = blocks.flatMap((r) => r.phases.tasks.map((t) => ({ r, t })));
  const some = (what, f) => assert.ok(all.some(({ r, t }) => f(t, r)), `no fixture task is ${what}`);
  some("quoted from a commit's subject", (t) => t.source === 'commit');
  some('quoted from a log entry', (t) => t.source === 'log');
  some('quoted from a pull request', (t) => t.source === 'pr' && /^#\d+$/.test(t.ref));
  // the service sets a ref only for a pull request edited by its number (`gh pr edit 235`), and a created one has none yet
  some('quoted from a pull request with no number yet, so its source is the bare `PR`', (t) => t.source === 'pr' && t.ref === '' && T.taskSource(t) === 'PR');
  some('built from names', (t) => t.kind === 'task' && t.source === null);
  some('a lone stretch', (t) => t.kind === 'lone');
  some('after a gap that names a program still running', (t) => t.gap?.alive.some(([c]) => c !== null));
  some('after a gap with a call still running, and no program to name', (t) => t.gap?.alive.some(([c]) => c === null));
  some('after a gap with nothing running', (t) => t.gap && t.gap.alive.length === 0);
  some('open at the live edge of a session running now', (t, r) => t.open && r.running && t === r.phases.tasks.at(-1));
  some('incomplete, from a call log that was cut', (t, r) => t.incomplete && r.phases.cut > 0);
  assert.ok(blocks.some((r) => r.phases.headline === null) && blocks.some((r) => r.phases.headline), 'a headline, and none');
  assert.ok(blocks.some((r) => r.phases.list.some((p) => p.kind === 'idle' && p.alive === null)), 'an idle gap with nothing running');

  // one agent over two day rows: both serve the one block, which lists its days, and each task and phase says which holds it
  const pair = blocks.filter((r) => r.phases.days);
  assert.equal(pair.length, 2, 'one agent over two day rows');
  const [d1, d2] = pair;
  assert.deepEqual(d1.phases, d2.phases, 'the same block on both rows');
  const days = d1.phases.days;
  assert.deepEqual(days.map((d) => d.sid), [d1.sid, d2.sid], 'the days, in time order');
  for (const d of days) {
    const row = rows.find((r) => r.sid === d.sid);
    assert.ok(ids.has(d.sid) && row, `${d.sid} is no row`);
    assert.ok(d.first <= d.last, d.sid);
    assert.deepEqual([d.first, d.last], [row.first, row.last], `${d.sid}: its row's first and last`);
  }
  assert.ok(days[0].last < days[1].first, 'the days do not overlap');
  assert.deepEqual([d1.user, d1.host, d1.agent_type], [d2.user, d2.host, d2.agent_type], 'one agent');
  // a task and a phase on each day, and the rows' own counts add up to the block's
  const onDay = (items, d) => items.filter((x) => x.sid === d.sid);
  assert.ok(onDay(d1.phases.tasks, days[0]).length >= 1 && onDay(d1.phases.tasks, days[1]).length >= 1, 'a task on each day');
  assert.ok(onDay(d1.phases.list, days[0]).length >= 1 && onDay(d1.phases.list, days[1]).length >= 1, 'a phase on each day');
  assert.equal(d1.n_calls + d2.n_calls, d1.phases.n_calls, 'the days add up');
  assert.ok(d1.n_calls > 0 && d2.n_calls > 0, 'each day has calls of its own');
  // a task is not cut at midnight: one begins on the first day and ends on the second, and holds its first day's sid
  const across = d1.phases.tasks.filter((t) => t.t0 <= days[0].last && t.t1 >= days[1].first);
  assert.ok(across.length >= 1, 'no task runs across midnight');
  for (const t of across) assert.equal(t.sid, days[0].sid, t.label);
  // a task and a phase hold the sid of the day that holds their start (`trajectories.day_of`), else the nearest
  const dayOf = (t) =>
    days.reduce((best, d) => {
      const away = t < d.first ? d.first - t : Math.max(0, t - d.last);
      return away < best.away ? { away, sid: d.sid } : best;
    }, { away: Infinity, sid: null }).sid;
  for (const x of [...d1.phases.tasks, ...d1.phases.list]) assert.equal(x.sid, dayOf(x.t0), `${x.t0}`);
});

test('the lane: a label where it fits, a number where only that does, nothing below that', () => {
  const lane = (tasks, axis = [0, 1000], W = 1000) => T.taskBlocks(tasks, axis, W);
  const shape = (bs) => bs.map(({ i, x, w, text }) => ({ i, x: Math.round(x * 1e6) / 1e6, w: Math.round(w * 1e6) / 1e6, text }));
  // 1000 px over 1000 s, a pixel a second. A stretch wider than 3 px is parted from the next by 0.6 px, as the bands are.
  const tasks = [
    task('Fix the loader', 0, 300), // 299.4 px: its 14 characters need 93.4
    task('Run b.py', 300, 330), //     29.4 px: its 8 characters need 56.8, and its number 14.1
    task('Read x', 330, 340), //        9.4 px: not even its number
    task('Edit y', 340, 1000),
  ];
  assert.deepEqual(shape(lane(tasks)), [
    { i: 0, x: 0, w: 299.4, text: 'Fix the loader' },
    { i: 1, x: 300, w: 29.4, text: '2' },
    { i: 2, x: 330, w: 9.4, text: '' },
    { i: 3, x: 340, w: 659.4, text: 'Edit y' },
  ]);
  // a label fits where a band's label does, never cut; then its number, where that fits; then nothing
  const one = [task('explore', 0, 1000)];
  const need = 7 * P.LABEL_CHAR_W + P.LABEL_PAD;
  assert.equal(lane(one, [0, 1000], need + 0.6 + 1)[0].text, 'explore');
  assert.equal(lane(one, [0, 1000], need + 0.6 - 1)[0].text, '1');
  assert.equal(lane(one, [0, 1000], P.LABEL_CHAR_W + P.LABEL_PAD + 0.6 + 1)[0].text, '1');
  assert.equal(lane(one, [0, 1000], P.LABEL_CHAR_W + P.LABEL_PAD + 0.6 - 1)[0].text, '');
  // a quote is shown in its quotation marks; its source is for the list and the hover
  assert.equal(lane([task('Fix it', 0, 300, { source: 'commit' })])[0].text, '“Fix it”');
  // a number of two digits needs more room than one of one
  const long = 'x'.repeat(30);
  const wide = Array.from({ length: 12 }, (_, k) => task(long, k * 25, k * 25 + 25));
  const narrow = Array.from({ length: 12 }, (_, k) => task(long, k * 18, k * 18 + 18));
  assert.deepEqual(lane(wide, [0, 300], 300).map((b) => b.text), ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']);
  assert.deepEqual(lane(narrow, [0, 216], 216).map((b) => b.text), ['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '', '']);
  // cut at the view, as the bands are; one outside it is not there, and the rest keep their place in `tasks`
  assert.deepEqual(shape(lane(tasks, [100, 400], 300)), [
    { i: 0, x: 0, w: 199.4, text: 'Fix the loader' },
    { i: 1, x: 200, w: 29.4, text: '2' },
    { i: 2, x: 230, w: 9.4, text: '' },
    { i: 3, x: 240, w: 59.4, text: 'Edit y' },
  ]);
  // one wider than both edges of the view is cut at both, fills it, and keeps its place in `tasks`
  assert.deepEqual(shape(lane(tasks, [500, 900])), [{ i: 3, x: 0, w: 999.4, text: 'Edit y' }]);
  assert.deepEqual(shape(lane([task('Run it', -500, 2000)])), [{ i: 0, x: 0, w: 999.4, text: 'Run it' }]);
  assert.deepEqual(lane(tasks, [2000, 3000]), []);
  assert.deepEqual(lane([]), []);
  // a stretch of no length is there where it sits, 1 px wide and kept inside the lane
  const point = lane([task('Read z', 500, 500), task('Read w', 1000, 1000)]);
  assert.deepEqual(shape(point).map(({ i, x, w }) => [i, x, w]), [[0, 500, 1], [1, 999, 1]]);
  // a lane narrower than a pixel: every block is one pixel at its left edge, with no room for a word, and none is NaN or left of 0
  assert.deepEqual(shape(lane(tasks, [0, 1000], 0.5)), [0, 1, 2, 3].map((i) => ({ i, x: 0, w: 1, text: '' })));
  assert.deepEqual(shape(lane(tasks, [0, 1000], 0)), [0, 1, 2, 3].map((i) => ({ i, x: 0, w: 1, text: '' })));
  assert.deepEqual(shape(lane(tasks, [100, 400], 0.5)).map(({ i, x, w }) => [i, x, w]), [[0, 0, 1], [1, 0, 1], [2, 0, 1], [3, 0, 1]]);
  // A task is numbered by its place among the tasks. A lone stretch has no number and says nothing, though its label
  // would fit, and it takes none from the tasks after it.
  const mixed = [task('Fix the loader', 0, 300), task('Read x', 300, 400, { kind: 'lone' }), task('Run the long thing', 400, 430), task('Edit it all', 430, 460)];
  assert.deepEqual(lane(mixed).map((b) => [b.i, b.text]), [[0, 'Fix the loader'], [1, ''], [2, '2'], [3, '3']]);
  // the tasks a view does not show are counted all the same
  assert.deepEqual(lane(mixed, [400, 460], 60).map((b) => [b.i, b.text]), [[2, '2'], [3, '3']]);
  // a lone stretch before the first task, and between tasks, one of them wide enough for a number though not for its label
  const first = [task('x', 0, 10, { kind: 'lone' }), task(long, 10, 40), task(long, 40, 70, { kind: 'lone' }), task(long, 70, 100)];
  assert.deepEqual(lane(first, [0, 100], 100).map((b) => [b.i, b.text]), [[0, ''], [1, '1'], [2, ''], [3, '2']]);
});

test('the lane: the task under the pointer is the block drawn there, so one of under a pixel of time has its hover as it has its click', () => {
  // 5,390 s drawn 900 px wide is 6 s a pixel, so a task of 0 s, 0.2 s, or 1 s is a block of the 1 px floor
  const axis = [0, 5390];
  const W = 900;
  const secPerPx = (axis[1] - axis[0]) / W;
  const timeAt = (x) => axis[0] + x * secPerPx;
  // the block that holds `x` as a click finds it: the last one drawn there, since a later hit rect sits on top of an earlier
  const reference = (blocks, x) => blocks.reduce((hit, b) => (x >= b.x && x < b.x + b.w ? b.i : hit), -1);

  for (const dur of [0, 0.01, 0.2, 1, 3, 6, 12]) {
    const tasks = [task('A', 0, 1000), task('Read x', 2000, 2000 + dur, { kind: 'lone', n: 1 }), task('B', 3000, 4000)];
    const blocks = T.taskBlocks(tasks, axis, W);
    const b = blocks.find((q) => q.i === 1);
    const where = `a lone stretch of ${dur} s`;
    assert.ok(b.w >= P.MIN_W, `${where}: ${b.w} px`);
    // the whole of the drawn block answers, from its left edge to just short of its right
    for (const f of [0, 0.01, 0.25, 0.5, 0.75, 0.99]) assert.equal(T.taskAtX(blocks, b.x + f * b.w), 1, `${where}, ${f} of the way across`);
    // and nothing outside it does: the interval is half open, and the tasks about it keep their own
    assert.equal(T.taskAtX(blocks, b.x - 0.01), -1, `${where}, just left`);
    assert.equal(T.taskAtX(blocks, b.x + b.w), -1, `${where}, at its right edge`);
    assert.equal(T.taskAtX(blocks, 100), 0, where);
    assert.equal(T.taskAtX(blocks, 600), 2, where);
  }

  // by time, the hover would miss most of such a block, or all of it: this is why the lane asks its blocks
  const quick = [task('A', 0, 1000), task('Read x', 2000, 2000, { kind: 'lone', n: 1 }), task('Fix y', 3000, 3000.2), task('B', 4000, 5000)];
  const qb = T.taskBlocks(quick, axis, W);
  assert.deepEqual(qb.filter((q) => q.i === 1 || q.i === 2).map((q) => q.w), [1, 1], 'a task of 0 s, and one of 0.2 s, are a pixel each');
  for (const q of qb.filter((x) => x.i === 1 || x.i === 2)) {
    assert.equal(T.taskAt(quick, timeAt(q.x + q.w / 2)), -1, `by time, task ${q.i} is not under the middle of its pixel`);
    for (const f of [0, 0.5, 0.99]) assert.equal(T.taskAtX(qb, q.x + f * q.w), q.i, `by block, task ${q.i}, ${f} of the way across`);
  }

  // where blocks are drawn over one another, the last takes the pointer, as it takes the click: two tasks 3 s apart are half a
  // pixel apart, so their blocks of a pixel each overlap, and a lone stretch is drawn over the middle of a long task
  const shared = [task('A', 100, 100, { n: 1 }), task('B', 103, 103, { n: 1 }), task('Long', 1000, 3000), task('Read z', 2000, 2000.1, { kind: 'lone', n: 1 })];
  const sb = T.taskBlocks(shared, axis, W);
  assert.ok(sb[1].x > sb[0].x && sb[1].x < sb[0].x + sb[0].w, 'the first two overlap');
  assert.equal(T.taskAtX(sb, sb[0].x + 0.1), 0, 'where only the first is drawn');
  assert.equal(T.taskAtX(sb, (sb[1].x + sb[0].x + sb[0].w) / 2), 1, 'where both are, the last takes it');
  assert.equal(T.taskAtX(sb, sb[1].x + sb[1].w - 0.1), 1, 'and where only the last is');
  const lone = sb.find((q) => q.i === 3);
  assert.equal(T.taskAtX(sb, lone.x + 0.5), 3, 'the lone stretch is over the long task');
  assert.equal(T.taskAtX(sb, lone.x - 3), 2, 'and the long task has the rest');
  assert.equal(T.taskAtX(sb, lone.x + lone.w + 3), 2);

  // it returns the task's place in `tasks`, not the block's in the lane: a task off the view has no block, and is counted
  const part = [task('Before', -500, -100), task('A', 100, 600, { kind: 'lone', n: 1 }), task('Read x', 800, 800, { kind: 'lone', n: 1 })];
  const pb = T.taskBlocks(part, axis, W);
  assert.deepEqual(pb.map((q) => q.i), [1, 2], 'the first is off the axis');
  assert.equal(T.taskAtX(pb, pb[1].x + 0.5), 2);
  assert.equal(T.taskAtX(pb, pb[0].x + 1), 1);
  assert.equal(T.taskAtX([], 10), -1, 'no blocks, no task');

  // a block cut at the edge of the view is as wide as it is drawn: from 0 on the left, and short of the right edge by the gap
  const cutView = T.taskBlocks([task('A', -100, 200), task('B', 5300, 6000)], axis, W);
  assert.equal(cutView[0].x, 0);
  assert.equal(T.taskAtX(cutView, 0), 0);
  assert.equal(T.taskAtX(cutView, cutView[1].x + cutView[1].w - 0.01), 1);
  assert.equal(T.taskAtX(cutView, cutView[1].x + cutView[1].w), -1);
  assert.equal(T.taskAtX(cutView, W), -1, 'the right edge of the lane is no block');

  // a sweep over the lane in steps of a quarter pixel finds what a click finds, on a lane of quick tasks, long ones, and overlaps
  const mixed = [...quick, ...shared.map((t) => ({ ...t, t0: t.t0 + 200, t1: t.t1 + 200 })), task('Tail', 5200, 5390)];
  const mb = T.taskBlocks(mixed, axis, W);
  for (let x = 0; x < W; x += 0.25) assert.equal(T.taskAtX(mb, x), reference(mb, x), `x ${x}`);

  // every block the fixture draws answers at its own middle, and at both its ends: its tasks tile the session, so none is
  // drawn over another
  for (const r of blocks) {
    const tasks = r.phases.tasks;
    const bl = T.taskBlocks(tasks, [tasks[0].t0, tasks.at(-1).t1], 900);
    assert.equal(bl.length, tasks.length, r.sid);
    for (const q of bl) {
      for (const f of [0, 0.5, 0.999]) assert.equal(T.taskAtX(bl, q.x + f * q.w), q.i, `${r.sid} task ${q.i}, ${f} of the way across`);
    }
  }
});

test("the chart works its lane's blocks out once and asks them for the pointer, and the inspector's task zoom takes the chart's own floor", () => {
  // `taskAtX` is only as good as its use: the chart draws the blocks it computes, and the hover asks those same blocks, not the
  // time under the pointer (`taskAt`), which a block under a pixel wide outruns
  assert.equal(callsOf('tabs/TrajectoryNav.tsx', 'taskBlocks'), 1, 'one set of blocks, for the drawing and the hover');
  assert.equal(callsOf('tabs/TrajectoryNav.tsx', 'taskAtX'), 1, 'the hover asks the blocks');
  assert.equal(callsOf('tabs/TrajectoryNav.tsx', 'taskAt'), 0, 'and not the time');
  // a task's zoom is no narrower than the chart's own narrowest zoom, which is declared once, in the chart, and imported
  assert.deepEqual(spelledIn('SPAN_MIN ='), ['tabs/TrajectoryNav.tsx']);
  assert.match(readFileSync(new URL('tabs/TrajectoryNav.tsx', SRC), 'utf8'), /export const SPAN_MIN = 1;/);
  const inspector = readFileSync(new URL('tabs/TrajectoryInspector.tsx', SRC), 'utf8');
  assert.match(inspector, /import \{[^}]*\bSPAN_MIN\b[^}]*\} from '\.\/TrajectoryNav';/);
  assert.match(inspector, /zoom: \[t\.t0, Math\.max\(t\.t1, t\.t0 \+ SPAN_MIN\)\]/);
});

test('the chart captions its lanes with the names of the three levels, and has no Tasks caption where its block has no tasks', () => {
  // the page calls the three levels Tasks, Phases, and Processes
  assert.deepEqual(P.LEVELS, { tasks: 'Tasks', phases: 'Phases', processes: 'Processes' });
  const said = (hasTasks, hasPhases) => P.chartLevels(hasTasks, hasPhases).map((l) => P.LEVELS[l]);
  // top to bottom: the lane of tasks, the phases, and the processes the bars are
  assert.deepEqual(said(true, true), ['Tasks', 'Phases', 'Processes']);
  // a block of the first version, or with no tasks, has no lane and no Tasks caption, and keeps the other two
  assert.deepEqual(said(false, true), ['Phases', 'Processes']);
  // a chart with no phases is the strip of process starts alone: its one lane is the processes, and it has the gutter all the same
  assert.deepEqual(said(false, false), ['Processes']);
  // the fixture's blocks: all three, and the same blocks as a block of the first version has them, with the lane taken out
  for (const r of blocks) {
    const b = r.phases;
    assert.deepEqual(said(b.tasks.length > 0, b.list.length > 0), ['Tasks', 'Phases', 'Processes'], r.sid);
    assert.deepEqual(said((undefined ?? []).length > 0, b.list.length > 0), ['Phases', 'Processes'], r.sid);
  }
});

test('the gutter the level captions stand in is as wide as the widest of them in the caption style, and its room', () => {
  const widest = Math.max(...Object.values(P.LEVELS).map((name) => name.length));
  assert.equal(widest, 'Processes'.length);
  // the caption style is `.tn-cap .eyebrow`: a letter is 0.6 em of IBM Plex Mono, and the letters are spaced by 0.08 em
  const rule = /\.tn-cap \.eyebrow \{([^}]*)\}/.exec(readFileSync(new URL('tabs/trajectories.css', SRC), 'utf8'))?.[1] ?? '';
  const size = Number(/font-size:\s*([\d.]+)px/.exec(rule)?.[1]);
  const spacing = Number(/letter-spacing:\s*([\d.]+)em/.exec(rule)?.[1]);
  assert.ok(size > 0 && spacing > 0, `the caption rule: ${rule}`);
  const letter = size * 0.6 + size * spacing;
  assert.ok(P.CAP_CHAR_W >= letter, `a letter is ${letter} px, and the page allows ${P.CAP_CHAR_W}`);
  // the widest caption, and room beside it
  assert.equal(P.GUTTER, Math.ceil(widest * P.CAP_CHAR_W) + P.GUTTER_PAD);
  assert.ok(P.GUTTER - widest * letter >= P.GUTTER_PAD, 'the room beside the widest caption');
  // and about the mockup's 80 px label column, which is 72 px and its 8 px of gap, and no more
  assert.ok(P.GUTTER >= 60 && P.GUTTER <= 80, `${P.GUTTER} px`);
  // the chart is drawn as wide as its box leaves beside the gutter, and never under a pixel, in a box narrower than the gutter
  assert.equal(P.plotW(746), 746 - P.GUTTER);
  assert.equal(P.plotW(P.GUTTER + 1), 1);
  assert.equal(P.plotW(P.GUTTER), 1);
  assert.equal(P.plotW(10), 1);
  assert.equal(P.plotW(0), 1);
});

test('the session track, the chart, and the zoom map all start after the same gutter, and the chart is drawn as wide as that leaves', () => {
  const css = readFileSync(new URL('tabs/trajectories.css', SRC), 'utf8');
  // the rule of a selector, as the stylesheet gives it
  const rule = (selector) => new RegExp(`(?:^|\\n)${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{([^}]*)\\}`).exec(css)?.[1] ?? '';
  // each track is set in from the left by the gutter, and the two that are drawings of their own are as wide as is left
  const inset = { '.tn-track .tn-svg': /margin:[^;]*var\(--tn-gut\)/, '.tn-plot': /padding-left:\s*var\(--tn-gut\)/, '.tn .zmap': /margin-left:\s*var\(--tn-gut\)/ };
  for (const [selector, re] of Object.entries(inset)) assert.match(rule(selector), re, `${selector} starts after the gutter`);
  for (const selector of ['.tn-track .tn-svg', '.tn .zmap']) {
    assert.match(rule(selector), /width:\s*calc\(100% - var\(--tn-gut\)\)/, `${selector} is as wide as the gutter leaves`);
  }
  // the gutter is one size, which the chart sets from `GUTTER` for the stylesheet to use, and takes off its own width
  const nav = readFileSync(new URL('tabs/TrajectoryNav.tsx', SRC), 'utf8');
  assert.match(nav, /'--tn-gut': `\$\{GUTTER\}px`/);
  assert.match(nav, /W=\{plotW\(boxW\)\}/);
});

test("the zoom hint shows over the chart with no phases as it does over the one with phases, and a caption of a level stands at its row in a chart drawn at any width", () => {
  const css = source('tabs/trajectories.css');
  const nav = source('tabs/TrajectoryNav.tsx');
  // The note and the plot: with phases they stand in `.tn-strip`, with none straight under `.tn`, and in both the plot (`.tn-plot`),
  // with the svg inside it, is a sibling of the note, which the rule in styles.css (the svg right after the note) does not reach.
  // So each chart has a rule of its own, and neither is the other's.
  assert.match(css, /\.tn-strip:has\(svg\.strip:hover\) > \.zoomnote \.hint \{\s*visibility: visible;\s*\}/, 'the chart with phases');
  assert.match(css, /\.tn > \.zoomnote:has\(\+ \.tn-plot svg\.strip:hover\) \.hint \{\s*visibility: visible;\s*\}/, 'the chart with none');
  assert.match(nav, /<ZoomNote[\s\S]*?\/>\s*\{merged && <BasisRow \/>\}\s*<div className="tn-plot">/, 'the plot follows the note, the basis line between them only where there are phases');
  assert.match(source('styles.css'), /\.zoomnote:has\(\+ svg:hover\) \.hint \{/, 'the other charts keep theirs');

  // The captions: which levels a chart has, and the rows they stand at, are read where the chart is drawn.
  assert.equal(callsOf('tabs/TrajectoryNav.tsx', 'chartLevels'), 1);
  assert.match(nav, /const levels = chartLevels\(R\.task > 0, bands\);/, 'a Tasks caption only where the chart has a lane, Phases only where it has bands');
  assert.match(nav, /tasks: \[0, R\.task\],/, 'Tasks at the lane');
  assert.match(nav, /phases: \[R\.top, R\.lbl\],/, 'Phases at the row of the names');
  assert.match(nav, /processes: \[BASE - PH, PH\],/, 'Processes at the bars');
  // These rows are in the chart's own units, which are px only where it is drawn at its box's width. The chart with no phases is
  // drawn in `BIN_W` units and scaled to its plot, so a caption's box is its row's share of the plot's height, and sits at the bars
  // there too, where px would put it below them and over the quick jumps in a narrow plot.
  assert.match(nav, /top: `\$\{\(capBox\[level\]\[0\] \/ H\) \* 100\}%`/);
  assert.match(nav, /height: `\$\{\(capBox\[level\]\[1\] \/ H\) \* 100\}%`/);
  assert.doesNotMatch(nav, /top: capBox\[level\]\[0\]/, 'no px of the chart\'s units');
  assert.match(nav, /\{LEVELS\[level\]\}/, 'each in the page\'s name for the level');
});

test("a caption of a level stands at the row it names, in a chart drawn at its box's width and in one drawn in units and scaled to it, and the zoom note and the plot stand where the hint's rule looks for them", async () => {
  const ui = await renderer();
  try {
    const row = rows.find((r) => r.sid === 'period4-holylogin01-thomwg11-1000');
    const det = api.trajectory_detail;
    // the read laid on the row's own span, as the dev server lays it (`alignDetail` of mock/plugin.ts): the bars, and the bands and lane on them
    const start = row.first;
    const end = Math.min(row.last, start + (det.timeline.hi - det.timeline.lo));
    const d = {
      ...det,
      sid: row.sid,
      row,
      nav: { ...det.nav, density: { ...det.nav.density, lo: start }, failures: { items: [], n: 0, listed: 0 } },
      span: { first: row.first, last: row.last },
      scan: { ...det.scan, from: start, first: start, last: end, to: end + 1, stopped: null, stopped_at: null },
      timeline: { lo: start, hi: end },
    };
    const render = (phases) =>
      ui.render('tabs/TrajectoryNav.tsx', 'TrajectoryNav', { d, axis: [start, end], view: null, setView() {}, cursor: null, onPick() {}, onFail() {}, quick: [], status: null, inView: null, phases, now: null, onPhase() {}, onTask() {} });
    /** Where the chart's rows are, in its own units, and where each caption's box is, which is a share of the plot's height. */
    const read = (html) => {
      const [W, H] = /<svg viewBox="0 0 ([\d.]+) ([\d.]+)"[^>]*class="tn-svg strip"/.exec(html).slice(1).map(Number);
      // a caption is its level's name, and the Phases caption the glossary's term for it, with its `?`
      const caps = Object.fromEntries(
        [...html.matchAll(/<div class="tn-cap tn-lane-cap" style="top:([\d.]+)%;height:([\d.]+)%" aria-hidden="true"><span class="eyebrow">(?:<span class="term" data-tip="[^"]*">)?(\w+)(?:<sup class="qm" aria-hidden="true">\?<\/sup><\/span>)?<\/span><\/div>/g)].map(([, top, height, name]) => [name, { y: (top * H) / 100, h: (height * H) / 100 }]),
      );
      const hit = (kind) => {
        const m = new RegExp(`<rect data-${kind}="0" x="[\\d.]+" y="([\\d.]+)" width="[\\d.]+" height="([\\d.]+)"`).exec(html);
        return m ? { y: Number(m[1]), h: Number(m[2]) } : null;
      };
      const base = Number(/<line class="base-line"[^>]* y1="([\d.]+)"/.exec(html)[1]);
      const bars = [...html.slice(html.indexOf('class="zoomable"')).matchAll(/<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="[\d.]+" fill="var\(--(?:s\d|sg)\)"><\/rect>/g)].map((m) => Number(m[1]));
      return { W, H, caps, base, lane: hit('task'), names: hit('phase'), barsTop: Math.min(...bars) };
    };
    const within = (a, b, tol, what) => assert.ok(Math.abs(a - b) <= tol, `${what}: ${a} is not ${b}`);
    const bars = (c, what) => {
      // the bars stand on the baseline, and the caption's box is as tall as they are, to within the least a bar is drawn
      const box = c.caps.Processes;
      within(box.y + box.h, c.base, 0.01, `${what}: the Processes box ends at the baseline`);
      within(box.y, c.barsTop, 4, `${what}: and begins where the tallest bar does`);
      assert.ok(box.y >= 0 && box.y + box.h <= c.H, `${what}: inside the chart`);
    };

    const noPhases = await render(undefined);
    const withTasks = await render(row.phases);
    const noTasks = await render({ ...row.phases, tasks: [] });

    // the chart with no phases is drawn in `BIN_W` units, scaled to its plot: one caption, at the bars, in a share of the plot's height
    const a = read(noPhases);
    assert.deepEqual(Object.keys(a.caps), ['Processes']);
    assert.equal(a.W, 1120);
    bars(a, 'no phases');
    // the chart with phases and a lane, and the one with phases and no lane: a caption at each row that is there
    const b = read(withTasks);
    assert.deepEqual(Object.keys(b.caps), ['Tasks', 'Phases', 'Processes']);
    assert.ok(b.lane && b.names, 'the lane and the names are on the axis');
    within(b.caps.Tasks.y, b.lane.y, 0.01, 'Tasks: the lane, from its top');
    within(b.caps.Tasks.h, b.lane.h, 0.01, 'Tasks: the lane, as tall');
    within(b.caps.Phases.y, b.names.y, 0.01, 'Phases: the names, from their top');
    within(b.caps.Phases.h, b.names.h, 0.01, 'Phases: the names, as tall');
    bars(b, 'tasks and phases');
    assert.ok(b.caps.Tasks.y + b.caps.Tasks.h <= b.caps.Phases.y && b.caps.Phases.y + b.caps.Phases.h <= b.caps.Processes.y, 'one above the next');
    const c = read(noTasks);
    assert.deepEqual(Object.keys(c.caps), ['Phases', 'Processes']);
    assert.equal(c.lane, null);
    within(c.caps.Phases.y, c.names.y, 0.01, 'no tasks: Phases at the names');
    bars(c, 'phases and no tasks');
    // every caption is in the page's name for its level, and in the box of its own, which a pixel of the chart's units would not size
    for (const html of [noPhases, withTasks, noTasks]) assert.ok(!/class="tn-cap tn-lane-cap" style="top:[\d.]+px/.test(html));
    // The Phases caption is the glossary's `phases` term, which the page reaches nowhere else: the basis line's term is `tasks`. The
    // other captions are their names alone.
    const term = (html) => [...html.matchAll(/<span class="eyebrow"><span class="term" data-tip="([^"]*)">(\w+)<sup/g)].map(([, tip, name]) => [name, tip]);
    const gloss = G.GLOSS.phases.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll("'", '&#x27;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    assert.deepEqual(term(withTasks), [['Phases', gloss]]);
    assert.deepEqual(term(noTasks), [['Phases', gloss]]);
    assert.deepEqual(term(noPhases), [], 'a chart with no phases has no Phases caption');

    // The zoom note and the plot. With no phases they are siblings straight under the chart's box, the plot right after the note, so the
    // rule that wants the svg right after it (styles.css) does not reach, and the nav's own rule for this chart does. With phases they
    // stand in `.tn-strip`, with the basis line between them, and that chart's rule is the one for it.
    assert.match(noPhases, /<div class="zoomnote"><span class="hint">[^<]*<\/span><\/div><div class="tn-plot">/);
    assert.ok(!noPhases.includes('tn-strip') && !noPhases.includes('tn-basis'));
    assert.match(withTasks, /<div class="tn-strip"><div class="zoomnote">.*?<\/div><div class="tn-cap tn-basis">.*?<\/div><div class="tn-plot">/);
    const css = source('tabs/trajectories.css');
    assert.match(css, /\.tn > \.zoomnote:has\(\+ \.tn-plot svg\.strip:hover\) \.hint \{\s*visibility: visible;/);
    assert.match(css, /\.tn-strip:has\(svg\.strip:hover\) > \.zoomnote \.hint \{\s*visibility: visible;/);
  } finally {
    await ui.close();
  }
});

test("the three-level list is the phase list's rows under the tasks, and is worded where the page words everything else", () => {
  const list = source('tabs/TaskList.tsx');
  const phaseList = source('tabs/PhaseList.tsx');
  // a phase is one component, which the phase list exports and draws its own rows with, and the task list imports; so is the live edge
  assert.match(phaseList, /export function PhaseRow\b/);
  assert.match(phaseList, /<PhaseRow\b/, 'the phase list draws its rows by it');
  assert.match(list, /import \{[^}]*\bPhaseRow\b[^}]*\} from '\.\/PhaseList';/);
  assert.match(list, /<PhaseRow\b/);
  assert.match(phaseList, /export function EdgeRow\b/);
  assert.match(list, /<EdgeRow\b/);
  assert.equal(callsOf('tabs/PhaseList.tsx', 'zoomTip'), 1, "the click line is the row's, wherever the row is drawn");
  assert.equal(callsOf('tabs/TaskList.tsx', 'cutNote'), 1, "the cut call log's note is the one function's");
  // A task opens into the phases its row counts (`taskPhases`), never into the raw range of the block's list, which would list the idle
  // phase of the gap after it twice; and it says a call still running in it, and a cut log, by `taskState`, with no wording of its own.
  assert.equal(callsOf('tabs/TaskList.tsx', 'taskPhases'), 1);
  assert.doesNotMatch(list, /\.list\.slice\(/);
  assert.equal(callsOf('tabs/TaskList.tsx', 'openClause'), 0);
  assert.equal(callsOf('tabs/TaskList.tsx', 'incompleteFrom'), 0);
  // each row's words are the page's own, from the one place each is worded, and its click line is the one a phase row has
  for (const name of ['taskLabel', 'taskSource', 'taskWhat', 'taskState', 'gapLine', 'loneLine', 'taskNumbers', 'taskInView', 'commandLine']) {
    assert.ok(callsOf('tabs/TaskList.tsx', name) >= 1, `the list calls ${name}`);
  }
  assert.equal(callsOf('tabs/TaskList.tsx', 'zoomTip'), 1, 'the click line of a task row, and of a lone row, which is a row of the same button');
  // the button that shows a phase's calls in the tree is worded here, once, and is the one an open phase has
  assert.deepEqual(spelledIn('show its calls in the tree'), ['tabs/TaskList.tsx']);
  assert.match(list, /\{opened && <PhaseCalls p=\{p\} onPick=\{onPhase\} \/>\}/);
  assert.match(list, /className="linkish tl-tree" onClick=\{\(\) => onPick\(p\)\}/, 'it picks the phase, as a click on the phase does');
  // a click on a task's row picks the task and opens it; it is the zoom that opens a task by default, and a choice of the reader's wins.
  // The choice is kept by where the task begins (`t0`), and a phase's by where the phase begins, never by a place in the list: a session
  // running now is cut again with every read, and a new split or a merge moves the places after it.
  assert.match(list, /setChose\(\(m\) => new Map\(m\)\.set\(t\.t0, true\)\);\s*onTask\(t\);/);
  assert.match(list, /const open = chose\.get\(t\.t0\) \?\? i === held;/);
  assert.match(list, /const held = taskInView\(tasks, view\);/);
  // the chevrons open and close, and a row of a phase picks the phase, whatever task it is under
  assert.match(list, /<Tog open=\{open\} what=\{what\} onClick=\{\(\) => setChose\(\(m\) => new Map\(m\)\.set\(t\.t0, !open\)\)\} \/>/);
  assert.match(list, /const opened = seen\.has\(p\.t0\);/);
  assert.match(list, /onClick=\{\(\) => setSeen\(\(s\) => flipped\(s, p\.t0\)\)\}/);
  assert.doesNotMatch(list, /chose\.get\(i\)|\.set\(i,|seen\.has\(at\)|flipped\(s, at\)/, 'no state kept by a place in the list');
  assert.match(list, /<PhaseRow p=\{p\} onPick=\{onPhase\} \/>/);
});

test('the list of tasks as the page draws it for every row of the fixture: a row to each task, with its number, span, label, source, and what it holds, and the lines between them', async () => {
  const ui = await renderer();
  try {
    const seen = { gap: 0, alive: 0, lone: 0, quoted: 0, built: 0, open: 0, cut: 0 };
    const check = async (b, where) => {
      const html = await ui.render('tabs/TaskList.tsx', 'TaskList', { block: b, now: null, view: null, onTask() {}, onPhase() {} });
      // the children of the list, in order: a gap line, or a row to a task or a lone stretch (a row holds no other div)
      const parts = [...html.matchAll(/<div class="(tl-gap|tl-row[^"]*)"[^>]*>(.*?)<\/div>/g)].map(([, cls, inner]) => ({ cls, inner }));
      const nums = T.taskNumbers(b.tasks);
      const want = [];
      b.tasks.forEach((t, i) => {
        if (t.gap) want.push(['tl-gap', T.gapLine(t.gap)]);
        const state = T.taskState(t, b).join(' · ');
        const src = T.taskSource(t);
        // closed, as the page first shows it: a chevron, then the row's words
        want.push(
          t.kind === 'lone'
            ? ['tl-row lone', `▸${T.loneLine(t)}${state}`]
            : ['tl-row', `▸${nums[i]}${F.clock(t.t0)}–${F.clock(t.t1)}${T.taskLabel(t)}${src ? ` · ${src}` : ''}${T.taskWhat(t, b)}${state}`],
        );
        seen.gap += t.gap ? 1 : 0;
        seen.alive += t.gap?.alive.length ? 1 : 0;
        seen.lone += t.kind === 'lone' ? 1 : 0;
        seen.quoted += t.kind === 'task' && src ? 1 : 0;
        seen.built += t.kind === 'task' && !src ? 1 : 0;
        seen.open += t.open ? 1 : 0;
        seen.cut += t.incomplete ? 1 : 0;
      });
      assert.deepEqual(parts.map(({ cls, inner }) => [cls, textOf(inner)]), want, where);
      assert.ok(!/ open"/.test(html) && !html.includes('tl-phases'), `${where}: a row is open where the chart is not zoomed`);
      // a gap and a lone stretch are lines of a note; a task row has a number, and a lone stretch none
      assert.equal((html.match(/class="tl-num"/g) ?? []).length, b.tasks.filter((t) => t.kind === 'task').length, where);
      assert.equal((html.match(/class="tl-gap"/g) ?? []).length, b.tasks.filter((t) => t.gap).length, where);
      // a quoted label is in quotation marks and names its source; a built label has neither
      b.tasks.forEach((t, i) => {
        if (t.kind !== 'task') return;
        const row = parts.find((q) => q.cls === 'tl-row' && q.inner.includes(`<span class="tl-num">${nums[i]}</span>`)).inner;
        assert.equal(row.includes('class="tl-src"'), t.source !== null, `${where} ${t.label}`);
        assert.equal(/[“”]/.test(textOf(row)), t.source !== null, `${where} ${t.label}`);
      });
      // a call still running in a task, and a cut call log, are said under its label, in a muted line of their own
      assert.equal((html.match(/class="tl-state"/g) ?? []).length, b.tasks.filter((t) => T.taskState(t, b).length).length, where);
      // the note under the rows, for a log that was cut
      assert.equal(html.includes('class="pl-note"'), P.cutNote(b) !== null, where);
      if (P.cutNote(b)) assert.ok(html.includes(`<div class="pl-note">${P.cutNote(b)}</div>`), where);
      // A chevron is named for what it opens, once: `aria-expanded` says whether it is open, so its name does not say it too
      const togs = [...html.matchAll(/<div class="tl-row[^"]*" data-i="\d+"><button type="button" class="tl-tog" aria-expanded="(true|false)" aria-label="([^"]*)">(.)<\/button>/g)].map(([, expanded, label, glyph]) => [expanded, label, glyph]);
      assert.deepEqual(togs, b.tasks.map((t, i) => ['false', t.kind === 'lone' ? `the lone stretch at ${F.clock(t.t0)}` : `task ${nums[i]}`, '▸']), where);
      assert.doesNotMatch(html, /aria-label="(?:collapse|expand)/, where);
    };
    for (const r of blocks) await check(r.phases, r.sid);
    // A lone stretch ahead of the tasks, and one among them, takes no number and gives none up. No row of the fixture has one before a task.
    const long = blocks.find((r) => r.phases.tasks.length >= 3).phases;
    const kinds = (...ks) => ({ ...long, tasks: long.tasks.map((t, i) => ({ ...t, kind: ks[i] ?? t.kind })) });
    await check(kinds('lone'), 'a lone stretch first');
    await check(kinds('task', 'lone', 'task'), 'a lone stretch between two tasks');
    // the fixture drew each kind of row
    for (const [what, n] of Object.entries(seen)) assert.ok(n >= 1, `no fixture row has ${what}: ${JSON.stringify(seen)}`);
  } finally {
    await ui.close();
  }
});

test('the task that holds the zoom is open into the phases its row counts, drawn as the phase list draws them, and none is open where the chart is not zoomed or the zoom is in a gap', async () => {
  const ui = await renderer();
  try {
    const props = (b, view) => ({ block: b, now: null, view, onTask() {}, onPhase() {} });
    let gaps = 0;
    for (const r of blocks) {
      const b = r.phases;
      // the phase list's own rows, by the phase's place in the block's list
      const all = await ui.render('tabs/PhaseList.tsx', 'PhaseList', { block: b, now: null, onPick() {} });
      const rowsOfList = [...all.matchAll(/<button type="button" class="pl-row".*?<\/button>/g)].map((m) => m[0]);
      assert.equal(rowsOfList.length, b.list.length, r.sid);
      for (const [k, t] of b.tasks.entries()) {
        const where = `${r.sid} task ${k}`;
        // the zoom a pick of the task makes: its own span, and the chart's narrowest at the least
        const html = await ui.render('tabs/TaskList.tsx', 'TaskList', props(b, [t.t0, Math.max(t.t1, t.t0 + 1)]));
        const open = [...html.matchAll(/<div class="tl-row[^"]* open" data-i="(\d+)">/g)].map((m) => Number(m[1]));
        assert.deepEqual(open, [k], where);
        // its chevron has the name it has closed, and is open by `aria-expanded`; a phase's has the phase's name, and is closed
        const nums = T.taskNumbers(b.tasks);
        const name = t.kind === 'lone' ? `the lone stretch at ${F.clock(t.t0)}` : `task ${nums[k]}`;
        assert.ok(html.includes(`<div class="tl-row${t.kind === 'lone' ? ' lone' : ''} open" data-i="${k}"><button type="button" class="tl-tog" aria-expanded="true" aria-label="${name}">▾</button>`), where);
        const phaseTogs = [...html.matchAll(/<div class="tl-ph">(?:<button type="button" class="tl-tog" aria-expanded="(true|false)" aria-label="([^"]*)">▸<\/button>|<span class="tl-tog none" aria-hidden="true"><\/span>)<button type="button" class="pl-row"/g)].map(([, expanded, label]) => (label === undefined ? null : [expanded, label]));
        assert.deepEqual(phaseTogs, T.taskPhases(t, b).map((p) => (p.n > 0 ? ['false', `${P.phaseLbl(p.kind)} ${F.clock(p.t0)}`] : null)), `${where}: the phases' chevrons`);
        assert.doesNotMatch(html, /aria-label="(?:collapse|expand)/, where);
        // its phases are the ones its row counts, in order, each the row the phase list draws for it
        const mine = [...html.matchAll(/<div class="tl-ph">.*?(<button type="button" class="pl-row".*?<\/button>)/g)].map((m) => m[1]);
        const counted = T.taskPhases(t, b);
        assert.deepEqual(mine, counted.map((_, j) => rowsOfList[t.phases[0] + j]), where);
        assert.equal(counted.length, Number(/^(\d[\d,]*) phase/.exec(T.taskWhat(t, b))[1].replaceAll(',', '')), `${where}: what its row counts`);
        gaps += t.gap && k ? 1 : 0;
      }
      // none where the chart is not zoomed, nor in a gap, nor before the first task or after the last
      const first = b.tasks[0];
      const last = b.tasks.at(-1);
      const cuts = b.tasks.map((t, k) => (k && t.gap ? [b.tasks[k - 1].t1 + 1, t.t0 - 1] : null)).filter(Boolean);
      for (const view of [null, [first.t0 - 500, first.t0 - 100], [last.t1 + 10, last.t1 + 500], ...cuts]) {
        const html = await ui.render('tabs/TaskList.tsx', 'TaskList', props(b, view));
        assert.ok(!/ open"/.test(html) && !html.includes('tl-phases'), `${r.sid}: open for ${JSON.stringify(view)}`);
      }
    }
    assert.ok(gaps >= 1, 'a task after a gap, whose row does not count the idle phase of that gap');
  } finally {
    await ui.close();
  }
});

test("the list ends in the session's live edge, as the phase list does, and what is under an open phase is its commands and the button for the tree", async () => {
  const ui = await renderer();
  mock.timers.enable({ apis: ['Date'], now: api.now_epoch * 1000 });
  try {
    let edges = 0;
    for (const r of blocks) {
      const b = r.phases;
      for (const now of r.running ? [r.running.now] : [null]) {
        const tasks = await ui.render('tabs/TaskList.tsx', 'TaskList', { block: b, now, view: null, onTask() {}, onPhase() {} });
        const phases = await ui.render('tabs/PhaseList.tsx', 'PhaseList', { block: b, now, onPick() {} });
        const edge = (html) => /<div class="pl-row edge".*?<\/div>/.exec(html)?.[0] ?? null;
        assert.equal(edge(tasks), edge(phases), r.sid);
        if (now) {
          edges++;
          assert.ok(edge(tasks), `${r.sid}: a session running now ends in its live edge`);
          // after the last row
          assert.ok(tasks.indexOf('pl-row edge') > tasks.lastIndexOf('class="tl-row'), r.sid);
        }
      }
      // what is under an open phase: its commands, as the phase's line says them, a line each, and the button
      for (const p of b.list) {
        const html = await ui.render('tabs/TaskList.tsx', 'PhaseCalls', { p, onPick() {} });
        const lines = [...html.matchAll(/<div class="tl-cmd" data-tip="([^"]*)" data-tip-wide="true">(.*?)<\/div>/g)].map(([, tip, line]) => [textOf(tip), textOf(line)]);
        assert.deepEqual(lines, p.top.map((c) => [c[0], P.commandLine(c)]), `${r.sid} ${p.kind} ${p.t0}`);
        assert.equal((html.match(/<button type="button" class="linkish tl-tree">show its calls in the tree<\/button>/g) ?? []).length, 1, r.sid);
      }
    }
    assert.ok(edges >= 1, 'the fixture has a session running now');
  } finally {
    mock.timers.reset();
    await ui.close();
  }
});

test("a lone stretch's line, and the words under a task's label, are text in a row to click and clear 4.5 to 1, and a gap line, which is no click, stays muted: all are quieter than a task's label", () => {
  const css = source('tabs/trajectories.css');
  const rule = (selector) => new RegExp(`(?:^|\\n)${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{([^}]*)\\}`).exec(css)?.[1] ?? '';
  const colour = (selector) => /(?:^|[\s;])color:\s*var\(--([a-z0-9-]+)\)/.exec(rule(selector))?.[1];
  // the contrast of two of the page's own colours (WCAG), from the tokens of styles.css
  const root = source('styles.css');
  const token = (name) => /(#[0-9a-f]{6})/i.exec(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i').exec(root)?.[1] ?? '')?.[1];
  const lin = (c) => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const lum = (hex) => { const n = parseInt(hex.slice(1), 16); return 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255); };
  const contrast = (fg, bg) => { const [hi, lo] = [lum(token(fg)), lum(token(bg))].sort((a, b) => b - a); return (hi + 0.05) / (lo + 0.05); };

  // A lone stretch's line is a row to click, so its text is interactive and needs 4.5 to 1 on the page's surface and on the shade the
  // row takes under the pointer; `--muted` is 3.82 to 1 on the surface at this size, and `--ink-2` 8.98
  assert.equal(colour('.tl-line'), 'ink-2');
  for (const bg of ['surface', 'surface-2']) assert.ok(contrast(colour('.tl-line'), bg) >= 4.5, `the lone line on ${bg}: ${contrast(colour('.tl-line'), bg)}`);
  assert.equal(contrast('muted', 'surface').toFixed(2), '3.82', 'the number the stylesheet says');
  assert.equal(contrast('ink-2', 'surface').toFixed(2), '8.98', 'and the other');
  // The words of a task's row beside and under its label, where its words came from and what it says of a call still running or a cut log,
  // are inside the row's button as the label is, so they are interactive text too, and are `--ink-2` on both
  for (const selector of ['.tl-src', '.tl-state']) {
    assert.equal(colour(selector), 'ink-2', selector);
    for (const bg of ['surface', 'surface-2']) assert.ok(contrast(colour(selector), bg) >= 4.5, `${selector} on ${bg}: ${contrast(colour(selector), bg)}`);
  }
  // a gap line is a note with no click, and stays muted
  assert.equal(colour('.tl-gap'), 'muted');
  // both are quieter than the label of a task's row: the gap line the most
  assert.equal(colour('.tl-label'), 'ink');
  assert.ok(contrast('ink', 'surface') > contrast(colour('.tl-line'), 'surface') && contrast(colour('.tl-line'), 'surface') > contrast(colour('.tl-gap'), 'surface'));
  // and the hover of a lone row has nothing of its own to say about the colour: it is the one the line has
  assert.doesNotMatch(css, /\.tl-row\.lone:hover \.tl-line/);
});

test("the inspector shows the three-level list where the block has tasks and the phase list where it has none, and sends a pick held by another day's row there", () => {
  const ins = source('tabs/TrajectoryInspector.tsx');
  assert.match(ins, /import \{ TaskList \} from '\.\/TaskList';/);
  assert.match(ins, /block\.tasks\?\.length \? \(\s*<TaskList\b[\s\S]*?\/>\s*\) : \(\s*<PhaseList\b[\s\S]*?\/>\s*\)/, 'tasks, else the phases');
  const el = /<TaskList\b[\s\S]*?\/>/.exec(ins)?.[0] ?? '';
  for (const prop of ['block={block}', 'now={edge}', 'view={view ?? aim}', 'onTask={onTask}', 'onPhase={onPhase}']) assert.ok(el.includes(prop), `TaskList gets ${prop}`);
  // The chart's pick and the list's are one: a task or a phase held by another day's row goes there (`otherDay`), the rest zoom here
  assert.equal(callsOf('tabs/TrajectoryInspector.tsx', 'otherDay'), 1, 'one place decides');
  assert.match(ins, /const onPhase = \(p: Phase\) =>\s*pick\(/);
  assert.match(ins, /const onTask = \(t: Task\) =>\s*pick\(/);
  assert.match(ins, /zoom: \[p\.t0, p\.t1\],\s*\},\s*p,?\s*\)/, 'a phase says its own sid');
  assert.match(ins, /zoom: \[t\.t0, Math\.max\(t\.t1, t\.t0 \+ SPAN_MIN\)\],\s*\},\s*t,?\s*\)/, 'and so does a task');
  // the jump to another row, and the one this row was opened for: gone to once its read is in, by the way every pick goes
  assert.match(ins, /onJump\?: \(day: PhaseDay, t0: number, t1: number, what\?: string\) => boolean;/, 'the tab says whether it made the jump');
  assert.match(ins, /pendingJump\?: DayJump \| null;/);
  assert.match(ins, /export interface DayJump \{\s*sid: string;\s*t0: number;\s*t1: number;\s*what\?: string;\s*\}/);
  assert.match(ins, /const jumpHere = pendingJump && pendingJump\.sid === sid \? pendingJump : null;/, 'no row takes the jump of another');
  assert.match(ins, /setWant\(\{\s*t: jumpHere\.t0,\s*by: 'start',[\s\S]*?zoom: \[jumpHere\.t0, jumpHere\.t1\],\s*\}\);/, 'to its start, zoomed to its stretch');
  // It goes once, because the effect is keyed on the jump alone. Were the read (`d`, `tree`) or the want among its dependencies, it would
  // go again after every read that arrives, and every want that is made or cleared, and put the chart back on the jumped stretch.
  const goes = /const jumpHere = [^;]*;\s*useEffect\(\(\) => \{\s*if \(!jumpHere\) return;[\s\S]*?\}, (\[[^\]]*\])\);/.exec(ins);
  assert.ok(goes, 'the effect that goes to the jump');
  assert.equal(goes[1], '[jumpHere]', 'keyed on the jump alone');
  // the tab is handed the other row as the block's `days` lists it, with the span that says whether the window holds it
  assert.match(
    ins,
    /const day = away \? block\?\.days\?\.find\(\(x\) => x\.sid === away\) : undefined;\s*if \(day && onJump\?\.\(day, w\.t, w\.zoom\[1\], w\.what\)\) return;\s*setWant\(w\);/,
    'a jump the tab does not make is a pick on this row',
  );
});

test("a day row is in the window when its last record is at or after the window's start, ranked in the table or not", () => {
  const NOW = 1_789_400_000_000; // the service's present, in ms
  const from = T.windowStart(1440, NOW);
  assert.equal(from, NOW / 1000 - 86_400);
  for (const m of [null, undefined, 0, -5, Number.NaN]) assert.equal(T.windowStart(m, NOW), null, String(m));
  // The fixture's agent over two days: its block lists both rows, and the table's own ranks need not hold either of them. A pick on the
  // other day's task is a jump to that row whenever the window holds it, by its span alone.
  const pair = blocks.find((r) => r.phases.days)?.phases.days;
  assert.equal(pair?.length, 2);
  const [first, second] = pair;
  assert.ok(T.dayInWindow(first, first.last - 1) && T.dayInWindow(first, first.last), 'its last record at or after the start: in the window');
  assert.ok(T.dayInWindow(second, T.windowStart(1440, second.last * 1000 + 3_600_000)), 'a day of a 24 h window an hour later');
  // a row whose last record is before the window's start is not held: the pick stays on the open row, as a zoom there
  assert.equal(T.dayInWindow(first, first.last + 0.001), false);
  assert.equal(T.dayInWindow(first, T.windowStart(60, first.last * 1000 + 3_600_001)), false, 'a 1 h window that begins after it');
  // and where the window's length is not known, no row is known to be in it
  assert.equal(T.dayInWindow(first, null), false);
});

test("a zoom the chart cannot make is the list's aim until the reader zooms the chart or resets it, or a new read begins: it is not the jump's, which lasts as long as its row", () => {
  const ins = source('tabs/TrajectoryInspector.tsx');
  // A pick's zoom as wide as the read is none (`fitView` gives null), as a task as long as the read is. Then the stretch it asked for
  // stands in for the zoom in the list, which opens the task that holds it. That is state of the inspector's own, and not worked out from
  // the pending jump, which the tab keeps for as long as the row is open, so a reset of the zoom would open the jumped task again.
  assert.match(ins, /const \[aim, setAim\] = useState<\[number, number\] \| null>\(null\);/);
  assert.doesNotMatch(ins, /const aim:/, 'not worked out from the pending jump');
  assert.match(ins, /const zoom = fitView\(w\.zoom\);\s*setView\(zoom\);\s*setAim\(zoom \? null : w\.zoom\);/, 'set only where the chart cannot zoom to the pick');
  // let go where the reader zooms the chart, or resets it: every zoom the chart makes of its own goes through `zoomTo`
  assert.match(ins, /const zoomTo = \(v: \[number, number\] \| null\) => \{\s*setAim\(null\);\s*setView\(v\);\s*\};/);
  assert.match(ins, /<TrajectoryNav[\s\S]*?setView=\{zoomTo\}/, 'the chart zooms through it');
  // and where a new read begins, which is a new axis: nothing from the last read applies
  const reading = /const stampKey =[\s\S]*?\}, \[stampKey\]\);/.exec(ins)?.[0] ?? '';
  assert.match(reading, /setView\(null\);\s*setAim\(null\);/);
  // the list takes the chart's zoom, and the aim only where the chart has none
  assert.match(ins, /view=\{view \?\? aim\}/);
});

test("the tab opens the other day's row for a jump and hands that row the jump, which it takes once it has read itself", () => {
  const tab = source('tabs/TrajectoriesTab.tsx');
  assert.match(tab, /import type \{ DayJump, RunJump \} from '\.\/TrajectoryInspector';/);
  assert.match(tab, /useState<DayJump \| null>\(null\)/);
  // the inspector is keyed on its session, so the other row is a new inspector, which takes the jump as it starts
  assert.match(tab, /<TrajectoryInspector\s+key=\{sel\}/);
  assert.match(tab, /onJump=\{jumpTo\}/);
  assert.match(tab, /pendingJump=\{pending\?\.sid === sel \? pending : null\}/, 'only the row it is for');
  const jumpTo = /const jumpTo = [\s\S]*?\n {2}\};/.exec(tab)?.[0] ?? '';
  assert.match(jumpTo, /setPending\(\{ sid, t0, t1, what \}\)/);
  assert.match(jumpTo, /select\(sid\)/);
  assert.doesNotMatch(jumpTo, /\bopen\(/, 'it picks the row itself: `open` drops a jump');
  // A day row the window holds opens, whether the table ranks it or not: the window holds it when its own last record is at or after the
  // window's start (`dayInWindow`), and the table's rows are not asked. One the window does not hold opens as the panel that says it is not
  // in this window, so the tab makes no jump to it, says so, and the inspector makes the pick on the row it has.
  assert.match(
    jumpTo,
    /^const jumpTo = \(day: PhaseDay, t0: number, t1: number, what\?: string\): boolean => \{\s*if \(!dayInWindow\(day, windowStart\(t\?\.window\?\.minutes, serverNow\(\)\)\)\) return false;\s*const \{ sid \} = day;\s*setJump\(null\);/,
    'no row in the window, no jump, before anything is changed',
  );
  assert.doesNotMatch(jumpTo, /rows\[/, 'whether the table ranks the row is not asked');
  assert.match(jumpTo, /select\(sid\);\s*return true;\s*\};$/, 'and a jump that is made says so');
  // a jump is for the row it names, and lasts only while that row is the open one; any other way to a row drops it
  assert.match(tab, /if \(pending && pending\.sid !== sel\) setPending\(null\);/);
  assert.match(/const open = [\s\S]*?\n {2}\};/.exec(tab)?.[0] ?? '', /setPending\(null\)/);
});

test("a band's place on an axis: cut at its edges, never thinner than a pixel, parted from the next, and kept inside", () => {
  // the one rule for the session row's ribbon, the inspector's chart, and the task lane
  assert.deepEqual([P.MIN_W, P.GAP, P.GAP_OVER], [1, 0.6, 3]);
  // an axis of 1,024 s drawn 1,024 px wide, a pixel a second, so that the sums are exact
  const END = 1024;
  const at = (a, b, axis = [0, END], W = END, gap = 0) => P.placeBand(a, b, axis, W, gap);
  const is = (got, want, what) => assert.ok(got.length === want.length && got.every((v, k) => near(v, want[k])), `${what}: ${got} is not ${want}`);
  is(at(100, 300), [100, 200], 'a pixel a second');
  is(at(150, 250, [100, 300], 400), [100, 200], 'a zoomed axis');
  // parted from the next by `GAP`, where it is wider than `GAP_OVER`
  is(at(100, 300, [0, END], END, P.GAP), [100, 199.4], 'parted');
  is(at(100, 103, [0, END], END, P.GAP), [100, 3], 'a stretch of 3 px is not wider than 3');
  is(at(100, 103.5, [0, END], END, P.GAP), [100, 2.9], 'a stretch wider than 3 px is parted');
  // never thinner than a pixel, and a stretch of no length is a pixel where it sits
  is(at(100, 100), [100, 1], 'no length');
  is(at(100, 100.4), [100, 1], 'a short one');
  is(at(100, 300, [0, END], END, 250), [100, 1], 'a gap larger than the stretch');
  // cut at the edges of the axis: before it, after it, and across both
  is(at(-50, 50), [0, 50], 'begins before the axis');
  is(at(950, 2000), [950, END - 950], 'ends after the axis');
  is(at(-50, 2000), [0, END], 'across both edges');
  // kept inside: a stretch at, or past, the end of the axis is a pixel against it; one before the start is a pixel at it
  is(at(END, END), [END - 1, 1], 'at the end');
  is(at(2000, 3000), [END - 1, 1], 'past the end');
  is(at(-300, -200), [0, 1], 'before the start');
  // an axis of no length does not divide by zero, and an axis drawn narrower than a pixel has no x below 0
  is(at(5, 5, [5, 5]), [0, 1], 'an axis of no length');
  is(at(0, 1000, [0, END], 0.5), [0, 1], 'under a pixel wide');
  is(at(0, 1000, [0, END], 0), [0, 1], 'no width');
});

/** The trajectories tab as the page draws it, for the fixture's rows, with the rows in `changed` (by sid) in place of its own: each row
 *  of its list, by sid, with the line under its ribbon (null where it has none), its class and its hover (`tip`, and whether the hover
 *  is the wide one), and what the ribbon draws, in px along its band: the left edge of each of its bands, and each tick. */
async function drawnTable(ui, changed = {}) {
  const data = { ...api.trajectories, rows: { ...api.trajectories.rows, ...changed } };
  const html = await ui.render('tabs/TrajectoriesTab.tsx', 'TrajectoriesTab', {
    traj: { data, error: null, loading: false, reload() {} },
    rank: 'recent',
    setRank() {},
    cls: ['agent', 'human', 'ide'],
    win: null,
    sel: null,
    select() {},
    tfrom: null,
    setTfrom() {},
  });
  const items = [...html.matchAll(/<button[^>]*\bdata-sid="([^"]*)"[^>]*>([\s\S]*?)<\/button>/g)];
  return new Map(
    items.map(([, sid, inner]) => {
      const seq = /<span class="(tj-seq[^"]*)"([^>]*)>([\s\S]*?)<\/span>/.exec(inner);
      return [
        sid,
        {
          line: seq ? textOf(seq[3]) : null,
          cls: seq ? seq[1] : null,
          tip: seq && /\sdata-tip="([^"]*)"/.test(seq[2]) ? textOf(/\sdata-tip="([^"]*)"/.exec(seq[2])[1]) : null,
          wide: seq ? /\sdata-tip-wide\b/.test(seq[2]) : false,
          bands: [...inner.matchAll(/<g data-kind="(\w+)"[^>]*><rect x="([\d.e+-]+)"/g)].map((m) => ({ kind: m[1], x: Number(m[2]) })),
          ticks: [...inner.matchAll(/<line class="ph-tick" x1="([\d.e+-]+)"/g)].map((m) => Number(m[1])),
        },
      ];
    }),
  );
}

test("a session row's line lists its tasks, and its ribbon ticks where each begins; a row with no tasks keeps the order of its phases, and a row with one span has no tick", async () => {
  const ui = await renderer();
  try {
    const edge = (r) => r.running?.now ?? null;

    // every row of the fixture, as the service sends it
    const as = await drawnTable(ui);
    for (const r of rows) {
      const got = as.get(r.sid);
      assert.ok(got, `${r.sid}: no row in the list`);
      if (!P.isBlock(r.phases)) {
        // a person, an IDE, and an agent whose commands are not read have no band: no line under it and no tick on it
        assert.equal(got.line, null, r.sid);
        assert.deepEqual(got.ticks, [], r.sid);
        continue;
      }
      const b = r.phases;
      assert.equal(got.line, T.taskSequence(b), `${r.sid}: the line is its tasks`);
      assert.match(got.line, /^\S/, `${r.sid}: a line`);
      // the whole of it is its hover, in the wide box that a long line needs
      assert.equal(got.tip, got.line, `${r.sid}: the hover holds the whole line`);
      assert.ok(got.wide, `${r.sid}: the wide hover`);
      // a tick for each span after the first, task or lone stretch: at the left edge of the band its first call begins, where the span begins
      assert.equal(got.ticks.length, b.tasks.length - 1, `${r.sid}: a tick for each span after the first`);
      b.tasks.slice(1).forEach((t, k) => {
        assert.ok(near(got.ticks[k], got.bands[t.phases[0]].x), `${r.sid} span ${k + 1}: the tick ${got.ticks[k]} is not at the band ${got.bands[t.phases[0]].x}`);
      });
    }
    // the fixture has a lone stretch and a cutting gap among the spans a row ticks, and a row that names two tasks and counts the rest
    assert.ok(blocks.some((r) => r.phases.tasks.some((t) => t.kind === 'lone')) && blocks.some((r) => r.phases.tasks.some((t) => t.gap)));
    assert.ok([...as.values()].some((g) => /→ \d+ more$/.test(g.line ?? '')), 'a row with more tasks than its line names');

    // an agent over two days is on two rows that serve the one block: the same line and the same number of ticks on both
    const [d1, d2] = blocks.filter((r) => r.phases.days);
    assert.equal(as.get(d1.sid).line, as.get(d2.sid).line);
    assert.equal(as.get(d1.sid).ticks.length, as.get(d2.sid).ticks.length);

    // a row whose block has no task to name keeps the line it had: its phases in the order they came, ending in the live edge for a
    // session running now. A row that is not running, and one that is.
    for (const r of [blocks.find((r) => !r.running && r.phases.tasks.length >= 3), blocks.find((r) => r.running?.now?.state === 'running')]) {
      const b = r.phases;
      const { tasks, headline, days, ...first } = b;
      const wants = {
        'a block of the first version, which has no tasks': [{ ...first, v: 1 }, 0],
        'a block with an empty list of tasks': [{ ...b, tasks: [] }, 0],
        // the service sends none, since every session with calls has a task; it names none, so the line is not left empty
        'a block of lone stretches alone': [{ ...b, tasks: tasks.map((t) => ({ ...t, kind: 'lone' })) }, tasks.length - 1],
      };
      for (const [what, [block, ticks]] of Object.entries(wants)) {
        const got = (await drawnTable(ui, { [r.sid]: { ...r, phases: block } })).get(r.sid);
        assert.equal(got.line, P.sequenceLine(b.list, edge(r)), `${r.sid}, ${what}`);
        assert.ok(got.line.length > 0 && got.line !== T.taskSequence(b), `${r.sid}, ${what}: the order of the phases`);
        // it is the same element, so its hover holds the whole line too
        assert.equal(got.tip, got.line, `${r.sid}, ${what}: the hover holds the whole line`);
        assert.equal(got.ticks.length, ticks, `${r.sid}, ${what}: ticks`);
      }
      // one span is the whole session, so no boundary lies in it: its label, with no arrow, and no tick
      const got = (await drawnTable(ui, { [r.sid]: { ...r, phases: { ...b, tasks: tasks.slice(0, 1) } } })).get(r.sid);
      assert.equal(got.line, T.taskLabel(tasks[0]), `${r.sid}, one span`);
      assert.deepEqual(got.ticks, [], `${r.sid}, one span`);
    }
    assert.ok(blocks.some((r) => r.running?.now?.state === 'running') && blocks.some((r) => !r.running), 'the fixture has a row that runs now, and one that does not');
  } finally {
    await ui.close();
  }
});

test("a row of two long quoted labels has both whole in its line and in its hover, and the line's class has the CSS that clamps it to two lines", async () => {
  const ui = await renderer();
  try {
    // This reads the markup and the stylesheet. No height is measured here: the clamp is the browser's work, and node has no layout.
    // Two quotes as long as the service sends one (`WORDS_CHARS`: 120 characters, the last `…` where the line was cut).
    const long = (head) => `${head} ${'and then a good deal more of the same '.repeat(5)}`.slice(0, 119) + '…';
    const r = blocks.find((x) => !x.running && x.phases.tasks.filter((t) => t.kind === 'task').length >= 3);
    const b = r.phases;
    const tasks = b.tasks.map((t, i) => (i < 2 ? { ...t, kind: 'task', source: 'commit', ref: '', label: long(`Task ${i + 1}: rework the loader`) } : t));
    const block = { ...b, tasks };
    const got = (await drawnTable(ui, { [r.sid]: { ...r, phases: block } })).get(r.sid);

    // the line is the whole of both quotes and the count of the rest: nothing is cut in the markup, so the page reads all of it
    assert.equal(got.line, T.taskSequence(block));
    assert.ok(tasks.slice(0, 2).every((t) => t.label.length === 120 && got.line.includes(T.taskLabel(t))), 'both quotes, in full');
    assert.ok(got.line.length > 240, `${got.line.length} characters`);
    // its hover holds that whole line, in the page's own tooltip (`data-tip`, which a child's takes over from the row's), and in its wide box
    assert.equal(got.tip, got.line);
    assert.ok(got.wide);

    // and what is on screen is two lines of it with an ellipsis: the line's class clamps it, in the box that makes the clamp work
    assert.equal(got.cls, 'tj-seq');
    const css = source('tabs/trajectories.css');
    const rule = new RegExp(`(?:^|\\n)\\.${got.cls} \\{([^}]*)\\}`).exec(css)?.[1] ?? '';
    assert.match(rule, /display:\s*-webkit-box;/, 'a vertical box');
    assert.match(rule, /-webkit-box-orient:\s*vertical;/);
    assert.match(rule, /-webkit-line-clamp:\s*2;/, 'two lines');
    assert.match(rule, /(?:^|[\s;])line-clamp:\s*2;/, 'and the standard property for it');
    assert.match(rule, /overflow:\s*hidden;/, 'the rest is cut off');
    // the old line of the phases is the same element, so it is clamped alike
    const phases = (await drawnTable(ui, { [r.sid]: { ...r, phases: { ...b, tasks: [] } } })).get(r.sid);
    assert.equal(phases.cls, got.cls);
    assert.equal(phases.tip, phases.line);
  } finally {
    await ui.close();
  }
});

test('a ribbon ticks where each span after the first begins, which across a cutting gap is where its first call starts; none for one span, for no tasks, or off its axis', async () => {
  const ui = await renderer();
  try {
    const W = 1000;
    const draw = (props) => ui.render('components/PhaseBand.tsx', 'PhaseBand', { W, H: 7, t0: 0, t1: 4100, ...props });
    const ticksOf = (html) =>
      [...html.matchAll(/<line class="ph-tick"([^>]*)>/g)].map(([, attrs]) =>
        Object.fromEntries([...attrs.matchAll(/(\w+)="([^"]*)"/g)].map(([, k, v]) => [k, Number(v)])),
      );
    const xs = async (props) => ticksOf(await draw(props)).map((t) => t.x1);
    const same = (got, want, what) => assert.ok(got.length === want.length && got.every((v, k) => near(v, want[k])), `${what}: ${got} is not ${want}`);
    /** where a time is on the axis [0, 4100], drawn `W` px wide */
    const at = (t) => (t / 4100) * W;

    // A cutting gap, the idle hour from 100 to 4000, lies between two tasks: the tick is at the second's first call, the end of the gap, and not at the
    // first's end. It stands across the band, from its top to its bottom, whatever the band's height.
    const list = [ph('explore', 0, 100), ph('idle', 100, 4000), ph('test', 4000, 4100)];
    const tasks = [task('First', 0, 100, { phases: [0, 2] }), task('Second', 4000, 4100, { phases: [2, 3], gap: { s: 3900, alive: [] } })];
    const gap = ticksOf(await draw({ list, tasks }));
    assert.equal(gap.length, 1);
    assert.ok(near(gap[0].x1, at(4000)) && near(gap[0].x2, at(4000)), `at ${gap[0].x1}, and not at ${at(100)}`);
    assert.deepEqual([gap[0].y1, gap[0].y2], [0, 7]);
    assert.deepEqual(ticksOf(await draw({ list, tasks, H: 12 })).map((t) => [t.y1, t.y2]), [[0, 12]]);

    // every span after the first has one, a lone stretch as a task: the boundaries at 1,000 and at 3,000, and none at the first span's start
    const long = [ph('explore', 0, 1000), ph('git', 1000, 1010), ph('idle', 1010, 3000), ph('test', 3000, 4100)];
    const three = [task('A', 0, 1000, { phases: [0, 1] }), task('read x', 1000, 1010, { kind: 'lone', phases: [1, 3] }), task('B', 3000, 4100, { phases: [3, 4] })];
    same(await xs({ list: long, tasks: three }), [at(1000), at(3000)], 'a lone stretch is a span');
    // a band can begin before its first task does (the session's first record can come before its first call), and the first span is still no boundary
    same(await xs({ list: long, tasks: three, t0: -100 }), [(1100 / 4200) * W, (3100 / 4200) * W], 'none at the first span, which begins inside the axis');

    // one span has no boundary in it, and nor has a ribbon with no tasks, or with none given: the ribbon is as it was
    for (const given of [{ tasks: three.slice(0, 1) }, { tasks: [] }, {}]) same(await xs({ list: long, ...given }), [], JSON.stringify(Object.keys(given)));
    assert.equal(await draw({ list: long, tasks: [] }), await draw({ list: long }), 'no tasks draws what no `tasks` draws');

    // a boundary is a tick where it lies inside the axis. One at its edge is no boundary, and one outside it is not drawn: a ribbon drawn over a
    // part of the session has the ticks of the part
    const edges = [task('A', 0, 1000), task('B', 1000, 3000), task('C', 3000, 4100), task('D', 4100, 4200)];
    same(await xs({ list: long, tasks: edges, t0: 1000, t1: 4100 }), [(2000 / 3100) * W], 'the boundaries at the axis start and at its end are none');
    same(await xs({ list: long, tasks: three, t0: 0, t1: 2000 }), [(1000 / 2000) * W], 'the one at 3,000 is off an axis that ends at 2,000');

    // it is drawn over the bands, so a band's hover is its own, and the ribbon says the phases it always said
    const html = await draw({ list, tasks });
    assert.ok(html.indexOf('class="ph-tick"') > html.lastIndexOf('data-kind='), 'the tick is drawn after the bands');
    assert.ok(html.includes(`aria-label="phases: ${P.sequenceLine(list)}"`));
  } finally {
    await ui.close();
  }
});

test('a tick is a crisp 1 px line in `--ink-2`, dark against the pale part between two bands, and it takes no pointer', () => {
  const css = source('tabs/trajectories.css');
  const tick = /(?:^|\n)\.ph-tick \{([^}]*)\}/.exec(css)?.[1] ?? '';
  assert.match(tick, /(?:^|[\s;])stroke:\s*var\(--ink-2\);/, 'in `--ink-2`, the ink of the inspector\'s task lane: no new hue');
  // 1 px is a device pixel at density 1 and two at density 2. A stroke under a pixel is a faint blur at density 1, and `--muted` is lost over the bands.
  assert.equal(Number(/stroke-width:\s*([\d.]+);/.exec(tick)?.[1]), 1, 'one pixel wide');
  assert.match(tick, /shape-rendering:\s*crispEdges;/, 'with crisp edges, so it is whole pixels and not a blur across two');
  assert.match(tick, /pointer-events:\s*none;/, 'the band under it keeps its hover');
});
