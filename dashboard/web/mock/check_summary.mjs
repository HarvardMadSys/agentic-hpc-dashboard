/* A self-check for Summary, the page's first screen (src/tabs/SummaryTab.tsx): its four answers render from the
 * fixture, in eight numbers at most, each with its words; a value nobody measured reads "not measured", never 0, and a
 * class the filter hides "not shown"; the classes are told by their words; every link goes to a tab the rail has and
 * lands on a part that tab draws; and the bare address opens Summary while a link the page wrote before Summary was
 * first still opens Live.
 *
 *   npm run check:summary       (node --test mock/check_summary.mjs)
 *
 * Like check_ux.mjs, it is not part of `npm run build` or `npm run typecheck`, and nothing under `src/` imports it.
 * The screen is rendered (mock/ssr.mjs) on the fixture's numbers and on hand-made ones; App is not drawn (it needs a
 * browser and the service), so its wiring is read as text.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { renderer, textOf } from './ssr.mjs';
import { panelsOf, tabsOf } from './views_src.mjs';

const api = JSON.parse(readFileSync(fileURLToPath(new URL('./api.json', import.meta.url)), 'utf8'));
const FIX = api.panels.panels;
const ALL = ['agent', 'human', 'ide'];
const source = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8');

let ui;
before(async () => {
  ui = await renderer('summary');
});
after(() => ui?.close());

const props = (over = {}) => ({
  P: FIX,
  traj: api.trajectories,
  cls: ALL,
  status: api.status,
  statusError: null,
  go() {},
  href: (tab) => `/?class=agent&class=human&class=ide&tab=${tab}`,
  ...over,
});
const draw = (over) => ui.render('tabs/SummaryTab.tsx', 'Summary', props(over));
/** each card's text, by its anchor */
const cards = (html) =>
  Object.fromEntries(html.split('<section').slice(1).map((s) => [/\bid="([^"]+)"/.exec(s)[1], textOf('<section' + s.split('</section>')[0])]));
/** each figure as the screen shows it: a number, or the words in its place */
const figures = (html) => [...html.matchAll(/class="sum-num( sm| nm)?">([^<]*)</g)].map((m) => ({ nm: m[1] === ' nm', text: m[2] }));
const numbers = (html) => figures(html).filter((f) => !f.nm);

/** A reduced risk panel with a command section and a credentials section, as the service sends one. */
const risk = (dg, sec) => ({
  _feed: 'ebpfm', _status: 'ok', _present: true, _paths: [], _notice: null,
  sections: [
    { id: 'login_compute', kind: 'login_compute', title: 'Compute on a login node' },
    { id: 'commands', kind: 'commands', title: 'Commands that should not run here', rules: ['rm_shared'] },
    { id: 'credentials', kind: 'credentials', title: 'Credentials on a command line', rules: ['openai_key'] },
  ],
  dangerous: {
    rows: [{ id: 'rm_shared', command: 'rm -rf on a shared path', axis: 'files', severity: 'HIGH', n: dg + 2, users: 2,
      hosts: ['holylogin01'], cpu_s: 1, rated: 0, succeeded: 0, works_pct: null, classes: { agent: dg, human: 2 } }],
  },
  secrets: {
    rows: [{ id: 'openai_key', pattern: 'OpenAI/Anthropic-style key literal', severity: 'HIGH', n: sec + 1, users: 2,
      tools: ['curl'], classes: { agent: sec, human: 1 } }],
    args_scanned: 41102, args_truncated: 0, truncation_pct: 0,
  },
});

test('the four answers render from the fixture, each number with its words', async () => {
  const c = cards(await draw());
  assert.deepEqual(Object.keys(c), ['sec-summary-collection', 'sec-summary-agents', 'sec-summary-risk', 'sec-summary-nodes']);
  // collection: the hosts whose collector is OK of every host /api/status lists, the level in a word, and who is not
  const ok = api.status.hosts.filter((h) => h.verdict === 'ok').length;
  assert.match(c['sec-summary-collection'], /^Is collection healthy\? something to look at /);
  assert.match(c['sec-summary-collection'], new RegExp(`\\b${ok} of ${api.status.hosts.length} login nodes’ collectors OK\\b`));
  assert.match(c['sec-summary-collection'], /not OK: holylogin02 \(degraded\)/);
  // agents: the trajectories' summary, as Live's sessions plate reads it
  const a = api.trajectories.summary.agent;
  assert.match(c['sec-summary-agents'], new RegExp(`\\b${a.users} users ran an agent ${a.sessions} agent sessions\\b`));
  // risk: the fixture sends no risk panel, so nothing is counted and the card says why
  assert.match(c['sec-summary-risk'], /not checked not measured agent commands that should not run here not checked: no risk panel was sent/);
  // the nodes: the busiest by CPU, and what the service's own lines flag, the way Needs attention counts it
  const node = FIX.node;
  const busiest = [...node.hosts].sort((x, y) => x.cpu_idle_pct - y.cpu_idle_pct)[0];
  const flags = node.disk.filter((d) => d.full).length
    + node.hosts.flatMap((h) => h.per_user).filter((u) => (u.throttled_delta_s ?? 0) > 0).length
    + node.nfs.filter((m) => (m.rd_retrans ?? 0) + (m.wr_retrans ?? 0) > 0).length;
  assert.ok(flags > 0, 'the fixture flags something on its nodes');
  assert.match(c['sec-summary-nodes'], /^Is a login node struggling\? flagged /);
  assert.match(c['sec-summary-nodes'], new RegExp(`\\b${Math.round(100 - busiest.cpu_idle_pct)}% CPU busy on ${busiest.host}, the busiest login node\\b`));
  assert.match(c['sec-summary-nodes'], new RegExp(`\\b${flags} problems flagged\\b`));
  // a risk panel with hits: each kind its own number, the card flagged, and its link to the first section with a hit
  const r = cards(await draw({ P: { ...FIX, risk: risk(4, 3) } }))['sec-summary-risk'];
  assert.match(r, /^Is anything risky\? flagged 4 agent commands that should not run here 3 credentials on agents’ command lines/);
  const clean = cards(await draw({ P: { ...FIX, risk: risk(0, 0) } }))['sec-summary-risk'];
  assert.match(clean, /nothing flagged 0 agent commands that should not run here 0 credentials on agents’ command lines/, 'a measured 0 stays 0');
});

test('a login node with its CPU nearly all busy is struggling with nothing else flagged; a failed status read is not current', async () => {
  // the fixture's nodes with nothing the service's lines flag, and one of them 96% busy
  const quiet = {
    ...FIX.node,
    disk: FIX.node.disk.map((d) => ({ ...d, full: false })),
    nfs: [],
    hosts: FIX.node.hosts.map((h, i) => ({
      ...h,
      cpu_idle_pct: i === 0 ? 4 : 60,
      per_user: (h.per_user ?? []).map((u) => ({ ...u, throttled_delta_s: 0 })),
    })),
  };
  const busy = cards(await draw({ P: { ...FIX, node: quiet } }))['sec-summary-nodes'];
  assert.match(busy, /^Is a login node struggling\? busy /);
  assert.match(busy, new RegExp(`96% CPU busy on ${quiet.hosts[0].host}, the busiest login node 0 problems flagged ${quiet.hosts[0].host}’s CPU nearly all busy`));
  const calm = { ...quiet, hosts: quiet.hosts.map((h) => ({ ...h, cpu_idle_pct: 60 })) };
  assert.match(cards(await draw({ P: { ...FIX, node: calm } }))['sec-summary-nodes'], /^Is a login node struggling\? no problem flagged 40% CPU busy/);
  // an answer, then a read that failed: the answer before is drawn, and the verdict says it is not current
  const stale = cards(await draw({ statusError: '503 Service Unavailable' }))['sec-summary-collection'];
  assert.match(stale, /^Is collection healthy\? not current /);
  assert.match(stale, /\/api\/status did not answer its last read; this is its answer before that; not OK then: holylogin02 \(degraded\)/);
});

test('at most eight numbers, and no other number in the words around them', async () => {
  // as the service sends them: each payload names the window it was reduced over
  const win = { label: '24h', state: 'ready' };
  const traj = { ...api.trajectories, _window: win };
  for (const P of [FIX, { ...FIX, risk: { ...risk(4, 3), _window: win } }]) {
    const html = await draw({ P, traj });
    const shown = numbers(html);
    assert.ok(shown.length <= 8, `${shown.length} numbers: ${shown.map((f) => f.text).join(', ')}`);
    // every number is one of the figures: the words around them name hosts and the window, and count nothing
    let text = textOf(html);
    assert.match(text, /last 24h/);
    text = text.replaceAll('last 24h', ' ');
    for (const f of shown) text = text.replace(f.text, ' ');
    for (const h of FIX.node.hosts) text = text.replaceAll(h.host, ' ');
    assert.doesNotMatch(text, /\d/, text);
  }
  assert.equal(numbers(await draw({ P: { ...FIX, risk: risk(4, 3) } })).length, 8, 'two to a card');
});

test('a value nobody measured reads "not measured", never 0, and says why', async () => {
  // nothing has come yet: no status, no sessions, no panels
  const waiting = await draw({ P: {}, traj: null, status: null });
  const figs = figures(waiting);
  assert.ok(figs.length >= 4 && figs.every((f) => f.nm && f.text === 'not measured'), JSON.stringify(figs));
  const c = cards(waiting);
  assert.match(c['sec-summary-collection'], /not checked not measured login nodes’ collectors OK waiting for \/api\/status/);
  assert.match(c['sec-summary-agents'], /waiting for \/api\/trajectories/);
  assert.match(c['sec-summary-risk'], /not checked: waiting for the service/);
  assert.match(c['sec-summary-nodes'], /waiting for the service/);
  assert.doesNotMatch(textOf(waiting), /\b0\b/);
  // a request that failed says it did not answer, and keeps its error for the pill's hover
  const failed = cards(await draw({ status: null, statusError: '503 Service Unavailable' }))['sec-summary-collection'];
  assert.match(failed, /not checked not measured login nodes’ collectors OK \/api\/status did not answer/);
  // the service's summary missing is not a 0
  const { summary: _s, ...bare } = api.trajectories;
  assert.match(cards(await draw({ traj: bare }))['sec-summary-agents'], /not measured users ran an agent not measured agent sessions not measured: the service sent no count of sessions/);
  // a node tier with no snapshot, and one whose hosts report no CPU
  const noNode = { ...FIX, node: { ...FIX.node, _present: false, hosts: [] } };
  assert.match(cards(await draw({ P: noNode }))['sec-summary-nodes'], /not checked not measured CPU busy on the busiest not measured problems flagged not checked: no login node has written a snapshot yet/);
  const noCpu = { ...FIX, node: { ...FIX.node, hosts: FIX.node.hosts.map((h) => ({ ...h, cpu_idle_pct: null })) } };
  assert.match(cards(await draw({ P: noCpu }))['sec-summary-nodes'], /not measured CPU busy: no login node reported its CPU/);
  // a risk panel the service could not reduce counts nothing
  assert.match(cards(await draw({ P: { ...FIX, risk: { ...risk(4, 3), _error: 'boom' } } }))['sec-summary-risk'], /not checked: its rules could not be applied/);
});

test('a class the filter hides is not shown, never measured as 0, and the classes are told by their words', async () => {
  const html = await draw({ cls: ['human', 'ide'], P: { ...FIX, risk: risk(4, 3) } });
  const c = cards(html);
  assert.match(c['sec-summary-agents'], /not shown users ran an agent not shown agent sessions the agent class is filtered out/);
  assert.match(c['sec-summary-risk'], /not shown agent commands that should not run here the agent class is filtered out/);
  // every class's sessions side by side, each with its word beside its colour: agent, human, IDE, never two
  const all = await draw();
  const legend = /<div class="legend">([\s\S]*?)<\/div>/.exec(all)?.[1] ?? '';
  assert.deepEqual([...legend.matchAll(/<\/i>([^<]+)<\/span>/g)].map((m) => m[1]), ['agent', 'human', 'IDE']);
  // and each verdict is a word, not a colour alone
  for (const v of ['something to look at', 'flagged', 'not checked']) assert.match(textOf(all), new RegExp(v));
});

test('every link goes to a tab the rail has, and lands on a part that tab draws', async () => {
  const tabs = new Set(tabsOf().map(([k]) => k));
  const anchors = new Set(panelsOf().map((p) => p.anchor).filter(Boolean));
  const hrefs = [...(await draw({ P: { ...FIX, risk: risk(4, 3) } })).matchAll(/<a class="sum-go" href="([^"]+)"/g)].map((m) =>
    m[1].replaceAll('&amp;', '&'),
  );
  assert.ok(hrefs.length >= 6, hrefs.join(' '));
  for (const h of hrefs) {
    const tab = new URLSearchParams(h.split('?')[1]).get('tab');
    assert.ok(tabs.has(tab), `${h}: no tab ${tab}`);
  }
  // where a click lands: each card's links, as the page follows them (`go`)
  const S = await ui.load('tabs/SummaryTab.tsx');
  const N = await ui.load('lib/nodes.ts');
  const nodes = N.nodeWords(null);
  const P = { ...FIX, risk: risk(4, 3) };
  const links = [
    ...S.collection(api.status, null, nodes).links,
    ...S.agents(api.trajectories, null, ALL).links,
    ...S.risky(P, ALL).links,
    ...S.struggling(P, nodes).links,
  ];
  for (const l of links) {
    assert.ok(tabs.has(l.tab), `${l.where}: no tab ${l.tab}`);
    if (l.sec && l.tab !== 'risk') assert.ok(anchors.has(l.sec), `${l.where}: no part ${l.sec}`);
  }
  // a risk section is titled by the site's rules: the link lands on the one Site rules draws with a hit
  const sec = links.find((l) => l.tab === 'risk').sec;
  assert.equal(sec, 'sec-commands-that-should-not-run-here');
  const rules = await ui.render('tabs/RiskIoTab.tsx', 'RiskIoTab', { P, cls: ALL, part: 'risk' });
  assert.match(rules, new RegExp(`<section[^>]*id="${sec}"`));
  // Live is one click away, from the rail and from the screen's foot
  assert.match(textOf(await draw()), /What runs now, and who runs it: Live →$/);
});

test('the bare address opens Summary; a link the page wrote before it was first opens Live', async () => {
  const { parseUrl, urlQuery } = await ui.load('lib/filters.ts');
  assert.equal(parseUrl('').tab, 'summary');
  assert.equal(parseUrl('?tab=').tab, 'summary');
  // every link the page writes carries its classes; one without a tab was a link to Live
  assert.equal(parseUrl('?class=agent&class=human&class=ide').tab, 'live');
  assert.equal(parseUrl('?class=agent&win=60').tab, 'live');
  assert.equal(parseUrl('?user=alice').tab, 'live');
  // ... so the tab is always written now, Summary's too, and reads back as itself
  const s = parseUrl('');
  for (const tab of ['summary', 'live', 'resources']) {
    const q = urlQuery({ ...s, tab, win: 60 });
    assert.match(q, new RegExp(`(^|&)tab=${tab}(&|$)`));
    assert.equal(parseUrl('?' + q).tab, tab);
  }
  // old keys still land where they did
  assert.equal(parseUrl('?tab=overview').tab, 'live');
  assert.equal(parseUrl('?tab=io').tab, 'resources');
  // App: Summary is the first tab, so a tab that is not there opens it too, and the setup screen takes it over
  const app = source('App.tsx');
  assert.equal(tabsOf(app)[0][0], 'summary');
  assert.match(app, /const tab = tabs\.find\(\(t\) => t\.k === url\.tab\) \|\| tabs\[0\];/);
  assert.match(app, /case 'summary':\s*return <SummaryTab P=\{P\} traj=\{traj\.data\} trajError=\{traj\.error\} cls=\{cls\} go=\{goPart\} href=\{hrefTo\} \/>;/);
  assert.match(app, /const setup = empty && tab\.k !== 'feeds' && tab\.k !== 'trend';/);
  // one request of its own: /api/status, asked by the tab while it is open
  assert.match(source('tabs/SummaryTab.tsx'), /export function SummaryTab\(props: SummaryProps\) \{\s*const st = useStatus\(\);/);
});

test('one column on a phone, two where each card has room, and nothing wider than the screen', () => {
  const css = source('styles.css');
  assert.match(css, /\.grid\.sum \{\s*grid-template-columns: minmax\(0, 1fr\);\s*\}/);
  assert.match(css, /@media \(min-width: 1040px\) \{\s*\.grid\.sum \{\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/);
  // the words wrap rather than push the card out
  for (const sel of ['.sum-what', '.sum-note', '.sum-go']) {
    assert.match(css, new RegExp(`\\${sel} \\{[^}]*overflow-wrap: anywhere;`), sel);
  }
});
