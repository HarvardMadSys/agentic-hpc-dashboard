/* The process tree in a tab of its own (tabs/TreePopout.tsx): its link, and
 * the way back. The pop-out reads the session the way the inspector does, so
 * the link carries what that read is keyed on -- the session, the window,
 * where the read starts, and which end it draws -- and whether the routine
 * was hidden. */
import type { TrajOrder } from '../api/types';

export interface TreePop {
  sid: string | null;
  win: number | null;
  from: number | null;
  hide: boolean;
  order: TrajOrder;
}

export function treePopHref(
  sid: string,
  win: number | null,
  from: number | null,
  hide: boolean,
  order: TrajOrder = 'oldest',
): string {
  const p = new URLSearchParams({ pop: 'tree', traj: sid });
  if (win != null) p.set('win', String(win));
  if (from != null) p.set('tfrom', String(from));
  if (!hide) p.set('hide', '0');
  if (order === 'newest') p.set('torder', order);
  return `${window.location.pathname}?${p}`;
}

export function readTreePop(search: string): TreePop | null {
  const p = new URLSearchParams(search);
  if (p.get('pop') !== 'tree') return null;
  const num = (k: string) => {
    const v = Number(p.get(k));
    return Number.isFinite(v) && v > 0 ? v : null;
  };
  return {
    sid: p.get('traj') || null,
    win: num('win'),
    from: num('tfrom'),
    hide: p.get('hide') !== '0',
    order: p.get('torder') === 'newest' ? 'newest' : 'oldest',
  };
}

/** The same session in the dashboard's inspector. */
export function dashboardHref(t: TreePop): string {
  const p = new URLSearchParams({ tab: 'trajectories' });
  if (t.sid) p.set('traj', t.sid);
  if (t.win != null) p.set('win', String(t.win));
  if (t.from != null) p.set('tfrom', String(t.from));
  if (t.order === 'newest') p.set('torder', t.order);
  return `${window.location.pathname}?${p}`;
}
