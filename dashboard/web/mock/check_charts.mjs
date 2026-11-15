/* A self-check for the chart sizing rules in `src/charts/scale.ts`.
 *
 *   npm run check:charts        (node --test mock/check_charts.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`,
 * and nothing under `src/` imports it. It loads the real module (node strips the
 * types). It pins the claim the fluid charts make: a chart drawn at its natural
 * width keeps the ticks it had before it was fluid, a wider one gains none, and a
 * narrow one thins them rather than overprinting them.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tickTarget, timeTicks } from '../src/charts/scale.ts';

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
