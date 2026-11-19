/* A self-check that the process diagram says how a repeated run ended as the table does
 * (`src/lib/procexit.ts`, `src/components/ProcDiagram.tsx`).
 *
 *   npm run check:procexit      (node --test mock/check_procexit.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. The diagram's box counted only non-zero exit codes, so a run killed by a signal,
 * or one still running, read "all exit 0". The module is loaded with Vite's loader (mock/ssr.mjs).
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { renderer } from './ssr.mjs';

let ui;
before(async () => {
  ui = await renderer('procexit');
});
after(() => ui?.close());

/** A run of `count` members, as the service sends one (proctree.py `_group_dict`). */
const run = (count, ends) => ({
  k: 'g', motif: ['sleep'], period: 1, reps: count, count, n: count, start: 0, end: 10, cpu: null,
  sub_cpu: null, sub_io: null, dur_max: 1, exits: {}, signals: {}, alive: null, distinct: 1,
  samples: [{ args: 'sleep 1', n: count }], items: [], more: 0, ...ends,
});
/** The line the box says the run's ending in, `[text, marked]`. */
async function ending(g) {
  const { boxText } = await ui.load('components/ProcDiagram.tsx');
  return boxText(g, false, 0, false).lines[1][0];
}

test('a run killed by a signal is not "all exit 0"', async () => {
  // on develop: ['all exit 0', false]
  assert.deepEqual(await ending(run(3, { signals: { 9: 3 } })), ['3 ended by a signal', true]);
  assert.deepEqual(await ending(run(4, { signals: { 15: 1 }, exits: { 0: 2, 2: 1 } })), ['1 ended by a signal, 1 failed', true]);
});

test('a run still running, or with no exit seen, is not "all exit 0"', async () => {
  // on develop: ['all exit 0', false]
  assert.deepEqual(await ending(run(2, { alive: 2 })), ['2 running at the end', false]);
  assert.deepEqual(await ending(run(3, { exits: { 0: 2 }, alive: 1 })), ['2 exit 0 · 1 running at the end', false]);
  assert.deepEqual(await ending(run(3, { exits: { 0: 1 } })), ['1 exit 0 · 2 no exit seen', false]);
});

test('"all exit 0" when every member exited 0, and a non-zero exit is a failure', async () => {
  assert.deepEqual(await ending(run(4, { exits: { 0: 4 } })), ['all exit 0', false]);
  assert.deepEqual(await ending(run(3, { exits: { 0: 2, 1: 1 } })), ['1 failed', true]);
});
