/* Three classes, never two.
 *
 * Folding `human-vscode` into `agent` is what produced the retracted GPU
 * near-parity claim, so the class list is closed and the labels are explicit.
 * `unlabeled` is a fourth bin that is rendered when present but is NOT one of
 * the three physical classes and is never merged into them.
 */
import type { Cls, ClsAny } from '../api/types';

export const CLS: Cls[] = ['agent', 'human-vscode', 'human'];

export const CLS_LBL: Record<string, string> = {
  agent: 'agent',
  'human-vscode': 'human in VS Code',
  human: 'human (shell)',
  unlabeled: 'unlabeled',
};

/** Short forms for dense table cells. */
export const CLS_ABBR: Record<string, string> = {
  agent: 'agt',
  'human-vscode': 'vsc',
  human: 'hum',
  unlabeled: '--',
};

/** Crimson is RESERVED for `agent`. Do not reuse it for any other series. */
export const CLS_COL: Record<string, string> = {
  agent: 'var(--s1)',
  'human-vscode': 'var(--s2)',
  human: 'var(--s3)',
  unlabeled: 'var(--sg)',
  mixed: 'var(--s7)',
  unknown: 'var(--sg)',
  job: 'var(--s4)',
  other: 'var(--sg)',
};

export const clsLabel = (c: string): string => CLS_LBL[c] ?? c;
export const clsColor = (c: string): string => CLS_COL[c] ?? 'var(--sg)';

/** Class keys present in a `by_class`-style map, in canonical order, tail last. */
export function classKeys(m: Record<string, unknown> | undefined | null): ClsAny[] {
  if (!m) return [];
  const out: ClsAny[] = CLS.filter((c) => c in m);
  if ('unlabeled' in m) out.push('unlabeled');
  return out;
}
