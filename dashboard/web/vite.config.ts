import { execFileSync } from 'node:child_process';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import type { Plugin, ViteDevServer } from 'vite';
import { mockApi } from './mock/plugin.ts';

/* `virtual:build-info`: which code the page is, for the foot of the rail -- the
 * commit HEAD was at, whether anything under web/ differed from it, and when
 * the bundle was built (src/build-info.d.ts has the shape).
 *
 * Read each time the module is loaded, never once in this file: `./run.sh build`
 * keeps one `vite build --watch` alive across every rebuild, and a `define`
 * would stamp all of them with the watcher's start. The dev server bundles
 * nothing, so it has no build time, and it drops the module on every page load
 * so that a reload sees a commit made since.
 *
 * No git, or not a checkout: `commit` is null and the page says it cannot
 * tell, rather than naming one.
 */
function buildInfo(): Plugin {
  const ID = 'virtual:build-info';
  const RID = '\0' + ID;
  let root = '';
  let serve = false;
  let server: ViteDevServer | null = null;
  const git = (...args: string[]): string | null => {
    try {
      return execFileSync('git', args, {
        cwd: root,
        encoding: 'utf8',
        timeout: 5000,
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
    } catch {
      return null;
    }
  };
  return {
    name: 'rc-build-info',
    configResolved(c) {
      root = c.root;
      serve = c.command === 'serve';
    },
    configureServer(s) {
      server = s;
    },
    transformIndexHtml() {
      const graph = server?.environments.client.moduleGraph;
      const mod = graph?.getModuleById(RID);
      if (graph && mod) graph.invalidateModule(mod);
    },
    resolveId: (id) => (id === ID ? RID : undefined),
    load(id) {
      if (id !== RID) return;
      const commit = git('rev-parse', 'HEAD');
      // Only web/ goes into the bundle. --no-optional-locks: a rebuild set off
      // by a checkout must not take the index lock from under it.
      const status = commit == null ? null : git('--no-optional-locks', 'status', '--porcelain', '--', '.');
      return `export default ${JSON.stringify({
        commit,
        dirty: status == null ? null : status !== '',
        builtAt: serve ? null : new Date().toISOString(),
      })};`;
    },
  };
}

/* The backend serves `web/dist/` as static files and answers `/api/*` and `/ws`
 * on the same origin, so asset URLs are relative (`base: './'`) and the API is
 * addressed absolutely at `/api` (override with VITE_API_BASE).
 *
 * Dev has two modes:
 *   npm run dev        -> /api and /ws answered from mock/api.json (no backend)
 *   npm run dev:real   -> /api, /ws, /login, and /logout proxied to RC_BACKEND
 *                         (default :8787); the last two are the sign-in form's,
 *                         for a backend with a password (rc_dashboard/auth.py)
 */
export default defineConfig(({ mode }) => {
  const real = mode === 'real' || process.env.RC_MOCK === '0';
  const target = process.env.RC_BACKEND || 'http://127.0.0.1:8787';
  return {
    base: './',
    plugins: [react(), buildInfo(), ...(real ? [] : [mockApi()])],
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
      // One chunk boundary that is actually meaningful: the React runtime is
      // cacheable across dashboard deploys, the app code is not.
      rollupOptions: {
        output: {
          manualChunks: (id: string) =>
            id.includes('node_modules/react') || id.includes('node_modules/scheduler')
              ? 'react'
              : undefined,
        },
      },
    },
    server: real
      ? {
          proxy: {
            '/api': { target, changeOrigin: true },
            '/ws': { target, ws: true, changeOrigin: true },
            '/login': { target, changeOrigin: true },
            '/logout': { target, changeOrigin: true },
          },
        }
      : undefined,
  };
});
