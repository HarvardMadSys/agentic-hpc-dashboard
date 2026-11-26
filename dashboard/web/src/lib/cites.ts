/* Going to a place on the page: a tab, a panel on it and a row in it, lit up
 * once the tab has drawn it (`go`). The Attention list on Live and the Security
 * tab's checks link to their panels so.
 *
 * A panel is found by its anchor (Panel.tsx: `id ?? slug(title)`), or by the
 * end of a title the site words itself (`titled`), and a row in it by the text
 * that names it -- a user, a host, a command -- or an agent's row by its
 * `host:pid` (`data-keys`), so a table needs no wiring to be gone to. A row the
 * page does not draw as a row lands on its panel; a place no panel draws lands
 * on its tab.
 *
 * The module is named for Ask LLM's citations, which it was written for; that
 * feature has been removed.
 */
import { parseUrl, patched, urlQuery } from './filters';
import type { UrlState } from './filters';

/** Where a link goes, and how it is found once the page is there. */
export interface Arrival {
  /** what to set in the URL: the tab, and the window and filter it is read
   *  over when it names them */
  patch: Partial<UrlState>;
  anchor?: string;
  titled?: string;
  /** the texts that name its row, the most telling first */
  find: string[];
  /** the agents it is (`host:pid`): an agent's row is found by these */
  keys?: string[];
  /** its rows are drawn from a view the filter builds: wait for them */
  waits?: boolean;
}

const sameList = (a: readonly string[] | undefined, b: readonly string[] | undefined) =>
  (a ?? []).length === (b ?? []).length && (a ?? []).every((x) => (b ?? []).includes(x));

/** Whether going to `a` from the page `from` asks for a view the service may
 *  still have to build -- another filter or window -- whose rows come later. */
function builds(a: Arrival, from: UrlState): boolean {
  const p = a.patch;
  return (
    (p.users !== undefined && !sameList(p.users, from.users)) ||
    (p.atypes !== undefined && !sameList(p.atypes, from.atypes)) ||
    (p.win !== undefined && p.win !== from.win)
  );
}

/* ------------------------------------------------------------ arriving */

/** A cell's text without the disclosure mark a row that opens leads with. */
const cellText = (c: Element) => (c.textContent ?? '').replace(/^[\s▸▾►▼›]+/, '').trim();

/** The row of `root` the texts name: a cell that says exactly one of them
 *  beats a row that mentions it in passing, and the first text beats the rest. */
function findRow(root: ParentNode, needles: string[]): HTMLElement | null {
  const rows = Array.from(root.querySelectorAll<HTMLElement>('tr, li, [role="row"]')).filter(
    (r) => !r.closest('thead') && r.offsetParent !== null,
  );
  for (const n of needles) {
    let best: HTMLElement | null = null;
    for (const r of rows) {
      const t = r.textContent ?? '';
      if (!t.includes(n)) continue;
      if (Array.from(r.children).some((c) => cellText(c) === n)) return r;
      if (!best || t.length < (best.textContent ?? '').length) best = r;
    }
    if (best) return best;
  }
  return null;
}

/** The element an arrival is for: an agent's row by its keys (the agent's own
 *  row before a fold that holds it), else the row its texts name. */
function targetIn(scope: ParentNode, a: Arrival): HTMLElement | null {
  for (const k of a.keys ?? []) {
    const all = Array.from(scope.querySelectorAll<HTMLElement>(`[data-keys~="${k.replace(/["\\]/g, '\\$&')}"]`))
      .filter((e) => e.offsetParent !== null);
    const own = all.find((e) => !e.classList.contains('fold')) ?? all[0];
    if (own) return own;
  }
  return a.find.length ? findRow(scope, a.find) : null;
}

/** Open the row an arrival landed on, so it shows what it is: an agent's row
 *  draws its tree, a row that folds out lists what it counts. One already
 *  open stays open. */
function openRow(el: HTMLElement) {
  if (el.matches('button.al-item:not(.on):not(.fold), tr.xrow:not(.open), tr.u-row:not(.open)')) el.click();
}

/** In the tree an agent's row drew, the agent's own box: picked, so its
 *  blast radius opens beside the tree. The tree draws after the row opens, so
 *  this looks again for a moment; an agent folded away in it ("+3 more")
 *  leaves the tree as it is. */
function pickNode(keys: string[], tries = 0) {
  for (const k of keys) {
    const el = document.querySelector<HTMLElement>(`.tnode[data-id="${k.replace(/["\\]/g, '\\$&')}"]`);
    if (el) {
      if (!el.classList.contains('on')) el.click();
      return;
    }
  }
  if (tries < 15) window.setTimeout(() => pickNode(keys, tries + 1), 150);
}

/** A fold of look-alike agents holding the one sought: opened, it draws the
 *  agent's own row, which is looked for next. */
function openFold(el: HTMLElement): boolean {
  if (!el.matches('button.al-item.fold')) return false;
  if (el.getAttribute('aria-expanded') !== 'true') el.click();
  return true;
}

function panelOf(a: Arrival): HTMLElement | null {
  if (a.anchor) return document.getElementById(a.anchor);
  if (a.titled) {
    const want = a.titled.toLowerCase();
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('section.panel[data-title]'))) {
      if ((el.dataset.title ?? '').toLowerCase().endsWith(want)) return el;
    }
  }
  return null;
}

const motion = (): ScrollBehavior =>
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';

function light(el: HTMLElement, center: boolean) {
  el.scrollIntoView({ block: center ? 'center' : 'start', behavior: motion() });
  el.classList.remove('cite-hit');
  // a reflow between, so a second visit flashes again
  void el.offsetWidth;
  el.classList.add('cite-hit');
  window.setTimeout(() => el.classList.remove('cite-hit'), 2600);
}

/** Once the page is on its tab: go to the row and open it, or to its panel.
 *  The tab draws after its data, so this looks again for a while -- for
 *  minutes when the filter it set has a view to build, showing the panel
 *  meanwhile -- and stops when the reader goes to another tab. A row still
 *  missing by then leaves the panel lit. */
function arrive(a: Arrival) {
  const t0 = performance.now();
  const tab = a.patch.tab;
  const patience = a.waits ? 300000 : 10000;
  let shown = false;
  const tick = () => {
    if (tab && (parseUrl(location.search).tab || 'live') !== tab) return;
    const late = performance.now() - t0;
    const panel = panelOf(a);
    const scope = panel ?? (a.anchor || a.titled ? null : document.querySelector('main'));
    const row = scope ? targetIn(scope, a) : null;
    if (row) {
      if (openFold(row)) {
        window.setTimeout(tick, 150);
        return;
      }
      // opened first: the page redraws the row's classes as it opens, and
      // would take the light off with them
      openRow(row);
      requestAnimationFrame(() => requestAnimationFrame(() => light(row, true)));
      if (a.keys?.length && row.matches('button.al-item')) pickNode(a.keys);
      return;
    }
    const sought = a.find.length > 0 || (a.keys?.length ?? 0) > 0;
    if (panel && !shown && (!sought || late > 2500)) {
      light(panel, false);
      shown = true;
      if (!sought) return;
    }
    // a row the tab does not draw: the tab, from its top
    if (!a.anchor && !a.titled && late > 2500 && !a.waits) {
      window.scrollTo({ top: 0, behavior: motion() });
      return;
    }
    if (late < patience) window.setTimeout(tick, late < 10000 ? 120 : 500);
    else if (!shown) window.scrollTo({ top: 0, behavior: motion() });
  };
  tick();
}

/** Go to a place in this tab: the URL first (Back returns to where the reader
 *  was), then the row. */
export function go(a: Arrival) {
  const from = parseUrl(location.search);
  const q = urlQuery(patched(from, a.patch));
  const url = `${location.pathname}${q ? '?' + q : ''}`;
  const b = { ...a, waits: builds(a, from) };
  if (url !== location.pathname + location.search) {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
    // the page draws the tab it is going to first: until then the rows on
    // screen are the ones it is leaving
    requestAnimationFrame(() => requestAnimationFrame(() => arrive(b)));
    return;
  }
  arrive(b);
}
