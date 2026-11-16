/* A self-check that every stylesheet under `src/` closes what it opens.
 *
 *   npm run check:css           (node --test mock/check_css.mjs)
 *
 * Like check_charts.mjs, it is not part of `npm run build` or `npm run typecheck`,
 * and nothing under `src/` imports it. It exists because the build cannot catch
 * this: a rule left without its `}` -- one merge dropped the brace after the
 * focus ring on `.seg button` -- makes everything after it nest inside that rule,
 * which CSS nesting accepts. The phone layout and the touch sizes that followed
 * then matched nothing, on every screen, with no error anywhere.
 *
 * The page writes no nested rules: a style rule holds declarations, and only an
 * at-rule (@media, @supports, @keyframes, @property, @container, @layer, @font-face)
 * holds rules. So it checks both that the braces balance and that no style
 * rule opens inside another style rule.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';

const SRC = new URL('../src/', import.meta.url).pathname;

function cssFiles(dir) {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? cssFiles(p) : f.endsWith('.css') ? [p] : [];
  });
}

/** Comments and strings blanked out, newlines kept, so line numbers still hold. */
function strip(css) {
  return css
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'/g, (m) => ' '.repeat(m.length));
}

/** Problems in one file: a `}` with nothing open, a style rule inside a style
 *  rule, a block still open at the end. Each names the line. */
function problems(css) {
  const out = [];
  const open = []; // { line, at } for each block not yet closed
  let line = 1;
  let head = ''; // what has been read since the last `{`, `}` or `;`
  for (const ch of strip(css)) {
    if (ch === '\n') line++;
    if (ch === '{') {
      const at = /^\s*@/.test(head);
      const parent = open[open.length - 1];
      // a declaration block inside a style rule is a nested rule
      if (parent && !parent.at && !/^\s*$/.test(head)) {
        out.push(`line ${line}: "${head.trim().slice(0, 60)}" opens inside the rule opened at line ${parent.line}`);
      }
      // keyframe selectors (from, to, 50%) live in @keyframes and count as at-content
      open.push({ line, at: at || (parent?.at && /keyframes/.test(parent.head ?? '')), head: head.trim() });
      head = '';
    } else if (ch === '}') {
      if (!open.length) out.push(`line ${line}: a "}" closes nothing`);
      open.pop();
      head = '';
    } else if (ch === ';') {
      head = '';
    } else {
      head += ch;
    }
  }
  for (const o of open) out.push(`line ${o.line}: "${o.head.slice(0, 60)}" is never closed`);
  return out;
}

const files = cssFiles(SRC);

test('there are stylesheets to check', () => {
  assert.ok(files.length > 0, 'no .css file found under src/');
});

for (const f of files) {
  test(`${f.slice(SRC.length)}: every block closes, and no rule nests in a rule`, () => {
    assert.deepEqual(problems(readFileSync(f, 'utf8')), []);
  });
}

test('the check sees a dropped brace', () => {
  const broken = '.a:focus-visible {\n  outline: 2px solid red;\n@media (max-width: 900px) {\n  .b { color: red; }\n}\n';
  assert.ok(problems(broken).some((p) => /opens inside the rule|never closed/.test(p)));
  assert.deepEqual(problems('.a { color: red; }\n@media (x) { .b { color: red; } }\n'), []);
  assert.deepEqual(problems('@keyframes k { from { opacity: 0; } to { opacity: 1; } }\n'), []);
});
