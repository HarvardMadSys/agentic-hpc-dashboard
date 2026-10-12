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
  Workspace,
} from '../api/types';
import { GroupBars, StackRow } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Segmented } from '../components/ClassFilter';
import { Basis, More, Panel, NoRows } from '../components/Panel';
import { Plate } from '../components/Plate';
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

/* Agents only, named in words. The processes between them -- the remote
 * server or daemon that hosts a set of sessions, a node launcher, the shells --
 * are plumbing: the tree's heading says what it is, and the hover keeps the
 * command line and pids for whoever needs them. A tree of one agent needs no
 * heading at all; its row says everything. */
function TreeRows({ t, open, toggle }: { t: CascadeTree; open: boolean; toggle: () => void }) {
  const agents = t.nodes.filter((n) => n.role === 'agent');
  const grouped = agents.length > 1;
  return (
    <tbody>
      {grouped && (
        <tr className="treehead" onClick={toggle}>
          <td colSpan={3} data-tip={`started under\n${nodeTip(t.root)}`}>
            <span className="caret">{open ? '▾' : '▸'}</span>
            <b>{treeTitle(t, agents)}</b>
          </td>
          <td className="num">{fint(t.tool_calls)}</td>
          <td className="num">{dur(t.cpu_s)}</td>
          <td colSpan={2} />
        </tr>
      )}
      {(open || !grouped) &&
        agents.map((n) => {
          const w = n.workspace;
          const sub = n.agent_level != null && n.agent_level >= 2;
          const indent = grouped ? (n.agent_level ?? 1) : 0;
          return (
            <tr key={n.pid}>
              <td style={{ paddingLeft: 8 + indent * 18 }} data-tip={nodeTip(n)}>
                {indent > 0 && <span style={{ color: 'var(--edge)' }}>{'└ '}</span>}
                {typeLbl(n.agent_type)}
                {sub && (
                  <span className="sev sub-chip" data-tip={`started beneath another agent, ${n.agent_level} agents deep`}>
                    subagent
                  </span>
                )}
              </td>
              <td className="mono">{n.user ?? DASH}</td>
              <td
                className={`ws-home${n.home_src === 'first_call' ? ' inferred' : ''}`}
                data-tip={
                  n.home
                    ? [n.home, n.home_src ? HOME_TIP[n.home_src] : null, sub && n.cwd_vs_parent_agent ? PARENT_LBL[n.cwd_vs_parent_agent] : null]
                        .filter(Boolean).join('\n')
                    : 'no working directory recorded for it or its calls'
                }
              >
                {tildeOf(n.home, n.user)}
              </td>
              <td className="num">{n.tool_calls ? fint(n.tool_calls) : DASH}</td>
              <td className="num">{n.cpu_s ? dur(n.cpu_s) : DASH}</td>
              <td className="num" data-tip="distinct directories it ran in or named (see Workspace & blast radius)">
                {w?.dirs ? fint(w.dirs) : DASH}
              </td>
              <td>{w ? <OutsideWrites n={w.outside_writes} /> : DASH}</td>
            </tr>
          );
        })}
    </tbody>
  );
}

function TreeTable({ trees, isOpen, toggle }: {
  trees: CascadeTree[];
  isOpen: (t: CascadeTree) => boolean;
  toggle: (t: CascadeTree) => void;
}) {
  return (
    <div className="scroller">
      <table className="roomy trees">
        <thead>
          <tr>
            <th>agent</th>
            <th>user</th>
            <th data-tip="the directory the agent was started in; italic: it started at `/`, so its first call's directory">working in</th>
            <th className="num" data-tip="processes in the agent's session: its tool calls and what they ran">processes</th>
            <th className="num">CPU time</th>
            <th className="num" data-tip="distinct directories it ran in or named">directories</th>
            <th data-tip="distinct paths written or deleted outside its working directory, read off command lines">outside home</th>
          </tr>
        </thead>
        {trees.map((t) => (
          <TreeRows key={`${t.host}:${t.root.pid}`} t={t} open={isOpen(t)} toggle={() => toggle(t)} />
        ))}
      </table>
    </div>
  );
}

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

function AgentRow({
  a,
  mode,
  open,
  toggle,
}: {
  a: WsAgent;
  mode: Mode;
  open: boolean;
  toggle: () => void;
}) {
  const v = viewOf(a, mode);
  if (!v) return null;
  const s = v.summary;
  const usingTree = mode === 'tree' && a.tree != null;
  const outRoots = s.roots;
  return (
    <tbody>
      <tr className={`ws-agent${open ? ' open' : ''}`} onClick={toggle}>
        <td style={{ minWidth: 200 }}>
          <span className="ws-caret">{open ? '▾' : '▸'}</span>
          <span style={{ color: 'var(--ink)' }} data-tip={`pid ${a.pid} on ${a.host}`}>
            {typeLbl(a.agent_type)}
          </span>
          <div className="ws-sub" style={{ marginLeft: 16 }}>
            {a.user ?? DASH}
            {a.parent_agent != null && ' · a subagent'}
            {a.subagents.length > 0 &&
              ` · ${usingTree ? 'with' : '+'} ${a.subagents.length} subagent${a.subagents.length === 1 ? '' : 's'}`}
          </div>
        </td>
        <td
          className={`ws-home${a.home_src === 'first_call' ? ' inferred' : ''}`}
          style={{ maxWidth: 240 }}
          data-tip={a.home ? `${a.home}\n${a.home_src ? HOME_TIP[a.home_src] : ''}` : 'no cwd recorded for it or its calls: nothing to be inside or outside of'}
        >
          {a.home_shown ?? DASH}
        </td>
        <td className="num" data-tip={`processes folded into this agent${usingTree ? ' and its subagents' : ''}, from ${fint(s.keys)} collector session keys`}>
          {fint(s.procs)}
        </td>
        <td style={{ minWidth: 150 }}>
          {s.procs_inside != null && s.procs_outside != null && s.procs_inside + s.procs_outside > 0 ? (
            <div
              data-tip={[
                `ran inside the home: ${fint(s.procs_inside)} processes`,
                `ran outside it: ${fint(s.procs_outside)}`,
                s.cwd_read_pct != null
                  ? `cwd read at exec for ${pc(s.cwd_read_pct)} of them; the rest copied from their parent at fork, before any cd`
                  : null,
              ]
                .filter(Boolean)
                .join('\n')}
            >
              <StackRow
                segs={[
                  { k: 'ran inside the home', v: s.procs_inside, c: 'var(--s2)' },
                  { k: 'ran outside it', v: s.procs_outside, c: 'var(--s6)' },
                ]}
                W={120}
                H={14}
              />
            </div>
          ) : (
            DASH
          )}
        </td>
        <td className="num" data-tip="distinct directories it ran in or named, before folding for display">
          {fint(s.dirs)}
        </td>
        <td className="ws-inf">
          <Ops d={s.named} />
        </td>
        <td>
          <OutsideWrites n={s.outside_writes} />
        </td>
        <td>
          <div className="ws-roots">
            {outRoots.slice(0, 5).map((g) => {
              const wrote = g.write + g.delete > 0;
              return (
                <span
                  key={g.root}
                  className={`ws-root${wrote ? ' wrote' : ''}${wrote && SEVERITY[g.kind] === 'high' ? ' high' : ''}`}
                  data-tip={`${g.root} · ${g.kind}\nran there: ${fint(g.procs)} processes\nnamed: read ${fint(g.read)}, written ${fint(g.write)}, deleted ${fint(g.delete)}, other ${fint(g.named)}`}
                >
                  {g.root}
                  {g.delete > 0 ? <b className="ws-d"> d</b> : g.write > 0 ? <b className="ws-w"> w</b> : null}
                </span>
              );
            })}
            {outRoots.length > 5 && <span className="ws-sub">+{outRoots.length - 5}</span>}
            {!outRoots.length && <span style={{ color: 'var(--muted)' }}>{a.home ? 'none' : DASH}</span>}
          </div>
        </td>
      </tr>
      {open && (
        <tr className="ws-detail">
          <td colSpan={8}>
            <Flags v={v} />
            <DirTable v={v} />
          </td>
        </tr>
      )}
    </tbody>
  );
}

function BlastRadius({ c, w }: { c: Cascades; w: Workspace | null | undefined }) {
  const [mode, setMode] = useState<Mode>('self');
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const agents = [...(w?.agents ?? [])]
    .filter((a) => viewOf(a, mode) != null)
    .sort((x, y) => (viewOf(y, mode)?.summary.procs ?? 0) - (viewOf(x, mode)?.summary.procs ?? 0));
  const k = (a: WsAgent) => `${a.host}:${a.pid}`;
  // Opened on first render: the first agent that wrote outside its home, so
  // the reading this panel exists for is on screen without a click.
  const firstFlagged = agents.find((a) => (viewOf(a, mode)?.summary.outside_writes ?? 0) > 0) ?? agents[0];
  const isOpen = (a: WsAgent) => opened[k(a)] ?? a === firstFlagged;
  const s = w?.summary;
  const basis = (
    <Basis
      items={[
        ...Object.entries(w?.basis ?? {}),
        ['summary', s],
        ['buffer', w?.buffer],
      ]}
    />
  );
  return (
    <Panel
      title="Workspace & blast radius"
      p={c}
      span
      empty={!agents.length}
      emptyWhat="no agent left a process record in this window"
      header={
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { k: 'self', lbl: 'this agent only', tip: 'only the processes folded into the agent itself' },
            {
              k: 'tree',
              lbl: 'with its subagents',
              tip: 'adds every agent started beneath it, judged against THIS agent’s home',
            },
          ]}
        />
      }
      basis={basis}
    >
      <div
        className="eyebrow"
        style={{ marginBottom: 4 }}
        data-tip={[
          'home: the agent process’s own cwd; italic when it started at `/` (IDE-hosted) and its first call’s directory stands in',
          'outside the home: a write or delete NAMED beyond it is flagged; /tmp, caches, and the agent’s own state directory too, drawn quieter',
          `directories deeper than ${s?.collapse_depth ?? 4} levels below the home or a top-level root fold into that ancestor, as do a directory’s quieter subdirectories past six; every fold is counted`,
          'scope: agent-class exit records with a session key, folded into their agent as trajectory rows are; a fork that inherited its parent’s argv is not read twice',
        ].join('\n')}
      >
        {s
          ? `${fint(s.agents)} agent${s.agents === 1 ? '' : 's'} · ${fint(s.with_outside_writes)} wrote or deleted outside their home · ${fint(s.with_subagents)} started subagents${s.shown < s.agents ? ` · busiest ${fint(s.shown)} served` : ''}`
          : DASH}
      </div>
      <div className="scroller">
        <table>
          <thead>
            <tr>
              <th>agent</th>
              <th data-tip="the agent's own cwd: the directory it was started in">home</th>
              <th className="num">processes</th>
              <th data-tip="share of its processes whose working directory was inside the home">ran inside home</th>
              <th className="num" data-tip="distinct directories it ran in or named">dirs</th>
              <th className="ws-inf" data-tip="paths its commands named: r read · w write · d delete · n other. Taken from command lines, so a path named is not proof it was touched">named</th>
              <th data-tip="distinct paths written or deleted outside the home (inferred)">outside home</th>
              <th data-tip="top-level places outside the home it ran in or named; w: wrote there, d: deleted there (inferred)">roots outside home</th>
            </tr>
          </thead>
          {agents.map((a) => (
            <AgentRow
              key={k(a)}
              a={a}
              mode={mode}
              open={isOpen(a)}
              toggle={() => setOpened((o) => ({ ...o, [k(a)]: !isOpen(a) }))}
            />
          ))}
        </table>
      </div>
    </Panel>
  );
}

export function AgentsTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const c = pick<Cascades>(P, 'cascades', 'ebpfm');
  // Every plate here counts agents, so its headline is the agent class's: with
  // the agent class filtered out there is nothing to count, not zero.
  const on = cls.includes('agent');
  const ag = <T,>(v: T | null | undefined): T | null => (on && v != null ? v : null);
  const win = winLabel(c);
  const s = c.summary;
  const w = c.workspace;
  const trees = c.trees ?? [];
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const treeKey = (t: CascadeTree) => `${t.host}:${t.root.pid}`;
  const isOpen = (t: CascadeTree) => opened[treeKey(t)] ?? true;
  const toggleTree = (t: CascadeTree) => setOpened((o) => ({ ...o, [treeKey(t)]: !isOpen(t) }));
  // idle by its agents' own work: a host process can run things while the
  // agents it hosts run nothing
  const agentWork = (t: CascadeTree) =>
    t.nodes.reduce((a, n) => a + (n.role === 'agent' ? n.tool_calls ?? 0 : 0), 0);
  const busyTrees = trees.filter((t) => agentWork(t) > 0);
  const idleTrees = trees.filter((t) => !(agentWork(t) > 0));

  /* Both are histograms, and a histogram has to show its empty bins: listing
   * only the values that occurred put "8 agents" straight under "2 agents", as
   * if nothing lay between them. Depth stays exact -- agents nest a few levels
   * at most, and every level is a different story. Fan-out does not stay small:
   * one orchestrator looping `claude -p` over a directory starts dozens, so it
   * is binned by doubling (1, 2, 3-4, 5-8, ...), which keeps a fan-out of 500
   * to nine columns without hiding the long tail. Drawn as columns, the bins
   * left to right in order, as a histogram reads. */
  const depthHist = c.depth_hist ?? {};
  const maxDepth = Math.max(0, ...Object.keys(depthHist).map(Number));
  // Two series so a nested tree keeps its alarm colour; each column holds one.
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
    return {
      k,
      v: { n },
      tips: { n: `${fint(n)} started ${k} agent${hi === 1 ? '' : 's'} directly` },
    };
  });
  const fanSeries: BarSeries[] = [{ k: 'n', lbl: 'started', c: 'var(--s4)' }];

  const basis = (extra: [string, unknown][] = []) => (
    <Basis items={[...Object.entries(c.basis ?? {}), ['window', c.window], ['buffer', c.buffer], ...extra]} />
  );
  const ws = w?.summary;

  return (
    <>
      <div className="plates">
        <Plate
          k={`agent trees ${win}`}
          total={ag(s?.trees)}
          stripe="var(--s1)"
          by={{}}
          classes={[]}
          note={s ? `${fint(s.agents)} agent processes · ${fint(s.trees_without_agents)} trees of infrastructure only` : undefined}
          tip="a tree is a root and everything started under it; only a process whose own argv makes it an agent session (roles.py) counts as an agent -- tool-call shells, sandboxes, probes, and helpers do not"
        />
        <Plate
          k="subagents: agent under agent"
          total={ag(s?.agents_nested)}
          stripe={(s?.agents_nested ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={{}}
          classes={[]}
          note={s ? (s.agents_nested ? `in ${fint(s.nested)} tree${s.nested === 1 ? '' : 's'} · deepest chain ${s.max_agent_depth ?? DASH} agents` : 'no agent started another agent as a process') : undefined}
          tip="agents started, through any number of steps, by another agent; in-process subagents (Task tool) are not processes and are not counted"
        />
        <Plate
          k="wrote outside their home"
          total={ag(ws?.with_outside_writes)}
          stripe={(ws?.with_outside_writes ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={{}}
          classes={[]}
          note={ws ? `of ${fint(ws.agents)} agents · a write or delete named on a command line (inferred)` : undefined}
          tip="agents (each on its own processes, not its subagents') whose command lines wrote or deleted a path outside the directory they were started in. Inferred from argv: the collector records no file paths, and Claude Code's Read/Edit/Write tools leave no command line"
        />
        <Plate
          k="reached another user's home"
          total={ag(ws?.other_homes)}
          stripe={(ws?.other_homes ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={{}}
          classes={[]}
          note={ws?.shared_roots != null ? `${fint(ws.shared_roots)} reached shared storage (/scratch, /n/…) · ran there or named a path there` : undefined}
          tip="agents with a process that RAN in another user's home directory (measured, its cwd) or a command line that NAMED a path there (inferred) -- the agent left its own user's space"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Agent trees"
          p={c}
          span
          empty={!trees.length}
          emptyWhat="no agent process in this window"
          basis={basis()}
        >
          {/* the shape of the trees first: how deep they go, how wide they fan */}
          {(depthGroups.length > 0 || fanGroups.length > 0) && (
            <div className="trees-shape">
              <div>
                <div className="eyebrow" data-tip="agent depth: the most agents on any root-to-leaf path of a tree; 1 means no agent was started beneath another">
                  how deep agents nest
                </div>
                {depthGroups.length ? (
                  <GroupBars groups={depthGroups} series={depthSeries} W={480} H={190} maxBar={44} ylabel="trees" fmtVal={fint} tickFmt={fint} labelTop />
                ) : (
                  <NoRows what="no agent tree" />
                )}
              </div>
              <div>
                <div className="eyebrow" data-tip="agents directly below an agent (or a tree's root) with no other agent between; an infra node in a chain is not counted again at every hop">
                  fan-out: agents each one started directly
                </div>
                {fanGroups.length ? (
                  <GroupBars groups={fanGroups} series={fanSeries} W={480} H={190} maxBar={44} ylabel="agents or roots" fmtVal={fint} tickFmt={fint} labelTop />
                ) : (
                  <NoRows what="no agent started another" />
                )}
              </div>
            </div>
          )}
          <TreeTable trees={busyTrees} isOpen={isOpen} toggle={toggleTree} />
          {idleTrees.length > 0 && (
            <More label={`${fint(idleTrees.length)} agent tree${idleTrees.length === 1 ? '' : 's'} that ran nothing in the window`}>
              <TreeTable trees={idleTrees} isOpen={isOpen} toggle={toggleTree} />
            </More>
          )}
        </Panel>

        {w === undefined || w === null ? (
          <Panel title="Workspace & blast radius" p={c} span empty emptyWhat="the service sent no workspace for this window" />
        ) : (
          <BlastRadius c={c} w={w} />
        )}

      </div>
    </>
  );
}
