/* What kind of session an agent's phase block says it was (`PhaseBlock.category`, lib/phases.ts
 * CATEGORIES), as a chip: its short name, with a dot of the colour of the phases its use counts, and
 * on hover how it is decided, the shares of its work, and the session's summary. A block, or a
 * user's session that carries its block's category (`SessionKind`), is drawn alike, so a session
 * reads one way on the Sessions tab and the Users tab. A session no agent ran that ran no command is
 * `no commands`, as an agent's that made no tool call is `no tool calls`. A session that made no call, or whose
 * commands were not recorded, is drawn dashed, as everything unread is. With no category, as for a
 * row with no block, or a block from a service that predates categories, it draws nothing. */
import type { SessionKind } from '../api/types';
import { categoryOf, categoryTip, driverOf } from '../lib/phases';

export function CategoryChip({ of }: { of: SessionKind | null | undefined }) {
  const c = of?.category;
  if (!of || !c) return null;
  const [short, , colour] = categoryOf(c, driverOf(of));
  return (
    <span className={`sess-cat sess-cat-${c}`} data-tip={categoryTip(of)} data-tip-wide>
      {colour !== 'none' && <i style={{ background: colour }} />}
      {short}
    </span>
  );
}
