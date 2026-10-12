/* Active users: one row per account, opened with a click to see what it runs.
 *
 * Built for a node with thousands of accounts. The live frame carries only the
 * busiest 100 (`live.user_rows`); the table holds all of them and scrolls, its
 * header in view; and a search goes to the server once the frame does not hold
 * everyone, so it reaches every account and not just the busiest. Opening a
 * user fetches its sessions and its last processes on demand, so a closed row
 * costs nothing.
 *
 * Everything a reader would look up about an account sits on its row or in its
 * drill-down (roadmap note 8): what it runs now and what that costs, what it
 * ran in the window by agent, and its last processes -- the exit stream that
 * used to be its own panel (note 18).
 */
import { Fragment, useEffect, useMemo, useState } from 'react';
import { getLiveUser, getLiveUsers } from '../api/client';
import type { Cascades, Cls, LiveEvent, LiveUserDetail, LiveUserRow, LiveUserSession, LiveUsers, UsageBlock } from '../api/types';
import { CmdCell } from '../components/Cmd';
import { NoRows, Panel } from '../components/Panel';
import { SortTh } from '../components/SortTh';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { serverNow } from '../lib/clock';
import { UNTAGGED } from '../lib/filters';
import { DASH, ago, dur, fint, fmt, mb } from '../lib/format';
import { GLOSS } from '../lib/glossary';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { pcol } from '../lib/purposes';
import { AP_LABEL } from '../lib/sandbox';
import { useSort } from '../lib/useSort';

const typeLbl = (t: string) => AGENT_TYPE_LBL[t] ?? t;

/* ------------------------------------------------------------- columns */

/* The table's columns, each switchable (roadmap note 3). The defaults come
 * from the service's config (`live.user_columns`) when it names them, and a
 * reader's own choice is kept in this browser. */
type ColKey = 'agents' | 'ides' | 'running' | 'exited' | 'cpu' | 'read' | 'written' | 'sent' | 'received' | 'gpu' | 'last';
const COLS: { k: ColKey; lbl: string; tip?: string; num?: boolean }[] = [
  { k: 'agents', lbl: 'agents running', tip: 'agent sessions running now, by agent' },
  { k: 'ides', lbl: 'IDEs running', tip: 'editor sessions running now' },
  { k: 'running', lbl: 'running', tip: GLOSS.running, num: true },
  { k: 'exited', lbl: 'exited', tip: GLOSS.exited + ' in the window, split by class', num: true },
  { k: 'cpu', lbl: 'CPU time', tip: 'CPU used by the processes that exited in the window', num: true },
  { k: 'read', lbl: 'disk read', tip: GLOSS.diskbytes + ', by the processes that exited in the window', num: true },
  { k: 'written', lbl: 'disk written', tip: GLOSS.diskbytes + ', by the processes that exited in the window', num: true },
  { k: 'sent', lbl: 'network sent', tip: 'bytes sent over the network by the processes that exited in the window', num: true },
  { k: 'received', lbl: 'network received', tip: 'bytes received over the network by the processes that exited in the window', num: true },
  { k: 'gpu', lbl: 'GPU·h asked', tip: GLOSS.gpuh + '. Only what the sbatch / srun command line named; a #SBATCH request in the script is not seen', num: true },
  { k: 'last', lbl: 'last active', num: true },
];
const DEFAULT_COLS: ColKey[] = ['agents', 'ides', 'running', 'exited', 'cpu', 'read', 'written', 'last'];
const COLS_KEY = 'rc.activeUsers.columns';

/** Known column keys only; `net`, the one network column there used to be,
 * is both of the two there are now. */
function knownCols(v: unknown[]): ColKey[] {
  return v
    .flatMap((k) => (k === 'net' ? ['sent', 'received'] : [k]))
    .filter((k): k is ColKey => COLS.some((c) => c.k === k));
}

function loadCols(fallback: ColKey[]): ColKey[] {
  try {
    const v = JSON.parse(localStorage.getItem(COLS_KEY) ?? 'null');
    if (Array.isArray(v)) return knownCols(v);
  } catch {
    /* private window or blocked storage: the defaults */
  }
  return fallback;
}
function saveCols(v: ColKey[]) {
  try {
    localStorage.setItem(COLS_KEY, JSON.stringify(v));
  } catch {
    /* not kept; still applied for this page */
  }
}

function ColumnPicker({ on, setOn, gpuKnown }: { on: ColKey[]; setOn: (v: ColKey[]) => void; gpuKnown: boolean }) {
  return (
    <details className="u-cols-pick">
      <summary className="btn">columns</summary>
      <div className="u-cols-menu">
        {COLS.filter((c) => c.k !== 'gpu' || gpuKnown).map((c) => (
          <label key={c.k}>
            <input
              type="checkbox"
              checked={on.includes(c.k)}
              onChange={(e) => {
                const next = e.target.checked ? COLS.map((x) => x.k).filter((k) => k === c.k || on.includes(k)) : on.filter((k) => k !== c.k);
                setOn(next);
                saveCols(next);
              }}
            />
            {c.lbl}
          </label>
        ))}
      </div>
    </details>
  );
}

/* ---------------------------------------------------------------- cells */

/** "Claude Code 3 · Codex 1": a class's running sessions, by type. */
function Running({ m }: { m: Record<string, number> | undefined }) {
  const e = Object.entries(m ?? {}).sort((a, b) => b[1] - a[1]);
  if (!e.length) return <span className="nm">{DASH}</span>;
  return (
    <span className="u-chips">
      {e.map(([t, n]) => (
        <span key={t} className="u-chip">
          {typeLbl(t)} <b>{fint(n)}</b>
        </span>
      ))}
    </span>
  );
}

/** Processes exited, split by class: the bar is the account's own mix. */
function Mix({ r, cls }: { r: LiveUserRow; cls: Cls[] }) {
  const parts = cls.map((c) => [c, r.by_class[c]?.n ?? 0] as const).filter(([, n]) => n > 0);
  const tot = parts.reduce((a, [, n]) => a + n, 0);
  if (!tot) return <span className="nm">{DASH}</span>;
  return (
    <span className="u-mix" data-tip={parts.map(([c, n]) => `${CLS_LBL[c]} ${fint(n)}`).join('\n')}>
      <span className="u-bar">
        {parts.map(([c, n]) => (
          <i key={c} style={{ width: `${(100 * n) / tot}%`, background: CLS_COL[c] }} />
        ))}
      </span>
      <b>{fint(tot)}</b>
    </span>
  );
}

/* The roster at a glance: how many people have each agent or IDE running. A
 * chip is also a filter -- click it to list just those people. */
function Summary({ counts, cls, run, setRun }: {
  counts: Partial<Record<string, Record<string, number>>>;
  cls: Cls[];
  run: string | null;
  setRun: (r: string | null) => void;
}) {
  const chips = (['agent', 'ide'] as Cls[])
    .filter((c) => cls.includes(c))
    .flatMap((c) =>
      Object.entries(counts[c] ?? {})
        .sort((a, b) => b[1] - a[1])
        .map(([t, n]) => ({ c, t, n, k: `${c}:${t}` })),
    );
  if (!chips.length) return null;
  return (
    <div className="u-summary">
      <span className="eyebrow">running now</span>
      {chips.map((x) => (
        <button
          key={x.k}
          className="u-pick"
          aria-pressed={run === x.k}
          onClick={() => setRun(run === x.k ? null : x.k)}
          data-tip={run === x.k ? 'show everyone again' : `list only the users with ${typeLbl(x.t)} running`}
        >
          <i className="u-dot" style={{ background: CLS_COL[x.c] }} />
          {typeLbl(x.t)} <b>{fint(x.n)}</b> {x.n === 1 ? 'user' : 'users'}
        </button>
      ))}
    </div>
  );
}

const sumCls = (r: LiveUserRow, cls: Cls[], f: (b: UsageBlock) => number | undefined) =>
  cls.reduce((a, c) => a + (r.by_class[c] ? f(r.by_class[c]!) ?? 0 : 0), 0);
const runningN = (r: LiveUserRow, cls: Cls[]) => cls.reduce((a, c) => a + (r.running?.[c] ?? 0), 0);
const inClasses = (r: LiveUserRow, cls: Cls[]) => cls.some((c) => r.by_class[c] || r.live[c]);

/* -------------------------------------------------- Claude Code runs Codex */

/** child agent type -> parent agent type -> processes, for one account: the
 * agents another agent started (roadmap note 4), from the Agents tab's trees. */
function nestedAgents(c: Cascades | undefined, user: string): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  for (const t of c?.trees ?? []) {
    const byPid = new Map(t.nodes.map((n) => [n.pid, n]));
    for (const n of t.nodes) {
      if (n.role !== 'agent' || n.user !== user || !n.agent_type) continue;
      let p = n.parent != null ? byPid.get(n.parent) : undefined;
      while (p && p.role !== 'agent') p = p.parent != null ? byPid.get(p.parent) : undefined;
      if (!p?.agent_type) continue;
      const d = (out[n.agent_type] ??= {});
      d[p.agent_type] = (d[p.agent_type] ?? 0) + (n.tool_calls ?? 0);
    }
  }
  return out;
}

/* ------------------------------------------------------------ drill-down */

/** The launch, as badges on the session (roadmap note 12). */
function Badges({ s }: { s: Pick<LiveUserSession, 'actor3' | 'approval' | 'sandbox' | 'headless'> }) {
  const out: [string, string, string][] = [];
  if (s.actor3 === 'agent') {
    if (s.approval === 'bypassed') out.push(['bypass', 'crit', AP_LABEL.bypassed + ': ' + GLOSS.bypass]);
    else if (s.approval === 'auto') out.push(['auto mode', 'warn', GLOSS.automode]);
  }
  // Only the states worth noticing: on a host where no sandbox works, a "not
  // sandboxed" on every session is noise, and "asks first" is the default.
  if (s.sandbox === 'sandboxed') out.push(['sandboxed', 'ok', GLOSS.sandbox]);
  if (s.headless) out.push(['headless', 'nm', GLOSS.headless]);
  if (!out.length) return null;
  return (
    <span className="u-badges">
      {out.map(([t, k, tip]) => (
        <span key={t} className={`u-badge ${k}`} data-tip={tip}>{t}</span>
      ))}
    </span>
  );
}

function Usage({ b }: { b: UsageBlock | undefined }) {
  return (
    <>
      <td className="num">{b ? fint(b.n) : DASH}</td>
      <td className="num">{b && b.cpu_s ? dur(b.cpu_s) : DASH}</td>
      <td className="num">{b?.rd_mb ? mb(b.rd_mb) : DASH}</td>
      <td className="num">{b?.wr_mb ? mb(b.wr_mb) : DASH}</td>
      <td className="num">{b?.net_tx_mb ? mb(b.net_tx_mb) : DASH}</td>
      <td className="num">{b?.net_rx_mb ? mb(b.net_rx_mb) : DASH}</td>
    </>
  );
}

function Detail({ name, windowMin, windowLbl, cls, cascades, openTraj }: {
  name: string;
  windowMin: number | null;
  windowLbl: string;
  cls: Cls[];
  cascades: Cascades | undefined;
  openTraj: (sid: string) => void;
}) {
  const [n, setN] = useState(25);
  const [host, setHost] = useState<string>('');
  const [d, setD] = useState<LiveUserDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    setErr(null);
    getLiveUser(name, windowMin, ac.signal, n)
      .then(setD)
      .catch((e) => !ac.signal.aborted && setErr(String(e)));
    return () => ac.abort();
  }, [name, windowMin, n]);
  const nested = useMemo(() => nestedAgents(cascades, name), [cascades, name]);
  // both boxes sort by any column (roadmap note 27); before the early returns,
  // as hooks must be
  const sessSort = useSort<string>();
  const winSort = useSort<string>();
  if (err) return <div className="nm">could not load {name}: {err}</div>;
  if (!d) return <div className="nm">loading {name}…</div>;
  const row = d.row;
  const sessions = sessSort.sort(
    d.sessions.filter((s) => cls.includes((s.actor3 ?? 'agent') as Cls)),
    (s, k) => k === 'session' ? typeLbl(s.agent_type ?? '') : k === 'host' ? s.host : k === 'age' ? s.age_s
      : k === 'procs' ? s.n_procs : k === 'mem' ? s.rss_mb : s.cpu_s,
  );
  const usage = (b: UsageBlock | undefined, k: string) =>
    !b ? null : k === 'who' ? null : (b as unknown as Record<string, number | undefined>)[k] ?? null;
  const all = d.recent.filter((e) => !e.actor3 || cls.includes(e.actor3 as Cls));
  const hosts = Array.from(new Set(all.map((e) => e.host).filter((h): h is string => !!h))).sort();
  const recent = host ? all.filter((e) => e.host === host) : all;
  const multiHost = hosts.length > 1 || new Set(sessions.map((s) => s.host)).size > 1;
  const now = serverNow();
  return (
    <div className="u-detail">
      {/* Two boxes, never one wide table: what runs NOW and what ran in the
          WINDOW are different spans of different things (roadmap note 6). */}
      <section className="u-box now">
        <h4>
          running now <span className="nm">· sessions seen in the last 10 min</span>
        </h4>
        {sessions.length ? (
          <div className="scroller">
            <table className="roomy">
              <thead>
                <tr>
                  <SortTh s={sessSort} k="session" first="asc">session</SortTh>
                  {multiHost && <SortTh s={sessSort} k="host" first="asc">host</SortTh>}
                  <SortTh s={sessSort} k="age" num>running for</SortTh>
                  <SortTh s={sessSort} k="procs" num tip={GLOSS.running}>running</SortTh>
                  <SortTh s={sessSort} k="mem" num tip="resident memory now, and the most and the average over every 60-second census of this session">memory now · peak · avg</SortTh>
                  <SortTh s={sessSort} k="cpu" num>CPU time</SortTh>
                  <th />
                </tr>
              </thead>
              <tbody>
                {sessions.map((s, i) => (
                  <Fragment key={i}>
                  <tr>
                    <td>
                      <span className="u-sess">
                        <i className="u-dot" style={{ background: CLS_COL[s.actor3 ?? 'agent'] }} />
                        <span>
                          {typeLbl(s.agent_type ?? '')}
                          {/* the version under the agent's name, where its
                              program's path names it, else nothing (note 13) */}
                          {s.version && <span className="u-ver" data-tip={GLOSS.version}>v{s.version}</span>}
                        </span>
                        <Badges s={s} />
                        {s.no_census && (
                          <span className="nm" data-tip="seen running in the last 10 min, but its process tree sent no 60-second census, so what it holds is not known">
                            no census
                          </span>
                        )}
                      </span>
                    </td>
                    {multiHost && <td className="mono nm">{s.host}</td>}
                    <td className="num">{dur(s.age_s)}</td>
                    <td className="num">{fint(s.n_procs)}</td>
                    <td className="num">
                      {mb(s.rss_mb)}
                      <span className="sub"> · {s.rss_peak_mb != null ? mb(s.rss_peak_mb) : DASH} · {s.rss_mean_mb != null ? mb(s.rss_mean_mb) : DASH}</span>
                    </td>
                    <td className="num">{dur(s.cpu_s)}</td>
                    <td>
                      {s.sid && (
                        <button className="linkbtn" onClick={() => openTraj(s.sid!)}
                          data-tip="open this session's process tree on the Trajectories tab">
                          trajectory →
                        </button>
                      )}
                    </td>
                  </tr>
                  {/* each agent running inside this session's tree, on a row
                      of its own with its own launch (roadmap notes 4 and 12) */}
                  {(s.agents ?? []).map((a) => (
                    <tr key={`${i}:${a.pid}`} className="u-sub">
                      <td colSpan={multiHost ? 7 : 6}>
                        <span className="u-sess">
                          <span
                            data-tip={`an agent running inside this session's process tree, with its own launch flags · pid ${a.pid}`}
                          >
                            {typeLbl(a.agent_type ?? '')}
                            {a.version && <span className="u-ver" data-tip={GLOSS.version}>v{a.version}</span>}
                          </span>
                          <span className="nm">
                            {s.actor3 === 'agent' ? 'started by' : 'started from'} {typeLbl(s.agent_type ?? '')}
                          </span>
                          <Badges s={{ actor3: 'agent', approval: a.approval, sandbox: a.sandbox, headless: null }} />
                        </span>
                      </td>
                    </tr>
                  ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <NoRows what="no agent or IDE session running" />
        )}
      </section>

      <section className="u-box window">
        <h4>
          in the {windowLbl} <span className="nm">· processes that exited, by class and by agent</span>
        </h4>
        {row && cls.some((c) => row.by_class[c]) ? (
          <div className="scroller">
            <table className="roomy">
              <thead>
                <tr>
                  <SortTh s={winSort} k="who" first="asc">who ran it</SortTh>
                  <SortTh s={winSort} k="n" num tip={GLOSS.exited}>exited</SortTh>
                  <SortTh s={winSort} k="cpu_s" num>CPU time</SortTh>
                  <SortTh s={winSort} k="rd_mb" num tip={GLOSS.diskbytes}>disk read</SortTh>
                  <SortTh s={winSort} k="wr_mb" num tip={GLOSS.diskbytes}>disk written</SortTh>
                  <SortTh s={winSort} k="net_tx_mb" num tip="bytes sent over the network">network sent</SortTh>
                  <SortTh s={winSort} k="net_rx_mb" num tip="bytes received over the network">network received</SortTh>
                </tr>
              </thead>
              <tbody>
                {/* classes sort by the column, and each class's agent types
                    under it by the same one: a sort never breaks the nesting */}
                {winSort.sort(cls.filter((c) => row.by_class[c]),
                  (c, k) => (k === 'who' ? CLS_LBL[c] : usage(row.by_class[c], k))).flatMap((c) => {
                  const types = winSort.key
                    ? winSort.sort(Object.entries(row.type_detail?.[c] ?? {}),
                        ([t, b], k) => (k === 'who' ? typeLbl(t) : usage(b, k)))
                    : Object.entries(row.type_detail?.[c] ?? {}).sort((a, b) => b[1].n - a[1].n);
                  return [
                    <tr key={c} className="u-cls">
                      <td>
                        <i className="u-dot" style={{ background: CLS_COL[c] }} />
                        {CLS_LBL[c]}
                      </td>
                      <Usage b={row.by_class[c]} />
                    </tr>,
                    // one nested row per agent (roadmap note 5)
                    ...types.flatMap(([t, b]) => {
                      const by = nested[t] ?? {};
                      const under = Object.entries(by).filter(([, k]) => k > 0).sort((a, b2) => b2[1] - a[1]);
                      const nestedN = under.reduce((a, [, k]) => a + k, 0);
                      return [
                        <tr key={c + t} className="u-sub">
                          <td>{typeLbl(t)}</td>
                          <Usage b={b} />
                        </tr>,
                        // an agent another agent started, split out (roadmap note 4)
                        ...under.map(([p, k]) => (
                          <tr key={c + t + p} className="u-sub2">
                            <td data-tip={`${typeLbl(t)} processes in sessions that ${typeLbl(p)} started, from the Agents tab's trees`}>
                              started by {typeLbl(p)}
                            </td>
                            <td className="num">{fint(k)}</td>
                            <td colSpan={4} />
                          </tr>
                        )),
                        ...(under.length && b.n > nestedN
                          ? [
                              <tr key={c + t + '-own'} className="u-sub2">
                                <td>on its own</td>
                                <td className="num">{fint(b.n - nestedN)}</td>
                                <td colSpan={4} />
                              </tr>,
                            ]
                          : []),
                      ];
                    }),
                  ];
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <NoRows what="ran nothing in the window" />
        )}
      </section>

      <section className="u-box cmds">
        <h4>
          last processes exited <span className="nm">· however long ago, newest first</span>
        </h4>
        {/* the last N, not "within the window" (roadmap note 9), on any host (note 17) */}
        <div className="u-ctl">
          <span className="seg" role="group" aria-label="how many">
            {[10, 25, 100].map((k) => (
              <button key={k} aria-pressed={n === k} onClick={() => setN(k)}>last {k}</button>
            ))}
          </span>
          {hosts.length > 1 && (
            <select className="wininput" value={host} onChange={(e) => setHost(e.target.value)} aria-label="host">
              <option value="">every host</option>
              {hosts.map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          )}
          {d.recent_held != null && (
            <span className="nm">{fint(d.recent_held)} held for this user (at most {fint(d.recent_depth)})</span>
          )}
        </div>
        {recent.length ? <RecentTable rows={recent} multiHost={hosts.length > 1} now={now} /> : (
          <NoRows what="no exited process held for this user" />
        )}
      </section>
    </div>
  );
}

function RecentTable({ rows, multiHost, now }: { rows: LiveEvent[]; multiHost: boolean; now: number }) {
  const s = useSort<string>();
  const val = (e: LiveEvent, k: string) =>
    k === 'at' ? e.epoch : k === 'host' ? e.host : k === 'agent' ? e.agent_type ?? e.actor3
    : k === 'cmd' ? e.comm ?? e.tool : k === 'ran' ? e.duration_s : k === 'cpu' ? e.cpu_s
    : k === 'rd' ? e.rd_mb : k === 'wr' ? e.wr_mb : k === 'tx' ? e.net_tx_mb : k === 'rx' ? e.net_rx_mb
    : e.signal != null ? 1000 + e.signal : e.exit_code;
  return (
    <div className="scroller">
      <table className="roomy u-recent">
        <thead>
          <tr>
            <SortTh s={s} k="at">at</SortTh>
            {multiHost && <SortTh s={s} k="host" first="asc">host</SortTh>}
            <SortTh s={s} k="agent" first="asc">agent</SortTh>
            <SortTh s={s} k="cmd" first="asc">command</SortTh>
            <SortTh s={s} k="ran" num tip={GLOSS.lifetime}>ran</SortTh>
            <SortTh s={s} k="cpu" num>CPU</SortTh>
            <SortTh s={s} k="rd" num tip={GLOSS.diskbytes}>read</SortTh>
            <SortTh s={s} k="wr" num tip={GLOSS.diskbytes}>written</SortTh>
            <SortTh s={s} k="tx" num tip="bytes sent over the network">sent</SortTh>
            <SortTh s={s} k="rx" num tip="bytes received over the network">received</SortTh>
            <SortTh s={s} k="exit" num tip={GLOSS.exitcode}>exit code</SortTh>
          </tr>
        </thead>
        <tbody>
          {s.sort(rows, val).map((e, i) => {
            const failed = (e.exit_code != null && e.exit_code !== 0) || e.signal != null;
            return (
              <tr key={i}>
                <td className="mono nm" data-tip={e.epoch ? ago(now - e.epoch * 1000) + ' ago' : undefined}>{e.ts?.slice(5) ?? DASH}</td>
                {multiHost && <td className="mono nm">{e.host ?? DASH}</td>}
                <td>
                  <i className="u-dot" style={{ background: CLS_COL[e.actor3 ?? 'agent'] }} />
                  {e.agent_type ? typeLbl(e.agent_type) : CLS_LBL[e.actor3 ?? ''] ?? DASH}
                </td>
                <CmdCell text={e.args || e.comm || e.tool} className="u-cmd">
                  <span style={{ color: pcol(e.purpose) }}>{e.comm ?? e.tool ?? DASH}</span>{' '}
                  {(e.args ?? '').replace(/^\S+\s?/, '')}
                </CmdCell>
                <td className="num">{dur(e.duration_s)}</td>
                <td className="num">{e.cpu_s != null ? dur(e.cpu_s) : DASH}</td>
                <td className="num">{e.rd_mb ? mb(e.rd_mb) : DASH}</td>
                <td className="num">{e.wr_mb ? mb(e.wr_mb) : DASH}</td>
                <td className="num">{e.net_tx_mb ? mb(e.net_tx_mb) : DASH}</td>
                <td className="num">{e.net_rx_mb ? mb(e.net_rx_mb) : DASH}</td>
                <td className="num" style={failed ? { color: 'var(--crit)' } : undefined}>
                  {e.signal != null ? `signal ${e.signal}` : e.exit_code ?? DASH}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------ the table */

/** The page-wide user and agent-type filter, applied to one account: the
 * same rule the service applies to a filtered view's records, at the grain of
 * a row. A type keeps an account that ran it or has it running; `none`
 * (untagged) keeps one with processes no agent type was put on. */
function pageMatch(r: LiveUserRow, users: string[], types: string[]): boolean {
  if (users.length && !users.includes(r.user)) return false;
  if (!types.length) return true;
  const cs = new Set([...Object.keys(r.by_class), ...Object.keys(r.live)]);
  return types.some((t) =>
    [...cs].some((c) =>
      t === UNTAGGED
        ? (r.by_class[c]?.n ?? 0) > Object.values(r.types[c] ?? {}).reduce((a, b) => a + b, 0)
        : (r.types[c]?.[t] ?? 0) > 0 || (r.live[c]?.[t] ?? 0) > 0,
    ),
  );
}

interface UsersProps {
  U: LiveUsers | undefined;
  cls: Cls[];
  windowMin: number | null;
  windowLbl: string;
  P: Panels;
  /** the service's default columns (`live.user_columns`), when it names them */
  defaultCols?: string[] | null;
  pageUsers?: string[];
  pageTypes?: string[];
  /** open a session's trajectory, or a user's sessions, on the Trajectories tab */
  openTraj: (sid: string) => void;
  openUserTraj: (user: string) => void;
}

export function UsersPanel({
  U,
  cls,
  windowMin,
  windowLbl,
  P,
  defaultCols,
  pageUsers = [],
  pageTypes = [],
  openTraj,
  openUserTraj,
}: UsersProps) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [remote, setRemote] = useState<LiveUsers | null>(null);
  const [run, setRun] = useState<string | null>(null);
  const fallback = knownCols(defaultCols ?? []);
  const [cols, setCols] = useState<ColKey[]>(() => loadCols(fallback.length ? fallback : DEFAULT_COLS));
  const sort = useSort<ColKey | 'user'>();
  const cascades = pick<Cascades>(P, 'cascades', 'ebpfm');

  // the frame holds the busiest rows only: past them, the server searches
  const partial = !!U && U.rows_total > U.rows.length;
  const pageOn = pageUsers.length + pageTypes.length > 0;
  useEffect(() => {
    setRemote(null);
    if ((!q && !run && !pageOn) || !partial) return;
    const ac = new AbortController();
    const t = setTimeout(() => {
      getLiveUsers(q, windowMin, ac.signal, run).then(setRemote).catch(() => {});
    }, 250);
    return () => {
      clearTimeout(t);
      ac.abort();
    };
  }, [q, run, pageOn, partial, windowMin]);

  const [runC, runT] = run ? run.split(':') : [null, null];
  const base =
    remote?.rows ??
    (U?.rows ?? []).filter(
      (r) =>
        (!q || r.user.toLowerCase().includes(q.toLowerCase())) &&
        (!runC || ((r.live[runC] ?? {})[runT ?? ''] ?? 0) > 0),
    );
  const rows = base.filter((r) => inClasses(r, cls) && pageMatch(r, pageUsers, pageTypes));
  // under a page filter, "running now" counts the people it kept
  const counts: Partial<Record<string, Record<string, number>>> = {};
  if (pageOn) {
    for (const r of (remote?.rows ?? U?.rows ?? []).filter((x) => pageMatch(x, pageUsers, pageTypes))) {
      for (const [c, m] of Object.entries(r.live)) {
        for (const t of Object.keys(m ?? {})) {
          const d = (counts[c] ??= {});
          d[t] = (d[t] ?? 0) + 1;
        }
      }
    }
  }
  const gpuKnown = rows.some((r) => r.gpu_h != null && r.gpu_h > 0);
  const shownCols = COLS.filter(
    (c) =>
      cols.includes(c.k) &&
      (c.k !== 'agents' || cls.includes('agent')) &&
      (c.k !== 'ides' || cls.includes('ide')) &&
      (c.k !== 'gpu' || gpuKnown),
  );
  const val = (r: LiveUserRow, k: ColKey | 'user'): number | string | null => {
    switch (k) {
      case 'user': return r.user;
      case 'agents': return Object.values(r.live.agent ?? {}).reduce((a, b) => a + b, 0);
      case 'ides': return Object.values(r.live.ide ?? {}).reduce((a, b) => a + b, 0);
      case 'running': return runningN(r, cls);
      case 'exited': return sumCls(r, cls, (b) => b.n);
      case 'cpu': return sumCls(r, cls, (b) => b.cpu_s);
      case 'read': return sumCls(r, cls, (b) => b.rd_mb);
      case 'written': return sumCls(r, cls, (b) => b.wr_mb);
      case 'sent': return sumCls(r, cls, (b) => b.net_tx_mb);
      case 'received': return sumCls(r, cls, (b) => b.net_rx_mb);
      case 'gpu': return r.gpu_h ?? null;
      case 'last': return r.last_epoch;
    }
  };
  // the service's own order until a header is clicked: agents running, then activity, then name
  const sorted = sort.sort(rows, val);
  const now = serverNow();
  const more = !q && !run && !pageOn && partial ? U!.rows_total - U!.rows.length : 0;

  const cell = (r: LiveUserRow, k: ColKey) => {
    switch (k) {
      case 'agents': return <td key={k}><Running m={r.live.agent} /></td>;
      case 'ides': return <td key={k}><Running m={r.live.ide} /></td>;
      case 'running': return <td key={k} className="num">{runningN(r, cls) ? fint(runningN(r, cls)) : DASH}</td>;
      case 'exited': return <td key={k} className="num"><Mix r={r} cls={cls} /></td>;
      case 'cpu': { const v = sumCls(r, cls, (b) => b.cpu_s); return <td key={k} className="num">{v ? dur(v) : DASH}</td>; }
      case 'read': { const v = sumCls(r, cls, (b) => b.rd_mb); return <td key={k} className="num">{v ? mb(v) : DASH}</td>; }
      case 'written': { const v = sumCls(r, cls, (b) => b.wr_mb); return <td key={k} className="num">{v ? mb(v) : DASH}</td>; }
      case 'sent': { const v = sumCls(r, cls, (b) => b.net_tx_mb); return <td key={k} className="num">{v ? mb(v) : DASH}</td>; }
      case 'received': { const v = sumCls(r, cls, (b) => b.net_rx_mb); return <td key={k} className="num">{v ? mb(v) : DASH}</td>; }
      case 'gpu': return <td key={k} className="num">{r.gpu_h ? `${fmt(r.gpu_h)} GPU·h` : DASH}</td>;
      case 'last': return <td key={k} className="num nm">{r.last_epoch ? ago(now - r.last_epoch * 1000) : DASH}</td>;
    }
  };

  return (
    <div className="u-panel">
      {U && (
        <Summary counts={pageOn ? counts : U.running_by_type ?? {}} cls={cls} run={run} setRun={setRun} />
      )}
      <div className="u-ctl">
        <input
          className="wininput u-search"
          placeholder="find a user"
          value={q}
          onChange={(e) => setQ(e.target.value.trim())}
          aria-label="find a user"
        />
        <ColumnPicker on={cols} setOn={setCols} gpuKnown={gpuKnown} />
        {sort.key && (
          <button className="linkbtn" onClick={() => sort.toggle(sort.key!)} data-tip="the sort in use; click a header to change it">
            sorted by {sort.key === 'user' ? 'user' : COLS.find((c) => c.k === sort.key)?.lbl}
          </button>
        )}
      </div>
      {sorted.length ? (
        <div className="scroller vscroll">
          <table className="roomy users">
            <thead>
              <tr>
                <SortTh s={sort} k="user" first="asc">user</SortTh>
                {shownCols.map((c) => (
                  <SortTh key={c.k} s={sort} k={c.k} num={c.num} tip={c.tip}>{c.lbl}</SortTh>
                ))}
              </tr>
            </thead>
            {sorted.map((r) => {
              const isOpen = open === r.user;
              return (
                <tbody key={r.user}>
                  <tr className={`u-row${isOpen ? ' open' : ''}`} onClick={() => setOpen(isOpen ? null : r.user)}>
                    <td>
                      <span className="u-caret">{isOpen ? '▾' : '▸'}</span>
                      <span className="mono u-name">{r.user}</span>
                      {/* to this user's sessions (roadmap note 7) */}
                      <button
                        className="linkbtn u-go"
                        onClick={(e) => {
                          e.stopPropagation();
                          openUserTraj(r.user);
                        }}
                        data-tip="this user's sessions on the Trajectories tab"
                      >
                        trajectories →
                      </button>
                    </td>
                    {shownCols.map((c) => cell(r, c.k))}
                  </tr>
                  {isOpen && (
                    <tr className="u-open">
                      <td colSpan={shownCols.length + 1}>
                        <Detail name={r.user} windowMin={windowMin} windowLbl={windowLbl} cls={cls}
                          cascades={cascades._present === false ? undefined : cascades} openTraj={openTraj} />
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
          </table>
        </div>
      ) : (
        <NoRows what={q ? `no user matches “${q}”` : 'no user active in the window'} />
      )}
      <div className="u-foot">
        {sorted.length > 0 && (
          <span className="nm">{`${fint(sorted.length)} user${sorted.length === 1 ? '' : 's'}`}</span>
        )}
        {more > 0 && <span className="nm">{fint(more)} quieter users not listed · find them by name</span>}
      </div>
    </div>
  );
}

/** The panel, with an anchor the plate can scroll to. */
export function ActiveUsers(props: UsersProps & { goto?: (tab: string) => void }) {
  const { goto, ...rest } = props;
  return (
    <Panel
      id="live-users"
      title="Active users"
      ask={[{ panel: 'live', keys: ['users', 'now'] }]}
      span
      live
      feed="live"
      empty={!props.U}
      emptyWhat="the service sent no user roster"
      header={
        goto && (
          <button className="btn" onClick={() => goto('events')} data-tip="every process record, filtered and downloadable">
            search & export {'→'}
          </button>
        )
      }
    >
      <UsersPanel {...rest} />
    </Panel>
  );
}

export const scrollToUsers = () =>
  document.getElementById('live-users')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

