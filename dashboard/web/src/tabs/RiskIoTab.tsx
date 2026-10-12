/* Risk & I/O -- the `io_process` payload, plus the search/filter/export bench.
 *
 * `rchar/wchar` against `rd/wr` is the page-cache question, and `cache_hit_pct`
 * is meaningless without its coverage, so every accumulator here is rendered
 * with the coverage attached rather than as a bare total.
 */
import { GLOSS } from '../lib/glossary';
import { Fragment, useState } from 'react';
import type { Cls, IoByTool, IoProcess, Resources } from '../api/types';
import { GroupBars, HBars } from '../charts/primitives';
import { TimeChart } from './ResourcesTab';
import type { TimeLine, View } from './ResourcesTab';
import type { BarGroup, HBarItem } from '../charts/primitives';
import { Legend, More, NoRows, Panel } from '../components/Panel';
import { AccumCell, Coverage, QuantCells, QuantHead, quantVal } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { ByUser, distinct } from '../components/ByUser';
import { CmdCell } from '../components/Cmd';
import { DASH, dur, fint, fmt, mb, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import type { RiskInstance, RiskPanel, RiskRules, RiskSection } from '../api/types';
import { SortTh } from '../components/SortTh';
import { useSort, useSortMap } from '../lib/useSort';


/** Who ran it, at a glance: one chip per agent type (a human's untagged
 *  runs as "human"), in the class's colour, the most first. */
function WhoChips({ r, cls }: {
  r: { classes?: Partial<Record<string, number>>; types?: Partial<Record<string, Record<string, number>>> };
  cls: Cls[];
}) {
  const chips: { c: string; k: string; n: number }[] = [];
  for (const c of cls) {
    const ty = r.types?.[c];
    if (ty) {
      for (const [t, n] of Object.entries(ty)) chips.push({ c, k: t ? AGENT_TYPE_LBL[t] ?? t : CLS_LBL[c] ?? c, n });
    } else if (r.classes?.[c]) {
      chips.push({ c, k: CLS_LBL[c] ?? c, n: r.classes[c]! });
    }
  }
  chips.sort((a, b) => b.n - a.n);
  if (!chips.length) return <span className="nm">{DASH}</span>;
  const head = chips.slice(0, 3);
  const rest = chips.slice(3);
  return (
    <span className="u-chips">
      {head.map((x) => (
        <span key={x.c + x.k} className="u-chip">
          <i className="u-dot" style={{ background: CLS_COL[x.c] }} />
          {x.k} <b>{fint(x.n)}</b>
        </span>
      ))}
      {rest.length > 0 && (
        <span className="nm" data-tip={rest.map((x) => `${x.k} ${fint(x.n)}`).join('\n')}>
          +{rest.length}
        </span>
      )}
    </span>
  );
}

/** The login-compute categories, in the legend's words. */
const LC_KIND: Record<string, string> = { compute: 'compute', transfer: 'bulk transfer' };

function Caret({ on, show }: { on: boolean; show: boolean }) {
  return <span className="u-caret" style={show ? undefined : { visibility: 'hidden' }}>{on ? '▾' : '▸'}</span>;
}

/** The instance lists keep the first few runs a pattern matched; say so when
 *  that is not all of them, so the groups never read as the whole count. */
function Capped({ shown, of, noun }: { shown: number; of: number; noun: string }) {
  if (shown >= of) return null;
  return <div className="nm" style={{ marginTop: 6 }}>the first {fint(shown)} of {fint(of)} {noun}{noun.endsWith('h') ? 'es' : 's'}</div>;
}

const failed = (w: RiskInstance) => w.signal != null || (w.exit_code != null && w.exit_code !== 0);
const fromLbl = (w: RiskInstance) =>
  w.agent_type ? AGENT_TYPE_LBL[w.agent_type] ?? w.agent_type : CLS_LBL[w.actor3 ?? ''] ?? w.actor3;

/** Who ran it: one row per user, opened into their processes. `args` is
 *  absent on a secrets instance by design. */
export function InstanceTable({ rows, showArgs }: { rows: RiskInstance[]; showArgs: boolean }) {
  return (
    <div style={{ marginTop: 6 }}>
      <ByUser
        rows={rows}
        user={(w) => w.user}
        noun="process"
        cols={[
          { h: 'from', cell: (rs) => distinct(rs, fromLbl), sort: (rs) => distinct(rs, fromLbl) },
          {
            h: 'tools',
            cell: (rs) => <span className="mono">{distinct(rs, (w) => w.tool)}</span>,
            sort: (rs) => distinct(rs, (w) => w.tool),
          },
          {
            h: 'CPU',
            num: true,
            cell: (rs) => fmt(rs.reduce((a, w) => a + (w.cpu_s ?? 0), 0)),
            sort: (rs) => rs.reduce((a, w) => a + (w.cpu_s ?? 0), 0),
          },
          {
            h: 'failed',
            num: true,
            tip: 'exited non-zero or on a signal',
            cell: (rs) => (rs.some(failed) ? fint(rs.filter(failed).length) : DASH),
            sort: (rs) => rs.filter(failed).length,
          },
          {
            h: 'latest',
            cell: (rs) => (
              <span className="mono nm">{rs.map((w) => w.ts ?? '').sort().pop() || DASH}</span>
            ),
            sort: (rs) => rs.map((w) => w.ts ?? '').sort().pop() || null,
          },
        ]}
        sub={(rs) => <InstanceRows rows={rs} showArgs={showArgs} />}
      />
    </div>
  );
}

/** One user's processes, as rows. */
function InstanceRows({ rows, showArgs }: { rows: RiskInstance[]; showArgs: boolean }) {
  const s = useSort<string>();
  if (!rows.length) return null;
  const val = (w: RiskInstance, k: string) =>
    k === 'at' ? w.ts : k === 'pid' ? w.pid : k === 'from' ? fromLbl(w) : k === 'host' ? w.host
    : k === 'tool' ? w.tool : k === 'cmd' ? w.args : k === 'cpu' ? w.cpu_s
    : w.signal != null ? 1000 + w.signal : w.exit_code;
  return (
    <div className="scroller">
      <table>
        <thead>
          <tr>
            <SortTh s={s} k="at">at</SortTh><SortTh s={s} k="pid" num>pid</SortTh>
            <SortTh s={s} k="from" first="asc">from</SortTh>
            <SortTh s={s} k="host" first="asc">host</SortTh><SortTh s={s} k="tool" first="asc">tool</SortTh>
            {showArgs && <SortTh s={s} k="cmd" first="asc">command</SortTh>}
            <SortTh s={s} k="cpu" num>CPU</SortTh>
            <SortTh s={s} k="exit" num tip={GLOSS.exitcode}>exit code</SortTh>
          </tr>
        </thead>
        <tbody>
          {s.sort(rows, val).map((w, i) => (
            <tr key={`${w.pid}-${i}`}>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.ts ?? DASH}</td>
              <td className="num mono" style={{ fontSize: '10.5px' }}
                  data-tip={w.ppid != null ? `parent pid ${w.ppid}` : undefined}>
                {w.pid ?? DASH}
              </td>
              <td>{fromLbl(w) ?? DASH}</td>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.host ?? DASH}</td>
              <td className="mono">{w.tool ?? DASH}</td>
              {showArgs && <CmdCell text={w.args} style={{ fontSize: '10.5px' }} />}
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

/** One direction of per-tool I/O, ranked on its own counter: disk bytes as the
 *  bar, syscall bytes in the tip. A tool that never moved a byte this way is not
 *  a top reader (or writer) and is left off rather than drawn as a zero. */
function toolBars(
  rows: IoByTool[],
  dir: 'read' | 'write',
  color: string,
): HBarItem[] {
  const [disk, sys] = dir === 'read' ? (['rd_mb', 'rchar_mb'] as const) : (['wr_mb', 'wchar_mb'] as const);
  return rows
    .filter((t) => (t[disk] ?? 0) > 0)
    .sort((a, b) => (b[disk] ?? 0) - (a[disk] ?? 0))
    .slice(0, 14)
    .map((t) => ({
      k: t.tool,
      v: t[disk] ?? null,
      c: t.cls ? CLS_COL[t.cls] : color,
      lbl: mb(t[disk]),
      tip:
        `${t.tool}: ${dir === 'read' ? 'read' : 'wrote'} ${mb(t[disk])} on disk, ` +
        `${mb(t[sys])} through ${dir}() syscalls` +
        (dir === 'read' && t.cache_hit_pct != null ? `, cache hit ${pc(t.cache_hit_pct)}` : '') +
        `, coverage ${pc(t.io_coverage_pct)}, ${fint(t.events)} processes exited`,
    }));
}

/** The rules the Risk tab counted with: they differ by cluster (a `risk`
 *  section in the service config, or a profile file), so the page says which. */
function RulesInEffect({ r }: { r: RiskRules | undefined }) {
  const s = useSort<string>();
  if (!r) return <NoRows what="the service sent no rule list" />;
  const all = [
    ...r.dangerous.map((x) => ({ ...x, kind: 'dangerous command' })),
    ...r.secrets.map((x) => ({ ...x, kind: 'credential' })),
  ];
  const changed = all.filter((x) => x.source !== 'built-in').length;
  const secTitle = Object.fromEntries((r.sections ?? []).map((sc) => [sc.id, sc.title]));
  const unsectioned = r.unsectioned ?? [];
  return (
    <>
      <p className="paths" style={{ marginTop: 0 }}>
        {r.profile ? `profile ${r.profile}` : 'built-in rules, no cluster profile'}
        {` · ${fint(all.length)} rules in effect`}
        {changed ? ` · ${fint(changed)} changed or added for this cluster` : ''}
        {r.disabled.length ? ` · ${fint(r.disabled.length)} switched off` : ''}
        {unsectioned.length ? ` · ${fint(unsectioned.length)} in no section, so not counted` : ''}
        {' · set in the risk section of the config, or a profile file (dashboard/risk-profiles/)'}
      </p>
      {r.sections && (
        <p className="paths" style={{ marginTop: 0 }}>
          {'sections, in order: '}
          {r.sections.length
            ? r.sections.map((sc) => (sc.kind === 'login_compute' ? sc.title : `${sc.title} (${fint(sc.rules.length)})`)).join(' · ')
            : 'none'}
        </p>
      )}
      {(r.invalid.length > 0 || r.unknown.length > 0) && (
        <div className="err">
          {r.invalid.map((x) => (
            <div key={`i${x.kind}${x.id}`}>{`rule ${x.id ?? '(no id)'} (${x.kind}) left out: ${x.error}`}</div>
          ))}
          {r.unknown.map((x) => (
            <div key={`u${x.kind}${x.id}`}>
              {x.kind === 'login_compute'
                ? `no tool bucket called ${x.id} (login_compute): it counts nothing`
                : `no ${x.kind} rule called ${x.id}: check the id`}
            </div>
          ))}
        </div>
      )}
      <More label={`every rule, its pattern, and where it came from · ${fint(all.length)}`}>
        <div className="scroller vscroll">
          <table>
            <thead>
              <tr>
                <SortTh s={s} k="kind" first="asc">category</SortTh>
                <SortTh s={s} k="rule" first="asc">rule</SortTh>
                <SortTh s={s} k="sev" first="asc" tip={GLOSS.severity}>severity</SortTh>
                <SortTh s={s} k="from" first="asc">from</SortTh>
                <SortTh s={s} k="section" first="asc">section</SortTh>
                <SortTh s={s} k="pattern" first="asc">matches</SortTh>
              </tr>
            </thead>
            <tbody>
              {/* the switched-off and unsectioned rules stay after the rest */}
              {s.sort(all, (x, k) =>
                k === 'kind' ? x.kind : k === 'rule' ? x.label : k === 'sev' ? x.severity : k === 'from' ? x.source
                : k === 'section' ? (x.section ? secTitle[x.section] ?? x.section : null) : x.pattern_resolved,
              ).map((x) => (
                <tr key={x.kind + x.id}>
                  <td className="nm">{x.kind}</td>
                  <td>
                    {x.label}
                    <div className="mono nm" style={{ fontSize: '10.5px' }}>{x.id}</div>
                  </td>
                  <td>{x.severity}</td>
                  <td style={x.source !== 'built-in' ? { color: 'var(--crimson-ink)' } : undefined}>{x.source}</td>
                  <td>{x.section ? secTitle[x.section] ?? x.section : DASH}</td>
                  <CmdCell text={x.pattern_resolved} style={{ fontSize: '10.5px' }} />
                </tr>
              ))}
              {r.disabled.map((x) => (
                <tr key={`d${x.kind}${x.id}`} className="nm">
                  <td>{x.kind === 'dangerous' ? 'dangerous command' : 'credential'}</td>
                  <td>
                    <s>{x.label}</s>
                    <div className="mono" style={{ fontSize: '10.5px' }}>{x.id}</div>
                  </td>
                  <td>{DASH}</td>
                  <td>switched off</td>
                  <td>{DASH}</td>
                  <td>{DASH}</td>
                </tr>
              ))}
              {unsectioned.map((x) => (
                <tr key={`n${x.kind}${x.id}`} className="nm">
                  <td>{x.kind === 'dangerous' ? 'dangerous command' : 'credential'}</td>
                  <td>
                    {x.label}
                    <div className="mono" style={{ fontSize: '10.5px' }}>{x.id}</div>
                  </td>
                  <td>{DASH}</td>
                  <td>{DASH}</td>
                  <td>in no section: not counted</td>
                  <td>{DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </More>
    </>
  );
}

/* Two tabs from one reduction: Risk (what agents did that they should not have,
 * on a shared login node) and I/O (what their reads, writes and traffic cost).
 * They read the same panels, so they share the setup and each renders its half. */
export function RiskIoTab({ P, cls, part, plates = true, series = true }: {
  P: Panels;
  cls: Cls[];
  part: 'risk' | 'io';
  /** the I/O tiles: off on Resources, where their numbers are Overview rows */
  plates?: boolean;
  /** the syscall-against-disk time series: off on Resources, they are on Live */
  series?: boolean;
}) {
  // A command or pattern row opens in place into who ran it; the old list of
  // per-row dropdowns under the table named every row a second time.
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const isOpen = (id: string) => !!opened[id];
  const rowOpen = (id: string, can: boolean) =>
    can
      ? { className: `xrow${isOpen(id) ? ' open' : ''}`, onClick: () => setOpened((m) => ({ ...m, [id]: !m[id] })) }
      : {};
  const io = pick<IoProcess>(P, 'io_process', 'ebpfm');
  const byClass = io.by_class ?? {};
  const have = shown(byClass, cls);
  // every table here sorts by any column (roadmap note 27), each on its own
  const sortOf = useSortMap();

  const totRd = have.reduce((a, c) => a + (byClass[c]?.rd_mb ?? 0), 0);
  const totWr = have.reduce((a, c) => a + (byClass[c]?.wr_mb ?? 0), 0);

  /* Disk against syscall bytes over time, one axis per direction, MB/min.
   * Solid is what reached the disk, dashed is what passed through read() or
   * write(): the gap between a class's two lines IS the page cache. Binned by
   * exit time, like every series on the Resources tab. */
  const res = pick<Resources>(P, 'resources', 'ebpfm');
  const rs = res.series;
  const ioCls = shown(rs?.by_class, cls);
  const [zoom, setZoom] = useState<View>(null);
  const ioLines = (disk: 'rd_mb' | 'wr_mb', sys: 'rchar_mb' | 'wchar_mb', sysWord: string): TimeLine[] =>
    ioCls.flatMap((c) => [
      { lbl: `${CLS_LBL[c] ?? c} · disk`, c: CLS_COL[c] ?? 'var(--ink-3)', v: rs?.by_class[c]?.[disk] },
      { lbl: `${CLS_LBL[c] ?? c} · ${sysWord}`, c: CLS_COL[c] ?? 'var(--ink-3)', v: rs?.by_class[c]?.[sys], dashed: true },
    ]);
  const ioLegend = (sysWord: string) => [
    ...ioCls.map((c) => ({ k: CLS_LBL[c] ?? c, c: CLS_COL[c] ?? 'var(--ink-3)' })),
    { k: 'disk', c: 'var(--ink-2)', solid: true },
    { k: sysWord, c: 'var(--ink-2)', dashed: true },
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
  const toolReads = toolBars(byTool, 'read', 'var(--s2)');
  const toolWrites = toolBars(byTool, 'write', 'var(--s1)');
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
  const dgRows = dg?.rows ?? [];
  const secRows = sec?.rows ?? [];
  // The Risk tab's sections, in the cluster's order (risk.sections); a payload
  // from before sections existed reads as the three it always had.
  const sections: RiskSection[] = risk.sections ?? [
    ...(lc ? [{ id: 'login_compute', kind: 'login_compute' as const, title: 'Compute on a login node', rules: [] }] : []),
    ...(dg ? [{ id: 'commands', kind: 'commands' as const, title: 'Commands that should not run here', rules: dgRows.map((r) => r.id) }] : []),
    ...(sec ? [{ id: 'credentials', kind: 'credentials' as const, title: 'Credentials exposed in argv', rules: secRows.map((r) => r.id) }] : []),
  ];
  const inSection = <T extends { id: string }>(rows: T[], sc: RiskSection) => {
    const want = new Set(sc.rules);
    return rows.filter((r) => want.has(r.id));
  };
  const sumBy = (rows: { classes?: Partial<Record<string, number>> }[]) =>
    Object.fromEntries(have.map((c) => [c, rows.reduce((a, r) => a + (r.classes?.[c] ?? 0), 0)]));
  const quantKeys = Array.from(
    new Set(have.flatMap((c) => Object.keys(byClass[c]?.quants ?? {}))),
  );

  return (
    <>
      {/* The headline is the agent's; the other classes sit beside it for scale. */}
      {part === 'risk' && (
      <div className={`plates${sections.length === 3 ? ' three' : sections.length === 2 ? ' two' : ''}`}>
        {sections.slice(0, 4).map((sc) => {
          if (sc.kind === 'login_compute') {
            return (
              <Plate
                key={sc.id}
                k={sc.title}
                total={cls.includes('agent') ? lc?.by_class?.agent?.cpu_h ?? (lc ? 0 : null) : null}
                stripe="var(--s8)"
                by={Object.fromEntries(have.map((c) => [c, lc?.by_class?.[c]?.cpu_h ?? (lc ? 0 : null)]))}
                fmtFn={(n) => fmt(n)}
                unit="CPU-h"
                tip="agents' CPU in builds and interpreters (make, gcc, python, …) on a login node, which is shared and not for compute; the other classes beside it"
                note={lc ? `${fint(lc.by_class?.agent?.users ?? 0)} agent users · all classes ${pc(lc.share_of_all_cpu_pct)} of the node's CPU` : undefined}
              />
            );
          }
          if (sc.kind === 'credentials') {
            const rows = inSection(secRows, sc);
            const by = sumBy(rows);
            return (
              <Plate
                key={sc.id}
                k={sc.title}
                total={cls.includes('agent') ? by.agent ?? 0 : null}
                stripe="var(--s7)"
                by={by}
                fmtFn={fint}
                tip="credentials agents wrote into a command line, where any user on the node can read them with ps; the other classes beside it. Counted, never shown."
                note={sec ? `of ${fint(sec.args_scanned)} command lines scanned · values never shown` : undefined}
              />
            );
          }
          const rows = inSection(dgRows, sc);
          const by = sumBy(rows);
          const top = [...rows]
            .filter((r) => (r.classes?.agent ?? 0) > 0)
            .sort((x, y) => (y.classes?.agent ?? 0) - (x.classes?.agent ?? 0))
            .slice(0, 2);
          return (
            <Plate
              key={sc.id}
              k={sc.title}
              total={cls.includes('agent') ? by.agent ?? 0 : null}
              stripe="var(--crit)"
              by={by}
              fmtFn={fint}
              tip={`agents' runs matching this section's rules (${sc.rules.length}); the other classes beside it`}
              note={top.length ? top.map((r) => `${r.command} ×${fint(r.classes?.agent ?? 0)}`).join(' · ') : 'none in the window'}
            />
          );
        })}
      </div>
      )}

      {part === 'io' && plates && (
      <div className="plates">
        <Plate
          k="agent disk read"
          total={cls.includes('agent') ? byClass.agent?.rd_mb ?? null : null}
          stripe="var(--s2)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.rd_mb ?? null]))}
          fmtFn={(n) => mb(n)}
          note={totRd ? `${pc((100 * (byClass.agent?.rd_mb ?? 0)) / totRd)} of all disk reads` : undefined}
        />
        <Plate
          k="agent disk written"
          total={cls.includes('agent') ? byClass.agent?.wr_mb ?? null : null}
          stripe="var(--s1)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.wr_mb ?? null]))}
          fmtFn={(n) => mb(n)}
          note={totWr ? `${pc((100 * (byClass.agent?.wr_mb ?? 0)) / totWr)} of all disk writes` : undefined}
        />
        <Plate
          k="agent network sent"
          total={cls.includes('agent') ? (byClass.agent?.net_tx_mb?.n ? byClass.agent.net_tx_mb.total ?? null : null) : null}
          stripe="var(--s4)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.net_tx_mb?.n ? byClass[c]!.net_tx_mb.total ?? null : null]))}
          fmtFn={(n) => mb(n)}
          tip="bytes sent over the network by the processes, as each exited"
          note={byClass.agent?.net_tx_mb ? `measured on ${pc(byClass.agent.net_tx_mb.coverage_pct)} of agent processes` : undefined}
        />
        <Plate
          k="agent network received"
          total={cls.includes('agent') ? (byClass.agent?.net_rx_mb?.n ? byClass.agent.net_rx_mb.total ?? null : null) : null}
          stripe="var(--s5)"
          by={Object.fromEntries(have.map((c) => [c, byClass[c]?.net_rx_mb?.n ? byClass[c]!.net_rx_mb.total ?? null : null]))}
          fmtFn={(n) => mb(n)}
          tip="bytes received over the network by the processes, as each exited"
          note={byClass.agent?.net_rx_mb ? `measured on ${pc(byClass.agent.net_rx_mb.coverage_pct)} of agent processes` : undefined}
        />
      </div>
      )}

      {part === 'io' && (
      <div className="grid wide">
        {series && (
        <Panel
          title="Syscall bytes against disk bytes"
          p={res}
          span
          empty={!ioCls.length || !rs?.t.length}
          emptyWhat="no class block for the selection"
        >
          {rs && (
            <div className="pair-row">
              <div>
                <div className="eyebrow">read · solid from disk, dashed through read()</div>
                <TimeChart s={rs} lines={ioLines('rd_mb', 'rchar_mb', 'read()')} ylabel="MB / min"
                  yname="read" perMin view={zoom} onView={setZoom} legend={ioLegend('read() calls')} />
              </div>
              <div>
                <div className="eyebrow">written · solid to disk, dashed through write()</div>
                <TimeChart s={rs} lines={ioLines('wr_mb', 'wchar_mb', 'write()')} ylabel="MB / min"
                  yname="written" perMin view={zoom} onView={setZoom} legend={ioLegend('write() calls')} />
              </div>
            </div>
          )}
          <More label="bytes per class">
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <SortTh s={sortOf('io-class')} k="cls" first="asc">class</SortTh>
                    <SortTh s={sortOf('io-class')} k="events" num tip="processes exited">processes</SortTh>
                    <SortTh s={sortOf('io-class')} k="events_with_io" num tip="processes that read or wrote anything, and the share of the class they are">with I/O</SortTh>
                    <SortTh s={sortOf('io-class')} k="rchar_mb" num tip={GLOSS.rchar}>rchar</SortTh>
                    <SortTh s={sortOf('io-class')} k="rd_mb" num tip={GLOSS.rd}>rd</SortTh>
                    <SortTh s={sortOf('io-class')} k="wchar_mb" num tip={GLOSS.wchar}>wchar</SortTh>
                    <SortTh s={sortOf('io-class')} k="wr_mb" num tip={GLOSS.wr}>wr</SortTh>
                    <SortTh s={sortOf('io-class')} k="cache_served_mb" num tip={GLOSS.cacheserved}>cache served</SortTh>
                    <SortTh s={sortOf('io-class')} k="cache_hit_pct" num tip={GLOSS.cachehit}>cache hit</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {sortOf('io-class').sort(have, (c, k) =>
                    k === 'cls' ? CLS_LBL[c] ?? c : ((byClass[c] as unknown as Record<string, number | null>)?.[k] ?? null),
                  ).map((c) => {
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
          </More>
        </Panel>
        )}

        <Panel
          title="Reads by tool"
          ask={[{ panel: 'io_process', keys: ['by_tool', 'notes'] }]}
          p={io}
          empty={!toolReads.length}
          emptyWhat="no tool read from disk in the selection"
        >
          <HBars items={toolReads} labelW={150} valW={100} />
        </Panel>

        <Panel
          title="Writes by tool"
          ask={[{ panel: 'io_process', keys: ['by_tool', 'notes'] }]}
          p={io}
          empty={!toolWrites.length}
          emptyWhat="no tool wrote to disk in the selection"
        >
          <HBars items={toolWrites} labelW={150} valW={100} />
        </Panel>

        <Panel
          title="Network bytes"
          ask={[{ panel: 'io_process', keys: ['by_class', 'by_tool', 'notes'] }]}
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
          <More label="network bytes per class">
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <SortTh s={sortOf('net-class')} k="cls" first="asc">class</SortTh>
                    <SortTh s={sortOf('net-class')} k="tx" num tip={GLOSS.tx}>tx</SortTh>
                    <SortTh s={sortOf('net-class')} k="rx" num tip={GLOSS.rx}>rx</SortTh>
                    <SortTh s={sortOf('net-class')} k="calls" num tip={GLOSS.netcalls}>calls</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {sortOf('net-class').sort(have, (c, k) =>
                    k === 'cls' ? CLS_LBL[c] ?? c
                    : k === 'tx' ? byClass[c]?.net_tx_mb?.total ?? null
                    : k === 'rx' ? byClass[c]?.net_rx_mb?.total ?? null
                    : byClass[c]?.net_calls?.total ?? null,
                  ).map((c) => (
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
                      <td className="num">
                        <AccumCell a={byClass[c]?.net_calls} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </More>
        </Panel>

        <Panel
          title="I/O by user"
          ask={[{ panel: 'io_process', keys: ['by_user', 'notes'] }]}
          p={io}
          span
          empty={!byUser.length}
          emptyWhat="no per-user I/O rows for the selection"
        >
          {/* Every row the service sent: it is already the union of the top N
              on each read and write counter, and cutting it again here would
              drop exactly the write-heavy users that union exists to keep. */}
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th colSpan={3} />
                  <th className="num" colSpan={2}>read</th>
                  <th className="num" colSpan={2}>write</th>
                  <th className="num" colSpan={2}>network</th>
                  <th />
                </tr>
                <tr>
                  <SortTh s={sortOf('io-user')} k="user" first="asc">user</SortTh>
                  <SortTh s={sortOf('io-user')} k="cls" first="asc">class</SortTh>
                  <SortTh s={sortOf('io-user')} k="events" num tip="processes exited">processes</SortTh>
                  <SortTh s={sortOf('io-user')} k="rd_mb" num tip="block-layer bytes read (read_bytes)">disk</SortTh>
                  <SortTh s={sortOf('io-user')} k="rchar_mb" num tip="bytes through read() syscalls (rchar)">syscall</SortTh>
                  <SortTh s={sortOf('io-user')} k="wr_mb" num tip="block-layer bytes written (write_bytes)">disk</SortTh>
                  <SortTh s={sortOf('io-user')} k="wchar_mb" num tip="bytes through write() syscalls (wchar)">syscall</SortTh>
                  <SortTh s={sortOf('io-user')} k="net_rx_mb" num tip={GLOSS.rx}>rx</SortTh>
                  <SortTh s={sortOf('io-user')} k="net_tx_mb" num tip={GLOSS.tx}>tx</SortTh>
                  <SortTh s={sortOf('io-user')} k="io_coverage_pct" num tip={GLOSS.coverage}>coverage</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortOf('io-user').sort(byUser, (u, k) =>
                  k === 'cls' ? (u.cls ? CLS_LBL[u.cls] ?? u.cls : null)
                  : ((u as unknown as Record<string, string | number | null | undefined>)[k] ?? null),
                ).map((u, i) => (
                  <tr key={u.user + i}>
                    <td className="mono">{u.user}</td>
                    <td>{u.cls ? (CLS_LBL[u.cls] ?? u.cls) : DASH}</td>
                    <td className="num">{fint(u.events)}</td>
                    <td className="num">{mb(u.rd_mb)}</td>
                    <td className="num">{mb(u.rchar_mb)}</td>
                    <td className="num">{mb(u.wr_mb)}</td>
                    <td className="num">{mb(u.wchar_mb)}</td>
                    <td className="num">{mb(u.net_rx_mb)}</td>
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
          <Panel title="I/O distributions" p={io} span empty={!have.length}
            ask={[{ panel: 'io_process', keys: ['by_class', 'notes'] }]}>
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <SortTh s={sortOf('io-quant')} k="metric" first="asc" tip="the raw counter each row describes; hover a name for what it counts">metric</SortTh>
                    <SortTh s={sortOf('io-quant')} k="cls" first="asc">class</SortTh>
                    <QuantHead s={sortOf('io-quant')} />
                  </tr>
                </thead>
                <tbody>
                  {sortOf('io-quant').sort(quantKeys.flatMap((qk) => have.map((c) => [qk, c] as const)),
                    ([qk, c], k) => (k === 'metric' ? qk : k === 'cls' ? CLS_LBL[c] ?? c
                      : quantVal(byClass[c]?.quants?.[qk], k)),
                  ).map(([qk, c]) => (
                      <tr key={qk + c}>
                        <td className="mono lbl" data-tip={GLOSS[qk]}>{qk}</td>
                        <td>{CLS_LBL[c] ?? c}</td>
                        <QuantCells q={byClass[c]?.quants?.[qk]} />
                      </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </div>
      )}

      {part === 'risk' && (
      <div className="grid wide">
        {/* One panel per section, in the order this cluster's risk profile
            lists them (risk.sections); a section it leaves out is not drawn and
            not computed. */}
        {sections.length === 0 && (
          <Panel title="No risk sections" span>
            <NoRows what="this cluster's risk profile lists no sections (risk.sections)" />
          </Panel>
        )}
        {sections.map((sc) =>
          sc.kind === 'login_compute' ? (
            <Panel
              key={sc.id}
              title={sc.title}
              ask={[{ panel: 'risk', keys: ['login_compute', 'sections'] }]}
              p={risk}
              span
              empty={!lcRows.length}
              emptyWhat="no build or interpreter CPU in the window"
              basis={lc?.basis}
            >
              <div className="eyebrow">
                {fmt(lc?.compute_cpu_h ?? 0, 3)} CPU-h of compute{' '}
                <span style={{ color: 'var(--muted)' }}>
                  {'\u00b7'} {pc(lc?.share_of_all_cpu_pct ?? 0)} of all CPU
                </span>
              </div>
              <HBars
                items={lcRows.slice(0, 12).map((r) => ({
                  k: r.tool,
                  v: r.cpu_s,
                  c: r.kind === 'compute' ? 'var(--s8)' : 'var(--sg)',
                  lbl: `${dur(r.cpu_s)} ${'\u00b7'} ${fint(r.users)} user${r.users === 1 ? '' : 's'}`,
                  tip: `${r.tool}: ${fmt(r.cpu_s)} CPU-s over ${fint(r.n)} exited processes, ${r.users} user(s), hosts ${r.hosts.join(', ')}`,
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
              <More label="per tool, and the heaviest processes">
                <div className="scroller vscroll" style={{ marginTop: 8 }}>
                  <table>
                    <thead>
                      <tr>
                        <SortTh s={sortOf(sc.id)} k="tool" first="asc">tool</SortTh>
                        <SortTh s={sortOf(sc.id)} k="kind" first="asc">category</SortTh>
                        <SortTh s={sortOf(sc.id)} k="n" num>times run</SortTh>
                        <SortTh s={sortOf(sc.id)} k="cpu" num>CPU time</SortTh>
                        <SortTh s={sortOf(sc.id)} k="pct" num>% of CPU</SortTh>
                        <SortTh s={sortOf(sc.id)} k="users" num>users</SortTh>
                        <SortTh s={sortOf(sc.id)} k="hosts" first="asc">hosts</SortTh>
                        <th>run by</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortOf(sc.id).sort(lcRows, (r, k) =>
                        k === 'tool' ? r.tool : k === 'kind' ? (r.kind ? LC_KIND[r.kind] ?? r.kind : null) : k === 'n' ? r.n
                        : k === 'cpu' ? r.cpu_s : k === 'pct' ? r.cpu_pct : k === 'users' ? r.users : r.hosts.join(' '),
                      ).map((r, i) => (
                        <tr key={r.tool + i}>
                          <td className="mono">{r.tool}</td>
                          <td>{r.kind ? LC_KIND[r.kind] ?? r.kind : DASH}</td>
                          <td className="num">{fint(r.n)}</td>
                          <td className="num">{dur(r.cpu_s)}</td>
                          <td className="num">{pc(r.cpu_pct)}</td>
                          <td className="num">{fint(r.users)}</td>
                          <td className="mono" style={{ fontSize: '10.5px' }}>{r.hosts.join(' ')}</td>
                          <td><WhoChips r={r} cls={cls} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="eyebrow" style={{ marginTop: 10 }}>heaviest individual processes</div>
                <InstanceTable rows={lc?.top_procs ?? []} showArgs />
              </More>
            </Panel>
          ) : sc.kind === 'credentials' ? (
            <Panel
              key={sc.id}
              title={sc.title}
              ask={[{ panel: 'risk', keys: ['secrets', 'sections', 'rules'] }]}
              p={risk}
              span
              empty={!inSection(secRows, sc).length}
              emptyWhat="no credential pattern matched in the window"
              basis={[sec?.redaction, sec?.undercount].filter(Boolean).join(' ')}
            >
              <div className="eyebrow">
                {fint(sec?.args_scanned ?? 0)} command lines scanned
              </div>
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <SortTh s={sortOf(sc.id)} k="pattern" first="asc">pattern</SortTh>
                      <SortTh s={sortOf(sc.id)} k="sev" first="asc" tip={GLOSS.severity}>severity</SortTh>
                      <SortTh s={sortOf(sc.id)} k="n" num>matches</SortTh>
                      <SortTh s={sortOf(sc.id)} k="users" num>users</SortTh>
                      <SortTh s={sortOf(sc.id)} k="tools" first="asc">tools</SortTh>
                      <th>run by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortOf(sc.id).sort(inSection(secRows, sc), (r, k) =>
                      k === 'pattern' ? r.pattern : k === 'sev' ? r.severity : k === 'n' ? r.n
                      : k === 'users' ? r.users : r.tools.join(' '),
                    ).map((r) => (
                      <Fragment key={r.id}>
                      <tr {...rowOpen(r.id, (r.instances ?? []).length > 0)}>
                        <td>
                          <Caret on={isOpen(r.id)} show={(r.instances ?? []).length > 0} />
                          {r.pattern}
                        </td>
                        <td><span className={`sev ${r.severity}`}>{r.severity}</span></td>
                        <td className="num">{fint(r.n)}</td>
                        <td className="num">{fint(r.users)}</td>
                        <td className="mono" style={{ fontSize: '10.5px' }}>{r.tools.join(' ')}</td>
                        <td><WhoChips r={r} cls={cls} /></td>
                      </tr>
                      {isOpen(r.id) && (
                        <tr className="xsub">
                          <td colSpan={6}>
                            <InstanceTable rows={r.instances ?? []} showArgs={false} />
                            <Capped shown={(r.instances ?? []).length} of={r.n} noun="match" />
                          </td>
                        </tr>
                      )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          ) : (
            <Panel
              key={sc.id}
              title={sc.title}
              ask={[{ panel: 'risk', keys: ['dangerous', 'sections', 'rules'] }]}
              p={risk}
              span
              empty={!inSection(dgRows, sc).length}
              emptyWhat="no matching command in the window"
              basis={[dg?.basis, dg?.site_assumption].filter(Boolean).join(' ')}
            >
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <SortTh s={sortOf(sc.id)} k="command" first="asc">command</SortTh>
                      <SortTh s={sortOf(sc.id)} k="sev" first="asc" tip={GLOSS.severity}>severity</SortTh>
                      <SortTh s={sortOf(sc.id)} k="n" num>times run</SortTh>
                      <SortTh s={sortOf(sc.id)} k="users" num>users</SortTh>
                      <th>run by</th>
                      <SortTh s={sortOf(sc.id)} k="works" num tip="runs that exited 0: nothing stopped them">succeeded</SortTh>
                      <SortTh s={sortOf(sc.id)} k="cpu" num>CPU s</SortTh>
                    </tr>
                  </thead>
                  <tbody>
                    {sortOf(sc.id).sort(inSection(dgRows, sc), (r, k) =>
                      k === 'command' ? r.command : k === 'sev' ? r.severity : k === 'n' ? r.n : k === 'users' ? r.users
                      : k === 'works' ? r.works_pct : r.cpu_s,
                    ).map((r) => (
                      <Fragment key={r.id}>
                      <tr {...rowOpen(r.id, (r.instances ?? []).length > 0)}>
                        <td className="mono">
                          <Caret on={isOpen(r.id)} show={(r.instances ?? []).length > 0} />
                          {r.command}
                        </td>
                        <td><span className={`sev ${r.severity}`}>{r.severity}</span></td>
                        <td className="num">{fint(r.n)}</td>
                        <td className="num">{fint(r.users)}</td>
                        <td><WhoChips r={r} cls={cls} /></td>
                        <td className="num">
                          {r.works_pct == null ? (
                            <span className="nm">no verdict</span>
                          ) : (
                            <span data-tip={`${fint(r.succeeded)} of the ${fint(r.rated)} runs with an exit code exited 0; ${fint(r.n - r.rated)} ended on a signal and carry no verdict`}>
                              {pc(r.works_pct)}
                              <span style={{ color: 'var(--muted)' }}>{` · ${fint(r.succeeded)} of ${fint(r.rated)}`}</span>
                            </span>
                          )}
                        </td>
                        <td className="num">{fmt(r.cpu_s)}</td>
                      </tr>
                      {isOpen(r.id) && (
                        <tr className="xsub">
                          <td colSpan={7}>
                            <InstanceTable rows={r.instances ?? []} showArgs />
                            <Capped shown={(r.instances ?? []).length} of={r.n} noun="run" />
                          </td>
                        </tr>
                      )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          ),
        )}

        <Panel title="Rules in effect" p={risk} span empty={!risk.rules} emptyWhat="the service sent no rule list"
          ask={[{ panel: 'risk', keys: ['rules', 'sections', 'totals'] }]}>
          <RulesInEffect r={risk.rules} />
        </Panel>
      </div>
      )}
    </>
  );
}
