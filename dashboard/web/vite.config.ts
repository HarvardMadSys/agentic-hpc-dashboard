import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { mockApi } from './mock/plugin.ts';

/* The backend serves `web/dist/` as static files and answers `/api/*` and `/ws`
 * on the same origin, so asset URLs are relative (`base: './'`) and the API is
 * addressed absolutely at `/api` (override with VITE_API_BASE).
 *
 * Dev has two modes:
 *   npm run dev        -> /api and /ws answered from mock/api.json (no backend)
 *   npm run dev:real   -> /api and /ws proxied to RC_BACKEND (default :8787)
 */
export default defineConfig(({ mode }) => {
  const real = mode === 'real' || process.env.RC_MOCK === '0';
  const target = process.env.RC_BACKEND || 'http://127.0.0.1:8787';
  return {
    base: './',
    plugins: [react(), ...(real ? [] : [mockApi()])],
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
          },
        }
      : undefined,
  };
});
