/* Risk & I/O -- the `io_process` payload.
 *
 * `rchar/wchar` against `rd/wr` is the page-cache question, and `cache_hit_pct`
 * is meaningless without its coverage, so every accumulator here is rendered
 * with the coverage attached rather than as a bare total.
 */
import { GLOSS } from '../lib/glossary';
import { Fragment, memo, useState } from 'react';
import type { Cls, IoByTool, IoProcess, IoSort } from '../api/types';
import { Fluid, HBars, HGroupBars } from '../charts/primitives';
import { TOOL_COL } from '../charts/TimeChart';
import type { BarSeries, HBarCell, HBarGroup } from '../charts/primitives';
import { Legend, More, NoRows, NotReturned, Panel } from '../components/Panel';
import { Coverage, QuantCells, QuantHead, quantVal } from '../components/Coverage';
import { Plate } from '../components/Plate';
import { AGENT_TYPE_LBL, CLS_COL, CLS_LBL } from '../lib/classes';
import { ByUser, distinct } from '../components/ByUser';
import { CmdCell } from '../components/Cmd';
import { DASH, dur, fint, fmt, mb, pc } from '../lib/format';
import { pick, shown } from '../lib/panels';
import type { Panels } from '../lib/panels';
import { coveredBy, openIoCmds } from '../lib/iocmds';
import { IoNum } from '../components/IoCmds';
import { NetEndpoints } from './NetEndpoints';
import type { RiskInstance, RiskPanel, RiskRules, RiskSection } from '../api/types';
import { SortTh } from '../components/SortTh';
import { useSort, useSortMap } from '../lib/useSort';
import { useNodeWords } from '../lib/nodes';
import { table } from '../lib/lookup';


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
const LC_KIND: Record<string, string> = table({ compute: 'compute', transfer: 'bulk transfer' });

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
        sub={(rs, withUser) => <InstanceRows rows={rs} showArgs={showArgs} withUser={withUser} />}
      />
    </div>
  );
}

/** One user's processes, as rows; every user's, with a user column. */
function InstanceRows({ rows, showArgs, withUser }: { rows: RiskInstance[]; showArgs: boolean; withUser?: boolean }) {
  const nodes = useNodeWords();
  const s = useSort<string>('at');
  if (!rows.length) return null;
  const val = (w: RiskInstance, k: string) =>
    k === 'at' ? w.ts : k === 'user' ? w.user : k === 'pid' ? w.pid : k === 'from' ? fromLbl(w) : k === 'host' ? w.host
    : k === 'tool' ? w.tool : k === 'cmd' ? w.args : k === 'cpu' ? w.cpu_s
    : w.signal != null ? 1000 + w.signal : w.exit_code;
  return (
    // flat, it sits in the list's own scroll box, which holds its header in view
    <div className={withUser ? undefined : 'scroller'}>
      <table>
        <thead>
          <tr>
            <SortTh s={s} k="at">at</SortTh>
            {withUser && <SortTh s={s} k="user" first="asc">user</SortTh>}
            <SortTh s={s} k="pid" num>pid</SortTh>
            <SortTh s={s} k="from" first="asc">from</SortTh>
            <SortTh s={s} k="host" first="asc">{nodes.one}</SortTh><SortTh s={s} k="tool" first="asc">tool</SortTh>
            {showArgs && <SortTh s={s} k="cmd" first="asc">command</SortTh>}
            <SortTh s={s} k="cpu" num>CPU</SortTh>
            <SortTh s={s} k="exit" num tip={GLOSS.exitcode}>exit code</SortTh>
          </tr>
        </thead>
        <tbody>
          {s.sort(rows, val).map((w, i) => (
            <tr key={`${w.pid}-${i}`}>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.ts ?? DASH}</td>
              {withUser && <td className="mono">{w.user ?? DASH}</td>}
              <td className="num mono" style={{ fontSize: '12px' }}
                  data-tip={w.ppid != null ? `parent pid ${w.ppid}` : undefined}>
                {w.pid ?? DASH}
              </td>
              <td>{fromLbl(w) ?? DASH}</td>
              <td className="mono" style={{ color: 'var(--muted)' }}>{w.host ?? DASH}</td>
              <td className="mono">{w.tool ?? DASH}</td>
              {showArgs && <CmdCell text={w.args} style={{ fontSize: '12px' }} />}
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

/** A per-process counter's drawer sort: its commands, the heaviest first. */
const QUANT_SORT: Record<string, IoSort | undefined> = {
  rd_mb: 'rd_mb', wr_mb: 'wr_mb', rchar_mb: 'rchar_mb', wchar_mb: 'wchar_mb',
  net_tx_mb: 'tx_mb', net_rx_mb: 'rx_mb',
};
/** A per-process counter in the page's words; its raw name is on hover. */
const QUANT_LBL: Record<string, string> = table({
  rd_mb: 'disk read (MB)', wr_mb: 'disk written (MB)', rchar_mb: 'read requested (MB)', wchar_mb: 'write requested (MB)',
  net_tx_mb: 'sent (MB)', net_rx_mb: 'received (MB)',
});
const quantLbl = (k: string) => QUANT_LBL[k] ?? k;

/** The tools each disk counter ranks: a tool among the heaviest on either is drawn. */
const TOOL_ROWS = 14;

/* The two bars of a tool's row are directions, not classes, so they wear hues
 * no class owns: a tool's bar in crimson read as "every write was an agent's". They are
 * the network chart's hues for the same direction -- into a process (read,
 * received) and out of it (written, sent) -- so one hue means one direction
 * across the I/O panels. */
const DISK_SERIES: BarSeries[] = [
  { k: 'read', lbl: 'disk read', c: TOOL_COL[1] },
  { k: 'written', lbl: 'disk written', c: TOOL_COL[0] },
];

/** Per-tool disk I/O, a row per tool: its disk read and disk written as two
 *  bars on one scale, the bytes requested in each bar's tip. The rows are the
 *  14 heaviest on each counter, so a tool that only writes is drawn wherever a
 *  chart of writes alone would have drawn it; the most disk bytes in all come
 *  first. A tool's 0 on the other counter is drawn as the 0 it measured, and
 *  opens no commands: none moved a byte that way. A row the service split by
 *  class (its own `by_tool` has none) names its class after the tool, since
 *  the hue says the direction. */
function toolBars(rows: IoByTool[], from?: string | number | null): HBarGroup[] {
  const heaviest = (k: 'rd_mb' | 'wr_mb') =>
    rows
      .filter((t) => (t[k] ?? 0) > 0)
      .sort((a, b) => (b[k] ?? 0) - (a[k] ?? 0))
      .slice(0, TOOL_ROWS);
  const disk = (t: IoByTool) => (t.rd_mb ?? 0) + (t.wr_mb ?? 0);
  return Array.from(new Set([...heaviest('rd_mb'), ...heaviest('wr_mb')]))
    .sort((a, b) => disk(b) - disk(a))
    .map((t) => {
      const who = t.cls ? ` · ${CLS_LBL[t.cls] ?? t.cls}` : '';
      const bar = (dir: 'read' | 'write'): HBarCell => {
        const [k, sys] = dir === 'read' ? (['rd_mb', 'rchar_mb'] as const) : (['wr_mb', 'wchar_mb'] as const);
        const done = dir === 'read' ? 'read' : 'written';
        const v = t[k] ?? null;
        return {
          v,
          lbl: mb(v),
          tip:
            `${t.tool}${who}: disk ${done} ${mb(v)}, ` +
            `${dir === 'read' ? 'read' : 'write'} requested ${mb(t[sys])}, through ${dir}() calls` +
            (dir === 'read' && t.cache_hit_pct != null ? `, cache hit ${pc(t.cache_hit_pct)}` : '') +
            `, coverage ${pc(t.io_coverage_pct)}, ${fint(t.events)} processes exited`,
          onPick: v != null && v > 0
            ? () => openIoCmds({
                title: `disk ${done} · ${t.tool}${who}`,
                sort: k, tool: [t.tool], class: t.cls ? [t.cls] : undefined, from,
              })
            : undefined,
        };
      };
      return { k: t.tool + who, v: { read: bar('read'), written: bar('write') } };
    });
}

/** The rules the Risks tab counted with: they differ by cluster (a `risk`
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
                    <div className="mono nm" style={{ fontSize: '12px' }}>{x.id}</div>
                  </td>
                  <td>{x.severity}</td>
                  <td style={x.source !== 'built-in' ? { color: 'var(--crimson-ink)' } : undefined}>{x.source}</td>
                  <td>{x.section ? secTitle[x.section] ?? x.section : DASH}</td>
                  <CmdCell text={x.pattern_resolved} style={{ fontSize: '12px' }} />
                </tr>
              ))}
              {r.disabled.map((x) => (
                <tr key={`d${x.kind}${x.id}`} className="nm">
                  <td>{x.kind === 'dangerous' ? 'dangerous command' : 'credential'}</td>
                  <td>
                    <s>{x.label}</s>
                    <div className="mono" style={{ fontSize: '12px' }}>{x.id}</div>
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
                    <div className="mono" style={{ fontSize: '12px' }}>{x.id}</div>
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
export const RiskIoTab = memo(function RiskIoTab({ P, cls, part, io: ioPart = 'main' }: {
  P: Panels;
  cls: Cls[];
  part: 'risk' | 'io';
  /** which I/O panels: `main` is who moved the bytes (by tool, by user) and
   *  where the network bytes went, on Resources; `internal` is the per-class
   *  page-cache table (its series over time are More over time's, on
   *  Resources: tabs/LiveCharts.tsx) and the raw counters' quantiles, on
   *  Internal */
  io?: 'main' | 'internal';
}) {
  const nodes = useNodeWords();
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
  // a section's table opens by its first column, A to Z
  const secSort = (sc: RiskSection) =>
    sortOf(sc.id, sc.kind === 'login_compute' ? 'tool' : sc.kind === 'credentials' ? 'pattern' : 'command', 'asc');

  // a class with a reading of the bytes it sent or received: an accumulator
  // with nothing in it is "not measured", not a 0
  const netRead = (c: string) =>
    (['net_tx_mb', 'net_rx_mb'] as const).some((k) => !!byClass[c]?.[k]?.n && byClass[c]![k].total != null);

  const byTool = (io.by_tool ?? []).filter((t) => !t.cls || cls.includes(t.cls as Cls));
  const toolDisk = toolBars(byTool, coveredBy(io).from);
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
  // The Risks tab's sections, in the cluster's order (risk.sections); a payload
  // from before sections existed reads as the three it always had.
  const sections: RiskSection[] = risk.sections ?? [
    ...(lc ? [{ id: 'login_compute', kind: 'login_compute' as const, title: 'Compute on a login node', rules: [] }] : []),
    ...(dg ? [{ id: 'commands', kind: 'commands' as const, title: 'Commands that should not run here', rules: dgRows.map((r) => r.id) }] : []),
    ...(sec ? [{ id: 'credentials', kind: 'credentials' as const, title: 'Credentials on a command line', rules: secRows.map((r) => r.id) }] : []),
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
                by={Object.fromEntries(have.map((c) => [c, lc?.by_class?.[c]?.cpu_h ?? (lc ? 0 : null)]))}
                fmtFn={(n) => fmt(n)}
                unit="CPU-h"
                tip="agents' CPU in builds and interpreters (make, gcc, python, …) on a shared login node"
                note={lc ? `${fint(lc.by_class?.agent?.users ?? 0)} agent users · all classes ${pc(lc.share_of_all_cpu_pct)} of the ${nodes.many}’ CPU` : undefined}
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
                by={by}
                fmtFn={fint}
                tip={`credentials agents typed into a command line, where any user on the ${nodes.one} can read them with ps; counted, never shown`}
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
              alarm={cls.includes('agent') && (by.agent ?? 0) > 0}
              by={by}
              fmtFn={fint}
              tip={`agents' runs matching this section's ${sc.rules.length} rules`}
              note={top.length ? top.map((r) => `${r.command} ×${fint(r.classes?.agent ?? 0)}`).join(' · ') : 'none in the window'}
            />
          );
        })}
      </div>
      )}

      {part === 'io' && (
      <div className="grid wide">
        {/* The bytes per class, over the window: what the programs asked for
            against what reached the disk, and the page cache's part between.
            The same bytes over time are More over time's, on Resources. */}
        {ioPart === 'internal' && (
        <Panel
          id="sec-syscall-bytes-against-disk-bytes"
          title="Requested bytes against disk bytes"
          p={io}
          span
          empty={!have.length}
          emptyWhat="no class block for the selection"
        >
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <SortTh s={sortOf('io-class')} k="cls" first="asc">class</SortTh>
                  <SortTh s={sortOf('io-class')} k="events" num tip="processes exited">processes</SortTh>
                  <SortTh s={sortOf('io-class')} k="events_with_io" num tip="processes that read or wrote anything, and the share of the class they are">with I/O</SortTh>
                  <SortTh s={sortOf('io-class')} k="rchar_mb" num tip={GLOSS.rchar}>read requested</SortTh>
                  <SortTh s={sortOf('io-class')} k="rd_mb" num tip={GLOSS.rd}>disk read</SortTh>
                  <SortTh s={sortOf('io-class')} k="wchar_mb" num tip={GLOSS.wchar}>write requested</SortTh>
                  <SortTh s={sortOf('io-class')} k="wr_mb" num tip={GLOSS.wr}>disk written</SortTh>
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
        </Panel>
        )}

        {ioPart === 'main' && (
        <>
        {/* the anchor the read chart had, so a link or a citation to it lands here */}
        <Panel
          id="sec-reads-by-tool"
          title="Disk read and written by tool"
          ask={[{ panel: 'io_process', keys: ['by_tool', 'notes'] }]}
          p={io}
          span
          empty={!toolDisk.length}
          emptyWhat="no tool read from or wrote to disk in the selection"
        >
          {/* as wide as the panel, which has the row to itself; never drawn under
              560, so in a narrower box it fits as HBars does (useFit) */}
          <Fluid natural={560}>
            {(w) => (
              <HGroupBars groups={toolDisk} series={DISK_SERIES} W={Math.max(560, w)} labelW={150} valW={100} fmtVal={mb} />
            )}
          </Fluid>
          <Legend items={DISK_SERIES.map((s) => ({ k: s.lbl, c: s.c }))} />
        </Panel>

        <Panel
          title="Network bytes"
          ask={[{ panel: 'io_process', keys: ['by_class', 'by_tool', 'endpoints', 'notes'] }]}
          p={io}
          span
          empty={!have.length || !have.some(netRead)}
          emptyWhat="no TCP accounting for the selection"
        >
          {/* Where each class's bytes went. The bytes themselves are Who uses
              what's sent and received rows, on Live, which open the same
              commands: a table of them here said it again. */}
          {io.endpoints ? (
            <NetEndpoints ep={io.endpoints} have={have} byClass={byClass} />
          ) : (
            <NotReturned feed={io._feed} notice="the I/O panel came without its endpoint list" />
          )}
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
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="user" first="asc" className="pin">user</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="cls" first="asc">class</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="events" num tip="processes exited">processes</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="rd_mb" num tip={GLOSS.rd}>disk</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="rchar_mb" num tip={GLOSS.rchar}>requested</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="wr_mb" num tip={GLOSS.wr}>disk</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="wchar_mb" num tip={GLOSS.wchar}>requested</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="net_rx_mb" num tip={GLOSS.rx}>received</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="net_tx_mb" num tip={GLOSS.tx}>sent</SortTh>
                  <SortTh s={sortOf('io-user', 'user', 'asc')} k="io_coverage_pct" num tip={GLOSS.coverage}>coverage</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortOf('io-user', 'user', 'asc').sort(byUser, (u, k) =>
                  k === 'cls' ? (u.cls ? CLS_LBL[u.cls] ?? u.cls : null)
                  : ((u as unknown as Record<string, string | number | null | undefined>)[k] ?? null),
                ).map((u, i) => (
                  <tr key={u.user + i}>
                    <td className="mono pin">{u.user}</td>
                    <td>{u.cls ? (CLS_LBL[u.cls] ?? u.cls) : DASH}</td>
                    <td className="num">{fint(u.events)}</td>
                    {([['rd_mb', u.rd_mb, 'disk read'], ['rchar_mb', u.rchar_mb, 'read requested'],
                      ['wr_mb', u.wr_mb, 'disk written'], ['wchar_mb', u.wchar_mb, 'write requested'],
                      ['rx_mb', u.net_rx_mb, 'network received'], ['tx_mb', u.net_tx_mb, 'network sent']] as const).map(([k, v, lbl]) => (
                      <td key={k} className="num">
                        {v ? (
                          <IoNum q={{ title: `${lbl} · ${u.user}${u.cls ? ` · ${CLS_LBL[u.cls] ?? u.cls}` : ''}`, sort: k,
                            user: [u.user], class: u.cls ? [u.cls] : undefined, ...coveredBy(io) }}>{mb(v)}</IoNum>
                        ) : mb(v)}
                      </td>
                    ))}
                    <td className="num">
                      <Coverage pct={u.io_coverage_pct} n={u.events} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
        </>
        )}

        {ioPart === 'internal' && quantKeys.length > 0 && (
          <Panel title="I/O distributions" p={io} span empty={!have.length}
            ask={[{ panel: 'io_process', keys: ['by_class', 'notes'] }]}>
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <SortTh s={sortOf('io-quant')} k="metric" first="asc" tip="hover a name for what it counts">metric</SortTh>
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
                        <td className="lbl" data-tip={GLOSS[qk] ?? qk}>
                          {QUANT_SORT[qk] ? (
                            <IoNum q={{ title: `${quantLbl(qk)} · ${CLS_LBL[c] ?? c}`, sort: QUANT_SORT[qk]!, class: [c], ...coveredBy(io) }}>{quantLbl(qk)}</IoNum>
                          ) : quantLbl(qk)}
                        </td>
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
        {/* none only of a risk panel the service reduced: while it is waited for,
            building, not reduced or not sent, the panel's envelope says which */}
        {sections.length === 0 && (
          <Panel title="Risk sections" p={risk} span empty
            emptyWhat="this cluster's risk profile lists no sections" />
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
                  tip: `${r.tool}: ${fmt(r.cpu_s)} CPU-s over ${fint(r.n)} exited processes, ${r.users} user(s), ${nodes.many} ${r.hosts.join(', ')}`,
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
                        <SortTh s={secSort(sc)} k="tool" first="asc">tool</SortTh>
                        <SortTh s={secSort(sc)} k="kind" first="asc">category</SortTh>
                        <SortTh s={secSort(sc)} k="n" num>times run</SortTh>
                        <SortTh s={secSort(sc)} k="cpu" num>CPU time</SortTh>
                        <SortTh s={secSort(sc)} k="pct" num>% of CPU</SortTh>
                        <SortTh s={secSort(sc)} k="users" num>users</SortTh>
                        <SortTh s={secSort(sc)} k="hosts" first="asc">{nodes.many}</SortTh>
                        <th>run by</th>
                      </tr>
                    </thead>
                    <tbody>
                      {secSort(sc).sort(lcRows, (r, k) =>
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
                          <td className="mono" style={{ fontSize: '12px' }}>{r.hosts.join(' ')}</td>
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
            >
              <div className="eyebrow">
                {fint(sec?.args_scanned ?? 0)} command lines scanned
              </div>
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <SortTh s={secSort(sc)} k="pattern" first="asc">pattern</SortTh>
                      <SortTh s={secSort(sc)} k="sev" first="asc" tip={GLOSS.severity}>severity</SortTh>
                      <SortTh s={secSort(sc)} k="n" num>matches</SortTh>
                      <SortTh s={secSort(sc)} k="users" num>users</SortTh>
                      <SortTh s={secSort(sc)} k="tools" first="asc">tools</SortTh>
                      <th>run by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {secSort(sc).sort(inSection(secRows, sc), (r, k) =>
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
                        <td className="mono" style={{ fontSize: '12px' }}>{r.tools.join(' ')}</td>
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
            >
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <SortTh s={secSort(sc)} k="command" first="asc">command</SortTh>
                      <SortTh s={secSort(sc)} k="sev" first="asc" tip={GLOSS.severity}>severity</SortTh>
                      <SortTh s={secSort(sc)} k="n" num>times run</SortTh>
                      <SortTh s={secSort(sc)} k="users" num>users</SortTh>
                      <th>run by</th>
                      <SortTh s={secSort(sc)} k="works" num tip="runs that exited 0: nothing stopped them">succeeded</SortTh>
                      <SortTh s={secSort(sc)} k="cpu" num>CPU s</SortTh>
                    </tr>
                  </thead>
                  <tbody>
                    {secSort(sc).sort(inSection(dgRows, sc), (r, k) =>
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
});
