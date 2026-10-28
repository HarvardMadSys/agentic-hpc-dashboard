/* Agents -- agent and subagent behaviour: which agents started which, and where
 * each one worked.
 *
 * An agent started, through any number of steps, beneath another agent is that
 * agent's SUBAGENT. Three things this page refuses to do, all of which the raw
 * data invites:
 *
 * - Count every classifier-tagged process as an agent. The collector tags any
 *   process whose argv mentions ~/.claude, ~/.codex or ~/.vscode-server, which
 *   makes thousands of "nested agents" a day out of tool-call shells, embedded
 *   ripgrep and version probes. Only role `agent` is an agent here; the rest
 *   of what was tagged is still in the payload's `role_census`.
 * - Call hosting nesting. A daemon or a remote server running eight sessions is
 *   fan-out from infrastructure, not an agent inside an agent; `nested` counts
 *   only agent-under-agent paths.
 * - Present a named path as a touched file. The collector records no file
 *   paths. Where an agent's processes RAN (their cwd) and the bytes they moved
 *   are measured; the paths its command lines NAMED are inferred, and every
 *   place that shows them is drawn in the dashed `inferred` style.
 */
import { ToolPanels } from './ToolPanels';
import { useEffect, useRef, useState } from 'react';
import type {
  Cls,
  CascadeNode,
  IoSort,
  CascadeTree,
  Cascades,
  HomeSrc,
  WsAgent,
  WsDir,
  WsKind,
  WsView,
} from '../api/types';
import { GroupBars } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Segmented } from '../components/ClassFilter';
import { More, Panel, NoRows } from '../components/Panel';
import { TreeDiagram } from '../components/TreeDiagram';
import type { TreeNode } from '../components/TreeDiagram';
import { Term } from '../components/Term';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { GLOSS } from '../lib/glossary';
import { DASH, dur, fint, mb, pc } from '../lib/format';
import { AGENT_TYPE_COL, AGENT_TYPE_LBL } from '../lib/classes';
import { AP_LABEL, CALL_LABEL, SESSION_LABEL, label } from '../lib/sandbox';
import { pick, winLabel } from '../lib/panels';
import { coveredBy } from '../lib/iocmds';
import type { IoQuery } from '../lib/iocmds';
import { IoNum } from '../components/IoCmds';
import type { Panels } from '../lib/panels';
import './agents.css';

const HOME_TIP: Record<HomeSrc, string> = {
  agent: "the agent process's own cwd: the directory it was started in",
  first_call: 'it was started at `/` (IDE-hosted), so its first call’s directory off the root stands in',
};

/* Directory rows an opened agent shows before "show all". */

/* How alarming a write outside the home is, by where it landed. A write to the
 * agent's own scratch in /tmp is still flagged -- it is outside the project --
 * but it is not drawn like a write into another user's home. */
const SEVERITY: Record<WsKind, 'high' | 'med' | 'low'> = {
  "other user's home": 'high',
  system: 'high',
  'filesystem root': 'high',
  'shared storage': 'med',
  'own home': 'med',
  other: 'med',
  home: 'low',
  'cache & tooling': 'low',
  'agent state': 'low',
  temp: 'low',
};

/** `/home/<user>` as `~`, another user's home as `~<them>`. */
function tildeOf(path: string | null | undefined, user: string | null | undefined): string {
  if (!path) return DASH;
  if (user && (path === `/home/${user}` || path.startsWith(`/home/${user}/`))) {
    return '~' + path.slice(`/home/${user}`.length);
  }
  return path.replace(/^\/home\//, '~');
}

function nodeTip(n: CascadeNode): string {
  return [
    `pid ${n.pid}${n.ppid != null ? ' · ppid ' + n.ppid : ''} · ${n.host}`,
    n.args ? `argv ${n.args}` : null,
    n.cwd ? `cwd ${n.cwd}` : null,
    n.started ? `started ${n.started}` : null,
    n.last_seen ? `last seen ${n.last_seen}${n.exited ? ' (exited)' : ''}` : null,
    n.spawned_by != null ? `--spawned-by pid ${n.spawned_by}` : null,
    n.frontend ? 'front-end: it only launches the agent directly below it (`codex app-server`, or the native binary a node launcher execs), which runs the session' : null,
    n.implied ? 'implied: a tool-call shell named it as parent; it left no record itself' : null,
    ...(n.role === 'agent' ? launchTip(n) : []),
  ]
    .filter(Boolean)
    .join('\n');
}

/** An agent's launch and its calls' sandbox verdict, in words: what its
 * letters (Tags) say, and why a box has none. */
function launchTip(n: CascadeNode): string[] {
  const flags = n.approval_detail && n.approval_detail !== 'no launch flag' ? ` · ${n.approval_detail}` : '';
  const c = Object.entries(n.sandbox_calls ?? {}).filter(([, v]) => v);
  const all = c.reduce((a, [, v]) => a + (v ?? 0), 0);
  const calls = c.length === 1 ? ` · ${fint(all)} call${all === 1 ? '' : 's'}`
    : c.length ? ' · ' + c.map(([k, v]) => `${fint(v ?? 0)} ${label(CALL_LABEL, k)}`).join(', ') : '';
  return [
    `launch: ${label(AP_LABEL, n.approval ?? 'unknown')}${flags}`,
    `sandbox: ${label(SESSION_LABEL, n.sandbox ?? 'no_calls')}${calls}${
      !n.sandbox && n.agent_type === 'codex' ? ' (a Codex command run without a sandbox wrapper is not counted)' : ''}`,
  ];
}

function OutsideWrites({ n, tip }: { n: number | null | undefined; tip?: string }) {
  if (n == null) return <span data-tip="no home to be outside of">{DASH}</span>;
  if (n === 0) return <span style={{ color: 'var(--muted)' }}>0</span>;
  return (
    <span className="ws-flagmark" data-tip={tip ?? 'distinct paths written or deleted outside the home, as its commands mention them (guessed from the text)'}>
      {fint(n)} outside
    </span>
  );
}

const typeLbl = (t: string | null | undefined) => AGENT_TYPE_LBL[t ?? ''] ?? t ?? 'agent';
const typeCol = (t: string | null | undefined) => AGENT_TYPE_COL[t ?? ''] ?? 'var(--sg)';

const PARENT_LBL: Record<string, string> = {
  same: "in its parent agent's directory",
  inside: "inside its parent agent's directory",
  above: "above its parent agent's directory",
  outside: "outside its parent agent's directory",
};

/* ---------------------------------------------------------- blast radius */

type Mode = 'self' | 'tree';

/* With its subagents, an agent with none is its own view; alone, an agent whose
 * subagents left the only records has no row (it ran nothing itself). */
const viewOf = (a: WsAgent, mode: Mode): WsView | null => (mode === 'tree' ? a.tree ?? a.self : a.self);

function Ops({ d }: { d: { read: number; write: number; delete: number; named: number } }) {
  const parts: [string, number, string][] = [
    ['ws-r', d.read, 'read'],
    ['ws-w', d.write, 'written'],
    ['ws-d', d.delete, 'deleted'],
    ['ws-n', d.named, 'mentioned, with no known effect'],
  ];
  const shown = parts.filter(([, v]) => v > 0);
  if (!shown.length) return <span style={{ color: 'var(--muted)' }}>{DASH}</span>;
  return (
    <span className="ws-ops">
      {shown.map(([c, v, lbl]) => (
        <span key={c} className={c} data-tip={`${fint(v)} distinct path${v === 1 ? '' : 's'} ${lbl} (guessed from the text of its commands)`}>
          {c.slice(3)} {fint(v)}
        </span>
      ))}
    </span>
  );
}

function when(e: number | null): string {
  if (e == null) return DASH;
  const d = new Date(e * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** First to last seen; the second date is dropped when it is the same day. */
function seen(first: number | null, last: number | null): string {
  if (first == null || last == null || first === last) return when(first ?? last);
  const a = when(first);
  const b = when(last);
  return `${a} – ${a.slice(0, 5) === b.slice(0, 5) ? b.slice(6) : b}`;
}

function dirTip(d: WsDir): string {
  return [
    d.path,
    d.kind === 'home' ? 'inside the home' : `outside the home · ${d.kind}${d.root ? ' · ' + d.root : ''}`,
    d.deeper > 0 ? `${fint(d.deeper)} deeper director${d.deeper === 1 ? 'y' : 'ies'} folded in` : null,
    d.examples.length
      ? 'named here (inferred):\n' + d.examples.map(([p, o]) => `  ${o.padEnd(3)} ${p}`).join('\n')
      : null,
    d.names_capped ? `+${fint(d.names_capped)} more named paths, counted not listed` : null,
  ]
    .filter(Boolean)
    .join('\n');
}

function ioTip(d: WsDir): string | undefined {
  if (!d.io) return d.procs ? 'no io block measured for these processes' : 'no process ran here';
  return [
    `syscall bytes: read ${mb(d.io.rchar_mb)}, written ${mb(d.io.wchar_mb)} (pipes and terminals included)`,
    `disk bytes: read ${mb(d.io.rd_mb)}, written ${mb(d.io.wr_mb)}`,
    `measured on ${pc(d.io.coverage_pct)} of the ${fint(d.procs)} processes that ran here`,
    'credited to where the process ran, not to where the bytes went',
  ].join('\n');
}

/* Every directory, A to Z, in a box that scrolls with its header in view:
 * the busiest dozen used to be shown and the rest were a click away. */
function DirTable({ v, ioq }: { v: WsView; ioq?: (d: WsDir, sort: IoSort) => IoQuery }) {
  const maxProcs = Math.max(1, ...v.dirs.map((d) => d.procs));
  const s = useSort<string>('dir', 'asc');
  const rows = s.sort(v.dirs, (d, k) =>
    k === 'dir' ? d.shown : k === 'procs' ? d.procs : k === 'rd' ? d.io?.rchar_mb ?? null
    : k === 'wr' ? d.io?.wchar_mb ?? null : k === 'named' ? d.named : d.last);
  return (
    <>
      <div className="scroller vscroll short">
        <table>
          <thead>
            <tr>
              <SortTh s={s} k="dir" first="asc">directory</SortTh>
              <SortTh s={s} k="procs" num
                tip={`MEASURED: processes whose cwd the collector recorded as this directory${v.summary.cwd_read_pct != null ? ` -- read at exec for ${pc(v.summary.cwd_read_pct)}, copied from the parent at fork for the rest` : ''}`}>
                ran in
              </SortTh>
              <th />
              <SortTh s={s} k="rd" num tip="MEASURED per process: syscall bytes read by the processes that ran here">bytes read</SortTh>
              <SortTh s={s} k="wr" num tip="MEASURED per process: syscall bytes written by the processes that ran here">bytes written</SortTh>
              <SortTh s={s} k="named" className="ws-inf" tip="GUESSED from the text of its commands, not measured: distinct files here that its commands mention, by what each command does with them">
                files its commands mention
              </SortTh>
              <SortTh s={s} k="seen">seen</SortTh>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const sev = d.flag ? SEVERITY[d.kind] : null;
              return (
                <tr key={d.path}>
                  <td style={{ minWidth: 260, maxWidth: 520 }} data-tip={dirTip(d)}>
                    {d.flag && (
                      <span className={`ws-flagmark ${sev ?? ''}`} data-tip={`${d.flag === 'delete' ? 'a delete' : 'a write'} named outside the home`}>
                        {d.flag} outside
                      </span>
                    )}
                    <span className={`ws-path${d.inside === false ? ' out' : ''}`}>{d.shown}</span>
                    {d.deeper > 0 && <span className="ws-kind">+{fint(d.deeper)} deeper</span>}
                    {d.inside === false && <span className="ws-kind">{d.kind}</span>}
                  </td>
                  <td className="num">{fint(d.procs)}</td>
                  <td>
                    {d.procs > 0 && (
                      <div className="ws-track">
                        <div className="ws-fill" style={{ width: `${(100 * d.procs) / maxProcs}%` }} />
                      </div>
                    )}
                  </td>
                  <td className="num" data-tip={ioTip(d)}>
                    {d.io ? (ioq ? <IoNum q={ioq(d, 'rchar_mb')}>{mb(d.io.rchar_mb)}</IoNum> : mb(d.io.rchar_mb)) : DASH}
                  </td>
                  <td className="num" data-tip={ioTip(d)}>
                    {d.io ? (ioq ? <IoNum q={ioq(d, 'wchar_mb')}>{mb(d.io.wchar_mb)}</IoNum> : mb(d.io.wchar_mb)) : DASH}
                  </td>
                  <td className="ws-inf">
                    <Ops d={d} />
                  </td>
                  <td className="mono" style={{ fontSize: '10.5px', whiteSpace: 'nowrap', color: 'var(--muted)' }}>
                    {seen(d.first, d.last)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

/* Low-severity flags shown before the rest fold: an agent's own /tmp scratch,
 * cache or state directory is outside its project, but a list of forty of
 * them would bury the one write into another user's home. */
const LOW_SHOWN = 3;

function Flags({ v }: { v: WsView }) {
  const [all, setAll] = useState(false);
  if (!v.flags.length) return null;
  const high = v.flags.some((f) => SEVERITY[f.kind] === 'high');
  const loud = v.flags.filter((f) => SEVERITY[f.kind] !== 'low');
  const quiet = v.flags.filter((f) => SEVERITY[f.kind] === 'low');
  const shown = all ? v.flags : [...loud, ...quiet.slice(0, LOW_SHOWN)];
  const folded = v.flags.length - shown.length;
  const kinds = [...new Set(quiet.slice(LOW_SHOWN).map((f) => f.kind))].join(', ');
  return (
    <div className={`ws-flags${high ? ' high' : ''}`}>
      <h4>
        Written or deleted outside its home
      </h4>
      <ul>
        {shown.map((f) => (
          <li key={f.op + f.path}>
            <span className={`ws-flagmark ${SEVERITY[f.kind]}`}>{f.op}</span>
            <span className="ws-path" data-tip={f.path}>
              {f.shown}
            </span>
            <span className="ws-kind">{f.kind}</span>
          </li>
        ))}
      </ul>
      {folded > 0 && (
        <button className="ws-more" style={{ marginTop: 4 }} onClick={() => setAll(true)}>
          {`+${fint(folded)} more in ${kinds}`}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------- the agents view
 * Split by agent type (roadmap note 35): a strip compares the types, and a
 * click shows one type's agents -- trees with subagents drawn as trees, the
 * rest as cards -- and each agent's blast radius opens from its own card
 * (note 35.3), not from a second table. */

interface AgentPt {
  n: CascadeNode;
  t: CascadeTree;
  ws: WsAgent | undefined;
}

const keyOf = (host: string, pid: number) => `${host}:${pid}`;

/** Each agent in a tree, with the nearest agent above it (or none). */
function agentParents(t: CascadeTree): Map<number, number | null> {
  const byPid = new Map(t.nodes.map((n) => [n.pid, n]));
  const out = new Map<number, number | null>();
  for (const n of t.nodes) {
    if (n.role !== 'agent') continue;
    let p = n.parent != null ? byPid.get(n.parent) : undefined;
    while (p && p.role !== 'agent') p = p.parent != null ? byPid.get(p.parent) : undefined;
    out.set(n.pid, p ? p.pid : null);
  }
  return out;
}

interface TypeStat {
  type: string;
  agents: number;
  /** started beneath another agent */
  nested: number;
  /** started at least one subagent */
  started: number;
  procs: number;
  cpu: number;
  outside: number;
  otherHome: number;
}

function typeStats(pts: AgentPt[]): TypeStat[] {
  const m = new Map<string, TypeStat>();
  for (const { n, t, ws } of pts) {
    const k = n.agent_type ?? '';
    const s = m.get(k) ?? { type: k, agents: 0, nested: 0, started: 0, procs: 0, cpu: 0, outside: 0, otherHome: 0 };
    s.agents += 1;
    if ((n.agent_level ?? 1) >= 2) s.nested += 1;
    if (agentParents(t).size && [...agentParents(t).values()].includes(n.pid)) s.started += 1;
    s.procs += n.tool_calls ?? 0;
    s.cpu += n.cpu_s ?? 0;
    if ((ws?.self?.summary.outside_writes ?? n.workspace?.outside_writes ?? 0) > 0) s.outside += 1;
    if (ws?.self?.summary.roots.some((r) => r.kind === "other user's home")) s.otherHome += 1;
    m.set(k, s);
  }
  return [...m.values()].sort((a, b) => b.procs - a.procs);
}

/** Agents by type, side by side: a summary, not a switch. A Claude Code can
 * start a Codex and a Codex a Claude Code, so the trees below are one view of
 * every type, each box named by its own. */
function TypeStrip({ stats }: { stats: TypeStat[] }) {
  return (
    <div className="atype-strip">
      {stats.map((s) => (
        <div key={s.type} className="atype" style={{ borderLeftColor: typeCol(s.type) }}>
          <div className="k">{typeLbl(s.type)}</div>
          <div className="v">
            {fint(s.agents)} <small>agent{s.agents === 1 ? '' : 's'}</small>
          </div>
          <div className="n">
            <span className="q-mark" data-tip={GLOSS.subagent}>{fint(s.nested)} subagent{s.nested === 1 ? '' : 's'}</span>
            {' · '}
            <span className="q-mark" data-tip="agents of this type that started another agent, of any type">{fint(s.started)} started one</span>
          </div>
          <div className="n">
            {fint(s.procs)} processes · {dur(s.cpu)} CPU
          </div>
          <div className="n">
            <span className={`q-mark${s.outside ? ' warnword' : ''}`} data-tip={GLOSS.outsidehome}>
              {fint(s.outside)} wrote outside home
            </span>
            {' · '}
            <span className={`q-mark${s.otherHome ? ' critword' : ''}`} data-tip={GLOSS.otherhome}>
              {fint(s.otherHome)} reached another user's home
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

const procs = (n: number | null | undefined) =>
  n ? `${fint(n)} process${n === 1 ? '' : 'es'}` : 'ran nothing';

/** The agent's version, after its type, where its program's path names it. */
function Ver({ n }: { n: CascadeNode }) {
  return n.version ? <span className="aver" data-tip={GLOSS.version}>v{n.version}</span> : null;
}

const calls = (k: number) => (k === 1 ? 'its one tool call' : `all ${fint(k)} of its tool calls`);

/** How an agent was launched, and whether its tool calls ran in a sandbox, as
 * a letter in a box each: what is usual -- no bypass flag, calls confined, no
 * calls at all -- shows nothing. A sandbox tried and failed is [F], never [U]:
 * on rtx6000a every Codex sandbox fails to start, and that is not "never tried". */
const TAGS: { k: string; cls: string; lbl: string }[] = [
  { k: 'B', cls: 'crit', lbl: 'bypass flag' },
  { k: 'A', cls: 'warn', lbl: 'auto mode' },
  { k: 'U', cls: '', lbl: 'tool calls not sandboxed' },
  { k: 'F', cls: '', lbl: 'sandbox failed to start' },
];

function tagsOf(n: CascadeNode): { k: string; tip: string }[] {
  const out: { k: string; tip: string }[] = [];
  const how = n.approval_detail && n.approval_detail !== 'no launch flag' ? `\nlaunched with ${n.approval_detail}` : '';
  if (n.approval === 'bypassed') out.push({ k: 'B', tip: GLOSS.bypass + how });
  else if (n.approval === 'auto') out.push({ k: 'A', tip: GLOSS.automode + how });
  const c = n.sandbox_calls ?? {};
  const iso = c.isolated ?? 0;
  const open = c.not_isolated ?? 0;
  const failed = c.failed ?? 0;
  const also = open ? `; ${fint(open)} more ran without isolation` : '';
  if (n.sandbox === 'failed') {
    out.push({ k: 'F', tip: `sandbox failed to start: ${failed === 1 ? 'its one tool call' : `${fint(failed)} of its tool calls`} tried a sandbox, and the sandbox did not come up${also}` });
  } else if (n.sandbox === 'unsandboxed') {
    out.push({ k: 'U', tip: `not sandboxed: ${calls(open)} ran without isolation` });
  } else if (n.sandbox === 'partial') {
    out.push({ k: 'U', tip: `partly sandboxed: ${fint(iso)} of its ${fint(iso + open + failed)} tool calls ran isolated${also}${failed ? `, and ${fint(failed)} tried a sandbox that did not come up` : ''}` });
  }
  return out;
}

function Tags({ n }: { n: CascadeNode }) {
  const t = tagsOf(n);
  if (!t.length) return null;
  return (
    <span className="ltags">
      {t.map(({ k, tip }) => (
        <span key={k} className={`ltag ${TAGS.find((x) => x.k === k)?.cls ?? ''}`} data-tip={tip}>{k}</span>
      ))}
    </span>
  );
}

/** The tags an entry's agents wear, each letter once, with how many wear it. */
function TagSum({ ns }: { ns: CascadeNode[] }) {
  const m = new Map<string, number>();
  for (const n of ns) for (const { k } of tagsOf(n)) m.set(k, (m.get(k) ?? 0) + 1);
  if (!m.size) return null;
  return (
    <span className="ltags">
      {TAGS.filter(({ k }) => m.has(k)).map(({ k, cls, lbl }) => (
        <span key={k} className={`ltag ${cls}`} data-tip={`${lbl}: ${fint(m.get(k))} of its ${fint(ns.length)} agents`}>
          {k}
        </span>
      ))}
    </span>
  );
}

/* ------------------------------------------------- what the list shows
 * Who and which type are the page's filter (WhoFilter). The list narrows
 * further by marks: an agent passes when it wears one of the picked marks, a
 * tag's letter or `O` for a write outside its home, and a tree passes when
 * any of its agents does. */

/** the letters, then the write outside its home, as filter chips */
const MARKS = [...TAGS, { k: 'O', cls: '', lbl: 'wrote outside home' }];

const marksOf = (n: CascadeNode): string[] => [
  ...tagsOf(n).map((t) => t.k),
  ...(n.workspace?.outside_writes ? ['O'] : []),
];

const passes = (marks: string[], n: CascadeNode) => marksOf(n).some((k) => marks.includes(k));

/** The marks as chips, each saying what its letter means and how many agents
 * wear it, and how the list is laid out. */
function AgentFilter({ pts, marks, setMarks, by, setBy, shown }: {
  pts: AgentPt[];
  marks: string[];
  setMarks: (m: string[]) => void;
  by: By;
  setBy: (b: By) => void;
  shown: number;
}) {
  const n = new Map<string, number>();
  for (const p of pts) for (const k of marksOf(p.n)) n.set(k, (n.get(k) ?? 0) + 1);
  const toggle = (k: string) => setMarks(marks.includes(k) ? marks.filter((x) => x !== k) : [...marks, k]);
  return (
    <div className="afilter">
      <span className="ltag-key">
        {MARKS.map(({ k, cls, lbl }) => {
          const on = marks.includes(k);
          return (
            <button key={k} aria-pressed={on} disabled={!on && !n.get(k)} onClick={() => toggle(k)}
              data-tip={on ? 'click to stop filtering on it' : `show only the agents with it${marks.length ? ', or with another picked mark' : ''}`}>
              {k === 'O' ? <span className="ws-flagmark">outside</span> : <span className={`ltag ${cls}`}>{k}</span>} {lbl}
              <span className="nm"> {fint(n.get(k) ?? 0)}</span>
            </button>
          );
        })}
      </span>
      <span className="afilter-end">
        {marks.length > 0 && (
          <>
            <span className="nm">{fint(shown)} of {fint(pts.length)} agents</span>
            <button className="linkbtn" onClick={() => setMarks([])}>clear</button>
          </>
        )}
        <Segmented
          value={by}
          onChange={setBy}
          options={[
            { k: 'list', lbl: 'one list', tip: 'every entry in one list, the latest first' },
            { k: 'user', lbl: 'by user', tip: 'a heading per user, the user active last first' },
          ]}
        />
      </span>
    </div>
  );
}

/* ------------------------------------------- the list beside the detail
 * Every tree of more than one agent, and every agent on its own, is a row of
 * one list; the row picked opens on the right: its tree, then the blast radius
 * of the agent picked in it. It was a page of trees above a grid of cards,
 * where a blast radius opened below the whole tree or grid. */

type By = 'list' | 'user';

/** When an entry was last active. Running ones come first, the latest
 * started first; the rest follow by when they were last seen. The service's
 * `YYYY-MM-DD HH:MM:SS` sorts as text. */
interface Recency {
  running: boolean;
  /** running: when it started (a tree: its earliest running agent) */
  since: string;
  last: string;
}

const newer = (a: Recency, b: Recency) =>
  Number(b.running) - Number(a.running) ||
  (a.running ? b.since.localeCompare(a.since) : b.last.localeCompare(a.last));

const latest = (xs: (string | null)[]) => xs.reduce<string>((m, x) => (x && x > m ? x : m), '');
const earliest = (xs: (string | null)[]) => xs.reduce<string>((m, x) => (x && (!m || x < m) ? x : m), '');

function recencyOf(ns: CascadeNode[], since: 'first' | 'last'): Recency {
  const run = ns.filter((n) => !n.exited).map((n) => n.started);
  return {
    running: run.length > 0,
    since: since === 'first' ? earliest(run) : latest(run),
    last: latest(ns.map((n) => n.last_seen)),
  };
}

/** `MM-DD HH:MM` of the service's `YYYY-MM-DD HH:MM:SS` */
const hm = (s: string | null | undefined) => (s ? s.slice(5, 16) : DASH);

/** One time to another; the second's date is dropped on the same day. */
function span(a: string | null | undefined, b: string | null | undefined): string {
  if (!a || !b || a.slice(0, 16) === b.slice(0, 16)) return hm(a || b);
  return `${hm(a)} – ${a.slice(0, 10) === b.slice(0, 10) ? b.slice(11, 16) : hm(b)}`;
}

function Live({ since }: { since: string }) {
  return (
    <span className="al-live" data-tip="still running at the end of the window">
      <i className="ld" />
      running since {hm(since)}
    </span>
  );
}

/** A row of the list: a tree of more than one agent, an agent on its own, or
 * a fold of look-alike agents on their own (one user, type, and home). */
type Entry =
  | { k: 'tree'; key: string; t: CascadeTree; agents: CascadeNode[]; at: Recency }
  | { k: 'lone'; key: string; p: AgentPt; at: Recency }
  | { k: 'fold'; key: string; ps: AgentPt[]; at: Recency };

/* Agents on their own that share a user, type, and home fold into one row at
 * this many: nineteen short Codex runs in ~ are one row, not nineteen cards. */
const FOLD_AT = 3;

const entryUsers = (e: Entry): string =>
  (e.k === 'tree' ? e.t.users.join(', ') : e.k === 'lone' ? e.p.n.user : e.ps[0].n.user) || DASH;

function Outside({ n, tip }: { n: number; tip: string }) {
  return n ? <span className="ws-flagmark" data-tip={tip}>{fint(n)} outside</span> : null;
}

/** Paths written outside their homes, over several agents, for a row that
 * stands for them all: the same count a lone agent's row shows, summed. */
function OutsideOf({ ns }: { ns: CascadeNode[] }) {
  const by = ns.map((n) => n.workspace?.outside_writes ?? 0).filter((v) => v > 0);
  const sum = by.reduce((a, v) => a + v, 0);
  return (
    <Outside n={sum} tip={`${fint(sum)} distinct path${sum === 1 ? '' : 's'} written or deleted outside their home, as their commands mention them (guessed from the text): ${
      fint(by.length)} of these ${fint(ns.length)} agents wrote them, and a path two of them named counts twice`} />
  );
}

/** An agent on its own: its type, version and tags; whose it is and its
 * home; when it ran, how much, and whether it wrote outside its home. */
function LoneRow({ n, on, onPick, inFold }: { n: CascadeNode; on: boolean; onPick: () => void; inFold?: boolean }) {
  const w = n.workspace;
  return (
    <button className={`al-item${on ? ' on' : ''}`} style={{ borderLeftColor: on ? undefined : typeCol(n.agent_type) }}
      onClick={onPick} data-tip={nodeTip(n)}>
      <span className="al-h">
        {!inFold && <i className="tdot" style={{ background: typeCol(n.agent_type) }} />}
        <b>{typeLbl(n.agent_type)}</b>
        <Ver n={n} />
        <span className="al-flags">
          <Outside n={w?.outside_writes ?? 0} tip="distinct paths written or deleted outside its home, as its commands mention them (guessed from the text)" />
          <Tags n={n} />
        </span>
      </span>
      {!inFold && (
        <span className="al-s mono">
          {n.user ?? DASH} · <span className={n.home_src === 'first_call' ? 'inferred' : undefined}>{tildeOf(n.home, n.user)}</span>
        </span>
      )}
      <span className="al-s">
        {n.exited ? `${span(n.started, n.last_seen)}${n.duration_s != null ? ` · ${dur(n.duration_s)}` : ''}` : <Live since={n.started ?? ''} />}
        {' · '}
        {procs(n.tool_calls)}
        {n.cpu_s ? ` · ${dur(n.cpu_s)} CPU` : ''}
      </span>
    </button>
  );
}

function TreeRow({ e, on, onPick }: { e: Extract<Entry, { k: 'tree' }>; on: boolean; onPick: () => void }) {
  const { t, agents, at } = e;
  const lead = agents.filter((n) => (n.agent_level ?? 1) <= 1).sort(busier)[0];
  const user = t.users.length === 1 ? t.users[0] : null;
  return (
    <button className={`al-item${on ? ' on' : ''}`} style={{ borderLeftColor: on ? undefined : typeCol(lead?.agent_type) }}
      onClick={onPick} data-tip="click to draw its tree">
      <span className="al-h">
        <b className="wrap">{composition(t, agents)}</b>
        <span className="al-flags">
          <OutsideOf ns={agents} />
          <TagSum ns={agents} />
        </span>
      </span>
      <span className="al-s mono">
        {t.users.join(', ') || DASH}{lead?.home ? ` · ${tildeOf(lead.home, user)}` : ''}
      </span>
      <span className="al-s">
        {at.running ? <Live since={at.since} /> : span(earliest(agents.map((n) => n.started)), at.last)}
        {' · '}
        {fint(t.tool_calls)} processes · {dur(t.cpu_s)} CPU
      </span>
    </button>
  );
}

/** Look-alike agents on their own, as one row that opens to theirs. */
function FoldRow({ e, open, onOpen }: { e: Extract<Entry, { k: 'fold' }>; open: boolean; onOpen: () => void }) {
  const ns = e.ps.map((p) => p.n);
  const n0 = ns[0];
  const starts = ns.map((n) => n.started);
  const took = ns.map((n) => n.duration_s).filter((d): d is number => d != null);
  const running = ns.filter((n) => !n.exited).length;
  return (
    <button className={`al-item fold${open ? ' open' : ''}`} style={{ borderLeftColor: typeCol(n0.agent_type) }}
      onClick={onOpen} aria-expanded={open}
      data-tip={`${fint(ns.length)} ${typeLbl(n0.agent_type)} agents on their own, of one user, in one home: click to ${open ? 'fold them' : 'list them'}`}>
      <span className="al-h">
        <span className="al-caret">{open ? '▾' : '▸'}</span>
        <i className="tdot" style={{ background: typeCol(n0.agent_type) }} />
        <b>{fint(ns.length)} {typeLbl(n0.agent_type)} runs</b>
        <span className="al-flags">
          <OutsideOf ns={ns} />
          <TagSum ns={ns} />
        </span>
      </span>
      <span className="al-s mono">
        {n0.user ?? DASH} · {tildeOf(n0.home, n0.user)}
      </span>
      <span className="al-s">
        {running ? <span className="al-live"><i className="ld" />{fint(running)} running · </span> : null}
        {hm(earliest(starts))} – {hm(latest(starts))}
        {took.length ? ` · ${dur(Math.min(...took))} to ${dur(Math.max(...took))} each` : ''}
      </span>
    </button>
  );
}

/** The picked agent on its own: what its row said, at the head of its blast radius. */
function LoneHead({ n }: { n: CascadeNode }) {
  return (
    <div className="atree-t">
      <span className="atree-what">
        <b>{typeLbl(n.agent_type)}</b> <Ver n={n} />
        <span className="nm"> · {n.user ?? DASH}</span>
        <span className="nm mono"> · {tildeOf(n.home, n.user)}</span>
        <span className="nm"> · {n.exited ? span(n.started, n.last_seen) : `running since ${hm(n.started)}`}</span>
      </span>
      <span className="atree-sum nm">
        <Tags n={n} /> {procs(n.tool_calls)}{n.cpu_s ? ` · ${dur(n.cpu_s)} CPU` : ''}
      </span>
    </div>
  );
}

/** An agent in a tree, named by its type -- the one thing every box needs,
 * since a tree can hold more than one -- with its version and tags, then how
 * much it ran, then whether it wrote outside its home. Its user and home show
 * only where they differ from the tree's title and its parent's. Under a
 * filter, a box that does not pass it is dimmed. */
function AgentNode({ n, treeUser, home, dim }: { n: CascadeNode; treeUser: string | null; home: string | null; dim?: boolean }) {
  const w = n.workspace;
  const sub = (n.agent_level ?? 1) >= 2;
  const user = n.user && n.user !== treeUser ? n.user : null;
  const own = n.home && n.home !== home ? tildeOf(n.home, n.user) : null;
  const facts = [procs(n.tool_calls), n.cpu_s ? `${dur(n.cpu_s)} CPU` : null].filter(Boolean).join(' · ');
  return (
    <div
      className={`anode${dim ? ' dim' : ''}`}
      style={{ borderLeftColor: typeCol(n.agent_type) }}
      data-tip={nodeTip(n) + (sub && n.cwd_vs_parent_agent ? '\n' + PARENT_LBL[n.cwd_vs_parent_agent] : '')}
    >
      <div className="anode-h">
        <b>{typeLbl(n.agent_type)}</b>
        <Ver n={n} />
        <Tags n={n} />
      </div>
      <div className="anode-s">
        {facts}
        {user ? ` · ${user}` : ''}
      </div>
      {/* writes outside its home, beside the home they are outside of */}
      {(w?.outside_writes || own) && (
        <div className="anode-s">
          {w?.outside_writes ? <span className="ws-flagmark">{fint(w.outside_writes)} outside</span> : null}
          {w?.outside_writes && own ? ' ' : null}
          {own && <span className={`mono${n.home_src === 'first_call' ? ' inferred' : ''}`}>{own}</span>}
        </div>
      )}
    </div>
  );
}

/* A parent shows its busiest few agents, and the rest wait in one box that
 * says what they did and opens a dozen at a time: a Codex that started 211
 * subagents of one process each is one box, not a row of 211. */
const FOLD_FIRST = 4;
const FOLD_STEP = 12;

type TreeBox =
  | { k: 'infra' }
  | { k: 'agent'; n: CascadeNode; home: string | null }
  | { k: 'more'; at: string; rest: CascadeNode[]; unsent: CascadeNode['folded']; below: number; top: boolean; any: boolean };

/** busiest first: processes, then writes outside home, then CPU */
const busier = (a: CascadeNode, b: CascadeNode) =>
  (b.tool_calls ?? 0) - (a.tool_calls ?? 0) ||
  (b.workspace?.outside_writes ?? 0) - (a.workspace?.outside_writes ?? 0) ||
  (b.cpu_s ?? 0) - (a.cpu_s ?? 0);

/** What the folded agents did, for their box: the ones on the page, and the
 * ones the service counted but did not send (`folded`). Named by their type
 * when the ones on the page are all of one. */
function MoreBox({ b }: { b: Extract<TreeBox, { k: 'more' }> }) {
  const u = b.unsent;
  const all = b.rest.length + (u?.agents ?? 0);
  const nothing = b.rest.filter((n) => !n.tool_calls).length + (u?.ran_nothing ?? 0);
  const one = b.rest.filter((n) => n.tool_calls === 1).length + (u?.one ?? 0);
  const more = all - nothing - one;
  const outside = b.rest.filter((n) => n.workspace?.outside_writes).length + (u?.outside ?? 0);
  const types = Array.from(new Set(b.rest.map((n) => typeLbl(n.agent_type))));
  const noun = `${types.length === 1 ? types[0] + ' ' : ''}${b.top ? 'agent' : 'subagent'}${all === 1 ? '' : 's'}`;
  const groups: [number, string][] = [
    [one, `${fint(one)} ran 1 process each`],
    [nothing, `${fint(nothing)} ran nothing`],
    [more, `${fint(more)} ran more`],
  ];
  const lines = [
    ...groups.filter(([n]) => n > 0).sort((a, z) => z[0] - a[0]).map(([, l]) => l),
    outside ? `${fint(outside)} wrote outside home` : null,
    b.below ? `${fint(b.below)} more under them` : null,
  ].filter((l): l is string => !!l);
  const tip = b.rest.length
    ? `click to show ${Math.min(FOLD_STEP, b.rest.length)} more, the busiest first`
    : 'counted, not drawn: the service sends a big tree’s busiest agents';
  return (
    <div className={`anode more${b.rest.length ? '' : ' unsent'}`} data-tip={tip}>
      <div className="anode-h">
        <b>{b.any ? `+${fint(all)} more ${noun}` : `${fint(all)} ${noun}`}</b>
      </div>
      <div className="anode-s">{lines[0] ?? ''}</div>
      <div className="anode-s nm">{lines.slice(1).join(' · ')}</div>
    </div>
  );
}

/** "Claude Code with 2 Codex subagents", "9 Codex agents with 211
 * subagents", "3 Claude Code and Codex agents started side by side": what a
 * tree is made of, every type named, counted over every agent in it --
 * those the service sent and those it only counted. */
function composition(t: CascadeTree, agents: CascadeNode[]): string {
  const leads = agents.filter((n) => (n.agent_level ?? 1) <= 1);
  const subs = agents.filter((n) => (n.agent_level ?? 1) >= 2);
  const nSubs = t.agents_nested ?? subs.length;
  const nLeads = Math.max(leads.length, (t.n_agents ?? agents.length) - nSubs);
  const kinds = (list: CascadeNode[]) => Array.from(new Set(list.map((n) => typeLbl(n.agent_type)))).join(' and ');
  const leadTxt = nLeads === 1 ? kinds(leads) || 'agent' : `${fint(nLeads)} ${kinds(leads)} agents`;
  if (!nSubs) return `${leadTxt} started side by side`;
  const subKinds = kinds(subs);
  const same = !subKinds || subKinds === kinds(leads);
  return `${leadTxt} with ${fint(nSubs)} ${same ? '' : subKinds + ' '}subagent${nSubs === 1 ? '' : 's'}`;
}

/** A tree's title row: what it is made of, whose, where, and what it ran. */
function TreeTitle({ t, agents }: { t: CascadeTree; agents: CascadeNode[] }) {
  const lead = [...agents].filter((n) => (n.agent_level ?? 1) <= 1).sort(busier)[0];
  const user = t.users.length === 1 ? t.users[0] : null;
  return (
    <div className="atree-t">
      <span className="atree-what">
        <b>{composition(t, agents)}</b>
        {t.users.length ? <span className="nm"> · {t.users.join(', ')}</span> : null}
        {lead?.home ? <span className="nm mono"> · {tildeOf(lead.home, user)}</span> : null}
      </span>
      <span className="atree-sum nm">
        {fint(t.tool_calls)} processes · {dur(t.cpu_s)} CPU
      </span>
    </div>
  );
}

/** A tree of more than one agent, of any types, drawn as a tree, folded.
 * Under a filter, the agents that pass it, and the ones above them, are drawn
 * ahead of the fold, and every other box is dimmed. */
function TreeView({ t, sel, onSel, match }: {
  t: CascadeTree;
  sel: string | null;
  onSel: (k: string) => void;
  match: ((n: CascadeNode) => boolean) | null;
}) {
  // how many of a parent's agents are open, past the first FOLD_FIRST
  const [open, setOpen] = useState<Record<string, number>>({});
  const parents = agentParents(t);
  const rootIsAgent = t.root.role === 'agent';
  const id = (n: CascadeNode) => keyOf(t.host, n.pid);
  const agents = t.nodes.filter((n) => n.role === 'agent');
  const byId = new Map(agents.map((n) => [id(n), n]));
  // an agent's parent box: its agent parent; else the tree's root process,
  // drawn as its own box only when it is not itself an agent
  const parentOf = (n: CascadeNode): string | null => {
    const p = parents.get(n.pid);
    if (p != null) return keyOf(t.host, p);
    return rootIsAgent ? null : 'root';
  };
  const kids = new Map<string | null, CascadeNode[]>();
  for (const n of agents) {
    const p = parentOf(n);
    const list = kids.get(p);
    if (list) list.push(n);
    else kids.set(p, [n]);
  }
  const below = (n: CascadeNode): number =>
    (kids.get(id(n)) ?? []).reduce((a, c) => a + 1 + below(c), 0);
  // the picked agent and the agents above it stay in view, folded or not
  const keep = new Set<string>();
  for (let n = sel ? byId.get(sel) : undefined; n; ) {
    keep.add(id(n));
    const p = parentOf(n);
    n = p ? byId.get(p) : undefined;
  }
  const treeUser = t.users.length === 1 ? t.users[0] : null;
  // the title names the busiest lead's home; a box names its own only if it differs
  const titleHome = [...agents].filter((n) => (n.agent_level ?? 1) <= 1).sort(busier)[0]?.home ?? null;
  // an agent that ran one process or none, wrote nothing outside its home,
  // and started no other: a box of its own would only repeat the fold's count
  const trivial = (n: CascadeNode) =>
    (n.tool_calls ?? 0) <= 1 && !n.workspace?.outside_writes && !(kids.get(id(n)) ?? []).length;
  // passes the filter, or has an agent below it that does
  const hits = new Map<string, boolean>();
  const hit = (n: CascadeNode): boolean => {
    if (!match) return false;
    let h = hits.get(id(n));
    if (h == null) {
      h = match(n) || (kids.get(id(n)) ?? []).some(hit);
      hits.set(id(n), h);
    }
    return h;
  };

  const nodes: TreeNode<TreeBox>[] = [];
  if (!rootIsAgent) nodes.push({ id: 'root', parent: null, data: { k: 'infra' } });
  const place = (at: string | null, list: CascadeNode[], home: string | null) => {
    const sorted = [...list].sort((a, b) => Number(hit(b)) - Number(hit(a)) || busier(a, b));
    const opened = open[at ?? ''];
    const limit = opened ?? FOLD_FIRST;
    const shown = sorted.filter((n, i) => keep.has(id(n)) || (i < limit && (opened != null || !trivial(n) || hit(n))));
    for (const n of shown) {
      nodes.push({ id: id(n), parent: at, data: { k: 'agent', n, home } });
      place(id(n), kids.get(id(n)) ?? [], n.home ?? home);
    }
    const rest = sorted.filter((n) => !shown.includes(n));
    // the agents the service counted under this parent and did not send
    const unsent = (at === 'root' ? t.root : at ? byId.get(at) : undefined)?.folded ?? null;
    if (rest.length || unsent?.agents) {
      nodes.push({
        id: `${at ?? 'top'}:more`,
        parent: at,
        data: {
          k: 'more', at: at ?? '', rest, unsent,
          below: rest.reduce((a, n) => a + below(n), 0),
          top: at === null || at === 'root', any: shown.length > 0,
        },
      });
    }
  };
  place(rootIsAgent ? null : 'root', kids.get(rootIsAgent ? null : 'root') ?? [], titleHome);

  const focus = sel != null && nodes.some((x) => x.id === sel) ? sel : null;
  return (
    <>
      {Object.keys(open).length > 0 && (
        <button className="linkbtn atree-fold" onClick={() => setOpen({})}>
          fold again
        </button>
      )}
      <TreeDiagram
        nodes={nodes}
        nodeW={212}
        nodeH={58}
        gapX={10}
        gapY={22}
        className="atree-d"
        selected={sel}
        focus={focus}
        onSelect={(nid) => {
          const x = nodes.find((y) => y.id === nid)?.data;
          if (!x || x.k === 'infra') return;
          if (x.k === 'more') {
            if (!x.rest.length) return;              // counted, not sent: nothing to open
            const limit = open[x.at] ?? FOLD_FIRST;
            setOpen({ ...open, [x.at]: limit + FOLD_STEP });
            return;
          }
          onSel(nid);
        }}
        render={(x) =>
          x.data.k === 'agent' ? (
            <AgentNode n={x.data.n} treeUser={treeUser} home={x.data.home} dim={!!match && !match(x.data.n)} />
          ) : x.data.k === 'more' ? (
            <MoreBox b={x.data} />
          ) : (
            <div className="anode infra" data-tip={`started under\n${nodeTip(t.root)}`}>
              <div className="anode-h">
                <b className="mono">{t.root.command ?? 'process'}</b>
              </div>
              <div className="anode-s nm">{t.root.role === 'client' ? 'a client that launched the agents' : 'runs the agents below'}</div>
            </div>
          )
        }
      />
    </>
  );
}

/** One agent's blast radius, opened from its card (roadmap note 35.3). */
function AgentBlast({ a, from }: { a: WsAgent | undefined; from?: string | number | null }) {
  const [mode, setMode] = useState<Mode>('self');
  if (!a) {
    return <NoRows what="no blast radius was served for this agent: the service sends the busiest agents' only" />;
  }
  const v = viewOf(a, mode);
  if (!v) return <NoRows what="it ran nothing itself; its subagents did" />;
  const s = v.summary;
  const inside = s.procs_inside != null && s.procs_outside != null && s.procs_inside + s.procs_outside > 0
    ? (100 * s.procs_inside) / (s.procs_inside + s.procs_outside) : null;
  return (
    <div className="ablast">
      <div className="ablast-head">
        <h4>
          <Term k="blast">blast radius</Term> · {typeLbl(a.agent_type)} · {a.user ?? DASH}
        </h4>
        {a.subagents.length > 0 && (
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { k: 'self', lbl: 'this agent only', tip: 'only the processes folded into the agent itself' },
              { k: 'tree', lbl: 'with its subagents', tip: 'adds every agent started beneath it, judged against THIS agent’s home' },
            ]}
          />
        )}
      </div>
      <div className="ablast-sum">
        <span data-tip={a.home ? `${a.home}\n${a.home_src ? HOME_TIP[a.home_src] : ''}` : 'no cwd recorded'}>
          <Term k="home">home</Term> <b className="mono">{a.home_shown ?? DASH}</b>
        </span>
        <span>{fint(s.procs)} processes{inside != null ? `, ${pc(inside)} inside home` : ''}</span>
        <span>{fint(s.dirs)} directories</span>
        <span className="ws-inf"><Term k="named">files its commands mention</Term> <Ops d={s.named} /></span>
        <span><Term k="outsidehome">outside home</Term> <OutsideWrites n={s.outside_writes} /></span>
        {s.roots.length > 0 && (
          <span className="ws-roots">
            {s.roots.slice(0, 6).map((g) => {
              const wrote = g.write + g.delete > 0;
              return (
                <span key={g.root} className={`ws-root${wrote ? ' wrote' : ''}${wrote && SEVERITY[g.kind] === 'high' ? ' high' : ''}`}
                  data-tip={`${g.root} · ${g.kind}\nran there: ${fint(g.procs)} processes\nnamed: read ${fint(g.read)}, written ${fint(g.write)}, deleted ${fint(g.delete)}, other ${fint(g.named)}`}>
                  {g.root}
                  {g.delete > 0 ? <b className="ws-d"> d</b> : g.write > 0 ? <b className="ws-w"> w</b> : null}
                </span>
              );
            })}
          </span>
        )}
      </div>
      <Flags v={v} />
      <DirTable
        v={v}
        ioq={(d, sort) => ({
          title: `${sort === 'rchar_mb' ? 'bytes read' : 'bytes written'} in ${d.shown} · ${typeLbl(a.agent_type)} pid ${a.pid}${mode === 'tree' && a.subagents.length ? ' with its subagents' : ''}`,
          sort,
          class: ['agent'],
          host: a.host,
          apid: mode === 'tree' ? [a.pid, ...a.subagents] : [a.pid],
          cwd: d.path,
          from,
        })}
      />
    </div>
  );
}

export function AgentsTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const c = pick<Cascades>(P, 'cascades', 'ebpfm');
  const win = winLabel(c);
  const trees = c.trees ?? [];
  const ws = new Map((c.workspace?.agents ?? []).map((a) => [keyOf(a.host, a.pid), a]));
  const pts: AgentPt[] = cls.includes('agent')
    ? trees.flatMap((t) =>
        t.nodes.filter((n) => n.role === 'agent').map((n) => ({ n, t, ws: ws.get(keyOf(t.host, n.pid)) })))
    : [];
  const stats = typeStats(pts);
  const [marks, setMarks] = useState<string[]>([]);
  const [by, setBy] = useState<By>('list');
  // the row picked, and the agent picked in it: a lone agent is both
  const [sel, setSel] = useState<{ entry: string; agent: string | null } | null>(null);
  const [opened, setOpened] = useState<string[]>([]);
  const [showIdle, setShowIdle] = useState(false);
  // a row picked while the page is scrolled past the top of the detail
  // brings the detail back into view
  const pane = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = pane.current;
    if (el && sel && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }, [sel?.entry]);

  // One list of every type: a Claude Code can start a Codex and a Codex a
  // Claude Code, so a view per type drew one tree on two pages. A tree of
  // more than one agent is a row; so is an agent on its own, unless it folds.
  const match = marks.length ? (n: CascadeNode) => passes(marks, n) : null;
  const ok = (n: CascadeNode) => !match || match(n);
  const entries: Entry[] = [];
  const lone: AgentPt[] = [];
  for (const t of trees) {
    const agents = t.nodes.filter((n) => n.role === 'agent');
    if (!cls.includes('agent') || !agents.length) continue;
    if (agents.length === 1) {
      if (ok(agents[0])) lone.push({ n: agents[0], t, ws: ws.get(keyOf(t.host, agents[0].pid)) });
    } else if (agents.some(ok)) {
      entries.push({ k: 'tree', key: keyOf(t.host, t.root.pid), t, agents, at: recencyOf(agents, 'first') });
    }
  }
  const busy = lone.filter((p) => (p.n.tool_calls ?? 0) > 0);
  const idle = lone.filter((p) => !((p.n.tool_calls ?? 0) > 0));
  const alike = new Map<string, AgentPt[]>();
  for (const p of busy) {
    const k = `fold:${p.n.user}|${p.n.agent_type}|${p.n.home}`;
    alike.set(k, [...(alike.get(k) ?? []), p]);
  }
  for (const [k, ps] of alike) {
    if (ps.length >= FOLD_AT) {
      ps.sort((a, b) => newer(recencyOf([a.n], 'last'), recencyOf([b.n], 'last')));
      entries.push({ k: 'fold', key: k, ps, at: recencyOf(ps.map((p) => p.n), 'last') });
    } else {
      for (const p of ps) entries.push({ k: 'lone', key: keyOf(p.t.host, p.n.pid), p, at: recencyOf([p.n], 'last') });
    }
  }
  entries.sort((a, b) => newer(a.at, b.at));
  idle.sort((a, b) => newer(recencyOf([a.n], 'last'), recencyOf([b.n], 'last')));
  const shownAgents = entries.reduce((a, e) =>
    a + (e.k === 'tree' ? e.agents.filter(ok).length : e.k === 'fold' ? e.ps.length : 1), 0) + idle.length;
  // by user: a heading per user, the user active last first
  const groups: [string, Entry[]][] = [];
  if (by === 'user') {
    const at = new Map<string, Entry[]>();
    for (const e of entries) {
      const u = entryUsers(e);
      const g = at.get(u);
      if (g) g.push(e);
      else {
        const ng = [e];
        at.set(u, ng);
        groups.push([u, ng]);
      }
    }
  } else groups.push(['', entries]);

  const picked = sel ? entries.find((e) => e.key === sel.entry) ?? null : null;
  const pickedLone = sel && !picked
    ? [...lone].find((p) => keyOf(p.t.host, p.n.pid) === sel.entry) ?? null
    : null;
  const pickRow = (key: string, agent: string | null) =>
    setSel(sel?.entry === key && sel.agent === agent ? null : { entry: key, agent });
  const toggleFold = (k: string) => setOpened(opened.includes(k) ? opened.filter((x) => x !== k) : [...opened, k]);
  // the arrow keys walk the rows, opening each; a fold only takes the focus
  const walk = (ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    const rows = Array.from(ev.currentTarget.querySelectorAll<HTMLButtonElement>('button.al-item'));
    const i = rows.indexOf(document.activeElement as HTMLButtonElement);
    const next = rows[ev.key === 'ArrowDown' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1)];
    if (!next) return;
    ev.preventDefault();
    next.focus();
    if (!next.classList.contains('fold')) next.click();
  };
  const loneRow = (p: AgentPt, inFold?: boolean) => {
    const k = keyOf(p.t.host, p.n.pid);
    return <LoneRow key={k} n={p.n} on={sel?.entry === k} onPick={() => pickRow(k, k)} inFold={inFold} />;
  };

  // The two histograms, kept for whoever wants the shape of nesting.
  const depthHist = c.depth_hist ?? {};
  const maxDepth = Math.max(0, ...Object.keys(depthHist).map(Number));
  const depthGroups: BarGroup[] = Array.from({ length: maxDepth }, (_, i) => i + 1).map((d) => {
    const n = depthHist[String(d)] ?? 0;
    const k = d === 1 ? 'no subagent' : `${d - 1} level${d === 2 ? '' : 's'}`;
    return {
      k,
      k2: d === 1 ? undefined : 'of subagents',
      v: { [d === 1 ? 'flat' : 'nested']: n },
      tips: { [d === 1 ? 'flat' : 'nested']: `${fint(n)} tree${n === 1 ? '' : 's'}: ${d === 1 ? k : `${k} of subagents`}` },
    };
  });
  const depthSeries: BarSeries[] = [
    { k: 'flat', lbl: 'no subagent', c: 'var(--s1)' },
    { k: 'nested', lbl: 'subagents', c: 'var(--crit)' },
  ];
  const fanHist = Object.entries(c.fanout_hist ?? {}).map(([f, n]) => [+f, n] as const);
  const maxFan = Math.max(0, ...fanHist.map(([f]) => f));
  const fanBins: [number, number][] = [];
  for (let lo = 1, hi = 1; lo <= maxFan; lo = hi + 1, hi = Math.max(lo, 2 * hi)) fanBins.push([lo, hi]);
  const fanGroups: BarGroup[] = fanBins.map(([lo, hi]) => {
    const n = fanHist.reduce((a, [f, m]) => (f >= lo && f <= hi ? a + m : a), 0);
    const k = lo === hi ? `${lo}` : `${lo}–${hi}`;
    return { k, v: { n }, tips: { n: `${fint(n)} started ${k} agent${hi === 1 ? '' : 's'} directly` } };
  });
  const fanSeries: BarSeries[] = [{ k: 'n', lbl: 'started', c: 'var(--s4)' }];

  const detail = (() => {
    if (picked?.k === 'tree') {
      const { t, agents } = picked;
      const a = sel?.agent ?? null;
      return (
        <div className="al-pane">
          <TreeTitle t={t} agents={agents} />
          <TreeView key={picked.key} t={t} sel={a} match={match}
            onSel={(k) => setSel({ entry: picked.key, agent: a === k ? null : k })} />
          {a ? <AgentBlast key={a} a={ws.get(a)} from={coveredBy(c).from} /> : <div className="al-hint">click an agent’s box for its blast radius</div>}
        </div>
      );
    }
    const p = picked?.k === 'lone' ? picked.p : pickedLone;
    if (p) {
      const k = keyOf(p.t.host, p.n.pid);
      return (
        <div className="al-pane">
          <LoneHead n={p.n} />
          <AgentBlast key={k} a={p.ws} from={coveredBy(c).from} />
        </div>
      );
    }
    return <div className="al-empty">pick an agent on the left: its tree and blast radius open here</div>;
  })();

  return (
    <>
      <div className="rowlabel">agents by type · {win}</div>
      {stats.length ? (
        <TypeStrip stats={stats} />
      ) : (
        <NoRows what={cls.includes('agent') ? 'no agent process in this window' : 'the agent class is filtered out'} />
      )}

      {stats.length > 0 && (
        <div className="grid wide">
          <Panel
            title="Agents and their subagents"
            ask={[{ panel: 'cascades', keys: ['summary', 'trees', 'role_census', 'workspace.summary'] }]}
            id="sec-agent-trees"
            p={c}
            span
            empty={!pts.length}
            emptyWhat="no agent in the window"
          >
            <AgentFilter pts={pts} marks={marks} setMarks={setMarks} by={by} setBy={setBy} shown={shownAgents} />
            <div className="amd">
              <div className="al-list" onKeyDown={walk}>
                {!entries.length && !idle.length && <NoRows what="no agent passes the filters" />}
                {groups.map(([u, es]) => (
                  <div key={u} className="al-group">
                    {u && <div className="al-user mono">{u}</div>}
                    {es.map((e) =>
                      e.k === 'tree' ? (
                        <TreeRow key={e.key} e={e} on={sel?.entry === e.key} onPick={() => pickRow(e.key, null)} />
                      ) : e.k === 'lone' ? (
                        loneRow(e.p)
                      ) : (
                        <div key={e.key} className="al-foldwrap">
                          <FoldRow e={e} open={opened.includes(e.key)} onOpen={() => toggleFold(e.key)} />
                          {opened.includes(e.key) && <div className="al-fold">{e.ps.map((p) => loneRow(p, true))}</div>}
                        </div>
                      ),
                    )}
                  </div>
                ))}
                {idle.length > 0 && (
                  <>
                    <button className="linkbtn al-idle" onClick={() => setShowIdle(!showIdle)}>
                      {showIdle ? 'hide' : 'show'} {fint(idle.length)} agent{idle.length === 1 ? '' : 's'} that ran nothing in the window
                    </button>
                    {showIdle && idle.map((p) => loneRow(p))}
                  </>
                )}
              </div>
              <div className="al-detail" ref={pane}>{detail}</div>
            </div>
            <More label="nesting at a glance: how deep agents nest, and how many each started">
              <div className="pair-row">
                <div>
                  <div className="eyebrow" data-tip="trees by the longest chain of agents in them, every type">how deep agents nest</div>
                  {depthGroups.length ? (
                    <GroupBars groups={depthGroups} series={depthSeries} W={480} H={190} maxBar={44} ylabel="trees" fmtVal={fint} tickFmt={fint} labelTop />
                  ) : <NoRows what="no tree in the window" />}
                </div>
                <div>
                  <div className="eyebrow" data-tip={GLOSS.fanout}>fan-out: agents each one started directly</div>
                  {fanGroups.length ? (
                    <GroupBars groups={fanGroups} series={fanSeries} W={480} H={190} maxBar={44} ylabel="agents or roots" fmtVal={fint} tickFmt={fint} labelTop />
                  ) : <NoRows what="no agent started another" />}
                </div>
              </div>
            </More>
          </Panel>
        </div>
      )}

      {/* What agents ran, by kind of tool: was the Sessions & tools tab
          (roadmap notes 46 and 47). */}
      <div className="grid-gap" />
      <ToolPanels P={P} cls={cls} />
    </>
  );
}
