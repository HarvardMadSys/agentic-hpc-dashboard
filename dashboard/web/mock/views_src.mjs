/* The page's tabs and its panels, read off its own source: every tab App.tsx's TABS lists, and every <Panel> with
 * the anchor Panel.tsx gives it -- its `id`, else the slug of its title -- and its `view` where it names one.
 *
 * Not a check itself, and nothing under `src/` imports it. The checks that must know every place a link can land read
 * it (check_summary.mjs), so a link to a tab or a part that is not there fails a check, not a reader.
 *
 *   const tabs = tabsOf();            // [['summary', 'Summary'], ['live', 'Live'], ...]
 *   const panels = panelsOf();        // [{ file, anchor, view, title }, ...]
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

export const SRC = fileURLToPath(new URL('../src/', import.meta.url));

/** Panel.tsx's anchor for a title. */
export const slug = (s) => 'sec-' + s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

/** Every tab, `[key, label]`, in the rail's order, from App.tsx's TABS. */
export function tabsOf(app = readFileSync(join(SRC, 'App.tsx'), 'utf8')) {
  const block = /const TABS: Tab\[\] = \[([\s\S]*?)\n\];/.exec(app)?.[1];
  if (!block) throw new Error('App.tsx keeps no TABS');
  return [...block.matchAll(/\{ k: '([a-z]+)', lbl: '([^']+)'/g)].map((m) => [m[1], m[2]]);
}

/** Every .tsx file under src/. */
function sources(dir = SRC) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? sources(p) : p.endsWith('.tsx') ? [p] : [];
  });
}

/** Where a string literal may start in an expression: after an operator or an opening bracket, not inside a word
 *  (an apostrophe in JSX text, `don't`, is not a quote). */
const OPENS = new Set(['(', '[', '{', ',', '=', ':', '?', '!', '&', '|', '+', '>', '\n', '']);

/** Each `<Panel ...>` opening tag in `text`, flattened to its own attributes: every `{...}` body blanked, so a
 *  `title=` of an element inside a `header={...}` is not taken for the panel's, and every comment dropped. */
export function panelTags(text) {
  const out = [];
  const re = /<Panel(?=[\s>/])/g;
  let m;
  while ((m = re.exec(text))) {
    // a mention in a comment ("every <Panel> is a section") is no panel
    const line = text.lastIndexOf('\n', m.index) + 1;
    if (text.lastIndexOf('/*', m.index) > text.lastIndexOf('*/', m.index) || text.slice(line, m.index).includes('//')) continue;
    let flat = '';
    let depth = 0;
    let quote = null;
    let prev = '';
    let i = m.index;
    for (; i < text.length; i++) {
      const c = text[i];
      const keep = (s) => {
        flat += depth ? ' '.repeat(s.length) : s;
      };
      if (quote) {
        keep(c);
        if (c === '\\') keep(text[++i] ?? '');
        else if (c === quote) quote = null;
        continue;
      }
      if (c === '/' && text[i + 1] === '/') {
        i = text.indexOf('\n', i) - 1;
        if (i < 0) i = text.length;
        continue;
      }
      if (c === '/' && text[i + 1] === '*') {
        i = text.indexOf('*/', i) + 1;
        continue;
      }
      if (c === '"' || c === '`' || (c === "'" && OPENS.has(prev))) {
        quote = c;
        keep(c);
        continue;
      }
      if (c === '{') {
        keep(c);
        depth++;
      } else if (c === '}') {
        depth--;
        keep(c);
      } else {
        keep(c);
        if (c === '>' && depth === 0) break;
      }
      if (!/\s/.test(c)) prev = c;
    }
    out.push(flat);
    re.lastIndex = i;
  }
  return out;
}

const attr = (flat, name) => new RegExp(`\\s${name}="([^"]*)"`).exec(flat)?.[1] ?? null;

/** Every <Panel> under src/: its file, its anchor (`id`, else the slug of a literal `title`; null when neither is
 *  written out), the `view` it is counted under where it names one, and its literal title (null for one made up at
 *  run time). */
export function panelsOf() {
  return sources().flatMap((p) =>
    panelTags(readFileSync(p, 'utf8')).map((flat) => {
      const id = attr(flat, 'id');
      const title = attr(flat, 'title');
      return { file: relative(SRC, p), anchor: id ?? (title != null ? slug(title) : null), view: attr(flat, 'view'), title };
    }),
  );
}
