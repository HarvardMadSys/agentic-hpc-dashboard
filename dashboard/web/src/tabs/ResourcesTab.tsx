/* Resources -- the `resources` payload.
 *
 * Every accumulator here is coverage-carrying, and a `{n: 0, coverage_pct: 0}`
 * accumulator renders as `not measured`: schedstat and delay accounting are not
 * readable for every process (the collector is unprivileged), so a 0 total
 * would be a claim about the cluster rather than about the instrument.
 */
import type { Cls, Quant, ResBin, ResBinCell, Resources } from '../api/types';
import { HBars, Lines } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { timeFmt, timeTicks } from '../charts/scale';
import { Basis, Legend, Panel } from '../components/Panel';
import { AccumCell, Coverage, QUANT_HEAD, QuantCells } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, notMeasured, pc } from '../lib/format';
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

/** One class's value in every bin, as a line.
 *
 * A bin no exit landed in is null for every class: a gap in the line, because
 * the instrument recorded nothing there. A class merely ABSENT from a bin other
 * classes filled is handed to `f` as undefined -- the collector was recording,
 * so whether that is a measured 0 or still unmeasured is the metric's call. */
function pts(
  rows: ResBin[],
  c: string,
  f: (cell: ResBinCell | undefined, secs: number) => number | null,
) {
  return rows.map((b) => ({ x: b.epoch, y: b.by_class ? f(b.by_class[c], b.secs) : null }));
}

const perMin = (cell: ResBinCell | undefined, secs: number) => ((cell?.n ?? 0) * 60) / secs;
// cpu-seconds per second: the mean number of cores the class kept busy
const cores = (cell: ResBinCell | undefined, secs: number) =>
  !cell ? 0 : cell.cpu_n ? (cell.cpu_s ?? 0) / secs : null;
// schedstat is per-process and often unreadable: no reporting exit, no value
const runRate = (cell: ResBinCell | undefined, secs: number) =>
  cell?.run_n ? (cell.run_s ?? 0) / secs : null;
const waitRate = (cell: ResBinCell | undefined, secs: number) =>
  cell?.wait_n ? (cell.wait_s ?? 0) / secs : null;

export function ResourcesTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const r = pick<Resources>(P, 'resources', 'ebpfm');
  const byClass = r.by_class ?? {};
  const have = shown(byClass, cls);

  const eventsBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.events ?? null]));
  const cpuBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.cpu_s ?? null]));
  const totEvents = have.reduce((a, c) => a + (byClass[c]?.events ?? 0), 0);
  const totCpu = have.reduce((a, c) => a + (byClass[c]?.cpu_s ?? 0), 0);

  /* The same exits, binned in time. Events and CPU stay two charts on two axes:
   * the whole point of this tab is that the two rankings are not the same
   * ranking, and now also not the same shape over the day. */
  const ts = r.timeseries;
  const rows = ts?.rows ?? [];
  const t0 = rows.length ? rows[0].epoch : 0;
  const t1 = rows.length ? rows[rows.length - 1].epoch : 0;
  const xticks = timeTicks(t0, t1);
  const xfmt = timeFmt(t1 - t0);
  const hhmm = timeFmt(0);
  const byEpoch = new Map(rows.map((b) => [b.epoch, b]));
  const line = (c: string, f: Parameters<typeof pts>[2], lbl = CLS_LBL[c] ?? c): LineSeries => ({
    lbl,
    c: CLS_COL[c],
    pts: pts(rows, c, f),
  });
  /* One tooltip per bin, every selected class in it, so a column reads as "who
   * was doing what at 14:20" instead of one hovered point at a time. */
  const tip = (x: number, cell: (c: string, v: ResBinCell | undefined, secs: number) => string) => {
    const b = byEpoch.get(x);
    if (!b) return '';
    const head = `${xfmt(x)}\u2013${hhmm(x + (ts?.bin_s ?? 0))}${
      b.secs < (ts?.bin_s ?? 0) ? ' (in progress)' : ''
    }`;
    if (!b.by_class) return `${head}\nno exit recorded in this bin`;
    return [head, ...have.map((c) => `${CLS_LBL[c] ?? c}: ${cell(c, b.by_class![c], b.secs)}`)].join('\n');
  };
  const ccTip = (x: number) =>
    tip(x, (_c, v, secs) => `${fmt(perMin(v, secs))}/min \u00b7 ${fmt(cores(v, secs))} cores`);
  const schedTip = (x: number) =>
    tip(x, (_c, v, secs) =>
      v?.run_n || v?.wait_n
        ? `on cpu ${fmt(runRate(v, secs))} \u00b7 wait ${fmt(waitRate(v, secs))} \u00b7 cov ${pc(
            (100 * Math.max(v.run_n, v.wait_n)) / v.n,
          )}`
        : 'not measured',
    );

  const runLines = have.map((c) => line(c, runRate));
  const waitLines = have.map((c) => line(c, waitRate));
  /* One y range for both: run against wait is a comparison of magnitudes, and
   * two independently-scaled charts would draw a 10x gap as a tie. */
  const schedMax = Math.max(
    0,
    ...[...runLines, ...waitLines].flatMap((l) => l.pts.map((p) => p.y ?? 0)),
  );
  const schedEmpty = have.every(
    (c) => notMeasured(byClass[c]?.sched_run_s) && notMeasured(byClass[c]?.sched_wait_s),
  );

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
          title="Events and CPU over time"
          p={r}
          span
          empty={!have.length || !rows.length}
          emptyWhat={!have.length ? 'no class block for the selection' : 'no time bins in this payload'}
          basis={
            <Basis
              items={[
                ['bins', ts ? `${dur(ts.bin_s)}; rates are per elapsed second of each bin` : null],
                ['attribution', ts?.basis],
                ['cpu_basis', r.cpu_basis],
                ['scope', r.scope],
                ['no timestamp', ts?.no_ts ? `${fint(ts.no_ts)} exits could not be binned` : null],
              ]}
            />
          }
        >
          <div className="eyebrow">events per minute</div>
          <Lines
            series={have.map((c) => line(c, perMin))}
            W={1060}
            H={200}
            xmin={t0}
            xmax={t1}
            xticks={xticks}
            xfmt={xfmt}
            ylabel="events / min"
            xtip={ccTip}
          />
          <div className="eyebrow" style={{ marginTop: 6 }}>
            CPU in use
          </div>
          <Lines
            series={have.map((c) => line(c, cores))}
            W={1060}
            H={200}
            xmin={t0}
            xmax={t1}
            xticks={xticks}
            xfmt={xfmt}
            ylabel="cores (cpu-s per s)"
            xtip={ccTip}
          />
          <Legend items={have.map((c) => ({ k: CLS_LBL[c] ?? c, c: CLS_COL[c] }))} />
        </Panel>

        <Panel
          title="Run time against wait time"
          p={r}
          empty={!have.length || schedEmpty || !rows.length}
          emptyWhat="schedstat was not readable for any selected class"
          basis={
            <Basis
              items={[
                ['schedstat', 'run/wait come from /proc/<pid>/schedstat; unreadable for other users'],
                ['rates', 'seconds per second of bin: the mean number of measured processes on a CPU, or runnable and waiting for one'],
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
          {(
            [
              ['on cpu', runLines],
              ['runqueue wait', waitLines],
            ] as [string, LineSeries[]][]
          ).map(([lbl, lines], i) => (
            <div key={lbl}>
              <div className="eyebrow" style={i ? { marginTop: 6 } : undefined}>
                {lbl}
              </div>
              <Lines
                series={lines}
                W={520}
                H={160}
                ymax={schedMax || 1}
                xmin={t0}
                xmax={t1}
                xticks={timeTicks(t0, t1, 4)}
                xfmt={xfmt}
                ylabel="processes (s per s)"
                xtip={schedTip}
              />
            </div>
          ))}
          <Legend items={have.map((c) => ({ k: CLS_LBL[c] ?? c, c: CLS_COL[c] }))} />
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
