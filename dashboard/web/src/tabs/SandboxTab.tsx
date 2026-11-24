/* Sandbox -- two INDEPENDENT axes, each at the grain it lives at.
 *
 * `sandbox` and `approval` are different questions: an agent can be confined and
 * still be running with approvals bypassed. They are therefore never crossed into
 * a single "autonomy" score here; each gets its own chart.
 *
 * What is measured, said in the labels rather than left to a footnote:
 *   - "namespace-isolated" (key `sandboxed`): /proc/<pid>/ns/* differs from pid
 *     1's. Landlock and seccomp-only sandboxes are invisible to it; a container
 *     or a systemd unit counts.
 *   - a SESSION is judged by its tool calls. Claude Code and Codex wrap each call
 *     and never themselves, so the root's own verdict says nothing about it.
 *   - "sandbox attempt failed": the wrapper exited non-zero and nothing ran under
 *     it. Tried-and-failed must not look like never-tried.
 *   - approval is "no bypass flag at launch", not "supervised": argv is all the
 *     collector sees.
 *
 * Every share is a state over the KNOWN verdicts, with coverage beside it
 * (lib/sandbox.ts). `exited before read` and `unknown` are never folded into a
 * "no", and never silently into a denominator either.
 */
import { memo } from 'react';
import type { Cls, Sandbox, SandboxSessionRow } from '../api/types';
import { Panel, More } from '../components/Panel';
import { Coverage } from '../components/Coverage';
import { ShareCell } from '../components/ShareCell';
import { ByUser, distinct } from '../components/ByUser';
import { SortTh } from '../components/SortTh';
import { useSort, useSortMap } from '../lib/useSort';
import { AGENT_TYPE_LBL, CLS_INK, CLS_LBL } from '../lib/classes';
import { useNodeWords } from '../lib/nodes';
import { DASH, fint, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import {
  APPROVAL_KNOWN,
  APPROVAL_ORDER,
  AP_COL,
  AP_LABEL,
  CALL_KNOWN,
  CALL_LABEL,
  CALL_ORDER,
  SANDBOX_KNOWN,
  SANDBOX_ORDER,
  SB_COL,
  SB_LABEL,
  SESSION_KNOWN,
  SESSION_LABEL,
  SESSION_ORDER,
  label,
  legendCol,
  shareOfKnown,
} from '../lib/sandbox';

type ByClass = Record<string, Record<string, number>> | undefined;

/** One axis's states by class. A state the axis can judge (`known`) is a share of the class's
 *  judged ones; the others -- unknown, no tool calls, exited before it was checked, not an agent --
 *  are counts, and the coverage under each class says how many were judged at all (lib/sandbox.ts).
 *  Dividing by every state made the bypass share 10.0% (2 of 20) against the 11.8% (2 of 17) the
 *  security checks give. */
function AxisBlock({ by, have, order, col, lbl, grain, known }: {
  by: ByClass;
  have: string[];
  order: string[];
  col: Record<string, string>;
  lbl: Record<string, string>;
  grain: string;
  known: string[];
}) {
  const sort = useSort<string>();
  if (!by) return null;
  const tot = (c: string) => Object.values(by[c] ?? {}).reduce((a, b) => a + b, 0);
  const share = (c: string, st: string) => shareOfKnown(by[c], st, known);
  /** the class's judged ones, and their coverage */
  const judged = (c: string) => shareOfKnown(by[c], known, known);
  const cols = have.filter((c) => tot(c) > 0);
  if (!cols.length) return null;
  // every state in the canonical order, zero included -- "isolated: 0%" is
  // the finding, not an empty row -- then anything the order does not name
  const extra = Array.from(new Set(cols.flatMap((c) => Object.keys(by[c] ?? {})))).filter(
    (k) => !order.includes(k),
  );
  return (
    <div className="scroller">
      <table className="roomy shares" style={{ '--track': '64px' } as React.CSSProperties}>
        <thead>
          <tr>
            <SortTh s={sort} k="state" first="asc">state</SortTh>
            {cols.map((c) => (
              <SortTh key={c} s={sort} k={c} className="cls"
                tip={`the shares are of the ${fint(judged(c).known)} of ${fint(tot(c))} ${grain} whose state is known; the rest are counted below them`}
                sub={<>{fint(tot(c))} {grain} <Coverage pct={judged(c).coverage} /></>}>
                <span style={{ color: CLS_INK[c] }}>{CLS_LBL[c] ?? c}</span>
              </SortTh>
            ))}
          </tr>
        </thead>
        <tbody>
          {sort.sort([...order, ...extra], (st, k) =>
            k === 'state' ? label(lbl, st) : known.includes(st) ? share(k, st).pct : null,
          ).map((s) => (
            <tr key={s}>
              <td>
                <i className="u-dot" style={{ background: legendCol(col[s] ?? 'var(--sg)') }} />
                {label(lbl, s)}
              </td>
              {cols.map((c) => {
                const v = by[c]?.[s] ?? 0;
                return (
                  <td key={c}>
                    {known.includes(s) ? (
                      <ShareCell pct={share(c, s).pct} sub={fint(v)} c={legendCol(col[s] ?? 'var(--sg)')} />
                    ) : (
                      // not judged: a count, never a share of the judged ones
                      <span className="nm" data-tip={`${fint(v)} of ${fint(tot(c))} ${grain}: not in the shares above`}>
                        {fint(v)}
                      </span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const callsOf = (r: SandboxSessionRow) =>
  Object.values(r.calls ?? {}).reduce((a: number, b) => a + (b ?? 0), 0);

/* One fact per column, so each lines up down the page: the old single
 * "claude_code · user@host · pid" cell and the "0.0% of 930" cell were ragged,
 * and nothing in them could be read down a column. The launch flags, as
 * written, are their own muted column; "no launch flag" is already what the
 * approval column says, so it shows as a dash. */
function SessionTable({ rows, multiHost }: { rows: SandboxSessionRow[]; multiHost: boolean }) {
  const known = (rs: SandboxSessionRow[]) => rs.reduce((a, r) => a + (r.calls_known ?? 0), 0);
  return (
    <ByUser
      rows={rows}
      user={(r) => r.user}
      noun="session"
      cols={[
        {
          h: 'agents',
          cell: (rs) => distinct(rs, (r) => AGENT_TYPE_LBL[r.agent_type ?? ''] ?? r.agent_type),
          sort: (rs) => distinct(rs, (r) => AGENT_TYPE_LBL[r.agent_type ?? ''] ?? r.agent_type),
        },
        {
          h: 'approval at launch',
          cell: (rs) => distinct(rs, (r) => label(AP_LABEL, r.approval), 2),
          sort: (rs) => distinct(rs, (r) => label(AP_LABEL, r.approval), 2),
        },
        {
          h: 'tool calls',
          num: true,
          cell: (rs) => fint(rs.reduce((a, r) => a + callsOf(r), 0)) || DASH,
          sort: (rs) => rs.reduce((a, r) => a + callsOf(r), 0),
        },
        {
          h: 'sandboxed',
          num: true,
          tip: 'share of their tool calls whose sandbox state is known that ran sandboxed',
          cell: (rs) =>
            known(rs) ? pc((100 * rs.reduce((a, r) => a + (r.calls?.isolated ?? 0), 0)) / known(rs)) : DASH,
          sort: (rs) => (known(rs) ? rs.reduce((a, r) => a + (r.calls?.isolated ?? 0), 0) / known(rs) : null),
        },
        {
          h: 'failed to start',
          num: true,
          cell: (rs) => fint(rs.reduce((a, r) => a + (r.calls?.failed ?? 0), 0)) || DASH,
          sort: (rs) => rs.reduce((a, r) => a + (r.calls?.failed ?? 0), 0),
        },
      ]}
      sub={(rs, withUser) => <SessionRows rows={rs} multiHost={multiHost} withUser={withUser} />}
    />
  );
}

/** One user's sessions, as rows; every user's, with a user column. */
function SessionRows({ rows, multiHost, withUser }: { rows: SandboxSessionRow[]; multiHost: boolean; withUser?: boolean }) {
  const nodes = useNodeWords();
  const s = useSort<string>('agent', 'asc');
  const val = (r: SandboxSessionRow, k: string) =>
    k === 'agent' ? r.agent_type : k === 'user' ? r.user : k === 'host' ? r.host : k === 'pid' ? r.pid : k === 'approval' ? label(AP_LABEL, r.approval)
    : k === 'flags' ? r.approval_detail : k === 'calls' ? callsOf(r) : k === 'isolated' ? r.isolated_pct
    : k === 'failed' ? r.calls?.failed ?? null : label(SESSION_LABEL, r.state);
  return (
    // flat, it sits in the list's own scroll box, which holds its header in view
    <div className={withUser ? undefined : 'scroller'}>
      <table className="roomy">
        <thead>
          <tr>
            {withUser && <SortTh s={s} k="user" first="asc">user</SortTh>}
            <SortTh s={s} k="agent" first="asc">agent</SortTh>
            {multiHost && <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>}
            <SortTh s={s} k="pid" num>pid</SortTh>
            <SortTh s={s} k="approval" first="asc" gloss="approval">approval at launch</SortTh>
            <SortTh s={s} k="flags" first="asc" tip="the flags on the agent's own command line when it started">launch flags</SortTh>
            <SortTh s={s} k="calls" num gloss="toolcall">tool calls</SortTh>
            <SortTh s={s} k="isolated" num tip="share of the session's tool calls whose sandbox state is known that ran sandboxed">sandboxed</SortTh>
            <SortTh s={s} k="failed" num tip="sandbox wrapper runs that failed before running the command they were given">failed to start</SortTh>
            <SortTh s={s} k="verdict" first="asc" tip="what its tool calls add up to">session</SortTh>
          </tr>
        </thead>
        <tbody>
          {s.sort(rows, val).map((r) => {
            const n = callsOf(r);
            const flags = r.approval_detail && r.approval_detail !== 'no launch flag' ? r.approval_detail : null;
            return (
              <tr key={`${r.host}:${r.pid}:${r.gen ?? '-'}`}>
                {withUser && <td className="mono">{r.user ?? DASH}</td>}
                <td>{AGENT_TYPE_LBL[r.agent_type ?? ''] ?? r.agent_type ?? DASH}</td>
                {multiHost && <td className="mono nm">{r.host}</td>}
                <td className="num nm">{r.pid ?? DASH}</td>
                <td>{label(AP_LABEL, r.approval)}</td>
                <td className="mono nm wrap">{flags ?? DASH}</td>
                <td className="num">{n ? fint(n) : DASH}</td>
                <td
                  className="num"
                  data-tip={r.calls_known ? `of ${fint(r.calls_known)} tool call${r.calls_known === 1 ? '' : 's'} whose sandbox state is known` : undefined}
                >
                  {r.isolated_pct == null ? DASH : pc(r.isolated_pct)}
                </td>
                <td className="num">{r.calls?.failed ? fint(r.calls.failed) : DASH}</td>
                <td>{label(SESSION_LABEL, r.state)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export const SandboxTab = memo(function SandboxTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');
  const st = sb.states ?? { sandbox: SANDBOX_ORDER, approval: APPROVAL_ORDER };
  const callKnown = st.tool_call_known ?? CALL_KNOWN;
  const apKnown = st.approval_known ?? APPROVAL_KNOWN;
  const evHave = shown(sb.by_class_events, cls);
  const seHave = shown(sb.by_class_sessions, cls);
  const tcHave = shown(sb.tool_calls, cls);
  const apSeHave = shown(sb.approval_by_class_sessions, cls);
  const apEvHave = shown(sb.approval_by_class_events, cls);
  // every table here sorts by any column (roadmap note 27), each on its own
  const sortOf = useSortMap();
  const sessions = sb.sessions ?? [];
  const busy = sessions.filter((r) => callsOf(r) > 0);
  const idle = sessions.filter((r) => callsOf(r) === 0);
  const multiHost = new Set(sessions.map((r) => r.host)).size > 1;
  const attempts = sb.attempts ?? [];
  const modes = sb.launch_modes ?? [];

  // The shares sandboxed and bypassed, and the sandboxes that failed to start, are the
  // security checks above these panels (SecurityTab.tsx); the panels hold what is behind them.
  return (
    <>
      <div className="grid wider">
        <Panel
          id="sec-tool-calls-did-they-run-namespace-isolated"
          title="Tool calls — did they run sandboxed?"
          p={sb}
          span
          empty={!tcHave.length && !sessions.length}
          emptyWhat="no tool calls or agent sessions in the window"
        >
          <div className="eyebrow">tool calls, by outcome</div>
          <AxisBlock by={sb.tool_calls} have={tcHave} order={CALL_ORDER} col={SB_COL} lbl={CALL_LABEL} grain="tool calls" known={callKnown} />
          <div className="eyebrow" style={{ marginTop: 10 }}>
            sessions that made tool calls{sb.sessions_total > sessions.length ? ` · busiest ${sessions.length} of ${fint(sb.sessions_total)}` : ''}
          </div>
          <SessionTable rows={busy} multiHost={multiHost} />
          {idle.length > 0 && (
            <More label={`${fint(idle.length)} session${idle.length === 1 ? '' : 's'} with no tool calls in the window`}>
              <SessionTable rows={idle} multiHost={multiHost} />
            </More>
          )}
        </Panel>

        <Panel
          id="sec-namespace-isolation-per-process-and-per-session"
          title="Sandboxed — per process and per session"
          p={sb}
          empty={!evHave.length && !seHave.length}
          emptyWhat="no sandbox breakdown for the selection"
        >
          <div className="eyebrow">per process</div>
          <AxisBlock by={sb.by_class_events} have={evHave} order={SANDBOX_ORDER} col={SB_COL} lbl={SB_LABEL} grain="processes" known={st.sandbox_known ?? SANDBOX_KNOWN} />
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per session
          </div>
          <AxisBlock by={sb.by_class_sessions} have={seHave} order={SESSION_ORDER} col={SB_COL} lbl={SESSION_LABEL} grain="sessions" known={st.session_known ?? SESSION_KNOWN} />
        </Panel>

        <Panel
          title="Approval at launch — an independent axis"
          p={sb}
          empty={!apSeHave.length && !apEvHave.length}
          emptyWhat="no approval breakdown for the selection"
        >
          <div className="eyebrow">per session</div>
          <AxisBlock by={sb.approval_by_class_sessions} have={apSeHave} order={APPROVAL_ORDER} col={AP_COL} lbl={AP_LABEL} grain="sessions" known={apKnown} />
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per process
          </div>
          <AxisBlock by={sb.approval_by_class_events} have={apEvHave} order={APPROVAL_ORDER} col={AP_COL} lbl={AP_LABEL} grain="processes" known={apKnown} />
          {modes.length > 0 && (
            <div className="scroller" style={{ marginTop: 8 }}>
              <table>
                <thead>
                  <tr>
                    <SortTh s={sortOf('modes', 'agent', 'asc')} k="agent" first="asc">agent</SortTh>
                    <SortTh s={sortOf('modes', 'agent', 'asc')} k="detail" first="asc" tip="the flags exactly as they appeared on the agent's command line">launch flags, as written</SortTh>
                    <SortTh s={sortOf('modes', 'agent', 'asc')} k="approval" first="asc" gloss="approval">reads as</SortTh>
                    <SortTh s={sortOf('modes', 'agent', 'asc')} k="n" num>sessions</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {sortOf('modes', 'agent', 'asc').sort(modes, (m, k) =>
                    k === 'agent' ? m.agent_type : k === 'detail' ? m.detail : k === 'approval' ? label(AP_LABEL, m.approval) : m.n,
                  ).map((m, i) => (
                    <tr key={`${m.agent_type}|${m.detail}|${m.approval}|${i}`}>
                      <td>{m.agent_type ? (AGENT_TYPE_LBL[m.agent_type] ?? m.agent_type) : DASH}</td>
                      <td className="mono">{m.detail ?? DASH}</td>
                      <td>{label(AP_LABEL, m.approval)}</td>
                      <td className="num">{fint(m.n)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="Sandbox wrapper calls, by outcome"
          p={sb}
          empty={!attempts.length}
          emptyWhat="no bwrap or codex-linux-sandbox ran in the window"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <SortTh s={sortOf('attempts', 'wrapper', 'asc')} k="wrapper" first="asc" gloss="wrapper">wrapper</SortTh>
                  <SortTh s={sortOf('attempts', 'wrapper', 'asc')} k="agent" first="asc">agent</SortTh>
                  <SortTh s={sortOf('attempts', 'wrapper', 'asc')} k="outcome" first="asc" tip="what happened to the command the wrapper was given">outcome</SortTh>
                  <SortTh s={sortOf('attempts', 'wrapper', 'asc')} k="n" num>calls</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortOf('attempts', 'wrapper', 'asc').sort(attempts, (a, k) =>
                  k === 'wrapper' ? a.wrapper : k === 'agent' ? a.agent_type : k === 'outcome' ? label(CALL_LABEL, a.outcome) : a.n,
                ).map((a, i) => (
                  <tr key={`${a.wrapper}|${a.agent_type}|${a.outcome}|${i}`}>
                    <td className="mono">{a.wrapper}</td>
                    <td>{a.agent_type ? (AGENT_TYPE_LBL[a.agent_type] ?? a.agent_type) : DASH}</td>
                    <td>
                      <span
                        className="sev"
                        style={{ borderColor: legendCol(SB_COL[a.outcome]), color: 'var(--ink, inherit)' }}
                      >
                        {label(CALL_LABEL, a.outcome)}
                      </span>
                    </td>
                    <td className="num">{fint(a.n)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
});
