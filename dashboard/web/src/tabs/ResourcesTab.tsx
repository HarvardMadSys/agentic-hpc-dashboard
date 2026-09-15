/* Resources -- the `resources` payload.
 *
 * Every accumulator here is coverage-carrying, and a `{n: 0, coverage_pct: 0}`
 * accumulator renders as `not measured`: schedstat and delay accounting are not
 * readable for every process (the collector is unprivileged), so a 0 total
 * would be a claim about the cluster rather than about the instrument.
 */
import type { Cls, Quant, Resources } from '../api/types';
import { GroupBars, HBars } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { AccumCell, Coverage, QUANT_HEAD, QuantCells } from '../components/Coverage';
import { CountCost } from '../components/CountCost';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, notMeasured, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import { InstanceTable } from './RiskIoTab';
import type { ResourceInstance } from '../api/types';
import type { Panels } from '../lib/panels';

const QUANT_KEYS = ['cpu_s', 'peak_rss_mb', 'sched_wait_s', 'duration_s', 'on_cpu_frac'] as const;
const QUANT_UNITS: Record<string, string> = {
  cpu_s: 's',
  peak_rss_mb: 'MB',
  sched_wait_s: 's',
  duration_s: 's',
  on_cpu_frac: '',
};

export function ResourcesTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const r = pick<Resources>(P, 'resources', 'ebpfm');
  const byClass = r.by_class ?? {};
  const have = shown(byClass, cls);
  const series: BarSeries[] = have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c] }));

  const eventsBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.events ?? null]));
  const cpuBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.cpu_s ?? null]));
  const totEvents = have.reduce((a, c) => a + (byClass[c]?.events ?? 0), 0);
  const totCpu = have.reduce((a, c) => a + (byClass[c]?.cpu_s ?? 0), 0);

  /* Events and CPU per class, as the same two-chart pattern: the whole point of
   * this tab is that the two rankings are not the same ranking. */
  const ccGroups: BarGroup[] = have.map((c) => {
    const v: Record<string, number | null> = {};
    for (const s of series) {
      v[s.k] = s.k === c ? ((100 * (byClass[c]?.events ?? 0)) / (totEvents || 1)) : null;
      v['cpu:' + s.k] = s.k === c ? ((100 * (byClass[c]?.cpu_s ?? 0)) / (totCpu || 1)) : null;
    }
    return { k: CLS_LBL[c] ?? c, v };
  });

  /* Run vs wait: one axis, seconds. Each is an accumulator with its own coverage. */
  const schedGroups: BarGroup[] = have.map((c) => ({
    k: CLS_LBL[c] ?? c,
    k2: `cov ${pc(byClass[c]?.sched_run_s?.coverage_pct)}`,
    v: {
      run: notMeasured(byClass[c]?.sched_run_s) ? null : (byClass[c]!.sched_run_s.total ?? null),
      wait: notMeasured(byClass[c]?.sched_wait_s) ? null : (byClass[c]!.sched_wait_s.total ?? null),
    },
  }));

  const stalls = (r.stall_by_tool ?? []).filter((s) => !s.cls || cls.includes(s.cls as Cls));

  return (
    <>
      <div className="plates">
        <Plate k="events" total={have.length ? totEvents : null} stripe="var(--s1)" by={eventsBy} />
        <Plate
          k="cpu seconds"
          total={have.length ? totCpu : null}
          stripe="var(--s6)"
          by={cpuBy}
          fmtFn={(n) => fmt(n)}
          unit="s"
          note={r.cpu_basis ? 'see basis for the CPU definition' : undefined}
        />
        <Plate
          k="schedstat run:wait ratio"
          total={null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.schedstat_ratio ?? null]))}
          fmtFn={(n) => (n == null ? DASH : fmt(n, 2))}
          note="run seconds per wait second; per class only, no fleet total"
        />
        <Plate
          k="median peak RSS"
          total={null}
          stripe="var(--s4)"
          by={Object.fromEntries(
            // all_zero => null, so the plate reads as unmeasured rather than
            // asserting a 0.00 MB median that was never measured
            have.map((c) => {
              const q = byClass[c]?.quants?.peak_rss_mb;
              return [c, q?.all_zero ? null : (q?.p50 ?? null)];
            }),
          )}
          fmtFn={(n) => (n == null ? DASH : fmt(n))}
          note="p50 MB, per class; coverage in the quantile table"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Where the events are, and where the CPU is"
          p={r}
          span
          empty={!have.length}
          emptyWhat="no class block for the selection"
          basis={<Basis items={[['cpu_basis', r.cpu_basis], ['scope', r.scope]]} />}
        >
          <CountCost
            groups={ccGroups}
            series={series}
            countLabel="share of events"
            costLabel="share of CPU seconds"
            countAxis="% of events"
          />
        </Panel>

        <Panel
          title="Run time against wait time"
          p={r}
          empty={!have.length || schedGroups.every((g) => g.v.run == null && g.v.wait == null)}
          emptyWhat="schedstat was not readable for any selected class"
          basis={
            <Basis
              items={[
                ['schedstat', 'run/wait come from /proc/<pid>/schedstat; unreadable for other users'],
                ...have.map(
                  (c) =>
                    [
                      `${c} coverage`,
                      `run ${pc(byClass[c]?.sched_run_s?.coverage_pct)} · wait ${pc(
                        byClass[c]?.sched_wait_s?.coverage_pct,
                      )}`,
                    ] as [string, string],
                ),
              ]}
            />
          }
        >
          <GroupBars
            groups={schedGroups}
            series={[
              { k: 'run', lbl: 'on cpu', c: 'var(--s5)' },
              { k: 'wait', lbl: 'runqueue wait', c: 'var(--s6)' },
            ]}
            W={520}
            H={210}
            ylabel="seconds"
            fmtVal={(v) => fmt(v) + ' s'}
          />
          <Legend
            items={[
              { k: 'on cpu', c: 'var(--s5)' },
              { k: 'runqueue wait', c: 'var(--s6)' },
            ]}
          />
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>class</th>
                  <th className="num">run</th>
                  <th className="num">wait</th>
                  <th className="num">ratio</th>
                </tr>
              </thead>
              <tbody>
                {have.map((c) => (
                  <tr key={c}>
                    <td>{CLS_LBL[c] ?? c}</td>
                    <td className="num">
                      <AccumCell a={byClass[c]?.sched_run_s} unit="s" />
                    </td>
                    <td className="num">
                      <AccumCell a={byClass[c]?.sched_wait_s} unit="s" />
                    </td>
                    <td className="num">
                      {byClass[c]?.schedstat_ratio == null ? (
                        <span className="nm">not measured</span>
                      ) : (
                        fmt(byClass[c]!.schedstat_ratio, 2)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Per-event distributions"
          p={r}
          empty={!have.length}
          emptyWhat="no class block for the selection"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>metric</th>
                  <th>class</th>
                  {QUANT_HEAD}
                </tr>
              </thead>
              <tbody>
                {QUANT_KEYS.flatMap((qk) =>
                  have.map((c) => {
                    const q = byClass[c]?.quants?.[qk] as Quant | undefined;
                    return (
                      <tr key={qk + c}>
                        <td className="mono">
                          {qk}
                          {QUANT_UNITS[qk] ? ` (${QUANT_UNITS[qk]})` : ''}
                        </td>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <QuantCells q={q} />
                      </tr>
                    );
                  }),
                )}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Stall by tool"
          p={r}
          empty={!stalls.length}
          emptyWhat="no per-tool stall rows for the selection"
        >
          <HBars
            items={stalls.slice(0, 14).map((s) => ({
              k: s.tool,
              v: s.sched_wait_s ?? s.dstate_wait_s ?? null,
              c: s.cls ? CLS_COL[s.cls] : 'var(--s6)',
              lbl:
                s.sched_wait_s == null && s.dstate_wait_s == null
                  ? 'not measured'
                  : fmt(s.sched_wait_s ?? s.dstate_wait_s) + ' s',
              tip: `${s.tool}${s.cls ? ' · ' + (CLS_LBL[s.cls] ?? s.cls) : ''}: wait ${fmt(
                s.sched_wait_s,
              )} s, D-state ${fmt(s.dstate_wait_s)} s, coverage ${pc(s.coverage_pct)}, ${fint(
                s.events,
              )} events`,
            }))}
            labelW={150}
            valW={110}
          />
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>tool</th>
                  <th>class</th>
                  <th className="num">events</th>
                  <th className="num">wait s</th>
                  <th className="num">D-state s</th>
                  <th className="num">coverage</th>
                </tr>
              </thead>
              <tbody>
                {stalls.slice(0, 20).map((s, i) => (
                  <tr key={s.tool + i}>
                    <td className="mono">{s.tool}</td>
                    <td>{s.cls ? (CLS_LBL[s.cls] ?? s.cls) : DASH}</td>
                    <td className="num">{fint(s.events)}</td>
                    <td className="num">{fmt(s.sched_wait_s)}</td>
                    <td className="num">{fmt(s.dstate_wait_s)}</td>
                    <td className="num">
                      <Coverage pct={s.coverage_pct} n={s.events} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Faults, delays and D-state"
          p={r}
          empty={
            !have.some(
              (c) =>
                Object.keys(byClass[c]?.faults ?? {}).length ||
                Object.keys(byClass[c]?.delays ?? {}).length ||
                Object.keys(byClass[c]?.dstate ?? {}).length,
            )
          }
          emptyWhat="delay accounting was not readable for the selection"
          span
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>class</th>
                  <th>group</th>
                  <th>field</th>
                  <th className="num">value</th>
                </tr>
              </thead>
              <tbody>
                {have.flatMap((c) =>
                  (['faults', 'delays', 'dstate'] as const).flatMap((g) =>
                    Object.entries(byClass[c]?.[g] ?? {}).map(([k, v]) => (
                      <tr key={c + g + k}>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <td className="mono" style={{ color: 'var(--muted)' }}>
                          {g}
                        </td>
                        <td className="mono">{k}</td>
                        <td className="num">
                          {v == null ? (
                            <span className="nm">not measured</span>
                          ) : typeof v === 'number' ? (
                            fmt(v)
                          ) : typeof v === 'object' && v && 'total' in (v as object) ? (
                            <AccumCell a={v as never} />
                          ) : (
                            String(v)
                          )}
                        </td>
                      </tr>
                    )),
                  ),
                )}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel
          title="Worst individual processes"
          p={r}
          span
          empty={
            !(r.worst_stall?.length || r.worst_fault?.length || r.worst_wait?.length)
          }
          emptyWhat="no process stalled, faulted or waited measurably in the window"
        >
          {/* An aggregate by class says how much; only a pid says whose process
              to go and look at. One table per axis, because the worst staller
              and the worst faulter are rarely the same process. */}
          {([
            ['longest D-state wait (uninterruptible sleep \u2014 the NFS/autofs signal)', r.worst_stall],
            ['most major faults (the ones that actually hit disk)', r.worst_fault],
            ['longest runqueue wait (ready, but not scheduled)', r.worst_wait],
          ] as [string, ResourceInstance[] | undefined][]).map(([label, list], i) => {
            const rows = list ?? [];
            const key = i;
            if (!rows.length) return null;
            return (
              <div key={key}>
                <div className="eyebrow" style={{ marginTop: 10 }}>{label}</div>
                <InstanceTable rows={rows} showArgs />
              </div>
            );
          })}
        </Panel>

      </div>
    </>
  );
}
