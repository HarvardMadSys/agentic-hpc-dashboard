/* Trend -- each class's use over up to a year, from the roll-up table.
 *
 * "Is agent use growing over the term, and among whom?" is not a question any
 * window answers: the live tier holds a week. The service sums every exit and
 * Slurm submission it ingests into its local day (rc_dashboard/rollup.py) and
 * writes each day to a table once it has read all of it, so the days outlive
 * retention and the day-files. This tab reads that table, `/api/trend`.
 *
 * Four rules, the page's own:
 *  - A day, week or month the table holds no record of is a hole in the line,
 *    never a 0. Stretches before the table's first day are not drawn at all.
 *  - Processes and CPU are a mean per day over the bucket's days with data, so
 *    the week still going and the month the range starts inside sit on the
 *    same scale as a whole one. Users are distinct accounts over the whole
 *    bucket -- a person on five days of a week is one user that week -- so a
 *    partial bucket is marked, not scaled.
 *  - Count and cost side by side: processes and CPU-hours on their own axes.
 *  - Not the window and not the filter: it is everyone, every day held.
 */
import { useMemo, useState } from 'react';
import type { Cls, TrendBucket, TrendResponse } from '../api/types';
import { useTrend } from '../api/hooks';
import { Fluid, Lines, ZOOM_HINT } from '../charts/primitives';
import type { LineSeries } from '../charts/primitives';
import { TOOL_COL } from '../charts/TimeChart';
import { Segmented } from '../components/ClassFilter';
import { Legend, Panel } from '../components/Panel';
import { SortTh } from '../components/SortTh';
import { AGENT_TYPE_COL, AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
import type { Who } from '../lib/filters';
import { useSort } from '../lib/useSort';

type By = 'day' | 'week' | 'month';
type Read = 'abs' | 'share';
type View = [number, number] | null;

const RANGES = [
  { k: '30', lbl: '30 d', tip: 'the last 30 days' },
  { k: '90', lbl: '90 d', tip: 'the last 90 days' },
  { k: '180', lbl: '180 d', tip: 'the last 180 days' },
  { k: '365', lbl: '1 y', tip: 'the last 365 days' },
];
const BYS: { k: By; lbl: string; tip: string }[] = [
  { k: 'day', lbl: 'by day', tip: 'one point a local day' },
  { k: 'week', lbl: 'by week', tip: 'one point a week, Monday to Sunday' },
  { k: 'month', lbl: 'by month', tip: 'one point a calendar month' },
];
const DAY_S = 86400;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
/** Agent types drawn by name; the rest are left to the table. */
const TYPES_SHOWN = 6;
const HOSTS_SHOWN = 6;
/** The table is written a minute at a time; read it again this often. */
const EVERY_MS = 120000;

/** Ticks on local midnights: a day, a week (Mondays), or a month's first,
 *  whichever puts at most `target` across the span. */
function dateTicks(x0: number, x1: number, target: number): number[] {
  const spanD = (x1 - x0) / DAY_S;
  const out: number[] = [];
  const d = new Date(x0 * 1000);
  d.setHours(0, 0, 0, 0);
  if (spanD / 7 > target) {
    // months, every 1, 2, 3 or 6 of them
    const every = [1, 2, 3, 6, 12].find((m) => spanD / 30.4 / m <= target) ?? 12;
    d.setDate(1);
    while (d.getTime() / 1000 < x0 || d.getMonth() % every) d.setMonth(d.getMonth() + 1);
    for (; d.getTime() / 1000 <= x1; d.setMonth(d.getMonth() + every)) out.push(d.getTime() / 1000);
  } else if (spanD > target) {
    const every = Math.max(1, Math.ceil(spanD / 7 / target)) * 7;
    while (d.getDay() !== 1 || d.getTime() / 1000 < x0) d.setDate(d.getDate() + 1);
    for (; d.getTime() / 1000 <= x1; d.setDate(d.getDate() + every)) out.push(d.getTime() / 1000);
  } else {
    while (d.getTime() / 1000 < x0) d.setDate(d.getDate() + 1);
    for (; d.getTime() / 1000 <= x1; d.setDate(d.getDate() + 1)) out.push(d.getTime() / 1000);
  }
  return out.length ? out : [x0];
}

/** A tick: the month's name on its first day, the day of the month otherwise. */
function dateLabel(e: number): string {
  const d = new Date(e * 1000);
  if (d.getDate() === 1) return d.getMonth() === 0 ? String(d.getFullYear()) : MONTHS[d.getMonth()];
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

/** What a bucket is called, in a hover and in the table. */
function bucketName(b: TrendBucket, by: By): string {
  if (by === 'month') {
    const d = new Date(`${b.key}T12:00:00`);
    return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  }
  if (by === 'week') return `week of ${b.key}`;
  return `${b.key} ${WEEKDAY[new Date(`${b.key}T12:00:00`).getDay()]}`;
}

/** What about a bucket is partial, in a few words. */
function bucketNote(b: TrendBucket, by: By): string {
  if (!b.data_days) return 'no data';
  const bits: string[] = [];
  if (b.today) bits.push(by === 'day' ? 'so far' : 'still going');
  else if (b.complete_days < b.days) bits.push(by === 'day' ? 'part' : `${b.complete_days} of ${b.days} days whole`);
  if (b.cut && by !== 'day') bits.push(`from ${b.from.slice(5)}`);
  if (by !== 'day' && b.data_days < b.days) bits.push(`${b.data_days} of ${b.days} days with data`);
  return bits.join(' · ');
}

function bucketTip(b: TrendBucket, by: By): string {
  const lines = [bucketName(b, by) + (b.from !== b.to ? ` (${b.from} to ${b.to})` : '')];
  if (!b.data_days) lines.push('the table holds no record of it: no data, not a quiet stretch');
  else {
    lines.push(`${fint(b.held)} of ${fint(b.minutes)} minutes held an exit (${pc((100 * b.held) / Math.max(1, b.minutes))})`);
    if (b.complete_days < b.days) lines.push(`${b.complete_days} of its ${b.days} days read whole; the rest still going or read in part`);
  }
  return lines.join('\n');
}

interface Line {
  k: string;
  lbl: string;
  c: string;
  v: (b: TrendBucket) => number | null;
}

/** One chart of the tab: a line a series, a point a bucket, a hole where the
 *  table holds nothing. Every chart takes the same `view`, so a zoom into one
 *  is a zoom into all. */
function TrendChart({
  bs,
  lines,
  by,
  ylabel,
  yname,
  yfmt,
  view,
  onView,
}: {
  bs: TrendBucket[];
  lines: Line[];
  by: By;
  ylabel: string;
  yname: string;
  yfmt?: (v: number) => string;
  view: View;
  onView: (v: View) => void;
}) {
  const names = useMemo(() => new Map(bs.map((b) => [b.t, bucketName(b, by)])), [bs, by]);
  const series: LineSeries[] = lines.map((l) => ({
    lbl: l.lbl,
    c: l.c,
    pts: bs.map((b) => ({ x: b.t, y: b.data_days ? l.v(b) : null })),
  }));
  const x0 = bs.length ? bs[0].t : 0;
  const x1 = bs.length ? bs[bs.length - 1].t : 0;
  const step = by === 'month' ? 31 * DAY_S : by === 'week' ? 7 * DAY_S : DAY_S;
  return (
    <>
      <Fluid natural={520}>
        {(W) => (
          <Lines
            series={series}
            W={W}
            H={210}
            ymin={0}
            yfmt={yfmt}
            xmin={x0}
            xmax={x1 > x0 ? x1 : x0 + step}
            xticks={(a, b) => dateTicks(a, b, Math.max(3, Math.min(8, Math.floor((W - 60) / 70))))}
            xfmt={dateLabel}
            xtip={(x) => names.get(x) ?? dateLabel(x)}
            ylabel={ylabel}
            xname={by}
            yname={yname}
            view={view}
            onView={onView}
            minSpan={3 * step}
          />
        )}
      </Fluid>
      <Legend items={lines.map((l) => ({ k: l.lbl, c: l.c }))} />
    </>
  );
}

export function TrendTab({ cls, who }: { cls: Cls[]; who: Who | null }) {
  const [range, setRange] = useState('365');
  const [by, setBy] = useState<By>('week');
  const [read, setRead] = useState<Read>('abs');
  const [view, setView] = useState<View>(null);
  const t = useTrend(Number(range), by, EVERY_MS);
  const d: TrendResponse | null = t.data;
  const s = useSort<string>('period', 'desc');

  // Nothing is drawn before the table's first bucket with a record: those
  // stretches were never held, and drawn they would read as a quiet start.
  const all = d?.buckets ?? [];
  const first = all.findIndex((b) => b.data_days > 0);
  const bs = first < 0 ? [] : all.slice(first);
  const have = cls.filter((c) => bs.some((b) => b.by_class[c]));
  const filtered = !!(who && (who.users.length || who.atypes.length));

  const perDay = (b: TrendBucket, v: number) => v / Math.max(1, b.data_days);
  const total = (b: TrendBucket, f: 'n' | 'cpu_s') =>
    Object.values(b.by_class).reduce((a, c) => a + (c?.[f] ?? 0), 0);
  const val = (b: TrendBucket, c: string, f: 'n' | 'cpu_s') => {
    const v = b.by_class[c]?.[f] ?? 0;
    if (read === 'share') {
      const tot = total(b, f);
      return tot ? (100 * v) / tot : null;
    }
    return perDay(b, f === 'cpu_s' ? v / 3600 : v);
  };
  const classLines = (f: 'n' | 'cpu_s'): Line[] =>
    have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: (b) => val(b, c, f) }));
  const userLines: Line[] = [
    { k: 'all', lbl: 'anyone', c: 'var(--sg)', v: (b) => b.users },
    ...have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: (b: TrendBucket) => b.by_class[c]?.users ?? 0 })),
  ];

  // agent types and hosts, the busiest over the range by name
  const rank = (get: (b: TrendBucket) => Record<string, { users?: number; n: number }>, f: 'users' | 'n') => {
    const sum = new Map<string, number>();
    for (const b of bs) for (const [k, v] of Object.entries(get(b))) sum.set(k, (sum.get(k) ?? 0) + ((v as Record<string, number>)[f] ?? 0));
    return [...sum.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  };
  const types = rank((b) => b.types, 'users');
  const hosts = rank((b) => b.hosts, 'n');
  const typeLines: Line[] = types.slice(0, TYPES_SHOWN).map((ty, i) => ({
    k: ty,
    lbl: AGENT_TYPE_LBL[ty] ?? ty,
    c: AGENT_TYPE_COL[ty] ?? TOOL_COL[i % TOOL_COL.length],
    v: (b) => b.types[ty]?.users ?? 0,
  }));
  const hostLines: Line[] = hosts.slice(0, HOSTS_SHOWN).map((h, i) => ({
    k: h,
    lbl: h,
    c: TOOL_COL[i % TOOL_COL.length],
    v: (b) => (b.hosts[h] ? perDay(b, b.hosts[h].n) : 0),
  }));
  if (hosts.length > HOSTS_SHOWN) {
    const rest = new Set(hosts.slice(HOSTS_SHOWN));
    hostLines.push({
      k: 'other', lbl: `${hosts.length - HOSTS_SHOWN} more`, c: 'var(--sg)',
      v: (b) => perDay(b, Object.entries(b.hosts).reduce((a, [h, v]) => a + (rest.has(h) ? v.n : 0), 0)),
    });
  }

  const rows = s.sort([...bs], (b, k) =>
    k === 'period' ? b.key : k === 'held' ? (b.data_days ? b.held / Math.max(1, b.minutes) : null)
      : k === 'users' ? b.users
      : k.startsWith('u:') ? (b.data_days ? b.by_class[k.slice(2)]?.users ?? 0 : null)
      : k.startsWith('n:') ? (b.data_days ? perDay(b, b.by_class[k.slice(2)]?.n ?? 0) : null)
      : k === 'submits' ? (b.data_days ? Object.values(b.by_class).reduce((a, c) => a + c.submits, 0) : null)
      : (b.data_days ? b.types[k.slice(2)]?.users ?? 0 : null));

  const tbl = d?.table;
  const shareTip = 'each class’s share of the processes or CPU of its days, out of every class';
  const header = (
    <span className="wrapctl" style={{ gap: 6 }}>
      {tbl && tbl.days > 0 && (
        <span className="pill" data-tip={`the roll-up table holds ${fint(tbl.days)} day(s), ${fint(tbl.complete)} of them read whole; days start at local midnight${d?.tz ? `, ${d.tz}` : ''}`}>
          table from {tbl.oldest}
        </span>
      )}
      {d?.state === 'reading' && (
        <span className="pill stale" data-tip="the service is still reading the history behind its start; a day is written to the table only once it has read all of it">
          reading history
        </span>
      )}
      {d?.state === 'error' && (
        <span className="pill stale" data-tip={`the table could not be written: ${d.error ?? ''}. What the service reads is held in memory and written at the next try.`}>
          not written
        </span>
      )}
      {filtered && (
        <span className="pill stale" data-tip="the trend comes from the roll-up table, which the user and agent-type filter does not narrow">
          everyone, not the filter
        </span>
      )}
    </span>
  );
  const controls = (
    <span className="wrapctl" style={{ gap: 6 }}>
      <Segmented value={range} onChange={(v) => { setRange(v); setView(null); }} options={RANGES} label="range" />
      <Segmented value={by} onChange={(v) => { setBy(v); setView(null); }} options={BYS} label="points" />
      <Segmented value={read} onChange={setRead} options={[
        { k: 'abs', lbl: 'how many', tip: 'processes and CPU-hours a day, the mean over each point’s days with data' },
        { k: 'share', lbl: 'each class’s share', tip: shareTip },
      ]} />
    </span>
  );

  const off = d && !d.enabled;
  const emptyWhat = t.error
    ? `the trend could not be read: ${t.error}`
    : off
      ? `${d?.note ?? 'the roll-up table is off'}`
      : d?.error
        ? `the roll-up table could not be opened: ${d.error}`
        : 'the roll-up table holds no day yet. It fills as the service reads the feed, a day at a time; '
          + 'days still on disk from before can be filled with `python -m rc_dashboard rollup --rollup-days 365`';
  const empty = !bs.length;
  const zoom = <span className="nm" data-tip={ZOOM_HINT}>{view ? 'zoomed · double-click to reset' : ''}</span>;

  return (
    <div className="grid wide">
      <Panel title="Over the year" id="sec-trend" span empty={empty && !t.loading} emptyWhat={emptyWhat} header={<>{header}{controls}</>}>
        <p className="nm" style={{ margin: '0 0 8px' }}>
          Every exit and Slurm submission the service has read, summed into its local day as it was read
          and kept in a table past the live tier’s retention, so the line reaches as far back as the table does.
          Processes and CPU are a mean per day over each point’s days with data; users are distinct accounts over the whole point.
          A gap is a stretch the table holds no record of, not a quiet one. {zoom}
        </p>
        <div className="pair-row">
          <div>
            <div className="eyebrow">{read === 'share' ? 'share of the processes that exited' : 'processes that exited, a day'}</div>
            <TrendChart bs={bs} by={by} lines={classLines('n')} view={view} onView={setView}
              ylabel={read === 'share' ? '% of processes' : 'processes / day'} yname={read === 'share' ? '% of processes' : 'processes a day'}
              yfmt={read === 'share' ? (v) => `${fmt(v)}%` : undefined} />
          </div>
          <div>
            <div className="eyebrow">{read === 'share' ? 'share of the CPU' : 'CPU time, a day'}</div>
            <TrendChart bs={bs} by={by} lines={classLines('cpu_s')} view={view} onView={setView}
              ylabel={read === 'share' ? '% of CPU' : 'CPU-hours / day'} yname={read === 'share' ? '% of CPU' : 'CPU-hours a day'}
              yfmt={read === 'share' ? (v) => `${fmt(v)}%` : undefined} />
          </div>
        </div>
        <div className="pair-row" style={{ marginTop: 12 }}>
          <div>
            <div className="eyebrow">users</div>
            <TrendChart bs={bs} by={by} lines={userLines} view={view} onView={setView} ylabel="accounts" yname="accounts" />
          </div>
          <div>
            <div className="eyebrow">users of each agent</div>
            {typeLines.length
              ? <TrendChart bs={bs} by={by} lines={typeLines} view={view} onView={setView} ylabel="accounts" yname="accounts" />
              : <p className="nm">no agent ran in the range</p>}
          </div>
        </div>
        {hostLines.length > 1 && (
          <div style={{ marginTop: 12 }}>
            <div className="eyebrow">processes a day, by login node</div>
            <TrendChart bs={bs} by={by} lines={hostLines} view={view} onView={setView} ylabel="processes / day" yname="processes a day" />
          </div>
        )}
      </Panel>

      <Panel title={by === 'day' ? 'Each day' : by === 'week' ? 'Each week' : 'Each month'} id="sec-trend-table" span empty={empty && !t.loading} emptyWhat={emptyWhat}>
        <div className="scroller vscroll" style={{ maxHeight: 520 }}>
          <table className="roomy">
            <thead>
              <tr>
                <SortTh s={s} k="period" first="desc">{by}</SortTh>
                <SortTh s={s} k="held" num tip="minutes the collector wrote any exit in, of the minutes the table could hold">minutes held</SortTh>
                <SortTh s={s} k="users" num tip="distinct accounts that ran anything, over the whole point">users</SortTh>
                {have.map((c) => (
                  <SortTh key={`u:${c}`} s={s} k={`u:${c}`} num className="cls" style={{ color: CLS_COL[c] }}
                    tip={`distinct accounts that ran something as ${CLS_LBL[c] ?? c}`}>
                    {CLS_LBL[c] ?? c} users
                  </SortTh>
                ))}
                {have.map((c) => (
                  <SortTh key={`n:${c}`} s={s} k={`n:${c}`} num className="cls" style={{ color: CLS_COL[c] }}
                    tip={`processes of the ${CLS_LBL[c] ?? c} class that exited, a day (the mean over the days with data)`}>
                    {CLS_LBL[c] ?? c} / day
                  </SortTh>
                ))}
                <SortTh s={s} k="submits" num tip="Slurm submissions the collector saw, every class">submits</SortTh>
                {types.slice(0, 4).map((ty) => (
                  <SortTh key={ty} s={s} k={`t:${ty}`} num tip={`distinct accounts that ran ${AGENT_TYPE_LBL[ty] ?? ty}`}>
                    {AGENT_TYPE_LBL[ty] ?? ty}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.key}>
                  <td className="mono" data-tip={bucketTip(b, by)}>
                    {by === 'month' ? bucketName(b, by) : b.key} <span className="nm">{bucketNote(b, by)}</span>
                  </td>
                  <td className="num">{b.data_days ? pc((100 * b.held) / Math.max(1, b.minutes)) : <span className="nm">no data</span>}</td>
                  <td className="num">{b.users == null ? DASH : fint(b.users)}</td>
                  {have.map((c) => (
                    <td key={`u:${c}`} className="num">{b.data_days ? fint(b.by_class[c]?.users ?? 0) : DASH}</td>
                  ))}
                  {have.map((c) => (
                    <td key={`n:${c}`} className="num">{b.data_days ? fmt(perDay(b, b.by_class[c]?.n ?? 0)) : DASH}</td>
                  ))}
                  <td className="num">{b.data_days ? fint(Object.values(b.by_class).reduce((a, c) => a + c.submits, 0)) : DASH}</td>
                  {types.slice(0, 4).map((ty) => (
                    <td key={ty} className="num">{b.data_days ? fint(b.types[ty]?.users ?? 0) : DASH}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {d?.basis && <p className="nm" style={{ marginTop: 8 }}>{d.basis}</p>}
      </Panel>
    </div>
  );
}
