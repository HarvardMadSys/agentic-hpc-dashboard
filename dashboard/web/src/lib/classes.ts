/* Three classes, never two: agent, human, IDE.
 *
 * Folding the IDE class into `agent` is what produced the retracted GPU
 * near-parity claim, so the class list is closed and the labels are explicit,
 * and written out in full wherever they appear: an abbreviation is one more
 * thing to decode. `ide` is any editor a person drives -- VS Code, Cursor,
 * Windsurf, and the other remote-IDE servers -- whatever AI features it has;
 * an agent CLI started inside one stays `agent`. `unlabeled` is a fourth bin
 * that is rendered when present but is NOT one of the three physical classes
 * and is never merged into them.
 */
import type { Cls } from '../api/types';

export const CLS: Cls[] = ['agent', 'human', 'ide'];

export const CLS_LBL: Record<string, string> = {
  agent: 'agent',
  human: 'human',
  ide: 'IDE',
  unlabeled: 'unlabeled',
};

/** Crimson is RESERVED for `agent`. Do not reuse it for any other series. */
export const CLS_COL: Record<string, string> = {
  agent: 'var(--s1)',
  human: 'var(--s3)',
  ide: 'var(--s2)',
  unlabeled: 'var(--sg)',
  mixed: 'var(--s7)',
  unknown: 'var(--sg)',
  job: 'var(--s4)',
  other: 'var(--sg)',
};

/** The agent or editor a process belongs to, as people call it. */
export const AGENT_TYPE_LBL: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
  copilot: 'Copilot',
  cursor: 'Cursor',
  windsurf: 'Windsurf',
  vscode: 'VS Code',
  bwrap: 'sandbox wrapper',
  '': 'untagged',
};
