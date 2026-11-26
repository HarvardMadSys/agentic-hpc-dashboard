/* Shell: rail, header, staleness readout, tab dispatch.
 *
 * The header's stale/live pill is driven by the feeds report -- `lag_s` against
 * that feed's own `max_age_s` -- not by a wall-clock guess about how often the
 * page should have heard something. A feed with a 24 h cadence is not stale at
 * 10 minutes, and the pill must not say so.
 */
import { Fragment, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useConfig, useFeeds, usePanels, useTrajectories, useWindows } from './api/hooks';
import { useLive } from './api/useLive';
import type { Transport } from './api/useLive';
import type { Cls, Facets, FeedReport, FeedStatus, TrajOrder, TrajRank } from './api/types';
import { ClassFilter } from './components/ClassFilter';
import { WhoFilter } from './components/WhoFilter';
import { IoCmdsDrawer } from './components/IoCmds';
import { StatusPill } from './components/Panel';
import { useSections } from './components/SectionNav';
import { useScrollEdges } from './lib/scrollEdges';
import { buildStamp } from './lib/buildStamp';
import { WindowPicker, offeredWindows } from './components/WindowPicker';
import { TooltipLayer } from './components/Tooltip';
import { ago, fint, lagStr } from './lib/format';
import { patched, urlQuery, whoQuery } from './lib/filters';
import type { Who } from './lib/filters';
import { useUrlState } from './lib/useUrlState';
import type { Panels } from './lib/panels';
import { useStickyBar } from './lib/sticky';
import { FrameCtx, PinnedCtx } from './lib/useNow';
import { AGENT_TYPE_LBL, CLS } from './lib/classes';
import { sessionTitle } from './lib/sessions';
import { Icon } from './components/Icon';
import { LogoGlyph } from './components/Logo';
import type { IconName } from './components/Icon';
import { CommandPalette } from './components/CommandPalette';
import type { PaletteItem } from './components/CommandPalette';
import type { SessionNav } from './tabs/OpenSession';
import { LiveTab } from './tabs/LiveTab';
import { OverviewTab } from './tabs/OverviewTab';
import { SummaryTab } from './tabs/SummaryTab';
// The other tabs are chunks of their own (lib/lazyTab.tsx). Their stylesheets
// are not: imported here, where the tabs were, each keeps its place in the
// page's one stylesheet -- the sessions list's and its inspector's where the
// Sessions tab was imported, before it was the Agents tab's last part.
import './tabs/trajectories.css';
import './tabs/agents.css';
import './tabs/users.css';
import { TabBoundary, lazyTab, preloadTabs } from './lib/lazyTab';
import { NodeNounCtx, nodeWords } from './lib/nodes';
import { feedLabel } from './lib/feeds';
import { table } from './lib/lookup';
import { zoneLabel } from './lib/zone';
import { useServiceZone } from './lib/useZone';
import { SetupScreen, noData } from './components/SetupScreen';
import { signIn } from './lib/auth';
import { countTab, setCounting } from './lib/usage';

/* Summary, the first tab, and Live, one click from it, are the first paint,
 * with the shell; every other tab loads its code apart, fetched as soon as the
 * page is idle (lib/lazyTab.tsx). The sessions
 * list and its inspector (TrajectoriesTab), the Agents tab's last part, are split
 * off like a tab: the Sessions tab was not, so that its inspector, near the
 * top of that tab, opened in the first render without scrolling the page. At
 * the foot of the Agents tab, scrolling to it is what opening it is for. */
const ResourcesTab = lazyTab(() => import('./tabs/ResourcesTab').then((m) => m.ResourcesTab));
const RiskIoTab = lazyTab(() => import('./tabs/RiskIoTab').then((m) => m.RiskIoTab));
const InternalTab = lazyTab(() => import('./tabs/InternalTab').then((m) => m.InternalTab));
const SecurityTab = lazyTab(() => import('./tabs/SecurityTab').then((m) => m.SecurityTab));
const AgentsTab = lazyTab(() => import('./tabs/AgentsTab').then((m) => m.AgentsTab));
const TrajectoriesTab = lazyTab(() => import('./tabs/TrajectoriesTab').then((m) => m.TrajectoriesTab));
const SlurmTab = lazyTab(() => import('./tabs/SlurmTab').then((m) => m.SlurmTab));
const FeedsTab = lazyTab(() => import('./tabs/FeedsTab').then((m) => m.FeedsTab));
const UsersTab = lazyTab(() => import('./tabs/UsersTab').then((m) => m.UsersTab));
const TrendTab = lazyTab(() => import('./tabs/TrendTab').then((m) => m.TrendTab));

/* Seven stops in the rail, in the order an operator reads them: the four
 * answers a reader opens the page for, what is happening now, who works with
 * agents and how, the agents and every session, what was risky, what the nodes
 * and the scheduler gave, and the system itself.
 * Eleven entries made every question a hunt; each entry now answers one, and
 * holds the tabs that answer it as sub-tabs along the foot of the head:
 *
 *   Summary is collection healthy, who runs agents, is anything risky, is a
 *           login node struggling: the first tab, which the bare address opens
 *   Live    the window now (Overview's table is its section)
 *   Users   each user's portrait, and their sessions under it
 *   Agents  › Agents (trees, blast radius, tools) · Sessions (every session, and its inspector)
 *   Risk    › Security (launch, confinement, exposure, reach) · Site rules (what should not run on a login node)
 *   Usage   › Login nodes (CPU, memory, I/O) · HPC (the scheduler) · Trend (each class over a year)
 *   System  › Feeds · Internal (only where `ui.show_internal` lists it)
 *
 * Every tab keeps its key, so `?tab=resources` and every link into a tab land
 * where they always did; old keys still open (lib/filters.ts TAB_ALIAS):
 * cascades -> agents, trajectories -> sessions, io -> resources, sandbox ->
 * security, overview -> live, hpc -> slurm. A link with no tab but the page's
 * other keys was written while Live was the first tab, and opens Live
 * (lib/filters.ts `parseUrl`). */
/* `also`: the names a tab had before, which "go to" still finds it by. */
type Parent = 'agents' | 'risk' | 'usage' | 'system';
type Tab = { k: string; lbl: string; eyebrow: string; icon: IconName; group: string; parent?: Parent; also?: string };
const TABS: Tab[] = [
  { k: 'summary', lbl: 'Summary', eyebrow: 'is collection healthy, who runs agents, is anything risky, is a login node struggling', icon: 'overview', group: 'Monitor', also: 'today home start glance four questions' },
  { k: 'live', lbl: 'Live', eyebrow: 'what runs, and who runs it', icon: 'live', group: 'Monitor', also: 'overview who uses what now' },
  { k: 'users', lbl: 'Users', eyebrow: 'how each user works with coding agents, and their sessions', icon: 'users', group: 'Monitor', also: 'people portrait' },
  { k: 'agents', lbl: 'Agents', eyebrow: 'agents, subagents, where they reached, and the tools they ran', icon: 'agents', group: 'Monitor', parent: 'agents', also: 'cascades subagents blast radius tools' },
  { k: 'sessions', lbl: 'Sessions', eyebrow: 'every session, ranked, and its process tree', icon: 'trajectories', group: 'Monitor', parent: 'agents', also: 'session trajectories trajectory inspector process tree' },
  { k: 'security', lbl: 'Security', eyebrow: 'how agents were launched and sandboxed, what they exposed, and where they reached', icon: 'security', group: 'Monitor', parent: 'risk', also: 'sandbox approval bypass credentials secrets blast radius' },
  { k: 'risk', lbl: 'Site rules', eyebrow: 'what this site’s rules say should not run on a login node', icon: 'risk', group: 'Monitor', parent: 'risk', also: 'risks risk warning dangerous commands credentials' },
  { k: 'resources', lbl: 'Login nodes', eyebrow: 'CPU, memory, waits, and I/O on the login nodes', icon: 'resources', group: 'Monitor', parent: 'usage', also: 'resources cpu memory io nodes host' },
  { k: 'slurm', lbl: 'HPC', eyebrow: 'what sessions ask of the Slurm scheduler, and what looks abnormal', icon: 'hpc', group: 'Monitor', parent: 'usage', also: 'slurm scheduler jobs queue abnormal cluster' },
  { k: 'trend', lbl: 'Trend', eyebrow: 'each class’s use over up to a year, from the roll-up table', icon: 'trend', group: 'Monitor', parent: 'usage', also: 'year history growth' },
  { k: 'feeds', lbl: 'Feeds', eyebrow: 'where the data comes from, and how fresh it is', icon: 'feeds', group: 'More', parent: 'system', also: 'provenance data sources collectors settings version' },
  { k: 'internal', lbl: 'Internal', eyebrow: 'panels not settled yet', icon: 'internal', group: 'More', parent: 'system' },
];
/** A parent entry: the rail's stop for its sub-tabs, and the head's title. The
 *  usage entry's first sub-tab is named in the site's own word for its hosts
 *  (`ui.node_noun`): its figures are login-node use, not the cluster's
 *  (README, "Scope is stated"). */
const PARENTS: Record<Parent, { lbl: string; icon: IconName; eyebrow: string }> = {
  agents: { lbl: 'Agents', icon: 'agents', eyebrow: 'every agent and its subagents, and every session, one at a time' },
  risk: { lbl: 'Risk', icon: 'security', eyebrow: 'how agents were launched and confined, what they exposed, and what should not run here' },
  usage: { lbl: 'Usage', icon: 'resources', eyebrow: 'what the login nodes gave, what was asked of the scheduler, and each class over a year' },
  system: { lbl: 'System', icon: 'feeds', eyebrow: 'where the data comes from, how fresh it is, and every setting' },
};
/** The Sessions tab's list of every session (TrajectoriesTab.tsx) and the
 *  inspector beside it (TrajectoryInspector.tsx). */
const SESSIONS_SEC = 'sec-ranked-sessions';
const INSPECTOR_SEC = 'sec-session-inspector';
/** A link to a tab that folded into another lands on the part it became. */
const LANDS_ON: Record<string, string> = table({ overview: 'sec-who-uses-what' });

/** Where a page opened on this link lands: a tab that folded into another, on
 *  the part it became; the Sessions tab with a session open, on its inspector
 *  (below the list on a narrow screen). */
function landsOn(opened: string, url: { tab: string; traj: string | null; tuser: string | null }): string | null {
  if (url.tab === 'sessions' && url.traj) return INSPECTOR_SEC;
  return LANDS_ON[opened] ?? null;
}
/** The tab the page was opened on, read once, before the link is rewritten
 *  (an effect can run twice on mount, and the second saw the rewritten link). */
const OPENED_ON = new URLSearchParams(window.location.search).get('tab') ?? '';

/** The rail folded to its icons, remembered in this browser only. */
const RAIL_KEY = 'rc.railFolded';
const TRANSPORT_WORDS = { ws: 'streaming', polling: 'every 15 s', connecting: 'connecting…', down: 'disconnected' };

/** A key typed into a field is the field's, never a shortcut. */
const typing = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

/** How long after each answer the sessions list asks again, while the Sessions tab is shown. */
const TRAJ_LIVE_MS = 5000;
/** No panels yet: one empty map, not a new one each render, so a memoized tab
 *  handed it is not drawn again for nothing. */
const NO_PANELS: Panels = Object.freeze({}) as Panels;
/** The palette's items while it is closed: none, so it has nothing to rank. */
const NO_ITEMS: PaletteItem[] = [];

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
  empty: ['no data yet', "a feed's folder is there but holds no records: the collector may not be running, or writes somewhere else"],
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
        tip: 'the live stream is unavailable' };
    case 'connecting':
      return { status: 'empty', text: 'connecting…', beat: false,
        tip: 'opening the live stream to the dashboard service' };
    default:
      return { status: 'missing', text: 'disconnected · retrying', beat: false,
        tip: 'the dashboard service is not answering' };
  }
}

export default function App() {
  const [url, setUrl] = useUrlState();
  // A part of a tab to go to once it is drawn (its anchor), while that tab is the one open: where the page
  // was opened on (`landsOn`), and the Agents tab's list or inspector, at its foot, when a session or a
  // user's sessions are opened there from elsewhere.
  const [land, setLand] = useState<{ tab: string; sec: string } | null>(() => {
    const sec = landsOn(OPENED_ON, url);
    return sec ? { tab: url.tab, sec } : null;
  });
  // The user whose portrait was opened from "sessions →": it lands on its list of their sessions, not its top.
  const [toSessions, setToSessions] = useState<string | null>(null);
  // Every handler a tab is given keeps its identity across renders, so the
  // tabs (React.memo) are drawn again only when what they show changes, not
  // with every live frame. `setUrl` itself is stable (useUrlState).
  // Opening a tab, a session or a user's sessions is navigation: back returns.
  // Another tab closes the session open on this one (lib/filters.ts `patched`).
  const goto = useCallback((t: string) => setUrl({ tab: t }, true), [setUrl]);
  const goResources = useCallback(() => setUrl({ tab: 'resources' }, true), [setUrl]);
  // Summary's links: a tab, from its top or landing on the part named (`land`).
  const goPart = useCallback(
    (t: string, sec?: string) => {
      setUrl({ tab: t }, true);
      setLand(sec ? { tab: t, sec } : null);
      window.scrollTo({ top: 0 });
    },
    [setUrl],
  );
  // ... and the same link as an address, over this page's window and filters,
  // for a reader who opens it in a new tab
  const hrefTo = useCallback(
    (t: string) => `${window.location.pathname}?${urlQuery(patched(url, { tab: t }))}`,
    [url],
  );
  // Every session opens on the Sessions tab: its list, and the inspector beside it.
  const openTraj = useCallback(
    (sid: string) => {
      setUrl({ tab: 'sessions', traj: sid, tfrom: null, tuser: null }, true);
      setLand({ tab: 'sessions', sec: INSPECTOR_SEC });
    },
    [setUrl],
  );
  // A user's sessions are their portrait's, on Users, where each opens in place.
  const openUserTraj = useCallback(
    (u: string) => {
      setToSessions(u);
      setUrl({ tab: 'users', portrait: u, traj: null, tfrom: null }, true);
    },
    [setUrl],
  );
  // Every one of a user's sessions, of every class, the portrait's newest few
  // and the rest: the Sessions tab's list, narrowed to them.
  const listUserTraj = useCallback(
    (u: string) => {
      setUrl({ tab: 'sessions', tuser: u, traj: null, tfrom: null }, true);
      setLand({ tab: 'sessions', sec: SESSIONS_SEC });
    },
    [setUrl],
  );
  // A portrait opened or closed closes the session open under the last one.
  const setPortrait = useCallback(
    (u: string | null) => {
      setToSessions(null);
      setUrl({ portrait: u, traj: null, tfrom: null }, true);
    },
    [setUrl],
  );
  const setRank = useCallback((r: TrajRank) => setUrl({ rank: r }), [setUrl]);
  const select = useCallback((sid: string | null) => setUrl({ traj: sid, tfrom: null }, true), [setUrl]);
  const setTfrom = useCallback((t: number | null) => setUrl({ tfrom: t }, true), [setUrl]);
  const setTorder = useCallback((o: TrajOrder) => setUrl({ torder: o }), [setUrl]);
  const clearUser = useCallback(() => setUrl({ tuser: null }), [setUrl]);
  // The other tabs' code, fetched once the page is idle after its first
  // paint: a tab opened later is drawn at once, with no wait.
  useEffect(() => {
    if (typeof window.requestIdleCallback === 'function') {
      const h = window.requestIdleCallback(preloadTabs, { timeout: 3000 });
      return () => window.cancelIdleCallback(h);
    }
    const t = window.setTimeout(preloadTabs, 1000);
    return () => window.clearTimeout(t);
  }, []);
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
  // Re-read every few seconds while the sessions list (the Sessions tab) is open.
  const traj = useTrajectories(url.rank, url.win, live.buildNonce, who,
    url.tab === 'sessions' ? TRAJ_LIVE_MS : null);
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
  // The service's floor (5 minutes): `/api/windows` says it, the websocket's
  // `windows` frame that supersedes it does not.
  const minWin = windowsRest.data?.min_minutes ?? windows?.min_minutes ?? null;
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

  // The refresh button and command: everything on the page asked for again --
  // the live frame, the windows and the feeds (`live.refresh`), the panels and
  // the sessions -- not only the live frame.
  const refreshLive = live.refresh;
  const reloadTraj = traj.reload;
  const refresh = useCallback(() => {
    refreshLive();
    reloadPanels();
    reloadTraj();
  }, [refreshLive, reloadPanels, reloadTraj]);

  // A `feeds` frame supersedes the REST snapshot.
  const wsFeeds = live.feeds;
  const mergeFeeds = feeds.merge;
  useEffect(() => {
    if (wsFeeds) mergeFeeds(wsFeeds);
  }, [wsFeeds, mergeFeeds]);

  // A cluster without Slurm sets `slurm.enabled: false`; its tab goes away.
  const slurmLeaf = (config.data?.slurm as Record<string, { _value?: unknown }> | undefined)?.enabled;
  const tabs = TABS.filter((t) => t.k !== 'slurm' || slurmLeaf?._value !== false);
  // A tab that is not here -- an old link to Search & export (`?tab=events`) or
  // to Ask LLM (`?tab=chat`), both since removed, or HPC where it is off --
  // opens the first.
  const tab = tabs.find((t) => t.k === url.tab) || tabs[0];
  // Internal's panels are not settled: listed in the rail only where the site
  // asks (`ui.show_internal`), or while it is the tab open. A link still opens it.
  const showInternal =
    (config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.show_internal?._value === true;
  // What the site calls its hosts (`ui.node_noun`): every place the page names
  // them says it, through NodeNounCtx (lib/nodes.ts).
  const nodeNoun =
    ((config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.node_noun?._value as
      | string
      | undefined) ?? null;
  const nodes = nodeWords(nodeNoun);
  const nodesLbl = nodes.Many;

  /** a tab's name; the login nodes' sub-tab in the site's own word for its hosts */
  const tabLbl = (t: Tab) => (t.k === 'resources' ? nodesLbl : t.lbl);
  // The rail's entries: a tab, or the one entry its sub-tabs share (PARENTS).
  // An entry left with one sub-tab -- System where Internal is not listed --
  // takes that tab's name: a stop called System that opens Feeds is a riddle.
  type RailItem = { key: string; lbl: string; icon: IconName; group: string; tabs: Tab[] };
  const railItems: RailItem[] = [];
  for (const t of tabs) {
    if (t.k === 'internal' && !showInternal && tab.k !== 'internal') continue;
    const key = t.parent ?? t.k;
    const have = railItems.find((r) => r.key === key);
    if (have) have.tabs.push(t);
    else
      railItems.push(
        t.parent
          ? { key, lbl: PARENTS[t.parent].lbl, icon: PARENTS[t.parent].icon, group: t.group, tabs: [t] }
          : { key, lbl: t.lbl, icon: t.icon, group: t.group, tabs: [t] },
      );
  }
  for (const r of railItems) {
    if (r.tabs.length === 1 && r.tabs[0].parent) {
      r.lbl = tabLbl(r.tabs[0]);
      r.icon = r.tabs[0].icon;
    }
  }
  const railOn = railItems.find((r) => r.tabs.includes(tab)) ?? railItems[0];
  /** the open tab's parent entry, when it has sub-tabs to show */
  const parent = tab.parent && railOn.tabs.length > 1 ? railOn : null;
  /** what the head's title answers: the entry's question, or the tab's own */
  const eyebrow = parent && tab.parent ? PARENTS[tab.parent].eyebrow : tab.eyebrow;
  // Which tabs readers open, counted for the site's own use (lib/usage.ts): only
  // once /api/config says the site counts (`ui.count_views`).
  const countViews = (config.data?.ui as Record<string, { _value?: unknown }> | undefined)?.count_views
    ?._value;
  useEffect(() => {
    if (typeof countViews === 'boolean') setCounting(countViews);
  }, [countViews]);
  useEffect(() => {
    countTab(tab.k);
  }, [tab.k]);
  // Back to a parent entry from elsewhere, the sub-tab last open there opens.
  const [lastSub, setLastSub] = useState<Record<string, string>>({});
  useEffect(() => {
    const p = tab.parent;
    if (p) setLastSub((m) => (m[p] === tab.k ? m : { ...m, [p]: tab.k }));
  }, [tab.k, tab.parent]);

  // Which deployment: the sidebar and the browser tab both say, so a frozen
  // capture and a live feed open side by side are never confused. With no
  // site name set, the dashboard's own host says it (`_meta.host`).
  const meta = config.data?._meta as Record<string, { _value?: unknown }> | undefined;
  const hostName = typeof meta?.host?._value === 'string' ? meta.host._value : '';
  const siteName = String(
    (config.data?.site as Record<string, { _value?: unknown }> | undefined)?.name?._value ?? '',
  ) || hostName;
  // Every time on the page is in the service's zone (lib/zone.ts), which the
  // config names; set before the tabs draw, and they are drawn again once it is
  // known (their key), since what they drew before was in the browser's zone.
  const tzKey = useServiceZone(config.data as Record<string, unknown> | null | undefined);
  const zl = zoneLabel();
  useEffect(() => {
    document.title = siteName ? `${siteName} · Agent Observatory` : 'Agent Observatory';
  }, [siteName]);
  // Names are aliased on the way out (rc_dashboard/anonymize.py): the header
  // says so, or a reader takes `user-7f3a9c1e` for a real account.
  const anonymized =
    (config.data?.anonymize as Record<string, { _value?: unknown }> | undefined)?.enabled?._value ===
    true;
  // Records the demo made up (rc_dashboard/demo) say so themselves, and the
  // header says it whenever any host's do, whatever the site is called.
  const synthetic = Object.values(feeds.data ?? {}).flatMap((f) => f.synthetic_hosts ?? []);
  // How the reader got in (lib/auth.ts): the head names a user the site's single
  // sign-on signed in, and the rail offers to sign out where there is a way to.
  const signin = signIn(config.data as Record<string, unknown> | null | undefined);
  const mainRef = useRef<HTMLElement>(null);
  const { secs, go: goSec } = useSections(mainRef, tab.k);
  useScrollEdges(mainRef);
  // A link to a tab that folded into another (`?tab=overview`) is rewritten to
  // the tab it is now; it lands on the part it became (`land`).
  useEffect(() => {
    if (LANDS_ON[OPENED_ON]) setUrl({ tab: url.tab });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // A part of a tab to go to (`land`), gone to once it is on the page and what
  // is drawn above it has come -- the panels and the sessions: a part at the
  // foot of the Agents tab, gone to sooner, is pushed down by what arrives
  // above it after the jump. Going to another tab, or scrolling by hand first,
  // lets it go.
  const settled = (panels.data != null || panels.error != null) && (traj.data != null || traj.error != null);
  useEffect(() => {
    if (!land) return;
    if (land.tab !== tab.k) {
      setLand(null);
      return;
    }
    const byHand = () => setLand(null);
    window.addEventListener('wheel', byHand, { passive: true });
    window.addEventListener('touchstart', byHand, { passive: true });
    let t = 0;
    const done = () => {
      window.clearInterval(t);
      window.removeEventListener('wheel', byHand);
      window.removeEventListener('touchstart', byHand);
    };
    if (!settled) return done;
    let tries = 0;
    const reach = () => {
      const el = document.getElementById(land.sec);
      if (!el && ++tries <= 50) return false;
      setLand(null);
      if (el) goSec(land.sec, 'auto');
      return true;
    };
    if (!reach()) {
      t = window.setInterval(() => {
        if (reach()) done();
      }, 120);
    }
    return done;
  }, [land, tab.k, goSec, settled]);
  // No feed holds a record: the tabs give way to the setup screen, which says
  // where the service looked and what to do (components/SetupScreen.tsx). Not
  // Feeds, which is where it looked; nor Trend, which reads the roll-up table:
  // that holds days whose files are gone (expired, or not yet copied back after a
  // restore), and Trend says so itself when it holds none.
  const empty = noData(feeds.data);
  const setup = empty && tab.k !== 'feeds' && tab.k !== 'trend';
  // The window and the filters, one strip held at the top while the tab scrolls
  // (lib/sticky.ts). Feeds is about the service, not a window of data: none;
  // nor the setup screen, which has no window to pick.
  const showControls = tab.k !== 'feeds' && !setup;
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
  const closePalette = useCallback(() => setPaletteOpen(false), []);

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
  const hl = headline(feeds.data);
  // The header says one thing about the data: the pill (pillOf). What stands
  // behind it -- the newest record's age, each feed past its own limit, the
  // login nodes that reported -- is its hover, not a chip each.
  const pill = empty
    ? { status: 'stale' as const, text: 'no data', beat: false,
        tip: 'no feed holds a record: the collector is not writing where the dashboard looks (see the page below)' }
    : pillOf(hl.status, live.transport);
  const hostNames = (live.live?.hosts ?? []).map((h) => h.host);
  const statusTip = [
    pill.tip,
    hl.lag ? `newest record ${hl.lag} old` : null,
    ...hl.stale.map(
      (f) => `${feedLabel(f.name)}: newest record ${lagStr(f.lag_s)} old, past its limit of ${lagStr(f.max_age_s)}`,
    ),
    hostNames.length
      ? `${fint(hostNames.length)} ${hostNames.length === 1 ? nodes.one : nodes.many} reporting: ${hostNames.join(', ')}`
      : `no ${nodes.one} has reported yet`,
  ]
    .filter(Boolean)
    .join('\n');
  const P = panels.data?.panels ?? NO_PANELS;
  const cls: Cls[] = url.cls;
  // the old Overview tab's table, under Live's rate chart: an element made
  // again only when what it shows changes, so Live's memo holds across renders
  const whoUses = useMemo(() => <OverviewTab P={P} cls={cls} traj={traj.data} />, [P, cls, traj.data]);
  const uiLeaf = config.data?.ui as Record<string, { _value?: unknown }> | undefined;
  const userCols = (uiLeaf?.user_columns?._value as string[] | undefined) ?? null;
  const routine = (uiLeaf?.routine_commands?._value as string[] | undefined) ?? null;
  // A session opened under a portrait on Users: the same URL keys as the Agents
  // tab's inspector, one object while none of them changes, so the tab's memo holds.
  const sessionNav: SessionNav = useMemo(
    () => ({ sel: url.traj, select, tfrom: url.tfrom, setTfrom, torder: url.torder, setTorder, routine }),
    [url.traj, select, url.tfrom, setTfrom, url.torder, setTorder, routine],
  );

  const nClasses = url.cls.length;
  const body = (() => {
    if (setup) return <SetupScreen feeds={feeds.data!} goFeeds={() => goto('feeds')} />;
    switch (tab.k) {
      case 'summary':
        return <SummaryTab P={P} traj={traj.data} trajError={traj.error} cls={cls} go={goPart} href={hrefTo} />;
      case 'live':
        return (
          <LiveTab
            live={live}
            P={P}
            traj={traj.data}
            cls={cls}
            who={who}
            feeds={feeds.data}
            goto={goto}
            openTraj={openTraj}
            openUserTraj={openUserTraj}
            userCols={userCols}
            whoUses={whoUses}
          />
        );
      case 'users':
        return (
          <UsersTab
            win={url.win}
            buildNonce={live.buildNonce}
            who={who}
            cls={cls}
            open={url.portrait}
            // Opening a portrait, or a session from it, is navigation: back returns.
            setOpen={setPortrait}
            // a session of theirs opens under the portrait, on this tab
            session={sessionNav}
            trajRows={traj.data?.rows ?? null}
            toSessions={toSessions != null && toSessions === url.portrait}
            listUserTraj={listUserTraj}
          />
        );
      case 'trend':
        return <TrendTab cls={cls} who={who} />;
      case 'resources':
        return <ResourcesTab P={P} cls={cls} live={live.live} />;
      case 'risk':
        return <RiskIoTab P={P} cls={cls} part="risk" />;
      case 'internal':
        return <InternalTab P={P} cls={cls} />;
      case 'security':
        return <SecurityTab P={P} cls={cls} />;
      case 'slurm':
        return <SlurmTab P={P} live={live.live} cls={cls} goResources={goResources} />;
      case 'agents':
        return <AgentsTab P={P} cls={cls} />;
      case 'sessions':
        return (
          <>
            <TrajectoriesTab
              traj={traj}
              rank={url.rank}
              setRank={setRank}
              cls={cls}
              win={url.win}
              sel={url.traj}
              // Opening a session is navigation: back closes it again.
              select={select}
              tfrom={url.tfrom}
              setTfrom={setTfrom}
              torder={url.torder}
              setTorder={setTorder}
              tuser={url.tuser}
              clearUser={clearUser}
              routine={routine}
              landing={land?.sec === INSPECTOR_SEC}
            />
          </>
        );
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
  // The rest of how the page gets its data, one fact to a line, on the status
  // line's hover: the window and how much of it the service holds, when the
  // panels were built -- restored from a saved build, everything written since
  // was read from the files on top of it -- and which code this page is.
  const footTip = [
    `updates ${footWords}`,
    `last update ${footAgo}`,
    `window ${resolvedWin?.label ?? '—'}` +
      (windows?.retention_minutes != null ? ` of ${Math.round(windows.retention_minutes / 60)}h held` : ''),
    `panels built ${panels.data?.full_built_at ?? '—'}` +
      (winStatus?.cache?.source === 'snapshot'
        ? ` · restored from a saved build${winStatus.cache.note ? `: ${winStatus.cache.note}` : ''}`
        : ''),
    buildStamp(),
  ].join('\n');

  // The users "go to" finds: the filter's (the window's busiest), and anyone
  // with a session listed, which the page holds before the filter's are built.
  const paletteUsers = !paletteOpen ? [] : [...new Set([
    ...(facets?.users ?? []).map((u) => u.user),
    ...Object.values(traj.data?.rows ?? {}).map((r) => r.user),
  ].filter((u): u is string => typeof u === 'string' && u !== ''))];
  // What "go to" offers: only what this page can already do. Made only while
  // it is open; closed, it is handed nothing and is not drawn again.
  const paletteItems: PaletteItem[] = !paletteOpen ? NO_ITEMS : [
    ...tabs
      .filter((t) => t.k !== 'internal' || showInternal)
      .map((t) => ({
        id: `tab:${t.k}`,
        group: 'Tabs',
        // a sub-tab under its entry's name: "Usage · HPC"
        label: t.parent ? `${PARENTS[t.parent].lbl} · ${tabLbl(t)}` : t.lbl,
        hint: t.eyebrow,
        icon: t.icon,
        // the old tab names still find where their panels went
        keywords: `${t.group}${t.also ? ` ${t.also}` : ''}`,
        run: () => goTab(t.k),
      })),
    ...secs.map((s) => ({
      id: `sec:${s.id}`,
      group: `On ${tab.lbl}`,
      label: s.title,
      icon: 'section' as IconName,
      keywords: 'section panel',
      run: () => goSec(s.id),
    })),
    // Who and what the window holds, once a word is typed: "what is ada
    // doing?" and "what is on login02?" are the first questions an operator
    // asks, and this list was where a username once found nothing.
    ...paletteUsers.map((u) => ({
      id: `user:${u}`,
      group: 'Users',
      label: u,
      hint: 'their portrait',
      icon: 'users' as IconName,
      keywords: 'user account person portrait who',
      searchOnly: true,
      run: () => {
        setToSessions(null);
        setUrl({ tab: 'users', portrait: u, traj: null, tfrom: null }, true);
        setNavOpen(false);
      },
    })),
    ...paletteUsers.map((u) => ({
      id: `only:${u}`,
      group: 'Narrow the page',
      label: `Only ${u}`,
      hint: 'every tab shows this user alone',
      icon: 'users' as IconName,
      keywords: 'filter user only narrow',
      searchOnly: true,
      run: () => setUrl({ users: [u] }),
    })),
    ...(live.live?.hosts ?? []).map((h) => ({
      id: `host:${h.host}`,
      group: nodesLbl,
      label: h.host,
      hint: `${nodes.one} health`,
      icon: 'nodes' as IconName,
      keywords: 'host node server machine login health',
      searchOnly: true,
      run: () => {
        setUrl({ tab: 'resources' }, true);
        setLand({ tab: 'resources', sec: 'sec-node-health' });
        setNavOpen(false);
      },
    })),
    ...(facets?.agent_types ?? []).filter((t) => t.agent_type).map((t) => ({
      id: `atype:${t.agent_type}`,
      group: 'Narrow the page',
      label: `Only ${AGENT_TYPE_LBL[t.agent_type] ?? t.agent_type}`,
      hint: 'every tab shows this agent type alone',
      icon: 'agents' as IconName,
      keywords: 'agent type filter only narrow',
      searchOnly: true,
      run: () => setUrl({ atypes: [t.agent_type] }),
    })),
    ...Object.values(traj.data?.rows ?? {}).slice(0, 200).map((r) => {
      const nm = sessionTitle(r);
      return {
        id: `traj:${r.sid}`,
        group: 'Sessions',
        label: nm.title,
        hint: [nm.detail, nm.date].filter(Boolean).join(' · ') || undefined,
        icon: 'trajectories' as IconName,
        keywords: `session ${r.host} ${r.user} ${AGENT_TYPE_LBL[r.agent_type ?? ''] ?? ''} ${r.sid}`,
        searchOnly: true,
        run: () => {
          openTraj(r.sid);
          setNavOpen(false);
        },
      };
    }),
    ...(showControls
      ? offeredWindows(windows?.presets, minWin).map((p) => ({
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
      run: refresh,
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

  // What the site calls its hosts reaches every panel through NodeNounCtx.
  const page = (
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
          <span className="bmark bmark-sm" aria-hidden="true">
            <LogoGlyph size={22} />
          </span>
          Agent Observatory
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
              <LogoGlyph />
            </span>
            <span className="mark-t">Agent Observatory</span>
          </div>
          {siteName && (
            <div className="sub mono" data-tip="which deployment this is">
              {siteName}
            </div>
          )}
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
              {/* the groups part by a rule, not a word */}
              {i > 0 && railItems[i - 1].group !== r.group && <div className="navgroup" aria-hidden="true" />}
              <button
                aria-current={r === railOn ? 'page' : undefined}
                data-tip={mini ? r.lbl : undefined}
                onClick={() => goRail(r)}
              >
                <Icon name={r.icon} />
                <span className="navlbl">{r.lbl}</span>
                {r.key === 'live' && (
                  <>
                    {/* lit while records stream in: a socket open over no data is not live */}
                    <i className={`navdot${live.transport === 'ws' && !empty ? '' : ' off'}`} aria-hidden="true" />
                    <span className="vh">
                      {empty ? ', no data yet' : live.transport === 'ws' ? ', streaming' : ', not streaming'}
                    </span>
                  </>
                )}
              </button>
            </Fragment>
          ))}
        </nav>
        {/* The rail's foot, held at its bottom: a warning while classes are
            hidden, sign-out, how the page gets its data, and the credits. */}
        <div className="rail-end">
          {nClasses < 3 && (
            <div className="foot-warn mono" data-tip="the class filter above the panels hides the rest">
              <span className="foot-warn-n">{nClasses}</span>
              <span className="foot-warn-t"> of 3 classes shown</span>
            </div>
          )}
          {/* Only where there is a way out (lib/auth.ts): after the password, the
              service's own form, so signing out works without the page's own code;
              under the site's single sign-on, a link to the proxy's sign-out. */}
          {signin.signOut?.kind === 'form' && (
            <form className="rail-signout" method="post" action="/logout">
              <button type="submit" data-tip="sign this browser out">
                <Icon name="signout" size={16} />
                <span>Sign out</span>
              </button>
            </form>
          )}
          {signin.signOut?.kind === 'link' && (
            <div className="rail-signout">
              <a href={signin.signOut.href} data-tip="sign out of the site's single sign-on">
                <Icon name="signout" size={16} />
                <span>Sign out</span>
              </a>
            </div>
          )}
          {/* How the page gets its data, in one line: the transport as a light,
              and the last update. The window, when the panels were built, and
              which code this page is are its hover. Folded, the light alone. */}
          <div className="foot">
            {/* a tab stop, so its facts show on keyboard focus, and read to a
                screen reader, which the tip layer is hidden from */}
            <div className="foot-status mono" data-tip={footTip} tabIndex={0}>
              <i className={`sdot ${live.transport}`} />
              <span className="foot-t">
                {footWords} · {footAgo}
              </span>
              <span className="vh">{`. ${footTip}`}</span>
            </div>
            <button
              className="iconbtn rail-toggle"
              onClick={() => setMini(!mini)}
              aria-pressed={mini}
              aria-label={mini ? 'unfold the sidebar' : 'fold the sidebar to its icons'}
              data-tip={mini ? 'unfold the sidebar ( [ )' : 'fold the sidebar to its icons ( [ )'}
            >
              <Icon name={mini ? 'expand' : 'collapse'} size={16} />
            </button>
          </div>
          {/* MaDSys, Harvard SEAS and FASRC. Folded, the MaDSys hexagon alone. */}
          <div className="credits">
            <a
              className="credits-lab"
              href="https://madsys.seas.harvard.edu/"
              target="_blank"
              rel="noopener noreferrer"
              data-tip={mini ? 'MaDSys · Harvard SEAS · FASRC' : undefined}
            >
              <img
                className="credits-full"
                src="./logos/madsys.png"
                alt="Harvard University MaDSys: Measurement and Design of Systems"
                width={385}
                height={140}
              />
              <img className="credits-mark" src="./logos/madsys-mark.png" alt="MaDSys" width={117} height={132} />
            </a>
            <div className="credits-partners">
              <a href="https://seas.harvard.edu/" target="_blank" rel="noopener noreferrer">
                <img
                  src="./logos/harvard-seas.svg"
                  alt="Harvard John A. Paulson School of Engineering and Applied Sciences"
                  width={307}
                  height={86}
                />
              </a>
              <a href="https://www.rc.fas.harvard.edu/" target="_blank" rel="noopener noreferrer">
                <img
                  src="./logos/fasrc.png"
                  alt="FAS Research Computing, Harvard University"
                  width={284}
                  height={61}
                />
              </a>
            </div>
          </div>
        </div>
      </aside>

      <main ref={mainRef}>
        {synthetic.length > 0 && (
          <p className="fake-note" role="note">
            <b>Fake data.</b> A demo: every record on this page was made up. Nothing here was
            measured.
          </p>
        )}
        <div className={`head${showControls ? ' with-bar' : ''}`}>
          {/* The tab's name, and what it answers on hover. Below 900px the top
              bar names the tab, so this one is for a screen reader there. */}
          <h1 data-tip={eyebrow}>{railOn.lbl}</h1>
          {/* what the tab answers, for a screen reader: the tip layer is hidden from one */}
          <p className="vh">{eyebrow}</p>
          <div className="status">
            {/* A pinned clock is a capture, not a feed: "live" and a lag of a
                second would both be false (rc_dashboard/clock.py). */}
            {live.live?.clock?.pinned ? (
              <StatusPill
                focusable
                status="empty"
                text={`frozen capture · as of ${live.live.clock.as_of ?? '—'}`}
                tip={
                  "not a live feed: the clock stops at the capture's end; every window ends there, and nothing " +
                  `after it exists in this feed\n${live.live.clock.source}`
                }
              />
            ) : (
              <StatusPill {...pill} tip={statusTip} focusable />
            )}
            {anonymized && (
              <span
                className="pill"
                data-tip="names are stable aliases (user-…, lab-…); email addresses and user IDs are hidden"
              >
                anonymized
              </span>
            )}
            {synthetic.length > 0 && (
              <span
                className="pill synthetic"
                data-tip={`made up by the demo:\n${synthetic.join(', ')}`}
              >
                fake data
              </span>
            )}
            {/* the one zone every time on the page is in: a tab stop, so its
                words reach the keyboard and a screen reader */}
            <span className="pill zone" data-tip={zl.tip} tabIndex={0}>
              {zl.short}
              <span className="vh">{`. ${zl.tip}`}</span>
            </span>
            {/* who the site's single sign-on signed in, as the proxy named them
                (lib/auth.ts); with the password or on an open page, no one */}
            {signin.user && (
              <span
                className="pill who"
                data-tip={`signed in as ${signin.user}, through the site's single sign-on`}
                tabIndex={0}
              >
                <Icon name="users" size={12} />
                <span className="who-n">{signin.user}</span>
                <span className="vh">, signed in through the site's single sign-on</span>
              </span>
            )}
            <button
              className="iconbtn"
              onClick={refresh}
              aria-label="refresh"
              data-tip="refresh: ask the service for everything on this page again"
            >
              <Icon name="refresh" size={16} />
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
                  {tabLbl(t)}
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
              {/* Trend reads its own range, a year at most, never the window */}
              {tab.k !== 'trend' && <WindowPicker
                value={url.win}
                onChange={(w) => setUrl({ win: w })}
                windows={windows}
                minMinutes={minWin}
                resolved={resolvedWin}
                status={winStatus}
                backfill={live.live?.backfill}
              />}
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

        {/* a tab whose code has not come yet draws nothing under the controls until it has */}
        <TabBoundary key={`${tab.k}|${tzKey}`}>
          <Suspense fallback={null}>{body}</Suspense>
        </TabBoundary>
      </main>
      <IoCmdsDrawer win={url.win} who={who} cls={cls} />
      <CommandPalette open={paletteOpen} onClose={closePalette} items={paletteItems} />
      <TooltipLayer />
    </div>
  );
  return (
    <NodeNounCtx.Provider value={nodeNoun}>
      {/* Text read against the clock renders again with each live frame, and
          the frozen-capture wording when the clock's pin flips (lib/useNow.ts). */}
      <FrameCtx.Provider value={live.receivedAt}>
        <PinnedCtx.Provider value={live.live?.clock?.pinned === true}>{page}</PinnedCtx.Provider>
      </FrameCtx.Provider>
    </NodeNounCtx.Provider>
  );
}
