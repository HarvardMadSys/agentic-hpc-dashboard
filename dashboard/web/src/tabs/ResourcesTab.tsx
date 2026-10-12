/* Resources -- the `resources` payload.
 *
 * Every accumulator here is coverage-carrying, and a `{n: 0, coverage_pct: 0}`
 * accumulator renders as `not measured`: schedstat and delay accounting are not
 * readable for every process (the collector is unprivileged), so a 0 total
 * would be a claim about the cluster rather than about the instrument.
 *
 * The charts are time series over the window, from `series`. Most are sums BY
 * EXIT TIME, and each such chart says so in its pill: a whole-life counter
 * lands in the bin its process exited in. Resident memory is the exception,
 * sampled while processes run, and its pill says that instead.
 */
import { useState } from 'react';
import type { Accum, Cls, Quant, ResSeries, Resources } from '../api/types';
import { Lines } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { clockLabel, fullClock, timeTicks } from '../charts/scale';
import { Basis, Legend, Panel, More } from '../components/Panel';
import { AccumCell } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb, notMeasured, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import { InstanceTable } from './RiskIoTab';
import type { ResourceInstance } from '../api/types';
import type { Panels } from '../lib/panels';

const QUANT_KEYS = ['cpu_s', 'peak_rss_mb', 'sched_wait_s', 'duration_s', 'on_cpu_frac'] as const;

/* Tools are not classes, so their lines skip the class hues (crimson is the
 * agent's alone); everything past the named tools is the grey tail. */
// Ordered so no two neighbours are close: --s5 next to --s4 is violet on purple.
const TOOL_COL = ['var(--s4)', 'var(--s6)', 'var(--s5)', 'var(--s8)', 'var(--s7)', 'var(--s2)'];

function binLabel(binS: number): string {
  return binS % 3600 === 0 ? `${binS / 3600} h` : `${binS / 60} min`;
}

export interface TimeLine {
  lbl: string;
  c: string;
  v: (number | null)[] | undefined;
  dashed?: boolean;
}

const anyValue = (lines: TimeLine[]) => lines.some((l) => l.v?.some((x) => x != null));

export type View = [number, number] | null;

/** Bins on a shared wall-clock axis. `perMin` turns a per-bin SUM into a rate,
 * so the scale reads the same at every window; a sampled level is left as is.
 * Every chart on the tab takes the same `view`, so a zoom into one is a zoom
 * into all of them, and a spike can be read across exits, CPU and memory at
 * once. It stops at three bins: finer than that, the chart would be implying a
 * resolution the bins never had. */
export function TimeChart({
  s,
  lines,
  ylabel,
  yname,
  perMin = false,
  scale = 1,
  view,
  onView,
  yfmt,
  ymax,
  legend,
}: {
  s: ResSeries;
  lines: TimeLine[];
  ylabel: string;
  yname: string;
  perMin?: boolean;
  scale?: number;
  view: View;
  onView: (v: View) => void;
  yfmt?: (v: number) => string;
  ymax?: number;
  /** a legend other than one swatch per line, e.g. classes plus a line-style key */
  legend?: { k: string; c: string; dashed?: boolean; solid?: boolean }[];
}) {
  const k = scale * (perMin ? 60 / s.bin_s : 1);
  const x0 = s.t[0];
  const x1 = s.t[s.t.length - 1];
  const series: LineSeries[] = lines.map((l) => ({
    lbl: l.lbl,
    c: l.c,
    dashed: l.dashed,
    pts: s.t.map((x, i) => {
      const v = l.v?.[i];
      return { x, y: v == null ? null : v * k };
    }),
  }));
  return (
    <>
      <Lines
        series={series}
        W={520}
        H={210}
        ymin={0}
        ymax={ymax}
        yfmt={yfmt}
        xmin={x0}
        xmax={x1 > x0 ? x1 : x0 + s.bin_s}
        xticks={(a, b) => timeTicks(a, b)}
        xfmt={clockLabel}
        xtip={fullClock}
        ylabel={ylabel}
        xname="at"
        yname={yname}
        view={view}
        onView={onView}
        minSpan={3 * s.bin_s}
      />
      <Legend items={legend ?? lines.map((l) => ({ k: l.lbl, c: l.c, dashed: l.dashed }))} />
    </>
  );
}

/* One row per measure, one column per class: the old table was one row per
 * (class, group, field), forty rows to read across. The headline number is
 * per process -- the classes differ in size by three orders of magnitude, so
 * totals side by side only rank them by how many processes they ran -- and
 * the total sits beneath it. */
type FaultKind = 'count' | 'secs' | 'share' | 'max';
const FAULT_ROWS: [string, [group: 'faults' | 'delays' | 'dstate', key: string, label: string, kind: FaultKind, tip?: string][]][] = [
  ['page faults and context switches', [
    ['faults', 'min_flt', 'minor page faults', 'count', 'a page mapped without touching storage'],
    ['faults', 'maj_flt', 'major page faults', 'count', 'a page that had to be read from storage, often over a network filesystem such as NFS'],
    ['faults', 'cmaj_flt', 'major faults, in reaped children', 'count'],
    ['faults', 'nvcsw', 'gave up the CPU to wait', 'count', 'voluntary context switches: blocked on I/O, a lock, or sleep'],
    ['faults', 'nivcsw', 'preempted', 'count', 'involuntary context switches: the scheduler took the CPU away'],
  ]],
  ['time spent waiting', [
    ['delays', 'blkio_wait_s', 'on block I/O', 'secs'],
    ['delays', 'swapin_wait_s', 'on swap-in', 'secs'],
    ['delays', 'freepages_wait_s', 'on memory reclaim', 'secs'],
  ]],
  ['uninterruptible sleep (D-state)', [
    ['dstate', 'events_with_stall', 'processes that stalled', 'share'],
    ['dstate', 'wait_s', 'time stalled', 'secs'],
    ['dstate', 'episodes', 'separate stalls', 'count', 'each time a thread blocked in uninterruptible sleep and woke again is one stall'],
    ['dstate', 'max_s', 'longest single stall', 'max'],
  ]],
];

function FaultCell({
  v,
  kind,
  events,
  dropped,
}: {
  v: unknown;
  kind: FaultKind;
  events: number;
  dropped?: number;
}) {
  const sub = (t: string) => <div className="sub">{t}</div>;
  if (kind === 'max' || kind === 'share') {
    if (typeof v !== 'number') return <span className="nm">not measured</span>;
    return kind === 'max' ? (
      <>{dur(v)}</>
    ) : (
      <>
        {pc(events ? (100 * v) / events : null)}
        {sub(`${fint(v)} of ${fint(events)}`)}
      </>
    );
  }
  const a = v as Accum | null | undefined;
  if (!a || notMeasured(a)) return <span className="nm">not measured</span>;
  // `dur` rounds a 0.3 ms wait to "0 ms", which reads as none at all
  const secs = (x: number | null | undefined) => (x != null && x > 0 && x < 0.0005 ? '<1 ms' : dur(x));
  // from total / n: the payload's `mean` is rounded to 4 places, so a 0.03 ms
  // mean arrives as exactly 0
  const mean = a.n && a.total != null ? a.total / a.n : null;
  const per = kind === 'secs' ? secs(mean) : fmt(mean);
  const tot = kind === 'secs' ? secs(a.total) : fmt(a.total);
  // Said in words, not in the collector's terms: the small line is the sum, and
  // a partial reading is named where it changes what the number means. Values
  // dropped as impossible are a hover, since they never change the reading.
  const partial = a.coverage_pct != null && a.coverage_pct < 99.5;
  const tip = [
    `${per} per process on average, ${tot} across ${fint(a.n)} processes`,
    partial
      ? `only ${pc(a.coverage_pct)} of processes could be measured for this; both numbers cover those alone`
      : null,
    dropped
      ? `${fint(dropped)} process${dropped === 1 ? '' : 'es'} reported a wait longer than ${dropped === 1 ? 'it was' : 'they were'} alive, which cannot be real, so ${dropped === 1 ? 'it is' : 'they are'} left out`
      : null,
  ].filter(Boolean).join('\n\n');
  return (
    <span data-tip={tip}>
      {per}
      {sub(`${tot} across all`)}
      {partial && sub(`measured on ${pc(a.coverage_pct)} of processes`)}
    </span>
  );
}

/* Same shape as the faults table: one row per measure, the classes across,
 * so a measure reads left to right. Each class gets its four quantiles; the
 * coverage column is gone (every class read 100%), and a sampled estimate says
 * so in the class header's tooltip instead of on every row. */
const DIST_ROWS: [key: (typeof QUANT_KEYS)[number], label: string, f: (v: number) => string][] = [
  ['cpu_s', 'CPU time', dur],
  ['duration_s', 'lifetime', dur],
  ['on_cpu_frac', 'share of lifetime on CPU', (v) => pc(100 * v)],
  ['sched_wait_s', 'runqueue wait', dur],
  ['peak_rss_mb', 'peak memory', mb],
];
const DIST_Q: [k: 'p50' | 'p90' | 'p99' | 'max', label: string][] = [
  ['p50', 'median'],
  ['p90', '90th'],
  ['p99', '99th'],
  ['max', 'max'],
];

function DistTable({ byClass, have }: { byClass: Resources['by_class']; have: string[] }) {
  const sampledN = (c: string) => {
    const qs = Object.values(byClass[c]?.quants ?? {}) as (Quant | undefined)[];
    const q = qs.find((x) => x?.sampled);
    return q ? q.sample_n : null;
  };
  return (
    <div className="scroller">
      <table className="dist">
        <thead>
          <tr>
            <th rowSpan={2}>per process</th>
            {have.map((c) => {
              const sn = sampledN(c);
              return (
                <th
                  key={c}
                  colSpan={DIST_Q.length}
                  className="grp"
                  data-tip={sn ? `quantiles estimated from a sample of ${fint(sn)} processes; max is exact` : undefined}
                >
                  <span style={{ color: CLS_COL[c] }}>{CLS_LBL[c] ?? c}</span>
                  <span className="sub"> · {fint(byClass[c]?.events)} processes</span>
                </th>
              );
            })}
          </tr>
          <tr>
            {have.flatMap((c) =>
              DIST_Q.map(([k, l], i) => (
                <th key={c + k} className={`num${i === 0 ? ' g0' : ''}`}>{l}</th>
              )),
            )}
          </tr>
        </thead>
        <tbody>
          {DIST_ROWS.map(([qk, label, f]) => (
            <tr key={qk}>
              <td>{label}</td>
              {have.flatMap((c) => {
                const q = byClass[c]?.quants?.[qk] as Quant | undefined;
                return DIST_Q.map(([k], i) => {
                  const v = q?.n ? q[k] : null;
                  return (
                    <td key={c + k} className={`num${i === 0 ? ' g0' : ''}`}>
                      {v == null ? DASH : f(v)}
                    </td>
                  );
                });
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FaultTable({
  byClass,
  have,
  impossible,
}: {
  byClass: Resources['by_class'];
  have: string[];
  impossible: Record<string, Record<string, number>>;
}) {
  return (
    <div className="scroller">
      <table className="faults">
        <thead>
          <tr>
            <th>average per process</th>
            {have.map((c) => (
              <th key={c} className="num">
                <span style={{ color: CLS_COL[c] }}>{CLS_LBL[c] ?? c}</span>
                <div className="sub">{fint(byClass[c]?.events)} processes</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {FAULT_ROWS.flatMap(([section, rows]) => [
            <tr key={section} className="section">
              <td colSpan={have.length + 1}>{section}</td>
            </tr>,
            ...rows.map(([g, k, label, kind, tip]) => (
              <tr key={g + k}>
                <td data-tip={tip}>{label}</td>
                {have.map((c) => (
                  <td key={c} className="num">
                    <FaultCell
                      v={(byClass[c]?.[g] as Record<string, unknown> | undefined)?.[k]}
                      kind={kind}
                      events={byClass[c]?.events ?? 0}
                      dropped={g === 'dstate' && k === 'wait_s'
                        ? impossible[c]?.dstate_wait_s : impossible[c]?.[k]}
                    />
                  </td>
                ))}
              </tr>
            )),
          ])}
        </tbody>
      </table>
    </div>
  );
}

export function ResourcesTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const r = pick<Resources>(P, 'resources', 'ebpfm');
  const byClass = r.by_class ?? {};
  const have = shown(byClass, cls);

  const totCpu = have.reduce((a, c) => a + (byClass[c]?.cpu_s ?? 0), 0);

  /* Exits and CPU sit side by side on the same axis: the point of this tab is
   * that the two rankings are not the same ranking, and over time they are
   * not the same shape either. */
  const s = r.series;
  const hasSeries = !!s?.t.length;
  // D-state is typed loosely in the payload (a bag of counters); read it once here.
  const dst = (c: string) =>
    byClass[c]?.dstate as { wait_s?: Accum; events_with_stall?: number } | undefined;
  const stallS = (c: string) => {
    const w = dst(c)?.wait_s;
    return w && w.n ? w.total : null;
  };
  // Peak resident memory per class, in GB, and when it happened.
  const peakGb: Record<string, number | null> = {};
  const peakAt: Record<string, number | null> = {};
  const meanGb: Record<string, number | null> = {};
  for (const [c, d] of Object.entries(s?.resident ?? {})) {
    let best: number | null = null;
    let at: number | null = null;
    (d.rss_mb ?? []).forEach((v, i) => {
      if (v != null && (best == null || v > best)) {
        best = v;
        at = s!.t[i];
      }
    });
    peakGb[c] = best == null ? null : best / 1024;
    const seen = (d.rss_mb ?? []).filter((v): v is number => v != null);
    meanGb[c] = seen.length ? seen.reduce((x, y) => x + y, 0) / seen.length / 1024 : null;
    peakAt[c] = at;
  }
  // One zoom for the whole tab, tied to the window it was taken in: a range
  // picked on the 24 h view means nothing once the 1 h view is on screen.
  const winKey = r._window?.label ?? '';
  const [zoom, setZoom] = useState<{ win: string; v: View } | null>(null);
  const view = zoom && zoom.win === winKey ? zoom.v : null;
  const onView = (v: View) => setZoom(v ? { win: winKey, v } : null);
  const z = { view, onView };
  const byCls = (field: keyof ResSeries['by_class'][string]): TimeLine[] =>
    have.map((c) => ({ lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: s?.by_class?.[c]?.[field] }));
  const exitsLines = byCls('n');
  const cpuLines = byCls('cpu_s');
  const waitLines = byCls('sched_wait_s');
  const faultLines = byCls('maj_flt');
  const memLines: TimeLine[] = shown(s?.resident, cls).map((c) => ({
    lbl: CLS_LBL[c] ?? c,
    c: CLS_COL[c],
    v: s?.resident[c]?.rss_mb,
  }));
  const stallLines: TimeLine[] = Object.entries(s?.stall_by_tool ?? {}).map(([tool, v], i) => ({
    lbl: tool,
    c: TOOL_COL[i % TOOL_COL.length],
    v,
  }));
  if (s?.stall_other.some((v) => v)) {
    stallLines.push({ lbl: 'every other tool', c: 'var(--sg)', v: s.stall_other });
  }
  const hostsSeen = Math.max(0, ...(s?.resident_hosts ?? []).map((h) => h ?? 0));
  const seriesBasis: [string, unknown][] = [
    ['bins', s ? `${binLabel(s.bin_s)}, wall-clock aligned, sized to the window` : null],
    ['not binned', s?.skipped],
    ['scope', r.scope],
  ];

  const stalls = r.stall_by_tool ?? [];

  return (
    <>
      {/* What agents cost the host: CPU, memory at its worst, and time
          stuck in uninterruptible sleep (usually storage, often NFS).
          The headline is the agent's; the other classes sit beside it. */}
      <div className="plates three">
        <Plate
          k="agent CPU"
          total={cls.includes('agent') && byClass.agent ? (byClass.agent.cpu_s ?? 0) / 3600 : null}
          stripe="var(--s6)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.cpu_s == null ? null : byClass[c]!.cpu_s! / 3600]))}
          fmtFn={(n) => fmt(n)}
          unit="CPU-h"
          tip="CPU each process used itself, summed once per process; a parent is never credited with its children's"
          note={totCpu ? `${pc((100 * (byClass.agent?.cpu_s ?? 0)) / totCpu)} of all CPU on the node` : undefined}
        />
        <Plate
          k="agent peak resident memory"
          total={cls.includes('agent') ? peakGb.agent ?? null : null}
          stripe="var(--s4)"
          by={Object.fromEntries(have.map((c) => [c, peakGb[c] ?? null]))}
          fmtFn={(n) => (n == null ? DASH : fmt(n))}
          unit="GB"
          tip="the highest per-bin mean of resident memory in the window, from the per-minute residency samples"
          note={s && peakAt.agent != null ? `at ${fullClock(peakAt.agent)}, sampled while processes ran` : undefined}
        />
        <Plate
          k="agent time stalled on I/O"
          total={cls.includes('agent') ? stallS('agent') : null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, stallS(c)]))}
          fmtFn={(n) => dur(n)}
          tip="seconds processes spent in uninterruptible sleep (D-state), usually waiting on storage, often NFS"
          note={
            byClass.agent?.events
              ? `${pc((100 * (dst('agent')?.events_with_stall ?? 0)) / byClass.agent.events)} of agent processes stalled at least once`
              : undefined
          }
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Process exits per minute"
          p={r}
          empty={!hasSeries || !anyValue(exitsLines)}
          emptyWhat="no process exited in the window for the selection"
          basis={<Basis items={seriesBasis} />}
        >
          {s && <TimeChart {...z} s={s} lines={exitsLines} perMin ylabel="exits / min" yname="exits/min" />}
        </Panel>

        <Panel
          title="CPU seconds per minute"
          p={r}
          empty={!hasSeries || !anyValue(cpuLines)}
          emptyWhat="no CPU was recorded in the window for the selection"
          basis={<Basis items={[['cpu_basis', r.cpu_basis], ...seriesBasis]} />}
        >
          {s && <TimeChart {...z} s={s} lines={cpuLines} perMin ylabel="CPU s / min" yname="CPU s/min" />}
        </Panel>

        <Panel
          title="Resident memory"
          p={r}
          empty={!hasSeries || !anyValue(memLines)}
          emptyWhat="no residency tick was read in the window for the selection"
          basis={
            <Basis
              items={[
                ['source', 'residency_totals.by_actor3.rss_mb, every host, every residency tick'],
                ['hosts', hostsSeen ? `${fint(hostsSeen)} reporting in the busiest bin` : null],
                ...seriesBasis,
              ]}
            />
          }
        >
          {s && <TimeChart {...z} s={s} lines={memLines} scale={1 / 1024} ylabel="GB resident" yname="GB" />}
          <More label="memory per class">
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <th>class</th>
                    <th className="num">peak</th>
                    <th>at</th>
                    <th className="num">average</th>
                  </tr>
                </thead>
                <tbody>
                  {memLines.map((l, i) => {
                    const c = shown(s?.resident, cls)[i];
                    return (
                      <tr key={c}>
                        <td>{l.lbl}</td>
                        <td className="num">{peakGb[c] == null ? DASH : `${fmt(peakGb[c]!)} GB`}</td>
                        <td className="mono nm">{peakAt[c] == null ? DASH : fullClock(peakAt[c]!)}</td>
                        <td className="num">{meanGb[c] == null ? DASH : `${fmt(meanGb[c]!)} GB`}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </More>
        </Panel>

        <Panel
          title="Runqueue wait per minute"
          p={r}
          empty={!have.length}
          emptyWhat="no class block for the selection"
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
                ...seriesBasis,
              ]}
            />
          }
        >
          {s && anyValue(waitLines) ? (
            <TimeChart {...z} s={s} lines={waitLines} perMin ylabel="wait s / min" yname="wait s/min" />
          ) : (
            <p className="nm">schedstat was not readable for any selected class</p>
          )}
          <More label="run and wait time per class">
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
          </More>
        </Panel>

        <Panel
          title="D-state wait by tool"
          p={r}
          empty={!stalls.length}
          emptyWhat="no process stalled in uninterruptible sleep in the window"
          header={
            <>
              <span
                className="pill"
                data-tip="tools are ranked over every class: the class filter does not narrow this panel"
              >
                all classes
              </span>
            </>
          }
          basis={<Basis items={seriesBasis} />}
        >
          {s && anyValue(stallLines) && (
            <TimeChart {...z}
              s={s}
              lines={stallLines}
              perMin
              ylabel="D-state s / min"
              yname="D-state s/min"
            />
          )}
          <More label="every tool that stalled">
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <th>tool</th>
                    <th className="num" data-tip="its processes that spent any time in uninterruptible sleep (D-state)">processes stalled</th>
                    <th className="num" data-tip="their time in D-state, summed">time stalled</th>
                    <th className="num" data-tip="each time a thread blocked in uninterruptible sleep and woke again is one stall; a process can stall thousands of times">stalls</th>
                    <th className="num" data-tip="the longest single stall">longest stall</th>
                  </tr>
                </thead>
                <tbody>
                  {stalls.map((t) => (
                    <tr key={t.tool}>
                      <td className="mono">{t.tool}</td>
                      <td className="num">{fint(t.n)}</td>
                      <td className="num">{dur(t.wait_s)}</td>
                      <td className="num">{fint(t.episodes)}</td>
                      <td className="num">{dur(t.max_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </More>
        </Panel>

        <Panel
          title="Major page faults per minute"
          p={r}
          empty={!hasSeries || !anyValue(faultLines)}
          emptyWhat="no major fault was recorded in the window for the selection"
          basis={<Basis items={seriesBasis} />}
        >
          {s && <TimeChart {...z} s={s} lines={faultLines} perMin ylabel="faults / min" yname="faults/min" />}
        </Panel>

        <Panel
          title="Per-event distributions"
          p={r}
          span
          empty={!have.length}
          emptyWhat="no class block for the selection"
        >
          <DistTable byClass={byClass} have={have} />
        </Panel>

        <Panel
          title="Faults, delays, and D-state"
          p={r}
          empty={!have.some((c) => byClass[c]?.faults || byClass[c]?.delays || byClass[c]?.dstate)}
          emptyWhat="delay accounting was not readable for the selection"
          span
          basis={
            <Basis
              items={[
                ['set aside', r.impossible_waits && Object.keys(r.impossible_waits).length
                  ? r.impossible_waits : null],
                ['why', r.impossible_basis],
              ]}
            />
          }
        >
          <FaultTable byClass={byClass} have={have} impossible={r.impossible_waits ?? {}} />
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
              <More key={key} label={`${label} · ${fint(rows.length)}`}>
                <InstanceTable rows={rows} showArgs />
              </More>
            );
          })}
        </Panel>

      </div>
    </>
  );
}
