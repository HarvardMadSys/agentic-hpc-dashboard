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
import { ToolPanels } from './SessionsTab';
import { useState } from 'react';
import type {
  Cls,
  CascadeNode,
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
import { GLOSS } from '../lib/glossary';
import { DASH, dur, fint, mb, pc } from '../lib/format';
import { AGENT_TYPE_LBL } from '../lib/classes';
import { pick, winLabel } from '../lib/panels';
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
  ]
    .filter(Boolean)
    .join('\n');
}

function OutsideWrites({ n, tip }: { n: number | null | undefined; tip?: string }) {
  if (n == null) return <span data-tip="no home to be outside of">{DASH}</span>;
  if (n === 0) return <span style={{ color: 'var(--muted)' }}>0</span>;
  return (
    <span className="ws-flagmark" data-tip={tip ?? 'distinct paths written or deleted outside the home, named on command lines (inferred)'}>
      {fint(n)} outside
    </span>
  );
}

const typeLbl = (t: string | null | undefined) => AGENT_TYPE_LBL[t ?? ''] ?? t ?? 'agent';

/** A tree of more than one agent, in a few words. */
function treeTitle(t: CascadeTree, agents: CascadeNode[]): string {
  const types = Array.from(new Set(agents.map((n) => typeLbl(n.agent_type))));
  const kind = types.length === 1 ? types[0] : 'agent';
  const k = t.agents_nested;
  if (k > 0) {
    return `${kind} with ${k} subagent${k === 1 ? '' : 's'}${t.agent_depth > 2 ? `, ${t.agent_depth - 1} levels deep` : ''}`;
  }
  return `${agents.length} ${kind} sessions started side by side`;
}

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
    ['ws-n', d.named, 'named, no known effect'],
  ];
  const shown = parts.filter(([, v]) => v > 0);
  if (!shown.length) return <span style={{ color: 'var(--muted)' }}>{DASH}</span>;
  return (
    <span className="ws-ops">
      {shown.map(([c, v, lbl]) => (
        <span key={c} className={c} data-tip={`${fint(v)} distinct path${v === 1 ? '' : 's'} ${lbl} (inferred from command lines)`}>
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

/* Every directory, busiest first, in a box that scrolls with its header in
 * view: the busiest dozen used to be shown and the rest were a click away. */
function DirTable({ v }: { v: WsView }) {
  const maxProcs = Math.max(1, ...v.dirs.map((d) => d.procs));
  const rows = v.dirs;
  return (
    <>
      <div className="scroller vscroll short">
        <table>
          <thead>
            <tr>
              <th>directory</th>
              <th
                className="num"
                data-tip={`MEASURED: processes whose cwd the collector recorded as this directory${v.summary.cwd_read_pct != null ? ` -- read at exec for ${pc(v.summary.cwd_read_pct)}, copied from the parent at fork for the rest` : ''}`}
              >
                ran in
              </th>
              <th />
              <th className="num" data-tip="MEASURED per process: syscall bytes read by the processes that ran here">bytes read</th>
              <th className="num" data-tip="MEASURED per process: syscall bytes written by the processes that ran here">bytes written</th>
              <th className="ws-inf" data-tip="INFERRED: distinct paths here that command lines named, by what the command does with them">
                named on command lines
              </th>
              <th>seen</th>
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
                    {d.io ? mb(d.io.rchar_mb) : DASH}
                  </td>
                  <td className="num" data-tip={ioTip(d)}>
                    {d.io ? mb(d.io.wchar_mb) : DASH}
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

function TypeStrip({ stats, sel, setSel }: { stats: TypeStat[]; sel: string; setSel: (t: string) => void }) {
  return (
    <div className="atype-strip" role="tablist" aria-label="agent type">
      {stats.map((s) => (
        <button
          key={s.type}
          role="tab"
          aria-selected={s.type === sel}
          className={`atype${s.type === sel ? ' on' : ''}`}
          onClick={() => setSel(s.type)}
        >
          <div className="k">{typeLbl(s.type)}</div>
          <div className="v">
            {fint(s.agents)} <small>agent{s.agents === 1 ? '' : 's'}</small>
          </div>
          <div className="n">
            <span className="q-mark" data-tip={GLOSS.subagent}>{fint(s.nested)} subagent{s.nested === 1 ? '' : 's'}</span>
            {' · '}
            <span className="q-mark" data-tip="agents of this type that started another agent">{fint(s.started)} started one</span>
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
        </button>
      ))}
    </div>
  );
}

/** One agent as a box: what it is, whose, how much it ran, and whether it
 * wrote outside its home. `foreign`: another type's agent in this tree. */
function AgentCard({ n, foreign }: { n: CascadeNode; foreign?: boolean }) {
  const w = n.workspace;
  const sub = (n.agent_level ?? 1) >= 2;
  return (
    <div
      className={`acard${foreign ? ' foreign' : ''}`}
      data-tip={nodeTip(n) + (sub && n.cwd_vs_parent_agent ? '\n' + PARENT_LBL[n.cwd_vs_parent_agent] : '')}
    >
      <div className="acard-h">
        <b>{typeLbl(n.agent_type)}</b>
        {sub && <span className="sev sub-chip">subagent</span>}
        <span className="mono nm acard-u">{n.user ?? DASH}</span>
      </div>
      <div className="acard-b">
        {n.tool_calls ? `${fint(n.tool_calls)} processes` : 'ran nothing'}
        {n.cpu_s ? ` · ${dur(n.cpu_s)}` : ''}
        {w?.outside_writes ? <span className="ws-flagmark"> {fint(w.outside_writes)} outside</span> : null}
      </div>
      <div className={`acard-c nm${n.home_src === 'first_call' ? ' inferred' : ''}`}>{tildeOf(n.home, n.user)}</div>
    </div>
  );
}

/** A tree of more than one agent, drawn as a tree. */
function TreeView({ t, type, sel, onSel }: {
  t: CascadeTree;
  type: string;
  sel: string | null;
  onSel: (k: string) => void;
}) {
  const parents = agentParents(t);
  const rootIsAgent = t.root.role === 'agent';
  const nodes: TreeNode<CascadeNode | null>[] = [];
  if (!rootIsAgent) nodes.push({ id: 'root', parent: null, data: null });
  for (const n of t.nodes) {
    if (n.role !== 'agent') continue;
    const p = parents.get(n.pid);
    nodes.push({
      id: keyOf(t.host, n.pid),
      parent: p != null ? keyOf(t.host, p) : rootIsAgent && n.pid === t.root.pid ? null : 'root',
      data: n,
    });
  }
  // open on the picked agent, or on this type's first: in a wide tree of
  // another type's agents it can sit a screen to the right
  const first = nodes.find((x) => x.data && (x.data.agent_type ?? '') === type);
  const focus = sel != null && nodes.some((x) => x.id === sel) ? sel : first?.id ?? null;
  return (
    <TreeDiagram
      nodes={nodes}
      selected={sel}
      focus={focus}
      onSelect={(id) => id !== 'root' && onSel(id)}
      render={(x) =>
        x.data ? (
          <AgentCard n={x.data} foreign={(x.data.agent_type ?? '') !== type} />
        ) : (
          <div className="acard infra" data-tip={`started under\n${nodeTip(t.root)}`}>
            <div className="acard-h">
              <b>{t.root.command ?? 'process'}</b>
            </div>
            <div className="acard-b nm">{t.root.role === 'client' ? 'a client that launched the agents' : 'runs the agents below'}</div>
          </div>
        )
      }
    />
  );
}

/** One agent's blast radius, opened from its card (roadmap note 35.3). */
function AgentBlast({ a }: { a: WsAgent | undefined }) {
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
        <span className="ws-inf"><Term k="named">named</Term> <Ops d={s.named} /></span>
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
      <DirTable v={v} />
    </div>
  );
}

const GRID_SHOWN = 24;

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
  const [selType, setSelType] = useState<string | null>(null);
  const type = stats.find((x) => x.type === selType)?.type ?? stats[0]?.type ?? '';
  const [selAgent, setSelAgent] = useState<string | null>(null);
  const [allGrid, setAllGrid] = useState(false);

  // Trees with more than one agent are drawn; a lone agent is a card. A
  // tree belongs to every type it has an agent of: a Claude Code that started
  // a Codex is on both views, the other type's box drawn quieter.
  const mine = pts.filter((p) => (p.n.agent_type ?? '') === type);
  const multi = trees.filter(
    (t) => t.nodes.filter((n) => n.role === 'agent').length > 1 &&
      t.nodes.some((n) => n.role === 'agent' && (n.agent_type ?? '') === type),
  );
  const inMulti = new Set(multi.map((t) => keyOf(t.host, t.root.pid)));
  const lone = mine
    .filter((p) => !inMulti.has(keyOf(p.t.host, p.t.root.pid)))
    .sort((a, b) => (b.n.tool_calls ?? 0) - (a.n.tool_calls ?? 0));
  const busyLone = lone.filter((p) => (p.n.tool_calls ?? 0) > 0);
  const idleLone = lone.filter((p) => !((p.n.tool_calls ?? 0) > 0));
  const gridShown = allGrid ? busyLone : busyLone.slice(0, GRID_SHOWN);
  const selPt = selAgent ? pts.find((p) => keyOf(p.t.host, p.n.pid) === selAgent) : undefined;
  const pickAgent = (k: string) => setSelAgent(selAgent === k ? null : k);

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

  const detailHere = (where: 'tree' | 'grid', t?: CascadeTree) =>
    selPt &&
    (where === 'tree' ? selPt.t === t : !inMulti.has(keyOf(selPt.t.host, selPt.t.root.pid))) && (
      <AgentBlast key={selAgent} a={selPt.ws} />
    );

  return (
    <>
      <div className="rowlabel">agents by type · {win} · click one</div>
      {stats.length ? (
        <TypeStrip stats={stats} sel={type} setSel={(t) => { setSelType(t); setSelAgent(null); }} />
      ) : (
        <NoRows what={cls.includes('agent') ? 'no agent process in this window' : 'the agent class is filtered out'} />
      )}

      {stats.length > 0 && (
        <div className="grid wide">
          <Panel
            title={`${typeLbl(type)}: its agents and their subagents`}
            ask={[{ panel: 'cascades', keys: ['summary', 'trees', 'role_census', 'workspace.summary'] }]}
            id="sec-agent-trees"
            p={c}
            span
            empty={!mine.length}
            emptyWhat="no agent of this type in the window"
          >
            {multi.length > 0 && (
              <>
                <div className="eyebrow" data-tip="trees with more than one agent: agents that started subagents, or several sessions started side by side under one process">
                  started together · {fint(multi.length)} tree{multi.length === 1 ? '' : 's'}
                </div>
                {multi.map((t) => {
                  const agents = t.nodes.filter((n) => n.role === 'agent');
                  return (
                    <div key={keyOf(t.host, t.root.pid)} className="atree">
                      <div className="atree-t">{treeTitle(t, agents)}{t.users.length ? ` · ${t.users.join(', ')}` : ''}</div>
                      <TreeView t={t} type={type} sel={selAgent} onSel={pickAgent} />
                      {detailHere('tree', t)}
                    </div>
                  );
                })}
              </>
            )}
            {busyLone.length > 0 && (
              <>
                <div className="eyebrow" style={{ marginTop: multi.length ? 10 : 0 }}>
                  on their own · {fint(busyLone.length)} agent{busyLone.length === 1 ? '' : 's'}, busiest first · click one for its blast radius
                </div>
                <div className="agrid">
                  {gridShown.map((p) => {
                    const k = keyOf(p.t.host, p.n.pid);
                    return (
                      <div key={k} className={`agrid-cell${selAgent === k ? ' on' : ''}`} onClick={() => pickAgent(k)}
                        role="button" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && pickAgent(k)}>
                        <AgentCard n={p.n} />
                      </div>
                    );
                  })}
                </div>
                {busyLone.length > GRID_SHOWN && !allGrid && (
                  <button className="ws-more" onClick={() => setAllGrid(true)}>show all {fint(busyLone.length)}</button>
                )}
                {detailHere('grid')}
              </>
            )}
            {idleLone.length > 0 && (
              <More label={`${fint(idleLone.length)} agent${idleLone.length === 1 ? '' : 's'} that ran nothing in the window`}>
                <div className="agrid">
                  {idleLone.map((p) => (
                    <div key={keyOf(p.t.host, p.n.pid)} className="agrid-cell"><AgentCard n={p.n} /></div>
                  ))}
                </div>
              </More>
            )}
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

      {/* What the picked type's agents ran, by kind of tool: was the Sessions
          & tools tab (roadmap notes 46 and 47), and never mixes types. */}
      <div className="grid-gap" />
      <ToolPanels P={P} cls={cls} type={type || null} />
    </>
  );
}
