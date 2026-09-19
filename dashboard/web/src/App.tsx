/* Shell: rail, header, staleness readout, tab dispatch.
 *
 * The header's stale/live pill is driven by the feeds report -- `lag_s` against
 * that feed's own `max_age_s` -- not by a wall-clock guess about how often the
 * page should have heard something. A feed with a 24 h cadence is not stale at
 * 10 minutes, and the pill must not say so.
 */
import { useEffect } from 'react';
import { useConfig, useFeeds, usePanels, useTrajectories } from './api/hooks';
import { useLive } from './api/useLive';
import type { FeedReport, FeedStatus } from './api/types';
import { ClassFilter } from './components/ClassFilter';
import { StatusPill } from './components/Panel';
import { TooltipLayer } from './components/Tooltip';
import { ago, fint, lagStr } from './lib/format';
import { useUrlState } from './lib/useUrlState';
import { LiveTab } from './tabs/LiveTab';
import { OverviewTab } from './tabs/OverviewTab';
import { SessionsTab } from './tabs/SessionsTab';
import { ResourcesTab } from './tabs/ResourcesTab';
import { RiskIoTab } from './tabs/RiskIoTab';
import { SandboxTab } from './tabs/SandboxTab';
import { TrajectoriesTab } from './tabs/TrajectoriesTab';
import { FeedsTab } from './tabs/FeedsTab';

const TABS = [
  { k: 'live', lbl: 'Live', eyebrow: 'live tier' },
  { k: 'overview', lbl: 'Overview', eyebrow: 'overview' },
  { k: 'sessions', lbl: 'Sessions & tools', eyebrow: 'process tier' },
  { k: 'resources', lbl: 'Resources', eyebrow: 'process tier' },
  { k: 'risk', lbl: 'Risk & I/O', eyebrow: 'process tier' },
  { k: 'sandbox', lbl: 'Sandbox', eyebrow: 'process tier' },
  { k: 'trajectories', lbl: 'Trajectories', eyebrow: 'process tier' },
  { k: 'feeds', lbl: 'Feeds', eyebrow: 'provenance' },
];

/** Worst status across the feeds that matter, for the header pill. */
function headline(feeds: Record<string, FeedReport> | null): {
  status: FeedStatus | 'live';
  lag: string;
  stale: FeedReport[];
} {
  if (!feeds) return { status: 'live', lag: '', stale: [] };
  const all = Object.values(feeds);
  const req = all.filter((f) => f.required);
  const pool = req.length ? req : all;
  const stale = pool.filter(
    (f) => f.lag_s != null && f.max_age_s != null && f.lag_s > f.max_age_s,
  );
  const bad = pool.find((f) => f.status === 'missing') ||
    pool.find((f) => f.status === 'degraded') ||
    pool.find((f) => f.status === 'stale') ||
    pool.find((f) => f.status === 'empty');
  // The freshest lag among live-cadence feeds is the number a user wants.
  const lags = pool.map((f) => f.lag_s).filter((v): v is number => v != null);
  return {
    status: stale.length ? 'stale' : bad ? bad.status : 'live',
    lag: lags.length ? lagStr(Math.min(...lags)) : '',
    stale,
  };
}

export default function App() {
  const [url, setUrl] = useUrlState();
  const panels = usePanels();
  const feeds = useFeeds();
  const config = useConfig();
  const live = useLive();
  const traj = useTrajectories(url.rank);

  // A `rebuilt` frame means the reduced panels changed on disk: re-read them.
  const rebuild = live.rebuildNonce;
  const reloadPanels = panels.reload;
  useEffect(() => {
    if (rebuild > 0) reloadPanels();
  }, [rebuild, reloadPanels]);

  // A `feeds` frame supersedes the REST snapshot.
  const wsFeeds = live.feeds;
  const mergeFeeds = feeds.merge;
  useEffect(() => {
    if (wsFeeds) mergeFeeds(wsFeeds);
  }, [wsFeeds, mergeFeeds]);

  const tab = TABS.find((t) => t.k === url.tab) || TABS[0];
  const hl = headline(feeds.data);
  const P = panels.data?.panels ?? {};

  const nClasses = url.cls.length;
  const body = (() => {
    switch (tab.k) {
      case 'live':
        return <LiveTab live={live} cls={url.cls} feeds={feeds.data} goto={(t) => setUrl({ tab: t }, true)} />;
      case 'overview':
        return <OverviewTab P={P} live={live.live} cls={url.cls} traj={traj.data} />;
      case 'sessions':
        return <SessionsTab P={P} cls={url.cls} />;
      case 'resources':
        return <ResourcesTab P={P} cls={url.cls} />;
      case 'risk':
        return <RiskIoTab P={P} cls={url.cls} url={url} setUrl={setUrl} />;
      case 'sandbox':
        return <SandboxTab P={P} cls={url.cls} />;
      case 'trajectories':
        return <TrajectoriesTab traj={traj} rank={url.rank} setRank={(r) => setUrl({ rank: r })} cls={url.cls} />;
      case 'feeds':
        return <FeedsTab feeds={feeds} config={config} builtAt={panels.data?.full_built_at ?? null} />;
      default:
        return null;
    }
  })();

  return (
    <div className="app">
      <aside className="rail">
        <div className="brand">
          <div className="mark">
            <span className="bdot" />
            Agent Behaviour
          </div>
          <div className="sub mono">FASRC &middot; rc_measurement</div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.k}
              aria-selected={t.k === tab.k}
              onClick={() => {
                setUrl({ tab: t.k }, true);
                window.scrollTo({ top: 0 });
              }}
            >
              {t.lbl}
              <span className="n">
                {t.k === 'live' ? (
                  <i
                    className="dot"
                    style={{
                      background: live.transport === 'ws' ? 'var(--live)' : 'var(--warn)',
                      borderRadius: '50%',
                    }}
                  />
                ) : t.k === tab.k ? (
                  '●'
                ) : (
                  ''
                )}
              </span>
            </button>
          ))}
        </nav>
        <div className="foot mono">
          transport <span style={{ color: 'var(--ink-2)' }}>{live.transport}</span>
          <br />
          frame {live.receivedAt ? ago(Date.now() - live.receivedAt) : '—'}
          <br />
          panels built {panels.data?.full_built_at ?? '—'}
          <br />
          {nClasses < 3 && (
            <span style={{ color: 'var(--crimson-ink)' }}>
              {nClasses} of 3 classes shown
            </span>
          )}
        </div>
      </aside>

      <main>
        <div className="head">
          <div>
            <div className="eyebrow">{tab.eyebrow}</div>
            <h1>{tab.lbl}</h1>
          </div>
          <div className="status">
            <StatusPill
              status={hl.status}
              text={hl.status === 'live' ? live.transport : hl.status}
              beat={hl.status === 'live' && live.transport === 'ws'}
            />
            {hl.lag && (
              <span data-tip="newest record age, against that feed's own max_age_s">
                lag <b>{hl.lag}</b>
              </span>
            )}
            {hl.stale.length > 0 && (
              <span
                style={{ color: '#8a6410' }}
                data-tip={hl.stale
                  .map((f) => `${f.name}: lag ${lagStr(f.lag_s)} > max ${lagStr(f.max_age_s)}`)
                  .join('\n')}
              >
                {hl.stale.length} feed{hl.stale.length > 1 ? 's' : ''} past max_age
              </span>
            )}
            <span>
              {fint(live.live?.hosts?.length ?? null)} hosts
            </span>
            <button className="btn" onClick={live.refresh}>
              refresh
            </button>
          </div>
        </div>

        {(panels.error || live.error || feeds.error) && (
          <div className="err" style={{ marginBottom: 14 }}>
            {[panels.error && `/api/panels: ${panels.error}`,
              feeds.error && `/api/feeds: ${feeds.error}`,
              live.error && `/api/live: ${live.error}`]
              .filter(Boolean)
              .join(' · ')}
          </div>
        )}

        {tab.k !== 'feeds' && (
          <ClassFilter value={url.cls} onChange={(c) => setUrl({ cls: c })} />
        )}

        {body}
      </main>
      <TooltipLayer />
    </div>
  );
}
