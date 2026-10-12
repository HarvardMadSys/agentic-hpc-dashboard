/* Sessions & tools -- the `tool_mix` payload.
 *
 * The bucket mix is a table of calls share beside CPU share, never one dual
 * axis: on this cluster `poll/probe` is ~29% of calls and ~0% of CPU, and one
 * pair of axes would make those look like the same quantity.
 */
import type { Cls, ToolClassBlock, ToolMix } from '../api/types';
import { HBars } from '../charts/primitives';
import { Basis, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
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
  const head = tm.head ?? {};
  return (
    <div className="grid wide">
      <ToolMixPanel tm={tm} have={have} buckets={buckets} byClass={byClass} />
      <HeadPanel tm={tm} have={have} head={head} />
    </div>
  );
}

function ToolMixPanel({ tm, have, buckets, byClass }: {
  tm: ToolMix;
  have: string[];
  buckets: string[];
  byClass: Record<string, ToolClassBlock>;
}) {
  return (
    <Panel
      title="Tool mix: processes, then CPU"
      p={tm}
      span
      empty={!have.length || !buckets.length}
      emptyWhat="the feed carried no bucket rows for the selected classes"
    >
      <MixTable buckets={buckets} have={have} byClass={byClass} />
    </Panel>
  );
}

function HeadPanel({ tm, have, head }: { tm: ToolMix; have: string[]; head: NonNullable<ToolMix['head']> }) {
  return (
    <Panel
      title="Head of the tool distribution"
      p={tm}
      span
      empty={!have.some((c) => (head[c]?.length ?? 0) > 0)}
      emptyWhat="no head list for the selected classes"
    >
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
                  tip: `${r.tool}: ${fint(r.calls)} processes, ${pc(r.calls_pct)} of the class's processes, ${pc(r.cpu_pct)} of its CPU`,
                }))}
                W={380}
                labelW={100}
                valW={170}
              />
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
