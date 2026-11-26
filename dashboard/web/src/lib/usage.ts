/* Which tabs readers open, counted for the site's own use (rc_dashboard/usage.py,
 * `rc-dashboard usage`): a tab's key and how many times it was opened, posted at most
 * once a minute and when the page is put away. Never a user, a window, a filter or a
 * time: the service keeps a count a day. Nothing is sent until /api/config says the
 * site counts (`ui.count_views`), and nothing where it says not, or where a service
 * from before it says neither.
 */
import { API_BASE } from '../api/client';

/** How long opens are gathered before they are posted, in ms. */
export const FLUSH_MS = 60_000;
/** A tab's key, as `?tab=` writes it and the service takes it (usage.VIEW). */
const KEY = /^[a-z][a-z0-9_-]{0,31}$/;
/** The service's own limit on one view's count in one post (usage.MAX_COUNT). */
const MAX_COUNT = 1000;

const pending = new Map<string, number>();
let on: boolean | null = null; // unknown until /api/config says
let timer: ReturnType<typeof setTimeout> | null = null;

/** One opening of the tab `key`. */
export function countTab(key: string): void {
  if (on === false || !KEY.test(key)) return;
  const v = `tab:${key}`;
  pending.set(v, Math.min(MAX_COUNT, (pending.get(v) ?? 0) + 1));
  schedule();
}

/** Whether the site counts (`ui.count_views`); off drops what was gathered. */
export function setCounting(v: boolean): void {
  on = v;
  if (!v) {
    pending.clear();
    if (timer) clearTimeout(timer);
    timer = null;
  } else schedule();
}

/** The opens gathered since the last post, as the post carries them, and none kept. */
export function takePending(): Record<string, number> | null {
  if (!pending.size) return null;
  const views = Object.fromEntries(pending);
  pending.clear();
  return views;
}

function schedule(): void {
  if (on !== true || timer || !pending.size) return;
  timer = setTimeout(() => {
    timer = null;
    flush();
  }, FLUSH_MS);
}

/** Post what was gathered, if the site counts; a failed post loses its counts, which
 *  is all a count of tabs opened is worth. `keepalive`, so a post made as the page is
 *  put away still goes. */
export function flush(): void {
  if (on !== true) return;
  const views = takePending();
  if (!views) return;
  try {
    void fetch(API_BASE + '/usage', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ views }),
      keepalive: true,
      credentials: 'same-origin',
    }).catch(() => undefined);
  } catch {
    /* no fetch: nothing is counted */
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush();
  });
}
