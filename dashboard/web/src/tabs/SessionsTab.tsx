/* Sessions & tools -- the `tool_mix` payload.
 *
 * The bucket mix ships as two charts on one axis each, never a dual axis: on
 * this cluster `poll/probe` is ~29% of calls and ~0% of CPU, and one pair of
 * axes would make those look like the same quantity.
 */
import { useMemo } from 'react';
import type { Cls, ToolMix } from '../api/types';
import { HBars, StackRow } from '../charts/primitives';
import type { BarGroup, BarSeries } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { CountCost } from '../components/CountCost';
import { Plate } from '../components/Plate';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';

export function SessionsTab({ P, cls }: { P: Panels; cls: Cls[] }) {
  const tm = pick<ToolMix>(P, 'tool_mix', 'ebpfm');
  const byClass = tm.by_class ?? {};
  const have = shown(byClass, cls);
  const buckets = tm.buckets ?? [];

  const series: BarSeries[] = have.map((c) => ({ k: c, lbl: CLS_LBL[c] ?? c, c: CLS_COL[c] }));

  /* One group per bucket; count share and CPU share travel together so the two
   * charts cannot drift apart, but they are drawn on separate axes. */
  const groups: BarGroup[] = useMemo(() => {
    return buckets.map((b) => {
      const v: Record<string, number | null> = {};
      for (const c of have) {
        const row = byClass[c]?.rows?.find((r) => r.b === b);
        v[c] = row ? (row.calls_pct ?? null) : null;
        v['cpu:' + c] = row ? (row.cpu_pct ?? null) : null;
      }
      return { k: b, v };
    });
  }, [buckets, have, byClass]);

  const totCalls = have.reduce((a, c) => a + (byClass[c]?.calls ?? 0), 0);
  const totCpu = have.reduce((a, c) => a + (byClass[c]?.cpu_s ?? 0), 0);
  const callsBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.calls ?? null]));
  const cpuBy = Object.fromEntries(have.map((c) => [c, byClass[c]?.cpu_s ?? null]));

  const head = tm.head ?? {};
  const shellKinds = tm.shell_kinds ?? {};

  return (
    <>
      <div className="plates">
        <Plate
          k="tool calls in window"
          total={have.length ? totCalls : null}
          stripe="var(--s1)"
          by={callsBy}
          note={
            tm.dedup
              ? `${fint(tm.dedup.dropped)} resolved shells deduped (${pc(tm.dedup.dropped_pct)})`
              : undefined
          }
        />
        <Plate
          k="cpu seconds in window"
          total={have.length ? totCpu : null}
          stripe="var(--s6)"
          by={cpuBy}
          fmtFn={(n) => fmt(n)}
          unit="s"
          note="self cpu_s only; child_cpu_s is never added"
        />
        <Plate
          k="distinct tools in the head"
          total={have.length ? Math.max(...have.map((c) => head[c]?.length ?? 0), 0) : null}
          stripe="var(--s4)"
          by={Object.fromEntries(have.map((c) => [c, head[c]?.length ?? null]))}
          note="length of the per-class head list the reducer kept"
        />
        <Plate
          k="buckets in the taxonomy"
          total={buckets.length || null}
          stripe="var(--sg)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.rows?.length ?? null]))}
          note="12-way tool taxonomy; a class row is one bucket"
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Tool mix — counted, then costed"
          p={tm}
          span
          empty={!have.length || !buckets.length}
          emptyWhat="the feed carried no bucket rows for the selected classes"
          basis={
            <Basis
              items={[
                ['cpu_basis', tm.cpu_basis],
                ['dedup', tm.dedup?.rule],
                ['dropped', tm.dedup ? `${fint(tm.dedup.dropped)} (${pc(tm.dedup.dropped_pct)})` : null],
              ]}
            />
          }
        >
          <CountCost groups={groups} series={series} />
        </Panel>

        <Panel
          title="Head of the tool distribution"
          p={tm}
          empty={!have.some((c) => (head[c]?.length ?? 0) > 0)}
          emptyWhat="no head list for the selected classes"
        >
          {have.map((c) => {
            const rows = head[c] ?? [];
            if (!rows.length) return null;
            return (
              <div key={c}>
                <div className="eyebrow" style={{ margin: '8px 0 5px' }}>
                  {CLS_LBL[c] ?? c}
                </div>
                <HBars
                  items={rows.slice(0, 12).map((r) => ({
                    k: r.tool,
                    v: r.calls_pct,
                    c: CLS_COL[c],
                    lbl: `${pc(r.calls_pct)} calls / ${pc(r.cpu_pct)} cpu`,
                    tip: `${r.tool}: ${fint(r.calls)} calls, ${pc(r.calls_pct)} of calls, ${pc(
                      r.cpu_pct,
                    )} of CPU`,
                  }))}
                  labelW={150}
                  valW={150}
                />
              </div>
            );
          })}
        </Panel>

        <Panel
          title="Shell kinds"
          p={tm}
          empty={!have.some((c) => Object.keys(shellKinds[c] ?? {}).length > 0)}
          emptyWhat="no shell breakdown for the selected classes"
        >
          {have.map((c) => {
            const k = shellKinds[c] ?? {};
            const keys = Object.keys(k);
            if (!keys.length) return null;
            const segs = keys
              .map((kk, i) => ({
                k: kk,
                v: k[kk],
                c: ['var(--s2)', 'var(--s5)', 'var(--s6)', 'var(--s8)', 'var(--s4)', 'var(--sg)'][
                  i % 6
                ],
              }))
              .sort((a, b) => b.v - a.v);
            return (
              <div key={c}>
                <div className="eyebrow" style={{ margin: '6px 0 4px' }}>
                  {CLS_LBL[c] ?? c} {'·'} {fint(segs.reduce((a, s) => a + s.v, 0))} shells
                </div>
                <StackRow segs={segs} W={520} H={18} />
              </div>
            );
          })}
          <Legend
            items={Array.from(
              new Set(have.flatMap((c) => Object.keys(shellKinds[c] ?? {}))),
            ).map((kk, i) => ({
              k: kk,
              c: ['var(--s2)', 'var(--s5)', 'var(--s6)', 'var(--s8)', 'var(--s4)', 'var(--sg)'][
                i % 6
              ],
            }))}
          />
        </Panel>

        <Panel
          title="Bucket table — calls and CPU per class"
          p={tm}
          span
          empty={!have.length || !buckets.length}
          emptyWhat="the feed carried no bucket rows"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>bucket</th>
                  {have.map((c) => (
                    <th key={c} className="num" colSpan={2}>
                      {CLS_LBL[c] ?? c}
                    </th>
                  ))}
                </tr>
                <tr>
                  <th />
                  {have.flatMap((c) => [
                    <th key={c + 'a'} className="num">
                      calls
                    </th>,
                    <th key={c + 'b'} className="num">
                      cpu
                    </th>,
                  ])}
                </tr>
              </thead>
              <tbody>
                {buckets.map((b) => (
                  <tr key={b}>
                    <td className="mono">{b}</td>
                    {have.flatMap((c) => {
                      const r = byClass[c]?.rows?.find((x) => x.b === b);
                      return [
                        <td key={c + 'a'} className="num">
                          {r ? `${fint(r.calls)} / ${pc(r.calls_pct)}` : DASH}
                        </td>,
                        <td key={c + 'b'} className="num">
                          {r ? `${fmt(r.cpu_s)}s / ${pc(r.cpu_pct)}` : DASH}
                        </td>,
                      ];
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
}
