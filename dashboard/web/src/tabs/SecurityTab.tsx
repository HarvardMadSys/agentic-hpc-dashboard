/* Security -- how the agents here were launched, how their tool calls were
 * confined, what they exposed, and how far past their start folder they went.
 *
 * It holds no threshold of its own. Each check is a state the service already
 * assigns, or a rule it already applies -- a bypass flag or auto mode on the
 * agent's own command line, a tool call that ran outside a sandbox, a sandbox
 * that failed to start, a credential rule's hits, a path written outside the
 * agent's start folder -- so this tab and the panels behind it cannot
 * disagree. `sandbox` and `approval` stay independent axes: each check is one
 * question, and none is crossed with another into a score.
 *
 * Unlike Needs attention, every check is listed with what it found, because
 * "none" is a finding here. Three kinds of nothing stay three: a check whose
 * panel was reduced and holds nothing says none; one whose panel is waited
 * for, still being read, not reduced or not returned says "not checked" and
 * why, never none and never 0; and with the agent class filtered out, the
 * agent checks say they were not applied.
 *
 * The sandbox panels were Internal's while their takeaway was open; they are
 * this tab's confinement half now, under their old titles and anchors, so a
 * citation of one still lands on it (lib/cites.ts). Credentials on command
 * lines stay a section of Risks, which the site's risk profile lays out
 * (`risk.sections`): checked here, listed there.
 */
import { Fragment, memo } from 'react';
import type { Cascades, Cls, PanelEnvelope, RiskPanel, RiskSection, Sandbox, Workspace, WsAgent, WsFlag } from '../api/types';
import { ByUser, distinct } from '../components/ByUser';
import { NotReturned, Panel, slug } from '../components/Panel';
import { SortTh } from '../components/SortTh';
import { go } from '../lib/cites';
import type { Arrival } from '../lib/cites';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, pc } from '../lib/format';
import { GLOSS } from '../lib/glossary';
import { useNodeWords } from '../lib/nodes';
import { hasBody, pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { PLACE_SEVERITY, placeLabel, placeRank } from '../lib/places';
import { APPROVAL_KNOWN, CALL_KNOWN, shareOfKnown } from '../lib/sandbox';
import { useSort } from '../lib/useSort';
import { SandboxTab } from './SandboxTab';

/* ------------------------------------------------------------- the checks */

export type CheckState = 'flag' | 'clear' | 'unchecked';
type Axis = 'launch' | 'confinement' | 'exposure' | 'reach';

export interface Check {
  key: string;
  axis: Axis;
  /** flag: something found; clear: the check was made and found none; unchecked: not made */
  state: CheckState;
  what: string;
  note?: string;
  /** the per-class split, where the payload has one */
  by?: Partial<Record<string, number>>;
  to?: Arrival;
  where?: string;
}

const AXES: { k: Axis; lbl: string }[] = [
  { k: 'launch', lbl: 'launch · what the agent may do without asking' },
  { k: 'confinement', lbl: 'confinement · whether its tool calls ran sandboxed' },
  { k: 'exposure', lbl: 'exposure · credentials where any user can read them' },
  { k: 'reach', lbl: 'reach · where it went outside its start folder' },
];

/** The panels this tab's checks link to, by their titles: a title is its anchor (Panel.tsx). */
const APPROVAL_TITLE = 'Approval at launch — an independent axis';
const CALLS_ANCHOR = 'sec-tool-calls-did-they-run-namespace-isolated';
const WRAPPERS_TITLE = 'Sandbox wrapper calls, by outcome';
export const OUTSIDE_TITLE = 'Writes outside the start folder';

const at = (tab: string, anchor?: string): Arrival => ({ patch: { tab }, anchor, find: [] });
const plural = (n: number, one: string, many = one + 's') => `${fint(n)} ${n === 1 ? one : many}`;
const OFF = 'the agent class is filtered out';

/** Why a check cannot be made from `p`, in words; null when it can. A panel
 *  with no body has measured nothing, so this is never "none" (lib/panels.ts). */
function unread(p: PanelEnvelope, what: string): string | null {
  if (hasBody(p)) return null;
  if (p._waiting) return 'waiting for the dashboard service';
  if (p._error != null) return `the service could not reduce ${what}: ${p._error}`;
  if (p._present === false) return p._unsent ? `${what} was not returned` : `no data for ${what}: its feed is absent`;
  const pct = p._window?.progress?.pct;
  return `the window is still being read${pct != null ? ` (${pct}%)` : ''}`;
}

/** The agents that wrote or deleted outside their start folder, each judged on
 *  its own processes, as the service counts them (`with_outside_writes`). The
 *  service serves every such agent however quiet it was
 *  (reducers/cascades.py `_workspace`), so this list is whole. */
export function writersOf(ws: Workspace | null | undefined): WsAgent[] {
  return (ws?.agents ?? []).filter((a) => (a.self?.summary.outside_writes ?? 0) > 0);
}

export interface Place {
  kind: string;
  /** `host:pid` of each agent that wrote or deleted there */
  agents: string[];
  users: string[];
  written: number;
  deleted: number;
  roots: string[];
}

/** Where those agents wrote, by the kind of place, the most alarming first. */
export function placesOf(writers: WsAgent[]): Place[] {
  const m = new Map<string, Place>();
  for (const a of writers) {
    for (const g of a.self?.summary.roots ?? []) {
      if (g.write + g.delete <= 0) continue;
      let p = m.get(g.kind);
      if (!p) m.set(g.kind, (p = { kind: g.kind, agents: [], users: [], written: 0, deleted: 0, roots: [] }));
      const key = `${a.host}:${a.pid}`;
      if (!p.agents.includes(key)) p.agents.push(key);
      if (a.user && !p.users.includes(a.user)) p.users.push(a.user);
      p.written += g.write;
      p.deleted += g.delete;
      if (!p.roots.includes(g.root)) p.roots.push(g.root);
    }
  }
  return [...m.values()].sort((x, y) => placeRank(x.kind) - placeRank(y.kind) || (x.kind < y.kind ? -1 : 1));
}

/** Every check, in the order of the axes, with what it found. */
export function securityChecks(P: Panels, cls: Cls[]): Check[] {
  const out: Check[] = [];
  const agentOn = cls.includes('agent');

  // ---- launch and confinement: the sandbox panel's states
  const sb = pick<Sandbox>(P, 'sandbox', 'ebpfm');
  const sbWhy = unread(sb, 'the sandbox panel') ?? (agentOn ? null : OFF);
  const approval = at('security', slug(APPROVAL_TITLE));
  const approvalWhere = 'Risk › Security › Approval at launch';
  if (sbWhy) {
    out.push({ key: 'launch', axis: 'launch', state: 'unchecked', what: 'Approval at launch: not checked', note: sbWhy });
    out.push({ key: 'sandbox', axis: 'confinement', state: 'unchecked', what: 'Sandboxed tool calls: not checked', note: sbWhy });
  } else {
    const apKnown = sb.states?.approval_known ?? APPROVAL_KNOWN;
    const launch = sb.approval_by_class_sessions?.agent;
    // the agent sessions whose launch flags are readable, of all of them
    const judged = shareOfKnown(launch, apKnown, apKnown);
    if (!judged.total) {
      out.push({ key: 'launch', axis: 'launch', state: 'clear', what: 'No agent session in the window',
        note: 'none to judge by its launch flags', to: approval, where: approvalWhere });
    } else if (!judged.known) {
      out.push({ key: 'launch', axis: 'launch', state: 'unchecked', what: 'Approval at launch: can’t tell',
        note: `the launch flags of none of the ${plural(judged.total, 'agent session')} are readable`, to: approval, where: approvalWhere });
    } else {
      const of = `of the ${plural(judged.known, 'agent session')} whose launch flags are readable`;
      for (const [state, how, gloss] of [
        ['bypassed', 'with a bypass flag', 'it acts without asking'],
        ['auto', 'in auto mode', 'a classifier approves each action'],
      ] as const) {
        const n = launch?.[state] ?? 0;
        out.push({
          key: `launch:${state}`, axis: 'launch',
          state: n > 0 ? 'flag' : 'clear',
          what: n > 0 ? `${plural(n, 'agent session')} launched ${how}` : `No agent session launched ${how}`,
          note: n > 0 ? `${pc(shareOfKnown(launch, state, apKnown).pct)} ${of} · ${gloss}` : of,
          to: approval, where: approvalWhere,
        });
      }
    }

    const callKnown = sb.states?.tool_call_known ?? CALL_KNOWN;
    const calls = sb.tool_calls?.agent;
    const iso = shareOfKnown(calls, 'isolated', callKnown);
    const callsTo = at('security', CALLS_ANCHOR);
    const callsWhere = 'Risk › Security › Tool calls — did they run sandboxed?';
    if (!iso.total) {
      out.push({ key: 'sandbox:not', axis: 'confinement', state: 'clear', what: 'No agent tool call in the window',
        note: 'none to judge', to: callsTo, where: callsWhere });
    } else if (!iso.known) {
      out.push({ key: 'sandbox:not', axis: 'confinement', state: 'unchecked', what: 'Sandboxed tool calls: can’t tell',
        note: `the outcome of none of the ${plural(iso.total, 'agent tool call')} is known`, to: callsTo, where: callsWhere });
    } else {
      // A sandbox that failed to start ran nothing: it is its own check, below.
      const n = calls?.not_isolated ?? 0;
      const none = sb.by_class_sessions?.agent?.unsandboxed;
      out.push({
        key: 'sandbox:not', axis: 'confinement',
        state: n > 0 ? 'flag' : 'clear',
        what: n > 0 ? `${plural(n, 'agent tool call')} not sandboxed` : 'No agent tool call ran outside a sandbox',
        note: [
          `${pc(iso.pct)} of the ${plural(iso.known, 'agent tool call')} whose outcome is known ran sandboxed`,
          n > 0 && none != null ? `${plural(none, 'agent session')} had none sandboxed` : null,
        ].filter(Boolean).join(' · '),
        to: callsTo, where: callsWhere,
      });
    }
    const failed = calls?.failed ?? 0;
    const wrapperCalls = (sb.attempts ?? []).reduce((a, r) => a + (r.outcome === 'no_payload' ? 0 : r.n), 0);
    out.push({
      key: 'sandbox:failed', axis: 'confinement',
      state: failed > 0 ? 'flag' : 'clear',
      what: failed > 0 ? `${plural(failed, 'agent sandbox', 'agent sandboxes')} failed to start` : 'No agent sandbox failed to start',
      note: wrapperCalls
        ? `of ${plural(wrapperCalls, 'sandbox wrapper call')}`
        : 'no sandbox wrapper ran in the window',
      to: at('security', slug(WRAPPERS_TITLE)), where: `Security › ${WRAPPERS_TITLE}`,
    });
  }

  // ---- exposure: the site's credential rules, in its own sections (Risks)
  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  const riskWhy = unread(risk, 'the risk panel') ?? (agentOn ? null : OFF);
  if (riskWhy) {
    out.push({ key: 'cred', axis: 'exposure', state: 'unchecked', what: 'Credentials on a command line: not checked', note: riskWhy });
  } else {
    // a payload from before sections existed reads as the one it always had
    const all: RiskSection[] = risk.sections ?? (risk.secrets
      ? [{ id: 'credentials', kind: 'credentials', title: 'Credentials on a command line', rules: risk.secrets.rows.map((r) => r.id) }]
      : []);
    const sections = all.filter((sc) => sc.kind === 'credentials');
    if (!sections.length) {
      out.push({ key: 'cred', axis: 'exposure', state: 'unchecked', what: 'Credentials on a command line: not checked',
        note: 'this cluster’s risk profile has no credentials section',
        to: at('risk', slug('Rules in effect')), where: 'Risk › Site rules › Rules in effect' });
    }
    const scanned = risk.secrets ? `${plural(risk.secrets.args_scanned, 'command line')} scanned` : null;
    for (const sc of sections) {
      const want = new Set(sc.rules);
      const rows = (risk.secrets?.rows ?? []).filter((r) => want.has(r.id));
      // every class shown, 0 included: the panel was reduced, so a 0 was counted
      const by: Record<string, number> = Object.fromEntries(cls.map((c) => [c, 0]));
      for (const r of rows) for (const c of cls) by[c] += r.classes?.[c] ?? 0;
      const n = by.agent ?? 0;
      const top = rows
        .filter((r) => (r.classes?.agent ?? 0) > 0)
        .sort((a, b) => (b.classes?.agent ?? 0) - (a.classes?.agent ?? 0))
        .slice(0, 3)
        .map((r) => `${r.pattern} ×${fint(r.classes?.agent ?? 0)}`);
      // Each rule counts its own matches, so one line with a Bearer header and
      // a key in it is two: the sum is matches, as the Risks table calls them,
      // and never a count of command lines.
      out.push({
        key: `cred:${sc.id}`, axis: 'exposure',
        state: n > 0 ? 'flag' : 'clear',
        what: n > 0 ? `${sc.title}: ${plural(n, 'match', 'matches')} on agent command lines` : `${sc.title}: none on an agent’s`,
        note: [...top, n > 0 ? 'a line can match more than one pattern · the values are never shown' : null, scanned]
          .filter(Boolean).join(' · '),
        by, to: at('risk', slug(sc.title)), where: `Risk › Site rules › ${sc.title}`,
      });
    }
  }

  // ---- reach: each agent's workspace, read with its tree (Agents)
  const cas = pick<Cascades>(P, 'cascades', 'ebpfm');
  const ws = cas.workspace;
  const casWhy = unread(cas, 'the agent trees') ?? (agentOn ? null : OFF) ??
    (ws ? null : 'the agent trees came without a workspace reading');
  if (casWhy || !ws) {
    out.push({ key: 'reach', axis: 'reach', state: 'unchecked', what: `${OUTSIDE_TITLE}: not checked`, note: casWhy ?? undefined });
  } else {
    const writers = writersOf(ws);
    const n = ws.summary.with_outside_writes;
    const agents = ws.summary.agents;
    const places = placesOf(writers);
    const outsideTo = at('security', slug(OUTSIDE_TITLE));
    out.push({
      key: 'reach:outside', axis: 'reach',
      state: n > 0 ? 'flag' : 'clear',
      what: n > 0 ? `${plural(n, 'agent')} wrote outside ${n === 1 ? 'its' : 'their'} start folder` : 'No agent wrote outside its start folder',
      note: agents
        ? [
            `of ${plural(agents, 'agent')}`,
            places.length ? `into ${places.map((p) => `${placeLabel(p.kind)} ${fint(p.agents.length)}`).join(', ')}` : null,
            'paths read from the text of their commands',
          ].filter(Boolean).join(' · ')
        : 'no agent in the window',
      to: outsideTo, where: `Security › ${OUTSIDE_TITLE}`,
    });
    const reached = ws.summary.other_homes;
    if (reached == null) {
      out.push({ key: 'reach:home', axis: 'reach', state: 'unchecked', what: 'Another user’s home: not checked',
        note: 'the service did not count it in this payload' });
    } else {
      const wroteThere = writers.filter((a) =>
        (a.self?.summary.roots ?? []).some((g) => g.kind === "other user's home" && g.write + g.delete > 0)).length;
      out.push({
        key: 'reach:home', axis: 'reach',
        state: reached > 0 ? 'flag' : 'clear',
        what: reached > 0 ? `${plural(reached, 'agent')} reached another user’s home` : 'No agent reached another user’s home',
        note: reached > 0
          ? `ran in, or named a path in, a home folder not its user’s · ${fint(wroteThere)} wrote or deleted there`
          : agents ? `of ${plural(agents, 'agent')}` : 'no agent in the window',
        to: wroteThere > 0 ? outsideTo : at('agents', 'sec-agent-trees'),
        where: wroteThere > 0 ? `Risk › Security › ${OUTSIDE_TITLE}` : 'Agents › Agents and their subagents',
      });
    }
  }
  return out;
}

const STATE_WORD: Record<CheckState, string> = { flag: 'found: ', clear: 'none found: ', unchecked: 'not checked: ' };

function CheckLine({ c, cls }: { c: Check; cls: Cls[] }) {
  const by = c.by;
  return (
    <li>
      <i className={`attn-dot${c.state === 'flag' ? '' : ` ${c.state}`}`} aria-hidden="true" />
      <span className="attn-what">
        <span className="vh">{STATE_WORD[c.state]}</span>
        {c.what}
      </span>
      {c.to ? (
        <button className="linkbtn attn-go" onClick={() => go(c.to!, c.where ?? '')} data-tip={`open ${c.where}`}>
          see {'→'}
        </button>
      ) : (
        <span />
      )}
      {(c.note || by) && (
        <span className="attn-note">
          {by && (
            <span className="u-chips" style={{ marginRight: 8 }}>
              {cls.filter((k) => by[k] != null).map((k) => (
                <span key={k} className="u-chip">
                  <i className="u-dot" style={{ background: CLS_COL[k] }} />
                  {CLS_LBL[k] ?? k} <b>{fint(by[k])}</b>
                </span>
              ))}
            </span>
          )}
          {c.note}
        </span>
      )}
    </li>
  );
}

export function SecurityChecks({ P, cls }: { P: Panels; cls: Cls[] }) {
  const checks = securityChecks(P, cls);
  const flagged = checks.filter((c) => c.state === 'flag').length;
  const unchecked = checks.filter((c) => c.state === 'unchecked').length;
  return (
    <Panel
      title="Security checks"
      span
      header={
        <span
          className={`pill${flagged ? ' stale' : unchecked ? '' : ' ok'}`}
          data-tip={`${fint(flagged)} found something · ${fint(checks.length - flagged - unchecked)} found none · ${fint(unchecked)} not checked`}
        >
          {flagged ? `${fint(flagged)} flagged` : unchecked ? `${fint(unchecked)} not checked` : 'nothing flagged'}
        </span>
      }
    >
      <ul className="attn sec-checks">
        {AXES.map((ax) => {
          const mine = checks.filter((c) => c.axis === ax.k);
          if (!mine.length) return null;
          return (
            <Fragment key={ax.k}>
              <li className="sec-axis">{ax.lbl}</li>
              {mine.map((c) => (
                <CheckLine key={c.key} c={c} cls={cls} />
              ))}
            </Fragment>
          );
        })}
      </ul>
      <div className="attn-foot">no check is a score, and none is weighed against another</div>
    </Panel>
  );
}

/* --------------------------------------------- writes outside the start folder */

const outside = (a: WsAgent) => a.self?.summary.outside;
const sumOf = (rows: WsAgent[], f: (a: WsAgent) => number | null | undefined) =>
  rows.reduce((n, a) => n + (f(a) ?? 0), 0);
const typeLbl = (a: WsAgent) => (a.agent_type ? AGENT_TYPE_LBL[a.agent_type] ?? a.agent_type : null);
/** the kinds of place some agents wrote or deleted in, the most alarming first */
const kindsOf = (rows: WsAgent[]) => placesOf(rows).map((p) => p.kind);
/** An agent's paths the most alarming first, as the service sorts them (`workspace.py`): by the
 *  place, a delete before a write, then the path. Sorted here too, so the order never rests on
 *  the payload's. */
const byAlarm = (flags: WsFlag[]) =>
  [...flags].sort((x, y) =>
    placeRank(x.kind) - placeRank(y.kind) ||
    Number(x.op !== 'delete') - Number(y.op !== 'delete') ||
    (x.path < y.path ? -1 : x.path > y.path ? 1 : 0));
/** A count of paths: a 0 is counted, so it reads 0; a delete is marked, as on the Agents tab. */
const writes = (n: number | null | undefined) => (n == null ? DASH : fint(n));
const deletes = (n: number | null | undefined) => (n ? <b className="ws-d">{fint(n)}</b> : writes(n));

function KindMarks({ kinds }: { kinds: string[] }) {
  if (!kinds.length) return <span className="nm">{DASH}</span>;
  return (
    <span className="ws-roots">
      {kinds.map((k) => (
        <span key={k} className={`ws-root wrote${PLACE_SEVERITY[k as keyof typeof PLACE_SEVERITY] === 'high' ? ' high' : ''}`}>
          {placeLabel(k)}
        </span>
      ))}
    </span>
  );
}

function PlaceTable({ places }: { places: Place[] }) {
  // the places keep their own order, the most alarming first (lib/places.ts)
  const s = useSort<string>();
  return (
    <div className="scroller">
      <table className="roomy">
        <thead>
          <tr>
            <SortTh s={s} k="kind" first="asc" tip="the kind of place the paths are in, the most alarming first">where they landed</SortTh>
            <SortTh s={s} k="agents" num>agents</SortTh>
            <SortTh s={s} k="users" num>users</SortTh>
            <SortTh s={s} k="written" num tip="paths written there, as the agents’ commands name them">written</SortTh>
            <SortTh s={s} k="deleted" num tip="paths deleted there, as the agents’ commands name them">deleted</SortTh>
            <th>places</th>
          </tr>
        </thead>
        <tbody>
          {s.sort(places, (p, k) =>
            k === 'kind' ? placeLabel(p.kind) : k === 'agents' ? p.agents.length : k === 'users' ? p.users.length
            : k === 'written' ? p.written : p.deleted,
          ).map((p) => (
            <tr key={p.kind}>
              <td><KindMarks kinds={[p.kind]} /></td>
              <td className="num">{fint(p.agents.length)}</td>
              <td className="num" data-tip={p.users.join(', ') || undefined}>{fint(p.users.length)}</td>
              <td className="num">{writes(p.written)}</td>
              <td className="num">{deletes(p.deleted)}</td>
              <td className="mono nm wrap" data-tip={p.roots.length > 4 ? p.roots.join('\n') : undefined}>
                {p.roots.slice(0, 4).join(' · ')}
                {p.roots.length > 4 ? ` +${fint(p.roots.length - 4)}` : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One user's agents, as rows; every user's, with a user column. */
function WriterRows({ rows, multiHost, withUser }: { rows: WsAgent[]; multiHost: boolean; withUser?: boolean }) {
  const nodes = useNodeWords();
  const s = useSort<string>('agent', 'asc');
  const val = (a: WsAgent, k: string) =>
    k === 'agent' ? typeLbl(a) : k === 'user' ? a.user : k === 'host' ? a.host : k === 'pid' ? a.pid
    : k === 'home' ? a.home_shown : k === 'written' ? outside(a)?.write : k === 'deleted' ? outside(a)?.delete
    : placeRank(kindsOf([a])[0] ?? 'temp');
  return (
    <div className={withUser ? undefined : 'scroller'}>
      <table className="roomy">
        <thead>
          <tr>
            {withUser && <SortTh s={s} k="user" first="asc">user</SortTh>}
            <SortTh s={s} k="agent" first="asc">agent</SortTh>
            {multiHost && <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>}
            <SortTh s={s} k="pid" num>pid</SortTh>
            <SortTh s={s} k="home" first="asc" gloss="home">start folder</SortTh>
            <SortTh s={s} k="written" num tip="paths written outside its start folder, as its commands name them">written</SortTh>
            <SortTh s={s} k="deleted" num tip="paths deleted outside its start folder, as its commands name them">deleted</SortTh>
            <SortTh s={s} k="where" first="asc" tip="the kinds of place they are in, the most alarming first">where</SortTh>
            <th data-tip="the paths its commands wrote or deleted outside its start folder, the most alarming first">paths</th>
          </tr>
        </thead>
        <tbody>
          {s.sort(rows, val).map((a) => {
            const v = a.self!;
            const flags = byAlarm(v.flags);
            const more = v.flags_more;
            return (
              <tr key={`${a.host}:${a.pid}`}>
                {withUser && <td className="mono">{a.user ?? DASH}</td>}
                <td>{typeLbl(a) ?? DASH}</td>
                {multiHost && <td className="mono nm">{a.host}</td>}
                <td className="num nm">{a.pid}</td>
                <td className="mono nm" data-tip={a.home ?? undefined}>{a.home_shown ?? DASH}</td>
                <td className="num">{writes(outside(a)?.write)}</td>
                <td className="num">{deletes(outside(a)?.delete)}</td>
                <td><KindMarks kinds={kindsOf([a])} /></td>
                <td>
                  {flags.slice(0, 3).map((f) => (
                    <div key={f.op + f.path}>
                      <span className={`ws-flagmark ${PLACE_SEVERITY[f.kind] ?? 'med'}`}>{f.op}</span>
                      <span className="ws-path" data-tip={f.path}>{f.shown}</span>
                    </div>
                  ))}
                  {flags.length + more > 3 && (
                    <div
                      className="nm"
                      data-tip={flags.slice(3).map((f) => `${f.op} ${f.shown}`).join('\n') || undefined}
                    >
                      +{fint(flags.length + more - 3)} more
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function OutsideWrites({ P, cls }: { P: Panels; cls: Cls[] }) {
  const cas = pick<Cascades>(P, 'cascades', 'ebpfm');
  const ws = cas.workspace;
  const agentOn = cls.includes('agent');
  const writers = agentOn ? writersOf(ws) : [];
  const places = placesOf(writers);
  const multiHost = new Set(writers.map((a) => a.host)).size > 1;
  return (
    <Panel
      title={OUTSIDE_TITLE}
      p={cas}
      span
      ask={[{ panel: 'cascades', keys: ['workspace.summary', 'workspace.agents'] }]}
      empty={!agentOn || (ws != null && !writers.length)}
      emptyWhat={!agentOn ? OFF : 'no agent wrote outside its start folder in this window'}
    >
      {!ws ? (
        <NotReturned feed={cas._feed} notice="the agent trees came without a workspace reading" />
      ) : (
        <>
          <div className="eyebrow" data-tip={GLOSS.named}>
            {plural(writers.length, 'agent')} of {fint(ws.summary.agents)} wrote or deleted outside the folder{' '}
            {writers.length === 1 ? 'it was' : 'they were'} started in · paths read from the text of their commands, not measured
          </div>
          <PlaceTable places={places} />
          <div className="eyebrow" style={{ marginTop: 10 }}>by user</div>
          {/* few agents write outside, and their paths are the reading: the first user opens */}
          <ByUser
            rows={writers}
            user={(a) => a.user}
            noun="agent"
            open
            cols={[
              { h: 'agent type', cell: (rs) => distinct(rs, typeLbl), sort: (rs) => distinct(rs, typeLbl) },
              {
                h: 'written', num: true, tip: 'paths written outside their start folders, as their commands name them',
                cell: (rs) => fint(sumOf(rs, (a) => outside(a)?.write)), sort: (rs) => sumOf(rs, (a) => outside(a)?.write),
              },
              {
                h: 'deleted', num: true, tip: 'paths deleted outside their start folders, as their commands name them',
                cell: (rs) => deletes(sumOf(rs, (a) => outside(a)?.delete)), sort: (rs) => sumOf(rs, (a) => outside(a)?.delete),
              },
              {
                h: 'where', tip: 'the kinds of place they are in, the most alarming first',
                cell: (rs) => <KindMarks kinds={kindsOf(rs)} />,
                sort: (rs) => placeRank(kindsOf(rs)[0] ?? 'temp'),
              },
            ]}
            sub={(rs, withUser) => <WriterRows rows={rs} multiHost={multiHost} withUser={withUser} />}
          />
        </>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------------------ the tab */

export const SecurityTab = memo(function SecurityTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  return (
    <>
      <div className="grid wider">
        <SecurityChecks P={P} cls={cls} />
      </div>
      <div className="grid-gap" />
      <div className="rowlabel">launch and confinement</div>
      <SandboxTab P={P} cls={cls} />
      <div className="grid-gap" />
      <div className="rowlabel">reach</div>
      <div className="grid wider">
        <OutsideWrites P={P} cls={cls} />
      </div>
    </>
  );
});
