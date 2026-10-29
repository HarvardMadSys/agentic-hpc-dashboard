/* Where an agent session is driven from, as a chip: its group (CLI or app) and
 * its surface, with what that means on hover (lib/surfaces.ts). An unknown one
 * is drawn dashed, as everything unread is; a process that is not an agent
 * session has none and draws nothing. */
import type { Surface } from '../api/types';
import { SURFACE_GROUP, surfaceShort, surfaceTip } from '../lib/surfaces';

export function SurfaceChip({ s, detail }: { s: Surface | null | undefined; detail?: string | null }) {
  if (!s) return null;
  const g = SURFACE_GROUP[s];
  return (
    <span className={`surf surf-${g ? g.toLowerCase() : 'unknown'}`} data-tip={surfaceTip(s, detail)}>
      {/* a span, not <b>: the headers it sits in style their own <b> */}
      {g && <span className="surf-g">{g}</span>}
      {surfaceShort(s, detail)}
    </span>
  );
}
