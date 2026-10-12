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
import { GLOSS } from '../lib/glossary';
import type { Cls, Sandbox, SandboxSessionRow } from '../api/types';
import { Basis, Panel, More } from '../components/Panel';
import { Coverage } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { ShareCell } from '../components/ShareCell';
import { ByUser, distinct } from '../components/ByUser';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
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
  SANDBOX_ORDER,
  SB_COL,
  SB_LABEL,
  SESSION_LABEL,
  SESSION_ORDER,
  label,
  legendCol,
  shareOfKnown,
} from '../lib/sandbox';
import type { Share } from '../lib/sandbox';

type ByClass = Record<string, Record<string, number>> | undefined;

function axisBlock(
  by: ByClass,
  have: string[],
  order: string[],
  col: Record<string, string>,
  lbl: Record<string, string>,
  grain: string,
) {
  if (!by) return null;
  const tot = (c: string) => Object.values(by[c] ?? {}).reduce((a, b) => a + b, 0);
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
            <th />
            {cols.map((c) => (
              <th key={c} className="cls">
                <span style={{ color: CLS_COL[c] }}>{CLS_LBL[c] ?? c}</span>
                <div className="sub">
                  {fint(tot(c))} {grain}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...order, ...extra].map((s) => (
            <tr key={s}>
              <td>
                <i className="u-dot" style={{ background: legendCol(col[s] ?? 'var(--sg)') }} />
                {label(lbl, s)}
              </td>
              {cols.map((c) => {
                const v = by[c]?.[s] ?? 0;
                return (
                  <td key={c}>
                    <ShareCell pct={(100 * v) / tot(c)} sub={fint(v)} c={legendCol(col[s] ?? 'var(--sg)')} />
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

/** "coverage agent 99% · IDE 64%" -- what share of each class had a known verdict. */
/** "of 1,351 agent tool calls": the denominator, in words. When some could not
 * be judged, it says the share is of the ones that could, not of all of them. */
function agentOf(s: Share | undefined, what: string, knownWhat: string) {
  if (!s?.total) return undefined;
  return s.known < s.total ? `of ${fint(s.known)} ${what} ${knownWhat}` : `of ${fint(s.total)} ${what}`;
}

function sharesOf(by: ByClass, have: string[], state: string | string[], known: string[]) {
  return Object.fromEntries(have.map((c) => [c, shareOfKnown(by?.[c], state, known)]));
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
        { h: 'agents', cell: (rs) => distinct(rs, (r) => AGENT_TYPE_LBL[r.agent_type ?? ''] ?? r.agent_type) },
        { h: 'approval at launch', cell: (rs) => distinct(rs, (r) => label(AP_LABEL, r.approval), 2) },
        { h: 'tool calls', num: true, cell: (rs) => fint(rs.reduce((a, r) => a + callsOf(r), 0)) || DASH },
        {
          h: 'isolated',
          num: true,
          tip: 'share of their tool calls with a known verdict that ran namespace-isolated',
          cell: (rs) =>
            known(rs) ? pc((100 * rs.reduce((a, r) => a + (r.calls?.isolated ?? 0), 0)) / known(rs)) : DASH,
        },
        {
          h: 'failed attempts',
          num: true,
          cell: (rs) => fint(rs.reduce((a, r) => a + (r.calls?.failed ?? 0), 0)) || DASH,
        },
      ]}
      sub={(rs) => <SessionRows rows={rs} multiHost={multiHost} />}
    />
  );
}

/** One user's sessions, as rows. */
function SessionRows({ rows, multiHost }: { rows: SandboxSessionRow[]; multiHost: boolean }) {
  return (
    <div className="scroller">
      <table className="roomy">
        <thead>
          <tr>
            <th>agent</th>
            {multiHost && <th>host</th>}
            <th className="num">pid</th>
            <th data-tip={GLOSS.approval}>approval at launch</th>
            <th data-tip="the flags on the agent's own command line when it started">launch flags</th>
            <th className="num" data-tip={GLOSS.toolcall}>tool calls</th>
            <th className="num" data-tip="share of the session's tool calls with a known verdict that ran namespace-isolated">isolated</th>
            <th className="num" data-tip="sandbox wrapper runs that failed before running the command they were given">failed attempts</th>
            <th data-tip={GLOSS.verdict}>verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const n = callsOf(r);
            const flags = r.approval_detail && r.approval_detail !== 'no launch flag' ? r.approval_detail : null;
            return (
              <tr key={`${r.host}:${r.pid}`}>
                <td>{AGENT_TYPE_LBL[r.agent_type ?? ''] ?? r.agent_type ?? DASH}</td>
                {multiHost && <td className="mono nm">{r.host}</td>}
                <td className="num nm">{r.pid ?? DASH}</td>
                <td>{label(AP_LABEL, r.approval)}</td>
                <td className="mono nm wrap">{flags ?? DASH}</td>
                <td className="num">{n ? fint(n) : DASH}</td>
                <td
                  className="num"
                  data-tip={r.calls_known ? `of ${fint(r.calls_known)} tool call${r.calls_known === 1 ? '' : 's'} with a known verdict` : undefined}
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

export function SandboxTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');
  const st = sb.states ?? { sandbox: SANDBOX_ORDER, approval: APPROVAL_ORDER };
  const callKnown = st.tool_call_known ?? CALL_KNOWN;
  const apKnown = st.approval_known ?? APPROVAL_KNOWN;
  const evHave = shown(sb.by_class_events, cls);
  const seHave = shown(sb.by_class_sessions, cls);
  const tcHave = shown(sb.tool_calls, cls);
  const apSeHave = shown(sb.approval_by_class_sessions, cls);
  const apEvHave = shown(sb.approval_by_class_events, cls);
  const anc = sb.ancestry_matrix ?? [];
  const sessions = sb.sessions ?? [];
  const busy = sessions.filter((r) => callsOf(r) > 0);
  const idle = sessions.filter((r) => callsOf(r) === 0);
  const multiHost = new Set(sessions.map((r) => r.host)).size > 1;
  const attempts = sb.attempts ?? [];
  const modes = sb.launch_modes ?? [];

  const callIso = sharesOf(sb.tool_calls, tcHave, 'isolated', callKnown);
  const sessBypass = sharesOf(sb.approval_by_class_sessions, apSeHave, 'bypassed', apKnown);
  const failedAgent = tcHave.includes('agent') ? sb.tool_calls?.agent?.failed ?? 0 : null;
  const wrapperCalls = attempts.reduce((a, r) => a + (r.outcome === 'no_payload' ? 0 : r.n), 0);

  return (
    <>
      <div className="plates three">
        <Plate
          k="agent tool calls isolated"
          total={callIso.agent?.pct ?? null}
          stripe="var(--s5)"
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip="a tool call is a sandbox-wrapper invocation or a Claude Code Bash shell; a failed attempt counts as not isolated, an unreadable payload is left out"
          note={agentOf(callIso.agent, 'agent tool calls', 'whose outcome is known')}
        />
        <Plate
          k="agent sandbox attempts failed"
          total={failedAgent}
          stripe="var(--s4)"
          tip="a bwrap / codex-linux-sandbox call that exited non-zero with no payload under it: the sandbox was tried and did not come up"
          note={
            wrapperCalls
              ? `of ${fint(wrapperCalls)} sandbox wrapper calls`
              : 'no sandbox wrapper ran in the window'
          }
        />
        <Plate
          k="agent sessions launched with a bypass flag"
          total={sessBypass.agent?.pct ?? null}
          stripe="var(--crit)"
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          tip="read off each session root's own argv. Settings files, codex config.toml and a mid-session switch are invisible; `codex app-server` is not observable and is left out of the share."
          note={agentOf(sessBypass.agent, 'agent sessions', 'whose launch flags are readable')}
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Tool calls — did they run namespace-isolated?"
          p={sb}
          span
          empty={!tcHave.length && !sessions.length}
          emptyWhat="no tool calls or agent sessions in the window"
          basis={
            <Basis
              items={[
                ['sessions', sb.notes?.sessions],
                ['tool calls', sb.notes?.tool_calls],
                ['failed', sb.notes?.failed],
                ['below_capped', sb.below_capped || null],
              ]}
            />
          }
        >
          <div className="eyebrow">tool calls, by outcome</div>
          {axisBlock(sb.tool_calls, tcHave, CALL_ORDER, SB_COL, CALL_LABEL, 'tool calls')}
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
          title="Namespace isolation — per event and per session"
          p={sb}
          empty={!evHave.length && !seHave.length}
          emptyWhat="no isolation breakdown for the selection"
          basis={
            <Basis
              items={[
                ['meaning', sb.notes?.meaning],
                ['exited before read', sb.notes?.denied],
                ['inherit', sb.notes?.inherit],
                ['sandbox_src', sb.sandbox_src],
              ]}
            />
          }
        >
          <div className="eyebrow">per process</div>
          {axisBlock(sb.by_class_events, evHave, SANDBOX_ORDER, SB_COL, SB_LABEL, 'processes')}
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per session
          </div>
          {axisBlock(sb.by_class_sessions, seHave, SESSION_ORDER, SB_COL, SESSION_LABEL, 'sessions')}
        </Panel>

        <Panel
          title="Approval at launch — an independent axis"
          p={sb}
          empty={!apSeHave.length && !apEvHave.length}
          emptyWhat="no approval breakdown for the selection"
          basis={
            <Basis
              items={[
                ['approval', sb.notes?.approval],
                ['per record', sb.notes?.events_approval],
              ]}
            />
          }
        >
          <div className="eyebrow">per session</div>
          {axisBlock(sb.approval_by_class_sessions, apSeHave, APPROVAL_ORDER, AP_COL, AP_LABEL, 'sessions')}
          <div className="eyebrow" style={{ marginTop: 8 }}>
            per process
          </div>
          {axisBlock(sb.approval_by_class_events, apEvHave, APPROVAL_ORDER, AP_COL, AP_LABEL, 'processes')}
          {modes.length > 0 && (
            <div className="scroller" style={{ marginTop: 8 }}>
              <table>
                <thead>
                  <tr>
                    <th>agent</th>
                    <th data-tip="the flags exactly as they appeared on the agent's command line">launch flags, as written</th>
                    <th data-tip={GLOSS.approval}>reads as</th>
                    <th className="num">sessions</th>
                  </tr>
                </thead>
                <tbody>
                  {modes.map((m, i) => (
                    <tr key={`${m.agent_type}|${m.detail}|${m.approval}|${i}`}>
                      <td className="mono">{m.agent_type ?? DASH}</td>
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
          basis={<Basis items={[['failed', sb.notes?.failed]]} />}
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th data-tip={GLOSS.wrapper}>wrapper</th>
                  <th>agent</th>
                  <th data-tip="what happened to the command the wrapper was given (its payload)">outcome</th>
                  <th className="num">calls</th>
                </tr>
              </thead>
              <tbody>
                {attempts.map((a, i) => (
                  <tr key={`${a.wrapper}|${a.agent_type}|${a.outcome}|${i}`}>
                    <td className="mono">{a.wrapper}</td>
                    <td className="mono">{a.agent_type ?? DASH}</td>
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

        <Panel
          title="Ancestry × kind × verdict"
          p={sb}
          span
          empty={!anc.length}
          emptyWhat="the feed carried no ancestry rows"
          basis={
            <Basis
              items={[
                [
                  'reading',
                  'a wrapper is read at its own exec, before it unshares, so its own row is always "not isolated" and says nothing about the call it wraps; what ran under it decides that',
                ],
                ['sandbox_detail', sb.sandbox_detail],
                ['agent_only', sb.agent_only],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th data-tip={GLOSS.ancestry}>ancestry</th>
                  <th data-tip="process: an ordinary process; wrapper: the sandbox helper itself">category</th>
                  <th data-tip={GLOSS.verdict}>verdict</th>
                  <th className="num" data-tip="processes">n</th>
                  <th className="num" data-tip={GLOSS.dstate}>D-state wait</th>
                  <th className="num" data-tip={GLOSS.coverage}>coverage</th>
                  <th>reading</th>
                </tr>
              </thead>
              <tbody>
                {anc.map((a, i) => (
                  <tr key={a.ancestry + a.kind + a.sandbox + i}>
                    <td className="mono">{a.ancestry}</td>
                    <td className="mono">{a.kind ?? DASH}</td>
                    <td>
                      <span
                        className="sev"
                        style={{
                          borderColor: legendCol(SB_COL[a.sandbox]),
                          color:
                            a.sandbox === 'unsandboxed'
                              ? '#8a6410'
                              : a.sandbox === 'sandboxed'
                                ? '#166b50'
                                : 'var(--muted)',
                        }}
                      >
                        {label(SB_LABEL, a.sandbox)}
                      </span>
                    </td>
                    <td className="num">{fint(a.n)}</td>
                    <td className="num">
                      {a.dstate_wait_s == null ? (
                        <span className="nm">not measured</span>
                      ) : (
                        fmt(a.dstate_wait_s) + ' s'
                      )}
                    </td>
                    <td className="num">
                      <Coverage pct={a.dstate_coverage_pct} n={a.n} />
                    </td>
                    <td>{a.reading ?? DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Where the per-event verdict came from"
          p={sb}
          empty={!Object.keys(sb.sandbox_src ?? {}).length}
          emptyWhat="no source breakdown"
          basis={<Basis items={[['inherit', sb.notes?.inherit]]} />}
        >
          {(() => {
            const src = Object.entries(sb.sandbox_src ?? {}).sort((a, b) => b[1] - a[1]);
            const tot = src.reduce((a, [, v]) => a + v, 0);
            return (
              <table className="roomy shares">
                <tbody>
                  {src.map(([k, v]) => (
                    <tr key={k}>
                      <td className="mono lbl" data-tip={GLOSS[k === 'denied' ? 'exitedbeforeread' : k === 'inherit' ? 'inherit' : k.replace(':', '_')]}>{k === 'denied' ? 'exited before read' : k}</td>
                      <td>
                        <ShareCell pct={tot ? (100 * v) / tot : null} sub={fint(v)} c="var(--s2)" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          })()}
        </Panel>
      </div>
    </>
  );
}
