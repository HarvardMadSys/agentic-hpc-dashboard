/* Live tier.
 *
 * Websocket-driven, with nothing invented between frames. The rate chart plots
 * `null` bins as gaps: a minute in which the collector wrote no record is a hole
 * in the line, because a 0 there would be a claim that the cluster was idle.
 */
import { useMemo } from 'react';
import type { Cls, FeedReport, FeedsResponse, LiveEvent, LiveHost, LiveResponse } from '../api/types';
import type { LiveState } from '../api/useLive';
import { Lines } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { Basis, EmptyState, Legend, NoRows, Panel } from '../components/Panel';
import { Plate } from '../components/Plate';
import { CLS, CLS_ABBR, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt, lagStr, pc } from '../lib/format';
import { pcol, plbl, purposeLegend } from '../lib/purposes';

/* ------------------------------------------------------------- helpers */

/** Sum of a rate column over the window, skipping gaps. `null` when all gaps. */
function windowTotal(rate: LiveResponse['rate'], c: string): number | null {
  let any = false;
  let s = 0;
  for (const r of rate) {
    const v = r[c];
    if (typeof v === 'number') {
      any = true;
      s += v;
    }
  }
  return any ? s : null;
}

/** The newest bin that actually carried a value for this class. */
function latest(rate: LiveResponse['rate'], c: string): number | null {
  for (let i = rate.length - 1; i >= 0; i--) {
    const v = rate[i][c];
    if (typeof v === 'number') return v;
  }
  return null;
}

function nowNum(now: LiveResponse['now'], keys: string[]): number | null {
  for (const k of keys) {
    const v = now?.[k];
    if (typeof v === 'number') return v;
  }
  return null;
}

/** Per-class value out of `now`, trying `<key>_<cls>` then `<key>.<cls>` maps. */
function nowByClass(now: LiveResponse['now'], key: string): Partial<Record<string, number | null>> {
  const out: Partial<Record<string, number | null>> = {};
  const nested = now?.[key] as unknown;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    for (const c of CLS) {
      const v = (nested as Record<string, unknown>)[c];
      if (typeof v === 'number') out[c] = v;
    }
    if (Object.keys(out).length) return out;
  }
  for (const c of CLS) {
    const flat = c.replace('-', '_');
    const v = nowNum(now, [`${key}_${flat}`, `${key}_${c}`]);
    if (v != null) out[c] = v;
  }
  return out;
}

const HOST_COLS: [key: string, lbl: string, kind: 'int' | 'num' | 'pct'][] = [
  ['roots', 'roots', 'int'],
  ['procs', 'procs', 'int'],
  ['events', 'events', 'int'],
  ['rss_gb', 'RSS GB', 'num'],
  ['cpu_pct', 'CPU', 'pct'],
  ['load_1', 'load₁', 'num'],
  ['d_state', 'D-state', 'int'],
  ['users', 'users', 'int'],
  ['tcp_open_ext', 'ext TCP', 'int'],
];

function hostCell(h: LiveHost, key: string, kind: string) {
  const v = h[key];
  if (typeof v !== 'number') return DASH;
  if (kind === 'pct') return fmt(v) + '%';
  if (kind === 'num') return fmt(v);
  if (key === 'd_state' && v > 0) return <span style={{ color: 'var(--crit)' }}>{fint(v)}</span>;
  return fint(v);
}

/** Per-class split for one host, if the payload carries one. */
function hostByClass(h: LiveHost): Partial<Record<string, number>> | null {
  const b = (h.by_class ?? h.classes ?? h.events_by_class) as unknown;
  if (b && typeof b === 'object' && !Array.isArray(b)) {
    const out: Record<string, number> = {};
    for (const c of CLS) {
      const v = (b as Record<string, unknown>)[c];
      if (typeof v === 'number') out[c] = v;
    }
    if (Object.keys(out).length) return out;
  }
  const out: Record<string, number> = {};
  for (const c of CLS) {
    const v = h[`roots_${c.replace('-', '_')}`] ?? h[`events_${c.replace('-', '_')}`];
    if (typeof v === 'number') out[c] = v;
  }
  return Object.keys(out).length ? out : null;
}

/* --------------------------------------------------------------- the tab */

export function LiveTab({
  live,
  cls,
  feeds,
  goto,
}: {
  live: LiveState;
  cls: Cls[];
  feeds: FeedsResponse | null;
  goto: (tab: string) => void;
}) {
  const L = live.live;
  const rate = L?.rate ?? [];
  const now = L?.now ?? {};

  /* The live feed report, for the staleness line on the rate panel. */
  const liveFeed: FeedReport | null = useMemo(() => {
    if (!feeds) return null;
    const vals = Object.values(feeds);
    return (
      vals.find((f) => f.tier === 'process' && f.required) ??
      vals.find((f) => f.cadence === 'event') ??
      vals.find((f) => f.required) ??
      vals[0] ??
      null
    );
  }, [feeds]);

  const series: LineSeries[] = cls.map((c) => ({
    lbl: CLS_LBL[c],
    c: CLS_COL[c],
    area: c === 'agent',
    mark: c === 'agent' ? (latest(rate, c) != null ? fint(latest(rate, c)) + '/min' : null) : null,
    pts: rate.map((r, i) => ({ x: i, y: typeof r[c] === 'number' ? (r[c] as number) : null })),
  }));

  /* hourly x ticks over a 24 h / per-minute window */
  const xt = useMemo(
    () => rate.map((_, i) => i).filter((i) => rate[i].t?.endsWith(':00') && i % 120 === 0),
    [rate],
  );

  const gaps = useMemo(
    () => rate.filter((r) => cls.every((c) => typeof r[c] !== 'number')).length,
    [rate, cls],
  );

  const totals = Object.fromEntries(CLS.map((c) => [c, windowTotal(rate, c)]));
  const windowAll = CLS.reduce<number | null>((a, c) => {
    const v = totals[c];
    return v == null ? a : (a ?? 0) + v;
  }, null);
  const perMin = Object.fromEntries(CLS.map((c) => [c, latest(rate, c)]));
  const perMinAll = CLS.reduce<number | null>((a, c) => {
    const v = perMin[c];
    return v == null ? a : (a ?? 0) + v;
  }, null);

  const roots = nowByClass(now, 'roots');
  const rootsTot = nowNum(now, ['agent_roots', 'roots_total', 'roots']) ??
    (Object.keys(roots).length
      ? CLS.reduce((a, c) => a + (roots[c] ?? 0), 0)
      : null);
  const procs = nowByClass(now, 'procs');
  const procsTot = nowNum(now, ['procs_total', 'agent_procs', 'procs']) ??
    (Object.keys(procs).length ? CLS.reduce((a, c) => a + (procs[c] ?? 0), 0) : null);
  const users = nowByClass(now, 'users');
  const usersTot = nowNum(now, ['users_total', 'users']) ??
    (Object.keys(users).length ? CLS.reduce((a, c) => a + (users[c] ?? 0), 0) : null);

  const events = (L?.events ?? []).filter((e) => !e.actor3 || cls.includes(e.actor3 as Cls));
  const submits = L?.submits ?? [];
  const hosts = L?.hosts ?? [];

  const envelope = L
    ? null
    : ({
        _feed: liveFeed?.name ?? 'live',
        _status: (liveFeed?.status ?? 'missing') as FeedReport['status'],
        _present: false,
        _paths: liveFeed?.resolved?.length ? liveFeed.resolved : (liveFeed?.configured ?? []),
        _notice: liveFeed?.notice ?? 'no live payload has arrived yet',
      } as const);

  if (envelope) {
    return (
      <div className="grid wide">
        <Panel title="Live tier" span feed={envelope._feed}>
          <EmptyState
            feed={envelope._feed}
            paths={[...envelope._paths]}
            status={envelope._status}
            notice={envelope._notice}
          />
        </Panel>
      </div>
    );
  }

  const windowMin = L?.window?.minutes ?? rate.length;

  return (
    <>
      <div className="plates">
        <Plate
          k="process exits / min"
          total={perMinAll}
          stripe="var(--s1)"
          by={perMin}
          note={
            <>
              newest complete minute
              {perMinAll != null && perMin.agent != null
                ? ` · ${pc((100 * perMin.agent) / (perMinAll || 1))} agent`
                : ''}
            </>
          }
        />
        <Plate
          k={`exits in window (${fint(windowMin)} min)`}
          total={windowAll}
          stripe="var(--s2)"
          by={totals}
          note={
            <>
              {fint(rate.length - gaps)} of {fint(rate.length)} bins carried a record
              {gaps ? ` · ${fint(gaps)} gaps` : ''}
            </>
          }
        />
        <Plate
          k="resident roots"
          total={rootsTot}
          stripe="var(--s4)"
          by={roots}
          note={
            <>
              {procsTot != null ? `${fint(procsTot)} processes` : 'processes not reported'}
              {nowNum(now, ['rss_gb']) != null ? ` · ${fmt(nowNum(now, ['rss_gb']))} GB resident` : ''}
              {nowNum(now, ['autonomous_roots']) != null
                ? ` · ${fint(nowNum(now, ['autonomous_roots']))} with a bypass flag`
                : ''}
            </>
          }
        />
        <Plate
          k="distinct users on the fleet"
          total={usersTot}
          stripe="var(--s5)"
          by={users}
          note={
            <>
              {fint(hosts.length)} hosts reporting
              {liveFeed?.lag_s != null ? ` · newest record ${lagStr(liveFeed.lag_s)} old` : ''}
            </>
          }
        />
      </div>

      <div className="grid wide">
        <Panel
          title={`Process exits per minute · last ${fint(windowMin)} minutes`}
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!rate.length}
          emptyWhat="the live window holds no bins"
          header={
            <span className="pill" data-tip="a bin with no record is drawn as a gap, never as 0">
              {fint(gaps)} gap bins
            </span>
          }
          basis={
            <Basis
              items={[
                ['window', L?.window ? JSON.stringify(L.window) : null],
                ['transport', live.transport],
                ['gaps', 'a minute with no record is null and is drawn as a hole in the line'],
                ['backfill', L?.backfill ? JSON.stringify(L.backfill) : null],
              ]}
            />
          }
        >
          <Lines
            series={series}
            W={1060}
            H={230}
            ymin={0}
            xticks={xt.length ? xt : undefined}
            xfmt={(i) => rate[Math.round(i)]?.t ?? ''}
            ylabel="exits / min"
            xlabel={`${fint(windowMin)} minutes`}
            xname="at"
            yname="exits/min"
          />
          <Legend items={series.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        <Panel
          title="Login nodes right now"
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!hosts.length}
          emptyWhat="no host reported in the window"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>host</th>
                  {HOST_COLS.filter((c) => hosts.some((h) => typeof h[c[0]] === 'number')).map(
                    (c) => (
                      <th key={c[0]} className="num">
                        {c[1]}
                      </th>
                    ),
                  )}
                  <th>by class</th>
                </tr>
              </thead>
              <tbody>
                {hosts.map((h) => {
                  const bc = hostByClass(h);
                  return (
                    <tr key={h.host}>
                      <td className="mono">{h.host}</td>
                      {HOST_COLS.filter((c) => hosts.some((x) => typeof x[c[0]] === 'number')).map(
                        (c) => (
                          <td key={c[0]} className="num">
                            {hostCell(h, c[0], c[2])}
                          </td>
                        ),
                      )}
                      <td>
                        {bc ? (
                          <span className="rle">
                            {cls.map((c, i) => (
                              <span key={c}>
                                {i ? <span className="sep">{' · '}</span> : null}
                                <i
                                  className="dot"
                                  style={{ background: CLS_COL[c], marginRight: 4 }}
                                />
                                <b>{c in bc ? fint(bc[c]) : DASH}</b>
                              </span>
                            ))}
                          </span>
                        ) : (
                          <span className="nm">not reported per class</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Recent sbatch / salloc"
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!submits.length}
          emptyWhat="no submission in the window"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>at</th>
                  <th>user</th>
                  <th>product</th>
                  <th>tool</th>
                  <th className="num">job</th>
                  <th>partition</th>
                  <th className="num">gpu</th>
                  <th>array</th>
                </tr>
              </thead>
              <tbody>
                {submits.map((r, i) => (
                  <tr key={`${r.job_id ?? ''}-${i}`}>
                    <td className="mono" style={{ color: 'var(--muted)' }}>
                      {r.ts ?? DASH}
                    </td>
                    <td className="mono">{r.user ?? DASH}</td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>
                      {r.agent_type ?? DASH}
                    </td>
                    <td className="mono">{r.tool ?? DASH}</td>
                    <td className="num">{r.job_id ?? DASH}</td>
                    <td className="mono">{r.partition ?? DASH}</td>
                    <td className="num">{r.gpus ?? DASH}</td>
                    <td className="mono">{r.array ?? DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Process exit stream"
          span
          live
          feed={liveFeed?.name ?? 'live'}
          empty={!events.length}
          emptyWhat={
            (L?.events?.length ?? 0) > 0
              ? 'no event in the window matches the class filter'
              : 'the bounded tail is empty'
          }
          header={
            <>
              <span className="pill">
                {fint(events.length)} of {fint(L?.events?.length ?? 0)} buffered
              </span>
              <button className="btn" onClick={() => goto('risk')} data-tip="filter and export the full day-files">
                search & export {'→'}
              </button>
            </>
          }
        >
          <div className="stream">
            <EventTable rows={events} />
          </div>
          <Legend items={purposeLegend()} />
        </Panel>
      </div>
    </>
  );
}

/* Shared by the live stream and the /api/events explorer, so a row looks the
 * same however it was fetched. Class is tagged as TEXT, not colour: crimson is
 * reserved for agent identity, so purpose owns the colour here. */
export function EventTable({ rows }: { rows: LiveEvent[] }) {
  if (!rows.length) return <NoRows />;
  return (
    <table>
      <thead>
        <tr>
          <th>at</th>
          <th>class</th>
          <th>user</th>
          <th className="num">pid</th>
          <th>host</th>
          <th className="num">d</th>
          <th>command</th>
          <th className="num">ran</th>
          <th className="num">cpu</th>
          <th className="num">exit</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e, i) => {
          const comm = e.comm ?? e.tool ?? '';
          const args = e.args ?? '';
          const tail = args.startsWith(comm) ? args.slice(comm.length) : args ? ' ' + args : '';
          return (
            <tr key={`${e.epoch ?? e.ts ?? ''}-${i}`}>
              <td className="mono" style={{ color: 'var(--muted)' }}>
                {e.ts ?? DASH}
              </td>
              <td
                className="mono"
                style={{ fontSize: '10px', color: 'var(--muted)' }}
                data-tip={`${CLS_LBL[e.actor3 ?? ''] ?? e.actor3 ?? 'unlabeled'}${
                  e.agent_type ? ' · ' + e.agent_type : ''
                }`}
              >
                {CLS_ABBR[e.actor3 ?? ''] ?? '?'}
              </td>
              <td className="mono">{e.user ?? DASH}</td>
              <td
                className="num mono"
                style={{ fontSize: '10.5px' }}
                data-tip={
                  e.ppid != null
                    ? `ppid ${e.ppid}${e.session_key ? ' \u00b7 ' + e.session_key : ''}`
                    : e.session_key ?? undefined
                }
              >
                {e.pid ?? DASH}
              </td>
              <td className="mono" style={{ color: 'var(--muted)' }}>
                {(e.host ?? '').replace('login', '') || DASH}
              </td>
              <td className="num">{e.depth == null ? DASH : 'd' + e.depth}</td>
              <td className="arg" data-tip={e.purpose ? plbl(e.purpose) : undefined}>
                <span className="cmd" style={{ color: pcol(e.purpose) }}>
                  {comm || DASH}
                </span>
                <span style={{ color: 'var(--ink-2)' }}>{tail}</span>
              </td>
              <td className="num">{dur(e.duration_s)}</td>
              <td className="num">
                {e.cpu_s == null ? DASH : e.cpu_s < 0.01 ? '<0.01s' : e.cpu_s.toFixed(2) + 's'}
              </td>
              <td className="num">
                {e.signal ? (
                  <span style={{ color: 'var(--warn)' }}>sig{e.signal}</span>
                ) : e.exit_code == null ? (
                  DASH
                ) : e.exit_code ? (
                  <span style={{ color: 'var(--crit)' }}>{e.exit_code}</span>
                ) : (
                  '0'
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
