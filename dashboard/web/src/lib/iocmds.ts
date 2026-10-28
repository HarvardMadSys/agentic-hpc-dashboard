/* Which I/O number's commands are open: one at a time, for the whole page.
 *
 * A panel opens it (`openIoCmds`) with the number's own filters; the drawer
 * the shell mounts once (components/IoCmds.tsx) reads it and adds the page's:
 * the window, the class chips and the user and agent-type filter, unless the
 * number names its own. */
import { useSyncExternalStore } from 'react';
import type { IoSort } from '../api/types';

export interface IoQuery {
  /** what was clicked, in words, for the drawer's head */
  title: string;
  /** the column the commands open sorted by: the quantity that was clicked */
  sort: IoSort;
  class?: string[];
  agent_type?: string[];
  user?: string[];
  /** effective command names (`_eff`), as Top tools and Reads by tool name them */
  tool?: string[];
  /** tool kinds (Tool mix) */
  bucket?: string[];
  /** a blast-radius row: one agent's processes (and its subagents') on one host */
  host?: string;
  apid?: number[];
  /** ... that ran in this directory or below it */
  cwd?: string;
  /** a time bin, or the panel's covered range; the page's window when absent */
  from?: string | number | null;
  to?: string | number | null;
  /** the live tier: the class chips and the user filter do not narrow it */
  unfiltered?: boolean;
}

let cur: IoQuery | null = null;
const subs = new Set<() => void>();

export function openIoCmds(q: IoQuery): void {
  cur = q;
  subs.forEach((f) => f());
}

export function closeIoCmds(): void {
  cur = null;
  subs.forEach((f) => f());
}

export function useIoCmds(): IoQuery | null {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => cur,
  );
}

/** Where a panel's window starts, for a number summed over it. A built view
 *  keeps taking records as they arrive (`drift_s`), so it ends now, as the
 *  lookup does when it is given no end. */
export function coveredBy(p: { _window?: { covers_from?: string | null } | null } | null | undefined):
  Pick<IoQuery, 'from'> {
  const w = p?._window;
  return w?.covers_from ? { from: w.covers_from } : {};
}
