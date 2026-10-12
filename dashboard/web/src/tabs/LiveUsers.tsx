/* Active users: one row per account, opened with a click to see what it runs.
 *
 * Built for a node with thousands of accounts. The live frame carries only the
 * busiest 100 (`live.user_rows`); the table holds all of them and scrolls, its
 * header in view; and a search goes to the server once the frame does not hold
 * everyone, so it reaches every account and not just the busiest. Opening a
 * user fetches its sessions and latest commands on demand, so a closed row
 * costs nothing.
 */
import { useEffect, useState } from 'react';
import { getLiveUser, getLiveUsers } from '../api/client';
import type { Cls, LiveUserDetail, LiveUserRow, LiveUsers } from '../api/types';
import { Segmented } from '../components/ClassFilter';
import { CmdCell } from '../components/Cmd';
import { NoRows, Panel } from '../components/Panel';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { serverNow } from '../lib/clock';
import { UNTAGGED } from '../lib/filters';
import { DASH, ago, dur, fint, mb } from '../lib/format';
import { AP_LABEL, label } from '../lib/sandbox';
import { pcol } from '../lib/purposes';

type Sort = 'agents' | 'active' | 'name';

const typeLbl = (t: string) => AGENT_TYPE_LBL[t] ?? t;

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

/** Processes run, split by class: the bar is the account's own mix. */
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

/* The roster at a glance, which at hundreds of users matters more than any
 * one row: how many people have each agent or IDE running. A chip is also a
 * filter -- click it to list just those people. */
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

const shownN = (r: LiveUserRow, cls: Cls[]) => cls.reduce((a, c) => a + (r.by_class[c]?.n ?? 0), 0);
const shownCpu = (r: LiveUserRow, cls: Cls[]) => cls.reduce((a, c) => a + (r.by_class[c]?.cpu_s ?? 0), 0);
const inClasses = (r: LiveUserRow, cls: Cls[]) => cls.some((c) => r.by_class[c] || r.live[c]);

function Detail({ name, windowMin, cls }: { name: string; windowMin: number | null; cls: Cls[] }) {
  const [d, setD] = useState<LiveUserDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    setD(null);
    setErr(null);
    getLiveUser(name, windowMin, ac.signal)
      .then(setD)
      .catch((e) => !ac.signal.aborted && setErr(String(e)));
    return () => ac.abort();
  }, [name, windowMin]);
  if (err) return <div className="nm">could not load {name}: {err}</div>;
  if (!d) return <div className="nm">loading {name}…</div>;
  const row = d.row;
  const sessions = d.sessions.filter((s) => cls.includes((s.actor3 ?? 'agent') as Cls));
  const recent = d.recent.filter((e) => !e.actor3 || cls.includes(e.actor3 as Cls));
  return (
    <div className="u-detail">
      <div className="u-cols">
        <div>
          <div className="eyebrow">running now</div>
          {sessions.length ? (
            <table className="roomy">
              <thead>
                <tr>
                  <th>session</th>
                  <th className="num">running for</th>
                  <th className="num">processes</th>
                  <th className="num">memory</th>
                  <th className="num">CPU time</th>
                  <th>launched</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s, i) => (
                  <tr key={i}>
                    <td>
                      <i className="u-dot" style={{ background: CLS_COL[s.actor3 ?? 'agent'] }} />
                      {typeLbl(s.agent_type ?? '')}
                    </td>
                    <td className="num">{dur(s.age_s)}</td>
                    <td className="num">{fint(s.n_procs)}</td>
                    <td className="num">{mb(s.rss_mb)}</td>
                    <td className="num">{dur(s.cpu_s)}</td>
                    <td>{s.actor3 === 'agent' && s.approval && s.approval !== 'na' ? label(AP_LABEL, s.approval) : DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <NoRows what="no agent or IDE session running" />
          )}
        </div>
        <div>
          <div className="eyebrow">in the window</div>
          {row && cls.some((c) => row.by_class[c]) ? (
            <table className="roomy">
              <thead>
                <tr>
                  <th>class</th>
                  <th className="num">processes</th>
                  <th className="num">CPU time</th>
                  <th>by agent</th>
                </tr>
              </thead>
              <tbody>
                {cls.filter((c) => row.by_class[c]).map((c) => (
                  <tr key={c}>
                    <td>
                      <i className="u-dot" style={{ background: CLS_COL[c] }} />
                      {CLS_LBL[c]}
                    </td>
                    <td className="num">{fint(row.by_class[c]!.n)}</td>
                    <td className="num">{dur(row.by_class[c]!.cpu_s)}</td>
                    <td className="wrap">
                      {Object.entries(row.types[c] ?? {})
                        .sort((a, b) => b[1] - a[1])
                        .map(([t, n]) => `${typeLbl(t)} ${fint(n)}`)
                        .join(' · ') || DASH}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <NoRows what="ran nothing in the window" />
          )}
        </div>
      </div>
      <div className="eyebrow" style={{ marginTop: 12 }}>latest commands</div>
      {recent.length ? (
        <table className="roomy">
          <tbody>
            {recent.slice(0, 10).map((e, i) => (
              <tr key={i}>
                <td className="mono nm">{e.ts?.slice(11) ?? DASH}</td>
                <td>
                  <i className="u-dot" style={{ background: CLS_COL[e.actor3 ?? 'agent'] }} />
                  {e.agent_type ? typeLbl(e.agent_type) : CLS_LBL[e.actor3 ?? ''] ?? DASH}
                </td>
                <CmdCell text={e.args || e.comm || e.tool} className="u-cmd">
                  <span style={{ color: pcol(e.purpose) }}>{e.comm ?? e.tool ?? DASH}</span>{' '}
                  {(e.args ?? '').replace(/^\S+\s?/, '')}
                </CmdCell>
                <td className="num">{dur(e.duration_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <NoRows what="none in the recent stream" />
      )}
    </div>
  );
}

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

export function UsersPanel({
  U,
  cls,
  windowMin,
  pageUsers = [],
  pageTypes = [],
}: {
  U: LiveUsers | undefined;
  cls: Cls[];
  windowMin: number | null;
  pageUsers?: string[];
  pageTypes?: string[];
}) {
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<Sort>('agents');
  const [open, setOpen] = useState<string | null>(null);
  const [remote, setRemote] = useState<LiveUsers | null>(null);
  const [run, setRun] = useState<string | null>(null);

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
  const sorted =
    sort === 'agents'
      ? rows // the service's own order: agents running, then activity, then name
      : [...rows].sort((a, b) =>
          sort === 'name' ? a.user.localeCompare(b.user) : shownN(b, cls) - shownN(a, cls),
        );
  const now = serverNow();
  const more = !q && !run && !pageOn && partial ? U!.rows_total - U!.rows.length : 0;

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
        <Segmented<Sort>
          value={sort}
          onChange={setSort}
          options={[
            { k: 'agents', lbl: 'agents running', tip: 'users with the most agents running first' },
            { k: 'active', lbl: 'most active', tip: 'the most processes exited in the window first' },
            { k: 'name', lbl: 'name' },
          ]}
        />
      </div>
      {sorted.length ? (
        <div className="scroller vscroll">
          <table className="roomy users">
            <thead>
              <tr>
                <th>user</th>
                {cls.includes('agent') && <th>agents running</th>}
                {cls.includes('ide') && <th>IDEs running</th>}
                <th className="num" data-tip="processes that exited in the window, split by class">processes exited</th>
                <th className="num">CPU time</th>
                <th className="num">last active</th>
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
                    </td>
                    {cls.includes('agent') && (
                      <td>
                        <Running m={r.live.agent} />
                      </td>
                    )}
                    {cls.includes('ide') && (
                      <td>
                        <Running m={r.live.ide} />
                      </td>
                    )}
                    <td className="num">
                      <Mix r={r} cls={cls} />
                    </td>
                    <td className="num">{shownCpu(r, cls) ? dur(shownCpu(r, cls)) : DASH}</td>
                    <td className="num nm">{r.last_epoch ? ago(now - r.last_epoch * 1000) : DASH}</td>
                  </tr>
                  {isOpen && (
                    <tr className="u-open">
                      <td colSpan={6}>
                        <Detail name={r.user} windowMin={windowMin} cls={cls} />
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
export function ActiveUsers(props: {
  U: LiveUsers | undefined;
  cls: Cls[];
  windowMin: number | null;
  pageUsers?: string[];
  pageTypes?: string[];
}) {
  return (
    <Panel
      id="live-users"
      title="Active users"
      span
      live
      feed="live"
      empty={!props.U}
      emptyWhat="the service sent no user roster"
    >
      <UsersPanel {...props} />
    </Panel>
  );
}

export const scrollToUsers = () =>
  document.getElementById('live-users')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
