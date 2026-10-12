/* Overview -- the three classes at a glance, in the units that disagree.
 *
 * Nothing here is a new measurement: it is the same panels the other tabs draw,
 * reduced to one comparison each, and the comparisons are deliberately shown in
 * MORE THAN ONE unit. A single unit is what lets a few loud users stand in for
 * a population, so events and CPU appear side by side, and the session grain
 * appears beside the event grain.
 */
import type { Cls, IoProcess, LiveResponse, Resources, RiskPanel, Sandbox, SlurmClass, SlurmPanel, ToolMix, Trajectories } from '../api/types';
import { Basis, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { ShareCell } from '../components/ShareCell';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, mb, pc } from '../lib/format';
import { pick, winLabel } from '../lib/panels';
import { TimeChart } from './ResourcesTab';
import type { TimeLine, View } from './ResourcesTab';
import { Fragment, useState } from 'react';
import { CALL_KNOWN, shareOfKnown } from '../lib/sandbox';
import type { Panels } from '../lib/panels';

/** One row per unit: the whole point is that the ranking changes between them. */
interface UnitRow {
  k: string;
  get: (c: string) => number | null;
  fmt?: (n: number | null) => string;
  tip?: string;
  /** where the quantity lives: the login node itself, or the jobs it asked Slurm for */
  group?: 'node' | 'slurm';
}

const GROUPS: Record<NonNullable<UnitRow['group']>, [string, string]> = {
  node: ['on this host', 'measured on the host the collector runs on'],
  slurm: [
    'asked of Slurm',
    'what the jobs submitted from these hosts REQUESTED on their sbatch / srun / salloc command lines. ' +
      'A request in the script\'s #SBATCH lines is not seen, and GPU use on the compute nodes is not measured: ' +
      'the collector watches only these hosts, and the scheduler\'s accounting (sacct) is not read here',
  ],
};

export function OverviewTab({
  P,
  cls,
  traj,
}: {
  P: Panels;
  live: LiveResponse | null;
  cls: Cls[];
  traj: Trajectories | null;
}) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const sl = pick<SlurmPanel>(P, 'slurm', 'ebpfm');
  const win = winLabel(tm);
  const res = pick<Resources>(P, 'resources', 'ebpfm');
  const io = pick<IoProcess>(P, 'io_process', 'ebpfm');
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');

  const tmBy = tm.by_class ?? {};
  const resBy = res.by_class ?? {};
  const ioBy = io.by_class ?? {};
  const have = cls.filter((c) => c in tmBy || c in resBy || c in ioBy);

  const totCalls = have.reduce((a, c) => a + (tmBy[c]?.calls ?? 0), 0);
  // What each class is made of -- which agents, which editors -- for the
  // hover on the calls and CPU plates.
  const typeTip = (key: 'calls_pct' | 'cpu_pct') =>
    have
      .map((c) => {
        const parts = Object.entries(tm.by_type?.[c] ?? {})
          .filter(([at, t]) => at && (t[key] ?? 0) >= 0.05)
          .sort((a, b) => (b[1][key] ?? 0) - (a[1][key] ?? 0))
          .map(([at, t]) => `${AGENT_TYPE_LBL[at] ?? at} ${pc(t[key])}`);
        return parts.length ? `${CLS_LBL[c] ?? c}: ${parts.join(' · ')}` : null;
      })
      .filter(Boolean)
      .join('\n') || undefined;
  const totCpu = have.reduce((a, c) => a + (tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s ?? 0), 0);

  /* Share of calls against share of CPU, one bar group per class, two axes that
   * are never the same axis. */
  // Each class's share of the node, bin by bin, from the resources series
  // (by exit time). Out of every class's records, not just the shown ones: a
  // share of a filtered total would change when the filter does.
  const rs = res.series;
  const [zoom, setZoom] = useState<View>(null);
  const view = zoom;
  const onView = setZoom;
  const shareLines = (field: 'n' | 'cpu_s'): TimeLine[] => {
    const by = rs?.by_class ?? {};
    const all = Object.keys(by);
    const tot = (rs?.t ?? []).map((_, i) => {
      const vals = all.map((c) => by[c]?.[field]?.[i]).filter((v): v is number => v != null);
      return vals.length ? vals.reduce((a, v) => a + v, 0) : null;
    });
    return cls
      .filter((c) => by[c])
      .map((c) => ({
        lbl: CLS_LBL[c] ?? c,
        c: CLS_COL[c],
        v: tot.map((t, i) => {
          const v = by[c]?.[field]?.[i];
          return t ? (100 * (v ?? 0)) / t : null;
        }),
      }));
  };

  const callIso = Object.fromEntries(
    cls.map((c) => [c, shareOfKnown(sb.tool_calls?.[c], 'isolated', sb.states?.tool_call_known ?? CALL_KNOWN)]),
  );
  const callIsoAgent = callIso.agent;
  const agentCpu = tmBy.agent?.cpu_s ?? resBy.agent?.cpu_s ?? null;
  const agentSum = cls.includes('agent') ? traj?.summary?.agent : undefined;
  const launch = sb.approval_by_class_sessions?.agent;
  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  const classSum = (rows: { classes?: Partial<Record<string, number>> }[] | undefined, c: string) =>
    (rows ?? []).reduce((a, r) => a + (r.classes?.[c] ?? 0), 0);
  const dgAgent = classSum(risk.dangerous?.rows, 'agent');
  const secAgent = classSum(risk.secrets?.rows, 'agent');
  // A risk module this cluster's profile leaves out is not counted (absent),
  // which must not read as zero: the plate names only what was counted.
  const hasDg = risk.dangerous != null;
  const hasSec = risk.secrets != null;
  const riskyAgent = hasDg || hasSec ? dgAgent + secAgent : null;
  const riskyBy = Object.fromEntries(
    have.map((c) => [c, classSum(risk.dangerous?.rows, c) + classSum(risk.secrets?.rows, c)]),
  );
  const failedAgent = sb.tool_calls?.agent?.failed ?? 0;

  // Peak resident memory per class (GB), from the resources series' samples.
  const peakGb = (c: string): number | null => {
    const v = (res.series?.resident?.[c]?.rss_mb ?? []).filter((x): x is number => x != null);
    return v.length ? Math.max(...v) / 1024 : null;
  };
  // A class with no Slurm record in a window the panel was reduced over asked
  // for nothing -- a measured 0; with no panel at all, nothing is known.
  const slurmOf = (c: string, f: (s: SlurmClass) => number | null) =>
    !sl._present ? null : sl.by_class?.[c] ? f(sl.by_class[c]) : 0;
  const units: UnitRow[] = ([
    { k: 'commands run', get: (c) => tmBy[c]?.calls ?? null,
      tip: 'tool calls: every process that ran, shells resolved to the command they ran' },
    { k: 'CPU time', get: (c) => {
        const v = tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s;
        return v == null ? null : v / 3600;
      }, fmt: (n) => (n == null ? DASH : fmt(n) + ' h') },
    { k: 'disk read', get: (c) => ioBy[c]?.rd_mb ?? null, fmt: (n) => mb(n) },
    { k: 'disk written', get: (c) => ioBy[c]?.wr_mb ?? null, fmt: (n) => mb(n) },
    { k: 'peak memory', get: peakGb, fmt: (n) => (n == null ? DASH : fmt(n) + ' GB'),
      tip: 'the highest resident memory in the window, sampled while processes ran' },
    { k: 'sessions', get: (c) => traj?.summary?.[c]?.sessions ?? null,
      tip: 'one agent and everything it ran, or one login / terminal; scripts that only probe an agent are not counted' },
    { k: 'jobs submitted', group: 'slurm', get: (c) => slurmOf(c, (s) => s.jobs),
      tip: 'sbatch / srun / salloc submissions that came back with a job id; dry runs (--test-only) are not jobs' },
    { k: 'GPUs asked', group: 'slurm', get: (c) => slurmOf(c, (s) => s.gpus),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU${n === 1 ? '' : 's'}`),
      tip: 'GPUs on the command line (--gres gpu:N, --gpus), times the tasks of an array; a #SBATCH request in the script is not seen\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)} GPU jobs`).join(' · ') },
    { k: 'GPU-hours asked', group: 'slurm', get: (c) => slurmOf(c, (s) => (s.gpu_jobs ? s.gpu_h : 0)),
      fmt: (n) => (n == null ? DASH : `${fint(n)} GPU·h`),
      tip: 'GPUs × the time limit asked for -- a ceiling, not use -- over the GPU jobs that put both on the command line\n' +
        have.map((c) => `${CLS_LBL[c] ?? c}: ${fint(sl.by_class?.[c]?.gpu_h_known ?? 0)} of ${fint(sl.by_class?.[c]?.gpu_jobs ?? 0)}`).join(' · ') },
  ] as UnitRow[]).filter((u) => have.some((c) => u.get(c) != null));

  return (
    <>
      {/* The window in four numbers an operator acts on: how much the agents
          did, what it cost, how many sessions that was, and how they were
          launched, and what they did that they should not have. The headline
          is the agent's; the other classes sit beside it for scale. */}
      <div className="plates">
        <Plate
          k={`agent tool calls ${win}`}
          tip={typeTip('calls_pct')}
          total={cls.includes('agent') ? tmBy.agent?.calls ?? null : null}
          stripe="var(--s1)"
          by={Object.fromEntries(have.map((c) => [c, tmBy[c]?.calls ?? null]))}
          note={totCalls ? `${pc((100 * (tmBy.agent?.calls ?? 0)) / totCalls)} of every tool call on the node` : undefined}
        />
        <Plate
          k={`agent CPU ${win}`}
          tip={typeTip('cpu_pct')}
          total={cls.includes('agent') && agentCpu != null ? agentCpu / 3600 : null}
          stripe="var(--s6)"
          by={Object.fromEntries(
            have.map((c) => {
              const v = tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s;
              return [c, v == null ? null : v / 3600];
            }),
          )}
          fmtFn={(n) => fmt(n)}
          unit="CPU-h"
          note={totCpu ? `${pc((100 * (agentCpu ?? 0)) / totCpu)} of all CPU on the node` : undefined}
        />
        <Plate
          k="agent sessions"
          total={agentSum?.sessions ?? null}
          stripe="var(--s4)"
          by={Object.fromEntries(cls.filter((c) => traj?.summary?.[c]).map((c) => [c, traj!.summary![c].sessions]))}
          tip="a session is one agent process and everything it ran; scripts that only probe `codex --version` are not counted"
          note={
            launch
              ? [
                  launch.bypassed ? `${fint(launch.bypassed)} with a bypass flag` : null,
                  launch.auto ? `${fint(launch.auto)} in auto mode` : null,
                  launch.supervised ? `${fint(launch.supervised)} with no bypass flag` : null,
                ]
                  .filter(Boolean)
                  .join(' · ') || undefined
              : undefined
          }
        />
        <Plate
          k="risky agent actions"
          total={cls.includes('agent') && risk._present ? riskyAgent : null}
          stripe="var(--crit)"
          by={riskyBy}
          tip="commands this site's rules flag (rm -rf on a shared path, builds where they should not run, bulk downloads, …) plus credentials written into a command line"
          note={
            ([hasDg ? `${fint(dgAgent)} dangerous commands` : null,
              hasSec ? `${fint(secAgent)} credentials on a command line` : null]
              .filter(Boolean).join(' · ') || 'no command or credential section on this cluster') +
            (callIsoAgent && callIsoAgent.known
              ? ` · ${pc(callIsoAgent.pct)} of tool calls sandboxed${failedAgent ? `, ${fint(failedAgent)} attempts failed` : ''}`
              : '')
          }
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Share of calls and of CPU over time"
          p={res}
          span
          empty={!rs?.t.length}
          emptyWhat="no process exited in the window"
        >
          {/* The two side by side on one clock: the point is that the two
              shares are not the same shape -- agents can make nearly every call
              in an hour and still be a minority of its CPU. */}
          <div className="pair-row">
            <div>
              <div className="eyebrow">share of commands run</div>
              {rs && (
                <TimeChart s={rs} lines={shareLines('n')} ylabel="% of commands" yname="share"
                  view={view} onView={onView} ymax={100} yfmt={(v) => `${Math.round(v)}%`} />
              )}
            </div>
            <div>
              <div className="eyebrow">share of CPU time</div>
              {rs && (
                <TimeChart s={rs} lines={shareLines('cpu_s')} ylabel="% of CPU" yname="share"
                  view={view} onView={onView} ymax={100} yfmt={(v) => `${Math.round(v)}%`} />
              )}
            </div>
          </div>
        </Panel>

        <Panel
          title="Who uses what: each class's share of the node, and of what it asks of Slurm"
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
          {/* A table, not a stacked bar per measure: each class keeps its own
              track, so the IDE's 0.3% of the commands is still there to read. */}
          <div className="scroller">
            <table className="roomy shares">
              <thead>
                <tr>
                  <th>share of</th>
                  {have.map((c) => (
                    <th key={c} className="cls" style={{ color: CLS_COL[c] }}>
                      {CLS_LBL[c] ?? c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {units.map((u, i) => {
                  const tot = have.reduce((a, c) => a + (u.get(c) ?? 0), 0);
                  const g = u.group ?? 'node';
                  const head = i === 0 || (units[i - 1].group ?? 'node') !== g;
                  return (
                    <Fragment key={u.k}>
                    {head && (
                      <tr className="grp">
                        <td colSpan={have.length + 1} data-tip={GROUPS[g][1]}>
                          {GROUPS[g][0]}
                          {g === 'slurm' && <span className="nm"> · requests, not use: GPU use on the compute nodes is not measured</span>}
                        </td>
                      </tr>
                    )}
                    <tr>
                      <td data-tip={u.tip}>{u.k}</td>
                      {have.map((c) => {
                        const v = u.get(c);
                        return (
                          <td key={c}>
                            <ShareCell
                              pct={v == null || !tot ? null : (100 * v) / tot}
                              sub={u.fmt ? u.fmt(v) : fint(v)}
                              c={CLS_COL[c]}
                            />
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
    </>
  );
}
