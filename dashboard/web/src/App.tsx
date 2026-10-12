/* Shell: rail, header, staleness readout, tab dispatch.
 *
 * The header's stale/live pill is driven by the feeds report -- `lag_s` against
 * that feed's own `max_age_s` -- not by a wall-clock guess about how often the
 * page should have heard something. A feed with a 24 h cadence is not stale at
 * 10 minutes, and the pill must not say so.
 */
import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useConfig, useFeeds, usePanels, useTrajectories, useWindows } from './api/hooks';
import { useLive } from './api/useLive';
import type { Transport } from './api/useLive';
import type { Facets, FeedReport, FeedStatus } from './api/types';
import { ClassFilter } from './components/ClassFilter';
import { WhoFilter } from './components/WhoFilter';
import { StatusPill } from './components/Panel';
import { SectionList, useSections } from './components/SectionNav';
import { WindowPicker } from './components/WindowPicker';
import { TooltipLayer } from './components/Tooltip';
import { ago, fint, lagStr } from './lib/format';
import { whoQuery } from './lib/filters';
import type { Who } from './lib/filters';
import { useUrlState } from './lib/useUrlState';
import { LiveTab } from './tabs/LiveTab';
import { OverviewTab } from './tabs/OverviewTab';
import { SessionsTab } from './tabs/SessionsTab';
import { ResourcesTab } from './tabs/ResourcesTab';
import { RiskIoTab } from './tabs/RiskIoTab';
import { SandboxTab } from './tabs/SandboxTab';
import { TrajectoriesTab } from './tabs/TrajectoriesTab';
import { AgentsTab } from './tabs/AgentsTab';
import { SlurmTab } from './tabs/SlurmTab';
import { FeedsTab } from './tabs/FeedsTab';
import { ChatTab } from './tabs/ChatTab';
import { EventsExplorer } from './components/EventsExplorer';

const TABS = [
  { k: 'live', lbl: 'Live', eyebrow: 'live tier' },
  { k: 'overview', lbl: 'Overview', eyebrow: 'overview' },
  { k: 'sessions', lbl: 'Sessions & tools', eyebrow: 'process tier' },
  { k: 'resources', lbl: 'Resources', eyebrow: 'process tier' },
  { k: 'risk', lbl: 'Risk', eyebrow: 'what should not run here' },
  { k: 'io', lbl: 'I/O', eyebrow: 'disk and network' },
  { k: 'sandbox', lbl: 'Sandbox', eyebrow: 'process tier' },
  { k: 'trajectories', lbl: 'Trajectories', eyebrow: 'process tier' },
  { k: 'agents', lbl: 'Agents', eyebrow: 'agent & subagent behaviour' },
  { k: 'slurm', lbl: 'Slurm', eyebrow: 'jobs and the scheduler' },
  { k: 'chat', lbl: 'Chat with LLM', eyebrow: 'ask an agent about this window' },
  { k: 'events', lbl: 'Search & export', eyebrow: 'every process record, filtered and downloadable' },
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

/* The header pill, in words. Two separate questions share it, so it answers
 * the one that matters more: a problem with the DATA (a feed stale or gone)
 * outranks how the page gets its updates; with the data fine, it says whether
 * those updates stream in or arrive on a 15 s poll. "ws" said neither. */
const DATA_WORDS: Record<FeedStatus, [string, string]> = {
  ok: ['live', ''],
  stale: ['data delayed', 'a feed is older than its own max age: the numbers are not current'],
  degraded: ['partial data', 'a feed is reporting, but not all of it'],
  empty: ['no data yet', 'the collector is running but has written nothing in this window'],
  missing: ['collector not found', 'a required feed has no files where it is expected'],
};

function pillOf(
  status: FeedStatus | 'live',
  transport: Transport,
): { status: FeedStatus | 'live'; text: string; beat: boolean; tip: string } {
  if (status !== 'live') {
    const [text, tip] = DATA_WORDS[status];
    return { status, text, beat: false, tip };
  }
  switch (transport) {
    case 'ws':
      return { status: 'live', text: 'live', beat: true,
        tip: 'new records stream in as the collector writes them' };
    case 'polling':
      return { status: 'stale', text: 'refreshing every 15 s', beat: false,
        tip: 'the live stream is unavailable, so the page asks for new data every 15 seconds' };
    case 'connecting':
      return { status: 'empty', text: 'connecting…', beat: false,
        tip: 'opening the live stream to the dashboard service' };
    default:
      return { status: 'missing', text: 'disconnected · retrying', beat: false,
        tip: 'the dashboard service is not answering; the page keeps retrying' };
  }
}

export default function App() {
  const [url, setUrl] = useUrlState();
  const feeds = useFeeds();
  const config = useConfig();
  const live = useLive(url.win);
  // `buildNonce` bumps when the service finishes reducing this window, which is
  // the only moment /api/panels has anything new to say about it.
  // The user and agent-type filter is a view of its own server-side, built
  // like a window; memoised on its query so a re-render is not a new request.
  const whoKey = whoQuery(url);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const who: Who = useMemo(() => ({ users: url.users, atypes: url.atypes }), [whoKey]);
  const panels = usePanels(url.win, live.buildNonce, who);
  const traj = useTrajectories(url.rank, url.win, live.buildNonce, who);
  // The filter's choices come with the panels whenever the unfiltered view of
  // this window is held. Kept across a fetch that has none -- a window not yet
  // built unfiltered -- so the dropdowns do not empty under the reader.
  const [facets, setFacets] = useState<Facets | null>(null);
  const freshFacets = panels.data?.facets;
  useEffect(() => {
    if (freshFacets) setFacets(freshFacets);
  }, [freshFacets]);
  // Seeded over REST so the picker is populated before the socket is up; the
  // websocket's `windows` frame supersedes it with live build progress.
  const windowsRest = useWindows();
  const windows = live.windows ?? windowsRest.data;
  // The window the SERVICE resolved, which is what every panel was built over --
  // not the one in the URL, which may have been clamped.
  const resolvedWin = live.live?.window;
  const winStatus =
    panels.data?.window_status ??
    // A held list can carry a filtered and an unfiltered view of one window:
    // only this page's own filter's progress is this page's.
    windows?.held?.find(
      (w) =>
        w.minutes === resolvedWin?.minutes &&
        whoQuery({ users: w.filter?.users ?? [], atypes: w.filter?.agent_types ?? [] }) === whoKey,
    ) ??
    null;

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

  // A cluster without Slurm sets `slurm.enabled: false`; its tab goes away.
  const slurmLeaf = (config.data?.slurm as Record<string, { _value?: unknown }> | undefined)?.enabled;
  const tabs = TABS.filter((t) => t.k !== 'slurm' || slurmLeaf?._value !== false);
  const tab = tabs.find((t) => t.k === url.tab) || tabs[0];
  // Which deployment: the sidebar and the browser tab both say, so a frozen
  // capture and a live feed open side by side are never confused.
  const siteName = String(
    (config.data?.site as Record<string, { _value?: unknown }> | undefined)?.name?._value ?? '',
  );
  useEffect(() => {
    document.title = siteName ? `${siteName} · Agent Behaviour` : 'Agent Behaviour Console';
  }, [siteName]);
  // Names are aliased on the way out (rc_dashboard/anonymize.py): the header
  // says so, or a reader takes `user-7f3a9c1e` for a real account.
  const anonymized =
    (config.data?.anonymize as Record<string, { _value?: unknown }> | undefined)?.enabled?._value ===
    true;
  const mainRef = useRef<HTMLElement>(null);
  const { secs, active } = useSections(mainRef, tab.k);
  const hl = headline(feeds.data);
  const P = panels.data?.panels ?? {};

  const nClasses = url.cls.length;
  const body = (() => {
    switch (tab.k) {
      case 'live':
        return <LiveTab live={live} cls={url.cls} who={who} feeds={feeds.data} goto={(t) => setUrl({ tab: t }, true)} />;
      case 'overview':
        return <OverviewTab P={P} live={live.live} cls={url.cls} traj={traj.data} />;
      case 'sessions':
        return <SessionsTab P={P} cls={url.cls} />;
      case 'resources':
        return <ResourcesTab P={P} cls={url.cls} />;
      case 'risk':
        return <RiskIoTab P={P} cls={url.cls} part="risk" />;
      case 'io':
        return <RiskIoTab P={P} cls={url.cls} part="io" />;
      case 'chat':
        return <ChatTab windowMin={resolvedWin?.minutes ?? null} windowLabel={resolvedWin?.label ?? null} />;
      case 'events':
        return <EventsExplorer url={url} setUrl={setUrl} windowMin={resolvedWin?.minutes ?? null} />;
      case 'sandbox':
        return <SandboxTab P={P} cls={url.cls} />;
      case 'trajectories':
        return (
          <TrajectoriesTab
            traj={traj}
            rank={url.rank}
            setRank={(r) => setUrl({ rank: r })}
            cls={url.cls}
            win={url.win}
            sel={url.traj}
            // Opening a session is navigation: back closes it again.
            select={(sid) => setUrl({ traj: sid, tfrom: null }, true)}
            tfrom={url.tfrom}
            setTfrom={(t) => setUrl({ tfrom: t }, true)}
          />
        );
      case 'slurm':
        return <SlurmTab P={P} live={live.live} cls={url.cls} />;
      case 'agents':
        return <AgentsTab P={P} cls={url.cls} />;
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
          <div className="sub mono" data-tip="which deployment this is: site.name (RC_DASH_SITE_NAME)">
            {siteName || 'rc_measurement'}
          </div>
        </div>
        <nav>
          {tabs.map((t) => (
            <Fragment key={t.k}>
            <button
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
            {t.k === tab.k && <SectionList secs={secs} active={active} />}
            </Fragment>
          ))}
        </nav>
        <div className="foot mono">
          updates{' '}
          <span style={{ color: 'var(--ink-2)' }}>
            {{ ws: 'streaming', polling: 'every 15 s', connecting: 'connecting…', down: 'disconnected' }[
              live.transport
            ]}
          </span>
          <br />
          last update {live.receivedAt ? ago(Date.now() - live.receivedAt) : '—'}
          <br />
          window <span style={{ color: 'var(--ink-2)' }}>{resolvedWin?.label ?? '—'}</span>
          {windows?.retention_minutes != null && (
            <span> of {Math.round(windows.retention_minutes / 60)}h held</span>
          )}
          <br />
          panels built {panels.data?.full_built_at ?? '—'}
          {/* Restored from a saved build: the age above is that build's, and
              everything written since was read from the files on top of it. */}
          {winStatus?.cache?.source === 'snapshot' && (
            <span data-tip={winStatus.cache.note ?? ''}> · restored</span>
          )}
          <br />
          {nClasses < 3 && (
            <span style={{ color: 'var(--crimson-ink)' }}>
              {nClasses} of 3 classes shown
            </span>
          )}
        </div>
      </aside>

      <main ref={mainRef}>
        <div className="head">
          <div>
            <div className="eyebrow">{tab.eyebrow}</div>
            <h1>{tab.lbl}</h1>
          </div>
          <div className="status">
            {/* A pinned clock is a capture, not a feed: "live" and a lag of a
                second would both be false (rc_dashboard/clock.py). */}
            {live.live?.clock?.pinned ? (
              <>
                <StatusPill
                  status="empty"
                  text="frozen capture"
                  tip={`not a live feed: the service's clock is pinned to the capture's end and does not move\n${live.live.clock.source}`}
                />
                <span data-tip="every window ends here; nothing after it exists in this feed">
                  as of <b>{live.live.clock.as_of}</b>
                </span>
              </>
            ) : (
              <StatusPill {...pillOf(hl.status, live.transport)} />
            )}
            {anonymized && (
              <span
                className="pill"
                data-tip={
                  'names are replaced by stable aliases (user-…, lab-…) before anything leaves ' +
                  'the service; email addresses and user IDs are hidden'
                }
              >
                anonymized
              </span>
            )}
            {hl.lag && !live.live?.clock?.pinned && (
              <span data-tip="how long ago the collector wrote the newest record the dashboard has; a few seconds is normal">
                newest record <b>{hl.lag}</b> old
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
            <span
              data-tip={
                (live.live?.hosts ?? []).map((h) => h.host).join(', ') ||
                'no host has reported yet'
              }
            >
              {fint(live.live?.hosts?.length ?? null)} host
              {(live.live?.hosts?.length ?? 0) === 1 ? '' : 's'}
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
          <>
            <WindowPicker
              value={url.win}
              onChange={(w) => setUrl({ win: w })}
              windows={windows}
              resolved={resolvedWin}
              status={winStatus}
              backfill={live.live?.backfill}
            />
            <ClassFilter
              value={url.cls}
              onChange={(c) => setUrl({ cls: c })}
              extra={
                <WhoFilter
                  who={who}
                  facets={facets}
                  onChange={(w) => setUrl({ users: w.users, atypes: w.atypes })}
                />
              }
            />
          </>
        )}

        {body}
      </main>
      <TooltipLayer />
    </div>
  );
}
