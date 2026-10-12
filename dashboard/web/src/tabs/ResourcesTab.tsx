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
import type { Accum, Cls, Quant, Resources } from '../api/types';
import { TimeChart } from '../charts/TimeChart';
import type { TimeLine, View } from '../charts/TimeChart';
import { Basis, Panel, More } from '../components/Panel';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb, notMeasured, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import { InstanceTable, RiskIoTab } from './RiskIoTab';
import type { ResourceInstance } from '../api/types';
import type { Panels } from '../lib/panels';

const QUANT_KEYS = ['cpu_s', 'peak_rss_mb', 'sched_wait_s', 'duration_s', 'on_cpu_frac'] as const;

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

export { TimeChart };
export type { TimeLine, View };

export function ResourcesTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const r = pick<Resources>(P, 'resources', 'ebpfm');
  const byClass = r.by_class ?? {};
  const have = shown(byClass, cls);

  return (
    <>
      <div className="grid wide">
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
            ['longest D-state wait (uninterruptible sleep, the NFS/autofs signal)', r.worst_stall],
            ['most major faults (the ones that actually hit disk)', r.worst_fault],
            ['longest runqueue wait (ready, but not scheduled)', r.worst_wait],
          ] as [string, ResourceInstance[] | undefined][]).map(([label, list], i) => {
            const rows = list ?? [];
            if (!rows.length) return null;
            return (
              <More key={i} label={`${label} · ${fint(rows.length)}`}>
                <InstanceTable rows={rows} showArgs />
              </More>
            );
          })}
        </Panel>
      </div>

      {/* I/O is a resource (roadmap notes 41 and 42): its panels, as they were
          on the I/O tab. Its tiles became rows of the Overview table, and its
          time series moved to Live's picker. */}
      <div className="grid-gap" />
      <RiskIoTab P={P} cls={cls} part="io" plates={false} series={false} />
    </>
  );
}
