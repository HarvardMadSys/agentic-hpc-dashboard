/* A self-check for the portrait figures (`src/components/PortraitFigure.tsx`), on the fixture.
 *
 *   npm run check:figures        (node --test mock/check_figures.mjs)
 *
 * Like check:phases it is no unit-test runner: it is not part of `npm run build`,
 * and nothing under `src/` imports it. `apportion` (lib/portraits.ts) is loaded as
 * it is, node stripping its types; the figure is a component, so Vite's own module
 * loader turns it into a module and react-dom/server renders it to markup, for
 * every user in `api.json` and for made-up rows that reach the parts the fixture
 * does not. Each check is that what is drawn is what the row says: as many robots
 * as sessions open at once, up to the cap, a padlock only for launches that
 * bypassed approval, a shelf that says it was not read rather than drawing none.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { register } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
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

const L = await import('../src/lib/portraits.ts');
const api = JSON.parse(readFileSync(fileURLToPath(new URL('./api.json', import.meta.url)), 'utf8'));
const U = api.users.roster;
const rows = U.users;

/* --------------------------------------------------------------- apportion */

test('apportion shares items out by count, largest remainder first, and always as many as asked', () => {
  assert.deepEqual(L.apportion([['a', 7], ['b', 6]], 5), ['a', 'a', 'a', 'b', 'b']);
  assert.deepEqual(L.apportion([['job', 25], ['refused', 3]], 10), [...Array(9).fill('job'), 'refused']);
  for (const n of [1, 2, 3, 5, 6, 10]) {
    for (const types of [[['a', 1]], [['a', 9], ['b', 1]], [['a', 3], ['b', 3], ['c', 3]], [['a', 100], ['b', 1], ['c', 1], ['d', 1]]]) {
      assert.equal(L.apportion(types, n).length, n, `${n} of ${JSON.stringify(types)}`);
    }
  }
});

test('apportion gives each type one when there are enough items, the first types when not, and ties to the earlier', () => {
  assert.deepEqual(L.apportion([['a', 100], ['b', 1], ['c', 1]], 3), ['a', 'b', 'c']);
  assert.deepEqual(L.apportion([['a', 3], ['b', 2], ['c', 1]], 2), ['a', 'b']);
  assert.deepEqual(L.apportion([['a', 1], ['b', 1]], 3), ['a', 'a', 'b']);
  assert.deepEqual(L.apportion([['a', 1]], 0), []);
  assert.deepEqual(L.apportion([], 3), []);
});

/* ------------------------------------------------------------------ figures */

let server;
let draw;
before(async () => {
  const { createServer } = await import('vite');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const { createElement, Fragment } = await import('react');
  server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    appType: 'custom',
    logLevel: 'error',
    cacheDir: join(tmpdir(), 'rc-dashboard-check-figures'),
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
  });
  const F = await server.ssrLoadModule('/src/components/PortraitFigure.tsx');
  // one figure, or several on one page
  draw = (...rs) =>
    renderToStaticMarkup(createElement(Fragment, null, ...rs.map((row, i) => createElement(F.PortraitFigure, { key: i, row, catalogue: U.traits, tz: 'EDT' }))));
});
after(() => server?.close());

/** How many elements carry a class: `count(html, 'urf-robot')`. */
const count = (html, cls) => (html.match(new RegExp(`class="${cls}(?:"| )`, 'g')) ?? []).length;
/** The tooltips, their entities put back. */
const tips = (html) =>
  [...html.matchAll(/data-tip="([^"]*)"/g)].map((m) =>
    m[1].replaceAll('&quot;', '"').replaceAll('&#x27;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&'),
  );
const has = (row, id) => row.traits.some((t) => t.id === id);
/** The robots a row should draw: the most sessions open at once, six slots, the last a +N past six. */
const robotsOf = (row) => {
  const n = Math.max(row.peak_open, row.sessions > 0 ? 1 : 0);
  return n > 6 ? 5 : n;
};

/** A made-up row: the fixture's busiest user, with `o` over it and `lev` over its leverage. */
const base = rows.find((r) => r.user === 'thomwg11');
const T = (id, detail = 'its numbers') => {
  const t = U.traits.find((x) => x.id === id);
  return { id, label: t.label, tone: t.tone, detail };
};
const mk = (user, o = {}, lev = {}) => ({ ...base, user, ...o, leverage: { ...base.leverage, ...lev } });
const made = [
  mk('huge', {
    peak_open: 30, running: 12, sessions: 64, agent_types: { claude_code: 40, codex: 20, copilot: 4 },
    traits: [T('bypass'), T('sandboxed'), T('long'), T('poller'), T('overnight'), T('committer')],
    slurm: { ...base.slurm, jobs: 25, rejected: 3 }, words: { commit: 12, pr: 5, log: 3 },
  }, { autonomy: { id: 'bypass', bypassed: 50, auto: 0, of: 60 } }),
  mk('auto-day', {
    peak_open: 6, running: 0, sessions: 6, agent_types: { claude_code: 6 },
    traits: [T('interactive'), T('auto'), T('short')], slurm: null, words: {},
  }, { mode: { id: 'terminal', n: 6, of: 6 }, autonomy: { id: 'auto', bypassed: 0, auto: 5, of: 6 }, when: { busiest: 14, minutes: 20, night_pct: 0 } }),
  mk('script-few', {
    peak_open: 2, running: 1, sessions: 2, agent_types: { codex: 2 }, calls: 9, traits: [T('scripted')], slurm: null, words: {},
  }, { mode: { id: 'script', n: 2, of: 2 }, autonomy: { id: 'no_flag', bypassed: 0, auto: 0, of: 2 }, uses: [], actions: 9 }),
  mk('unread', {
    peak_open: 0, running: 0, sessions: 1, agent_types: { '': 1 }, calls: null, hours: Array(24).fill(0), active_min: 0,
    surfaces: { unknown: 1 }, traits: [], slurm: null, words: {},
  }, { mode: { id: 'unknown', n: 0, of: 1 }, autonomy: null, uses: [], actions: 0, when: null }),
];

test('every fixture user and made-up row draws what its row says', () => {
  for (const row of [...rows, ...made]) {
    const html = draw(row);
    const who = row.user;
    const n = robotsOf(row);
    assert.equal(count(html, 'urf-robot'), n, `${who}: robots`);
    const atOnce = Math.max(row.peak_open, row.sessions > 0 ? 1 : 0);
    assert.equal(count(html, 'urf-more'), atOnce > 6 ? 1 : 0, `${who}: +N`);
    if (atOnce > 6) assert.ok(html.includes(`>+${atOnce - 5}<`), `${who}: +N says the rest`);
    assert.equal(count(html, 'urf-live'), Math.min(row.running, n), `${who}: lit antennas`);
    assert.equal(count(html, 'urf-pod'), has(row, 'sandboxed') ? n : 0, `${who}: capsules`);
    const a = row.leverage.autonomy;
    assert.equal(count(html, 'urf-lock'), a?.id === 'bypass' ? 1 : 0, `${who}: padlock`);
    assert.equal(count(html, 'urf-shield'), a?.id === 'auto' ? 1 : 0, `${who}: shield`);
    assert.equal(count(html, 'urf-lean'), row.leverage.mode.id === 'script' ? 1 : 0, `${who}: leaning back`);
    assert.ok(html.includes(`data-mode="${row.leverage.mode.id}"`), `${who}: the laptop's mode`);
    assert.equal(count(html, 'urf-unread'), row.calls == null ? 1 : 0, `${who}: the shelf unread`);
    assert.equal(count(html, 'urf-use'), row.calls == null ? 0 : Math.min(3, row.leverage.uses.length), `${who}: use icons`);
    assert.equal(count(html, 'urf-moon'), has(row, 'overnight') ? 1 : 0, `${who}: the moon`);
    assert.equal(count(html, 'urf-sun'), !has(row, 'overnight') && row.leverage.when ? 1 : 0, `${who}: the sun`);
    assert.equal(count(html, 'urf-spoke'), row.hours.filter((m) => m > 0).length, `${who}: a spoke an active hour`);
    const words = (row.words.commit ?? 0) + (row.words.pr ?? 0) + (row.words.log ?? 0);
    assert.equal(count(html, 'urf-sheet'), Math.min(words, 5), `${who}: papers`);
    assert.equal(count(html, 'urf-mug'), has(row, 'long') ? 1 : 0, `${who}: mug`);
    assert.equal(count(html, 'urf-stopwatch'), has(row, 'short') ? 1 : 0, `${who}: stopwatch`);
    assert.equal(count(html, 'urf-hourglass'), has(row, 'poller') ? 1 : 0, `${who}: hourglass`);
    const sl = row.slurm;
    const rack = !!sl && (sl.jobs > 0 || sl.rejected > 0 || sl.queries > 0);
    assert.equal(count(html, 'urf-server'), rack ? 1 : 0, `${who}: server`);
    if (rack) {
      const asked = sl.jobs + sl.rejected;
      const want = asked > 10 ? L.apportion([['job', sl.jobs], ['refused', sl.rejected]].filter(([, k]) => k > 0), 10) : null;
      assert.equal(count(html, 'urf-light job'), want ? want.filter((x) => x === 'job').length : sl.jobs, `${who}: job lights`);
      assert.equal(count(html, 'urf-light refused'), want ? want.filter((x) => x === 'refused').length : sl.rejected, `${who}: refused lights`);
    }
  }
});

test('a figure is drawn in the page’s colour tokens only, with no number left unformed', () => {
  for (const row of [...rows, ...made]) {
    const html = draw(row);
    for (const [, attr, v] of html.matchAll(/ (fill|stroke)="([^"]*)"/g)) {
      assert.match(v, /^(var\(--[a-z0-9-]+\)|none)$/, `${row.user}: ${attr}="${v}"`);
    }
    assert.doesNotMatch(html, /NaN|Infinity|undefined/, `${row.user}: a number left unformed`);
    for (const t of tips(html)) {
      assert.ok(t.trim().length > 0, `${row.user}: an empty tooltip`);
      assert.doesNotMatch(t, /\bnull\b|undefined|NaN/, `${row.user}: ${t}`);
    }
  }
});

test('a row draws the same figure every time, and figures on one page clip by ids of their own', () => {
  for (const row of rows) assert.equal(draw(row), draw(row), row.user);
  const page = draw(rows[0], rows[1], rows[2]);
  const ids = [...page.matchAll(/<clipPath id="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(ids.length, 3);
  assert.equal(new Set(ids).size, 3, `clip ids: ${ids.join(', ')}`);
  for (const id of ids) assert.ok(page.includes(`clip-path="url(#${id})"`), id);
});

test('the parts that say a rule say the service’s own, from the catalogue it sent', () => {
  const huge = draw(made[0]);
  const all = tips(huge).join('\n');
  for (const id of ['sandboxed', 'long', 'poller', 'overnight']) {
    const rule = U.traits.find((t) => t.id === id).rule;
    assert.ok(all.includes(`rule: ${rule}`), id);
  }
  // past the cap the robots say how many are drawn, and the server that its lights share out
  assert.ok(all.includes('up to 30 sessions open at once (5 drawn, the rest +25)'));
  assert.ok(all.includes('its 10 lights shared out by them'));
  // an unread shelf says why, and a shelf with too few actions says so rather than drawing nothing silently
  assert.ok(tips(draw(made[3])).some((t) => t.startsWith('the shelf: what their agents do is not read')));
  assert.ok(tips(draw(made[2])).some((t) => t.startsWith('the shelf: too few actions to say')));
});
