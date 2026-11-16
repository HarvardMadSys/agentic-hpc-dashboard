/* "Now", for text that is read against the clock: "last process 2 min ago", the
 * live edge of a running session, which runs on to the present.
 *
 * Such text used to stay current only because App re-rendered every tab with
 * every live frame. The tabs and their heavy parts are memoized now, so a
 * component that reads the clock asks for it here: it renders again with each
 * live frame (App provides when the newest one came), and nothing else does.
 *
 * Each live frame also says whether the service's clock is pinned to a frozen
 * capture (lib/clock.ts `clockPinned()`), which words the phase and edge texts
 * of lib/phases.ts and lib/tasks.ts: "not known" against "not known yet", "was
 * still running when the record ended" against "is still running". A component
 * that writes such text calls `useClockPinned`, and renders again when it flips.
 */
import { createContext, use } from 'react';
import { clockPinned, serverNow } from './clock';

/** When the newest live frame came, epoch ms (App: `live.receivedAt`). */
export const FrameCtx = createContext<number | null>(null);
/** Whether the newest live frame said the service's clock is pinned (App). */
export const PinnedCtx = createContext<boolean>(false);

/** The service's present, epoch ms (`serverNow`), read again with every live
 *  frame while `on`. Off, it is read when the component renders for any other
 *  reason, as before; `use` may be called conditionally, a hook may not. */
export function useNow(on = true): number {
  if (on) use(FrameCtx);
  return serverNow();
}

/** `clockPinned()`, rendered again whenever the flag flips. */
export function useClockPinned(): boolean {
  use(PinnedCtx);
  return clockPinned();
}
