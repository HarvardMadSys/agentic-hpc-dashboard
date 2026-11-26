/* Overview -- one table: each class's share of the node, in every unit.
 *
 * Its headline plates moved to Live, under "right now", and its time series to
 * Live's charts (roadmap notes 21-23); what stays is the comparison only this
 * tab makes. The units deliberately disagree -- a single unit lets a few loud
 * users stand in for a population -- so processes, CPU, I/O, memory, and
 * sessions sit side by side, each row sortable by any class's share.
 */
import type { Cls, IoProcess, IoSort, Resources, SlurmClass, SlurmPanel, ToolMix, Trajectories } from '../api/types';
import { Panel } from '../components/Panel';
import { ShareCell } from '../components/ShareCell';
import { SplitBar } from '../components/SplitBar';
import type { SplitPart } from '../components/SplitBar';
import { SortTh } from '../components/SortTh';
import { Term } from '../components/Term';
import { CLS, CLS_COL, CLS_INK, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb } from '../lib/format';
import { GLOSS } from '../lib/glossary';
import { useNodeWords } from '../lib/nodes';
import type { NodeWords } from '../lib/nodes';
import { pick, winLabel } from '../lib/panels';
import { countedIn, noShare, shareOf, wholeOf } from '../lib/shares';
import { useSort } from '../lib/useSort';
import { Fragment, memo } from 'react';
import type { CSSProperties } from 'react';
import type { Panels } from '../lib/panels';
import { coveredBy } from '../lib/iocmds';
import { IoNum } from '../components/IoCmds';

/** One row per unit: the whole point is that the ranking changes between them. */
interface UnitRow {
  k: string;
  /** every class the quantity's panel counted, `unlabeled` too: a share is out
   *  of all of them, whatever the class chips show (lib/shares.ts) */
  all: string[];
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

const groupsOf = (nodes: NodeWords): Record<NonNullable<UnitRow['group']>, [string, string]> => ({
  node: [`on the ${nodes.many}`, `measured on the ${nodes.many} the collector runs on`],
  slurm: ['asked of Slurm', `what jobs submitted from these ${nodes.many} asked for on their command lines`],
});

export const OverviewTab = memo(function OverviewTab({
  P,
  cls,
  traj,
}: {
  P: Panels;
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
  const GROUPS = groupsOf(useNodeWords());
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
  const slBy = sl.by_class ?? {};
  const units: UnitRow[] = ([
    { k: 'processes exited', all: countedIn(tmBy), get: (c) => tmBy[c]?.calls ?? null,
      tip: GLOSS.exited },
    { k: 'CPU time', all: countedIn(tmBy, resBy), get: (c) => {
        const v = tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s;
        return v == null ? null : v / 3600;
      }, fmt: (n) => (n == null ? DASH : fmt(n) + ' h'), tip: GLOSS.cpuh,
      cellTip: (c) => topTip(c, 'cpu_s', dur, 'most CPU in one process') },
    { k: 'disk read', all: countedIn(ioBy), get: (c) => ioBy[c]?.rd_mb ?? null, fmt: (n) => mb(n), tip: GLOSS.rd, io: 'rd_mb' },
    { k: 'disk written', all: countedIn(ioBy), get: (c) => ioBy[c]?.wr_mb ?? null, fmt: (n) => mb(n), tip: GLOSS.wr, io: 'wr_mb' },
    { k: 'network sent', all: countedIn(ioBy), get: (c) => netOf(c, 'net_tx_mb'), fmt: (n) => mb(n), tip: GLOSS.tx, io: 'tx_mb' },
    { k: 'network received', all: countedIn(ioBy), get: (c) => netOf(c, 'net_rx_mb'), fmt: (n) => mb(n), tip: GLOSS.rx, io: 'rx_mb' },
    { k: 'peak memory', all: countedIn(res.series?.resident), get: peakGb, fmt: (n) => (n == null ? DASH : fmt(n) + ' GB'),
      tip: 'the most memory the class held at once in the window; hover a cell for the largest single process',
      cellTip: (c) => topTip(c, 'peak_rss_mb', (v) => mb(v), 'largest single process') },
    { k: 'sessions', all: countedIn(traj?.summary), get: (c) => traj?.summary?.[c]?.sessions ?? null,
      tip: GLOSS.session },
    { k: 'jobs submitted', group: 'slurm', all: countedIn(slBy), get: (c) => slurmOf(c, (x) => x.jobs),
      tip: 'sbatch / srun / salloc submissions that came back with a job id' },
    { k: 'GPUs asked', group: 'slurm', all: countedIn(slBy), get: (c) => slurmOf(c, (x) => x.gpus),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU${n === 1 ? '' : 's'}`),
      tip: 'GPUs asked on the command line; a #SBATCH request in a script is not seen\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)} GPU jobs`).join(' · ') },
    { k: 'GPU-hours asked', group: 'slurm', all: countedIn(slBy), get: (c) => slurmOf(c, (x) => (x.gpu_jobs ? x.gpu_h : 0)),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU·h`),
      tip: GLOSS.gpuh + '\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_h_known ?? 0)} of ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)}`).join(' · ') },
  ] as UnitRow[]).filter((u) => have.some((c) => u.get(c) != null));

  // Sort by a class's SHARE of each unit (the only thing comparable across rows
  // of different units), within each group, so "asked of Slurm" stays together.
  // A share is out of every class the unit's panel counted, not the ones the
  // chips show: turning the IDE off must not hand its share to the others.
  const sort = useSort<string>();
  const wholes = new Map(units.map((u) => [u, wholeOf(u.all, u.get)]));
  const shareIn = (u: UnitRow, c: string) => shareOf(u.get(c), wholes.get(u)!);
  const sorted = (['node', 'slurm'] as const).flatMap((g) =>
    sort.sort(units.filter((u) => (u.group ?? 'node') === g), (u, k) => (k === 'unit' ? u.k : shareIn(u, k))),
  );

  // The class with each unit's largest share, out of every class the unit
  // counted -- a class the chips hide and `unlabeled` too -- so turning the
  // leader off marks no other class in its place. None while the whole is not
  // known, or nothing was counted; every class at the top when they tie.
  const leads = new Map(units.map((u): [UnitRow, string[]] => {
    const w = wholes.get(u)!;
    if (w.unknown.length || !w.tot) return [u, []];
    let top = 0;
    let who: string[] = [];
    for (const c of u.all) {
      const v = u.get(c) ?? 0;
      if (v > top) [top, who] = [v, [c]];
      else if (v === top && top > 0) who.push(c);
    }
    return [u, who];
  }));

  // A unit's whole, split by class, in the classes' own order: every class it
  // counted, so the bar is the same whole whatever the chips show. A class the
  // chips hide is a neutral part, not a gap and not handed to the others.
  const ORDER: string[] = [...CLS, 'unlabeled'];
  const partsOf = (u: UnitRow): SplitPart[] =>
    [...u.all]
      .sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99))
      .map((c) => {
        const v = u.get(c) ?? 0;
        const shown = have.includes(c as Cls) || c === 'unlabeled';
        return {
          k: c,
          lbl: CLS_LBL[c] ?? c,
          v,
          amt: u.fmt ? u.fmt(v) : fint(v),
          c: CLS_COL[c] ?? 'var(--sg)',
          muted: !shown,
          note: !shown
            ? 'hidden by the class filter: still part of the whole'
            : c === 'unlabeled' ? 'counted, but in no class of its own' : undefined,
        };
      });

  return (
    <div className="grid wide">
      <Panel
        title={`Who uses what · ${win}`}
        // a fixed anchor: the title carries the window, and a link to the old
        // Overview tab lands here on Live
        id="sec-who-uses-what"
        p={tm}
        span
        empty={!units.length}
        emptyWhat="no unit is populated for the selection"
      >
        {/* Each unit's whole as one stacked bar, so the split reads at a
            glance and the lead can be followed down the table; and each class's
            share and amount written out in its own column beside it, so a part
            too thin to see on the bar -- the IDE's 0.3% of the processes -- is
            still there to read, and to sort by. */}
        <div className="scroller">
          <table className="roomy shares who" style={{ '--ncls': have.length } as CSSProperties}>
            <thead>
              <tr>
                <SortTh s={sort} k="unit" first="asc">each class's share of</SortTh>
                <th className="split" aria-label="the whole, split by class" />
                {have.map((c) => (
                  <SortTh key={c} s={sort} k={c} className="cls" style={{ color: CLS_INK[c] }}>
                    {CLS_LBL[c] ?? c}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((u, i) => {
                const why = noShare(wholes.get(u)!);
                const g = u.group ?? 'node';
                const head = i === 0 || (sorted[i - 1].group ?? 'node') !== g;
                return (
                  <Fragment key={u.k}>
                  {head && (
                    <tr className="grp">
                      <td colSpan={have.length + 2} data-tip={GROUPS[g][1]}>
                        {GROUPS[g][0]}
                        {g === 'slurm' && <span className="nm"> · requests, not use: GPU use on the compute nodes is not measured</span>}
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td><Term tip={[u.tip, why].filter(Boolean).join('\n\n') || undefined}>{u.k}</Term></td>
                    <td className="split">
                      {why == null && wholes.get(u)!.tot ? (
                        <SplitBar parts={partsOf(u)} what={u.k} />
                      ) : (
                        <div className="split-bar none" data-tip={why ?? undefined}>
                          <span>{why ? why.split(',')[0] : 'nothing counted'}</span>
                        </div>
                      )}
                    </td>
                    {have.map((c) => {
                      const v = u.get(c);
                      const pct = shareIn(u, c);
                      const lead = leads.get(u)!.includes(c);
                      return (
                        <td
                          key={c}
                          className={lead ? 'clsv lead' : 'clsv'}
                          data-tip={[
                            lead ? `the largest share of ${u.k}` : null,
                            u.cellTip?.(c),
                            v != null ? why : null,
                          ].filter(Boolean).join('\n') || undefined}
                        >
                          {/* the class's name over its numbers, shown only on a
                              phone, where the header row is out of sight */}
                          <span className="clsv-k">
                            <i style={{ background: CLS_COL[c] }} />
                            {CLS_LBL[c] ?? c}
                          </span>
                          {u.io && v ? (
                            <IoNum q={{ title: `${u.k} · ${CLS_LBL[c] ?? c}`, sort: u.io, class: [c], ...coveredBy(io) }}>
                              <ShareCell pct={pct} sub={u.fmt ? u.fmt(v) : fint(v)} c={CLS_COL[c]} bar={false} />
                            </IoNum>
                          ) : (
                            <ShareCell pct={pct} sub={u.fmt ? u.fmt(v) : fint(v)} c={CLS_COL[c]} bar={false} />
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
});
