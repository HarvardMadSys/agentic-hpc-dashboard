/* Every time series of the window, on Live (roadmap note 23).
 *
 * Four stay open, the ones an operator watches: CPU, memory, and each class's
 * share of the processes and of the CPU. Everything else -- waits, D-state,
 * faults, disk, the scheduler -- sits behind one picker, one chart at a time,
 * with the table that used to fold under that chart. All of them share one zoom,
 * tied to the window it was taken in: a range picked on the 24 h view means
 * nothing once the 1 h view is on screen.
 *
 * Sums are binned BY EXIT TIME (a whole-life counter lands in the bin its
 * process exited in); resident memory is the exception, sampled while processes
 * ran. The bins are sized to the window, which the eyebrow names.
 */
import { useState } from 'react';
import type { Accum, Cls, Resources, SlurmPanel } from '../api/types';
import { TimeChart, anyValue, binLabel, TOOL_COL } from '../charts/TimeChart';
import type { TimeLine, View } from '../charts/TimeChart';
import { fullClock } from '../charts/scale';
import { More, NoRows, Panel } from '../components/Panel';
import { AccumCell } from '../components/Coverage';
import { Term } from '../components/Term';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, dur, fint, fmt } from '../lib/format';
import { pick, shown, winLabel } from '../lib/panels';
import type { Panels } from '../lib/panels';

type PickKey = 'wait' | 'dstate' | 'faults' | 'read' | 'write' | 'queue' | 'jobs';

export function WindowCharts({ P, cls }: { P: Panels; cls: Cls[] }) {
  const r = pick<Resources>(P, 'resources', 'ebpfm');
  const sp = pick<SlurmPanel>(P, 'slurm', 'ebpfm');
  const s = r.series;
  const byClass = r.by_class ?? {};
  const have = shown(byClass, cls);
  const win = winLabel(r);

  const winKey = r._window?.label ?? '';
  const [zoom, setZoom] = useState<{ win: string; v: View } | null>(null);
  const view = zoom && zoom.win === winKey ? zoom.v : null;
  const onView = (v: View) => setZoom(v ? { win: winKey, v } : null);
  const z = { view, onView };
  const [picked, setPicked] = useState<PickKey>('wait');

  const byCls = (field: 'n' | 'cpu_s' | 'sched_wait_s' | 'maj_flt'): TimeLine[] =>
    shown(s?.by_class, cls).map((c) => ({ lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: s?.by_class?.[c]?.[field] }));

  // Each class's share of the node, bin by bin: out of EVERY class's records,
  // not just the shown ones, or a share would change when the filter does.
  const shareLines = (field: 'n' | 'cpu_s'): TimeLine[] => {
    const by = s?.by_class ?? {};
    const all = Object.keys(by);
    const tot = (s?.t ?? []).map((_, i) => {
      const vals = all.map((c) => by[c]?.[field]?.[i]).filter((v): v is number => v != null);
      return vals.length ? vals.reduce((a, v) => a + v, 0) : null;
    });
    return cls
      .filter((c) => by[c])
      .map((c) => ({
        lbl: CLS_LBL[c] ?? c,
        c: CLS_COL[c],
        v: tot.map((t, i) => {
          const v = by[c]?.[field]?.[i];
          return t ? (100 * (v ?? 0)) / t : null;
        }),
      }));
  };

  const memCls = shown(s?.resident, cls);
  const memLines: TimeLine[] = memCls.map((c) => ({ lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: s?.resident[c]?.rss_mb }));
  const mem = Object.fromEntries(
    memCls.map((c) => {
      const vals = s?.resident[c]?.rss_mb ?? [];
      let peak: number | null = null;
      let at: number | null = null;
      vals.forEach((v, i) => {
        if (v != null && (peak == null || v > peak)) {
          peak = v;
          at = s!.t[i];
        }
      });
      const seen = vals.filter((v): v is number => v != null);
      return [c, {
        peak: peak == null ? null : (peak as number) / 1024,
        at,
        mean: seen.length ? seen.reduce((x, y) => x + y, 0) / seen.length / 1024 : null,
      }];
    }),
  );

  // the picker's charts
  const ioCls = shown(s?.by_class, cls);
  const ioLines = (disk: 'rd_mb' | 'wr_mb', sys: 'rchar_mb' | 'wchar_mb', sysWord: string): TimeLine[] =>
    ioCls.flatMap((c) => [
      { lbl: `${CLS_LBL[c] ?? c} · disk`, c: CLS_COL[c] ?? 'var(--ink-3)', v: s?.by_class[c]?.[disk] },
      { lbl: `${CLS_LBL[c] ?? c} · ${sysWord}`, c: CLS_COL[c] ?? 'var(--ink-3)', v: s?.by_class[c]?.[sys], dashed: true },
    ]);
  const ioLegend = (sysWord: string) => [
    ...ioCls.map((c) => ({ k: CLS_LBL[c] ?? c, c: CLS_COL[c] ?? 'var(--ink-3)' })),
    { k: 'disk', c: 'var(--ink-2)', solid: true },
    { k: sysWord, c: 'var(--ink-2)', dashed: true },
  ];
  const stallLines: TimeLine[] = Object.entries(s?.stall_by_tool ?? {}).map(([tool, v], i) => ({
    lbl: tool,
    c: TOOL_COL[i % TOOL_COL.length],
    v,
  }));
  if (s?.stall_other.some((v) => v)) stallLines.push({ lbl: 'every other tool', c: 'var(--sg)', v: s.stall_other });
  const ss = sp.series;
  const slurmLines = (field: 'jobs' | 'queries'): TimeLine[] =>
    shown(ss?.by_class, cls).map((c) => ({ lbl: CLS_LBL[c] ?? c, c: CLS_COL[c], v: ss?.by_class[c]?.[field] }));

  const PICKS: { k: PickKey; lbl: string; tip?: string; show: boolean }[] = [
    { k: 'wait', lbl: 'runqueue wait', tip: 'runqueue', show: true },
    { k: 'dstate', lbl: 'D-state by tool', tip: 'dstate', show: true },
    { k: 'faults', lbl: 'major page faults', tip: 'majflt', show: true },
    { k: 'read', lbl: 'disk read', tip: 'syscall', show: true },
    { k: 'write', lbl: 'disk written', tip: 'syscall', show: true },
    { k: 'queue', lbl: 'Slurm queue checks', tip: 'queuecheck', show: !!ss?.t.length },
    { k: 'jobs', lbl: 'Slurm jobs submitted', show: !!ss?.t.length },
  ];
  const picks = PICKS.filter((p) => p.show);
  const cur = picks.find((p) => p.k === picked)?.k ?? picks[0]?.k;

  const stalls = r.stall_by_tool ?? [];
  const hasSeries = !!s?.t.length;
  const binEye = s ? `${binLabel(s.bin_s)} bins` : '';

  const pickedChart = () => {
    if (!s) return <NoRows what="no series in the window" />;
    switch (cur) {
      case 'wait': {
        const lines = byCls('sched_wait_s');
        return (
          <>
            {anyValue(lines) ? (
              <TimeChart {...z} s={s} lines={lines} perMin ylabel="wait s / min" yname="wait s/min" />
            ) : (
              <p className="nm">schedstat was not readable for any selected class</p>
            )}
            <More label="run and wait time per class">
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <th>class</th>
                      <th className="num"><Term tip="time on a CPU, summed">run</Term></th>
                      <th className="num"><Term k="runqueue">wait</Term></th>
                      <th className="num"><Term tip="wait divided by run: above 1 means these processes waited longer for a CPU than they ran">ratio</Term></th>
                    </tr>
                  </thead>
                  <tbody>
                    {have.map((c) => (
                      <tr key={c}>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <td className="num"><AccumCell a={byClass[c]?.sched_run_s as Accum | undefined} unit="s" /></td>
                        <td className="num"><AccumCell a={byClass[c]?.sched_wait_s as Accum | undefined} unit="s" /></td>
                        <td className="num">
                          {byClass[c]?.schedstat_ratio == null ? <span className="nm">not measured</span> : fmt(byClass[c]!.schedstat_ratio, 2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </More>
          </>
        );
      }
      case 'dstate':
        return (
          <>
            {anyValue(stallLines) ? (
              <TimeChart {...z} s={s} lines={stallLines} perMin ylabel="D-state s / min" yname="D-state s/min" />
            ) : (
              <NoRows what="no process stalled in uninterruptible sleep in the window" />
            )}
            <p className="nm" style={{ margin: 0 }}>tools are ranked over every class: the class filter does not narrow this chart</p>
            {stalls.length > 0 && (
              <More label="every tool that stalled">
                <div className="scroller">
                  <table>
                    <thead>
                      <tr>
                        <th>tool</th>
                        <th className="num" data-tip="its processes that spent any time in uninterruptible sleep (D-state)">processes stalled</th>
                        <th className="num" data-tip="their time in D-state, summed">time stalled</th>
                        <th className="num"><Term k="stall">stalls</Term></th>
                        <th className="num" data-tip="the longest single stall">longest stall</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stalls.map((t) => (
                        <tr key={t.tool}>
                          <td className="mono">{t.tool}</td>
                          <td className="num">{fint(t.n)}</td>
                          <td className="num">{dur(t.wait_s)}</td>
                          <td className="num">{fint(t.episodes)}</td>
                          <td className="num">{dur(t.max_s)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </More>
            )}
          </>
        );
      case 'faults': {
        const lines = byCls('maj_flt');
        return anyValue(lines)
          ? <TimeChart {...z} s={s} lines={lines} perMin ylabel="faults / min" yname="faults/min" />
          : <NoRows what="no major fault was recorded in the window for the selection" />;
      }
      case 'read':
        return (
          <>
            <div className="eyebrow">solid: read from the disk · dashed: passed through read(), the gap is the <Term k="pagecache">page cache</Term></div>
            <TimeChart {...z} s={s} lines={ioLines('rd_mb', 'rchar_mb', 'read()')} ylabel="MB / min"
              yname="read" perMin legend={ioLegend('read() calls')} />
          </>
        );
      case 'write':
        return (
          <>
            <div className="eyebrow">solid: written to the disk · dashed: passed through write()</div>
            <TimeChart {...z} s={s} lines={ioLines('wr_mb', 'wchar_mb', 'write()')} ylabel="MB / min"
              yname="written" perMin legend={ioLegend('write() calls')} />
          </>
        );
      case 'queue':
        return ss ? <TimeChart {...z} s={ss} lines={slurmLines('queries')} ylabel={`checks / ${dur(ss.bin_s)}`} yname="checks" /> : null;
      case 'jobs':
        return ss ? <TimeChart {...z} s={ss} lines={slurmLines('jobs')} ylabel={`jobs / ${dur(ss.bin_s)}`} yname="jobs" /> : null;
      default:
        return null;
    }
  };

  return (
    <>
      <Panel
        title={win.startsWith('last') ? `Over the ${win}` : 'Over the window'}
        id="sec-over-the-window"
        p={r}
        span
        empty={!hasSeries}
        emptyWhat="no process exited in the window for the selection"
        header={<span className="pill" data-tip="every chart on this panel is binned this finely; drag on any of them to zoom all four">{binEye}</span>}
      >
        <div className="pair-row">
          <div>
            <div className="eyebrow"><Term k="cpusmin">CPU seconds per minute</Term></div>
            {s && <TimeChart {...z} s={s} lines={byCls('cpu_s')} perMin ylabel="CPU s / min" yname="CPU s/min" />}
          </div>
          <div>
            <div className="eyebrow"><Term k="rss">memory in use</Term> · sampled while processes ran</div>
            {s && anyValue(memLines) ? (
              <TimeChart {...z} s={s} lines={memLines} scale={1 / 1024} ylabel="GB resident" yname="GB" />
            ) : (
              <NoRows what="no residency tick was read in the window for the selection" />
            )}
            <More label="memory per class: peak and average">
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <th>class</th>
                      <th className="num">peak</th>
                      <th>at</th>
                      <th className="num">average</th>
                    </tr>
                  </thead>
                  <tbody>
                    {memCls.map((c) => (
                      <tr key={c}>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <td className="num">{mem[c].peak == null ? DASH : `${fmt(mem[c].peak!)} GB`}</td>
                        <td className="mono nm">{mem[c].at == null ? DASH : fullClock(mem[c].at!)}</td>
                        <td className="num">{mem[c].mean == null ? DASH : `${fmt(mem[c].mean!)} GB`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </More>
          </div>
          <div>
            <div className="eyebrow">each class's share of the processes exited</div>
            {s && <TimeChart {...z} s={s} lines={shareLines('n')} ylabel="% of processes" yname="share"
              ymax={100} yfmt={(v) => `${Math.round(v)}%`} />}
          </div>
          <div>
            <div className="eyebrow">each class's share of the CPU time</div>
            {s && <TimeChart {...z} s={s} lines={shareLines('cpu_s')} ylabel="% of CPU" yname="share"
              ymax={100} yfmt={(v) => `${Math.round(v)}%`} />}
          </div>
        </div>
      </Panel>

      <Panel title="More over time" id="sec-more-over-time" p={r} span empty={!hasSeries}
        emptyWhat="no process exited in the window for the selection">
        <div className="picker-head">
          <div className="seg wrap" role="group" aria-label="which series">
            {picks.map((p) => (
              <button key={p.k} aria-pressed={p.k === cur} onClick={() => setPicked(p.k)}>
                {p.lbl}
              </button>
            ))}
          </div>
          {picks.find((p) => p.k === cur)?.tip && (
            <Term k={picks.find((p) => p.k === cur)!.tip}>
              <span className="nm">what is this</span>
            </Term>
          )}
        </div>
        {pickedChart()}
      </Panel>
    </>
  );
}
