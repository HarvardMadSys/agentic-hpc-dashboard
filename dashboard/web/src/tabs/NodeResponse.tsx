/* How quickly each login node answered, over the window (`node.series`).
 *
 * The node tier's probes -- start a program, look an account up -- and the
 * slowest NFS mount's round trip and the load, per host, on Live beside the
 * window's CPU chart and zoomed with it: "do agents make the node slow for
 * people?" is read by putting a spike here under the CPU above it.
 *
 * A latency bin is the slowest probe in it, a load bin its mean: a person
 * feels the worst moment, not the average. Bins are at least twice the
 * interval the snapshots arrive at (rc_dashboard/nodehist.py), so a hole is a
 * missing snapshot, never a sample that drifted over a bin edge.
 *
 * Hosts are not classes, so their lines take hues no class owns. There are
 * five such hues: past five hosts, the five that stand out most are named on
 * every chart, and the rest are one grey line, the slowest of them in each
 * bin (or the one host left, by name).
 */
import type { NodeField, NodePanel } from '../api/types';
import { TimeChart, anyValue, binLabel } from '../charts/TimeChart';
import type { TimeLine, View } from '../charts/TimeChart';
import { NoRows, Panel } from '../components/Panel';
import { Term } from '../components/Term';
import { GLOSS } from '../lib/glossary';
import { dur, fint, mb } from '../lib/format';
import { useNodeWords } from '../lib/nodes';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';

/** Hosts are not classes: hues no class owns (crimson, blue and green are
 *  agent, IDE and human), neighbours far apart, one per named host. */
const HOST_COL = ['var(--s4)', 'var(--s6)', 'var(--s8)', 'var(--s7)', 'var(--s5)'];
const NAMED = HOST_COL.length;

const CHARTS: { k: NodeField; eyebrow: string; tip: string; ylabel: string; yname: string }[] = [
  { k: 'fork_exec_ms', eyebrow: 'time to start a process', tip: GLOSS.forkexec, ylabel: 'ms, slowest in the bin', yname: 'start ms' },
  { k: 'getent_ms', eyebrow: 'time to look up an account', tip: GLOSS.getent, ylabel: 'ms, slowest in the bin', yname: 'lookup ms' },
  { k: 'nfs_rd_rtt_ms', eyebrow: 'NFS read round trip, slowest mount', tip: GLOSS.nfsrtt, ylabel: 'ms, slowest in the bin', yname: 'NFS read ms' },
  { k: 'load_1', eyebrow: 'load average, 1 minute', tip: GLOSS.load, ylabel: 'load, mean of the bin', yname: 'load' },
];

export function NodeResponse({ P, win, view, onView }: {
  P: Panels;
  win: string;
  view: View;
  onView: (v: View) => void;
}) {
  const nodes = useNodeWords();
  const n = pick<NodePanel>(P, 'node', 'ebpf_node');
  const ns = n.series;
  const hosts = Object.keys(ns?.hosts ?? {}).sort();
  const s = ns && ns.t?.length ? { t: ns.t, bin_s: ns.bin_s } : null;

  // One set of named hosts for all four charts, so a host keeps its hue on
  // each: every host when they fit, else the NAMED that stand out most -- by
  // their highest bin against the hosts' median highest, on whichever chart
  // they stand out on -- and the rest in grey.
  const peak = (h: string, k: NodeField) => Math.max(0, ...(ns?.hosts[h]?.[k] ?? []).map((v) => v ?? 0));
  const score = (h: string) => Math.max(...CHARTS.map((c) => {
    const ps = hosts.map((x) => peak(x, c.k)).sort((a, b) => a - b);
    const mid = ps[Math.floor(ps.length / 2)] || 1e-9;
    return peak(h, c.k) / mid;
  }));
  const named = (hosts.length <= NAMED ? hosts : [...hosts].sort((a, b) => score(b) - score(a) || a.localeCompare(b)).slice(0, NAMED)).sort();
  const rest = hosts.filter((h) => !named.includes(h));

  const linesOf = (k: NodeField): TimeLine[] => {
    if (!ns) return [];
    const lines: TimeLine[] = named.map((h, i) => ({ lbl: h, c: HOST_COL[i], v: ns.hosts[h]?.[k] }));
    if (rest.length === 1) {
      lines.push({ lbl: rest[0], c: 'var(--sg)', v: ns.hosts[rest[0]]?.[k] });
    } else if (rest.length) {
      lines.push({
        lbl: `the other ${rest.length}, slowest of them`, c: 'var(--sg)',
        v: ns.t.map((_, i) => {
          const vals = rest.map((h) => ns.hosts[h]?.[k]?.[i]).filter((v): v is number => v != null);
          return vals.length ? Math.max(...vals) : null;
        }),
      });
    }
    return lines;
  };

  const reading = ns?.state !== 'ready' && ns?.state != null;
  const pill = ns
    ? `${binLabel(ns.bin_s)} bins${ns.interval_s ? ` · a snapshot every ${dur(ns.interval_s)}` : ''}`
    : '';

  return (
    <Panel
      title={`${nodes.One} response over the ${win}`}
      id="sec-node-response"
      ask={[{ panel: 'node', keys: ['hosts'] }]}
      p={n}
      span
      empty={!s || !hosts.length}
      emptyWhat={ns?._error
        ? `the node history could not be read: ${ns._error}`
        : reading
          ? `reading the node tier’s history: ${fint(ns?.read.lines)} snapshots in ${fint(ns?.read.files)} files so far`
          : `no ${nodes.one} snapshot in the window`}
      header={ns && (
        <span className="pill" data-tip={`each chart is binned this finely, and zooms with the window’s charts above; the probes are the node tier’s own, every snapshot`}>
          {pill}
        </span>
      )}
    >
      {reading && (
        <p className="nm" style={{ margin: '0 0 8px' }}>
          still reading the node tier’s history ({fint(ns?.read.lines)} snapshots, {mb((ns?.read.bytes ?? 0) / 1e6)} so far): the
          older part of the window fills in as it does
        </p>
      )}
      <div className="pair-row">
        {CHARTS.map((c) => {
          const lines = linesOf(c.k);
          return (
            <div key={c.k}>
              <div className="eyebrow"><Term tip={c.tip}>{c.eyebrow}</Term></div>
              {s && anyValue(lines) ? (
                <TimeChart s={s} lines={lines} ylabel={c.ylabel} yname={c.yname} view={view} onView={onView} />
              ) : (
                <NoRows what={c.k === 'nfs_rd_rtt_ms' ? 'no NFS read in any sample of the window' : 'not reported in the window'} />
              )}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
