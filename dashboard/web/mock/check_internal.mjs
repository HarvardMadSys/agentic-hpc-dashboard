/* A self-check for the Internal tab's per-process distributions (`DistTable` in `src/tabs/ResourcesTab.tsx`).
 *
 *   npm run check:internal      (node --test mock/check_internal.mjs)
 *
 * Like check_panels.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. A quantile whose every value is exactly 0 is a counter the collector could not
 * fill, sent as 0s rather than nulls; the service flags it `all_zero` (`Quant.as_dict`,
 * rc_dashboard/reducers/__init__.py), and the page reads it as not measured, never as 0, as the I/O
 * distributions do (`QuantCells`). A distribution that holds a 0 among other values was measured, and
 * its 0 stays a 0. The tab is rendered (mock/ssr.mjs) on the fixture, whose IDE peak memory is all 0s,
 * and on a hand-made quantile.
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
  ui = await renderer('internal');
});
after(() => ui?.close());

/** The cells of the distributions table's row for `measure`, after its name: for each class shown, in
 *  order, one per statistic the chips show (mean, median and p99 at first), each as its text and its
 *  hover. */
async function cells(P, measure) {
  const html = await ui.render('tabs/ResourcesTab.tsx', 'ResourcesTab', { P, cls: ALL, part: 'internal' });
  const at = html.indexOf('id="sec-per-event-distributions"');
  assert.ok(at >= 0, 'the distributions panel keeps its anchor');
  const table = html.slice(at, html.indexOf('</table>', at));
  const row = table.split('<tr').find((r) => textOf(r.match(/<td[^>]*>(.*?)<\/td>/)?.[1] ?? '').startsWith(measure));
  assert.ok(row, `a row for ${measure}`);
  return [...row.matchAll(/<td[^>]*>(.*?)<\/td>/g)].slice(1).map(([, td]) => ({
    text: textOf(td),
    tip: td.match(/data-tip="([^"]*)"/)?.[1] ?? null,
  }));
}

test('a quantile of all 0s reads not measured, with the reason on hover, and never 0', async () => {
  const q = FIX.resources.by_class.ide.quants.peak_rss_mb;
  assert.equal(q.all_zero, true, 'the fixture holds an all-zero quantile');
  const row = await cells(FIX, 'peak memory');
  assert.equal(row.length, 9, 'three classes, three statistics each');
  const ide = row.slice(6);
  // on develop: 0.00 MB under the mean, the median and the p99
  assert.deepEqual(ide.map((c) => c.text), ['not measured', '—', '—']);
  assert.match(ide[0].tip ?? '', /every value is exactly 0/);
  for (const c of row) assert.doesNotMatch(c.text, /^0(\.0+)? MB$/, 'a 0 the collector never measured');
  // the classes whose peak memory was measured keep their numbers
  for (const c of row.slice(0, 6)) assert.match(c.text, /^[\d.,]+ (MB|GB|TB)$/);
});

test('a measured 0 in a distribution that is not all 0s stays a 0', async () => {
  const res = FIX.resources;
  const wait = {
    ...res.by_class.agent.quants.sched_wait_s,
    p50: 0, p99: 0.25, mean: 0.01, max: 0.5, zero_pct: 62, all_zero: false, suspect: null,
  };
  const agent = { ...res.by_class.agent, quants: { ...res.by_class.agent.quants, sched_wait_s: wait } };
  const P = { ...FIX, resources: { ...res, by_class: { ...res.by_class, agent } } };
  const [mean, median, p99] = (await cells(P, 'runqueue wait')).slice(0, 3).map((c) => c.text);
  assert.deepEqual([mean, median, p99], ['10 ms', '0 ms', '250 ms']);
});
