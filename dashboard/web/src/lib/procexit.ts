/* How the members of a repeated run ended, as the process table reads them.
 *
 * The service files each member of a run under one fate (proctree.py `_group_dict`): still
 * running when the collector stopped (`alive`), ended by a signal (`signals`), an exit code
 * (`exits`), or none seen. The table (ProcTree.tsx `groupExit`) says a signal first, then a
 * non-zero exit, then exit 0; the diagram's box says the same, and "all exit 0" only when every
 * member exited 0 -- a run killed by a signal, or still running, has no non-zero code and is
 * not one.
 */
import type { ProcGroup } from '../api/types';
import { fint } from './format';

export interface GroupEnd {
  signalled: number;
  /** a non-zero exit code */
  failed: number;
  /** exit 0 */
  ok: number;
  alive: number;
  /** no exit seen */
  unseen: number;
}

type Ends = Pick<ProcGroup, 'count' | 'exits' | 'signals' | 'alive'>;

const sum = (m: Record<string, number> | null | undefined) => Object.values(m ?? {}).reduce((a, n) => a + n, 0);

export function groupEnd(g: Ends): GroupEnd {
  const ok = g.exits?.['0'] ?? 0;
  const failed = sum(g.exits) - ok;
  const signalled = sum(g.signals);
  const alive = g.alive ?? 0;
  return { signalled, failed, ok, alive, unseen: Math.max(0, g.count - ok - failed - signalled - alive) };
}

/** A run's ending in a box's words, and whether it is a failure to mark. */
export function groupEndWords(g: Ends): [string, boolean] {
  const e = groupEnd(g);
  if (e.signalled) return [`${fint(e.signalled)} ended by a signal${e.failed ? `, ${fint(e.failed)} failed` : ''}`, true];
  if (e.failed) return [`${fint(e.failed)} failed`, true];
  if (g.count > 0 && e.ok === g.count) return ['all exit 0', false];
  const parts = [
    e.ok ? `${fint(e.ok)} exit 0` : null,
    e.alive ? `${fint(e.alive)} running at the end` : null,
    e.unseen ? `${fint(e.unseen)} no exit seen` : null,
  ].filter(Boolean);
  return [parts.join(' · ') || 'no exit seen', false];
}
