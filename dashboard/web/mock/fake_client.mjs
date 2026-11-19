/* `src/api/client.ts` as check_live.mjs answers it: every read logged, every answer the test's.
 *
 * Not a check itself, and nothing under `src/` imports it: check_live.mjs resolves useLive.ts's
 * `./client` here. The test sets `globalThis.__live` before loading the hook.
 */
const L = () => globalThis.__live;

export const wsUrl = () => 'ws://dashboard.test/ws';
export async function getLive(minutes) {
  L().reads.push('live');
  if (L().down) throw new Error('503 service unavailable');
  return L().live(minutes);
}
export async function getWindows() {
  L().reads.push('windows');
  if (L().down) throw new Error('503 service unavailable');
  return L().windows();
}
export async function getFeeds() {
  L().reads.push('feeds');
  if (L().down) throw new Error('503 service unavailable');
  return L().feeds();
}
