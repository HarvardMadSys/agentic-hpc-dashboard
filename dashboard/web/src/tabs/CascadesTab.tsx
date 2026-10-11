/* Cascades -- which agents started which other agents, and how far each drifted.
 *
 * Two things this tab refuses to do, both of which the raw data invites:
 *
 * - Count every classifier-tagged process as an agent. The collector tags any
 *   process whose argv mentions ~/.claude, ~/.codex or ~/.vscode-server, which
 *   makes thousands of "nested agents" a day out of tool-call shells, embedded
 *   ripgrep and version probes. Only role `agent` is an agent here, and the role
 *   census panel shows everything else that was tagged, so the exclusion is
 *   visible rather than silent.
 * - Call hosting nesting. A daemon or a remote server running eight sessions is
 *   fan-out from infrastructure, not an agent inside an agent; `nested` counts
 *   only agent-under-agent paths.
 *
 * Drift is two labelled proxies, never "context": the share of an agent's
 * processes outside its launch directory, and how much its purpose mix moved
 * between the first and last third of its session.
 */
import { useState } from 'react';
import type { CascadeNode, CascadeTree, Cascades, Drift } from '../api/types';
import { HBars } from '../charts/primitives';
import type { BarGroup } from '../charts/primitives';
import { CountCost } from '../components/CountCost';
import { Basis, Legend, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { DASH, fint, fmt, pc } from '../lib/format';
import { plbl } from '../lib/purposes';
import { pick, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

const ROLE_COL: Record<string, string> = {
  agent: 'var(--s1)',
  infra: 'var(--s4)',
  client: 'var(--s2)',
};

const VIA_LBL: Record<string, string> = {
  ancestor: 'process tree',
  spawned_by: '--spawned-by',
  same_user_daemon: "user's daemon (inferred)",
  tree_root: 'tree root (fallback)',
};

/* Trees opened on first render: anything with more than one agent, plus the
 * first few, so the page shows structure without a click. */
const OPEN_FIRST = 3;

function shortCmd(n: CascadeNode): string {
  if (n.implied) return '(no record of its own — known from its tool calls)';
  const a = (n.args || n.command || '').replace(/^\/home\/[^/]+/, '~');
  return a.length > 96 ? a.slice(0, 96) + '…' : a;
}

function nodeTip(n: CascadeNode): string {
  return [
    `pid ${n.pid}${n.ppid != null ? ' · ppid ' + n.ppid : ''} · ${n.host}`,
    n.args ? `argv ${n.args}` : null,
    n.cwd ? `cwd ${n.cwd}` : null,
    n.started ? `started ${n.started}` : null,
    n.last_seen ? `last seen ${n.last_seen}${n.exited ? ' (exited)' : ''}` : null,
    n.spawned_by != null ? `--spawned-by pid ${n.spawned_by}` : null,
    n.frontend ? 'front-end: its direct child `codex app-server` runs the session' : null,
    n.implied ? 'implied: a tool-call shell named it as parent; it left no record itself' : null,
  ]
    .filter(Boolean)
    .join('\n');
}

function RoleBadge({ n }: { n: CascadeNode }) {
  const lbl = n.frontend ? 'front-end' : n.role;
  return (
    <span
      className="sev"
      style={{ borderColor: ROLE_COL[n.role], color: ROLE_COL[n.role], marginRight: 6 }}
    >
      {lbl}
    </span>
  );
}

function driftTip(d: Drift | null | undefined): string | undefined {
  if (!d) return undefined;
  const lines = [
    d.cwd_anchor ? `launch dir ${d.cwd_anchor} (${d.cwd_anchor_src === 'agent' ? "the agent's own cwd" : 'its first call'})` : null,
    d.cwd_top?.length ? 'busiest dirs:\n' + d.cwd_top.map(([p, k]) => `  ${fint(k)}  ${p}`).join('\n') : null,
    d.cwd_coverage_pct != null ? `cwd measured on ${pc(d.cwd_coverage_pct)} of ${fint(d.n)} processes` : null,
  ];
  return lines.filter(Boolean).join('\n') || undefined;
}

function mixTip(d: Drift | null | undefined): string | undefined {
  if (!d) return undefined;
  if (d.mix_jsd == null) return `needs ${d.min_n} processes; this session has ${fint(d.n)}`;
  const side = (m: [string, number][] | null) =>
    (m ?? []).map(([p, v]) => `${plbl(p)} ${pc(v)}`).join(', ');
  return `first third: ${side(d.mix_first)}\nlast third: ${side(d.mix_last)}`;
}

function TreeRows({ t, i, open, toggle }: { t: CascadeTree; i: number; open: boolean; toggle: () => void }) {
  const parentOf = new Map(t.nodes.map((n) => [n.pid, n]));
  return (
    <tbody>
      <tr className="treehead" onClick={toggle} style={{ cursor: 'pointer', background: 'var(--surface-2)' }}>
        <td colSpan={9}>
          <span className="mono" style={{ color: 'var(--muted)', marginRight: 8 }}>
            {open ? '▾' : '▸'} #{i + 1}
          </span>
          <b>{t.agent_depth >= 2 ? `${t.agent_depth} agents deep` : t.root.role === 'agent' ? 'one agent' : `hosted by ${t.root.role}`}</b>
          <span style={{ color: 'var(--ink-2)' }}>
            {` · ${fint(t.n_agents)} agent${t.n_agents === 1 ? '' : 's'}`}
            {t.max_fanout > 1 ? ` · fan-out ${t.max_fanout}` : ''}
            {` · ${fint(t.tool_calls)} processes · ${t.agent_types.join(', ') || DASH}`}
            {` · ${t.users.join(', ') || DASH} @ ${t.host}`}
          </span>
          {t.hidden_nodes > 0 && (
            <span style={{ color: 'var(--muted)' }} data-tip="infrastructure and clients with no agent beneath them: counted, not listed">
              {` · +${fint(t.hidden_nodes)} idle infra`}
            </span>
          )}
          {t.nodes_truncated && <span style={{ color: 'var(--muted)' }}>{' · list capped'}</span>}
        </td>
      </tr>
      {open &&
        t.nodes.map((n) => {
          const d = n.drift;
          const parent = n.parent != null ? parentOf.get(n.parent) : undefined;
          return (
            <tr key={n.pid}>
              <td style={{ paddingLeft: 8 + n.depth * 18, minWidth: 360, maxWidth: 560 }} data-tip={nodeTip(n)}>
                {n.depth > 0 && <span style={{ color: 'var(--edge)' }}>{'└ '}</span>}
                <RoleBadge n={n} />
                <span className="mono" style={{ color: 'var(--ink-2)', marginRight: 6 }}>
                  {n.agent_type ?? '?'}
                </span>
                <span className="mono" style={{ fontSize: '10.5px', wordBreak: 'break-all' }}>
                  {shortCmd(n)}
                </span>
              </td>
              <td
                className="mono"
                style={{ fontSize: '10.5px', color: n.via === 'ancestor' ? 'var(--ink-2)' : 'var(--muted)' }}
                data-tip={
                  n.via
                    ? `${VIA_LBL[n.via] ?? n.via}${parent ? ' → pid ' + parent.pid : ''}${n.path.length ? '\nthrough: ' + n.path.join(' › ') : ''}`
                    : 'a root: nothing above it'
                }
              >
                {n.via ? VIA_LBL[n.via] ?? n.via : DASH}
                {n.path.length > 0 && <span style={{ color: 'var(--muted)' }}>{` via ${n.path.join(' › ')}`}</span>}
              </td>
              <td className="mono">{n.user ?? DASH}</td>
              <td className="num" data-tip={n.session_keys != null ? `${fint(n.session_keys)} collector session keys folded` : undefined}>
                {fint(n.tool_calls)}
              </td>
              <td className="num">{fmt(n.cpu_s)}</td>
              <td className="num" data-tip={driftTip(d)}>
                {d?.cwd_outside_pct != null ? pc(d.cwd_outside_pct) : DASH}
              </td>
              <td className="num">{d ? fint(d.cwd_distinct) : DASH}</td>
              <td className="num" data-tip={mixTip(d)}>
                {d?.mix_jsd != null ? d.mix_jsd.toFixed(2) : DASH}
              </td>
              <td className="mono" style={{ fontSize: '10.5px' }}>
                {n.cwd_vs_parent_agent ?? DASH}
              </td>
            </tr>
          );
        })}
    </tbody>
  );
}

export function CascadesTab({ P }: { P: Panels }) {
  const c = pick<Cascades>(P, 'cascades', 'ebpfm');
  const win = winLabel(c);
  const s = c.summary;
  const trees = c.trees ?? [];
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const isOpen = (t: CascadeTree, i: number) =>
    opened[`${t.host}:${t.root.pid}`] ?? (i < OPEN_FIRST || t.n_agents > 1);

  const depthItems = Object.entries(c.depth_hist ?? {})
    .sort((a, b) => +a[0] - +b[0])
    .map(([d, n]) => ({
      k: `${d} agent${d === '1' ? '' : 's'} deep`,
      v: n,
      lbl: fint(n),
      c: +d >= 2 ? 'var(--crit)' : 'var(--s1)',
    }));
  const fanItems = Object.entries(c.fanout_hist ?? {})
    .sort((a, b) => +a[0] - +b[0])
    .map(([f, n]) => ({ k: `${f} agent${f === '1' ? '' : 's'}`, v: n, lbl: fint(n), c: 'var(--s4)' }));

  const census = c.role_census ?? [];
  const cpuTot = census.reduce((a, r) => a + (r.cpu_s || 0), 0);
  const censusGroups: BarGroup[] = census.slice(0, 14).map((r) => ({
    k: r.role,
    k2: r.agent_type ?? '?',
    v: { n: r.pct, 'cpu:n': cpuTot ? (100 * (r.cpu_s || 0)) / cpuTot : null },
  }));

  const agents = trees
    .flatMap((t) => t.nodes)
    .filter((n) => n.role === 'agent' && n.drift && n.drift.n > 0)
    .sort(
      (a, b) =>
        (b.drift!.mix_jsd ?? -1) - (a.drift!.mix_jsd ?? -1) ||
        (b.drift!.cwd_outside_pct ?? -1) - (a.drift!.cwd_outside_pct ?? -1),
    );

  const basis = (extra: [string, unknown][] = []) => (
    <Basis items={[...Object.entries(c.basis ?? {}), ['window', c.window], ['buffer', c.buffer], ...extra]} />
  );

  return (
    <>
      <div className="plates">
        <Plate
          k={`agent trees ${win}`}
          total={s?.trees ?? null}
          stripe="var(--s1)"
          by={{}}
          classes={[]}
          note={s ? `${fint(s.agents)} agent processes · ${fint(s.trees_without_agents)} trees of infrastructure only` : undefined}
          tip="a tree is a root and everything started under it; only role `agent` counts as an agent"
        />
        <Plate
          k="nested: agent under agent"
          total={s?.nested ?? null}
          stripe={(s?.nested ?? 0) > 0 ? 'var(--crit)' : 'var(--s2)'}
          by={{}}
          classes={[]}
          note={s ? `${fint(s.agents_nested)} agents inside another · deepest ${s.max_agent_depth ?? DASH}` : undefined}
          tip="trees with an agent started, through any number of steps, by another agent"
        />
        <Plate
          k="hosted by infrastructure"
          total={s?.hosted ?? null}
          stripe="var(--s4)"
          by={{}}
          classes={[]}
          note="a daemon, a remote server or `claude rc` running sessions: fan-out, not nesting"
        />
        <Plate
          k="widest fan-out"
          total={s?.max_fanout ?? null}
          stripe="var(--s6)"
          by={{}}
          classes={[]}
          note="agents directly below one agent or root, none between"
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
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            deepest first · process counts and drift are each agent's folded trajectory session · in-process subagents (Task tool, workflows) are invisible here
          </div>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>process</th>
                  <th>started by</th>
                  <th>user</th>
                  <th className="num" data-tip="processes in the agent's session (its tool calls and what they ran)">processes</th>
                  <th className="num">cpu s</th>
                  <th className="num" data-tip="share of its processes whose cwd is outside its launch directory">cwd out</th>
                  <th className="num" data-tip="distinct working directories">dirs</th>
                  <th className="num" data-tip="Jensen–Shannon divergence of the purpose mix, first vs last third; 0 same, 1 disjoint">mix drift</th>
                  <th data-tip="this agent's launch directory relative to the agent above it">dir vs parent</th>
                </tr>
              </thead>
              {trees.map((t, i) => (
                <TreeRows
                  key={`${t.host}:${t.root.pid}`}
                  t={t}
                  i={i}
                  open={isOpen(t, i)}
                  toggle={() => setOpened((o) => ({ ...o, [`${t.host}:${t.root.pid}`]: !isOpen(t, i) }))}
                />
              ))}
            </table>
          </div>
          <Legend
            items={[
              { k: 'agent — runs a session', c: ROLE_COL.agent },
              { k: 'infra — hosts sessions', c: ROLE_COL.infra },
              { k: 'client / front-end', c: ROLE_COL.client },
            ]}
          />
        </Panel>

        <Panel
          title="Drift by agent"
          p={c}
          span
          empty={!agents.length}
          emptyWhat="no agent with processes of its own in this window"
          basis={
            <Basis
              items={[
                ['cwd out', 'share of the session’s processes whose cwd is outside the launch directory. cwd is read at exec for each tool-call shell and inherited below it, so a `cd` inside one command is not seen'],
                ['mix drift', 'Jensen–Shannon divergence (base 2) between the purpose mix of the first and the last third of the session’s processes; empty below the minimum'],
                ['not context', 'both are process-level proxies; nothing here sees the model’s context window'],
              ]}
            />
          }
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>agent</th>
                  <th>user</th>
                  <th className="num">processes</th>
                  <th>launch dir</th>
                  <th className="num">cwd out</th>
                  <th className="num">dirs</th>
                  <th>last dir</th>
                  <th className="num">mix drift</th>
                  <th>first third → last third</th>
                </tr>
              </thead>
              <tbody>
                {agents.slice(0, 30).map((n) => {
                  const d = n.drift!;
                  const top = (m: [string, number][] | null) => (m && m[0] ? `${plbl(m[0][0])} ${pc(m[0][1], 0)}` : DASH);
                  return (
                    <tr key={`${n.host}:${n.pid}`}>
                      <td data-tip={nodeTip(n)}>
                        <span className="mono" style={{ color: 'var(--ink-2)' }}>{n.agent_type ?? '?'}</span>{' '}
                        <span className="mono" style={{ fontSize: '10.5px', color: 'var(--muted)' }}>pid {n.pid}</span>
                      </td>
                      <td className="mono">{n.user ?? DASH}</td>
                      <td className="num">{fint(d.n)}</td>
                      <td className="mono" style={{ fontSize: '10.5px', maxWidth: 220, wordBreak: 'break-all' }} data-tip={driftTip(d)}>
                        {d.cwd_anchor ?? DASH}
                      </td>
                      <td className="num">{d.cwd_outside_pct != null ? pc(d.cwd_outside_pct) : DASH}</td>
                      <td className="num">{fint(d.cwd_distinct)}</td>
                      <td className="mono" style={{ fontSize: '10.5px', maxWidth: 220, wordBreak: 'break-all' }}>
                        {d.cwd_last ?? DASH}
                      </td>
                      <td className="num" data-tip={mixTip(d)}>
                        {d.mix_jsd != null ? d.mix_jsd.toFixed(2) : DASH}
                      </td>
                      <td style={{ fontSize: '11px' }} data-tip={mixTip(d)}>
                        {d.mix_jsd != null ? `${top(d.mix_first)} → ${top(d.mix_last)}` : DASH}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="How deep agents nest"
          p={c}
          empty={!depthItems.length}
          emptyWhat="no agent tree"
          basis={<Basis items={[['agent depth', c.basis?.agent_depth]]} />}
        >
          <div className="eyebrow">agent trees by the most agents on one path</div>
          <HBars items={depthItems} W={480} labelW={130} />
        </Panel>

        <Panel
          title="Fan-out"
          p={c}
          empty={!fanItems.length}
          emptyWhat="no agent or root started another agent"
          basis={<Basis items={[['fanout', c.basis?.fanout]]} />}
        >
          <div className="eyebrow">agents and roots by how many agents they started</div>
          <HBars items={fanItems} W={480} labelW={130} />
        </Panel>

        <Panel
          title="Who starts whom"
          p={c}
          empty={!(c.edges ?? []).length}
          emptyWhat="every node is a root"
          basis={<Basis items={[['via', c.basis?.via], ['links by how they were found', c.edge_via]]} />}
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>parent</th>
                  <th />
                  <th>child</th>
                  <th className="num">links</th>
                </tr>
              </thead>
              <tbody>
                {(c.edges ?? []).map((e) => (
                  <tr key={e.parent + '>' + e.child}>
                    <td className="mono">{e.parent}</td>
                    <td style={{ color: 'var(--muted)' }}>→</td>
                    <td className="mono">{e.child}</td>
                    <td className="num">{fint(e.n)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="What the classifier tagged as an agent"
          p={c}
          span
          empty={!census.length}
          emptyWhat="no tagged process exited in this window"
          basis={
            <Basis
              items={[
                ['roles', 'from each process’s own argv (rc_dashboard/roles.py). Only `agent` is an agent; the rest is what a count of the collector’s is_agent flag would include'],
                ['grain', 'exit and truncated records of tagged processes'],
              ]}
            />
          }
        >
          <CountCost
            groups={censusGroups}
            series={[{ k: 'n', lbl: 'tagged processes', c: 'var(--s1)' }]}
            countLabel="share of tagged processes"
            costLabel="share of their CPU seconds"
            H={200}
          />
          <div className="eyebrow" style={{ marginTop: 8 }}>
            {`${fint(census.reduce((a, r) => a + r.n, 0))} tagged · ${fint(census.filter((r) => r.role === 'agent').reduce((a, r) => a + r.n, 0))} of them agents`}
          </div>
        </Panel>
      </div>
    </>
  );
}
