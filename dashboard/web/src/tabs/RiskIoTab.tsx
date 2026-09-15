/* Risk & I/O -- the `io_process` payload, plus the search/filter/export bench.
 *
 * `rchar/wchar` against `rd/wr` is the page-cache question, and `cache_hit_pct`
 * is meaningless without its coverage, so every accumulator here is rendered
 * with the coverage attached rather than as a bare total.
 */
import type { Cls, IoProcess } from '../api/types';
import { GroupBars, HBars } from '../charts/primitives';
import type { BarGroup } from '../charts/primitives';
import { Basis, Legend, Panel } from '../components/Panel';
import { AccumCell, Coverage, QUANT_HEAD, QuantCells } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { EventsExplorer } from '../components/EventsExplorer';
import { CLS_COL, CLS_LBL } from '../lib/classes';
import { DASH, fint, fmt, mb, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import type { RiskInstance, RiskPanel } from '../api/types';
import type { UrlState } from '../lib/filters';


/** Who ran it. `args` is absent on a secrets instance by design. */
export function InstanceTable({ rows, showArgs }: { rows: RiskInstance[]; showArgs: boolean }) {
  if (!rows.length) return null;
  return (
    <div className="scroller" style={{ marginTop: 6 }}>
      <table>
        <thead>
          <tr>
            <th>at</th><th className="num">pid</th><th>user</th><th>actor</th>
            <th>host</th><th>tool</th>
            {showArgs && <th>command</th>}
            <th className="num">cpu</th><th className="num">exit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((w, i) => (
            <tr key={`${w.pid}-${i}`}>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.ts ?? DASH}</td>
              <td className="num mono" style={{ fontSize: '10.5px' }}
                  data-tip={w.ppid != null ? `ppid ${w.ppid}${w.session_key ? ' \u00b7 ' + w.session_key : ''}` : undefined}>
                {w.pid ?? DASH}
              </td>
              <td className="mono">{w.user ?? DASH}</td>
              <td className="mono" style={{ fontSize: '10.5px' }}
                  data-tip={w.agent_type ?? undefined}>
                {CLS_LBL[w.actor3 ?? ''] ?? w.actor3 ?? DASH}
              </td>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.host ?? DASH}</td>
              <td className="mono">{w.tool ?? DASH}</td>
              {showArgs && <td className="arg mono" style={{ fontSize: '10.5px' }}>{w.args ?? DASH}</td>}
              <td className="num">{w.cpu_s == null ? DASH : fmt(w.cpu_s)}</td>
              <td className="num">
                {w.signal != null ? `sig${w.signal}` : w.exit_code == null ? DASH : w.exit_code}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RiskIoTab({
  P,
  cls,
  url,
  setUrl,
}: {
  P: Panels;
  cls: Cls[];
  url: UrlState;
  setUrl: (patch: Partial<UrlState>, push?: boolean) => void;
}) {
  const io = pick<IoProcess>(P, 'io_process', 'ebpfm');
  const byClass = io.by_class ?? {};
  const have = shown(byClass, cls);

  const totRd = have.reduce((a, c) => a + (byClass[c]?.rd_mb ?? 0), 0);
  const totWr = have.reduce((a, c) => a + (byClass[c]?.wr_mb ?? 0), 0);

  /* Disk vs syscall bytes, one axis, MB. The gap between them IS the cache. */
  const ioGroups: BarGroup[] = have.map((c) => ({
    k: CLS_LBL[c] ?? c,
    k2: `io cov ${pc(byClass[c]?.io_coverage_pct)}`,
    v: {
      rchar: byClass[c]?.rchar_mb ?? null,
      rd: byClass[c]?.rd_mb ?? null,
      wchar: byClass[c]?.wchar_mb ?? null,
      wr: byClass[c]?.wr_mb ?? null,
    },
  }));
  const ioSeries = [
    { k: 'rchar', lbl: 'read syscalls (rchar)', c: 'var(--s8)' },
    { k: 'rd', lbl: 'read from disk', c: 'var(--s2)' },
    { k: 'wchar', lbl: 'write syscalls (wchar)', c: 'var(--s7)' },
    { k: 'wr', lbl: 'written to disk', c: 'var(--s1)' },
  ];

  const netGroups: BarGroup[] = have.map((c) => ({
    k: CLS_LBL[c] ?? c,
    k2: `tx cov ${pc(byClass[c]?.net_tx_mb?.coverage_pct)}`,
    v: {
      tx: byClass[c]?.net_tx_mb?.n ? (byClass[c]!.net_tx_mb.total ?? null) : null,
      rx: byClass[c]?.net_rx_mb?.n ? (byClass[c]!.net_rx_mb.total ?? null) : null,
    },
  }));

  const byTool = (io.by_tool ?? []).filter((t) => !t.cls || cls.includes(t.cls as Cls));
  const byUser = (io.by_user ?? []).filter((u) => !u.cls || cls.includes(u.cls as Cls));

  const risk = pick<RiskPanel>(P, 'risk', 'ebpfm');
  const lc = risk.login_compute;
  const dg = risk.dangerous;
  const sec = risk.secrets;
  // `compute` is the policy question; `transfer` (wget/curl/rsync) is a
  // different one, so it is listed but kept out of the compute total.
  const lcRows = (lc?.rows ?? []).filter(
    (r) => !Object.keys(r.classes ?? {}).length ||
           Object.keys(r.classes ?? {}).some((c) => cls.includes(c as Cls)),
  );
  const quantKeys = Array.from(
    new Set(have.flatMap((c) => Object.keys(byClass[c]?.quants ?? {}))),
  );

  return (
    <>
      <div className="plates">
        <Plate
          k="read from disk"
          total={have.length ? totRd : null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.rd_mb ?? null]))}
          fmtFn={(n) => mb(n)}
          note={
            have.length ? (
              <>
                coverage{' '}
                {have.map((c) => (
                  <span key={c}>
                    {CLS_LBL[c]} {pc(byClass[c]?.io_coverage_pct)}{' '}
                  </span>
                ))}
              </>
            ) : undefined
          }
        />
        <Plate
          k="written to disk"
          total={have.length ? totWr : null}
          stripe="var(--s1)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.wr_mb ?? null]))}
          fmtFn={(n) => mb(n)}
        />
        <Plate
          k="page-cache hit"
          total={null}
          stripe="var(--s5)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.cache_hit_pct ?? null]))}
          fmtFn={(n) => (n == null ? DASH : pc(n))}
          note="per class only; a fleet-wide mean would hide the split"
        />
        <Plate
          k="events carrying I/O"
          total={have.length ? have.reduce((a, c) => a + (byClass[c]?.events_with_io ?? 0), 0) : null}
          stripe="var(--sg)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.events_with_io ?? null]))}
          note={
            <>
              of {fint(have.reduce((a, c) => a + (byClass[c]?.events ?? 0), 0))} events {'—'} per-process
              I/O is blank for other users' processes
            </>
          }
        />
      </div>

      <div className="grid wide">
        <Panel
          title="Syscall bytes against disk bytes"
          p={io}
          span
          empty={!have.length}
          emptyWhat="no class block for the selection"
          basis={<Basis items={Object.entries(io.notes ?? {})} />}
        >
          <GroupBars
            groups={ioGroups}
            series={ioSeries}
            W={1060}
            H={240}
            maxBar={30}
            ylabel="MB"
            fmtVal={(v) => mb(v)}
            tickFmt={(t) => fmt(t)}
          />
          <Legend items={ioSeries.map((s) => ({ k: s.lbl, c: s.c }))} />
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>class</th>
                  <th className="num">events</th>
                  <th className="num">with I/O</th>
                  <th className="num">rchar</th>
                  <th className="num">rd</th>
                  <th className="num">wchar</th>
                  <th className="num">wr</th>
                  <th className="num">cache served</th>
                  <th className="num">cache hit</th>
                </tr>
              </thead>
              <tbody>
                {have.map((c) => {
                  const b = byClass[c]!;
                  return (
                    <tr key={c}>
                      <td>{CLS_LBL[c] ?? c}</td>
                      <td className="num">{fint(b.events)}</td>
                      <td className="num">
                        {fint(b.events_with_io)}
                        <Coverage pct={b.io_coverage_pct} n={b.events_with_io} />
                      </td>
                      <td className="num">{mb(b.rchar_mb)}</td>
                      <td className="num">{mb(b.rd_mb)}</td>
                      <td className="num">{mb(b.wchar_mb)}</td>
                      <td className="num">{mb(b.wr_mb)}</td>
                      <td className="num">{mb(b.cache_served_mb)}</td>
                      <td className="num">
                        {b.cache_hit_pct == null ? (
                          <span className="nm">not measured</span>
                        ) : (
                          pc(b.cache_hit_pct)
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
          title="Network bytes"
          p={io}
          empty={!have.length || netGroups.every((g) => g.v.tx == null && g.v.rx == null)}
          emptyWhat="no TCP accounting for the selection"
        >
          <GroupBars
            groups={netGroups}
            series={[
              { k: 'tx', lbl: 'egress (tx)', c: 'var(--s1)' },
              { k: 'rx', lbl: 'ingress (rx)', c: 'var(--s2)' },
            ]}
            W={520}
            H={200}
            ylabel="MB"
            fmtVal={(v) => mb(v)}
          />
          <Legend
            items={[
              { k: 'egress (tx)', c: 'var(--s1)' },
              { k: 'ingress (rx)', c: 'var(--s2)' },
            ]}
          />
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>class</th>
                  <th className="num">tx</th>
                  <th className="num">rx</th>
                  <th className="num">calls</th>
                </tr>
              </thead>
              <tbody>
                {have.map((c) => (
                  <tr key={c}>
                    <td>{CLS_LBL[c] ?? c}</td>
                    <td className="num">
                      <AccumCell a={byClass[c]?.net_tx_mb} unit="MB" />
                    </td>
                    <td className="num">
                      <AccumCell a={byClass[c]?.net_rx_mb} unit="MB" />
                    </td>
                    {/* an Acc like its tx/rx neighbours: the count carries the
                        same coverage, so it must show it the same way */}
                    <AccumCell a={byClass[c]?.net_calls} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="I/O by tool"
          p={io}
          empty={!byTool.length}
          emptyWhat="no per-tool I/O rows for the selection"
        >
          <HBars
            items={byTool.slice(0, 14).map((t) => ({
              k: t.tool,
              v: (t.rd_mb ?? 0) + (t.wr_mb ?? 0) || null,
              c: t.cls ? CLS_COL[t.cls] : 'var(--s2)',
              lbl: mb((t.rd_mb ?? 0) + (t.wr_mb ?? 0)),
              tip: `${t.tool}: rd ${mb(t.rd_mb)}, wr ${mb(t.wr_mb)}, coverage ${pc(
                t.io_coverage_pct,
              )}, ${fint(t.events)} events`,
            }))}
            labelW={150}
            valW={100}
          />
        </Panel>

        <Panel
          title="I/O by user"
          p={io}
          empty={!byUser.length}
          emptyWhat="no per-user I/O rows for the selection"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>user</th>
                  <th>class</th>
                  <th className="num">events</th>
                  <th className="num">rd</th>
                  <th className="num">wr</th>
                  <th className="num">tx</th>
                  <th className="num">coverage</th>
                </tr>
              </thead>
              <tbody>
                {byUser.slice(0, 20).map((u, i) => (
                  <tr key={u.user + i}>
                    <td className="mono">{u.user}</td>
                    <td>{u.cls ? (CLS_LBL[u.cls] ?? u.cls) : DASH}</td>
                    <td className="num">{fint(u.events)}</td>
                    <td className="num">{mb(u.rd_mb)}</td>
                    <td className="num">{mb(u.wr_mb)}</td>
                    <td className="num">{mb(u.net_tx_mb)}</td>
                    <td className="num">
                      <Coverage pct={u.io_coverage_pct} n={u.events} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        {quantKeys.length > 0 && (
          <Panel title="I/O distributions" p={io} span empty={!have.length}>
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <th>metric</th>
                    <th>class</th>
                    {QUANT_HEAD}
                  </tr>
                </thead>
                <tbody>
                  {quantKeys.flatMap((qk) =>
                    have.map((c) => (
                      <tr key={qk + c}>
                        <td className="mono">{qk}</td>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <QuantCells q={byClass[c]?.quants?.[qk]} />
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </div>

      <div className="grid wide" style={{ marginTop: 18 }}>
        <Panel
          title="Compute on a login node"
          p={risk}
          span
          empty={!lcRows.length}
          emptyWhat="no build or interpreter CPU in the window"
          basis={lc?.basis}
        >
          <div className="eyebrow">
            {fmt(lc?.compute_cpu_h ?? 0, 3)} CPU-h of compute{' '}
            <span style={{ color: 'var(--muted)' }}>
              {'\u00b7'} {pc(lc?.share_of_all_cpu_pct ?? 0)} of all CPU the collector saw
            </span>
          </div>
          <HBars
            items={lcRows.slice(0, 12).map((r) => ({
              k: r.tool,
              v: r.cpu_s,
              c: r.kind === 'compute' ? 'var(--s8)' : 'var(--sg)',
              lbl: `${fmt(r.cpu_s)}s ${'\u00b7'} ${r.users}u`,
              tip: `${r.tool}: ${fmt(r.cpu_s)} CPU-s over ${fint(r.n)} exits, ${r.users} user(s), hosts ${r.hosts.join(', ')}`,
            }))}
            labelW={140}
            valW={110}
          />
          <Legend
            items={[
              { k: 'compute (build / interpreter)', c: 'var(--s8)' },
              { k: 'bulk transfer', c: 'var(--sg)' },
            ]}
          />
          <div className="scroller" style={{ marginTop: 8 }}>
            <table>
              <thead>
                <tr>
                  <th>tool</th><th>kind</th><th className="num">exits</th>
                  <th className="num">CPU s</th><th className="num">% of CPU</th>
                  <th className="num">users</th><th>hosts</th><th>by class</th>
                </tr>
              </thead>
              <tbody>
                {lcRows.slice(0, 20).map((r, i) => (
                  <tr key={r.tool + i}>
                    <td className="mono">{r.tool}</td>
                    <td>{r.kind ?? DASH}</td>
                    <td className="num">{fint(r.n)}</td>
                    <td className="num">{fmt(r.cpu_s)}</td>
                    <td className="num">{pc(r.cpu_pct)}</td>
                    <td className="num">{fint(r.users)}</td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>{r.hosts.join(' ')}</td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>
                      {Object.entries(r.classes ?? {})
                        .map(([c, n]) => `${CLS_LBL[c] ?? c} ${n}`)
                        .join(' \u00b7 ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="eyebrow" style={{ marginTop: 10 }}>heaviest individual processes</div>
          <InstanceTable rows={lc?.top_procs ?? []} showArgs />
        </Panel>

        <Panel
          title="Commands that should not run here"
          p={risk}
          span
          empty={!(dg?.rows ?? []).length}
          emptyWhat="no matching command in the window"
          basis={[dg?.basis, dg?.site_assumption].filter(Boolean).join(' ')}
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>command</th><th>severity</th><th>axis</th>
                  <th className="num">exits</th><th className="num">users</th>
                  <th className="num">succeeds</th><th className="num">CPU s</th>
                </tr>
              </thead>
              <tbody>
                {(dg?.rows ?? []).map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.command}</td>
                    <td><span className={`sev ${r.severity}`}>{r.severity}</span></td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>{r.axis}</td>
                    <td className="num">{fint(r.n)}</td>
                    <td className="num">{fint(r.users)}</td>
                    <td className="num">
                      {r.works_pct == null ? (
                        <span className="nm">no verdict</span>
                      ) : (
                        <>
                          {pc(r.works_pct)}
                          <span style={{ color: 'var(--muted)' }}> of {fint(r.rated)}</span>
                        </>
                      )}
                    </td>
                    <td className="num">{fmt(r.cpu_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Legend
            items={[
              { k: 'environment-unaware: runs, tells you nothing here', c: 'var(--s6)' },
              { k: 'policy-unaware: works, and is what the facility asks you not to do', c: 'var(--s7)' },
            ]}
          />
          {(dg?.rows ?? []).filter((r) => (r.instances ?? []).length).map((r) => (
            <div key={r.id}>
              <div className="eyebrow" style={{ marginTop: 10 }}>
                {r.command} {'\u2014'} who ran it
              </div>
              <InstanceTable rows={r.instances ?? []} showArgs />
            </div>
          ))}
        </Panel>

        <Panel
          title="Credentials exposed in argv"
          p={risk}
          span
          empty={!(sec?.rows ?? []).length}
          emptyWhat="no credential pattern matched in the window"
          basis={[sec?.redaction, sec?.undercount].filter(Boolean).join(' ')}
        >
          <div className="eyebrow">
            {fint(sec?.args_scanned ?? 0)} argv scanned{' '}
            <span style={{ color: 'var(--muted)' }}>
              {'\u00b7'} {fint(sec?.args_truncated ?? 0)} truncated ({pc(sec?.truncation_pct ?? 0)}) {'\u2014'}{' '}
              a credential past the argv cap is invisible
            </span>
          </div>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>pattern</th><th>severity</th><th className="num">matches</th>
                  <th className="num">users</th><th>tools</th><th>by class</th>
                </tr>
              </thead>
              <tbody>
                {(sec?.rows ?? []).map((r) => (
                  <tr key={r.id}>
                    <td>{r.pattern}</td>
                    <td><span className={`sev ${r.severity}`}>{r.severity}</span></td>
                    <td className="num">{fint(r.n)}</td>
                    <td className="num">{fint(r.users)}</td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>{r.tools.join(' ')}</td>
                    <td className="mono" style={{ fontSize: '10.5px' }}>
                      {Object.entries(r.classes ?? {})
                        .map(([c, n]) => `${CLS_LBL[c] ?? c} ${n}`)
                        .join(' \u00b7 ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(sec?.rows ?? []).filter((r) => (r.instances ?? []).length).map((r) => (
            <div key={r.id}>
              <div className="eyebrow" style={{ marginTop: 10 }}>
                {r.pattern} {'\u2014'} who exposed it (no argv, by design)
              </div>
              <InstanceTable rows={r.instances ?? []} showArgs={false} />
            </div>
          ))}
          <p className="empty" style={{ marginTop: 8 }}>
            The matched value is never read back here {'\u2014'} only the pattern, the counts and
            the tools. On a shared node the alternative would make this page the leak.
          </p>
        </Panel>
      </div>

      <div className="head" style={{ marginTop: 26 }}>
        <div>
          <div className="eyebrow">search / filter / export</div>
          <h1 style={{ fontSize: 18 }}>Event bench</h1>
        </div>
      </div>
      <EventsExplorer url={url} setUrl={setUrl} />
    </>
  );
}
