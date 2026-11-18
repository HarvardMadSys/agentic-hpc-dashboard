/* When the page tries the live socket again, and when it stops trusting one.
 *
 * A socket that fails is tried again for as long as the page is open, each wait
 * twice the one before, up to RETRY_CAP_MS: a service that restarts, or a
 * network that comes back, is live again without a reload. Each wait is spread
 * over its upper half, so the readers one restart dropped together do not all
 * knock again in the same second. After POLL_AFTER failures in a row the page
 * also polls, and goes on trying the socket meanwhile.
 *
 * An open socket can die without closing (a laptop asleep, a proxy that drops
 * the connection silently): the service sends a `feeds` frame every 30 s
 * (app.py `feeds_loop`), so one that has said nothing for QUIET_MS is taken
 * for dead, closed, and replaced.
 */

/** The first wait, and the longest. */
export const RETRY_BASE_MS = 1000;
export const RETRY_CAP_MS = 30000;
/** Failed attempts in a row before the page polls as well: the fifth, as before
 *  it was retried for good, 8 to 15 s of waits after the first. */
export const POLL_AFTER = 5;
/** No frame of any kind for this long, and an open socket is taken for dead: two
 *  and a half of the service's 30 s `feeds` beats. */
export const QUIET_MS = 75000;

/** The wait before try `n` of the socket (1 for the first after a failure):
 *  RETRY_BASE_MS doubled each time, capped at RETRY_CAP_MS, and placed in its
 *  upper half by `spread` (0 to 1). */
export function retryDelay(n: number, spread: number): number {
  const d = Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** Math.min(Math.max(0, n - 1), 30));
  const s = Number.isFinite(spread) ? Math.min(1, Math.max(0, spread)) : 0;
  return Math.round(d / 2 + (d / 2) * s);
}

/** The spread of try `n`, 0 to 1, from `seed`: the moment the page opened
 *  (`performance.timeOrigin`), which differs from page to page, so pages one
 *  restart dropped together come back apart. A hash, not a random number: the
 *  page draws none (`npm run check:no-random`). */
export function spreadOf(seed: number, n: number): number {
  let h = (Math.floor(seed) ^ Math.imul(n + 1, 0x9e3779b1)) >>> 0;
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 2 ** 32;
}

/** The ready windows of a `windows` answer -- a frame or `/api/windows` -- as one
 *  key: a new key is a build that finished, and the panels have something new
 *  to say. Only `ready` rows, so a progress frame changes nothing; `installed_at`
 *  too, as a window resumed from a saved build keeps that build's `built_at`. */
export function readyKey(held: readonly { minutes: number; state: string; built_at: string | null; installed_at?: number | null }[] | null | undefined): string {
  return (held ?? [])
    .filter((w) => w.state === 'ready')
    .map((w) => `${w.minutes}:${w.built_at}:${w.installed_at ?? ''}`)
    .join('|');
}
