/* A self-check for the per-tool disk chart on Resources (`toolBars` in `src/tabs/RiskIoTab.tsx`,
 * drawn by `HGroupBars` in `src/charts/primitives.tsx`).
 *
 *   npm run check:io            (node --test mock/check_io.mjs)
 *
 * Like check_panels.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing
 * under `src/` imports it. Disk read and disk written were two charts, each ranked on its own
 * counter; they are one now, a row per tool with a bar for each. What the two charts drew must
 * still be drawn: the heaviest writers as surely as the heaviest readers, a measured 0 as 0 and
 * not as missing, and both bars on one scale, in direction hues no class owns. The I/O tab is
 * rendered (mock/ssr.mjs) on the fixture with `io_process.by_tool` made by hand, in the service's
 * shape: no `cls` on a row.
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
  ui = await renderer('io');
});
after(() => ui?.close());

/** A `by_tool` row as the service sends one (`reducers/io.py:_tool_row`). */
const tool = (name, rd, wr) => ({
  tool: name, events: 10, with_io: 9, rd_mb: rd, wr_mb: wr,
  rchar_mb: rd == null ? null : 2 * rd, wchar_mb: wr == null ? null : 2 * wr,
  cache_hit_pct: 50, io_coverage_pct: 90,
});

/** The per-tool panel's markup, drawn over `by_tool`. */
async function panel(byTool) {
  const P = { ...FIX, io_process: { ...FIX.io_process, by_tool: byTool } };
  const html = await ui.render('tabs/RiskIoTab.tsx', 'RiskIoTab', { P, cls: ALL, part: 'io', plates: false });
  const at = html.indexOf('id="sec-reads-by-tool"');
  assert.ok(at >= 0, 'the per-tool panel keeps the anchor its citations land on');
  return html.slice(at, html.indexOf('</section>', at));
}

/** The chart's rows as drawn: each tool's name, then each bar in series order with its value as
 *  written, its width (null: no bar at all), its fill, and whether a click opens its commands. */
function rows(sec) {
  const heads = [...sec.matchAll(/<text class="vlbl"[^>]*text-anchor="end"[^>]*>([^<]*)<\/text>/g)];
  return heads.map((h, i) => {
    const body = sec.slice(h.index + h[0].length, heads[i + 1]?.index);
    const bars = [...body.matchAll(/<g>(?:<rect([^>]*)><\/rect>)?<text class="([^"]*)"[^>]*>([^<]*)<\/text><\/g>/g)];
    return {
      k: h[1],
      bars: bars.map(([, rect, cls, val]) => ({
        val,
        w: rect ? Number(/\swidth="([^"]+)"/.exec(rect)[1]) : null,
        fill: rect ? /\sfill="([^"]+)"/.exec(rect)[1] : null,
        pick: cls.includes('hpick'),
      })),
    };
  });
}

// 16 tools that only read, 16 that only write, and one that does both, heaviest on each: no two
// totals alike
const READERS = Array.from({ length: 16 }, (_, i) => tool(`r${i + 1}`, 1000 - 10 * i, 0));
const WRITERS = Array.from({ length: 16 }, (_, i) => tool(`w${i + 1}`, 0, 500 - 10 * i));
const BOTH = tool('both', 1500, 600);

test('the rows are the 14 heaviest on each counter, the most disk bytes first', async () => {
  const drawn = rows(await panel([...READERS, ...WRITERS, BOTH]));
  const names = drawn.map((r) => r.k);
  // `both` is among the 14 heaviest on each, and a row once
  assert.deepEqual(names, [
    'both',
    ...READERS.slice(0, 13).map((t) => t.tool),
    ...WRITERS.slice(0, 13).map((t) => t.tool),
  ]);
  // a tool that only writes is drawn as a chart of writes alone drew it; one past the 14th is not
  assert.ok(names.includes('w13') && !names.includes('w14') && !names.includes('r14'));
  for (const r of drawn) assert.equal(r.bars.length, 2, `${r.k}: a bar for read and one for written`);
});

test("a kept tool's 0 on the other counter is drawn as 0 and opens nothing; an unmeasured one is a dash", async () => {
  const drawn = rows(await panel([tool('reads', 800, 0), tool('writes', 0, 300), tool('unread', null, 200)]));
  const at = (k) => drawn.find((r) => r.k === k).bars;
  const [readsRd, readsWr] = at('reads');
  assert.ok(readsRd.pick && readsRd.w > 0);
  assert.deepEqual(readsWr, { val: '0.00 MB', w: 0, fill: readsWr.fill, pick: false }, 'a measured 0, not a dash');
  assert.equal(at('writes')[0].val, '0.00 MB');
  assert.equal(at('writes')[0].pick, false, 'no command read a byte: nothing to open');
  const [unreadRd, unreadWr] = at('unread');
  assert.deepEqual(unreadRd, { val: '—', w: null, fill: null, pick: false }, 'not measured: no bar, a dash');
  assert.ok(unreadWr.pick && unreadWr.w > 0);
});

test('both bars are on one scale', async () => {
  const drawn = rows(await panel([tool('a', 1000, 0), tool('b', 0, 500), tool('c', 250, 750)]));
  const w = Object.fromEntries(drawn.map((r) => [r.k, r.bars.map((b) => b.w)]));
  // in a chart of its own, b's 500 written was as long as a's 1000 read
  assert.equal(w.b[1] / w.a[0], 0.5);
  assert.equal(w.c[0] / w.a[0], 0.25);
  assert.equal(w.c[1] / w.a[0], 0.75);
});

test('the bars wear one hue per direction, never a class hue, though the rows name a class', async () => {
  const { CLS, CLS_COL } = await ui.load('lib/classes.ts');
  const classHues = new Set(CLS.map((c) => CLS_COL[c]));
  const drawn = rows(await panel(FIX.io_process.by_tool)); // the fixture's rows each carry a class
  assert.ok(drawn.length > 0);
  const fills = (i) => new Set(drawn.map((r) => r.bars[i].fill));
  assert.equal(fills(0).size, 1, 'every read bar alike');
  assert.equal(fills(1).size, 1, 'every written bar alike');
  assert.notDeepEqual([...fills(0)], [...fills(1)]);
  for (const f of [...fills(0), ...fills(1)]) assert.ok(!classHues.has(f), `${f} is a class's hue`);
  for (const r of drawn) assert.match(r.k, / · (agent|human|IDE)$/, 'a split row names its class instead');
});

test('a selection where no tool read or wrote a byte says none', async () => {
  const text = textOf(await panel([tool('idle', 0, 0)]));
  assert.match(text, /none · no tool read from or wrote to disk in the selection/);
});
