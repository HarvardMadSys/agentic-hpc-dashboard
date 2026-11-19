/* The page's components rendered to markup, for the `check:*` scripts that must see what one shows.
 *
 * Not a check itself, and nothing under `src/` imports it. Node strips types but does not load JSX,
 * so Vite's own module loader turns a `.tsx` file into a module and react-dom/server renders it, as
 * check_phases.mjs does (`renderer()` there): no config of the project's, a cache directory of the
 * system's own, react from `node_modules`.
 *
 *   const ui = await renderer('shares');
 *   const html = await ui.render('components/WindowPlates.tsx', 'WindowPlates', props);
 *   ...
 *   await ui.close();
 */
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export async function renderer(name) {
  const { createServer } = await import('vite');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const { createElement } = await import('react');
  const server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    appType: 'custom',
    logLevel: 'error',
    cacheDir: join(tmpdir(), `rc-dashboard-check-${name}`),
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
  });
  const load = (file) => server.ssrLoadModule(`/src/${file}`);
  return {
    load,
    /** `file`'s export `name`, drawn with `props` */
    render: async (file, name, props) => renderToStaticMarkup(createElement((await load(file))[name], props)),
    /** an element made of the page's own components: `el(C, props, ...children)` */
    markup: (el) => renderToStaticMarkup(el),
    h: createElement,
    close: () => server.close(),
  };
}

/** The text of some markup: its tags taken out, and the entities React escapes put back. */
export const textOf = (html) =>
  html
    .replace(/<[^>]*>/g, ' ')
    .replaceAll('&quot;', '"')
    .replaceAll('&#x27;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&amp;', '&')
    .replace(/\s+/g, ' ')
    .trim();
