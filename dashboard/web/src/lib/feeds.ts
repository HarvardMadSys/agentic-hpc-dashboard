/* Plain-English names for the feeds.
 *
 * A panel names the feed that fills it, so a reader can tell a real zero from a
 * feed that never arrived. That only works if the name means something to them:
 * the raw key is a collector slug (`ebpf`, `ebpf_node`), which tells an
 * operator everything and a reader nothing. The slug is still the identifier
 * everywhere else -- the URL, the Feeds tab, `--check-feeds` -- so it stays one
 * hover away rather than being dropped.
 */
import { table } from './lookup';
export const FEED_LBL: Record<string, string> = table({
  ebpf: 'process tracer',
  ebpfm: 'process tracer',
  ebpf_node: 'node metrics',
  live: 'live stream',
});

export const feedLabel = (f: string): string => FEED_LBL[f] ?? f;
