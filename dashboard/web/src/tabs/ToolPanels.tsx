/* The tool panels on the Agents tab -- the `tool_mix` payload: the tool mix
 * and the top tools. (They were the Sessions & tools tab.)
 *
 * One resource at a time, never two on one axis: on this cluster `poll/probe`
 * is ~29% of calls and ~0% of CPU, and one pair of axes would make those look
 * like the same quantity.
 */
import { useState } from 'react';
import type { Cls, IoSort, ToolBucketRow, ToolClassBlock, ToolCostRow, ToolMix } from '../api/types';
import { Segmented } from '../components/ClassFilter';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { Term } from '../components/Term';
import { GLOSS } from '../lib/glossary';
import { HBars } from '../charts/primitives';
import { Panel } from '../components/Panel';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, mb, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { coveredBy, openIoCmds } from '../lib/iocmds';
import type { IoQuery } from '../lib/iocmds';
import { IoNum } from '../components/IoCmds';

/* What each kind of tool looks like, so a bucket name is never a riddle. */
const BUCKET_TIP: Record<string, string> = {
  'poll/probe': 'checking on things: ps, pgrep, top, sleep, uname, lsb_release, nvidia-smi',
  'slurm-submit': 'putting work on the cluster: sbatch, srun, salloc, scancel',
  'slurm-query': 'asking the scheduler: squeue, sacct, sinfo, scontrol',
  git: 'git and its helpers',
  runtime: 'the agent and editor runtimes themselves: node, claude, codex, cursor',
  interpreter: 'python, pytest, jupyter, wandb',
  'shell/env-init': 'shells and environment setup: bash, zsh, conda, module, pip',
  search: 'grep, rg, find, fd',
  build: 'compilers and build tools: make, cmake, gcc, cargo, nvcc',
  'net/data': 'moving data: rsync, wget, curl, scp, aws',
  files: 'reading and editing files: cat, ls, head, sed, awk, vim',
  other: 'everything the taxonomy does not name',
};

/** A share as a short inline bar with its value beside it. */
function MiniBar({ pct: v, c, faint }: { pct: number | null | undefined; c: string; faint?: boolean }) {
  if (v == null) return <span className="nm">{DASH}</span>;
  return (
    <span className="minibar">
      <i style={{ width: `${Math.min(100, Math.max(v > 0 ? 1.5 : 0, v))}%`, background: c, opacity: faint ? 0.45 : 1 }} />
      <b>{v > 0 && v < 0.05 ? '<0.1%' : pc(v)}</b>
    </span>
  );
}

/** The tool mix and the head of the tool distribution, on the Agents tab.
 *
 * With an agent type picked, the agent column is that type's processes alone,
 * beside people and IDEs: the Agents tab never mixes one type's tools with
 * another's. A window built before tools were counted per type has only the
 * class, and says so. */
export function ToolPanels({ P, cls, type = null }: { P: Panels; cls: Cls[]; type?: string | null }) {
  const raw = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const one = type ? raw.by_agent_type?.[type] : undefined;
  const tm: ToolMix = one
    ? {
        ...raw,
        by_class: { ...raw.by_class, agent: one.by },
        tools: { ...(raw.tools ?? {}), agent: one.tools },
        head: { ...raw.head, agent: one.head },
        n_tools: { ...(raw.n_tools ?? {}), agent: one.n_tools },
      }
    : raw;
  const byClass = tm.by_class ?? {};
  const have = shown(byClass, cls);
  const buckets = tm.buckets ?? [];
  const typeName = type ? AGENT_TYPE_LBL[type] ?? type : null;
  const lbl = (c: string) => (c === 'agent' && one && typeName ? typeName : CLS_LBL[c] ?? c);
  const scope: ToolScope = {
    lbl,
    title: (t: string) => (one && typeName ? `${typeName}: ${t.toLowerCase()}` : t),
    note: type && !one && cls.includes('agent')
      ? 'every agent type together: this window was read before tools were counted per type'
      : null,
    askKeys: (keys: string[]) => (one && type ? [...keys, `by_agent_type.${type}`] : keys),
    ioq: (c: string) => (c === 'agent' && one && type ? { class: [c], agent_type: [type] } : { class: [c] }),
  };
  return (
    <div className="grid wide">
      <ToolMixPanel tm={tm} have={have} buckets={buckets} byClass={byClass} scope={scope} />
      <HeadPanel tm={tm} have={have} scope={scope} />
    </div>
  );
}

/** How a tool panel names its columns and itself, with or without a type. */
interface ToolScope {
  lbl: (c: string) => string;
  title: (t: string) => string;
  note: string | null;
  askKeys: (keys: string[]) => string[];
  /** a column's processes, for the commands behind one of its numbers */
  ioq: (c: string) => Partial<IoQuery>;
}
const CLASS_SCOPE: ToolScope = {
  lbl: (c) => CLS_LBL[c] ?? c,
  title: (t) => t,
  note: null,
  askKeys: (keys) => keys,
  ioq: (c) => ({ class: [c] }),
};

/* One resource at a time (roadmap note 25): a table that put calls and CPU
 * side by side, per class, asked the reader to hold four numbers per cell. */
type Metric = 'calls' | 'cpu' | 'io' | 'net' | 'mem';
const METRICS: { k: Metric; lbl: string; tip: string }[] = [
  { k: 'calls', lbl: 'processes', tip: GLOSS.exited },
  { k: 'cpu', lbl: 'CPU', tip: 'CPU seconds the processes used' },
  { k: 'io', lbl: 'disk I/O', tip: 'disk bytes read and written' },
  { k: 'net', lbl: 'network', tip: 'bytes sent and received over the network' },
  { k: 'mem', lbl: 'memory', tip: 'each process’s peak memory, summed: how much memory the processes held at their highest' },
];
const rowVal = (r: ToolBucketRow | undefined, m: Metric): number | null =>
  !r ? null : m === 'calls' ? r.calls : m === 'cpu' ? r.cpu_s : m === 'io' ? r.io_mb ?? null
  : m === 'net' ? r.net_mb ?? null : r.mem_mb ?? null;
const rowPct = (r: ToolBucketRow | undefined, m: Metric): number | null =>
  !r ? null : m === 'calls' ? r.calls_pct : m === 'cpu' ? r.cpu_pct : m === 'io' ? r.io_pct ?? null
  : m === 'net' ? r.net_pct ?? null : r.mem_pct ?? null;
const fmtVal = (v: number | null, m: Metric) =>
  v == null ? DASH : m === 'calls' ? fint(v) : m === 'cpu' ? dur(v) : mb(v);
/** the drawer's sort for a byte metric; null for the rest, which open nothing */
const ioSort = (m: Metric): IoSort | null => (m === 'io' ? 'disk_mb' : m === 'net' ? 'net_mb' : null);

function ToolMixPanel({ tm, have, buckets, byClass, scope = CLASS_SCOPE }: {
  tm: ToolMix;
  have: string[];
  buckets: string[];
  byClass: Record<string, ToolClassBlock>;
  scope?: ToolScope;
}) {
  // Read across by default: each kind of tool split by who ran it, so a row
  // adds to 100% the way a reader's eye travels (roadmap note 25). The other
  // reading -- each class's work split into kinds of tool -- is a click away.
  const [read, setRead] = useState<'who' | 'kind'>('who');
  const [metric, setMetric] = useState<Metric>('calls');
  const s = useSort<string>();
  const get = (c: string, b: string) => byClass[c]?.rows?.find((r) => r.b === b);
  const total = (b: string) => have.reduce((a, c) => a + (rowVal(get(c, b), metric) ?? 0), 0);
  // the taxonomy's order, the same in both readings and for every metric, until
  // a header is clicked (roadmap note 27): by name, by the total, or by a
  // class's share -- of the row, or of the class's work
  const rows = s.sort(
    buckets.filter((b) => total(b) > 0),
    (b, k) => {
      if (k === 'kind') return b;
      if (k === 'total') return total(b);
      const c = k.slice(4);
      if (k.startsWith('who:')) return total(b) ? (100 * (rowVal(get(c, b), metric) ?? 0)) / total(b) : null;
      return rowPct(get(c, b), metric);
    },
  );
  const idle = buckets.filter((b) => !rows.includes(b));
  return (
    <Panel
      title={scope.title('Tool mix')}
      id="sec-tool-mix"
      ask={[{ panel: 'tool_mix', keys: scope.askKeys(['by_class', 'by_type', 'buckets']) }]}
      p={tm}
      span
      empty={!have.length || !buckets.length}
      emptyWhat="the feed carried no bucket rows for the selected classes"
      header={
        <span className="wrapctl" style={{ gap: 6 }}>
          <Segmented value={read} onChange={setRead} options={[
            { k: 'who', lbl: 'by who ran it', tip: 'each kind of tool, split by class: a row adds to 100%' },
            { k: 'kind', lbl: 'by kind of tool', tip: "each class's work, split by kind of tool: a column adds to 100%" },
          ]} />
          <Segmented value={metric} onChange={setMetric} options={METRICS.map((m) => ({ k: m.k, lbl: m.lbl, tip: m.tip }))} />
        </span>
      }
    >
      <div className="scroller">
        {read === 'who' ? (
          <table className="mix who">
            <thead>
              <tr>
                <SortTh s={s} k="kind" first="asc">kind of tool</SortTh>
                <SortTh s={s} k={`who:${have[0] ?? ''}`}
                  tip={`who ran it, as a share of the row; sorts by ${scope.lbl(have[0] ?? '')}'s share`}>
                  who ran it, as a share of the row
                </SortTh>
                <SortTh s={s} k="total" num>total</SortTh>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => {
                const tot = total(b);
                return (
                  <tr key={b}>
                    <td><Term tip={BUCKET_TIP[b]}>{b}</Term></td>
                    <td style={{ minWidth: 320 }}>
                      <span className="whobar">
                        {have.map((c) => {
                          const v = rowVal(get(c, b), metric) ?? 0;
                          const p = tot ? (100 * v) / tot : 0;
                          return p > 0 ? (
                            <i key={c} style={{ width: `${p}%`, background: CLS_COL[c] }}
                              data-tip={`${scope.lbl(c)}: ${pc(p)} (${fmtVal(v, metric)})`} />
                          ) : null;
                        })}
                      </span>
                      <span className="whopct">
                        {have.map((c) => {
                          const v = rowVal(get(c, b), metric) ?? 0;
                          const io = ioSort(metric);
                          return v > 0 ? (
                            <span key={c}><i style={{ background: CLS_COL[c] }} />{io ? (
                              <IoNum q={{ title: `${METRICS.find((m) => m.k === metric)!.lbl} · ${b} · ${scope.lbl(c)}`,
                                sort: io, bucket: [b], ...scope.ioq(c), ...coveredBy(tm) }}>{pc((100 * v) / tot)}</IoNum>
                            ) : pc((100 * v) / tot)}</span>
                          ) : null;
                        })}
                      </span>
                    </td>
                    <td className="num">{ioSort(metric) && !scope.note && have.every((c) => !scope.ioq(c).agent_type) ? (
                      <IoNum q={{ title: `${METRICS.find((m) => m.k === metric)!.lbl} · ${b}`, sort: ioSort(metric)!,
                        bucket: [b], class: have, ...coveredBy(tm) }}>{fmtVal(tot, metric)}</IoNum>
                    ) : fmtVal(tot, metric)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <table className="mix">
            <thead>
              <tr>
                <SortTh s={s} k="kind" first="asc">kind of tool</SortTh>
                {have.map((c) => (
                  <SortTh key={c} s={s} k={`cls:${c}`} className="cls" style={{ color: CLS_COL[c] }}
                    tip={`share of ${scope.lbl(c)}'s ${METRICS.find((m) => m.k === metric)!.lbl}`}>
                    {scope.lbl(c)}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b}>
                  <td><Term tip={BUCKET_TIP[b]}>{b}</Term></td>
                  {have.map((c) => {
                    const r = get(c, b);
                    return (
                      <td key={c} data-tip={r ? fmtVal(rowVal(r, metric), metric) : undefined}>
                        {ioSort(metric) && r && rowVal(r, metric) ? (
                          <IoNum q={{ title: `${METRICS.find((m) => m.k === metric)!.lbl} · ${b} · ${scope.lbl(c)}`,
                            sort: ioSort(metric)!, bucket: [b], ...scope.ioq(c), ...coveredBy(tm) }}>
                            <MiniBar pct={rowPct(r, metric)} c={CLS_COL[c]} />
                          </IoNum>
                        ) : <MiniBar pct={rowPct(r, metric)} c={CLS_COL[c]} />}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {idle.length > 0 && (
          <div style={{ marginTop: 8, fontSize: 11, color: 'var(--muted)' }}>
            none in the window: {idle.join(', ')}
          </div>
        )}
        {scope.note && <div className="nm" style={{ marginTop: 6, fontSize: 11 }}>{scope.note}</div>}
      </div>
    </Panel>
  );
}

/* The head of the tool distribution, sorted by any one resource (note 24). */
type HeadSort = 'calls' | 'cpu' | 'mem' | 'io' | 'net';
const HEAD_SORTS: { k: HeadSort; lbl: string; tip: string }[] = [
  { k: 'calls', lbl: 'processes', tip: 'the most processes first' },
  { k: 'cpu', lbl: 'CPU', tip: 'the most CPU first' },
  { k: 'mem', lbl: 'memory', tip: 'the most memory per process first: the mean of each process’s peak' },
  { k: 'io', lbl: 'disk I/O', tip: 'the most disk bytes read and written first' },
  { k: 'net', lbl: 'network', tip: 'the most network bytes first' },
];
const headVal = (r: ToolCostRow, k: HeadSort): number | null =>
  k === 'calls' ? r.calls : k === 'cpu' ? r.cpu_s : k === 'mem' ? r.mem_mean_mb : k === 'io' ? r.io_mb : r.net_mb;
const headLbl = (r: ToolCostRow, k: HeadSort): string =>
  k === 'calls' ? `${pc(r.calls_pct)} of processes`
  : k === 'cpu' ? `${dur(r.cpu_s)} · ${pc(r.cpu_pct)} of CPU`
  : k === 'mem' ? `${mb(r.mem_mean_mb)} mean peak`
  : k === 'io' ? `${mb(r.io_mb)} disk`
  : `${mb(r.net_mb)} network`;
const headTip = (r: ToolCostRow) => [
  r.tool,
  `${fint(r.calls)} processes (${pc(r.calls_pct)} of the class's)`,
  `CPU ${dur(r.cpu_s)} (${pc(r.cpu_pct)})`,
  r.mem_mean_mb != null ? `peak memory: ${mb(r.mem_mean_mb)} on average, ${mb(r.mem_max_mb)} at most` : 'peak memory not reported',
  `disk ${mb(r.io_mb)} · network ${mb(r.net_mb)}`,
].join('\n');

function HeadPanel({ tm, have, scope = CLASS_SCOPE }: { tm: ToolMix; have: string[]; scope?: ToolScope }) {
  const [by, setBy] = useState<HeadSort>('calls');
  const tools = tm.tools;
  const head = tm.head ?? {};
  return (
    <Panel
      title={scope.title('Top tools')}
      ask={[{ panel: 'tool_mix', keys: scope.askKeys(['tools', 'head', 'n_tools']) }]}
      id="sec-head-of-the-tool-distribution"
      p={tm}
      span
      empty={!have.some((c) => (tools?.[c]?.length ?? head[c]?.length ?? 0) > 0)}
      emptyWhat="no head list for the selected classes"
      header={tools && (
        <span className="wrapctl" style={{ gap: 6 }}>
          <span className="nm">sort by</span>
          <Segmented value={by} onChange={setBy} options={HEAD_SORTS.map((m) => ({ k: m.k, lbl: m.lbl, tip: m.tip }))} />
        </span>
      )}
    >
      <div className="head-row" style={{ gridTemplateColumns: `repeat(${have.length || 1}, minmax(0, 1fr))` }}>
        {have.map((c) => {
          const rows: ToolCostRow[] | undefined = tools?.[c]
            ?.filter((r) => (headVal(r, by) ?? 0) > 0)
            .sort((a, b) => (headVal(b, by) ?? 0) - (headVal(a, by) ?? 0))
            .slice(0, 12);
          return (
            <div key={c}>
              <div className="eyebrow" style={{ margin: '8px 0 5px', color: CLS_COL[c] }}>
                {scope.lbl(c)}
              </div>
              {rows ? (
                rows.length ? (
                  <HBars
                    items={rows.map((r) => ({
                      k: r.tool, v: headVal(r, by) ?? 0, c: CLS_COL[c], lbl: headLbl(r, by), tip: headTip(r),
                      onPick: by === 'io' || by === 'net'
                        ? () => openIoCmds({
                            title: `${by === 'io' ? 'disk I/O' : 'network'} · ${r.tool} · ${scope.lbl(c)}`,
                            sort: by === 'io' ? 'disk_mb' : 'net_mb', tool: [r.tool], ...scope.ioq(c), ...coveredBy(tm),
                          })
                        : undefined,
                    }))}
                    W={380}
                    labelW={100}
                    valW={170}
                  />
                ) : <div className="none">none</div>
              ) : (
                <HBars
                  items={(head[c] ?? []).slice(0, 12).map((r) => ({
                    k: r.tool, v: r.calls_pct ?? 0, c: CLS_COL[c],
                    lbl: `${pc(r.calls_pct)} processes / ${pc(r.cpu_pct)} CPU`,
                  }))}
                  W={380}
                  labelW={100}
                  valW={170}
                />
              )}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
