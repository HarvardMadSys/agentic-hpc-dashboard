/* The kinds of place a path outside an agent's start folder lands in, as the
 * service names them (`reducers/workspace.py:_root`), and how alarming a write
 * there is. One table for the Agents tab's flags and the Security tab's writes
 * outside the start folder, so the two never rank a place differently.
 *
 * A write to the agent's own scratch in /tmp is still outside the project, but
 * it is not drawn like a write into another user's home.
 */
import type { WsKind } from '../api/types';
import { table } from './lookup';

/** Indexed by the service's `kind`, so a table with no prototype (lib/lookup.ts). */
export const PLACE_SEVERITY: Record<WsKind, 'high' | 'med' | 'low'> = table({
  "other user's home": 'high',
  system: 'high',
  'filesystem root': 'high',
  'shared storage': 'med',
  'own home': 'med',
  other: 'med',
  home: 'low',
  'cache & tooling': 'low',
  'agent state': 'low',
  temp: 'low',
});

/** Most alarming first, the order the service sorts an agent's flags in
 *  (`workspace.py:_SEVERITY`). A kind it does not name goes with `other`. */
const ORDER: WsKind[] = [
  "other user's home",
  'system',
  'filesystem root',
  'shared storage',
  'own home',
  'other',
  'cache & tooling',
  'agent state',
  'temp',
  'home',
];

export function placeRank(kind: string): number {
  const i = ORDER.indexOf(kind as WsKind);
  return i < 0 ? ORDER.indexOf('other') : i;
}

/** A kind in the page's words: "another user's home", as the Agents tab and
 *  the glossary (`otherhome`) say it; any other kind as the service names it. */
const PLACE_LBL: Record<string, string> = table({ "other user's home": 'another user’s home' });
export const placeLabel = (kind: string): string => PLACE_LBL[kind] ?? kind;
