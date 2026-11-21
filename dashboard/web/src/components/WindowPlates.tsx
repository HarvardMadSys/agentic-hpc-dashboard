/* The window in three numbers an operator acts on: how much the agents ran, how
 * many sessions that was, and what they did that they should not have. Was
 * Overview's top row; it is Live's headline row now, under a label naming the
 * window its panels were built over (roadmap notes 21 and 22).
 *
 * The headline is the agent's; the other classes sit beside it for scale.
 */
import { memo } from 'react';
import type { Cls, Resources, RiskPanel, Sandbox, ToolMix, Trajectories } from '../api/types';
import { Plate } from './Plate';
import { AGENT_TYPE_LBL, CLS_LBL } from '../lib/classes';
import { fint, pc } from '../lib/format';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { CALL_KNOWN, riskLine, shareOfKnown } from '../lib/sandbox';
import { noShare, shareOf, wholeOf } from '../lib/shares';
import { GLOSS } from '../lib/glossary';
import { useNodeWords } from '../lib/nodes';

export const WindowPlates = memo(function WindowPlates({
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

  const nodes = useNodeWords();
  // The agent's share of every process: out of every class the panel counted,
  // `unlabeled` too, whatever the chips show (lib/shares.ts). A class the panel
  // did not count ran none.
  const allN = wholeOf(Object.keys(tmBy), (c) => tmBy[c]?.calls);
  const agentPct = cls.includes('agent') ? shareOf(tmBy.agent?.calls ?? 0, allN) : null;
  // What each class is made of -- which agents, which IDEs -- on hover.
  const typeTip =
    have
      .map((c) => {
        const parts = Object.entries(tm.by_type?.[c] ?? {})
          .filter(([at, t]) => at && (t.calls_pct ?? 0) >= 0.05)
          .sort((a, b) => (b[1].calls_pct ?? 0) - (a[1].calls_pct ?? 0))
          .map(([at, t]) => `${AGENT_TYPE_LBL[at] ?? at} ${pc(t.calls_pct)}`);
        return parts.length ? `${CLS_LBL[c] ?? c}: ${parts.join(' · ')}` : null;
      })
      .filter(Boolean)
      .join('\n') || undefined;

  const callIso = shareOfKnown(sb.tool_calls?.agent, 'isolated', sb.states?.tool_call_known ?? CALL_KNOWN);
  const agentSum = cls.includes('agent') ? traj?.summary?.agent : undefined;
  const launch = sb.approval_by_class_sessions?.agent;
  // a session whose tool calls ran and none was isolated (the Security tab's sandbox
  // session verdict); one with no tool call seen is neither
  const sessSb = sb.by_class_sessions?.agent;
  const classSum = (rows: { classes?: Partial<Record<string, number>> }[] | undefined, c: string) =>
    (rows ?? []).reduce((a, r) => a + (r.classes?.[c] ?? 0), 0);
  const dgAgent = classSum(risk.dangerous?.rows, 'agent');
  const secAgent = classSum(risk.secrets?.rows, 'agent');
  // A risk module this cluster's profile leaves out is not counted (absent),
  // which must not read as zero: the plate names only what was counted.
  const hasDg = risk.dangerous != null;
  const hasSec = risk.secrets != null;
  const riskyAgent = hasDg || hasSec ? dgAgent + secAgent : null;
  // per class, only when a kind was counted: a cluster whose profile shows
  // neither has no zeros to give (reducers/risk.py leaves the kind out)
  const riskyBy = hasDg || hasSec
    ? Object.fromEntries(have.map((c) => [c, classSum(risk.dangerous?.rows, c) + classSum(risk.secrets?.rows, c)]))
    : undefined;
  const failedAgent = sb.tool_calls?.agent?.failed ?? 0;

  return (
    <div className="plates three">
      <Plate
        k="agent processes exited"
        tip={[GLOSS.exited, typeTip].filter(Boolean).join('\n\n')}
        total={cls.includes('agent') ? tmBy.agent?.calls ?? null : null}
        by={Object.fromEntries(have.map((c) => [c, tmBy[c]?.calls ?? null]))}
        note={agentPct != null
          ? `${pc(agentPct)} of every process on the ${nodes.many}`
          : cls.includes('agent') ? noShare(allN) ?? undefined : undefined}
      />
      <Plate
        k="agent sessions"
        total={agentSum?.sessions ?? null}
        by={Object.fromEntries(cls.filter((c) => traj?.summary?.[c]).map((c) => [c, traj!.summary![c].sessions]))}
        tip={[
          GLOSS.session,
          'not sandboxed: none of its tool calls ran in a sandbox; a session with no tool call is neither',
        ].join('\n\n')}
        note={riskLine(launch, sessSb ? sessSb.unsandboxed ?? 0 : null)}
      />
      <Plate
        k="risky agent actions"
        total={cls.includes('agent') && risk._present ? riskyAgent : null}
        alarm={(riskyAgent ?? 0) > 0}
        by={riskyBy}
        tip="commands this site's rules flag, and credentials typed into a command line"
        note={
          ([hasDg ? `${fint(dgAgent)} commands that should not run here` : null,
            hasSec ? `${fint(secAgent)} credentials on a command line` : null]
            .filter(Boolean).join(' · ') || 'no command or credential section on this cluster') +
          (callIso && callIso.known
            ? ` · ${pc(callIso.pct)} of tool calls sandboxed${failedAgent ? `, ${fint(failedAgent)} sandboxes failed to start` : ''}`
            : '')
        }
      />
    </div>
  );
});
