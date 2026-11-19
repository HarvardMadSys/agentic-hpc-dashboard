/* A self-check for the chart tick and sizing rules in `src/charts/scale.ts`.
 *
 *   npm run check:charts        (node --test mock/check_charts.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`,
 * and nothing under `src/` imports it. It loads the real module (node strips the
 * types). It pins the claim the fluid charts make: a chart drawn at its natural
 * width keeps the ticks it had before it was fluid, a wider one gains none, and a
 * narrow one thins them rather than overprinting them. And the value axis: every
 * tick of one axis written alike, and a count axis in whole steps.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { axisFmt, niceTicks, tickTarget, timeTicks } from '../src/charts/scale.ts';

/** an axis's labels, as the charts write them when no formatter is given */
const labels = (max, integer = false) => {
  const t = niceTicks(max, 4, integer);
  return t.map(axisFmt(t));
};

test('an axis writes every tick alike: one unit, one number of decimals, and 0 at the base', () => {
  assert.deepEqual(labels(95), ['0', '25', '50', '75', '100']); // was 0.00 · 25.0 · … · 100
  assert.deepEqual(labels(18), ['0', '5', '10', '15', '20']); // was 0.00 · 5.0 · 10.0
  assert.deepEqual(labels(0.8), ['0', '0.2', '0.4', '0.6', '0.8']); // step 0.2: one decimal
  assert.deepEqual(labels(1), ['0', '0.25', '0.50', '0.75', '1.00']); // was 0.00 · … · 1.0
  assert.deepEqual(labels(1.4e6), ['0', '0.5M', '1.0M', '1.5M']); // was 0 · 500k · 1.00M · 1.50M
  assert.deepEqual(labels(1.9e4), ['0', '5k', '10k', '15k', '20k']); // was 0.00 · 5,000 · 10k
  assert.deepEqual(labels(7000), ['0', '2,000', '4,000', '6,000', '8,000']);
});

test('a small axis keeps its labels apart: four decimals, then powers of ten', () => {
  // with three decimals at most these read 0 · 0.001 · 0.001 · 0.002 · 0.002 and 0 · 0.000 · 0.001 · 0.001 · 0.001
  assert.deepEqual(labels(0.002), ['0', '0.0005', '0.0010', '0.0015', '0.0020']);
  assert.deepEqual(labels(0.0009), ['0', '2.5e-4', '5.0e-4', '7.5e-4', '1.0e-3']);
  assert.deepEqual(labels(0.00003), ['0', '1e-5', '2e-5', '3e-5']);
});

test('at any scale, an axis has no two labels alike and none wider than its margin holds', () => {
  for (const max of [3e-5, 1.1e-4, 2e-4, 4e-4, 9e-4, 1.1e-3, 2e-3, 4e-3, 8e-3, 0.03, 0.7, 3, 9, 18, 95, 240, 950,
    4000, 9000, 19000, 95000, 240000, 1.4e6, 9e6, 2.4e7, 9e8, 4e9]) {
    const l = labels(max);
    assert.equal(new Set(l).size, l.length, `a max of ${max}: ${l.join(' · ')}`);
    // the left margin holds six characters of a tick; an svg cuts the rest from the left
    for (const s of l) assert.ok(s.length <= 6, `a max of ${max}: ${s} is wider than six characters`);
  }
});

test('ticks are multiples of the step, never a running sum of it', () => {
  assert.deepEqual(niceTicks(0.3), [0, 0.1, 0.2, 0.3]);
  assert.deepEqual(niceTicks(0.7), [0, 0.2, 0.4, 0.6, 0.8]);
});

test('a count axis steps in whole numbers, so no gridline is labelled with a value it is not', () => {
  // with fint over the old ticks these read 0 · 1 · 1 · 2 · 2 and 0 · 0 · 1 · 1 · 1
  assert.deepEqual(labels(2, true), ['0', '1', '2']);
  assert.deepEqual(labels(1, true), ['0', '1']);
  assert.deepEqual(labels(9, true), ['0', '5', '10']); // not 2.5 · 5 · 7.5
  assert.deepEqual(labels(90, true), ['0', '25', '50', '75', '100']); // 25 is whole
  for (const max of [1, 2, 3, 5, 7, 9, 13, 40, 99, 250]) {
    for (const t of niceTicks(max, 4, true)) assert.ok(Number.isInteger(t), `${t} in the ticks for ${max}`);
  }
});

test('a chart at its natural width keeps the ticks it was drawn with', () => {
  assert.equal(tickTarget(520, 7), 7); // TimeChart
  assert.equal(tickTarget(1060, 10), 10); // Live's rate chart
});

test('a wider chart gains no ticks past its natural count', () => {
  for (const W of [556, 760, 1100, 1530]) assert.equal(tickTarget(W, 7), 7);
  for (const W of [1100, 1530]) assert.equal(tickTarget(W, 10), 10);
});

test('a narrow chart thins its ticks, and never below three', () => {
  assert.equal(tickTarget(330, 10), 4); // the rate chart on a 390px phone
  assert.equal(tickTarget(330, 7), 4);
  assert.equal(tickTarget(250, 7), 3);
  assert.equal(tickTarget(0, 7), 3);
});

// A day in January, so no daylight-saving change falls inside it in any zone.
const x0 = Date.UTC(2026, 0, 10, 0, 7) / 1000;
const day = x0 + 86400;
const step = (ticks) => (ticks.length > 1 ? (ticks[1] - ticks[0]) / 3600 : null);

test('a 24h window keeps its 6 h step from the natural width down to a phone', () => {
  assert.equal(step(timeTicks(x0, day, tickTarget(520, 7))), 6);
  assert.equal(step(timeTicks(x0, day, tickTarget(330, 7))), 6);
});

test("the rate chart's 24h window keeps its 3 h step at its natural width", () => {
  assert.equal(step(timeTicks(x0, day, tickTarget(1060, 10))), 3);
});
