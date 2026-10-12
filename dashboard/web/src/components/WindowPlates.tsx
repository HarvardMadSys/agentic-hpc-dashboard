/* The window in four numbers an operator acts on: how much the agents ran, what
 * it cost, how many sessions that was, and what they did that they should not
 * have. Was Overview's top row; it sits on Live now, under "right now", labeled
 * with the window it counts (roadmap notes 21 and 22).
 *
 * The headline is the agent's; the other classes sit beside it for scale.
 */
import type { Cls, Resources, RiskPanel, Sandbox, ToolMix, Trajectories } from '../api/types';
import { Plate } from './Plate';
import { AGENT_TYPE_LBL, CLS_LBL } from '../lib/classes';
import { fint, fmt, pc } from '../lib/format';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { CALL_KNOWN, shareOfKnown } from '../lib/sandbox';
import { GLOSS } from '../lib/glossary';

export function WindowPlates({
  P,
  cls,
  traj,
}: {
  P: Panels;
  cls: Cls[];
  traj: Trajectories | null;
}) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const res = pick<Resources>(P, 'resources', 'ebpfm');
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');
  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  const tmBy = tm.by_class ?? {};
  const resBy = res.by_class ?? {};
  const have = cls.filter((c) => c in tmBy || c in resBy);

  const totN = have.reduce((a, c) => a + (tmBy[c]?.calls ?? 0), 0);
  const totCpu = have.reduce((a, c) => a + (tmBy[c]?.cpu_s ?? resBy[c]?.cpu_s ?? 0), 0);
  // What each class is made of -- which agents, which editors -- on hover.
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

  const callIso = shareOfKnown(sb.tool_calls?.agent, 'isolated', sb.states?.tool_call_known ?? CALL_KNOWN);
  const agentCpu = tmBy.agent?.cpu_s ?? resBy.agent?.cpu_s ?? null;
  const agentSum = cls.includes('agent') ? traj?.summary?.agent : undefined;
  const launch = sb.approval_by_class_sessions?.agent;
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

  return (
    <div className="plates">
      <Plate
        k="agent processes exited"
        tip={[GLOSS.exited, typeTip('calls_pct')].filter(Boolean).join('\n\n')}
        total={cls.includes('agent') ? tmBy.agent?.calls ?? null : null}
        stripe="var(--s1)"
        by={Object.fromEntries(have.map((c) => [c, tmBy[c]?.calls ?? null]))}
        note={totN ? `${pc((100 * (tmBy.agent?.calls ?? 0)) / totN)} of every process on the node` : undefined}
      />
      <Plate
        k="agent CPU"
        tip={[GLOSS.cpuh, typeTip('cpu_pct')].filter(Boolean).join('\n\n')}
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
        tip={GLOSS.session + '; scripts that only probe `codex --version` are not counted'}
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
        tip="commands this site's rules flag (rm -rf on a shared path, builds where they should not run, bulk downloads, …), plus credentials written into a command line"
        note={
          ([hasDg ? `${fint(dgAgent)} dangerous commands` : null,
            hasSec ? `${fint(secAgent)} credentials on a command line` : null]
            .filter(Boolean).join(' · ') || 'no command or credential section on this cluster') +
          (callIso && callIso.known
            ? ` · ${pc(callIso.pct)} of tool calls sandboxed${failedAgent ? `, ${fint(failedAgent)} attempts failed` : ''}`
            : '')
        }
      />
    </div>
  );
}
