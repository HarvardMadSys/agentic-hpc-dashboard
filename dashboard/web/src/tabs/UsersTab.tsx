/* Users: a portrait of each user, read from how they use coding agents.
 *
 * The roster lists every user with an agent session in the window, one row each:
 * which agents, where their sessions are driven from, how they were launched,
 * what the agents' commands do, how long a session runs and how many run at
 * once, and a few traits -- plain statements, each from one rule the service
 * states beside it. A row opens that user's portrait below: the same numbers in
 * sentences and read against the window's other users, their rhythm by hour
 * and over the window, their agents' calls (counted) and time (by phase), their
 * projects and tasks in the agents' own words, the scheduler, and their sessions,
 * each one a link to its process tree on the Sessions tab.
 *
 * Everything is composed by the service from the reducers the other tabs read
 * (`rc_dashboard/portraits.py`), so a session here is the Sessions tab's
 * session and a verdict is the Sandbox's. Two rules carry over: a share is over
 * the known values, with the unknown ones counted beside it, and a count the
 * service could not make (the calls of an agent whose commands it does not read)
 * is said to be unread, never drawn as 0.
 */
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { useUserPortrait, useUsers } from '../api/hooks';
import type {
  ApprovalState,
  Cls,
  PortraitSession,
  PortraitTask,
  SessionSandboxState,
  Surface,
  Trait,
  TraitRule,
  UserPortrait,
  UserRow,
  UsersFleet,
  UsersResponse,
} from '../api/types';
import { clockLabel, fullClock, timeTicks } from '../charts/scale';
import { Svg } from '../charts/primitives';
import { NoRows, Panel } from '../components/Panel';
import { FigureKey, PortraitFigure } from '../components/PortraitFigure';
import { absent } from '../lib/panels';
import { Plate } from '../components/Plate';
import { SortTh } from '../components/SortTh';
import { SurfaceChip } from '../components/SurfaceChip';
import { CategoryChip } from '../components/CategoryChip';
import { AGENT_TYPE_COL } from '../lib/classes';
import { useNow } from '../lib/useNow';
import type { Who } from '../lib/filters';
import { DASH, ago, dur, fint, fmt, mb, pc } from '../lib/format';
import { GLOSS } from '../lib/glossary';
import { PHASES, phaseCol, phaseLbl, shortCmd } from '../lib/phases';
import {
  CALL_CATS,
  CAT_TIP,
  agentLbl,
  busiestHour,
  bypassShare,
  catCol,
  catLbl,
  hourSpan,
  loggedCalls,
  pctWords,
  plural,
  ranked,
  ruleOf,
  share,
  leverageHeadline,
  leveragePoints,
  surfaceShare,
} from '../lib/portraits';
import { AP_COL, AP_LABEL, AP_SHORT, APPROVAL_KNOWN, APPROVAL_ORDER, SB_COL, SESSION_KNOWN, SESSION_LABEL, SESSION_ORDER, legendCol } from '../lib/sandbox';
import { SURFACE_LBL, SURFACE_ORDER } from '../lib/surfaces';
import { taskLabel, taskSource } from '../lib/tasks';
import { useSort } from '../lib/useSort';
import './users.css';
import { table } from '../lib/lookup';

/** How often the roster and an open portrait are read again while on screen. */
const EVERY_MS = 15_000;

const typeCol = (t: string | null | undefined): string => (t && AGENT_TYPE_COL[t]) || 'var(--s1)';

/** In a row with room for a few, the traits that warrant attention come first. */
const TONE_RANK: Record<Trait['tone'], number> = { crit: 0, warn: 1, ok: 2, '': 3 };

/** The sandbox verdicts, short enough for a share row; SESSION_LABEL is on hover. */
const SESSION_SHORT: Record<string, string> = table({
  sandboxed: 'all sandboxed',
  partial: 'some sandboxed',
  failed: 'failed to start',
  unsandboxed: 'not sandboxed',
  unknown: 'unreadable',
  no_calls: 'no tool calls',
});

/** A phase's short name (lib/phases.ts), as a band's segment says it. */
const PHASE_SHORT: Record<string, string> = table(Object.fromEntries(PHASES.map(([k, , short]) => [k, short])));

/** Before the first answer: the service is starting or busy, which is no fact about the feed. */
const WAITING = { ...absent('ebpfm', 'waiting for the dashboard service…'), _waiting: true };

/* ------------------------------------------------------------- small parts */

/** `5 min ago`, from epoch seconds, kept current with every live frame (lib/useNow.ts). */
function Ago({ s }: { s: number }) {
  return <>{ago(useNow() - s * 1000)}</>;
}

/** A trait as a badge: its words, coloured by its tone, its numbers and its rule on hover. */
function TraitBadge({ t, rule, detail = false }: { t: Trait; rule?: string; detail?: boolean }) {
  return (
    <span className={`u-badge ${t.tone}`} data-tip={`${t.label}: ${t.detail}${rule ? `\nrule: ${rule}` : ''}`}>
      {t.label}
      {detail && <span className="ur-tdetail"> · {t.detail}</span>}
    </span>
  );
}

function TraitBadges({ traits, catalogue, max = 3 }: { traits: Trait[]; catalogue: TraitRule[]; max?: number }) {
  if (!traits.length) return <span className="nm">{DASH}</span>;
  const order = [...traits].sort((a, b) => TONE_RANK[a.tone] - TONE_RANK[b.tone]);
  const shown = order.slice(0, max);
  const more = order.slice(max);
  return (
    <span className="u-badges">
      {shown.map((t) => (
        <TraitBadge key={t.id} t={t} rule={ruleOf(catalogue, t.id)} />
      ))}
      {more.length > 0 && (
        <span className="nm" data-tip={more.map((t) => `${t.label}: ${t.detail}`).join('\n')}>
          +{more.length}
        </span>
      )}
    </span>
  );
}

/** Sessions by agent, as chips. */
function Agents({ types }: { types: Record<string, number> }) {
  const r = ranked(types);
  if (!r.length) return <span className="nm">{DASH}</span>;
  return (
    <span className="u-chips">
      {r.map(([t, n]) => (
        <span key={t} className="u-chip">
          <i className="u-dot" style={{ background: typeCol(t) }} />
          {agentLbl(t)} <b>{fint(n)}</b>
        </span>
      ))}
    </span>
  );
}

/** The calls the logs hold, by category, as one bar; unread calls are said so. */
function CatBar({ row, w = 76 }: { row: Pick<UserRow, 'cats' | 'calls' | 'calls_sessions' | 'sessions'>; w?: number }) {
  const total = loggedCalls(row);
  if (row.calls == null) {
    return (
      <span className="nm" data-tip="the service reads the commands of Claude Code and Codex; this user’s agents are neither, so their tool calls are not counted: unread, which is not none">
        not read
      </span>
    );
  }
  if (!total) return <span className="nm">{fint(row.calls)}</span>;
  const parts = CALL_CATS.map(([c]) => [c, row.cats[c] ?? 0] as const).filter(([, n]) => n > 0);
  return (
    <span
      className="u-mix"
      data-tip={[
        `tool calls by what they do, ${fint(total)} in the logs`,
        ...parts.map(([c, n]) => `${catLbl(c)}: ${fint(n)} (${pctWords((100 * n) / total)})`),
        row.calls_sessions < row.sessions ? `${row.sessions - row.calls_sessions} session(s) of an agent whose tool calls are not read` : '',
      ].filter(Boolean).join('\n')}
    >
      <span className="u-bar" style={{ width: w }}>
        {parts.map(([c, n]) => (
          <i key={c} style={{ width: `${(100 * n) / total}%`, background: catCol(c) }} />
        ))}
      </span>
      <b>{fint(row.calls)}</b>
    </span>
  );
}

/** Active minutes by local hour, as 24 small bars. */
function HourStrip({ hours, tz }: { hours: number[]; tz?: string | null }) {
  const max = Math.max(0, ...hours);
  if (!max) return <span className="nm">{DASH}</span>;
  const best = busiestHour(hours);
  return (
    <span
      className="ur-strip"
      data-tip={`minutes in which an agent began a tool call, by hour of day${tz ? ` (${tz})` : ''}${best ? `\nbusiest: ${hourSpan(best[0])}, ${plural(best[1], 'minute')}` : ''}`}
    >
      {hours.map((n, h) => (
        <i key={h} style={{ height: `${n ? Math.max(12, (100 * n) / max) : 4}%`, opacity: n ? 1 : 0.35 }} />
      ))}
    </span>
  );
}

/** Where its sessions are driven from: each surface and how many. */
function Surfaces({ m }: { m: Partial<Record<Surface, number>> }) {
  const r = ranked(m);
  if (!r.length) return <span className="nm">{DASH}</span>;
  return (
    <span className="ur-words" data-tip={r.map(([s, n]) => `${SURFACE_LBL[s]}: ${fint(n)}`).join('\n')}>
      {r.slice(0, 2).map(([s, n]) => `${SURFACE_LBL[s]} ${fint(n)}`).join(' · ')}
      {r.length > 2 && <span className="nm"> +{r.length - 2}</span>}
    </span>
  );
}

/** Launched with a bypass flag, of those whose launch is known. */
function Bypass({ m }: { m: Partial<Record<ApprovalState, number>> }) {
  const s = bypassShare(m as Record<string, number>);
  if (!s.known) return <span className="nm" data-tip="no session’s launch could be read off its command line">{DASH}</span>;
  const n = m.bypassed ?? 0;
  return (
    <span
      style={n ? { color: 'var(--crimson-ink)' } : undefined}
      data-tip={[
        `of ${plural(s.known, 'session')} whose launch is known, ${fint(n)} started with a bypass flag`,
        ...APPROVAL_ORDER.filter((a) => m[a as ApprovalState]).map((a) => `${AP_LABEL[a]}: ${fint(m[a as ApprovalState])}`),
      ].join('\n')}
    >
      {fint(n)} of {fint(s.known)}
    </span>
  );
}

/* -------------------------------------------------------------- the plates */

function Plates({ U, rows }: { U: UsersResponse; rows: UserRow[] }) {
  const f = U.fleet;
  const win = U._window?.label ?? (U.window ? `${Math.round(U.window.minutes / 60)}h` : '');
  const running = rows.filter((r) => r.running > 0).length;
  const bp = f ? bypassShare(f.approval as Record<string, number>) : null;
  const sc = f ? surfaceShare(f.surfaces, ['script']) : null;
  return (
    <div className="plates">
      <Plate
        k={`agent users · last ${win}`}
        total={f ? f.users : null}
        tip="accounts with at least one agent session in the window: an agent process seen at the root of its tree, as the Sessions tab’s plates count one"
        note={f ? `${fint(running)} with an agent running now` : undefined}
      />
      <Plate
        k="agent sessions"
        total={f ? f.sessions : null}
        tip="one session is one agent run, its rows on two days together; a typical one is the median"
        note={f ? `typically open ${dur(f.wall_p50_s)} · ${f.calls_p50 != null ? plural(f.calls_p50, 'tool call') : 'tool calls not read'}` : undefined}
      />
      <Plate
        k="launched with a bypass flag"
        total={bp?.pct ?? null}
        fmtFn={(n) => pc(n)}
        alarm={(f?.approval.bypassed ?? 0) > 0}
        tip={`${GLOSS.bypass}. A share of the sessions whose launch is known: a Codex app server takes its policy per thread, and a session whose own record was not seen says nothing`}
        note={bp?.known ? `${fint(f?.approval.bypassed ?? 0)} of ${fint(bp.known)} sessions whose launch is known` : undefined}
      />
      <Plate
        k="driven from a script"
        total={sc?.pct ?? null}
        fmtFn={(n) => pc(n)}
        tip="sessions with no one at a terminal: claude -p, codex exec, the Agent and Codex SDKs, an MCP server. A share of the sessions where that is known"
        note={sc?.known ? `${fint(sc.n)} of ${fint(sc.known)} sessions` : undefined}
      />
    </div>
  );
}

/* -------------------------------------------------------------- the roster */

type RosterKey = 'user' | 'sessions' | 'bypass' | 'calls' | 'wall' | 'open' | 'last';

function Roster({
  U,
  rows,
  open,
  setOpen,
}: {
  U: UsersResponse;
  rows: UserRow[];
  open: string | null;
  setOpen: (u: string | null) => void;
}) {
  const [q, setQ] = useState('');
  const [trait, setTrait] = useState<string | null>(null);
  // rows name people: A to Z until a header is clicked (lib/useSort.ts)
  const s = useSort<RosterKey>('user', 'asc');
  const catalogue = U.traits ?? [];
  const ql = q.toLowerCase();
  const kept = rows.filter(
    (r) => (!ql || r.user.toLowerCase().includes(ql)) && (!trait || r.traits.some((t) => t.id === trait)),
  );
  const sorted = s.sort(kept, (r, k) => {
    switch (k) {
      case 'user': return r.user;
      case 'sessions': return r.sessions;
      case 'bypass': return bypassShare(r.approval as Record<string, number>).pct;
      case 'calls': return r.calls;
      case 'wall': return r.wall_p50_s;
      case 'open': return r.peak_open;
      case 'last': return r.last;
    }
  });
  // the traits the window's users hold, the most held first: each a filter
  const held = ranked(U.fleet?.traits ?? {});
  return (
    <div className="u-panel">
      {held.length > 0 && (
        <div className="u-summary">
          <span className="eyebrow" data-tip="each trait is one rule over the user's numbers; hover one for its rule. Click to list only the users who hold it">traits</span>
          {held.map(([id, n]) => {
            const t = catalogue.find((x) => x.id === id);
            return (
              <button
                key={id}
                className={`u-pick ur-pick ${t?.tone ?? ''}`}
                aria-pressed={trait === id}
                onClick={() => setTrait(trait === id ? null : id)}
                data-tip={trait === id ? 'show every user again' : `${t?.rule ?? id}\nlist only these users`}
              >
                {t?.label ?? id} <b>{fint(n)}</b>
              </button>
            );
          })}
        </div>
      )}
      <div className="u-ctl">
        <input
          className="wininput u-search"
          placeholder="find a user"
          value={q}
          onChange={(e) => setQ(e.target.value.trim())}
          aria-label="find a user"
        />
        <span className="nm">click a user for their portrait</span>
      </div>
      {sorted.length ? (
        <div className="scroller vscroll">
          <table className="roomy users ur-roster wrap-head">
            <thead>
              <tr>
                <SortTh s={s} k="user" first="asc">user</SortTh>
                <SortTh s={s} k="sessions" num tip="agent sessions in the window, an agent's rows on two days together">sessions</SortTh>
                <th data-tip="sessions by agent">agents</th>
                <th data-tip={GLOSS.surface}>driven from</th>
                <SortTh s={s} k="bypass" num tip="sessions started with a bypass flag, of those whose launch is known">bypass flag</SortTh>
                <SortTh s={s} k="calls" num tip="tool calls, and what the agents' commands do: explore, edit, build, test, run, Slurm, wait, git. Read from commands: the agent's own Read and Edit tools leave none">tool calls</SortTh>
                <SortTh s={s} k="wall" num tip="how long a typical session was open, first record to last: the median">typical</SortTh>
                <SortTh s={s} k="open" num tip="the most sessions open at one moment">at once</SortTh>
                <SortTh s={s} k="last" num tip="minutes in which an agent began a tool call, by hour of day, and when one last did; sorts by the latter">when</SortTh>
                <th data-tip="plain statements about how this user's agents were used, each from one rule; hover one for its rule">traits</th>
              </tr>
            </thead>
            {sorted.map((r) => {
              const on = open === r.user;
              const toggle = () => setOpen(on ? null : r.user);
              return (
                // one body a user: their row, and the line under it that says how they leverage agents
                <tbody key={r.user} className={`ur-user${on ? ' open' : ''}`} aria-selected={on}>
                  <tr className="ur-row" onClick={toggle}>
                    <td>
                      <span className="u-caret">{on ? '▾' : '▸'}</span>
                      <span className="mono u-name">{r.user}</span>
                      {r.running > 0 && (
                        <span className="ur-live" data-tip={`${plural(r.running, 'session')} running now`}>
                          <i />
                          {fint(r.running)}
                        </span>
                      )}
                    </td>
                    <td className="num">{fint(r.sessions)}</td>
                    <td><Agents types={r.agent_types} /></td>
                    <td className="ur-wrapcell"><Surfaces m={r.surfaces} /></td>
                    <td className="num"><Bypass m={r.approval} /></td>
                    <td className="num"><CatBar row={r} /></td>
                    <td className="num">{dur(r.wall_p50_s)}</td>
                    <td className="num">{r.peak_open ? fint(r.peak_open) : DASH}</td>
                    <td className="num">
                      <HourStrip hours={r.hours} />
                      <div className="nm ur-ago">{r.last ? <Ago s={r.last} /> : DASH}</div>
                    </td>
                    <td className="ur-traitcell"><TraitBadges traits={r.traits} catalogue={catalogue} max={2} /></td>
                  </tr>
                  <tr className="ur-sumrow" onClick={toggle}>
                    <td colSpan={10}>
                      <span className="ur-sumline" data-tip="how they leverage agents, from the numbers on this row; their portrait says it point by point">
                        {leverageHeadline(r)}
                      </span>
                    </td>
                  </tr>
                </tbody>
              );
            })}
          </table>
        </div>
      ) : (
        <NoRows what={q ? `no user matches “${q}”` : trait ? 'no user holds this trait' : 'no user ran an agent in the window'} />
      )}
      <div className="u-foot">
        {sorted.length > 0 && <span className="nm">{plural(sorted.length, 'user')}{sorted.length < rows.length ? ` of ${fint(rows.length)}` : ''}</span>}
        {(U.unattributed ?? 0) > 0 && (
          <span
            className="nm"
            data-tip="an agent session whose records named no user and no uid is no one’s row: there is no one to draw and no name to open it by. The Sessions tab lists it with the rest"
          >
            {plural(U.unattributed ?? 0, 'agent session')} whose user was not read {U.unattributed === 1 ? 'is' : 'are'} not listed
          </span>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ the portrait */

/** How a user leverages their agents: the summary in one sentence, then point by
 *  point, each with the numbers it rests on and how they were counted on hover. */
function Leveraging({ p, row }: { p: UserPortrait; row: UserRow }) {
  const points = leveragePoints(row, p.fleet, p.tz);
  return (
    <section className="ur-lev" aria-label="how they leverage agents">
      <div className="eyebrow" data-tip={p.basis?.leverage ?? 'how this user leverages their agents, from the numbers below'}>
        how they leverage agents
      </div>
      <p className="ur-lev-head">{leverageHeadline(row)}</p>
      <dl className="ur-lev-points">
        {points.map((x) => (
          <div key={x.k}>
            <dt data-tip={x.tip}>{x.label}</dt>
            <dd>{x.text}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** One user's head: who, when, how they leverage agents, the traits, the numbers. */
function Head({ p, row, catalogue }: { p: UserPortrait; row: UserRow; catalogue: TraitRule[] }) {
  const nums: [string, string, string?][] = [
    ['sessions', fint(row.sessions), 'agent sessions in the window'],
    ['tool calls', row.calls == null ? 'not read' : fint(row.calls), GLOSS.toolcall],
    ['active minutes', fint(row.active_min), 'minutes in which an agent began a tool call'],
    ['processes', fint(row.processes), GLOSS.exited],
    ['CPU time', dur(row.cpu_s), 'CPU used by every process their agents ran, each counted once'],
    ['disk read · written', `${mb(row.rd_mb)} · ${mb(row.wr_mb)}`, GLOSS.diskbytes + ', each process’s own'],
    ['network sent · received', `${mb(row.net_tx_mb)} · ${mb(row.net_rx_mb)}`],
    ['peak memory', row.peak_rss_mb ? mb(row.peak_rss_mb) : 'not measured', 'the most memory any one process held'],
  ];
  return (
    <div className="ur-head">
      <div className="ur-top">
        <figure className="ur-fig">
          <PortraitFigure row={row} catalogue={catalogue} tz={p.tz} />
          <FigureKey />
        </figure>
        <div className="ur-topr">
          <div className="ur-who">
            <h2 className="mono">{row.user}</h2>
            <div className="nm">
              {row.hosts.join(' · ')}
              {row.first != null && row.last != null && ` · ${fullClock(row.first)} → ${fullClock(row.last)}`}
              {row.running > 0 && (
                <span className="ur-live" data-tip="sessions whose agent is still sending records"><i />{plural(row.running, 'session')} running now</span>
              )}
            </div>
          </div>
          <Leveraging p={p} row={row} />
        </div>
      </div>
      {row.traits.length > 0 && (
        <div className="ur-traits">
          {row.traits.map((t) => (
            <TraitBadge key={t.id} t={t} rule={ruleOf(catalogue, t.id)} detail />
          ))}
        </div>
      )}
      <dl className="ur-nums">
        {nums.map(([k, v, tip]) => (
          <div key={k}>
            <dt data-tip={tip}>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

interface ShareItem {
  k: string;
  label: string;
  /** what the share is of, as the row says it: a count or a time */
  val: string;
  /** this user's share, in percent */
  pct: number | null;
  /** the whole window's share, in percent: a tick on the track; null draws none */
  win?: number | null;
  color: string;
  tip?: string;
}

/** Share rows: a label, its count, the user's share as a bar, and the window's
 *  share as a tick on the same track, so the two read against each other in one
 *  row. Every share keeps its own row of pixels, at least a hairline above 0. */
function ShareRows({ items }: { items: ShareItem[] }) {
  return (
    <div className="ur-srows">
      {items.map((x) => (
        <div
          key={x.k}
          className="ur-srow"
          data-tip={[
            x.tip ?? x.label,
            `this user: ${x.val}${x.pct != null ? `, ${pctWords(x.pct)}` : ''}`,
            x.win != null ? `the whole window: ${pctWords(x.win)}` : '',
          ].filter(Boolean).join('\n')}
        >
          <span className="lbl">
            <i className="u-dot" style={{ background: x.color }} />
            {x.label}
          </span>
          <span className="val">{x.val}</span>
          <span className="trk">
            {x.pct != null && x.pct > 0 && <i style={{ width: `max(2px, ${Math.min(100, x.pct)}%)`, background: x.color }} />}
            {x.win != null && <b style={{ left: `${Math.min(100, x.win)}%` }} />}
          </span>
          <span className="pct">{x.pct != null ? pctWords(x.pct) : DASH}</span>
        </div>
      ))}
    </div>
  );
}

/** One measure's states as share rows: the user's share of the known values, and
 *  the window's on the same track; the values that say nothing are counted under them. */
function Shares({
  title,
  tip,
  order,
  counts,
  fleet,
  known,
  label,
  color,
  tipOf,
  rest,
}: {
  title: string;
  tip?: string;
  order: string[];
  counts: Partial<Record<string, number>>;
  fleet?: Partial<Record<string, number>> | null;
  /** the states a share is over; every state when omitted */
  known?: string[];
  label: (k: string) => string;
  color: (k: string) => string;
  /** a state's full name, on hover, where the row's label is short */
  tipOf?: (k: string) => string;
  /** what the states outside `known` are called, in the line under them */
  rest?: string;
}) {
  const over = known ?? order;
  const total = over.reduce((a, k) => a + (counts[k] ?? 0), 0);
  // The window's share is over every state the window holds, not only those this
  // user has: `order` may list just theirs (their agent types), and a Claude-only
  // user's tick is then the window's Claude share, not 100%.
  const fover = known ?? [...new Set([...order, ...Object.keys(fleet ?? {})])];
  const ftotal = fleet ? fover.reduce((a, k) => a + (fleet[k] ?? 0), 0) : 0;
  const shown = order.filter((k) => over.includes(k) && (counts[k] ?? 0) > 0);
  const unknown = order.filter((k) => !over.includes(k) && (counts[k] ?? 0) > 0);
  return (
    <div className="ur-shares">
      <div className="eyebrow" data-tip={tip}>{title}</div>
      {shown.length ? (
        <ShareRows
          items={shown.map((k) => ({
            k,
            label: label(k),
            val: fint(counts[k] ?? 0),
            pct: share(counts[k] ?? 0, total),
            win: fleet && ftotal ? share(fleet[k] ?? 0, ftotal) : null,
            color: legendCol(color(k)),
            tip: tipOf?.(k),
          }))}
        />
      ) : (
        <div className="nm">none known</div>
      )}
      {unknown.length > 0 && (
        <div className="nm ur-rest" data-tip={rest}>
          not in the shares: {unknown.map((k) => `${label(k)} ${fint(counts[k])}`).join(' · ')}
        </div>
      )}
    </div>
  );
}

/** What a share row's bar and tick are. */
function ShareKey() {
  return (
    <div className="nm ur-key">
      the bar is this user’s share; the tick <b /> is the whole window’s
    </div>
  );
}

function Launch({ row, fleet }: { row: UserRow; fleet: UsersFleet | null | undefined }) {
  const types = ranked(row.agent_types).map(([t]) => t);
  return (
    <>
    <div className="ur-launch">
      <Shares
        title="agent"
        order={types}
        counts={row.agent_types}
        fleet={fleet?.agent_types}
        label={agentLbl}
        color={typeCol}
      />
      <Shares
        title="driven from"
        tip={GLOSS.surface}
        order={SURFACE_ORDER}
        known={SURFACE_ORDER.filter((s) => s !== 'unknown')}
        counts={row.surfaces}
        fleet={fleet?.surfaces}
        label={(s) => SURFACE_LBL[s as Surface] ?? s}
        color={(s) => (s === 'terminal' ? 'var(--s2)' : s === 'script' ? 'var(--s4)' : s === 'unknown' ? 'var(--sg)' : 'var(--s8)')}
        rest="the agent’s own record was not read, so nothing says where it was driven from"
      />
      <Shares
        title="approval at launch"
        tip={GLOSS.approval}
        order={APPROVAL_ORDER}
        known={APPROVAL_KNOWN}
        counts={row.approval}
        fleet={fleet?.approval}
        label={(a) => AP_SHORT[a] ?? a}
        tipOf={(a) => AP_LABEL[a] ?? a}
        color={(a) => AP_COL[a] ?? 'var(--sg)'}
        rest="a Codex app server takes its policy per thread, and a session whose own record was not seen says nothing"
      />
      <Shares
        title="tool calls sandboxed"
        tip="whether each session's tool calls ran sandboxed, read from each process's namespaces (/proc/<pid>/ns), as the Internal tab counts them"
        order={SESSION_ORDER}
        known={SESSION_KNOWN}
        counts={row.sandbox}
        fleet={fleet?.sandbox}
        label={(k) => SESSION_SHORT[k] ?? k}
        tipOf={(k) => SESSION_LABEL[k] ?? k}
        color={(k) => SB_COL[k as SessionSandboxState] ?? 'var(--sg)'}
        rest="sessions whose tool calls could not be judged, or that made none the check reads"
      />
    </div>
    {fleet && fleet.users > 1 && <ShareKey />}
    </>
  );
}

/** Active minutes by local hour: 24 bars on one axis. */
function HourBars({ hours, tz }: { hours: number[]; tz?: string | null }) {
  const W = 480, H = 110, L = 28, B = 18, T = 6;
  const max = Math.max(1, ...hours);
  const bw = (W - L) / 24;
  const y = (n: number) => H - B - ((H - B - T) * n) / max;
  return (
    <div className="ur-chart">
      <div className="eyebrow" data-tip={`minutes in which one of their agents began a tool call, by the hour of day${tz ? `, in ${tz}, the service's time zone` : ''}`}>
        when their agents work{tz ? ` · ${tz}` : ''}
      </div>
      <Svg w={W} h={H} label="active minutes by hour of day">
        <line className="grid-line" x1={L} x2={W} y1={y(max)} y2={y(max)} />
        <text className="tick" x={L - 5} y={y(max) + 3} textAnchor="end">{fint(max)}</text>
        <line className="base-line" x1={L} x2={W} y1={H - B} y2={H - B} />
        {hours.map((n, h) => (
          <g key={h}>
            <rect
              x={L + h * bw + 1}
              y={n ? y(n) : H - B - 1}
              width={bw - 2}
              height={n ? H - B - y(n) : 1}
              rx={1.5}
              fill={n ? 'var(--s1)' : 'var(--rule)'}
              data-tip={`${hourSpan(h)} · ${plural(n, 'active minute')}`}
            />
            {h % 6 === 0 && (
              <text className="tick" x={L + h * bw + bw / 2} y={H - 4} textAnchor="middle">{`${String(h).padStart(2, '0')}:00`}</text>
            )}
          </g>
        ))}
      </Svg>
    </div>
  );
}

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** Active minutes by weekday and hour: a window of two days or more. */
function WeekGrid({ grid, tz }: { grid: number[][]; tz?: string | null }) {
  const max = Math.max(1, ...grid.flat());
  const shade = (n: number) => (!n ? 'var(--surface-2)' : n > 0.66 * max ? 'var(--seq4)' : n > 0.33 * max ? 'var(--seq3)' : n > 0.1 * max ? 'var(--seq2)' : 'var(--seq1)');
  return (
    <div className="ur-chart">
      <div className="eyebrow" data-tip={`the same minutes by weekday and hour${tz ? `, in ${tz}` : ''}`}>by weekday</div>
      <div className="ur-week">
        {grid.map((row, d) => (
          <div key={d} className="ur-weekrow">
            <span className="nm">{DAYS[d]}</span>
            {row.map((n, h) => (
              <i key={h} style={{ background: shade(n) }} data-tip={`${DAYS[d]} ${hourSpan(h)} · ${plural(n, 'active minute')}`} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Each session over the window, from its first record to its last, packed into
 *  lanes: how many ran at once, and when. */
function Timeline({ sessions, minutes, total }: { sessions: PortraitSession[]; minutes: number | null; total: number }) {
  // the window ends now, and a running session reaches it: read again with every live frame (lib/useNow.ts)
  const now = useNow() / 1000;
  const spans = sessions.filter((s) => s.first != null && s.last != null);
  if (!spans.length) return null;
  const lo = Math.min(minutes ? now - minutes * 60 : Infinity, ...spans.map((s) => s.first!));
  const hi = Math.max(now, ...spans.map((s) => s.last!));
  const lanes: number[] = [];
  const placed = [...spans]
    .sort((a, b) => a.first! - b.first!)
    .map((s) => {
      let k = lanes.findIndex((end) => end < s.first!);
      if (k < 0) {
        k = lanes.length;
        lanes.push(0);
      }
      lanes[k] = s.running ? hi : s.last!;
      return { s, k };
    });
  const MAX = 12;
  const n = Math.min(lanes.length, MAX);
  const W = 560, L = 6, R = 6, LH = 12, T = 4, B = 18;
  const H = T + n * LH + B;
  const x = (t: number) => L + ((W - L - R) * (t - lo)) / Math.max(1, hi - lo);
  const ticks = timeTicks(lo, hi, 6);
  return (
    <div className="ur-chart">
      <div className="eyebrow" data-tip="each session from its first record to its last, packed into rows so that sessions open at the same time sit on separate rows; a running one reaches now">
        sessions over the window
        {spans.length < total ? ` · the newest ${fint(spans.length)} of ${fint(total)}` : ''}
      </div>
      <Svg w={W} h={H} label="sessions over the window">
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={x(t)} x2={x(t)} y1={T} y2={H - B} />
            <text className="tick" x={x(t)} y={H - 5} textAnchor="middle">{clockLabel(t)}</text>
          </g>
        ))}
        {placed.filter(({ k }) => k < MAX).map(({ s, k }) => {
          const x0 = x(s.first!);
          const x1 = Math.max(x0 + 2, x(s.running ? hi : s.last!));
          return (
            <rect
              key={s.sid}
              className={s.running ? 'ur-run' : undefined}
              x={x0}
              y={T + k * LH + 2}
              width={x1 - x0}
              height={LH - 4}
              rx={2}
              fill={typeCol(s.agent_type)}
              data-tip={[
                `${agentLbl(s.agent_type)}${s.project ? ` · ${s.project}` : ''}`,
                `${fullClock(s.first!)} → ${s.running ? 'running now' : fullClock(s.last!)}`,
                `open ${dur(s.wall_s)} · ${s.calls == null ? 'tool calls not read' : plural(s.calls, 'tool call')}`,
              ].join('\n')}
            />
          );
        })}
      </Svg>
      {lanes.length > MAX && <div className="nm">{fint(lanes.length - MAX)} more rows of sessions not drawn</div>}
    </div>
  );
}

function Rhythm({ p, row }: { p: UserPortrait; row: UserRow }) {
  const minutes = p.window?.minutes ?? null;
  const fleet = p.fleet;
  const others = fleet && fleet.users > 1;
  const stat = (k: string, v: string, f?: string | null, tip?: string) => (
    <div>
      <dt data-tip={tip}>{k}</dt>
      <dd>
        {v}
        {others && f ? <span className="nm"> · window {f}</span> : null}
      </dd>
    </div>
  );
  return (
    <div className="ur-rhythm">
      <dl className="ur-nums">
        {stat('typical session', dur(row.wall_p50_s), fleet ? dur(fleet.wall_p50_s) : null, 'the median session, open from its first record to its last; beside it the median of every session in the window')}
        {stat('longest', dur(row.wall_max_s))}
        {stat('open at once', row.peak_open ? fint(row.peak_open) : DASH, fleet?.peak_open_p50 != null ? fint(fleet.peak_open_p50) : null, 'the most sessions open at one moment; beside it the median user’s')}
        {stat('active minutes', fint(row.active_min), fleet?.active_min_p50 != null ? fint(fleet.active_min_p50) : null, 'minutes in which an agent began a tool call; beside it the median user’s')}
      </dl>
      <div className="ur-pair">
        <HourBars hours={row.hours} tz={p.tz} />
        {row.grid && minutes != null && minutes >= 2880 && <WeekGrid grid={row.grid} tz={p.tz} />}
      </div>
      <Timeline sessions={p.sessions ?? []} minutes={minutes} total={p.sessions_total ?? 0} />
    </div>
  );
}

/** Calls counted by category beside the time in each phase: count and cost. */
function Work({ p, row }: { p: UserPortrait; row: UserRow }) {
  const total = loggedCalls(row);
  const fcalls = p.fleet?.cats ?? {};
  const ftotal = Object.values(fcalls).reduce<number>((a, b) => a + (b ?? 0), 0);
  const cats = CALL_CATS.filter(([c]) => (row.cats[c] ?? 0) > 0).sort((a, b) => (row.cats[b[0]] ?? 0) - (row.cats[a[0]] ?? 0));
  const ph = p.phases;
  const busy = ranked(ph?.time_s ?? {}).filter(([k]) => k !== 'idle');
  const busyTotal = busy.reduce((a, [, s]) => a + s, 0);
  const idle = ph?.time_s.idle ?? 0;
  return (
    <div className="ur-work">
      <div className="ur-pair">
        <div className="ur-shares">
          <div className="eyebrow" data-tip="tool calls by what each command does, counted from each session's call log. The agent's own Read and Edit tools run no command and are not here">tool calls · counted</div>
          {row.calls == null ? (
            <div className="nm">not read: the service reads the commands of Claude Code and Codex only</div>
          ) : cats.length ? (
            <ShareRows
              items={cats.map(([c, lbl]) => ({
                k: c,
                label: lbl,
                val: fint(row.cats[c] ?? 0),
                pct: share(row.cats[c] ?? 0, total),
                win: ftotal ? share(fcalls[c] ?? 0, ftotal) : null,
                color: catCol(c),
                tip: `${lbl}: ${CAT_TIP[c]}`,
              }))}
            />
          ) : (
            <div className="nm">no tool call in the logs</div>
          )}
        </div>
        <div className="ur-shares">
          <div className="eyebrow" data-tip={`${GLOSS.phases}. Time from the same phase blocks the Sessions tab’s rows show`}>time · by phase</div>
          {busy.length ? (
            <ShareRows
              items={busy.map(([k, v]) => ({
                k,
                label: PHASE_SHORT[k] ?? phaseLbl(k),
                tip: phaseLbl(k),
                val: dur(v),
                pct: share(v, busyTotal),
                color: phaseCol(k),
              }))}
            />
          ) : (
            <div className="nm">{ph?.of ? 'no phase with tool calls' : 'no session whose tool calls are read'}</div>
          )}
          {ph && (
            <div className="nm ur-rest">
              {idle > 0 && `${dur(idle)} idle between tool calls · `}
              from {ph.sessions < ph.of ? `the newest ${fint(ph.sessions)} of ${fint(ph.of)}` : plural(ph.sessions, 'session')}’s phase blocks
            </div>
          )}
        </div>
      </div>
      {(p.commands?.length ?? 0) > 0 && (
        <div className="ur-cmds">
          <div className="eyebrow" data-tip="the commands their agents ran most, as tool calls; masked as every command is">commands run most</div>
          <ol>
            {p.commands!.map(([c, n], i) => (
              <li key={i} data-tip={c}>
                <code>{shortCmd(c)}</code>
                <span className="nm"> ×{fint(n)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

function Tasks({ p, row, openTraj }: { p: UserPortrait; row: UserRow; openTraj: (sid: string) => void }) {
  const t = p.tasks;
  const q = t?.quoted ?? {};
  const words = [
    q.commit ? plural(q.commit, 'commit') : null,
    q.pr ? plural(q.pr, 'pull request') : null,
    q.log ? plural(q.log, 'log entry', 'log entries') : null,
  ].filter(Boolean);
  return (
    <div className="ur-tasks">
      {row.projects.length > 0 && (
        <div>
          <div className="eyebrow" data-tip="the folder each session's agent was started in, or, when that is a home folder or named for a person, the folder its commands change into most">projects</div>
          <span className="u-chips">
            {row.projects.map(([name, n]) => (
              <span key={name} className="u-chip">
                <span className="mono">{name}</span> <b>{fint(n)}</b>
              </span>
            ))}
            {row.projects_total > row.projects.length && <span className="nm">+{fint(row.projects_total - row.projects.length)} more</span>}
          </span>
        </div>
      )}
      {t && (
        <p className="ur-tline">
          <b>{plural(t.n, 'task')}</b>
          {t.lone > 0 && <span className="nm"> · {plural(t.lone, 'lone stretch', 'lone stretches')}</span>}
          {words.length > 0 && <span> · in the agents’ own words: {words.join(', ')}</span>}
        </p>
      )}
      {t?.recent.length ? (
        <div className="scroller vscroll short">
          <table className="roomy ur-tasklist">
            <tbody>
              {t.recent.map((x: PortraitTask, i) => {
                const src = taskSource(x);
                return (
                  <tr key={`${x.sid}:${x.t0}:${i}`}>
                    <td className="mono nm">{x.t0 != null ? fullClock(x.t0) : DASH}</td>
                    <td className="wrap">
                      <span className={src ? 'ur-quote' : undefined} data-tip={src ? 'the agent’s own words' : 'built from the names in its commands: not the agent’s words'}>
                        {taskLabel(x)}
                      </span>
                      {src && <span className="ur-src">{src}</span>}
                      {x.open && <span className="ur-src open">running</span>}
                    </td>
                    <td className="nm">{x.project ?? DASH}</td>
                    <td className="num">{x.n != null ? plural(x.n, 'tool call') : DASH}</td>
                    <td>
                      <button className="linkbtn" onClick={() => openTraj(x.sid)} data-tip="open this session's process tree on the Sessions tab">
                        process tree →
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <NoRows what={p.phases?.of ? 'no task in the phase blocks read' : 'no session whose tool calls are read'} />
      )}
      {t && t.listed_of > t.recent.length && <div className="nm">the newest {fint(t.recent.length)} of {fint(t.listed_of)} tasks</div>}
    </div>
  );
}

function Scheduler({ row }: { row: UserRow }) {
  const s = row.slurm;
  if (!s) return null;
  const l = s.longest_loop;
  const kv: [string, string, string?][] = [
    ['jobs submitted', fint(s.jobs), 'submissions that came back with a job id'],
    ['refused', s.rejected ? `${fint(s.rejected)} of ${fint(s.asked)}` : DASH, GLOSS.refused],
    ['queue checks', `${fint(s.queries)}${s.queries_per_job != null ? ` · ${fmt(s.queries_per_job)} a job` : ''}`, GLOSS.queuecheck],
    ['polling the queue', s.polling_sessions ? `${plural(s.polling_sessions, 'session')} · ${dur(s.polling_s)}` : 'no', 'a session that checked the queue at least every 3 min for 10 min or more'],
    ['longest stretch', l ? `every ${dur(l.every_s)} for ${dur(l.span_s)}` : DASH, 'its longest stretch of checks no more than 3 min apart'],
    ['--parsable', s.parsable_pct != null ? `${pc(s.parsable_pct)} of submissions` : DASH, GLOSS.parsable],
    ['cancels', s.cancels ? fint(s.cancels) : DASH],
    ['GPU·h asked', s.gpu_h != null ? fint(s.gpu_h) : DASH, GLOSS.gpuh],
  ];
  return (
    <dl className="ur-nums">
      {kv.map(([k, v, tip]) => (
        <div key={k}>
          <dt data-tip={tip}>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A session's time by phase, as one thin bar. */
function PhaseBar({ s }: { s: PortraitSession }) {
  const parts = ranked(s.phase_s ?? {}).filter(([k]) => k !== 'idle');
  const tot = parts.reduce((a, [, v]) => a + v, 0);
  if (!tot) return <span className="nm">{s.phase_s ? 'no tool calls' : DASH}</span>;
  return (
    <span className="u-bar ur-pbar" data-tip={parts.map(([k, v]) => `${phaseLbl(k)}: ${dur(v)}`).join('\n')}>
      {parts.map(([k, v]) => (
        <i key={k} style={{ width: `${(100 * v) / tot}%`, background: phaseCol(k) }} />
      ))}
    </span>
  );
}

function Launched({ s }: { s: PortraitSession }) {
  const out: [string, string, string][] = [];
  if (s.approval === 'bypassed') out.push(['bypass', 'crit', AP_LABEL.bypassed + ': ' + GLOSS.bypass]);
  else if (s.approval === 'auto') out.push(['auto mode', 'warn', GLOSS.automode]);
  if (s.sandbox === 'sandboxed' || s.sandbox === 'partial') out.push(['sandboxed', 'ok', SESSION_LABEL[s.sandbox]]);
  return (
    <span className="u-badges">
      {out.map(([t, k, tip]) => (
        <span key={t} className={`u-badge ${k}`} data-tip={tip}>{t}</span>
      ))}
    </span>
  );
}

function Sessions({ p, openTraj }: { p: UserPortrait; openTraj: (sid: string) => void }) {
  const s = useSort<string>('started');
  const rows = s.sort(p.sessions ?? [], (r, k) =>
    k === 'started' ? r.first : k === 'agent' ? agentLbl(r.agent_type) : k === 'project' ? r.headline ?? r.project
      : k === 'open' ? r.wall_s : k === 'calls' ? r.calls : k === 'tasks' ? r.tasks : r.cpu_s);
  const multiHost = new Set((p.sessions ?? []).map((r) => r.host)).size > 1;
  return (
    <div className="scroller vscroll">
      <table className="roomy">
        <thead>
          <tr>
            <SortTh s={s} k="started">started</SortTh>
            <SortTh s={s} k="agent" first="asc">agent</SortTh>
            <SortTh s={s} k="project" first="asc" tip="what kind of session it was, its project, and the names that recur across its tasks, as its row on the Sessions tab heads it">project</SortTh>
            <SortTh s={s} k="open" num tip="open from its first record to its last">open for</SortTh>
            <SortTh s={s} k="calls" num gloss="toolcall">tool calls</SortTh>
            <SortTh s={s} k="tasks" num gloss="tasks">tasks</SortTh>
            <th data-tip={`${GLOSS.phases}. Its time in each, idle left out`}>phases</th>
            <SortTh s={s} k="cpu" num>CPU</SortTh>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.sid}>
              <td className="mono nm">
                {r.first != null ? fullClock(r.first) : DASH}
                {r.running && <span className="ur-live" data-tip="its agent is still sending records"><i />running</span>}
              </td>
              <td>
                <span className="ur-sess">
                  <i className="u-dot" style={{ background: typeCol(r.agent_type) }} />
                  {agentLbl(r.agent_type)}
                  <SurfaceChip s={r.surface} detail={r.surface_detail} />
                  <Launched s={r} />
                  {multiHost && <span className="nm">{r.host}</span>}
                </span>
              </td>
              <td className="wrap mono">
                {r.category && (
                  <>
                    <CategoryChip of={r} />{' '}
                  </>
                )}
                {r.headline ?? r.project ?? <span className="nm">{DASH}</span>}
              </td>
              <td className="num">{dur(r.wall_s)}</td>
              <td className="num">{r.calls == null ? <span className="nm" data-tip="not an agent whose commands the service reads">not read</span> : fint(r.calls)}</td>
              <td className="num">
                {r.tasks == null ? DASH : fint(r.tasks)}
                {r.quoted ? <span className="nm" data-tip="tasks labelled with the agent's own words"> · {fint(r.quoted)} quoted</span> : null}
              </td>
              <td><PhaseBar s={r} /></td>
              <td className="num">{dur(r.cpu_s)}</td>
              <td>
                <button className="linkbtn" onClick={() => openTraj(r.sids[r.sids.length - 1] ?? r.sid)} data-tip="open this session's process tree on the Sessions tab">
                  process tree →
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Portrait({
  name,
  win,
  buildNonce,
  who,
  catalogue,
  close,
  openTraj,
  openUserTraj,
}: {
  name: string;
  win: number | null;
  buildNonce: number;
  who: Who;
  catalogue: TraitRule[];
  close: () => void;
  openTraj: (sid: string) => void;
  openUserTraj: (user: string) => void;
}) {
  const q = useUserPortrait(name, win, buildNonce, who, EVERY_MS);
  const p = q.data?.user === name ? q.data : null;
  const ref = useRef<HTMLDivElement>(null);
  // A portrait opened from the roster is scrolled to once per user, when its
  // answer has come. Scrolled as it mounts, it is one short panel still
  // waiting, the page below it is too short to bring its top up, and nothing
  // scrolls again once it grows: it stayed at the foot of the screen.
  const scrolled = useRef<string | null>(null);
  const arrived = !!p || !!q.error;
  useEffect(() => {
    if (!arrived || scrolled.current === name) return;
    scrolled.current = name;
    ref.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [name, arrived]);
  const winLbl = p?._window?.label ?? null;
  const closeBtn = (
    <button className="btn" onClick={close} data-tip="close this portrait">close</button>
  );
  const row = p?.row;
  const fleetNote =
    p?.fleet_of === 'selection'
      ? 'read against the users your filter keeps: the whole window is not built yet'
      : null;
  return (
    <div className="grid wide ur-portrait" ref={ref}>
      <Panel
        id="portrait"
        title={`Portrait · ${name}`}
        p={p ?? (q.error ? undefined : WAITING)}
        span
        header={
          <>
            <button className="linkbtn" onClick={() => openUserTraj(name)} data-tip="this user's sessions on the Sessions tab">
              their sessions →
            </button>
            {closeBtn}
          </>
        }
        empty={!!p && p.found === false}
        emptyWhat={`${name} ran no agent session in the ${winLbl ?? 'window'}`}
      >
        {q.error && <div className="err">{q.error}</div>}
        {p?.found == null && !q.error && p && <div className="none">this window is still being read</div>}
        {row && p && <Head p={p} row={row} catalogue={catalogue} />}
        {fleetNote && <div className="nm">{fleetNote}</div>}
      </Panel>
      {row && p && (
        <>
          <Panel title="How they launch agents" id="portrait-launch">
            <Launch row={row} fleet={p.fleet} />
          </Panel>
          <Panel title="Rhythm" id="portrait-rhythm">
            <Rhythm p={p} row={row} />
          </Panel>
          <Panel title="What their agents do" id="portrait-work">
            <Work p={p} row={row} />
          </Panel>
          <Panel title="Projects and tasks" id="portrait-tasks">
            <Tasks p={p} row={row} openTraj={openTraj} />
          </Panel>
          {row.slurm && (
            <Panel title="At the scheduler" id="portrait-slurm">
              <Scheduler row={row} />
            </Panel>
          )}
          <Panel
            title={`Sessions · ${name}`}
            id="portrait-sessions"
            span
            empty={!(p.sessions ?? []).length}
            emptyWhat="no session"
          >
            <Sessions p={p} openTraj={openTraj} />
            {(p.sessions_total ?? 0) > (p.sessions ?? []).length && (
              <p className="paths">
                the newest {fint(p.sessions?.length ?? 0)} of {plural(p.sessions_total ?? 0, 'session')} ·{' '}
                <button className="linkbtn" onClick={() => openUserTraj(name)}>every session on the Sessions tab →</button>
              </p>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- the tab */

export const UsersTab = memo(function UsersTab({
  win,
  buildNonce,
  who,
  cls,
  open,
  setOpen,
  openTraj,
  openUserTraj,
}: {
  win: number | null;
  buildNonce: number;
  who: Who;
  cls: Cls[];
  /** the user whose portrait is open (`?portrait=`) */
  open: string | null;
  setOpen: (u: string | null) => void;
  openTraj: (sid: string) => void;
  openUserTraj: (user: string) => void;
}) {
  const users = useUsers(win, buildNonce, who, EVERY_MS);
  const U = users.data;
  const rows = useMemo(() => U?.users ?? [], [U]);
  const agentShown = cls.includes('agent');
  return (
    <>
      {U && <Plates U={U} rows={rows} />}
      <div className="grid wide">
        <Panel
          id="users-roster"
          title="Users and their agents"
          p={U ?? (users.error ? undefined : WAITING)}
          span
          empty={!agentShown || (!!U && !rows.length)}
          emptyWhat={
            !agentShown
              ? 'the agent class is hidden by the class filter; this tab is about agents'
              : 'no user ran an agent session in the window'
          }
        >
          {users.error && <div className="err">/api/users: {users.error}</div>}
          {U && <Roster U={U} rows={rows} open={open} setOpen={setOpen} />}
        </Panel>
      </div>
      {open && agentShown && (
        <>
          <div className="grid-gap" />
          <Portrait
            name={open}
            win={win}
            buildNonce={buildNonce}
            who={who}
            catalogue={U?.traits ?? []}
            close={() => setOpen(null)}
            openTraj={openTraj}
            openUserTraj={openUserTraj}
          />
        </>
      )}
    </>
  );
});
