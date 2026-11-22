/* Login-node health -- the node tier right now (`node`), on Resources.
 *
 * What the eBPF tier cannot see, because none of it is a process: the load
 * and memory of the whole node, how quickly it answers (the collector's own
 * probes: start a program, stat a local file, look an account up), the file
 * servers it mounts, its local disks, each account's CPU against its limit,
 * and which processes are stuck waiting. One table at a time, picked in the
 * header, each choice with its count -- and the count of what is wrong in it.
 *
 * Each host's row is its newest snapshot, and says how old it is: the node
 * tier writes every few minutes and the service reads it when it rebuilds.
 * The line that flags a filesystem full is the service's (`thresholds`), so
 * this panel and the Needs-attention list draw the same one.
 */
import { useState } from 'react';
import type { LiveResponse, NodeDisk, NodeDState, NodeHost, NodeMount, NodePanel, NodeUser } from '../api/types';
import { CmdCell } from '../components/Cmd';
import { Segmented } from '../components/ClassFilter';
import { NoRows, Panel } from '../components/Panel';
import { ShareCell } from '../components/ShareCell';
import { SortTh } from '../components/SortTh';
import { Term } from '../components/Term';
import { serverNow } from '../lib/clock';
import { GLOSS } from '../lib/glossary';
import { DASH, ago, fint, fmt, pc } from '../lib/format';
import { useNodeWords } from '../lib/nodes';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { useSortMap } from '../lib/useSort';

type View = 'nodes' | 'nfs' | 'disk' | 'accounts' | 'stuck';
/** Rows a table draws before it asks to show the rest. */
const ROWS = 60;

const ms = (v: number | null | undefined) => (v == null ? DASH : `${fmt(v)} ms`);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** The columns of the nodes table: hidden where no host reported a value. */
const NODE_COLS: { k: string; lbl: string; tip?: string; get: (h: NodeHost) => number | null; show: (h: NodeHost) => string }[] = [
  { k: 'load_1', lbl: 'load', tip: GLOSS.load, get: (h) => h.load_1,
    show: (h) => (h.load_1 == null ? DASH : `${fmt(h.load_1)} · ${fmt(h.load_5)} · ${fmt(h.load_15)}`) },
  { k: 'busy', lbl: 'CPU busy', tip: 'the share of every core’s time spent working, at the snapshot',
    get: (h) => (h.cpu_idle_pct == null ? null : 100 - h.cpu_idle_pct),
    show: (h) => (h.cpu_idle_pct == null ? DASH : pc(100 - h.cpu_idle_pct)) },
  { k: 'cpu_iowait_pct', lbl: 'I/O wait', tip: GLOSS.iowait, get: (h) => h.cpu_iowait_pct, show: (h) => pc(h.cpu_iowait_pct) },
  { k: 'mem_used_pct', lbl: 'memory used', tip: 'memory in use, of the node’s total', get: (h) => h.mem_used_pct,
    show: (h) => (h.mem_used_pct == null ? DASH : `${pc(h.mem_used_pct)} of ${fmt(h.mem_total_gb)} GB`) },
  { k: 'fork_exec_ms', lbl: 'answers in', get: (h) => h.fork_exec_ms ?? h.getent_ms,
    tip: 'start a process · stat a local file · look up an account, in ms; sorts by the first',
    show: (h) => (h.fork_exec_ms == null && h.stat_local_ms == null && h.getent_ms == null ? DASH
      : `${fmt(h.fork_exec_ms)} · ${fmt(h.stat_local_ms)} · ${fmt(h.getent_ms)} ms`) },
  { k: 'd_state_procs', lbl: 'D-state', tip: `${GLOSS.dstate}; hover a count for what they wait in`, get: (h) => h.d_state_procs, show: (h) => fint(h.d_state_procs) },
  { k: 'logged_in_users', lbl: 'logged in', tip: 'accounts with a terminal login on the node',
    get: (h) => h.logged_in_users, show: (h) => fint(h.logged_in_users) },
  { k: 'throttled_users', lbl: 'held back', tip: 'accounts the kernel held back at their CPU limit between the two newest snapshots',
    get: (h) => h.throttled_users ?? null, show: (h) => fint(h.throttled_users) },
];

export function NodeHealth({ P, live }: { P: Panels; live: LiveResponse | null }) {
  const nodes = useNodeWords();
  const n = pick<NodePanel>(P, 'node', 'ebpf_node');
  const hosts = n.hosts ?? [];
  const nfs = n.nfs ?? [];
  const disk = n.disk ?? [];
  const stuck = hosts.flatMap((h) => (h.d_state ?? []).map((d) => ({ ...d, host: h.host })));
  const stuckAll = hosts.reduce((a, h) => a + (h.d_state_procs ?? 0), 0);
  const accounts = hosts.flatMap((h) => (h.per_user ?? []).map((u) => ({ ...u, host: h.host })));
  const held = accounts.filter((a) => (a.throttled_delta_s ?? 0) > 0);
  const full = disk.filter((d) => d.full);
  const slowNfs = nfs.filter((m) => (m.rd_retrans ?? 0) + (m.wr_retrans ?? 0) > 0);
  const [view, setView] = useState<View>('nodes');
  const [all, setAll] = useState(false);
  const sortOf = useSortMap();
  const now = serverNow() / 1000;
  // what the eBPF tier's tracked D-state processes were blocked in, per host
  const frames = new Map((live?.hosts ?? []).map((h) => [h.host, h.d_stack_top as Record<string, number> | null | undefined]));

  const count = (lbl: string, n: number, bad: number, badLbl: string) =>
    bad ? `${lbl} ${fint(n)} · ${fint(bad)} ${badLbl}` : `${lbl} ${fint(n)}`;
  const options: { k: View; lbl: string; tip: string }[] = [
    { k: 'nodes', lbl: count(nodes.many, hosts.length, 0, ''), tip: `each ${nodes.one}: load, memory, and how quickly it answers` },
    { k: 'nfs', lbl: count('NFS mounts', nfs.length, slowNfs.length, 'retrying'), tip: 'the file servers each node mounts, and how quickly they answer' },
    { k: 'disk', lbl: count('local disks', disk.length, full.length, 'full'), tip: 'each node’s own filesystems: space and file slots' },
    { k: 'accounts', lbl: count('accounts', accounts.length, held.length, 'held back'), tip: 'each account’s CPU, memory and processes against its limits' },
    { k: 'stuck', lbl: count('stuck', stuckAll, 0, ''), tip: 'processes in D state at the newest snapshot, and what they wait in' },
  ];

  const nodeCols = NODE_COLS.filter((c) => hosts.some((h) => c.get(h) != null));
  const cut = <T,>(rows: T[]) => (all ? rows : rows.slice(0, ROWS));
  const more = (n: number) =>
    n > ROWS && (
      <button className="linkbtn" style={{ marginTop: 8 }} onClick={() => setAll(!all)}>
        {all ? `show the first ${ROWS}` : `show all ${fint(n)}`}
      </button>
    );

  const table = (() => {
    if (view === 'nodes') {
      const s = sortOf('nh-nodes', 'host', 'asc');
      const rows = s.sort(hosts, (h, k) => (k === 'host' ? h.host : nodeCols.find((c) => c.k === k)?.get(h) ?? null));
      // nine columns: the plain table's spacing, so a panel holds them whole
      return (
        <table>
          <thead>
            <tr>
              <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
              {nodeCols.map((c) => (
                <SortTh key={c.k} s={s} k={c.k} num tip={c.tip}>{c.lbl}</SortTh>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => (
              <tr key={h.host}>
                <td className="mono" data-tip={h.synthetic ? 'written by the demo, not measured' : undefined}>
                  {h.host}
                  <div className="nm" style={{ fontSize: 11.5 }} data-tip={h.ts ?? undefined}>
                    {h.epoch != null ? `snapshot ${ago((now - h.epoch) * 1000)}` : 'snapshot time unknown'}
                  </div>
                </td>
                {nodeCols.map((c) => (
                  <td key={c.k} className="num" data-tip={c.k === 'd_state_procs' ? stuckTip(h, frames.get(h.host)) : undefined}>
                    {c.show(h)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
    }
    if (view === 'nfs') {
      if (!nfs.length) return <NoRows what="no NFS mount on these nodes" />;
      const s = sortOf('nh-nfs', 'host', 'asc');
      const retr = (m: NodeMount) => (m.rd_retrans == null && m.wr_retrans == null ? null : (m.rd_retrans ?? 0) + (m.wr_retrans ?? 0));
      const get = (m: NodeMount, k: string) =>
        k === 'host' ? `${m.host} ${m.mount}` : k === 'mount' ? m.mount : k === 'retrans' ? retr(m) : num((m as unknown as Record<string, unknown>)[k]);
      const rows = s.sort(nfs, get);
      return (
        <>
          <table className="roomy">
            <thead>
              <tr>
                <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
                <SortTh s={s} k="mount" first="asc">mount</SortTh>
                <SortTh s={s} k="rd_rtt_ms" num tip={GLOSS.nfsrtt}>read round trip</SortTh>
                <SortTh s={s} k="wr_rtt_ms" num tip={GLOSS.nfsrtt}>write round trip</SortTh>
                <SortTh s={s} k="retrans" num tip={GLOSS.retrans}>retransmissions</SortTh>
                <SortTh s={s} k="ops_s" num tip="NFS operations per second, in the collector’s short sample">operations / s</SortTh>
                <SortTh s={s} k="bad_xid" num tip={GLOSS.badxid}>bad replies</SortTh>
                <SortTh s={s} k="backlog_u" num tip={GLOSS.backlog}>backlog</SortTh>
              </tr>
            </thead>
            <tbody>
              {cut(rows).map((m) => (
                <tr key={m.host + m.mount}>
                  <td className="mono">{m.host}</td>
                  <td className="mono" data-tip={m.fstype ?? undefined}>{m.mount}</td>
                  <td className="num">{m.rd_rtt_ms == null ? <span className="nm" data-tip="no read in the sample">{DASH}</span> : ms(m.rd_rtt_ms)}</td>
                  <td className="num">{m.wr_rtt_ms == null ? <span className="nm" data-tip="no write in the sample">{DASH}</span> : ms(m.wr_rtt_ms)}</td>
                  <td className="num">{(retr(m) ?? 0) > 0 ? <b>{fint(retr(m))}</b> : fint(retr(m))}</td>
                  <td className="num">{fmt(m.ops_s)}</td>
                  <td className="num">{fint(m.bad_xid)}</td>
                  <td className="num">{fmt(m.backlog_u)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {more(rows.length)}
        </>
      );
    }
    if (view === 'disk') {
      if (!disk.length) return <NoRows what="no local filesystem reported" />;
      const s = sortOf('nh-disk', 'host', 'asc');
      const get = (d: NodeDisk, k: string) =>
        k === 'host' ? `${d.host} ${d.mountpoint}` : k === 'mountpoint' ? d.mountpoint : num((d as unknown as Record<string, unknown>)[k]);
      const rows = s.sort(disk, get);
      const th = n.thresholds;
      return (
        <>
          <table className="roomy shares">
            <thead>
              <tr>
                <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
                <SortTh s={s} k="mountpoint" first="asc">filesystem</SortTh>
                <SortTh s={s} k="total_gb" num>size</SortTh>
                <SortTh s={s} k="used_pct" tip={th ? `flagged full at ${th.disk_full_pct}%` : undefined}>space used</SortTh>
                <SortTh s={s} k="inode_used_pct" tip={`${GLOSS.inodes}${th ? `; flagged full at ${th.inode_full_pct}%` : ''}`}>file slots used</SortTh>
              </tr>
            </thead>
            <tbody>
              {cut(rows).map((d) => (
                <tr key={d.host + d.mountpoint}>
                  <td className="mono">{d.host}</td>
                  <td className="mono" data-tip={d.fstype ?? undefined}>
                    {d.mountpoint}
                    {d.full && <span className="pill stale" style={{ marginLeft: 6 }}>full</span>}
                  </td>
                  <td className="num">{d.total_gb == null ? DASH : `${fmt(d.total_gb)} GB`}</td>
                  <td><ShareCell pct={d.used_pct} c={d.full && (d.used_pct ?? 0) >= (th?.disk_full_pct ?? 90) ? 'var(--warn)' : 'var(--sg)'} /></td>
                  <td><ShareCell pct={d.inode_used_pct} c={d.full && (d.inode_used_pct ?? 0) >= (th?.inode_full_pct ?? 90) ? 'var(--warn)' : 'var(--sg)'} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          {more(rows.length)}
        </>
      );
    }
    if (view === 'accounts') {
      if (!accounts.length) {
        return <NoRows what={`no account’s cgroup was read${n.privilege ? ': ' + n.privilege : ''}`} />;
      }
      const s = sortOf('nh-acct', 'host', 'asc');
      const get = (u: NodeUser & { host: string }, k: string) =>
        k === 'host' ? `${u.host} ${u.user}` : k === 'user' ? u.user : num((u as unknown as Record<string, unknown>)[k]);
      const rows = s.sort(accounts, get);
      return (
        <>
          <table className="roomy">
            <thead>
              <tr>
                <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
                <SortTh s={s} k="user" first="asc">account</SortTh>
                <SortTh s={s} k="cpu_cores" num tip={GLOSS.cgroupcpu}>CPU in use</SortTh>
                <SortTh s={s} k="cpu_quota_cores" num tip={GLOSS.quota}>CPU limit</SortTh>
                <SortTh s={s} k="throttled_delta_s" num tip={GLOSS.throttled}>held back</SortTh>
                <SortTh s={s} k="mem_gb" num tip="memory the account’s processes hold now">memory</SortTh>
                <SortTh s={s} k="pids" num tip={GLOSS.pids}>processes</SortTh>
              </tr>
            </thead>
            <tbody>
              {cut(rows).map((u) => (
                <tr key={u.host + u.user}>
                  <td className="mono">{u.host}</td>
                  <td className="mono">{u.user}</td>
                  <td className="num">
                    {u.counter_reset ? (
                      <span className="nm" data-tip="the account’s cgroup was made again between the snapshots: its CPU over the gap cannot be told">restarted</span>
                    ) : u.cpu_cores == null ? (
                      <span className="nm" data-tip="one snapshot only: a rate needs two">{DASH}</span>
                    ) : `${fmt(u.cpu_cores)} cores`}
                  </td>
                  <td className="num">{u.cpu_quota_cores == null ? <span className="nm" data-tip="no limit set">{DASH}</span> : `${fmt(u.cpu_quota_cores)} cores`}</td>
                  <td className="num">{(u.throttled_delta_s ?? 0) > 0 ? <b data-tip={`over a ${fmt(u.gap_s)} s gap`}>{fmt(u.throttled_delta_s)} s</b> : u.throttled_delta_s == null ? DASH : '0 s'}</td>
                  <td className="num">{u.mem_gb == null ? DASH : `${fmt(u.mem_gb)} GB`}</td>
                  <td className="num">{u.pids == null ? DASH : `${fint(u.pids)}${u.pids_max ? ` of ${fint(u.pids_max)}` : ''}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {more(rows.length)}
        </>
      );
    }
    if (!stuck.length) {
      return <NoRows what={stuckAll ? 'the snapshot counted processes in D state but listed none' : 'no process was stuck in D state at the newest snapshot'} />;
    }
    const s = sortOf('nh-stuck', 'host', 'asc');
    const get = (d: NodeDState & { host: string }, k: string) =>
      k === 'host' ? `${d.host} ${String(d.pid).padStart(9, '0')}` : k === 'user' ? d.user : k === 'wchan' ? d.wchan : null;
    const rows = s.sort(stuck, get);
    return (
      <>
        <table className="roomy">
          <thead>
            <tr>
              <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh>
              <th className="num">pid</th>
              <SortTh s={s} k="user" first="asc">account</SortTh>
              <SortTh s={s} k="wchan" first="asc" tip={GLOSS.wchan}>waiting in</SortTh>
              <th>command</th>
            </tr>
          </thead>
          <tbody>
            {cut(rows).map((d) => (
              <tr key={`${d.host}:${d.pid}`}>
                <td className="mono">{d.host}</td>
                <td className="num mono">{d.pid ?? DASH}</td>
                <td className="mono">{d.user ?? DASH}</td>
                <td className="mono" data-tip={d.kstack?.length ? `kernel stack:\n${d.kstack.join('\n')}` : undefined}>{d.wchan || DASH}</td>
                <CmdCell text={d.args} />
              </tr>
            ))}
          </tbody>
        </table>
        {more(rows.length)}
        {stuckAll > stuck.length && <div className="nm" style={{ marginTop: 6, fontSize: 12.5 }}>{fint(stuckAll - stuck.length)} more counted, not listed</div>}
      </>
    );
  })();

  return (
    <Panel
      title={`${nodes.One} health`}
      id="sec-node-health"
      ask={[{ panel: 'node', keys: ['hosts', 'nfs', 'disk', 'thresholds'] }]}
      p={n}
      span
      empty={!hosts.length}
      emptyWhat={n._error ? `the node snapshot could not be read: ${n._error}` : `no ${nodes.one} has written a snapshot yet`}
      header={
        <span className="wrapctl" style={{ gap: 6 }}>
          <Term tip={`each ${nodes.one}’s newest snapshot: now, not over the window`}>
            <span className="nm">now</span>
          </Term>
          <Segmented value={view} onChange={(v) => { setView(v); setAll(false); }} options={options} wrap />
        </span>
      }
    >
      <div className="scroller">{table}</div>
    </Panel>
  );
}

/** The D-state cell's hover: what the stuck processes wait in, by the node
 *  tier's wait channels and the eBPF tier's kernel frames. */
function stuckTip(h: NodeHost, frames: Record<string, number> | null | undefined): string | undefined {
  const lines: string[] = [];
  if (h.zombie) lines.push(`zombies: ${fint(h.zombie)} (finished, not yet collected by their parent)`);
  const by = new Map<string, number>();
  for (const d of h.d_state ?? []) by.set(d.wchan || '-', (by.get(d.wchan || '-') ?? 0) + 1);
  if (by.size) {
    lines.push('waiting in (wait channel):');
    for (const [w, n] of Array.from(by).sort((a, b) => b[1] - a[1]).slice(0, 6)) lines.push(`  ${w} ×${n}`);
  }
  const fr = Object.entries(frames ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
  if (fr.length) {
    lines.push('blocked in (kernel frame, tracked processes):');
    for (const [f, n] of fr) lines.push(`  ${f} ×${n}`);
  }
  return lines.length ? lines.join('\n') : undefined;
}
