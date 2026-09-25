/* Panel lookup.
 *
 * A key the backend did not send is not an error and not an empty chart: it is
 * an absent feed, and it gets an envelope that says so. `pick` therefore always
 * returns something renderable, and the caller's JSX must be null-safe because
 * React evaluates children before <Panel> decides to show <EmptyState>.
 */
import type { PanelEnvelope } from '../api/types';

export type Panels = Record<string, PanelEnvelope & Record<string, unknown>>;

export function absent(feed: string, why = 'the backend did not return this panel'): PanelEnvelope {
  return { _feed: feed, _status: 'missing', _present: false, _paths: [], _notice: why };
}

/** The payload for `key`, or an absent-feed envelope carrying the same shape. */
export function pick<T extends PanelEnvelope>(P: Panels | undefined, key: string, feed: string): T {
  const p = P?.[key];
  if (p) return p as unknown as T;
  return absent(feed) as unknown as T;
}

/** How a plate should name the span it counted over.
 *
 * Read off the PANEL's own envelope rather than off the picker, so a plate can
 * never name a window the numbers beside it were not reduced over -- during a
 * rebuild those two genuinely differ, and the plate is the one that must be
 * right. Falls back to the bare phrase when the backend sent no window, which
 * is what an older service or the absent-feed envelope looks like.
 */
export function winLabel(p: PanelEnvelope | null | undefined): string {
  return p?._window?.label ? `last ${p._window.label}` : 'in window';
}

/** Class keys present in a by_class map, canonical order, intersected with the filter. */
export function shown<T>(
  by: Record<string, T> | undefined | null,
  cls: readonly string[],
): string[] {
  if (!by) return [];
  return cls.filter((c) => c in by);
}
