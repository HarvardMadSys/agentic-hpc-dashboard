/* Day by day -- each class per day, over every day the service holds (`trend`).
 * On the Trend tab, under the roll-up table's year.
 *
 * "Is agent use growing, and where?" is not a question one window answers.
 * This is the live tier's own minute bins and roster summed per local day, so
 * it covers retention -- whatever the window -- and is not narrowed by
 * the user or agent-type filter; the panel says both.
 *
 * Count and cost side by side, as every categorical panel: processes on one
 * chart, CPU on the other, each on its own axis. Two readings: how many, or
 * each class's share of the day, because a day with twice the processes and
 * a day where agents ran twice the share are different news.
 *
 * Three kinds of day are kept apart: a day the collector wrote in (its
 * numbers, and how many of its minutes held a record), a day no minute of was
 * held (no bars, and says so -- not a quiet day), and today, still going.
 */
import { useState } from 'react';
import type { Cls, TrendDay, TrendPanel } from '../api/types';
import { CountCost } from '../components/CountCost';
import { Segmented } from '../components/ClassFilter';
import { Panel } from '../components/Panel';
import { SortTh } from '../components/SortTh';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
import type { Who } from '../lib/filters';
import { pick } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { useSort } from '../lib/useSort';

const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** The day's sub-label: its weekday, and what about it is partial. */
function dayNote(d: TrendDay): string {
  const wd = WEEKDAY[new Date(`${d.date}T12:00:00`).getDay()];
  if (!d.held) return `${wd} · no data`;
  if (d.today) return `${wd} · so far`;
  if (d.cut) return `${wd} · part`;
  return wd;
}

function dayTip(d: TrendDay): string {
  const lines = [
    d.date + (d.today ? ' (today, still going)' : d.cut ? ' (from where retention starts)' : ''),
    d.held
      ? `${fint(d.held)} of ${fint(d.minutes)} minutes held a record (${pc((100 * d.held) / Math.max(1, d.minutes))})`
      : `not one minute of it held a record: no data, not a quiet day`,
  ];
  return lines.join('\n');
}

export function DayByDay({ P, cls, who }: { P: Panels; cls: Cls[]; who: Who | null }) {
  const t = pick<TrendPanel>(P, 'trend', 'ebpfm');
  const days = t.days ?? [];
  const have = cls.filter((c) => days.some((d) => d.by_class[c]));
  const [read, setRead] = useState<'abs' | 'share'>('abs');
  const s = useSort<string>('day', 'desc');
  const filtered = !!(who && (who.users.length || who.atypes.length));
  const held = days.filter((d) => d.held > 0);

  // every class's total, shown or not, so a share does not move with the filter
  const total = (d: TrendDay, f: 'n' | 'cpu_s') =>
    Object.values(d.by_class).reduce((a, c) => a + (c?.[f] ?? 0), 0);
  const val = (d: TrendDay, c: string, f: 'n' | 'cpu_s') => {
    const v = d.by_class[c]?.[f];
    if (v == null) return null;
    if (read === 'share') {
      const tot = total(d, f);
      return tot ? (100 * v) / tot : null;
    }
    return f === 'cpu_s' ? v / 3600 : v;
  };
  const groups = days.map((d) => ({
    k: d.date.slice(5),
    k2: dayNote(d),
    v: Object.fromEntries(have.flatMap((c) => [[c, val(d, c, 'n')], [`cpu:${c}`, val(d, c, 'cpu_s')]])),
    tips: Object.fromEntries(have.flatMap((c) => {
      const b = d.by_class[c];
      const head = `${d.date} · ${CLS_LBL[c] ?? c}`;
      return [
        [c, b?.n == null ? `${head}: no data` : `${head}: ${fint(b.n)} processes (${pc((100 * b.n) / Math.max(1, total(d, 'n')))} of the day's)\n${dayTip(d)}`],
        [`cpu:${c}`, b?.cpu_s == null ? `${head}: no data` : `${head}: ${fmt(b.cpu_s / 3600)} CPU-hours (${pc((100 * b.cpu_s) / Math.max(1e-9, total(d, 'cpu_s')))} of the day's)\n${dayTip(d)}`],
      ];
    })),
  }));
  const series = have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c] }));

  const types = Array.from(new Set(held.flatMap((d) => Object.keys(d.types)))).sort(
    (a, b) => held.reduce((x, d) => x + (d.types[b]?.users ?? 0), 0) - held.reduce((x, d) => x + (d.types[a]?.users ?? 0), 0),
  );
  const rows = s.sort(days, (d, k) =>
    k === 'day' ? d.date : k === 'held' ? d.held : k.startsWith('u:') ? d.by_class[k.slice(2)]?.users ?? null
      : d.types[k.slice(2)]?.users ?? null);

  const span = days.length ? `the last ${fint(days.length)} days` : 'every day held';
  return (
    <Panel
      title="Day by day"
      id="sec-day-by-day"
      ask={[{ panel: 'trend', keys: ['days'] }]}
      p={t}
      span
      empty={!held.length}
      emptyWhat={t._error ? `the days could not be read: ${t._error}` : 'the live tier holds no record yet'}
      header={
        <span className="wrapctl" style={{ gap: 6 }}>
          <span className="pill" data-tip={`every day the live tier holds (${fint((t.retention_minutes ?? 0) / 1440)} days of retention), whatever the window${t.tz ? `; days start at midnight, ${t.tz}` : ''}`}>
            {span}
          </span>
          {filtered && (
            <span className="pill stale" data-tip="the day-by-day counts come from the live tier, which the user and agent-type filter does not narrow">
              everyone, not the filter
            </span>
          )}
          <Segmented value={read} onChange={setRead} options={[
            { k: 'abs', lbl: 'how many', tip: 'processes and CPU-hours per day' },
            { k: 'share', lbl: 'each class’s share', tip: 'each class’s share of the day’s processes and CPU, out of every class' },
          ]} />
        </span>
      }
    >
      <CountCost
        groups={groups}
        series={series}
        W={1060}
        H={210}
        maxBar={22}
        pctAxis={read === 'share'}
        countLabel={read === 'share' ? 'each class’s share of the processes that exited' : 'processes that exited'}
        costLabel={read === 'share' ? 'each class’s share of the CPU' : 'CPU time'}
        countAxis={read === 'share' ? '% of processes' : 'processes'}
        costAxis={read === 'share' ? '% of CPU' : 'CPU-hours'}
        labelTop={false}
        countFmt={(v) => (v ? fmt(v) : '0')}
        costFmt={(v) => (v ? fmt(v) : '0')}
      />
      <div className="scroller" style={{ marginTop: 10 }}>
        <table className="roomy">
          <thead>
            <tr>
              <SortTh s={s} k="day" first="desc">day</SortTh>
              <SortTh s={s} k="held" num tip="minutes of the day the collector wrote any record in, of the minutes the service could hold">minutes held</SortTh>
              {have.map((c) => (
                <SortTh key={c} s={s} k={`u:${c}`} num className="cls" style={{ color: CLS_COL[c] }}
                  tip={`accounts that ran something as ${CLS_LBL[c] ?? c} that day`}>
                  {CLS_LBL[c] ?? c} users
                </SortTh>
              ))}
              {cls.includes('agent') && types.slice(0, 4).map((ty) => (
                <SortTh key={ty} s={s} k={`t:${ty}`} num tip={`accounts that ran ${AGENT_TYPE_LBL[ty] ?? ty} that day`}>
                  {AGENT_TYPE_LBL[ty] ?? ty}
                </SortTh>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.date}>
                <td className="mono" data-tip={dayTip(d)}>
                  {d.date} <span className="nm">{dayNote(d)}</span>
                </td>
                <td className="num">{d.held ? `${pc((100 * d.held) / Math.max(1, d.minutes))}` : <span className="nm">no data</span>}</td>
                {have.map((c) => (
                  <td key={c} className="num">{d.by_class[c]?.users == null ? DASH : fint(d.by_class[c].users)}</td>
                ))}
                {cls.includes('agent') && types.slice(0, 4).map((ty) => (
                  <td key={ty} className="num">{!d.held ? DASH : fint(d.types[ty]?.users ?? 0)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
