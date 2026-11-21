/* A self-check that no string from outside the page reads a table's prototype (`src/lib/lookup.ts`),
 * and that a part of the page that throws says so instead of blanking the page
 * (`src/components/Boundary.tsx`).
 *
 *   npm run check:lookup        (node --test mock/check_lookup.mjs)
 *
 * Like check_phases.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under
 * `src/` imports it. `?atype=__proto__` handed the filter's label `Object.prototype`, React threw
 * "Objects are not valid as a React child", and with no boundary above App the page went white.
 * The modules are loaded with Vite's loader and drawn with react-dom/server (mock/ssr.mjs). A server
 * render runs no error boundary, so a boundary is checked by what it draws once it has caught.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { renderer, textOf } from './ssr.mjs';

let ui;
before(async () => {
  ui = await renderer('lookup');
});
after(() => ui?.close());

const HOSTILE = ['__proto__', 'constructor', 'toString', 'hasOwnProperty', 'valueOf', '__defineGetter__'];
const source = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8');

test('a table answers only for the keys it holds', async () => {
  const { own, table } = await ui.load('lib/lookup.ts');
  const t = table({ a: 'A', '': 'empty' });
  for (const k of HOSTILE) {
    assert.equal(t[k], undefined, k);
    assert.equal(own({ a: 1 }, k), undefined, k);
  }
  assert.equal(t.a, 'A');
  assert.equal(t[''], 'empty');
  assert.deepEqual(Object.keys(t), ['a', '']);
  assert.equal(own({ a: 1 }, 'a'), 1);
  assert.equal(own(null, 'a'), undefined);
});

test('the shared tables an outside string indexes hold no prototype', async () => {
  const mods = {
    'lib/classes.ts': ['CLS_LBL', 'CLS_COL', 'AGENT_TYPE_COL', 'AGENT_TYPE_LBL'],
    'lib/feeds.ts': ['FEED_LBL'],
    'lib/sandbox.ts': ['SB_LABEL', 'SESSION_LABEL', 'CALL_LABEL', 'AP_LABEL', 'AP_SHORT', 'SB_COL', 'AP_COL'],
    'lib/surfaces.ts': ['SURFACE_LBL'],
    'lib/places.ts': ['PLACE_SEVERITY'],
    'lib/glossary.ts': ['GLOSS'],
    'lib/portraits.ts': ['CAT_TIP', 'USE_VERB', 'USE_TIP'],
    'components/ProcTree.tsx': ['ROLE_TAG', 'LINK_LBL', 'LINK_TIP'],
  };
  for (const [file, names] of Object.entries(mods)) {
    const m = await ui.load(file);
    for (const n of names) {
      assert.ok(m[n] && typeof m[n] === 'object', `${file} ${n}`);
      for (const k of HOSTILE) assert.equal(m[n][k], undefined, `${file} ${n}[${k}]`);
    }
  }
});

test('the words for an agent type, a feed, a state and a purpose are a string for any key', async () => {
  const W = await ui.load('components/WhoFilter.tsx');
  const F = await ui.load('lib/feeds.ts');
  const S = await ui.load('lib/sandbox.ts');
  const U = await ui.load('lib/purposes.ts');
  for (const k of HOSTILE) {
    // on develop: Object.prototype, or a function, for `__proto__` and the rest
    assert.equal(W.atypeLbl(k), k, `atypeLbl(${k})`);
    assert.equal(F.feedLabel(k), k, `feedLabel(${k})`);
    assert.equal(S.label(S.SB_LABEL, k), k, `label(${k})`);
    assert.equal(U.pcol(k), 'var(--sg)', `pcol(${k})`);
    assert.equal(U.plbl(k), k, `plbl(${k})`);
  }
  assert.equal(W.atypeLbl('none'), 'untagged');
  assert.equal(W.atypeLbl('claude_code'), 'Claude Code');
});

test('a purpose is coloured as its kind and named as itself, and the key holds each kind once', async () => {
  const U = await ui.load('lib/purposes.ts');
  // the service's purposes (rc_dashboard/normalize.py, _BUILTIN_PURPOSE), each to the kind it is drawn as
  const KIND = {
    poll: 'other',
    slurm_mon: 'slurm',
    slurm_sub: 'slurm',
    build: 'compute',
    compute: 'compute',
    filesearch: 'files',
    data: 'files',
    runtime: 'other',
    git: 'other',
    editor: 'other',
    other: 'other',
  };
  for (const [p, kind] of Object.entries(KIND)) {
    assert.equal(U.pkind(p), kind, p);
    assert.equal(U.pcol(p), U.pcol(kind), p);
  }
  assert.equal(U.pcol('no-such-purpose'), 'var(--sg)');
  assert.equal(U.pcol(null), 'var(--sg)');
  // a run or a process still says which purpose it was, not only its kind
  assert.equal(U.plbl('poll'), 'poll / introspection');
  assert.equal(U.plbl('slurm_sub'), 'slurm submit');
  assert.equal(U.plbl('data'), 'data move');
  assert.equal(U.plbl('git'), 'git');
  // the key: a swatch per kind, each its own colour, the grey tail last; a kind is its own kind, as the navigator stacks
  const key = U.purposeLegend();
  assert.equal(key.length, U.KINDS.length);
  assert.equal(new Set(key.map((e) => e.c)).size, key.length);
  assert.equal(key.at(-1).c, 'var(--sg)');
  for (const [kind] of U.KINDS) assert.equal(U.pkind(kind), kind);
  for (const e of key) assert.ok(e.tip, `${e.k} says what it holds`);
});

test('a link with ?atype=__proto__ and ?tab=__proto__ is read as strings, and the filter draws it', async () => {
  const { parseUrl } = await ui.load('lib/filters.ts');
  const u = parseUrl('?atype=__proto__&tab=__proto__&tab=constructor');
  assert.deepEqual(u.atypes, ['__proto__']);
  // on develop: Object.prototype, an object where the tab's key belongs
  assert.equal(u.tab, '__proto__');
  // on develop: "Objects are not valid as a React child", and a white page
  const html = await ui.render('components/WhoFilter.tsx', 'WhoFilter', {
    who: { users: [], atypes: ['__proto__', 'constructor'] }, facets: null, onChange: () => {},
  });
  assert.match(textOf(html), /__proto__/);
  assert.match(textOf(html), /constructor/);
});

test('a tab that throws while it is drawn says so, whatever it threw, with a way to reload', async () => {
  const { TabBoundary } = await ui.load('lib/lazyTab.tsx');
  // the class that catches for a tab: TabBoundary itself, or the boundary it draws
  const el = TabBoundary.getDerivedStateFromError ? ui.h(TabBoundary, null, 'the tab') : TabBoundary({ children: 'the tab' });
  const C = el.type;
  for (const thrown of [new Error('Objects are not valid as a React child'), 'a string', null, Object.create(null)]) {
    // a server render runs no boundary: draw what the boundary draws once it has caught `thrown`
    const b = new C(el.props);
    b.state = { ...b.state, ...C.getDerivedStateFromError(thrown) };
    let text;
    try {
      text = textOf(ui.markup(b.render()));
    } catch (e) {
      // on develop: anything but a chunk that would not load was thrown on up, to a white page
      text = `threw ${e?.message}`;
    }
    assert.match(text, /^not drawn this tab could not be drawn: .+ · reload$/, text);
  }
  assert.equal(ui.markup(ui.h(TabBoundary, null, 'the tab')), 'the tab', 'a tab that draws is drawn');
});

test('the page and each panel have a boundary of their own, and a panel tries again with new data', async () => {
  const { Boundary } = await ui.load('components/Boundary.tsx');
  // on develop: nothing above App
  assert.match(source('main.tsx'), /<Boundary what="the page">\s*\{readTreePop/);
  assert.match(source('components/Panel.tsx'), /<Boundary what="this panel" resetOn=\{p\}>\s*\{children\}\s*<\/Boundary>/);
  const before = { failed: true, error: new Error('x'), at: 'old data' };
  assert.equal(Boundary.getDerivedStateFromProps({ resetOn: 'old data' }, before), null, 'the same data: still failed');
  assert.equal(Boundary.getDerivedStateFromProps({ resetOn: 'new data' }, before).failed, false, 'new data: drawn afresh');
});
