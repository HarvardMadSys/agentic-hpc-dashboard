/* Overview -- one table: each class's share of the node, in every unit.
 *
 * Its headline plates moved to Live, under "right now", and its time series to
 * Live's charts (roadmap notes 21-23); what stays is the comparison only this
 * tab makes. The units deliberately disagree -- a single unit lets a few loud
 * users stand in for a population -- so processes, CPU, I/O, memory, and
 * sessions sit side by side, each row sortable by any class's share.
 */
import type { Cls, IoProcess, IoSort, LiveResponse, Resources, SlurmClass, SlurmPanel, ToolMix, Trajectories } from '../api/types';
import { Basis, Panel } from '../components/Panel';
import { ShareCell } from '../components/ShareCell';
import { SortTh } from '../components/SortTh';
import { Term } from '../components/Term';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb } from '../lib/format';
import { GLOSS } from '../lib/glossary';
import { pick, winLabel } from '../lib/panels';
import { useSort } from '../lib/useSort';
import { Fragment } from 'react';
import type { Panels } from '../lib/panels';
import { coveredBy } from '../lib/iocmds';
import { IoNum } from '../components/IoCmds';

/** One row per unit: the whole point is that the ranking changes between them. */
interface UnitRow {
  k: string;
  get: (c: string) => number | null;
  fmt?: (n: number | null) => string;
  tip?: string;
  /** where the quantity lives: the login node itself, or the jobs it asked Slurm for */
  group?: 'node' | 'slurm';
  /** a class's cell on hover: the single process behind a peak (roadmap note 27) */
  cellTip?: (c: string) => string | undefined;
  /** a byte quantity: a click on a class's cell lists every command behind it */
  io?: IoSort;
}

const GROUPS: Record<NonNullable<UnitRow['group']>, [string, string]> = {
  node: ['on this host', 'measured on the host the collector runs on'],
  slurm: [
    'asked of Slurm',
    'what the jobs submitted from these hosts REQUESTED on their sbatch / srun / salloc command lines. ' +
      'A request in the script\'s #SBATCH lines is not seen, and GPU use on the compute nodes is not measured: ' +
      'the collector watches only these hosts, and the scheduler\'s accounting (sacct) is not read here',
  ],
};

export function OverviewTab({
  P,
  cls,
  traj,
}: {
  P: Panels;
  live: LiveResponse | null;
  cls: Cls[];
  traj: Trajectories | null;
}) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const sl = pick<SlurmPanel>(P, 'slurm', 'ebpfm');
  const win = winLabel(tm);
  const res = pick<Resources>(P, 'resources', 'ebpfm');
  const io = pick<IoProcess>(P, 'io_process', 'ebpfm');

  const tmBy = tm.by_class ?? {};
  const resBy = res.by_class ?? {};
  const ioBy = io.by_class ?? {};
  const have = cls.filter((c) => c in tmBy || c in resBy || c in ioBy);

  // Peak resident memory per class (GB), from the resources series' samples.
  const peakGb = (c: string): number | null => {
    const v = (res.series?.resident?.[c]?.rss_mb ?? []).filter((x): x is number => x != null);
    return v.length ? Math.max(...v) / 1024 : null;
  };
  // A class with no Slurm record in a window the panel was reduced over asked
  // for nothing -- a measured 0; with no panel at all, nothing is known.
  const slurmOf = (c: string, f: (s: SlurmClass) => number | null) =>
    !sl._present ? null : sl.by_class?.[c] ? f(sl.by_class[c]) : 0;
  // The one process behind a class's heaviest value, in words.
  const topTip = (c: string, metric: string, f: (v: number) => string, what: string) => {
    const t = resBy[c]?.top?.[metric];
    return t
      ? `${what}: ${f(t.value)}, ${t.tool ?? '?'} by ${t.user ?? '?'} (pid ${t.pid ?? '?'}${t.host ? ' on ' + t.host : ''})`
      : undefined;
  };
  const netOf = (c: string, k: 'net_tx_mb' | 'net_rx_mb') => {
    const a = ioBy[c]?.[k];
    return a?.n ? a.total ?? null : null;
  };
  const units: UnitRow[] = ([
    { k: 'processes exited', get: (c) => tmBy[c]?.calls ?? null,
      tip: GLOSS.exited + '; a shell is counted as the command it ran' },
    { k: 'CPU time', get: (c) => {
        const v = tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s;
        return v == null ? null : v / 3600;
      }, fmt: (n) => (n == null ? DASH : fmt(n) + ' h'), tip: GLOSS.cpuh,
      cellTip: (c) => topTip(c, 'cpu_s', dur, 'most CPU in one process') },
    { k: 'disk read', get: (c) => ioBy[c]?.rd_mb ?? null, fmt: (n) => mb(n), tip: GLOSS.diskbytes, io: 'rd_mb' },
    { k: 'disk written', get: (c) => ioBy[c]?.wr_mb ?? null, fmt: (n) => mb(n), tip: GLOSS.diskbytes, io: 'wr_mb' },
    { k: 'network sent', get: (c) => netOf(c, 'net_tx_mb'), fmt: (n) => mb(n), tip: GLOSS.tx, io: 'tx_mb' },
    { k: 'network received', get: (c) => netOf(c, 'net_rx_mb'), fmt: (n) => mb(n), tip: GLOSS.rx, io: 'rx_mb' },
    { k: 'peak memory', get: peakGb, fmt: (n) => (n == null ? DASH : fmt(n) + ' GB'),
      tip: 'the most resident memory the class held at once in the window, sampled while processes ran; hover a cell for the single largest process',
      cellTip: (c) => topTip(c, 'peak_rss_mb', (v) => mb(v), 'largest single process') },
    { k: 'sessions', get: (c) => traj?.summary?.[c]?.sessions ?? null,
      tip: GLOSS.session + '; scripts that only probe an agent are not counted' },
    { k: 'jobs submitted', group: 'slurm', get: (c) => slurmOf(c, (x) => x.jobs),
      tip: 'sbatch / srun / salloc submissions that came back with a job id; dry runs (--test-only) are not jobs' },
    { k: 'GPUs asked', group: 'slurm', get: (c) => slurmOf(c, (x) => x.gpus),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU${n === 1 ? '' : 's'}`),
      tip: 'GPUs on the command line (--gres gpu:N, --gpus), times the tasks of an array; a #SBATCH request in the script is not seen\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)} GPU jobs`).join(' · ') },
    { k: 'GPU-hours asked', group: 'slurm', get: (c) => slurmOf(c, (x) => (x.gpu_jobs ? x.gpu_h : 0)),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU·h`),
      tip: GLOSS.gpuh + '\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_h_known ?? 0)} of ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)}`).join(' · ') },
  ] as UnitRow[]).filter((u) => have.some((c) => u.get(c) != null));

  // Sort by a class's SHARE of each unit (the only thing comparable across rows
  // of different units), within each group, so "asked of Slurm" stays together.
  const sort = useSort<string>();
  const shareOf = (u: UnitRow, c: string) => {
    const tot = have.reduce((a, x) => a + (u.get(x) ?? 0), 0);
    const v = u.get(c);
    return v == null || !tot ? null : (100 * v) / tot;
  };
  const sorted = (['node', 'slurm'] as const).flatMap((g) =>
    sort.sort(units.filter((u) => (u.group ?? 'node') === g), (u, k) => (k === 'unit' ? u.k : shareOf(u, k))),
  );

  return (
    <div className="grid wide">
      <Panel
        title={`Who uses what · ${win}`}
        ask={[
          { panel: 'tool_mix', keys: ['by_class'] },
          { panel: 'resources', keys: ['by_class', 'top', 'oncpu_set_aside'] },
          { panel: 'io_process', keys: ['by_class'] },
          { panel: 'slurm', keys: ['by_class'] },
          { panel: 'live', keys: ['now'] },
        ]}
        p={tm}
        span
        empty={!units.length}
        emptyWhat="no unit is populated for the selection"
        basis={
          <Basis
            items={[
              [
                'why',
                'the class ranking is not stable across units: a unit that de-weights the loudest users routinely erases or flips the per-process gap',
              ],
            ]}
          />
        }
      >
        {/* A table, not a stacked bar per measure: each class keeps its own
            track, so the IDE's 0.3% of the processes is still there to read. */}
        <div className="scroller">
          <table className="roomy shares">
            <thead>
              <tr>
                <SortTh s={sort} k="unit" first="asc">each class's share of</SortTh>
                {have.map((c) => (
                  <SortTh key={c} s={sort} k={c} className="cls" style={{ color: CLS_COL[c] }}>
                    {CLS_LBL[c] ?? c}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((u, i) => {
                const tot = have.reduce((a, c) => a + (u.get(c) ?? 0), 0);
                const g = u.group ?? 'node';
                const head = i === 0 || (sorted[i - 1].group ?? 'node') !== g;
                return (
                  <Fragment key={u.k}>
                  {head && (
                    <tr className="grp">
                      <td colSpan={have.length + 1} data-tip={GROUPS[g][1]}>
                        {GROUPS[g][0]}
                        {g === 'slurm' && <span className="nm"> · requests, not use: GPU use on the compute nodes is not measured</span>}
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td><Term tip={u.tip}>{u.k}</Term></td>
                    {have.map((c) => {
                      const v = u.get(c);
                      return (
                        <td key={c} data-tip={u.cellTip?.(c)}>
                          {u.io && v ? (
                            <IoNum q={{ title: `${u.k} · ${CLS_LBL[c] ?? c}`, sort: u.io, class: [c], ...coveredBy(io) }}>
                              <ShareCell pct={tot ? (100 * v) / tot : null} sub={u.fmt ? u.fmt(v) : fint(v)} c={CLS_COL[c]} />
                            </IoNum>
                          ) : (
                            <ShareCell
                              pct={v == null || !tot ? null : (100 * v) / tot}
                              sub={u.fmt ? u.fmt(v) : fint(v)}
                              c={CLS_COL[c]}
                            />
                          )}
                        </td>
                      );
                    })}
                  </tr>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
