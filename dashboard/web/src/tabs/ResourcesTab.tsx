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
import type { Accum, Cls, MaxProcess, Quant, Resources } from '../api/types';
import { Term } from '../components/Term';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { GLOSS } from '../lib/glossary';
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
    ['faults', 'min_flt', 'minor page faults', 'count', GLOSS.minflt],
    ['faults', 'maj_flt', 'major page faults', 'count', GLOSS.majflt],
    ['faults', 'cmaj_flt', 'major faults, in reaped children', 'count', GLOSS.cmajflt],
    ['faults', 'nvcsw', 'gave up the CPU to wait', 'count', GLOSS.nvcsw],
    ['faults', 'nivcsw', 'preempted', 'count', GLOSS.nivcsw],
  ]],
  ['time spent waiting', [
    ['delays', 'blkio_wait_s', 'on block I/O', 'secs', GLOSS.blkio],
    ['delays', 'swapin_wait_s', 'on swap-in', 'secs', GLOSS.swapin],
    ['delays', 'freepages_wait_s', 'on memory reclaim', 'secs', GLOSS.reclaim],
  ]],
  ['uninterruptible sleep (D-state)', [
    ['dstate', 'events_with_stall', 'processes that stalled', 'share', GLOSS.dstate],
    ['dstate', 'wait_s', 'time stalled', 'secs', 'time spent in D-state, per process on average and summed beneath'],
    ['dstate', 'episodes', 'separate stalls', 'count', GLOSS.stall],
    ['dstate', 'max_s', 'longest single stall', 'max', 'the longest single stall of any process in the class'],
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
 * so a measure reads left to right. Each statistic has its own chip: mean,
 * median, and 99th start on, and the rest open on a click (roadmap note 28).
 * The max names the process behind it on hover (note 27); the min does not,
 * since many processes usually share it. Each class's columns wear its colour. */
const LIFETIME_TIP =
  'how long the process existed. For most processes it is counted from when the program ' +
  'started (exec), while CPU time counts the whole process, including the moments before ' +
  'exec, so a very short process can show more CPU time than lifetime.';
const DIST_ROWS: [key: (typeof QUANT_KEYS)[number], label: string, f: (v: number) => string, tip: string][] = [
  ['cpu_s', 'CPU time', dur, 'CPU time each process used itself (its children are not added)'],
  ['duration_s', 'lifetime', dur, LIFETIME_TIP],
  ['on_cpu_frac', 'share of lifetime on CPU', (v) => pc(100 * v), GLOSS.oncpu],
  ['sched_wait_s', 'runqueue wait', dur, GLOSS.runqueue],
  ['peak_rss_mb', 'peak memory', mb, GLOSS.peakrss],
];
type QKey = 'mean' | 'min' | 'p1' | 'p10' | 'p50' | 'p90' | 'p99' | 'max';
// in column order, which is the chips' order too
const STATS: [QKey, string, string][] = [
  ['mean', 'mean', GLOSS.mean],
  ['min', 'min', GLOSS.min],
  ['p1', '1st', GLOSS.p1],
  ['p10', '10th', GLOSS.p10],
  ['p50', 'median', GLOSS.median],
  ['p90', '90th', GLOSS.p90],
  ['p99', '99th', GLOSS.p99],
  ['max', 'max', GLOSS.max + '; hover a value for the process'],
];
const STATS_ON: QKey[] = ['mean', 'p50', 'p99'];
const tint = (c: string) => ({ background: `color-mix(in srgb, ${CLS_COL[c] ?? 'transparent'} 6%, transparent)` });

function maxTip(t: MaxProcess | undefined): string | undefined {
  if (!t) return undefined;
  return [
    `${t.tool ?? '?'} · ${t.user ?? '?'} · pid ${t.pid ?? '?'}${t.host ? ' on ' + t.host : ''}`,
    t.ts ? `exited ${t.ts}` : null,
    t.args ? t.args : null,
  ].filter(Boolean).join('\n');
}

// one chip per statistic, as the class filter has one per class
function StatChips({ on, onChange }: { on: QKey[]; onChange: (v: QKey[]) => void }) {
  const toggle = (k: QKey) =>
    onChange(on.includes(k) ? on.filter((x) => x !== k) : STATS.map(([x]) => x).filter((x) => x === k || on.includes(x)));
  return (
    <>
      {STATS.map(([k, label, tip]) => {
        const sel = on.includes(k);
        const last = sel && on.length === 1;
        return (
          <button
            key={k}
            className="chip"
            aria-pressed={sel}
            disabled={last}
            onClick={() => toggle(k)}
            data-tip={last ? 'at least one statistic must stay shown' : `${sel ? 'hide' : 'show'} the ${tip}`}
          >
            {label}
          </button>
        );
      })}
    </>
  );
}

function DistTable({ byClass, have, on }: { byClass: Resources['by_class']; have: string[]; on: QKey[] }) {
  const qs = STATS.filter(([k]) => on.includes(k));
  // one row per measure, each in its own unit: the measures sort by name, and
  // a class's column, which would rank seconds against megabytes, does not
  const s = useSort<string>();
  return (
    <div className="scroller">
      <table className="dist">
        <thead>
          <tr>
            <SortTh s={s} k="measure" first="asc" rowSpan={2}>per process</SortTh>
            {have.map((c) => (
              <th key={c} colSpan={qs.length} className="grp" style={{ ...tint(c), borderTop: `3px solid ${CLS_COL[c]}` }}>
                <span style={{ color: CLS_COL[c] }}>{CLS_LBL[c] ?? c}</span>
                <span className="sub"> · {fint(byClass[c]?.events)} processes</span>
              </th>
            ))}
          </tr>
          <tr>
            {have.flatMap((c) =>
              qs.map(([k, l, tip], i) => (
                <th key={c + k} className={`num${i === 0 ? ' g0' : ''}`} style={tint(c)} data-tip={tip}>{l}</th>
              )),
            )}
          </tr>
        </thead>
        <tbody>
          {s.sort(DIST_ROWS, ([, label]) => label).map(([qk, label, f, tip]) => {
            const aside = qk === 'on_cpu_frac'
              ? have.reduce((a, c) => a + (byClass[c]?.oncpu_set_aside ?? 0), 0) : 0;
            return (
              <tr key={qk}>
                <td>
                  <Term tip={tip}>{label}</Term>
                  {aside > 0 && (
                    <div className="sub" data-tip={
                      `${fint(aside)} processes ran longer on a CPU than their lifetime as measured (it was counted from exec), ` +
                      'so their share cannot be read; they are left out rather than shown as 100%'}>
                      {fint(aside)} set aside
                    </div>
                  )}
                </td>
                {have.flatMap((c) => {
                  const q = byClass[c]?.quants?.[qk] as Quant | undefined;
                  const top = qk === 'on_cpu_frac' ? undefined : byClass[c]?.top?.[qk];
                  return qs.map(([k], i) => {
                    const v = q?.n ? q[k] : null;
                    const isMax = k === 'max' && top;
                    return (
                      <td key={c + k} className={`num${i === 0 ? ' g0' : ''}`} style={tint(c)}>
                        {v == null ? DASH : isMax ? <span className="term" data-tip={maxTip(top)}>{f(v)}</span> : f(v)}
                      </td>
                    );
                  });
                })}
              </tr>
            );
          })}
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
  // Every section starts closed: a reader opens what they came for (note 31).
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // the measures sort by name within their section, as in the table above
  const s = useSort<string>();
  return (
    <div className="scroller">
      <table className="faults">
        <thead>
          <tr>
            <SortTh s={s} k="measure" first="asc">average per process</SortTh>
            {have.map((c) => (
              <th key={c} className="num" style={{ ...tint(c), borderTop: `3px solid ${CLS_COL[c]}` }}>
                <span style={{ color: CLS_COL[c] }}>{CLS_LBL[c] ?? c}</span>
                <div className="sub">{fint(byClass[c]?.events)} processes</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {FAULT_ROWS.flatMap(([section, rows]) => [
            <tr key={section} className="section fold" onClick={() => setOpen((m) => ({ ...m, [section]: !m[section] }))}>
              <td colSpan={have.length + 1}>
                <span className="u-caret">{open[section] ? '▾' : '▸'}</span>
                {section}
                {!open[section] && <span className="nm"> · {rows.length} measures, click to open</span>}
              </td>
            </tr>,
            ...(open[section] ? s.sort(rows, ([, , label]) => label).map(([g, k, label, kind, tip]) => (
              <tr key={g + k}>
                <td><Term tip={tip}>{label}</Term></td>
                {have.map((c) => (
                  <td key={c} className="num" style={tint(c)}>
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
            )) : []),
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
  const [stats, setStats] = useState<QKey[]>(STATS_ON);

  return (
    <>
      <div className="grid wide">
        <Panel
          title="Per-process distributions"
          ask={[{ panel: 'resources', keys: ['by_class', 'top', 'oncpu_set_aside', 'scope', 'cpu_basis'] }]}
          id="sec-per-event-distributions"
          p={r}
          span
          empty={!have.length}
          emptyWhat="no class block for the selection"
          header={<StatChips on={stats} onChange={setStats} />}
        >
          <DistTable byClass={byClass} have={have} on={stats} />
        </Panel>

        <Panel
          title="Faults, delays, and D-state"
          ask={[{ panel: 'resources', keys: ['by_class', 'stall_by_tool', 'impossible_waits', 'impossible_basis'] }]}
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
          ask={[{ panel: 'resources', keys: ['worst_stall', 'worst_fault', 'worst_wait'] }]}
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
