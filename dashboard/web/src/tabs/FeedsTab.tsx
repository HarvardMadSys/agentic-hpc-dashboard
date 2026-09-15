/* Feeds -- the provenance tab.
 *
 * One row per feed: tier, the configured path against the path that actually
 * resolved, the status pill, lag against that feed's own max_age_s, rows, and
 * the notice verbatim. This is the tab that makes every other tab auditable, so
 * nothing here is summarised away -- a configured path that did NOT resolve is
 * shown next to the one that did, not replaced by it.
 */
import type { Async } from '../api/hooks';
import type { ConfigNode, ConfigTree, FeedReport, FeedsResponse } from '../api/types';
import { isConfigLeaf } from '../api/types';
import { Panel, StatusPill } from '../components/Panel';
import { DASH, bytes, fint, lagStr } from '../lib/format';

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

function FeedRow({ f }: { f: FeedReport }) {
  const past = f.lag_s != null && f.max_age_s != null && f.lag_s > f.max_age_s;
  return (
    <tr>
      <td className="mono">
        <b style={{ color: 'var(--ink)' }}>{f.name}</b>
        {f.label && f.label !== f.name && (
          <div style={{ color: 'var(--muted)', fontSize: '10.5px' }}>{f.label}</div>
        )}
      </td>
      <td className="mono" style={{ color: 'var(--muted)' }}>
        {f.tier}
        {f.required && (
          <div style={{ color: 'var(--crimson-ink)', fontSize: '10px' }}>required</div>
        )}
      </td>
      <td>
        <StatusPill status={f.status} />
      </td>
      <td className="mono" style={{ fontSize: '10.5px', maxWidth: 340, wordBreak: 'break-all' }}>
        {/* configured vs resolved side by side: a path that did not resolve is
            the single most useful thing on this page when a panel is empty. */}
        {(f.configured ?? []).map((p) => {
          const ok = (f.resolved ?? []).includes(p);
          return (
            <div key={p} style={{ color: ok ? 'var(--ink-2)' : 'var(--crit)' }}>
              {ok ? '✓ ' : '✗ '}
              {p}
            </div>
          );
        })}
        {(f.resolved ?? [])
          .filter((p) => !(f.configured ?? []).includes(p))
          .map((p) => (
            <div key={p} style={{ color: 'var(--ink-2)' }}>
              {'✓ '}
              {p}
              <span style={{ color: 'var(--muted)' }}> (resolved)</span>
            </div>
          ))}
        {(f.missing ?? []).length > 0 && (
          <div style={{ color: 'var(--crit)' }}>
            missing: {(f.missing ?? []).join(', ')}
          </div>
        )}
        {!f.configured?.length && !f.resolved?.length && (
          <span className="nm">no path configured</span>
        )}
      </td>
      <td className="mono" style={{ fontSize: '10.5px', color: 'var(--muted)' }}>
        {f.origin ?? DASH}
        {f.env && <div>env {f.env}</div>}
        {f.cli && <div>cli {f.cli}</div>}
      </td>
      <td className="num">{f.cadence ?? DASH}</td>
      <td className="num" style={past ? { color: 'var(--crit)' } : undefined}>
        {lagStr(f.lag_s)}
        {f.max_age_s != null && (
          <div style={{ color: 'var(--muted)', fontSize: '10px' }}>
            max {lagStr(f.max_age_s)}
          </div>
        )}
      </td>
      <td className="num">{fint(f.rows)}</td>
      <td className="num">
        {fint(f.n_files)}
        {f.bytes != null && (
          <div style={{ color: 'var(--muted)', fontSize: '10px' }}>{bytes(f.bytes)}</div>
        )}
      </td>
      <td className="num">
        {fint(f.days?.length ?? null)}
        {f.hosts?.length ? (
          <div style={{ color: 'var(--muted)', fontSize: '10px' }}>
            {f.hosts.length} hosts
          </div>
        ) : null}
      </td>
      <td style={{ maxWidth: 300 }}>
        {f.notice ? (
          <span style={{ color: f.status === 'ok' ? 'var(--ink-2)' : '#8a6410' }}>{f.notice}</span>
        ) : (
          DASH
        )}
      </td>
    </tr>
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
  const list = Object.values(feeds.data ?? {}).sort(
    (a, b) =>
      (a.tier || '').localeCompare(b.tier || '') ||
      Number(b.required) - Number(a.required) ||
      (a.name || '').localeCompare(b.name || ''),
  );
  const cfg = config.data ? flatten(config.data as unknown as ConfigNode) : [];

  return (
    <div className="grid wide">
      <Panel
        title="Feeds"
        span
        feed="/api/feeds"
        empty={!feeds.loading && !list.length}
        emptyWhat="the backend reported no feeds"
        header={<span className="pill">{fint(list.length)} feeds</span>}
      >
        {feeds.error && <div className="err">{feeds.error}</div>}
        <div className="scroller">
          <table>
            <thead>
              <tr>
                <th>feed</th>
                <th>tier</th>
                <th>status</th>
                <th>configured / resolved</th>
                <th>origin</th>
                <th className="num">cadence</th>
                <th className="num">lag</th>
                <th className="num">rows</th>
                <th className="num">files</th>
                <th className="num">days</th>
                <th>notice</th>
              </tr>
            </thead>
            <tbody>
              {list.map((f) => (
                <FeedRow key={f.name} f={f} />
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel
        title="Backfill"
        feed="/api/feeds"
        empty={!list.some((f) => f.backfill != null)}
        emptyWhat="no feed reported a backfill state"
      >
        <div className="scroller">
          <table>
            <thead>
              <tr>
                <th>feed</th>
                <th>backfill</th>
              </tr>
            </thead>
            <tbody>
              {list
                .filter((f) => f.backfill != null)
                .map((f) => (
                  <tr key={f.name}>
                    <td className="mono">{f.name}</td>
                    <td className="mono" style={{ fontSize: '10.5px', wordBreak: 'break-all' }}>
                      {JSON.stringify(f.backfill)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel
        title="Resolved configuration"
        feed="/api/config"
        empty={!config.loading && !cfg.length}
        emptyWhat="the backend reported no configuration"
        header={<span className="pill">panels built {builtAt ?? DASH}</span>}
      >
        {config.error && <div className="err">{config.error}</div>}
        <div className="scroller">
          <table>
            <thead>
              <tr>
                <th>key</th>
                <th>value</th>
                <th>origin</th>
              </tr>
            </thead>
            <tbody>
              {cfg.map(([k, v, o]) => (
                <tr key={k}>
                  <td className="mono">{k}</td>
                  <td className="mono" style={{ wordBreak: 'break-all', color: 'var(--ink)' }}>
                    {v}
                  </td>
                  <td className="mono" style={{ color: 'var(--muted)', fontSize: '10.5px' }}>
                    {o}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
