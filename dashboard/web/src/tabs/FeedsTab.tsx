/* Feeds: are the collectors OK, and where is every number on the page read from.
 *
 * An admin opens this tab to answer one question first -- is each collector
 * running and keeping up -- so it leads with one row per host and a verdict,
 * and puts the provenance (data sources, the startup read, the resolved
 * configuration) beneath it.
 *
 * The verdict comes from what the collector wrote and nothing else
 * (rc_dashboard/health.py): its `meta` record when it starts, a heartbeat
 * every minute, and a `stop` record saying why it stopped and how many kernel
 * events it lost. The dashboard never contacts a collector, so a capture copied
 * from another cluster is judged exactly as a live feed is.
 */
import { useState } from 'react';
import type { Async } from '../api/hooks';
import { useCollectors } from '../api/hooks';
import type {
  CollectorHost,
  CollectorRun,
  CollectorVerdict,
  ConfigNode,
  ConfigTree,
  FeedReport,
  FeedsResponse,
  FeedStatus,
} from '../api/types';
import { isConfigLeaf } from '../api/types';
import { fullClock } from '../charts/scale';
import { More, NoRows, Panel, StatusPill } from '../components/Panel';
import { SortTh } from '../components/SortTh';
import { useSort } from '../lib/useSort';
import { DASH, bytes, dur, fint, lagStr } from '../lib/format';

/* ----------------------------------------------------------- collectors */

const VERDICT: Record<CollectorVerdict, { pill: FeedStatus | 'live'; text: string; tip: string }> = {
  ok: { pill: 'live', text: 'OK', tip: 'running: a heartbeat within the last few minutes' },
  degraded: { pill: 'degraded', text: 'degraded', tip: 'running, but losing data it could have' },
  stopped: { pill: 'stale', text: 'stopped', tip: 'stopped cleanly: it wrote a stop record saying why' },
  silent: { pill: 'missing', text: 'silent', tip: 'no heartbeat and no stop record: killed, crashed, or the host is down' },
  unknown: { pill: 'empty', text: 'no heartbeat', tip: 'files for this host, but no heartbeat record in what was read' },
};

const when = (e: number | null | undefined) => (e == null ? DASH : fullClock(e));
const ago = (s: number | null | undefined) => (s == null ? DASH : `${dur(s)} ago`);

function lostText(r: CollectorRun | undefined | null): string {
  if (!r) return DASH;
  if (r.lost == null) return r.stopped == null ? 'reported when it stops' : DASH;
  return `${r.lost_pct != null ? `${r.lost_pct < 0.01 && r.lost ? '<0.01' : r.lost_pct.toFixed(2)}%` : DASH} · ${fint(r.lost)} of ${fint(r.seen)}`;
}

function Runs({ runs, now }: { runs: CollectorRun[]; now: number }) {
  const s = useSort<string>();
  if (!runs.length) return <NoRows what="no start or stop record in the files read" />;
  // newest first as read; the index each run had decides "running" below
  const rows = s.sort(runs.map((r, i) => ({ r, i })), ({ r }, k) =>
    k === 'started' ? r.started : k === 'stopped' ? r.stopped : k === 'ran' ? r.ran_s : k === 'why' ? r.stop_reason
    : k === 'records' ? r.records : k === 'lost' ? r.lost_pct : k === 'cut' ? r.truncated : r.pid);
  return (
    <table>
      <thead>
        <tr>
          <SortTh s={s} k="started">started</SortTh>
          <SortTh s={s} k="stopped">stopped</SortTh>
          <SortTh s={s} k="ran" num>ran for</SortTh>
          <SortTh s={s} k="why" first="asc">why it stopped</SortTh>
          <SortTh s={s} k="records" num tip="records it wrote, from its stop record">records</SortTh>
          <SortTh s={s} k="lost" num tip="kernel events it could not keep up with, against those it was handed">events lost</SortTh>
          <SortTh s={s} k="cut" num tip="processes still running when it stopped, written with what was known">cut short at stop</SortTh>
          <SortTh s={s} k="pid">pid · kernel</SortTh>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ r, i }) => (
          <tr key={`${r.pid}-${i}`}>
            <td className="mono">{when(r.started)}</td>
            <td className="mono">{r.stopped != null ? when(r.stopped) : <span className="nm">{i === 0 ? 'running' : 'no stop record'}</span>}</td>
            <td className="num">{dur(r.ran_s ?? (r.started != null && r.stopped == null && i === 0 ? now - r.started : null))}</td>
            <td>{r.stop_reason ?? DASH}</td>
            <td className="num">{fint(r.records)}</td>
            <td className="num" style={r.lost_pct != null && r.lost_pct > 0.1 ? { color: 'var(--crit)' } : undefined}>
              {lostText(r)}
            </td>
            <td className="num">{fint(r.truncated)}</td>
            <td className="mono nm">{[r.pid, r.kernel].filter((x) => x != null).join(' · ') || DASH}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function HostDetail({ h, now }: { h: CollectorHost; now: number }) {
  const t = h.files;
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div>
        <div className="eyebrow">runs in the files read, newest first</div>
        <div className="scroller">
          <Runs runs={h.runs} now={now} />
        </div>
      </div>
      <dl className="kv">
        <dt>probes loaded</dt>
        <dd className="mono">{h.features.length ? h.features.join(' ') : DASH}</dd>
        <dt>not available on this kernel</dt>
        <dd>
          {h.unavailable.length
            ? h.unavailable.map((u) => (
                <div key={u.feature}>
                  <span className="mono">{u.feature}</span>
                  {u.impact ? ` — ${u.impact}` : ''}
                  <div className="nm mono" style={{ fontSize: '10.5px' }}>{u.why}</div>
                </div>
              ))
            : 'none'}
        </dd>
        <dt>sysctls it depends on</dt>
        <dd className="mono">
          {h.sysctl ? Object.entries(h.sysctl).map(([k, v]) => `${k}=${v ?? 'unset'}`).join('  ') : DASH}
        </dd>
        <dt>eBPF files</dt>
        <dd>{t.ebpf ? `${fint(t.ebpf.files)} files · ${bytes(t.ebpf.bytes)} · last written ${when(t.ebpf.last_write)}` : 'none'}</dd>
        <dt>node-tier files</dt>
        <dd>{t.ebpf_node ? `${fint(t.ebpf_node.files)} files · ${bytes(t.ebpf_node.bytes)} · last written ${when(t.ebpf_node.last_write)}` : 'none for this host'}</dd>
        <dt>heartbeats read</dt>
        <dd>{`${fint(h.heartbeat.seen)}, one every ${dur(h.heartbeat.interval_s)}`}</dd>
      </dl>
    </div>
  );
}

function Collectors({ hosts, now }: { hosts: CollectorHost[]; now: number }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const s = useSort<string>();
  const sorted = s.sort(hosts, (h, k) =>
    k === 'host' ? h.host : k === 'status' ? h.verdict : k === 'beat' ? h.heartbeat.age_s
    : k === 'since' ? h.running?.started ?? null : k === 'restarts' ? h.restarts
    : k === 'lost' ? h.runs.find((r) => r.stopped != null)?.lost_pct ?? null : h.reasons.join(' '));
  return (
    <div className="scroller vscroll">
      <table className="roomy byuser-t">
        <thead>
          <tr>
            <SortTh s={s} k="host" first="asc">host</SortTh>
            <SortTh s={s} k="status" first="asc">status</SortTh>
            <SortTh s={s} k="beat" num tip="the collector writes a heartbeat (residency_totals) every minute">last heartbeat</SortTh>
            <SortTh s={s} k="since">running since</SortTh>
            <SortTh s={s} k="restarts" num tip="starts after the first one, in the files read">restarts</SortTh>
            <SortTh s={s} k="lost" num tip="kernel events its last finished run could not keep up with">events lost, last run</SortTh>
            <SortTh s={s} k="why" first="asc">why</SortTh>
          </tr>
        </thead>
        {sorted.map((h) => {
          const v = VERDICT[h.verdict] ?? VERDICT.unknown;
          const o = !!open[h.host];
          const lastDone = h.runs.find((r) => r.stopped != null);
          return (
            <tbody key={h.host}>
              <tr className={`byuser-row${o ? ' open' : ''}`} onClick={() => setOpen((m) => ({ ...m, [h.host]: !o }))}>
                <td className="mono">
                  <span className="caret">{o ? '▾' : '▸'}</span> <b>{h.host}</b>
                </td>
                <td>
                  <StatusPill status={v.pill} text={v.text} tip={v.tip} />
                </td>
                <td className="num" style={h.verdict === 'silent' || h.verdict === 'stopped' ? { color: 'var(--crit)' } : undefined}>
                  {ago(h.heartbeat.age_s)}
                </td>
                <td className="mono">
                  {h.running ? (
                    <>
                      {when(h.running.started)}
                      <div className="nm" style={{ fontSize: '10.5px' }}>
                        {h.running.started != null ? `up ${dur(now - h.running.started)}` : ''}
                        {h.running.pid != null ? ` · pid ${h.running.pid}` : ''}
                      </div>
                    </>
                  ) : (
                    <span className="nm">not running</span>
                  )}
                </td>
                <td className="num">{fint(h.restarts)}</td>
                <td className="num" style={lastDone?.lost_pct != null && lastDone.lost_pct > 0.1 ? { color: 'var(--crit)' } : undefined}>
                  {lastDone ? lostText(lastDone) : <span className="nm">no finished run</span>}
                </td>
                <td style={{ whiteSpace: 'normal', minWidth: 260 }}>
                  {h.reasons.map((r) => (
                    <div key={r}>{r}</div>
                  ))}
                  {h.limits.map((r) => (
                    <div key={r} className="nm" data-tip="a limit of this host's kernel: true of every run until the kernel changes, so it does not change the status">
                      {`limit: ${r}`}
                    </div>
                  ))}
                  {!h.reasons.length && !h.limits.length && <span className="nm">{DASH}</span>}
                </td>
              </tr>
              {o && (
                <tr className="byuser-sub">
                  <td colSpan={7}>
                    <HostDetail h={h} now={now} />
                  </td>
                </tr>
              )}
            </tbody>
          );
        })}
      </table>
    </div>
  );
}

/* ---------------------------------------------------------- data sources */

function flatten(node: ConfigNode, prefix = ''): [string, string, string][] {
  // A null or scalar where a branch was expected is data, not a crash: the
  // config tree comes off the wire and one stray shape must not blank the tab.
  if (node == null || typeof node !== 'object') {
    return prefix ? [[prefix, node == null ? DASH : String(node), 'unknown']] : [];
  }
  if (isConfigLeaf(node)) {
    const v = node._value;
    return [[prefix, v == null ? DASH : typeof v === 'object' ? JSON.stringify(v) : String(v), node._origin]];
  }
  return Object.entries(node).flatMap(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k));
}

function Where({ f }: { f: FeedReport }) {
  const tip = [f.origin ? `set by ${f.origin}` : null, f.env ? `env ${f.env}` : null, f.cli ? `flag ${f.cli}` : null]
    .filter(Boolean)
    .join('\n');
  const resolved = f.resolved ?? [];
  // A configured path that did not resolve is the single most useful thing on
  // this page when a panel is empty, so it stays, marked, beside the one that did.
  const missing = (f.configured ?? []).filter((p) => !resolved.includes(p));
  return (
    <div className="mono" style={{ fontSize: '10.5px', wordBreak: 'break-all' }} data-tip={tip || undefined}>
      {resolved.map((p) => (
        <div key={p}>{p}</div>
      ))}
      {missing.length > 0 && (
        <div style={{ color: resolved.length || !f.required ? 'var(--muted)' : 'var(--crit)' }}>
          {resolved.length ? 'also looked in: ' : 'looked in: '}
          {missing.join(', ')}
        </div>
      )}
      {!resolved.length && !missing.length && <span className="nm">no path configured</span>}
    </div>
  );
}

function Sources({ list }: { list: FeedReport[] }) {
  const s = useSort<string>();
  const sorted = s.sort(list, (f, k) =>
    k === 'source' ? f.name : k === 'status' ? f.status : k === 'newest' ? f.lag_s ?? null
    : k === 'from' ? (f.resolved.length ? f.resolved : f.configured).join(' ') : f.n_files);
  return (
    <div className="scroller">
      <table className="roomy">
        <thead>
          <tr>
            <SortTh s={s} k="source" first="asc">source</SortTh>
            <SortTh s={s} k="status" first="asc">status</SortTh>
            <SortTh s={s} k="newest" num first="asc" tip="the newest record in its newest file, and how old that is against the source's own limit">newest record</SortTh>
            <SortTh s={s} k="from" first="asc">read from</SortTh>
            <SortTh s={s} k="files" num>files</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((f) => {
            const past = f.lag_s != null && f.max_age_s != null && f.lag_s > f.max_age_s;
            // An optional source nobody set up is not a fault: red would send an
            // admin looking for a broken collector that does not exist.
            const unset = !f.required && (f.status === 'missing' || f.status === 'empty');
            // a missing or empty source's notice only repeats where it looked,
            // which "read from" shows already; stale and degraded say something new
            const note = f.status === 'missing' || f.status === 'empty' ? null : f.notice;
            return (
              <tr key={f.name}>
                <td>
                  <b className="mono">{f.name}</b>
                  {f.required && <span style={{ color: 'var(--crimson-ink)', fontSize: '10px' }}> · required</span>}
                  <div className="nm" style={{ fontSize: '10.5px' }}>{f.label}</div>
                </td>
                <td>
                  {unset ? (
                    <StatusPill status="empty" text="not set up" tip={`optional: the panels that would use it say it is absent\n${f.notice ?? ''}`} />
                  ) : (
                    <StatusPill status={f.status} />
                  )}
                </td>
                <td className="num" style={past ? { color: 'var(--crit)' } : undefined}>
                  {f.newest_record_ts ?? DASH}
                  {f.lag_s != null && (
                    <div className="nm" style={{ fontSize: '10px' }}>
                      {`${lagStr(f.lag_s)} old${f.max_age_s != null ? ` · limit ${lagStr(f.max_age_s)}` : ''}`}
                    </div>
                  )}
                </td>
                <td style={{ maxWidth: 520, whiteSpace: 'normal' }}>
                  <Where f={f} />
                  {note && <div style={{ color: '#8a6410', fontSize: '11px', marginTop: 3 }}>{note}</div>}
                </td>
                <td className="num">
                  {f.n_files ? fint(f.n_files) : DASH}
                  {f.n_files ? (
                    <div className="nm" style={{ fontSize: '10px' }}>
                      {[bytes(f.bytes), f.days?.length ? `${f.days.length} day${f.days.length === 1 ? '' : 's'}` : null,
                        f.hosts?.length ? `${f.hosts.length} host${f.hosts.length === 1 ? '' : 's'}` : null]
                        .filter(Boolean)
                        .join(' · ')}
                    </div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Backfill({ b }: { b: Record<string, unknown> | null | undefined }) {
  if (!b) return <NoRows what="no startup read reported" />;
  const n = (k: string) => (typeof b[k] === 'number' ? (b[k] as number) : null);
  const s = String(b.state ?? '');
  const text =
    s === 'running'
      ? `reading history back to ${b.from ?? DASH}: ${fint(n('pct'))}% · ${fint(n('records'))} records so far`
      : s === 'done'
        ? // a frozen clock (a captured feed) measures every duration as 0: say none
          `read ${fint(n('records'))} records (${bytes(n('bytes'))})${n('elapsed_s') ? ` in ${dur(n('elapsed_s'))}` : ''}; history reaches back to ${b.retained_from ?? DASH}`
        : s === 'error'
          ? `failed: ${String(b.error ?? 'unknown error')}`
          : s === 'skipped'
            ? 'skipped: live.backfill_hours is 0, so the page fills forward from start-up'
            : s || DASH;
  return (
    <p style={{ margin: 0 }}>
      {text}
      {n('parse_errors') ? <span className="nm">{` · ${fint(n('parse_errors'))} lines did not parse`}</span> : null}
    </p>
  );
}

export function FeedsTab({
  feeds,
  config,
  builtAt,
}: {
  feeds: Async<FeedsResponse>;
  config: Async<ConfigTree>;
  builtAt: string | null;
}) {
  const col = useCollectors();
  const hosts = col.data?.hosts ?? [];
  const now = col.data?.now ?? Date.now() / 1000;
  const list = Object.values(feeds.data ?? {}).sort(
    (a, b) => Number(b.required) - Number(a.required) || (a.tier || '').localeCompare(b.tier || '') || (a.name || '').localeCompare(b.name || ''),
  );
  const cfg = config.data ? flatten(config.data as unknown as ConfigNode) : [];
  const cfgSort = useSort<string>();
  const counts = hosts.reduce<Record<string, number>>((m, h) => ({ ...m, [h.verdict]: (m[h.verdict] ?? 0) + 1 }), {});
  const summary = (['ok', 'degraded', 'stopped', 'silent', 'unknown'] as CollectorVerdict[])
    .filter((v) => counts[v])
    .map((v) => `${fint(counts[v])} ${VERDICT[v].text}`)
    .join(' · ');
  const nodeTier = col.data?.tiers?.ebpf_node;

  return (
    <div className="grid wide">
      <Panel
        title="Collectors"
        span
        feed="/api/collectors"
        empty={!col.loading && !hosts.length}
        emptyWhat="no collector has written a file the dashboard can read"
        header={<span className="pill">{hosts.length ? `${fint(hosts.length)} host${hosts.length === 1 ? '' : 's'} · ${summary}` : 'no hosts'}</span>}
      >
        {col.error && <div className="err">{col.error}</div>}
        <p className="paths" style={{ marginTop: 0 }}>
          Judged from what each collector writes: a start record, a heartbeat every minute, and a stop record with
          why it stopped and how many kernel events it lost. Click a host for its runs, probes, and files.
        </p>
        {hosts.length > 0 && <Collectors hosts={hosts} now={now} />}
        {nodeTier && nodeTier.status !== 'ok' && (
          <p className="paths">
            {`Node tier (load, memory, NFS, per-user cgroups): ${nodeTier.status}`}
            {nodeTier.notice ? ` — ${nodeTier.notice}` : ''}
          </p>
        )}
      </Panel>

      <Panel
        title="Data sources"
        span
        feed="/api/feeds"
        empty={!feeds.loading && !list.length}
        emptyWhat="the backend reported no feeds"
      >
        {feeds.error && <div className="err">{feeds.error}</div>}
        <Sources list={list} />
      </Panel>

      <Panel title="Startup read" feed="/api/feeds">
        <p className="paths" style={{ marginTop: 0 }}>
          On every start the service reads the history behind the newest record back into the live charts.
        </p>
        <Backfill b={(col.data?.backfill ?? (feeds.data?.ebpf?.backfill as Record<string, unknown> | undefined)) as Record<string, unknown> | null} />
      </Panel>

      <Panel title="Configuration" feed="/api/config" header={<span className="pill">panels built {builtAt ?? DASH}</span>}>
        {config.error && <div className="err">{config.error}</div>}
        <More label={`every resolved setting and where it came from · ${fint(cfg.length)}`}>
          <div className="scroller vscroll">
            <table>
              <thead>
                <tr>
                  <SortTh s={cfgSort} k="key" first="asc">key</SortTh>
                  <SortTh s={cfgSort} k="value" first="asc">value</SortTh>
                  <SortTh s={cfgSort} k="by" first="asc">set by</SortTh>
                </tr>
              </thead>
              <tbody>
                {cfgSort.sort(cfg, ([k, v, o], key) => (key === 'key' ? k : key === 'value' ? v : o)).map(([k, v, o]) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td className="mono" style={{ wordBreak: 'break-all', color: 'var(--ink)' }}>{v}</td>
                    <td className="mono nm" style={{ fontSize: '10.5px' }}>{o}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </More>
      </Panel>
    </div>
  );
}
