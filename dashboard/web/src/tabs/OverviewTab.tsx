/* Overview -- the three classes at a glance, in the units that disagree.
 *
 * Nothing here is a new measurement: it is the same panels the other tabs draw,
 * reduced to one comparison each, and the comparisons are deliberately shown in
 * MORE THAN ONE unit. A single unit is what lets a few loud users stand in for
 * a population, so events and CPU appear side by side, and the session grain
 * appears beside the event grain.
 */
import type { Cls, IoProcess, LiveResponse, Resources, Sandbox, ToolMix, Trajectories } from '../api/types';
import { StackRow } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { CountCost } from '../components/CountCost';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, mb, pc } from '../lib/format';
import { pick, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

/** One row per unit: the whole point is that the ranking changes between them. */
interface UnitRow {
  k: string;
  get: (c: string) => number | null;
  fmt?: (n: number | null) => string;
  tip?: string;
}

export function OverviewTab({
  P,
  live,
  cls,
  traj,
}: {
  P: Panels;
  live: LiveResponse | null;
  cls: Cls[];
  traj: Trajectories | null;
}) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const win = winLabel(tm);
  const res = pick<Resources>(P, 'resources', 'ebpfm');
  const io = pick<IoProcess>(P, 'io_process', 'ebpfm');
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');

  const tmBy = tm.by_class ?? {};
  const resBy = res.by_class ?? {};
  const ioBy = io.by_class ?? {};
  const have = cls.filter((c) => c in tmBy || c in resBy || c in ioBy);

  const series: BarSeries[] = have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c] }));

  const totCalls = have.reduce((a, c) => a + (tmBy[c]?.calls ?? 0), 0);
  const totCpu = have.reduce((a, c) => a + (tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s ?? 0), 0);

  /* Share of calls against share of CPU, one bar group per class, two axes that
   * are never the same axis. */
  const ccGroups: BarGroup[] = have.map((c) => {
    const v: Record<string, number | null> = {};
    for (const s of series) {
      const calls = tmBy[c]?.calls;
      const cpu = tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s;
      v[s.k] = s.k === c && calls != null ? (100 * calls) / (totCalls || 1) : null;
      v['cpu:' + s.k] = s.k === c && cpu != null ? (100 * cpu) / (totCpu || 1) : null;
    }
    return { k: CLS_LBL[c] ?? c, v };
  });

  const units: UnitRow[] = ([
    { k: 'tool calls', get: (c) => tmBy[c]?.calls ?? null, tip: 'event grain' },
    { k: 'cpu seconds', get: (c) => tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s ?? null, fmt: (n) => fmt(n) },
    { k: 'events (resources)', get: (c) => resBy[c]?.events ?? null },
    { k: 'events carrying I/O', get: (c) => ioBy[c]?.events_with_io ?? null },
    { k: 'MB read from disk', get: (c) => ioBy[c]?.rd_mb ?? null, fmt: (n) => mb(n) },
    { k: 'MB written to disk', get: (c) => ioBy[c]?.wr_mb ?? null, fmt: (n) => mb(n) },
    {
      k: 'sessions (trajectory buffer)',
      get: (c) => traj?.by_class_counts?.[c] ?? null,
      tip: 'session grain: one long-lived root is 1 here and millions of events elsewhere',
    },
    {
      k: 'trees (sandbox grain)',
      get: (c) =>
        sb.by_class_trees?.[c]
          ? Object.values(sb.by_class_trees[c]).reduce((a, b) => a + b, 0)
          : null,
    },
  ] as UnitRow[]).filter((u) => have.some((c) => u.get(c) != null));

  const rate = live?.rate ?? [];
  const perMin = Object.fromEntries(
    cls.map((c) => {
      for (let i = rate.length - 1; i >= 0; i--) {
        const v = rate[i][c];
        if (typeof v === 'number') return [c, v];
      }
      return [c, null];
    }),
  );
  const perMinAll = cls.reduce<number | null>((a, c) => {
    const v = perMin[c] as number | null;
    return v == null ? a : (a ?? 0) + v;
  }, null);

  return (
    <>
      <div className="plates">
        <Plate
          k="process exits / min"
          total={perMinAll}
          stripe="var(--s1)"
          by={perMin as Record<string, number | null>}
          note="newest complete minute from the live tier"
        />
        <Plate
          k={`tool calls ${win}`}
          total={have.length ? totCalls : null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, tmBy[c]?.calls ?? null]))}
        />
        <Plate
          k={`cpu seconds ${win}`}
          total={have.length ? totCpu : null}
          stripe="var(--s6)"
          by={Object.fromEntries(have.map((c) => [c, tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s ?? null]))}
          fmtFn={(n) => fmt(n)}
          unit="s"
        />
        <Plate
          k="sandboxed share of trees"
          total={null}
          stripe="var(--s5)"
          by={Object.fromEntries(
            cls.map((c) => {
              const m = sb.by_class_trees?.[c];
              if (!m) return [c, null];
              const tot = Object.values(m).reduce((a, b) => a + b, 0);
              return [c, tot ? (100 * (m.sandboxed ?? 0)) / tot : null];
            }),
          )}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="`denied` is not counted as unsandboxed"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Share of calls against share of CPU"
          p={tm}
          span
          empty={!have.length}
          emptyWhat="no class block in the tool feed"
          basis={<Basis items={[['cpu_basis', tm.cpu_basis]]} />}
        >
          <CountCost groups={ccGroups} series={series} countLabel="share of calls" />
        </Panel>

        <Panel
          title="The same fleet in eight units"
          p={tm}
          span
          empty={!units.length}
          emptyWhat="no unit is populated for the selection"
          basis={
            <Basis
              items={[
                [
                  'why',
                  'the class ranking is not stable across units: a unit that de-weights the loudest users routinely erases or flips the per-event gap',
                ],
              ]}
            />
          }
        >
          {units.map((u) => (
            <div key={u.k}>
              <div className="eyebrow" style={{ marginBottom: 4 }} data-tip={u.tip}>
                {u.k} {'·'}{' '}
                {have.map((c) => (u.fmt ? u.fmt(u.get(c)) : fint(u.get(c)))).join(' / ')}
              </div>
              <StackRow
                segs={have.map((c) => ({
                  k: CLS_LBL[c] ?? c,
                  v: u.get(c) ?? 0,
                  c: CLS_COL[c],
                }))}
                W={1060}
                H={18}
              />
            </div>
          ))}
          <Legend items={have.map((c) => ({ k: CLS_LBL[c] ?? c, c: CLS_COL[c] }))} />
        </Panel>
      </div>
    </>
  );
}
