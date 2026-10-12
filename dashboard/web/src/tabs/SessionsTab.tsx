/* Sessions & tools -- the `tool_mix` payload.
 *
 * The bucket mix is a table of calls share beside CPU share, never one dual
 * axis: on this cluster `poll/probe` is ~29% of calls and ~0% of CPU, and one
 * pair of axes would make those look like the same quantity.
 */
import { useState } from 'react';
import type { Cls, ToolBucketRow, ToolClassBlock, ToolCostRow, ToolMix } from '../api/types';
import { Segmented } from '../components/ClassFilter';
import { Term } from '../components/Term';
import { GLOSS } from '../lib/glossary';
import { HBars } from '../charts/primitives';
import { Basis, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, mb, pc } from '../lib/format';
import { pick, shown, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

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

/* Count and cost side by side, as a table rather than two 36-bar charts:
 * one row per kind of tool, a calls column and a CPU column per class. A
 * bucket no class touched in the window is left out and counted, not drawn as
 * a row of zeros. */
function MixTable({
  buckets,
  have,
  byClass,
}: {
  buckets: string[];
  have: string[];
  byClass: Record<string, ToolClassBlock>;
}) {
  const get = (c: string, b: string) => byClass[c]?.rows?.find((r) => r.b === b);
  const lead = have.includes('agent') ? 'agent' : have[0];
  const rows = buckets
    .filter((b) => have.some((c) => (get(c, b)?.calls_pct ?? 0) >= 0.05 || (get(c, b)?.cpu_pct ?? 0) >= 0.05))
    .sort((x, y) => (get(lead, y)?.calls_pct ?? 0) - (get(lead, x)?.calls_pct ?? 0));
  const idle = buckets.filter((b) => !rows.includes(b));
  return (
    <div className="scroller">
      <table className="mix">
        <thead>
          <tr>
            <th rowSpan={2}>tool category</th>
            {have.map((c) => (
              <th key={c} colSpan={2} className="cls" style={{ color: CLS_COL[c] }}>
                {CLS_LBL[c] ?? c}
              </th>
            ))}
          </tr>
          <tr>
            {have.flatMap((c) => [
              <th key={c + 'n'} data-tip="share of the class's processes exited">processes</th>,
              <th key={c + 'c'} data-tip="share of the class's CPU seconds">CPU</th>,
            ])}
          </tr>
        </thead>
        <tbody>
          {rows.map((b) => (
            <tr key={b}>
              <td data-tip={BUCKET_TIP[b]}>{b}</td>
              {have.flatMap((c) => {
                const r = get(c, b);
                return [
                  <td key={c + 'n'} data-tip={r ? `${fint(r.calls)} processes` : undefined}>
                    <MiniBar pct={r ? r.calls_pct : null} c={CLS_COL[c]} />
                  </td>,
                  <td key={c + 'c'} data-tip={r ? `${fmt(r.cpu_s)} CPU s` : undefined}>
                    <MiniBar pct={r ? r.cpu_pct : null} c={CLS_COL[c]} faint />
                  </td>,
                ];
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {idle.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--muted)' }}>
          nothing ran in the window: {idle.join(', ')}
        </div>
      )}
    </div>
  );
}

/** The tool mix and the head of the tool distribution, on the Agents tab. */
export function ToolPanels({ P, cls }: { P: Panels; cls: Cls[] }) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const byClass = tm.by_class ?? {};
  const have = shown(byClass, cls);
  const buckets = tm.buckets ?? [];
  return (
    <div className="grid wide">
      <ToolMixPanel tm={tm} have={have} buckets={buckets} byClass={byClass} />
      <HeadPanel tm={tm} have={have} />
    </div>
  );
}

/* One resource at a time (roadmap note 25): a table that put calls and CPU
 * side by side, per class, asked the reader to hold four numbers per cell. */
type Metric = 'calls' | 'cpu' | 'io' | 'net';
const METRICS: { k: Metric; lbl: string; tip: string }[] = [
  { k: 'calls', lbl: 'processes', tip: GLOSS.exited },
  { k: 'cpu', lbl: 'CPU', tip: 'CPU seconds the processes used' },
  { k: 'io', lbl: 'disk I/O', tip: 'disk bytes read and written' },
  { k: 'net', lbl: 'network', tip: 'bytes sent and received over the network' },
];
const rowVal = (r: ToolBucketRow | undefined, m: Metric): number | null =>
  !r ? null : m === 'calls' ? r.calls : m === 'cpu' ? r.cpu_s : m === 'io' ? r.io_mb ?? null : r.net_mb ?? null;
const rowPct = (r: ToolBucketRow | undefined, m: Metric): number | null =>
  !r ? null : m === 'calls' ? r.calls_pct : m === 'cpu' ? r.cpu_pct : m === 'io' ? r.io_pct ?? null : r.net_pct ?? null;
const fmtVal = (v: number | null, m: Metric) =>
  v == null ? DASH : m === 'calls' ? fint(v) : m === 'cpu' ? dur(v) : mb(v);

function ToolMixPanel({ tm, have, buckets, byClass }: {
  tm: ToolMix;
  have: string[];
  buckets: string[];
  byClass: Record<string, ToolClassBlock>;
}) {
  // Read across by default: each kind of tool split by who ran it, so a row
  // adds to 100% the way a reader's eye travels (roadmap note 25). The other
  // reading -- each class's work split into kinds of tool -- is a click away.
  const [read, setRead] = useState<'who' | 'kind'>('who');
  const [metric, setMetric] = useState<Metric>('calls');
  const get = (c: string, b: string) => byClass[c]?.rows?.find((r) => r.b === b);
  const total = (b: string) => have.reduce((a, c) => a + (rowVal(get(c, b), metric) ?? 0), 0);
  const rows = buckets.filter((b) => total(b) > 0).sort((x, y) => total(y) - total(x));
  const idle = buckets.filter((b) => !rows.includes(b));
  return (
    <Panel
      title="Tool mix"
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
                <th>kind of tool</th>
                <th>who ran it, as a share of the row</th>
                <th className="num">total</th>
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
                              data-tip={`${CLS_LBL[c] ?? c}: ${pc(p)} (${fmtVal(v, metric)})`} />
                          ) : null;
                        })}
                      </span>
                      <span className="whopct">
                        {have.map((c) => {
                          const v = rowVal(get(c, b), metric) ?? 0;
                          return v > 0 ? (
                            <span key={c}><i style={{ background: CLS_COL[c] }} />{pc((100 * v) / tot)}</span>
                          ) : null;
                        })}
                      </span>
                    </td>
                    <td className="num">{fmtVal(tot, metric)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <table className="mix">
            <thead>
              <tr>
                <th>kind of tool</th>
                {have.map((c) => (
                  <th key={c} className="cls" style={{ color: CLS_COL[c] }}
                    data-tip={`share of the ${CLS_LBL[c] ?? c} class's ${METRICS.find((m) => m.k === metric)!.lbl}`}>
                    {CLS_LBL[c] ?? c}
                  </th>
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
                        <MiniBar pct={rowPct(r, metric)} c={CLS_COL[c]} />
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

function HeadPanel({ tm, have }: { tm: ToolMix; have: string[] }) {
  const [by, setBy] = useState<HeadSort>('calls');
  const tools = tm.tools;
  const head = tm.head ?? {};
  return (
    <Panel
      title="Top tools"
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
                {CLS_LBL[c] ?? c}
              </div>
              {rows ? (
                rows.length ? (
                  <HBars
                    items={rows.map((r) => ({ k: r.tool, v: headVal(r, by) ?? 0, c: CLS_COL[c], lbl: headLbl(r, by), tip: headTip(r) }))}
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

export function SessionsTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const win = winLabel(tm);
  const byClass = tm.by_class ?? {};
  const have = shown(byClass, cls);
  const buckets = tm.buckets ?? [];



  const head = tm.head ?? {};
  const topAgent = cls.includes('agent') ? head.agent?.[0] : undefined;
  // polling = the poll/probe and slurm-query buckets, as a share of the class's calls
  const POLL_BUCKETS = new Set(['poll/probe', 'slurm-query']);
  const pollPct = Object.fromEntries(
    have.map((c) => {
      const b = byClass[c];
      if (!b?.calls) return [c, null];
      const n = (b.rows ?? []).filter((r) => POLL_BUCKETS.has(r.b)).reduce((a, r) => a + (r.calls ?? 0), 0);
      return [c, (100 * n) / b.calls];
    }),
  );

  return (
    <>
      {/* What the agents spend their tool calls on: the one tool that dominates,
          how much of it is only waiting, and how varied the rest is. */}
      <div className="plates three">
        <Plate
          k={topAgent ? `agents' top tool · ${topAgent.tool}` : "agents' top tool"}
          total={topAgent?.calls_pct ?? null}
          stripe="var(--s1)"
          by={{}}
          classes={[]}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip="the single tool agents ran most, as a share of all agent tool calls"
          note={
            topAgent
              ? `of agent tool calls, ${pc(topAgent.cpu_pct)} of their CPU` +
                have
                  .filter((c) => c !== 'agent' && head[c]?.[0])
                  .map((c) => ` · ${CLS_LBL[c] ?? c}: ${head[c]![0].tool} ${pc(head[c]![0].calls_pct)}`)
                  .join('')
              : undefined
          }
        />
        <Plate
          k="agent tool calls spent polling"
          total={cls.includes('agent') ? pollPct.agent ?? null : null}
          stripe="var(--s4)"
          by={pollPct}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip="calls that only check on something: the process table (ps, pgrep, top, sleep, uname, …) and the scheduler (squeue, sacct, scontrol, sinfo, …)"
          note={cls.includes('agent') && byClass.agent ? `of ${fint(byClass.agent.calls)} agent tool calls ${win}` : undefined}
        />
        <Plate
          k="distinct tools agents ran"
          total={cls.includes('agent') ? tm.n_tools?.agent ?? null : null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, tm.n_tools?.[c] ?? null]))}
          tip="different commands, after resolving shell wrappers to the command they ran"
          note={`the twelve most used are listed below, per class`}
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Tool mix — counted, then costed"
          p={tm}
          span
          empty={!have.length || !buckets.length}
          emptyWhat="the feed carried no bucket rows for the selected classes"
          basis={
            <Basis
              items={[
                ['cpu_basis', tm.cpu_basis],
                ['dedup', tm.dedup?.rule],
                ['dropped', tm.dedup ? `${fint(tm.dedup.dropped)} (${pc(tm.dedup.dropped_pct)})` : null],
              ]}
            />
          }
        >
          <MixTable buckets={buckets} have={have} byClass={byClass} />
        </Panel>

        <Panel
          title="Head of the tool distribution"
          p={tm}
          span
          empty={!have.some((c) => (head[c]?.length ?? 0) > 0)}
          emptyWhat="no head list for the selected classes"
        >
          {/* the classes side by side, so one tool reads across all three */}
          <div className="head-row" style={{ gridTemplateColumns: `repeat(${have.length || 1}, minmax(0, 1fr))` }}>
          {have.map((c) => {
            const rows = head[c] ?? [];
            if (!rows.length) return <div key={c} />;
            return (
              <div key={c}>
                <div className="eyebrow" style={{ margin: '8px 0 5px', color: CLS_COL[c] }}>
                  {CLS_LBL[c] ?? c}
                </div>
                <HBars
                  items={rows.slice(0, 12).map((r) => ({
                    k: r.tool,
                    v: r.calls_pct,
                    c: CLS_COL[c],
                    lbl: `${pc(r.calls_pct)} processes / ${pc(r.cpu_pct)} CPU`,
                    tip: `${r.tool}: ${fint(r.calls)} processes, ${pc(r.calls_pct)} of the class's processes, ${pc(
                      r.cpu_pct,
                    )} of its CPU`,
                  }))}
                  W={380}
                  labelW={100}
                  valW={150}
                />
              </div>
            );
          })}
          </div>
        </Panel>
      </div>
    </>
  );
}
