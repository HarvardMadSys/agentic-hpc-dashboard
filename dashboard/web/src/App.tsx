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
import { getAskInfo } from './api/client';
import type { AskInfo, Facets, FeedReport, FeedStatus } from './api/types';
import { AskScopeCtx } from './components/Ask';
import type { AskScope } from './components/Ask';
import { BuildStamp } from './components/BuildStamp';
import { ClassFilter } from './components/ClassFilter';
import { WhoFilter } from './components/WhoFilter';
import { IoCmdsDrawer } from './components/IoCmds';
import { StatusPill } from './components/Panel';
import { SectionList, useSections } from './components/SectionNav';
import { WindowPicker } from './components/WindowPicker';
import { TooltipLayer } from './components/Tooltip';
import { ago, fint, lagStr } from './lib/format';
import { whoQuery } from './lib/filters';
import type { Who } from './lib/filters';
import { useUrlState } from './lib/useUrlState';
import { useStickyBar } from './lib/sticky';
import { CLS } from './lib/classes';
import { Icon } from './components/Icon';
import type { IconName } from './components/Icon';
import { CommandPalette } from './components/CommandPalette';
import type { PaletteItem } from './components/CommandPalette';
import { arriveFromHash } from './lib/cites';
import { LiveTab } from './tabs/LiveTab';
import { OverviewTab } from './tabs/OverviewTab';
import { ResourcesTab } from './tabs/ResourcesTab';
import { RiskIoTab } from './tabs/RiskIoTab';
import { InternalTab } from './tabs/InternalTab';
import { TrajectoriesTab } from './tabs/TrajectoriesTab';
import { AgentsTab } from './tabs/AgentsTab';
import { SlurmTab } from './tabs/SlurmTab';
import { FeedsTab } from './tabs/FeedsTab';
import { ChatTab } from './tabs/ChatTab';
import { UsersTab } from './tabs/UsersTab';
import { EventsExplorer } from './components/EventsExplorer';

/* In the order an operator reads them (roadmap notes 32 and 50): what is
 * happening and who uses what, how each user works with agents, the agents,
 * one session, then the detail. Old tab keys still open (lib/filters.ts
 * TAB_ALIAS): sessions and cascades -> agents, io -> resources, sandbox ->
 * internal, and overview -> live, where its table now is.
 *
 * Fewer stops in the rail: Overview's last table is a section of Live, and
 * Resources, Risk & Warning and Slurm are one entry, the login nodes, with a
 * sub-tab each. A sub-tab keeps its own key, so `?tab=resources`, a citation
 * and every link into it land where they always did. */
type Tab = { k: string; lbl: string; eyebrow: string; icon: IconName; group: string; parent?: 'nodes' };
const TABS: Tab[] = [
  { k: 'live', lbl: 'Live', eyebrow: 'right now, over the window, and who uses what', icon: 'live', group: 'Monitor' },
  { k: 'users', lbl: 'Users', eyebrow: 'a portrait of each user, from how they use coding agents', icon: 'users', group: 'Monitor' },
  { k: 'agents', lbl: 'Agents', eyebrow: 'agents, subagents, and the tools they ran', icon: 'agents', group: 'Monitor' },
  { k: 'trajectories', lbl: 'Trajectories', eyebrow: 'one session at a time', icon: 'trajectories', group: 'Monitor' },
  { k: 'resources', lbl: 'Resources', eyebrow: 'CPU, memory, waits, and I/O', icon: 'resources', group: 'Monitor', parent: 'nodes' },
  { k: 'risk', lbl: 'Risk & Warning', eyebrow: 'what should not run here', icon: 'risk', group: 'Monitor', parent: 'nodes' },
  { k: 'slurm', lbl: 'Slurm', eyebrow: 'jobs and the scheduler', icon: 'queue', group: 'Monitor', parent: 'nodes' },
  { k: 'chat', lbl: 'Ask LLM', eyebrow: 'ask an agent about this window', icon: 'chat', group: 'More' },
  { k: 'events', lbl: 'Search & export', eyebrow: 'every process record, filtered and downloadable', icon: 'events', group: 'More' },
  { k: 'internal', lbl: 'Internal', eyebrow: 'not settled yet', icon: 'internal', group: 'More' },
  { k: 'feeds', lbl: 'Feeds', eyebrow: 'provenance', icon: 'feeds', group: 'More' },
];
/** The one entry the three node tabs share. It is named in the site's own
 *  word for its hosts (`ui.node_noun`, "login nodes"): their figures are
 *  login-node use, not the cluster's (README, "Scope is stated"). */
const NODES_EYEBROW = 'what runs on them, what should not, and what they ask of the scheduler';
/** A link to a tab that folded into another lands on the part it became. */
const LANDS_ON: Record<string, string> = { overview: 'sec-who-uses-what' };
/** The tab the page was opened on, read once, before the link is rewritten
 *  (an effect can run twice on mount, and the second saw the rewritten link). */
const OPENED_ON = new URLSearchParams(window.location.search).get('tab') ?? '';

/** The rail folded to its icons, remembered in this browser only. */
const RAIL_KEY = 'rc.railFolded';
const TRANSPORT_WORDS = { ws: 'streaming', polling: 'every 15 s', connecting: 'connecting…', down: 'disconnected' };

/** A key typed into a field is the field's, never a shortcut. */
const typing = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

/* Hidden for now, its code kept (user, 2026-10-13): Search & export. An old
 * link to it (?tab=events) opens the first tab instead. */
const HIDDEN_TABS = new Set(['events']);
/** How long after each answer the Sessions list asks again, while it is shown. */
const TRAJ_LIVE_MS = 5000;

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
  // a page opened from an answer's citation goes to what it cites, once
  useEffect(() => arriveFromHash(), []);
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
  // Re-read every few seconds while the Sessions list is on screen.
  const traj = useTrajectories(url.rank, url.win, live.buildNonce, who,
    url.tab === 'trajectories' ? TRAJ_LIVE_MS : null);
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
  const tabs = TABS.filter((t) => !HIDDEN_TABS.has(t.k) && (t.k !== 'slurm' || slurmLeaf?._value !== false));
  const tab = tabs.find((t) => t.k === url.tab) || tabs[0];
  // What the site calls its hosts (`ui.node_noun`): the node tabs' entry and
  // Live's per-host panel both say it.
  const nodeNoun =
    ((config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.node_noun?._value as
      | string
      | undefined) ?? null;
  const nodesWord = nodeNoun?.trim() || 'login nodes';
  const nodesLbl = nodesWord.charAt(0).toUpperCase() + nodesWord.slice(1);

  // The rail's entries: a tab, or the one entry its sub-tabs share.
  type RailItem = { key: string; lbl: string; icon: IconName; group: string; tabs: Tab[] };
  const railItems: RailItem[] = [];
  for (const t of tabs) {
    const key = t.parent ?? t.k;
    const have = railItems.find((r) => r.key === key);
    if (have) have.tabs.push(t);
    else
      railItems.push(
        t.parent
          ? { key, lbl: nodesLbl, icon: 'nodes', group: t.group, tabs: [t] }
          : { key, lbl: t.lbl, icon: t.icon, group: t.group, tabs: [t] },
      );
  }
  const railOn = railItems.find((r) => r.tabs.includes(tab)) ?? railItems[0];
  /** the open tab's parent entry, when it is a sub-tab */
  const parent = tab.parent ? railOn : null;
  // Back to a parent entry from elsewhere, the sub-tab last open there opens.
  const [lastSub, setLastSub] = useState<Record<string, string>>({});
  useEffect(() => {
    const p = tab.parent;
    if (p) setLastSub((m) => (m[p] === tab.k ? m : { ...m, [p]: tab.k }));
  }, [tab.k, tab.parent]);

  // A link to a tab that folded into another (`?tab=overview`) lands on the
  // part it became, once that part is on the page; the link is rewritten to
  // the tab it is now.
  useEffect(() => {
    const sec = LANDS_ON[OPENED_ON];
    if (!sec) return;
    setUrl({ tab: url.tab });
    let tries = 0;
    const t = window.setInterval(() => {
      const el = document.getElementById(sec);
      if (el || ++tries > 50) {
        window.clearInterval(t);
        el?.scrollIntoView({ block: 'start' });
      }
    }, 120);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
  // Records the demo made up (rc_dashboard/demo) say so themselves, and the
  // header says it whenever any host's do, whatever the site is called.
  const synthetic = Object.values(feeds.data ?? {}).flatMap((f) => f.synthetic_hosts ?? []);
  const mainRef = useRef<HTMLElement>(null);
  const { secs, active } = useSections(mainRef, tab.k);
  // The window and the filters, one strip held at the top while the tab scrolls
  // (lib/sticky.ts). Feeds is about the service, not a window of data: none.
  const showControls = tab.k !== 'feeds';
  const controlsRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const stuck = useStickyBar(controlsRef, sentinelRef, showControls);

  // The rail folded to its icons: a reader's convenience, kept in this browser.
  // Storage can be absent or refuse (a private window); the rail just unfolds.
  const [mini, setMini] = useState<boolean>(() => {
    try {
      return localStorage.getItem(RAIL_KEY) === '1';
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(RAIL_KEY, mini ? '1' : '0');
    } catch {
      /* not kept: it unfolds next time */
    }
  }, [mini]);
  // Below 900px the rail is a drawer over the page, opened from the top bar.
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Ctrl+K or Cmd+K anywhere, or `/` outside a field, opens "go to"; `[` folds the rail.
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;
      if (e.key === '/') {
        e.preventDefault();
        setPaletteOpen(true);
      } else if (e.key === '[') {
        e.preventDefault();
        setMini((m) => !m);
      }
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);

  // The drawer: Escape closes it, the page under it holds still, and it closes
  // by itself when the window grows past the width where the rail is a column.
  useEffect(() => {
    if (!navOpen) return;
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNavOpen(false);
    };
    const wide = window.matchMedia('(min-width: 901px)');
    const grew = () => wide.matches && setNavOpen(false);
    window.addEventListener('keydown', k);
    wide.addEventListener('change', grew);
    document.body.classList.add('noscroll');
    return () => {
      window.removeEventListener('keydown', k);
      wide.removeEventListener('change', grew);
      document.body.classList.remove('noscroll');
    };
  }, [navOpen]);
  // The agent behind every "ask" button: asked for once. Failing that, the
  // buttons stay hidden -- a question that cannot run is not offered.
  const [askInfo, setAskInfo] = useState<AskInfo | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    getAskInfo(ac.signal).then(setAskInfo).catch(() => {});
    return () => ac.abort();
  }, []);
  const askScope: AskScope = useMemo(
    () => ({
      info: askInfo,
      windowMin: resolvedWin?.minutes ?? null,
      windowLabel: resolvedWin?.label ?? null,
      who,
      cls: url.cls,
      tab: tab.lbl,
    }),
    [askInfo, resolvedWin?.minutes, resolvedWin?.label, who, url.cls, tab.lbl],
  );
  const hl = headline(feeds.data);
  const P = panels.data?.panels ?? {};

  const nClasses = url.cls.length;
  const body = (() => {
    switch (tab.k) {
      case 'live':
        return (
          <LiveTab
            live={live}
            P={P}
            traj={traj.data}
            cls={url.cls}
            who={who}
            feeds={feeds.data}
            goto={(t) => setUrl({ tab: t }, true)}
            // Opening a session or a user's sessions is navigation: back returns.
            openTraj={(sid) => setUrl({ tab: 'trajectories', traj: sid, tfrom: null, tuser: null }, true)}
            openUserTraj={(u) => setUrl({ tab: 'trajectories', tuser: u, traj: null, tfrom: null }, true)}
            nodeNoun={nodeNoun}
            userCols={
              ((config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.user_columns?._value as
                | string[]
                | undefined) ?? null
            }
            // the old Overview tab's table, under the rate chart
            whoUses={<OverviewTab P={P} live={live.live} cls={url.cls} traj={traj.data} />}
          />
        );
      case 'users':
        return (
          <UsersTab
            win={url.win}
            buildNonce={live.buildNonce}
            who={who}
            cls={url.cls}
            open={url.portrait}
            // Opening a portrait, or a session from it, is navigation: back returns.
            setOpen={(u) => setUrl({ portrait: u }, true)}
            openTraj={(sid) => setUrl({ tab: 'trajectories', traj: sid, tfrom: null, tuser: null }, true)}
            openUserTraj={(u) => setUrl({ tab: 'trajectories', tuser: u, traj: null, tfrom: null }, true)}
          />
        );
      case 'resources':
        return <ResourcesTab P={P} cls={url.cls} />;
      case 'risk':
        return <RiskIoTab P={P} cls={url.cls} part="risk" />;
      case 'chat':
        return (
          <ChatTab
            windowMin={resolvedWin?.minutes ?? null}
            windowLabel={resolvedWin?.label ?? null}
            openUserTraj={(u) => setUrl({ tab: 'trajectories', tuser: u, traj: null, tfrom: null }, true)}
            goto={(t) => setUrl({ tab: t }, true)}
          />
        );
      case 'events':
        return <EventsExplorer url={url} setUrl={setUrl} windowMin={resolvedWin?.minutes ?? null} />;
      case 'internal':
        return <InternalTab P={P} cls={url.cls} />;
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
            torder={url.torder}
            setTorder={(o) => setUrl({ torder: o })}
            tuser={url.tuser}
            clearUser={() => setUrl({ tuser: null })}
            routine={
              ((config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.routine_commands?._value as
                | string[]
                | undefined) ?? null
            }
          />
        );
      case 'slurm':
        return <SlurmTab P={P} live={live.live} cls={url.cls} goLive={() => setUrl({ tab: 'live' }, true)} />;
      case 'agents':
        return <AgentsTab P={P} cls={url.cls} />;
      case 'feeds':
        return <FeedsTab feeds={feeds} config={config} builtAt={panels.data?.full_built_at ?? null} />;
      default:
        return null;
    }
  })();

  // Opening a tab is navigation: back returns. The drawer closes behind it.
  const goTab = (k: string) => {
    setUrl({ tab: k }, true);
    window.scrollTo({ top: 0 });
    setNavOpen(false);
  };
  /** a rail entry: its tab, or for a parent the sub-tab last open there */
  const goRail = (r: RailItem) => {
    const last = lastSub[r.key];
    goTab(r.tabs.some((t) => t.k === last) ? last : r.tabs[0].k);
  };
  const footWords = TRANSPORT_WORDS[live.transport];
  const footAgo = live.receivedAt ? ago(Date.now() - live.receivedAt) : '—';

  // What "go to" offers: only what this page can already do.
  const paletteItems: PaletteItem[] = [
    ...tabs.map((t) => ({
      id: `tab:${t.k}`,
      group: 'Tabs',
      // a sub-tab under its entry's name: "Login nodes · Slurm"
      label: t.parent ? `${nodesLbl} · ${t.lbl}` : t.lbl,
      hint: t.eyebrow,
      icon: t.icon,
      // the old tab names still find where their panels went
      keywords: `${t.group}${t.k === 'live' ? ' overview who uses what' : ''}${t.parent ? ' nodes host' : ''}`,
      run: () => goTab(t.k),
    })),
    ...secs.map((s) => ({
      id: `sec:${s.id}`,
      group: `On ${tab.lbl}`,
      label: s.title,
      icon: 'section' as IconName,
      keywords: 'section panel',
      run: () => document.getElementById(s.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    })),
    ...(showControls
      ? (windows?.presets ?? []).map((p) => ({
          id: `win:${p.minutes}`,
          group: 'Window',
          label: `Last ${p.label}`,
          hint: p.minutes === resolvedWin?.minutes ? 'showing now' : undefined,
          icon: 'window' as IconName,
          keywords: 'window time range span',
          run: () => setUrl({ win: p.minutes }),
        }))
      : []),
    ...(showControls && nClasses < 3
      ? [{
          id: 'act:classes',
          group: 'Actions',
          label: 'Show all three classes',
          icon: 'overview' as IconName,
          keywords: 'class filter agent human ide',
          run: () => setUrl({ cls: [...CLS] }),
        }]
      : []),
    {
      id: 'act:refresh',
      group: 'Actions',
      label: 'Refresh the data',
      icon: 'refresh',
      keywords: 'reload update',
      run: live.refresh,
    },
    {
      id: 'act:rail',
      group: 'Actions',
      label: mini ? 'Unfold the sidebar' : 'Fold the sidebar to icons',
      hint: '[',
      icon: mini ? 'expand' : 'collapse',
      keywords: 'sidebar rail collapse expand',
      run: () => setMini((m) => !m),
    },
  ];

  return (
    <div className={`app${mini ? ' mini' : ''}`}>
      {/* Below 900px the rail is a drawer, and this bar opens it. */}
      <div className="topbar">
        <button
          className="iconbtn"
          aria-label="open the menu"
          aria-expanded={navOpen}
          aria-controls="rail"
          onClick={() => setNavOpen(true)}
        >
          <Icon name="menu" />
        </button>
        <span className="topbar-brand">
          <span className="bdot" />
          Agent Behaviour
        </span>
        <span className="topbar-tab">
          <Icon name={railOn.icon} size={16} />
          {railOn.lbl}
        </span>
        <button className="iconbtn" aria-label="go to a tab, a section or a window" onClick={() => setPaletteOpen(true)}>
          <Icon name="search" />
        </button>
      </div>
      {navOpen && <div className="rail-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <aside className={`rail${navOpen ? ' open' : ''}`} id="rail" aria-label="tabs">
        <button className="iconbtn rail-close" aria-label="close the menu" onClick={() => setNavOpen(false)}>
          <Icon name="close" />
        </button>
        <div className="brand">
          <div className="mark">
            <span className="bmark" aria-hidden="true">
              <Icon name="agents" size={15} />
            </span>
            <span className="mark-t">Agent Behaviour</span>
          </div>
          <div className="sub mono" data-tip="which deployment this is: site.name (RC_DASH_SITE_NAME)">
            {siteName || 'rc_measurement'}
          </div>
        </div>
        <button
          className="rail-search"
          onClick={() => setPaletteOpen(true)}
          data-tip={mini ? 'go to a tab, a section or a window ( / or Ctrl K )' : undefined}
        >
          <Icon name="search" size={16} />
          <span className="rail-search-t">Go to…</span>
          <kbd>/</kbd>
        </button>
        <nav>
          {railItems.map((r, i) => (
            <Fragment key={r.key}>
              {(i === 0 || railItems[i - 1].group !== r.group) && <div className="navgroup">{r.group}</div>}
              <button
                aria-selected={r === railOn}
                aria-current={r === railOn ? 'page' : undefined}
                data-tip={mini ? r.lbl : undefined}
                onClick={() => goRail(r)}
              >
                <Icon name={r.icon} />
                <span className="navlbl">{r.lbl}</span>
                {r.key === 'live' && (
                  <>
                    <i className={`navdot${live.transport === 'ws' ? '' : ' off'}`} aria-hidden="true" />
                    <span className="vh">{live.transport === 'ws' ? ', streaming' : ', not streaming'}</span>
                  </>
                )}
              </button>
              {r === railOn && !mini && <SectionList secs={secs} active={active} />}
            </Fragment>
          ))}
        </nav>
        {/* How the page gets its data. One fact to a line, and a value never
            broken mid-token: a timestamp split across two lines reads as two
            values. Folded, the dot alone, with the facts on hover. */}
        <div
          className="foot mono"
          data-tip={
            mini
              ? `updates ${footWords}\nlast update ${footAgo}\nwindow ${resolvedWin?.label ?? '—'}\npanels built ${panels.data?.full_built_at ?? '—'}`
              : undefined
          }
        >
          <div className="foot-status">
            <i className={`sdot ${live.transport}`} />
            <span className="foot-t">
              updates <b>{footWords}</b>
            </span>
          </div>
          <dl>
            <div>
              <dt>last update</dt>
              <dd>{footAgo}</dd>
            </div>
            <div>
              <dt>window</dt>
              <dd>
                {resolvedWin?.label ?? '—'}
                {windows?.retention_minutes != null && (
                  <span className="held"> of {Math.round(windows.retention_minutes / 60)}h held</span>
                )}
              </dd>
            </div>
            <div>
              <dt>panels built</dt>
              <dd>
                {panels.data?.full_built_at ?? '—'}
                {/* Restored from a saved build: the age above is that build's, and
                    everything written since was read from the files on top of it. */}
                {winStatus?.cache?.source === 'snapshot' && (
                  <span className="held" data-tip={winStatus.cache.note ?? ''}> · restored</span>
                )}
              </dd>
            </div>
          </dl>
          <BuildStamp />
        </div>
        {nClasses < 3 && (
          <div className="foot-warn mono" data-tip="the class filter above the panels hides the rest">
            <span className="foot-warn-n">{nClasses}</span>
            <span className="foot-warn-t"> of 3 classes shown</span>
          </div>
        )}
        <button
          className="rail-toggle"
          onClick={() => setMini(!mini)}
          aria-pressed={mini}
          data-tip={mini ? 'unfold the sidebar ( [ )' : 'fold the sidebar to its icons ( [ )'}
        >
          <Icon name={mini ? 'expand' : 'collapse'} size={16} />
          <span>fold</span>
        </button>
      </aside>

      <main ref={mainRef}>
        {synthetic.length > 0 && (
          <p className="fake-note" role="note">
            <b>Fake data.</b> This is a demo: every record on this page was made up for
            fictional login nodes, users and sessions. Nothing here was measured.
          </p>
        )}
        <div className={`head${showControls ? ' with-bar' : ''}`}>
          <div className="head-title">
            <span className="head-icon" aria-hidden="true">
              <Icon name={railOn.icon} size={20} />
            </span>
            <div>
              <h1>{railOn.lbl}</h1>
              <div className="head-sub">{parent ? NODES_EYEBROW : tab.eyebrow}</div>
            </div>
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
                <span className="stat" data-tip="every window ends here; nothing after it exists in this feed">
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
            {synthetic.length > 0 && (
              <span
                className="pill synthetic"
                data-tip={
                  'a demo: every record here was made up by rc_dashboard/demo -- fictional hosts, ' +
                  `users and sessions; nothing was measured\n${synthetic.join(', ')}`
                }
              >
                synthetic data
              </span>
            )}
            {hl.lag && !live.live?.clock?.pinned && (
              <span className="stat" data-tip="how long ago the collector wrote the newest record the dashboard has; a few seconds is normal">
                newest record <b>{hl.lag}</b> old
              </span>
            )}
            {hl.stale.length > 0 && (
              <span
                className="stat warn"
                data-tip={hl.stale
                  .map((f) => `${f.name}: lag ${lagStr(f.lag_s)} > max ${lagStr(f.max_age_s)}`)
                  .join('\n')}
              >
                {hl.stale.length} feed{hl.stale.length > 1 ? 's' : ''} past max_age
              </span>
            )}
            <span
              className="stat"
              data-tip={
                (live.live?.hosts ?? []).map((h) => h.host).join(', ') ||
                'no host has reported yet'
              }
            >
              <Icon name="hosts" size={14} />
              <b>{fint(live.live?.hosts?.length ?? null)}</b> host
              {(live.live?.hosts?.length ?? 0) === 1 ? '' : 's'}
            </span>
            <button className="btn" onClick={live.refresh} data-tip="ask the service for everything on this page again">
              <Icon name="refresh" size={14} />
              refresh
            </button>
          </div>
          {/* A parent entry's sub-tabs. Each is a tab of its own in the URL, so
              this is navigation (aria-current), not an ARIA tab panel. */}
          {parent && parent.tabs.length > 1 && (
            <nav className="subtabs" aria-label={parent.lbl}>
              {parent.tabs.map((t) => (
                <button
                  key={t.k}
                  aria-current={t.k === tab.k ? 'page' : undefined}
                  onClick={() => goTab(t.k)}
                  data-tip={t.eyebrow}
                >
                  <Icon name={t.icon} size={16} />
                  {t.lbl}
                </button>
              ))}
            </nav>
          )}
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

        {showControls && (
          <>
            <div ref={sentinelRef} className="controls-sentinel" aria-hidden="true" />
            <div
              ref={controlsRef}
              className={`controls${stuck ? ' stuck' : ''}`}
              role="group"
              aria-label="time window and filters"
            >
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
            </div>
          </>
        )}

        <AskScopeCtx.Provider value={askScope}>{body}</AskScopeCtx.Provider>
      </main>
      <IoCmdsDrawer win={url.win} who={who} cls={url.cls} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} items={paletteItems} />
      <TooltipLayer />
    </div>
  );
}
