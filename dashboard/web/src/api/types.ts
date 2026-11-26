/* Types for the rc_dashboard HTTP/WS contract.
 *
 * Every panel payload is `PanelEnvelope & <shape>`: the envelope is what makes
 * "zero is not missing" renderable, so it is never optional. Numeric leaves are
 * `number | null` wherever the backend can legitimately not know the value --
 * `null` is absent, `0` is measured.
 */

/* --------------------------------------------------------------- classes */

/** The three physical classes. NEVER fold the IDE class into agent.
 *  `unlabeled` is a residual bin rendered alongside them, never merged into them. */
export type Cls = 'agent' | 'human' | 'ide';

/* ---------------------------------------------------------------- config */

export interface ConfigLeaf {
  _value: unknown;
  _origin: string;
}
/** `/api/config` is an arbitrarily nested tree whose leaves are `ConfigLeaf`. */
export type ConfigNode = ConfigLeaf | { [k: string]: ConfigNode };
export type ConfigTree = Record<string, ConfigNode>;

export function isConfigLeaf(n: ConfigNode): n is ConfigLeaf {
  return !!n && typeof n === 'object' && '_value' in n && '_origin' in n;
}

/** How the reader of this page got in: `/api/config`'s `_meta.auth._value`, with
 *  `_origin` `request` (rc_dashboard/auth.py `reader`). Through the site's single
 *  sign-on, a proxy in front of the service (`proxy`); with the dashboard's own
 *  password (`password`); or neither, on a page served open (`open`). */
export type AuthMethod = 'proxy' | 'password' | 'open';
export interface AuthInfo {
  method: AuthMethod;
  /** the user the proxy named; null unless `method` is `proxy` */
  user: string | null;
  /** where Sign out goes when the proxy signed the reader in
   *  (`server.auth.proxy_logout_url`); null otherwise, and then, under the proxy,
   *  no Sign out. After the password, Sign out is the service's own form. */
  logout_url: string | null;
}

/* ----------------------------------------------------------------- feeds */

export type FeedStatus = 'ok' | 'stale' | 'empty' | 'missing';

export interface FeedReport {
  name: string;
  tier: string;
  label: string;
  cadence: string | null;
  configured: string[];
  resolved: string[];
  missing: string[];
  origin: string | null;
  env: string | null;
  cli: string | null;
  exists: boolean;
  status: FeedStatus;
  required: boolean;
  n_files: number | null;
  bytes: number | null;
  days: string[];
  hosts: string[];
  newest_record_ts: string | null;
  lag_s: number | null;
  lag: string | null;
  max_age_s: number | null;
  rows: number | null;
  notice: string | null;
  backfill: unknown;
  /** hosts whose records were written by the demo (rc_dashboard/demo), not measured */
  synthetic_hosts?: string[];
  /** the eBPF tier: its sealed day-files kept as Parquet (rc_dashboard/history.py) */
  history?: HistoryStatus;
  /** every folder the service looked in for this feed, and what it found there
   *  (rc_dashboard/feeds.py; the same lines as `check-feeds`) */
  candidates?: FeedCandidate[];
}
export type FeedsResponse = Record<string, FeedReport>;

export interface FeedCandidate {
  path: string;
  /** found: holds day-files; empty: there, but none; missing: no such folder;
   *  denied: the service's account may not read it */
  state: 'found' | 'empty' | 'missing' | 'denied';
  /** e.g. "12 files", or who may read a denied folder */
  note: string | null;
}

/** The history store: a Parquet copy of every sealed (`.jsonl.gz`) day-file of
 *  the eBPF tier, made in the background. A selective read of a sealed file --
 *  one user's events, one session's tree -- and a search of it by time are
 *  answered from its copy once there is one; nothing waits for the store. */
export interface HistoryStatus {
  enabled: boolean;
  state: 'off' | 'idle' | 'converting' | 'error';
  note?: string | null;
  dir?: string;
  workers?: number;
  /** the newest dated dirs kept; 0 for every one */
  days?: number;
  source_roots?: string[];
  store_roots?: string[];
  /** sealed day-files with a current copy */
  replicas?: number;
  /** Parquet files, one per table a day-file has records of */
  files?: number;
  bytes?: number;
  /** day-files converted since the service started */
  converted?: number;
  /** sealed day-files a reader asked for since the service started: answered
   *  from a copy, or read as JSONL (no copy yet) */
  lookups?: { replica: number; jsonl: number };
  /** the current (or last) round of conversions */
  progress?: {
    todo: number;
    done: number;
    failed: number;
    records: number;
    bytes_in: number;
    elapsed_s: number;
  };
  errors?: { at: number; file: string | null; error: string }[];
}

/* ------------------------------------------------------------- collectors */

/** One collector run, from its own `meta` and `stop` records (rc_dashboard/health.py). */
export interface CollectorRun {
  pid: number | null;
  started: number | null;
  stopped: number | null;
  last_beat: number | null;
  ran_s: number | null;
  stop_reason: string | null;
  records: number | null;
  truncated: number | null;
  /** kernel events lost / handed over; null until the run stops */
  lost: number | null;
  seen: number | null;
  lost_pct: number | null;
  lost_by_buffer: Record<string, number> | null;
  kernel: string | null;
  bcc: string | null;
  schema_version: number | null;
  /** the collector's own version, from its `meta` record (`ebpfm version`); null from a collector too old to say */
  collector_version?: string | null;
}

/** How a host's files reach the dashboard where they are copied (rc_dashboard
 *  status: deploy/fleet/pull.py's state, or the ssh stream's status.json):
 *  a copy that failed is not a collector that stopped. */
export interface CollectorTransport {
  kind: 'rsync' | 'stream';
  ok: boolean;
  last_ok_age_s: number | null;
  error: string | null;
}

export type CollectorVerdict = 'ok' | 'degraded' | 'stopped' | 'silent' | 'unknown';

export interface CollectorTierFiles {
  files: number;
  bytes: number;
  last_write: number | null;
}

export interface CollectorHost {
  host: string;
  verdict: CollectorVerdict;
  reasons: string[];
  /** what this host's kernel cannot measure: true of every run until it changes */
  limits: string[];
  heartbeat: { last: number | null; age_s: number | null; interval_s: number | null; seen: number; max_age_s: number };
  running: CollectorRun | null;
  features: string[];
  unavailable: { feature: string; why: string; impact: string | null }[];
  sysctl_off: { sysctl: string; value: unknown; want: string; impact: string }[];
  sysctl: Record<string, unknown> | null;
  euid: number | null;
  runs: CollectorRun[];
  restarts: number;
  files: Partial<Record<'ebpf' | 'ebpf_node', CollectorTierFiles>>;
  /** its records were written by the demo (rc_dashboard/demo), not by a collector */
  synthetic?: boolean;
  /** where its files are copied to the dashboard, the copy's own state; null or absent where they are read in place */
  transport?: CollectorTransport | null;
}

export interface CollectorsResponse {
  hosts: CollectorHost[];
  tiers: Record<'ebpf' | 'ebpf_node', Partial<FeedReport>>;
  now: number;
  backfill: BackfillStatus | null;
}

/* ---------------------------------------------------------- panel envelope */

export interface PanelEnvelope {
  _feed: string;
  _status: FeedStatus;
  _present: boolean;
  _paths: string[];
  _notice: string | null;
  /** Build state of the window this panel was reduced over. Absent on panels
   *  that are not window-scoped (the node tier is a snapshot of now). */
  _window?: WindowStatus | null;
  /** No panel payload has arrived at all (the service is starting or
   *  unreachable): a fact about the connection, not about any feed. */
  _waiting?: boolean;
  /** The service sent no envelope for this panel (an answer without the key,
   *  or a request that failed), so the page made one up (lib/panels.ts
   *  `absent`): a fact about the response, not about the feed. */
  _unsent?: boolean;
  /** The service could not reduce this panel: its reducer raised, and this is
   *  what it said (`windows.materialize`). The body holds nothing else, so it
   *  is neither "none" nor a 0: <Panel> says so, and `pick` hands it on as not
   *  present, so no plate reads a number off it. */
  _error?: string | null;
}

/* ---------------------------------------------------------------- window */

/** A requested window, resolved against what the service actually retains. */
export interface WindowSpec {
  minutes: number;
  requested: number | null;
  /** True when `minutes` is NOT what was asked for. Never silently true. */
  clamped: boolean;
  retention_minutes: number;
  label: string;
  note: string | null;
}

/** The users and agent types a view was built for. An agent type is the
 *  collector's own name (`claude_code`, `codex`, ...), and `none` is a process
 *  it did not tag. */
export interface ViewFilterSpec {
  users: string[];
  agent_types: string[];
}
/** What a filtered response says it was built for; `active: false` is everyone. */
export interface ViewFilterBlock extends ViewFilterSpec {
  active: boolean;
  not_narrowed: string[];
}
/** One choice the filter offers, counted from the window's process exits. */
export interface FacetUser {
  user: string;
  events: number;
  by_class: Partial<Record<string, number>>;
}
export interface FacetType {
  agent_type: string;
  events: number;
  by_class: Partial<Record<string, number>>;
}
/** Who and what the UNFILTERED view of a window holds; null until it is built. */
export interface Facets {
  users: FacetUser[];
  /** accounts in the window; only the busiest `users_shown` are listed */
  users_total: number;
  users_shown: number;
  no_user: number;
  agent_types: FacetType[];
}

/** One built (or building) window in the service's registry. */
export interface WindowStatus {
  minutes: number;
  label: string;
  /** The user / agent-type filter this view holds; null or absent is everyone.
   *  A held list can carry a filtered and an unfiltered view of one window. */
  filter?: ViewFilterSpec | null;
  state: 'queued' | 'building' | 'ready' | 'error';
  error: string | null;
  /** A previous reduction for this window is still available and on screen.
   *  Separates a first build (nothing to show) from a drift re-build. */
  has_payload: boolean;
  /** Epoch the current reducers went live -- a build or a resume finishing.
   *  Unlike `built_at`, it moves on a resume. */
  installed_at?: number | null;
  progress: {
    pct: number;
    records: number;
    bytes: number;
    total_bytes: number;
    files: number;
    elapsed_s: number;
    /** `resume`: reading on from a saved build, only what was written since.
     *  `scan`: reducing the whole window from the rows. */
    mode?: 'scan' | 'resume' | 'merge';
  };
  built_at: string | null;
  built_age_s: number | null;
  covers_from: string | null;
  covers_to: string | null;
  /** Seconds of records held BEYOND the window, since the view keeps ingesting. */
  drift_s: number | null;
  drift_budget_s: number;
  live_records: number;
  /** Records the tail read during the build, let go from its full hold and not
   *  read by the build: missing from the view until it is built again. Past
   *  `LET_GO_CAP` let go in one build, each one let go after is counted as it
   *  goes, read by the build or not: the count is then an upper bound. */
  replay_dropped: number;
  /** While a build runs, what the tail read since it began and holds for it:
   *  `held` whole (records and the tail's notices), and the records `let_go`
   *  past `REPLAY_CAP` so far, judged as the build installs. Null otherwise. */
  replay_hold?: { held: number; let_go: number } | null;
  covers_note?: string;
  /** Where these reducers came from. A `snapshot` view was saved by an earlier
   *  build -- `built_at` is that build's -- and caught up from the day-files. */
  cache?: {
    source: 'scan' | 'snapshot' | 'hours' | null;
    /** Why a saved build was or was not used, or why this one was not saved. */
    note: string | null;
    saved_at: string | null;
    /** While the view shows panels an earlier view of its window saved
     *  (after a restart, or for a window the LRU dropped): which ones. Null
     *  once its own build has landed. */
    restored?: string | null;
    /** When this view's own panels were last saved. */
    panels_saved_at?: string | null;
    /** Why saved panels were not shown, or why this view's could not be saved. */
    panels_note?: string | null;
  };
}

export interface WindowsResponse {
  held: WindowStatus[];
  max_windows: number;
  queued: number[];
  queued_filtered?: { minutes: number; users: string[]; agent_types: string[] }[];
  presets: { minutes: number; label: string }[];
  retention_minutes: number;
  default_minutes: number;
  min_minutes: number;
}

/** A quantile summary. `coverage_pct` is part of the number, not a footnote. */
export interface Quant {
  n: number;
  p1: number | null;
  p10: number | null;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  p90: number | null;
  p99: number | null;
  min: number | null;
  max: number | null;
  mean: number | null;
  sampled: boolean;
  sample_n: number | null;
  coverage_pct: number | null;
  /* Set by the backend when a field is PRESENT on every record and exactly 0 on
     every record -- a counter the collector could not populate, reported as 0
     rather than null. Rendering p50=0 would assert a measurement never made. */
  all_zero?: boolean;
  zero_pct?: number | null;
  suspect?: string | null;
}

/** A coverage-carrying accumulator. `n: 0, coverage_pct: 0` means NOT MEASURED. */
export interface Accum {
  total: number | null;
  n: number;
  coverage_pct: number | null;
  mean: number | null;
}

/* ------------------------------------------------------------- tool_mix */

export interface ToolBucketRow {
  b: string;
  calls: number;
  calls_pct: number | null;
  cpu_s: number | null;
  cpu_pct: number | null;
  io_mb?: number | null;
  io_pct?: number | null;
  net_mb?: number | null;
  net_pct?: number | null;
  /** the sum of each process's peak memory, and its share */
  mem_mb?: number | null;
  mem_pct?: number | null;
}
export interface ToolClassBlock {
  calls: number;
  cpu_s: number | null;
  io_mb?: number;
  net_mb?: number;
  mem_mb?: number;
  rows: ToolBucketRow[];
}
/** One agent type's processes alone: its mix and its top tools. */
export interface ToolTypeBlock {
  by: ToolClassBlock;
  tools: ToolCostRow[];
  n_tools: number;
}
/** One tool, with every resource it used, for sorting by any of them. */
export interface ToolCostRow {
  tool: string;
  bucket: string;
  calls: number;
  calls_pct: number | null;
  cpu_s: number;
  cpu_pct: number | null;
  io_mb: number;
  io_pct: number | null;
  net_mb: number;
  net_pct: number | null;
  mem_mean_mb: number | null;
  mem_max_mb: number | null;
}
export interface ToolMix extends PanelEnvelope {
  buckets: string[];
  by_class: Record<string, ToolClassBlock>;
  /** class -> the top tools by every resource, each with all of them */
  tools?: Record<string, ToolCostRow[]>;
  /** distinct tools each class ran (`tools` lists only the busiest) */
  n_tools?: Record<string, number>;
  /** class -> agent type ("" = untagged) -> its share of the class's calls and CPU */
  by_type?: Record<string, Record<string, { calls: number; calls_pct: number | null; cpu_s: number; cpu_pct: number | null }>>;
  /** agent type -> that type's processes alone, so the Agents tab never mixes types */
  by_agent_type?: Record<string, ToolTypeBlock>;
  dedup: { dropped: number; dropped_pct: number | null; rule: string };
  /** how the counted processes ended (reducers/tools.py `exits`); absent on a
   *  window reduced before they were counted */
  exits?: ToolExits;
}

/** How a process ended, past a plain exit 0: a non-zero exit, an exit 1 that
 *  is an answer (grep: no match), a signal other than SIGPIPE, SIGPIPE (its
 *  reader went first, `| head`), or no verdict on the record. */
export type ExitVerdict = 'nonzero' | 'answer' | 'signal' | 'pipe' | 'none';
export interface ExitTally extends Record<ExitVerdict, number> {
  /** processes counted, after the shell dedup: the tool mix's own */
  calls: number;
}
export interface ExitToolRow extends ExitTally {
  tool: string;
  bucket: string;
}
export interface ExitBucketRow extends ExitTally {
  b: string;
}
export interface ExitBlock extends ExitTally {
  /** [exit code, processes] of the non-zero exits, the most first */
  codes: [number, number][];
  /** [signal, processes], SIGPIPE apart */
  signals: [number, number][];
  /** the tools with the most non-zero exits or signals, each with every verdict */
  tools: ExitToolRow[];
  buckets: ExitBucketRow[];
}
export interface ToolExits {
  by_class: Record<string, ExitBlock>;
  by_agent_type: Record<string, ExitBlock>;
  /** programs whose exit 1 is an answer */
  answers: string[];
}

/* ----------------------------------------------------------- io_process */

export interface IoClassBlock {
  events: number;
  events_with_io: number;
  io_coverage_pct: number | null;
  rd_mb: number | null;
  wr_mb: number | null;
  rchar_mb: number | null;
  wchar_mb: number | null;
  cache_served_mb: number | null;
  cache_hit_pct: number | null;
  net_tx_mb: Accum;
  net_rx_mb: Accum;
  scope: Record<string, unknown>;
  quants: Record<string, Quant>;
}
export interface IoByTool {
  tool: string;
  cls?: string;
  events?: number;
  with_io?: number;
  rd_mb?: number | null;
  wr_mb?: number | null;
  rchar_mb?: number | null;
  wchar_mb?: number | null;
  cache_hit_pct?: number | null;
  io_coverage_pct?: number | null;
  [k: string]: unknown;
}
export interface IoByUser {
  user: string;
  cls?: string;
  events?: number;
  with_io?: number;
  rd_mb?: number | null;
  wr_mb?: number | null;
  rchar_mb?: number | null;
  wchar_mb?: number | null;
  net_tx_mb?: number | null;
  net_rx_mb?: number | null;
  io_coverage_pct?: number | null;
  [k: string]: unknown;
}
/** One class's traffic with an endpoint: send and receive calls, and bytes. */
export interface EndpointUse {
  calls: number;
  /** bytes sent */
  tx: number;
  /** bytes received */
  rx: number;
}
/** One remote endpoint: every address that goes by one name. */
export interface EndpointRow {
  name: string;
  /** where the name came from: a reverse-DNS lookup, a command line that named
   *  it, or none (the name is the address) */
  source: 'dns' | 'argv' | 'address';
  /** a client that reached this host: its name, and this host's port */
  inbound: boolean;
  /** outside the private and loopback ranges */
  external: boolean;
  /** the collector's provider bucket for the address, if any */
  provider: string | null;
  /** process records that listed it: exact for one address; for several,
   *  summed over them, so a process that reached two counts twice */
  procs: number;
  /** a name lookup for one of its addresses has not finished */
  pending: boolean;
  /** the busiest few addresses, and how many there are */
  addrs: string[];
  n_addrs: number;
  /** the busiest few `proto/port`s */
  ports: string[];
  by_class: Record<string, EndpointUse>;
}
/** What the endpoint list cannot show, per class (bytes and calls). */
export interface EndpointCover {
  procs: number;
  calls: number;
  tx: number;
  rx: number;
  lost_calls: number;
  lost_tx: number;
  lost_rx: number;
  missing: number;
  missing_bytes: number;
}
export interface EndpointList {
  /** the top `top` by calls and by bytes for every mix of classes, so the
   *  page's class choice can rank its own mix and hold its whole top */
  rows: EndpointRow[];
  top: number;
  classes: string[];
  /** every name and address in the window, listed or not */
  names: number;
  addresses: number;
  /** addresses whose name lookup has not finished */
  pending: number;
  /** reverse-DNS lookups are on */
  rdns: boolean;
  cover: Record<string, EndpointCover>;
}
export interface IoProcess extends PanelEnvelope {
  by_class: Record<string, IoClassBlock>;
  by_tool: IoByTool[];
  by_user: IoByUser[];
  /** the remote ends of the network bytes, named and ranked */
  endpoints?: EndpointList;
}

/* ------------------------------------------------------------ resources */

export interface ResClassBlock {
  events: number;
  cpu_s: number | null;
  sched_run_s: Accum;
  sched_wait_s: Accum;
  quants: Record<string, Quant>;
  /** metric -> the process behind its max */
  top?: Record<string, MaxProcess>;
  /** processes left out of the on-CPU share: run time longer than measured lifetime */
  oncpu_set_aside?: number;
  faults: Record<string, unknown>;
  delays: Record<string, unknown>;
  dstate: Record<string, unknown>;
}
/** The one process behind a distribution's max. */
export interface MaxProcess {
  value: number;
  pid: number | null;
  user: string | null;
  host: string | null;
  ts: string | null;
  tool: string | null;
  args?: string | null;
  agent_type?: string | null;
  actor3?: string | null;
}
/** One tool's D-state stalls: only its records that stalled at all. */
export interface StallByTool {
  tool: string;
  /** seconds in uninterruptible sleep, summed */
  wait_s: number;
  /** records with a stall */
  n: number;
  episodes: number;
  /** the longest single episode, seconds */
  max_s: number;
}
/** Per-bin values, oldest first, aligned with `ResSeries.t`. null is a gap. */
type Bins = (number | null)[];
export interface ResSeries {
  bin_s: number;
  /** epoch seconds at the start of each bin */
  t: number[];
  /** sums by exit time: a process's whole-life counters land in its exit bin */
  by_class: Record<string, Record<'cpu_s' | 'sched_wait_s' | 'maj_flt'
    | 'rchar_mb' | 'rd_mb' | 'wchar_mb' | 'wr_mb', Bins> & Partial<Record<'net_tx_mb' | 'net_rx_mb', Bins>>>;
  /** D-state seconds per bin for the heaviest stallers; 0 where measured and none */
  stall_by_tool: Record<string, Bins>;
  stall_other: Bins;
  /** sampled from residency ticks: per host mean over the bin, summed over hosts */
  resident: Record<string, Record<'rss_mb', Bins>>;
  skipped: { no_ts: number; future: number; clipped_bins: number };
}
export interface Resources extends PanelEnvelope {
  by_class: Record<string, ResClassBlock>;
  stall_by_tool: StallByTool[];
  scope: Record<string, unknown>;
  series?: ResSeries;
  worst_stall?: ResourceInstance[];
  worst_fault?: ResourceInstance[];
  worst_wait?: ResourceInstance[];
  /** waits longer than the process lived, set aside as not measured: class -> field -> n */
  impossible_waits?: Record<string, Record<string, number>>;
}

/* -------------------------------------------------------------- sandbox */

/** Per record: namespace isolation (labels in lib/sandbox.ts; `denied` = exited before read). */
export type SandboxState = 'sandboxed' | 'unsandboxed' | 'denied' | 'unknown';
/** Per session: the outcome of its tool calls (reducers/sandbox.py: session_state). */
export type SessionSandboxState =
  | 'sandboxed'
  | 'partial'
  | 'failed'
  | 'unsandboxed'
  | 'unknown'
  | 'no_calls';
export type ToolCallOutcome = 'isolated' | 'not_isolated' | 'failed' | 'no_payload' | 'unknown';
/** Read off the session root's own argv; only a role-`agent` process has one. */
export type ApprovalState = 'bypassed' | 'auto' | 'supervised' | 'unobservable' | 'na' | 'unknown';

/** Where an agent session is driven from, off its own argv
 *  (`rc_dashboard/normalize.py: SURFACES`). CLI: `terminal`, `script`;
 *  app: `desktop`, `extension`, `web`. */
export type Surface = 'terminal' | 'script' | 'desktop' | 'extension' | 'web' | 'unknown';

export interface SandboxSessionRow {
  host: string;
  pid: number | null;
  /** Which of the processes that held this pid one after another: 0 the first,
   *  null a session whose calls found no root. A row's key with host and pid. */
  gen: number | null;
  user: string | null;
  actor3: string | null;
  agent_type: string | null;
  approval: ApprovalState;
  approval_detail: string | null;
  state: SessionSandboxState;
  calls: Partial<Record<ToolCallOutcome, number>>;
  calls_known: number;
  isolated_pct: number | null;
}
export interface SandboxAttemptRow {
  wrapper: string;
  agent_type: string | null;
  outcome: ToolCallOutcome;
  n: number;
}
export interface LaunchModeRow {
  agent_type: string | null;
  approval: ApprovalState;
  detail: string | null;
  n: number;
}
export interface Sandbox extends PanelEnvelope {
  by_class_events: Record<string, Record<string, number>>;
  approval_by_class_events: Record<string, Record<string, number>>;
  n_events: Record<string, number>;
  by_class_sessions: Record<string, Record<string, number>>;
  approval_by_class_sessions: Record<string, Record<string, number>>;
  n_sessions: Record<string, number>;
  tool_calls: Record<string, Record<string, number>>;
  attempts: SandboxAttemptRow[];
  sessions: SandboxSessionRow[];
  sessions_total: number;
  launch_modes: LaunchModeRow[];
  below_capped?: number;
  states: {
    sandbox: string[];
    sandbox_known?: string[];
    session?: string[];
    session_known?: string[];
    tool_call?: string[];
    tool_call_known?: string[];
    approval: string[];
    approval_known?: string[];
  };
}

/* --------------------------------------------------------- trajectories */

export type TrajRank =
  | 'events'
  | 'cpu_s'
  | 'peak_rss_mb'
  | 'io_mb'
  | 'net_mb'
  | 'distinct_tools'
  | 'recent';
export const TRAJ_RANKS: TrajRank[] = [
  'recent',
  'events',
  'cpu_s',
  'peak_rss_mb',
  'io_mb',
  'net_mb',
  'distinct_tools',
];

/** Which end of a session the inspector lists first, and draws in full
 *  (`ORDERS` in rc_dashboard/proctree.py). */
export type TrajOrder = 'oldest' | 'newest';

/* ---------------------------------------------------------------- phases
 * What an agent was doing, read from the commands it ran
 * (`rc_dashboard/reducers/phase_segment.py`): a handful of labelled stretches,
 * carried by an agent session's row and by the inspector's `row`.
 */

export type PhaseKind =
  | 'explore'
  | 'edit'
  | 'edit_guess'
  | 'build'
  | 'test'
  | 'loop'
  | 'run'
  | 'slurm'
  | 'wait'
  | 'git'
  | 'idle'
  | 'unknown';

/** What a wait waits on, from the command that polls it (`phase_rules.waits_on`). */
export type WaitsOn = 'slurm' | 'process' | 'output' | 'sleep' | 'command';

export interface Phase {
  kind: PhaseKind;
  /** epoch seconds. A phase runs until the next one starts; the last runs until every call has ended */
  t0: number;
  t1: number;
  /** tool calls in it; 0 for an idle gap and for a guess */
  n: number;
  /** `[category, calls]`, the top three: empty unless the dominant category holds under half the calls */
  mix: [string, number][];
  /** masked command previews, by how many calls ran each */
  top: [string, number][];
  /** why the pauses read as editing: on an `edit_guess`, and on any phase that holds guessed `slices`.
   *  A phase is a guess when its `kind` says so, not when this is set */
  guess: string | null;
  /** guessed edits before or inside its calls, `[t0, t1]`. A loop's, but any phase can carry them,
   *  and a run's `t0` can begin at one */
  slices: [number, number][] | null;
  /** runs of the command, in a loop */
  rounds: number | null;
  waits_on: WaitsOn | null;
  /** a wait's pace: the mean gap between its polling command's starts, and how many it started in this phase */
  every_s: number | null;
  times: number | null;
  /** the command that pace is named for: a finished call's preview, or the program run most under a call
   *  still running (R65, the first of its `seen`). Null with no pace */
  every_cmd: string | null;
  /** a call in it is still running */
  open: boolean;
  /** the programs seen under its calls still running, up to five, masked and cut as the census names them;
   *  null when it has none (R65). Most run first, a tie going to the first seen; a login shell's startup
   *  helpers left out, and Claude's binary under its version name read `claude` (leftovers L3). A call
   *  still running is never named by its first child's text */
  seen: string[] | null;
  /** `[preview, seconds]`: a process that outlived its call, or a call that ran past this phase. Never a call
   *  still running (R65) */
  ran_past: [string | null, number][] | null;
  /** where its own calls ended, a call still running at its edge so far; null on a phase that holds none. The
   *  last phase's `t1` is the lane's end, which a call of an earlier phase can hold open (phase_segment, Work).
   *  Absent from a service that predates it */
  own_t1?: number | null;
  /** `[category, seconds]`: how long its calls, or what they left running, went on past the end its work is
   *  weighed to (`t1`; the last phase's `own_t1`), by category, the longest first; the session's summary
   *  weighs that time to the call's own use. Never a call still running. Absent from a service that predates it */
  ran_on?: [string, number][] | null;
  /** an idle gap's: `[preview, since]` for each call from an earlier step that was still running as the gap began,
   *  three at most, the earliest first. The agent had moved past it, and it may have ended inside the gap. The
   *  preview is null for a call still running, which has none of its own (R65). Null on any other phase, and on an
   *  idle one with nothing running. Absent from a block of the first version */
  alive?: [string | null, number][] | null;
  /** the task it belongs to: an index into the block's `tasks`. Absent from a block of the first version */
  task?: number;
  /** the day row that holds it, for an agent over several days (`PhaseBlock.days`); null for one of one day.
   *  Absent from a block of the first version */
  sid?: string | null;
}

/** What the agent ran that the model did not ask for. Counted, never a phase. */
export interface PhaseHousekeeping {
  n: number;
  /** `[shape, commands]`, the top five */
  shapes: [string, number][];
  /** `[minute epoch, commands begun in it]`, in time order */
  bins: [number, number][];
  /** counted in `n` and in no minute: no time, past the bin bound, or under a process that never exited */
  cut: number;
  /** of `n`, the commands under a process that never exited: not known to be the agent's own, so told apart
   *  from the rest. Counted on its own, because `shapes` holds only the top five and may not list their shape */
  never_exited: number;
}

/** Where the agent's own words in a task's label came from (`phase_names.SOURCES`): the title of a pull request it
 *  opened or edited, the subject of a commit, or the title of an entry it wrote in a log or plan file. */
export type TaskSource = 'pr' | 'commit' | 'log';

/** The cutting gap before a task: an hour or more in which the agent waited on nothing it had started. */
export interface TaskGap {
  /** how long, in seconds */
  s: number;
  /** `[preview, since, name]` for each call from an earlier step that was still running as the gap began, three at
   *  most, the earliest first. The agent had moved past it, and it may have ended inside the gap. The preview is null
   *  for a call still running, which has none of its own (R65). The name is what the gap line calls it: the script it
   *  ran, else what it tested, the job it submitted, or the folder it made, as a label shows them, else for a call
   *  still running the programs seen under it (`pgrep and sleep`), else null. Empty when none was running */
  alive: [string | null, number, string | null][];
}

/** A stretch of a session that was about one thing (`rc_dashboard/reducers/phase_tasks.py`), above the fine phases:
 *  named from the agent's own words when it wrote any, else from the names its commands held. */
export interface Task {
  /** `lone`: a few calls hours from other work, set apart. It is no task, and it counts for no cap */
  kind: 'task' | 'lone';
  /** epoch seconds: from its first call's start to where the next task begins. A task before a cutting gap ends
   *  where the agent stopped waiting on its calls, and the session's last where its last call ended */
  t0: number;
  t1: number;
  /** tool calls in it */
  n: number;
  label: string;
  /** where the words in `label` came from. Null for a label built from names, which is no quote */
  source: TaskSource | null;
  /** `#235` for a pull request edited by its number, else '' */
  ref: string;
  /** the names its commands held, at most two, as a label shows them. The service sends them for a quoted task too;
   *  a built label is made of them */
  names: string[];
  /** `[first, end)` in the block's `list`. A block's tasks tile its phases in order, so the idle phase of the
   *  cutting gap after a task is the task's, and the next task's `gap` says it */
  phases: [number, number];
  /** a call in it is still running */
  open: boolean;
  /** the call log was cut, and the calls it left out may belong here: it ends after the cut, or it is the last */
  incomplete: boolean;
  /** the cutting gap before it; null for the first, and where the work moved on with no gap between */
  gap: TaskGap | null;
  /** the day row that holds it, for an agent over several days (`PhaseBlock.days`); null for one of one day */
  sid: string | null;
}

/** One day row of an agent that worked across midnight, as its block lists it. */
export interface PhaseDay {
  sid: string;
  /** the row's first and last record, epoch seconds */
  first: number;
  last: number;
}

export interface PhaseBlock {
  /** 2 for a block that carries its tasks; a block of the first version has no `tasks`, `headline`, or `days` */
  v: number;
  /** whose calls it was read from: an agent's tool calls (`agent`), or, where no agent ran the session, the jobs its
   *  terminals' shells ran (`terminal`: a person's login, an editor's terminal). Absent from a service that read
   *  agents alone, which is `agent` */
  driver?: PhaseDriver;
  /** every call seen, the cut ones included. For an agent over several days, the whole session's */
  n_calls: number;
  /** calls past the log's bound: in `n_calls` and in no phase. The earliest began at `cut_at` */
  cut: number;
  cut_at: number | null;
  housekeeping: PhaseHousekeeping;
  list: Phase[];
  /** the session's tasks, in time order, tiling `list` */
  tasks?: Task[];
  /** the project, and the names that recur across the tasks: `KempnerForge · PRs #229, #233 · video_io.py`. Null
   *  when it has neither */
  headline?: string | null;
  /** for an agent over several day rows: the rows, in time order. Each of them serves this one block, of the whole
   *  session, so that midnight cuts no task or phase; a `sid` on a task or a phase says which row holds it. Absent
   *  for an agent of one day */
  days?: PhaseDay[];
  /** what kind of session it was (`rc_dashboard/reducers/phase_summary.py`). Absent from a service that predates it */
  category?: SessionCategory;
  /** `[use, percent]`: each use's share of the session's work, the most first. The work is its phases' time but idle
   *  gaps, waiting (a wait on Slurm is Slurm work), and commands not recorded, the last phase's to its own calls'
   *  end, and the time a call ran on past its phase toward the call's own use (`ran_on`); where none of it had a
   *  length, its calls. Empty for a session that did no work */
  uses?: [UseKind, number][];
  /** the session in a few sentences, from the block's own numbers: what the work was and its mix, the tool calls and
   *  tasks with the names they held, and how long the agent waited. No model writes it */
  summary?: string;
}

/** What kind of session a block was: the use that holds half its work or more, as the Users tab counts a user's calls
 *  (`UseKind`); `mixed` when none does; `wait` for a session that only waited; `unknown` for one whose commands were
 *  mostly not recorded; `none` for one that made no tool call. */
export type SessionCategory = UseKind | 'mixed' | 'wait' | 'unknown' | 'none';

/** Whose calls a block was read from (`PhaseBlock.driver`). */
export type PhaseDriver = 'agent' | 'terminal';

/** A block's category, the uses it rests on, and its summary, as a portrait's session carries them too: null where no
 *  block was built for it. */
export interface SessionKind {
  category?: SessionCategory | null;
  uses?: [UseKind, number][] | null;
  summary?: string | null;
  /** whose calls it was read from; `agent` where absent */
  driver?: PhaseDriver;
}

/** What a service that read agents alone sent for a row whose calls it did not read: an agent type outside Claude
 *  Code and Codex, a person, an IDE. The service now reads every row, and sends a block for each. */
export interface PhasesUnavailable {
  unavailable: string;
}

/** What the per-minute census says is running at a session's live edge
 *  (`running_now`, rc_dashboard/reducers/live.py). `idle` is a proxy for it:
 *  nothing beyond what has been up since the session began. */
export interface TrajNow {
  state: 'running' | 'idle' | 'unknown';
  /** what is up beyond that, `[command, processes]`, most first */
  cmds?: [string, number][];
  /** the first phase category any of `cmds` has */
  cat?: string;
  /** the census tick it was read from */
  at?: number;
}

/** A session whose agent is running now (`aggregator._running_rows`). */
export interface TrajRunning {
  /** the agent's own newest record */
  seen: number | null;
  /** processes up in its tree, and the memory they hold, at the newest
   *  census; null for an agent inside another program's tree */
  n_procs: number | null;
  rss_mb: number | null;
  census_at: number | null;
  /** what is running at the live edge; absent from a service that predates phases */
  now?: TrajNow;
}

/** How a session's collector keys were attributed to it, most certain first
 * (`rc_dashboard/procindex.py: VIA`). A row reports its least certain key. */
export type OwnerVia = 'self' | 'ancestor' | 'parent' | 'tree_root' | 'root' | 'unknown';

export interface TrajRow {
  sid: string;
  key_type: string;
  /** collector session keys folded into this row; 1 when nothing was folded */
  fragments?: number;
  owner_via?: OwnerVia | null;
  host: string;
  user: string;
  actor3: string | null;
  agent_type: string | null;
  n_events: number;
  cpu_s: number | null;
  distinct_tools: number;
  chain_runs: number;
  wall_s: number | null;
  peak_rss_mb: number | null;
  /** each process's own disk MB, summed: not its reaped children's again */
  io_rd_mb: number | null;
  io_wr_mb: number | null;
  net_tx_mb?: number | null;
  net_rx_mb?: number | null;
  /** present while the session's agent is running */
  running?: TrajRunning;
  sandbox: SandboxState | null;
  approval: ApprovalState | null;
  /** its root's; null for a session that is not an agent's */
  surface?: Surface | null;
  /** the client or the mode, where the argv names it: `Claude desktop app`, `VS Code` */
  surface_detail?: string | null;
  sandbox_ancestry: string | null;
  autonomous: boolean | null;
  tool_rle: [string, number][];
  chain_truncated: boolean;
  purpose_rle: [string, number][];
  /** when each tool run began (its first record's exit time), parallel to
   *  `tool_rle` and capped with it; null where that record had no time */
  tool_t?: (number | null)[];
  /** the same for `purpose_rle` */
  purpose_t?: (number | null)[];
  /** the session's first and last record in the window */
  first?: number | null;
  last?: number | null;
  /** its tool calls, read from the commands its agent ran. Null where its calls are not
   *  recognized: not counted, which is not none. Absent from a service that predates phases.
   *  A row of an agent over several days counts its own day's, and the block's is the session's */
  n_calls?: number | null;
  /** what the agent was doing; `{unavailable}` where its calls are not recognized */
  phases?: PhaseBlock | PhasesUnavailable;
  users?: string[];
  n_users?: number;
  pids?: number[];
  agent_pid?: string | null;
}
export interface TrajSummary {
  /** sessions with an agent process at the root (every session, for other classes) */
  sessions: number;
  /** agent-class keys with no agent at the root: scripts probing `codex --version` */
  probes: number;
  users: number;
  top_user_share_pct: number | null;
  tool_calls: number;
  calls_p50: number | null;
  calls_p90: number | null;
  calls_max: number | null;
  wall_p50_s: number | null;
  wall_p90_s: number | null;
  longest: { sid: string; wall_s: number; user: string | null; agent_type: string | null; tool_calls: number } | null;
  /** share of tool calls that only wait: process-table and scheduler polling */
  wait_pct: number | null;
  /** agent sessions by where each is driven from; null for the other classes */
  by_surface?: Partial<Record<Surface, number>> | null;
}
export interface Trajectories extends PanelEnvelope {
  /** the window the ranks were read over: how long it is, and its ends as the service's local time */
  window: { minutes: number; from: string | null; to: string | null };
  filter?: ViewFilterBlock;
  rank_by: TrajRank;
  rank_meaning: string;
  ranks: Record<string, string[]>;
  rows: Record<string, TrajRow>;
  by_class_counts: Record<string, number>;
  by_key_type: Record<string, number>;
  /** Over every session in the window, per class (not just the listed rows). */
  summary?: Record<string, TrajSummary>;
}

/* ------------------------------------------- one trajectory's process tree
 * `GET /api/trajectory?sid=` (`rc_dashboard/proctree.py`): the session re-read
 * from the day-files and served as a tree. Every bound on that read is in
 * `scan` / `bounds`, and every process or count is in `tree`.
 */

/** How a process got its parent, strongest first. */
export type ProcLink = 'ppid' | 'index' | 'depth' | 'fold';
export const PROC_LINKS: ProcLink[] = ['ppid', 'index', 'depth', 'fold'];

export interface ProcNode {
  k: 'p';
  pid: number | null;
  ppid?: number | null;
  /** null for a parent the read never saw a record of, and the index could not name */
  cmd: string | null;
  /** `cmd › child` when every child runs one command; absent when it is `cmd` */
  label?: string;
  eff?: string;
  /** masked, then cut to 220 characters */
  args?: string;
  /** what a tool-call shell or a sandbox ran, unwrapped from its argv; masked */
  cmdline?: string;
  purpose?: string;
  role?: string;
  start: number | null;
  /** exit time; for a process with no exit (`ev` null), when it was last seen */
  end: number | null;
  /** no start in the read (`start` null): the earliest time of anything
   *  below it, so it started no later than this. A bound, drawn `≤` */
  t0?: number;
  /** no exit read (`ev` null): when its last child started, when later than
   *  `end`, so it was running then. A bound: a child's exit is not one, since
   *  a child keeps naming the parent it outlived */
  t1?: number;
  dur: number | null;
  /** own cpu_s; null is unmeasured */
  cpu: number | null;
  /** processes in this subtree, itself included when it has a record */
  n: number;
  /** own + everything below it; null when none of it was measured */
  sub_cpu: number | null;
  /** processes in the subtree whose cpu_s was null */
  cpu_nm?: number;
  /** its own disk, syscall and network bytes; absent when none moved or none was measured */
  io?: ProcIo;
  /** the same summed over everything below it, itself included; absent when it equals `io` */
  sub_io?: ProcIo;
  /** `exit`, `truncated` (alive when the collector stopped), or null: no record in the read */
  ev: 'exit' | 'truncated' | null;
  link: ProcLink | null;
  /** levels between it and its parent that were never recorded */
  gap?: number;
  /** its record names init: it outlived its parent */
  orphan?: boolean;
  /** a fork that never exec'd, such as a shell's subshell: its command line is its parent's */
  fork?: boolean;
  /** what a person mistyped at a terminal ("hide typos"): a name the shell
   *  could not find, or a line that failed and was typed again, corrected */
  typo?: 'not-found' | 'retried';
  /** the name a not-found helper was handed (`command-not-found -- clera`); masked */
  typed?: string;
  /** failed tries of this line, typed just before it and corrected */
  tries?: number;
  core?: boolean;
  exit_code?: number | null;
  signal?: number | null;
  /** only where it differs from the parent's */
  cwd?: string;
  actor3?: string;
  agent_type?: string;
  user?: string;
  kids?: ProcEntry[];
}

/** Consecutive siblings of one shape: `pgrep ×120`, `pgrep › sleep ×461`. */
export interface ProcGroup {
  k: 'g';
  /** labels of one period of the repeat */
  motif: string[];
  period: number;
  reps: number;
  /** siblings in the run */
  count: number;
  /** processes in them and below them */
  n: number;
  start: number | null;
  end: number | null;
  cpu: number | null;
  cpu_nm?: number | null;
  sub_cpu: number | null;
  /** the members' I/O, and everything below them */
  sub_io?: ProcIo | null;
  dur_max: number | null;
  exits: Record<string, number>;
  signals: Record<string, number>;
  alive: number | null;
  /** members a person mistyped (`ProcNode.typo`) */
  typos?: number | null;
  /** distinct command lines among the members */
  distinct: number;
  samples: { args?: string; cmdline?: string; n: number }[];
  /** the first members, in full */
  items: ProcNode[];
  /** members not itemised: counted in the group's totals */
  more: number;
  /** when the last member started: the undrawn ones started by then */
  last?: number | null;
}

/** Processes counted but not itemised: past the hold budget (`agg`), or past
 *  the served-row budget (`more`). Counted, never dropped. */
export interface ProcCount {
  k: 'agg' | 'more';
  why: 'hold' | 'budget';
  n: number;
  /** for `more`: how many sibling rows it stands for */
  count?: number;
  sub_cpu: number | null;
  cpu_nm: number | null;
  /** what the counted processes moved */
  sub_io?: ProcIo | null;
  start: number | null;
  end: number | null;
  cmds: [string, number][];
}

export type ProcEntry = ProcNode | ProcGroup | ProcCount;

export interface TrajectoryDetail extends PanelEnvelope {
  sid: string;
  /** null: the window is still being reduced; false: not in this window */
  found: boolean | null;
  why?: string;
  cached?: boolean;
  row?: TrajRow;
  host?: string;
  owner_pid?: number | null;
  keys?: number;
  /** the order the tree was served in; absent from a service that predates it */
  order?: TrajOrder;
  window?: WindowSpec;
  span?: { first: number | null; last: number | null };
  scan?: {
    from: number;
    to: number | null;
    files: number;
    bytes_planned: number;
    bytes: number;
    parsed: number;
    skipped: number;
    records: number;
    ticks: number;
    first: number | null;
    last: number | null;
    /** why the read ended before the session did, or null */
    stopped: 'records' | 'bytes' | null;
    stopped_at: number | null;
    elapsed_s: number;
    computed_at: number;
    /** `key search`: only the lines the session's keys hit were parsed */
    method?: string;
  };
  bounds?: {
    max_records: number;
    max_bytes: number;
    hold: number;
    per_parent: number;
    nodes: number;
    itemised: number;
    /** processes counted under a parent instead of itemised */
    counted: number;
    shown_entries: number;
    /** processes only in a `more` row: past the served-row budget */
    elided: number;
    stubs: number;
  };
  links?: Partial<Record<ProcLink, number>>;
  timeline?: { lo: number | null; hi: number | null };
  nav?: TrajNav;
  tree?: ProcNode;
}

/** A process the navigator can jump to: when it started and exited, which. */
export interface NavMark {
  t: number | null;
  end: number | null;
  pid: number | null;
  cmd: string | null;
}

/** The inspector's navigator, from EVERY record the read matched -- itemised
 *  or only counted -- so it is the read's activity, not the served rows'. */
export interface TrajNav {
  /** records with a start time, and those without one */
  starts: number;
  no_start: number;
  /** purpose names; `density.bins` refers to them by index */
  purposes: string[];
  /** process starts per bin, `w` seconds wide from `lo` (on the clock), `n`
   *  bins; sparse `[bin, purpose index, count]`; `before`: started before the
   *  read's axis, not binned */
  density: { lo: number; w: number; n: number; before: number; bins: [number, number, number][] } | null;
  /** the clock minute with most starts */
  busiest: { t: number; n: number } | null;
  /** `via: tool` a tool-call shell, sandbox wrapper or embedded tool;
   *  `first` there was none, so the first process */
  first_tool: (NavMark & { via: 'tool' | 'first' }) | null;
  /** the longest process other than the session root */
  longest: (NavMark & { dur: number | null }) | null;
  /** exits with a non-zero code or a signal; `items` the first `listed` by
   *  start as `[start, end, pid, cmd, exit_code, signal]` */
  failures: {
    n: number;
    listed: number;
    items: [number | null, number | null, number | null, string | null, number | null, number | null][];
  };
}

/* ---------------------------------------------------------- agents (cascades) */

export type CascadeRole = 'agent' | 'infra' | 'client';
export type CascadeVia = 'ancestor' | 'spawned_by' | 'same_user_daemon' | 'tree_root';
/** How an agent's home was found: its own cwd, or (started at `/`) its first call's. */
export type HomeSrc = 'agent' | 'first_call';

export interface CascadeNode {
  pid: number;
  ppid: number | null;
  host: string;
  role: CascadeRole;
  agent_type: string | null;
  user: string | null;
  command: string | null;
  args: string | null;
  cwd: string | null;
  implied: boolean;
  frontend: boolean;
  /** a `claude bg-spare`, a session `claude daemon` started ahead of need: an
   *  agent once claimed; one nobody claimed is infra, and not drawn (`idle_spares`) */
  spare?: boolean;
  parent: number | null;
  via: CascadeVia | null;
  /** commands passed through between the parent node and this one */
  path: string[];
  spawned_by: number | null;
  started: string | null;
  first_seen: string | null;
  last_seen: string | null;
  exited: boolean;
  duration_s: number | null;
  depth: number;
  agent_level: number | null;
  fanout: number;
  tool_calls: number | null;
  cpu_s: number | null;
  session_keys: number | null;
  /** the agent's home: its own cwd, or its first call's when it started at `/` */
  home: string | null;
  home_src: HomeSrc | null;
  /** this agent's own blast radius in brief (no subagents); null: no record of it */
  workspace: { dirs: number; inside_pct: number | null; outside_writes: number | null } | null;
  cwd_vs_parent_agent: 'same' | 'inside' | 'above' | 'outside' | null;
  /** its launch, off its own argv (a Codex app-server's is its front-end's) */
  approval?: ApprovalState | null;
  /** the launch flags as written: `--dangerously-skip-permissions`, `permission-mode=auto` */
  approval_detail?: string | null;
  /** read from the program's path where the path names it */
  version?: string | null;
  /** where it is driven from, off its own argv (a Codex app-server's is its
   *  front-end's); null for a node that is not an agent */
  surface?: Surface | null;
  surface_detail?: string | null;
  /** whether its tool calls ran sandboxed, as the Security tab counts its session; null: it made none */
  sandbox?: SessionSandboxState | null;
  sandbox_calls?: Partial<Record<ToolCallOutcome, number>> | null;
  /** a tree bigger than the service sends: what this node's agents that were
   *  not sent did (the busiest are sent) */
  folded?: { agents: number; ran_nothing: number; one: number; processes: number; outside: number } | null;
}

export interface CascadeTree {
  root: CascadeNode;
  host: string;
  users: string[];
  agent_depth: number;
  max_fanout: number;
  n_nodes: number;
  n_agents: number;
  agents_nested: number;
  agent_types: string[];
  tool_calls: number;
  cpu_s: number;
  nodes: CascadeNode[];
  nodes_truncated: boolean;
  hidden_nodes: number;
  /** of the hidden: `claude bg-spare` sessions its daemon started and nobody claimed */
  idle_spares?: number;
}

/** Where a directory sits relative to the agent's home. */
export type WsKind =
  | 'home'
  | 'own home'
  | 'cache & tooling'
  | 'agent state'
  | "other user's home"
  | 'temp'
  | 'shared storage'
  | 'system'
  | 'filesystem root'
  | 'other';

/** Bytes moved by the processes that RAN in a directory -- measured per process,
 *  credited to where it ran, not to where the bytes went. */
export interface WsIo {
  rchar_mb: number;
  wchar_mb: number;
  rd_mb: number;
  wr_mb: number;
  /** share of those processes whose io block was measured */
  coverage_pct: number | null;
}

/** Distinct paths NAMED on command lines, by what the command does with them (inferred). */
export interface WsOps {
  read: number;
  write: number;
  delete: number;
  named: number;
}

export interface WsDir {
  /** absolute, masked */
  path: string;
  /** `./sub` inside the home, `~/x` / `~bob/x` / absolute outside it */
  shown: string;
  /** null: the agent has no home to be inside of */
  inside: boolean | null;
  root: string | null;
  kind: WsKind;
  /** directories below this one folded into it (depth or quiet siblings) */
  deeper: number;
  /** MEASURED: processes whose cwd is this directory */
  procs: number;
  io: WsIo | null;
  read: number;
  write: number;
  delete: number;
  named: number;
  /** paths past the per-directory cap: counted, not itemised */
  names_capped: number;
  /** [path under this directory, ops as letters r/w/d/n] */
  examples: [string, string][];
  first: number | null;
  last: number | null;
  /** a write or delete named here, outside the home */
  flag: 'write' | 'delete' | null;
}

export interface WsRoot {
  root: string;
  kind: WsKind;
  procs: number;
  dirs: number;
  read: number;
  write: number;
  delete: number;
  named: number;
  wchar_mb: number | null;
}

export interface WsFlag {
  path: string;
  shown: string;
  op: 'write' | 'delete';
  kind: WsKind;
}

export interface WsView {
  summary: {
    procs: number;
    procs_no_cwd: number;
    /** share of these processes whose cwd the collector READ at exec rather than
     *  copied from the parent at fork; null when no record said */
    cwd_read_pct?: number | null;
    procs_inside: number | null;
    procs_outside: number | null;
    dirs: number;
    dirs_capped: number;
    inside_pct: number | null;
    io: WsIo | null;
    wchar_inside_pct: number | null;
    named: WsOps;
    /** null without a home: "outside" has nothing to be outside of */
    outside: WsOps | null;
    outside_writes: number | null;
    unresolved: number;
    roots: WsRoot[];
    first: number | null;
    last: number | null;
    keys: number;
  };
  dirs: WsDir[];
  dirs_more: { rows: number; procs: number };
  flags: WsFlag[];
  flags_more: number;
}

export interface WsAgent {
  host: string;
  pid: number;
  agent_type: string | null;
  user: string | null;
  tree_root: number;
  /** the nearest agent above: this one is its subagent */
  parent_agent: number | null;
  subagents: number[];
  home: string | null;
  home_src: HomeSrc | null;
  home_shown: string | null;
  /** this agent's own processes; null when only its subagents left records */
  self: WsView | null;
  /** with every subagent beneath it; null when it has none with records */
  tree: WsView | null;
}

export interface Workspace {
  agents: WsAgent[];
  summary: {
    agents: number;
    shown: number;
    /** the busiest this many are served, and past it only those that wrote outside */
    agents_cap?: number;
    with_subagents: number;
    with_outside_writes: number;
    /** agents that ran in, or named a path in, another user's home / shared storage */
    other_homes?: number;
    shared_roots?: number;
    unowned_procs: number;
    agents_without_records: number;
    collapse_depth: number;
    rows_cap: number;
  };
  buffer?: Record<string, number>;
}

export interface Cascades extends PanelEnvelope {
  window?: { minutes: number; from: string | null; to: string | null };
  summary?: {
    trees: number;
    /** sent in `trees`, the deepest first, up to reducers.cascades.top_trees */
    trees_sent?: number;
    nested: number;
    hosted: number;
    agents: number;
    agents_nested: number;
    agents_implied: number;
    max_agent_depth: number | null;
    max_fanout: number | null;
    trees_without_agents: number;
    /** `claude bg-spare` sessions the daemon started and nobody claimed: not agents */
    idle_spares?: number;
  };
  depth_hist?: Record<string, number>;
  fanout_hist?: Record<string, number>;
  trees?: CascadeTree[];
  buffer?: { nodes: number; cap: number; dropped: number };
  workspace?: Workspace | null;
}

/* ---------------------------------------------------------------- panels */

export interface PanelsResponse {
  panels: Record<string, PanelEnvelope & Record<string, unknown>>;
  full_built_at: string | null;
  window: WindowSpec;
  window_status: WindowStatus;
  filter?: ViewFilterBlock;
  facets?: Facets | null;
}

/* ------------------------------------------------------------------ live */

/** A rate sample. A class key that is `null` is a GAP -- never plot it as 0. */
export interface RateRow {
  t: string;
  epoch: number;
  agent: number | null;
  human: number | null;
  ide: number | null;
  [k: string]: string | number | null;
}
export interface LiveHost {
  host: string;
  [k: string]: unknown;
}
export interface LiveEvent {
  ts?: string;
  epoch?: number;
  actor3?: string | null;
  agent_type?: string | null;
  user?: string;
  host?: string;
  depth?: number | null;
  comm?: string;
  args?: string;
  tool?: string;
  purpose?: string;
  bucket?: string;
  duration_s?: number | null;
  cpu_s?: number | null;
  exit_code?: number | null;
  signal?: number | null;
  sandbox?: string | null;
  approval?: string | null;
  session_key?: string | null;
  rd_mb?: number | null;
  wr_mb?: number | null;
  net_tx_mb?: number | null;
  net_rx_mb?: number | null;
  [k: string]: unknown;
  pid?: number | null;
  event?: string;
  ppid?: number | null;
}
export interface LiveSubmit {
  ts?: string;
  user?: string;
  agent_type?: string | null;
  tool?: string;
  job_id?: string | number | null;
  partition?: string | null;
  gpus?: number | string | null;
  array?: string | null;
  [k: string]: unknown;
}
/** The startup read of retention from the collector's files, newest first.
 *  It runs on every start -- nothing is restored from a previous run -- so
 *  while it is `running`, bins older than the live window's `retained_from`
 *  have not been read yet: a gap, not a quiet fleet. `retained_from` here is
 *  set once it is `done`. */
export interface BackfillStatus {
  state: 'idle' | 'positioning' | 'running' | 'done' | 'skipped' | 'error';
  pct: number;
  records: number;
  hours: number;
  from?: string | null;
  bytes?: number;
  total_bytes?: number;
  files?: number;
  error?: string;
  elapsed_s?: number;
  retained_from?: string | null;
  retained_hours?: number | null;
}

/* ----------------------------------------------------------------- slurm */

/** One polling loop: polls no more than 3 min apart (rc_dashboard/reducers/slurm.py). */
export interface SlurmLoop {
  from: number;
  to: number;
  polls: number;
  span_s: number;
  every_s: number;
  /** ran for 10 min or more */
  polling: boolean;
}

/** An agent session (folded), or a person's / an IDE's activity as one row per user. */
export interface SlurmSession {
  cls: Cls | string;
  user: string | null;
  host: string | null;
  agent_type: string | null;
  /** `host:owner pid` for an agent session; null for a per-user row */
  session: string | null;
  keys: number;
  fold: Record<string, number> | null;
  first: number | null;
  last: number | null;
  jobs: number;
  outcomes: Partial<Record<'job' | 'dry_run' | 'step' | 'rejected' | 'no_id', number>>;
  rejected: number;
  dry_runs: number;
  tools: Record<string, number>;
  peak_per_min: number;
  /** distinct command lines once numbers are masked */
  shapes: number;
  shapes_capped: boolean;
  flags: Partial<Record<'parsable' | 'dependency' | 'wrap' | 'array', number>>;
  argv_req_pct: number | null;
  partitions: Record<string, number>;
  gpus: number;
  gpu_jobs: number;
  gpu_h: number | null;
  gpu_h_known: number;
  queries: number;
  queries_by: Record<string, number>;
  queries_per_job: number | null;
  polls: number;
  poll_gap_s: number | null;
  loop: SlurmLoop | null;
  queries_unplaced: number;
  /** the longest stretch of checks at most `poll_gap_s` apart (`reducers.slurm.abnormal`) */
  fast_loop?: SlurmStretch | null;
  /** the most queue checks in any 60 s; a lower bound when `queries_unplaced` */
  queries_peak_per_min?: number;
  /** jobs past the service's cap of times per key: `peak_per_min` is then a lower bound */
  jobs_unplaced?: number;
  cancels: number;
  runs: number;
  runs_failed: number;
  fails: { why: string; n: number }[];
  /** the latest job's command line, credentials masked */
  args: string | null;
  /** the rules it broke (`SlurmPanel.abnormal.rules`), in their order; absent from a
   *  service that does not judge them */
  abnormal?: SlurmRuleId[];
}

/** A stretch of polls each at most some gap after the one before. */
export type SlurmStretch = Omit<SlurmLoop, 'polling'>;
/** What makes activity at the scheduler abnormal (reducers/slurm.py, ABNORMAL). */
export type SlurmRuleId = 'fast_polling' | 'query_burst' | 'submit_burst' | 'refused' | 'cancels' | 'failed_runs';
/** A rule and its threshold: a stretch's gap and length for fast polling, a count for the rest. */
export type SlurmRule =
  | { id: 'fast_polling'; gap_s: number; min_s: number }
  | { id: Exclude<SlurmRuleId, 'fast_polling'>; min: number };

export interface SlurmClass {
  sessions: number;
  users: number;
  jobs: number;
  /** submissions that asked for a job: jobs, refusals, and ones with no id read */
  asked: number;
  rejected: number;
  rejected_pct: number | null;
  dry_runs: number;
  steps: number;
  queries: number;
  queries_by: Record<string, number>;
  queries_per_job: number | null;
  polls: number;
  polling_sessions: number;
  polling_h: number;
  cancels: number;
  runs: number;
  runs_failed: number;
  flags_pct: Partial<Record<'parsable' | 'dependency' | 'wrap' | 'array', number>>;
  argv_req_pct: number | null;
  partitions: Record<string, number>;
  gpus: number;
  gpu_jobs: number;
  gpu_h: number | null;
  gpu_h_known: number;
  time_limit_s: Quant | null;
  peak_per_min: number;
  /** sessions that broke a rule, and each rule's, 0 included */
  abnormal_sessions?: number;
  abnormal_by?: Partial<Record<SlurmRuleId, number>>;
}

export interface SlurmPanel extends PanelEnvelope {
  by_class?: Record<string, SlurmClass>;
  /** the busiest `sessions_busiest`, then every other one that broke a rule */
  sessions?: SlurmSession[];
  sessions_total?: number;
  sessions_busiest?: number;
  /** the rules, and the sessions that broke one: `served` of them are in `sessions` */
  abnormal?: { rules: SlurmRule[]; sessions: number; served: number };
  series?: { bin_s: number; t: number[]; by_class: Record<string, { jobs: (number | null)[]; queries: (number | null)[] }> };
  rejections?: { cls: string; tool: string; why: string; n: number; eg: { user: string | null; ts: string | null; args: string } | null }[];
}

/** The present the service computed a frame at (rc_dashboard/clock.py). */
export interface ClockStatus {
  /** pinned to a captured feed's end, and frozen there; false is the wall clock */
  pinned: boolean;
  now: number;
  as_of: string | null;
  source: string;
  behind_wall_s: number;
}

export interface LiveResponse {
  window: {
    minutes: number;
    bin_s: number;
    from: string | null;
    to: string | null;
    label?: string;
    requested_minutes?: number | null;
    clamped?: boolean;
    note?: string | null;
    retention_minutes?: number;
    /** How far the retained bins actually reach back -- younger than the window
     *  after a cold start, which is why an old empty bin is a gap, not a zero. */
    retained_from?: string | null;
    retained_minutes?: number | null;
  };
  rate: RateRow[];
  hosts: LiveHost[];
  events: LiveEvent[];
  events_tail_depth?: number | null;
  submits: LiveSubmit[];
  backfill: BackfillStatus | null;
  users?: LiveUsers;
  clock?: ClockStatus;
}

/** One account: what it ran in the window, and what it has running now. */
/** What a set of exited processes cost: how many, CPU, disk, and network. */
export interface UsageBlock {
  n: number;
  cpu_s: number;
  rd_mb?: number;
  wr_mb?: number;
  /** sent and received, and their sum */
  net_tx_mb?: number;
  net_rx_mb?: number;
  net_mb?: number;
}
export interface LiveUserRow {
  user: string;
  /** processes that exited in the window, every class */
  n: number;
  cpu_s: number;
  rd_mb?: number;
  wr_mb?: number;
  net_tx_mb?: number;
  net_rx_mb?: number;
  net_mb?: number;
  by_class: Partial<Record<string, UsageBlock>>;
  /** class -> agent type -> processes, for records that carry a type */
  types: Partial<Record<string, Record<string, number>>>;
  /** class -> agent type -> what that type's processes cost */
  type_detail?: Partial<Record<string, Record<string, UsageBlock>>>;
  /** class -> agent type -> sessions running now */
  live: Partial<Record<string, Record<string, number>>>;
  /** class -> processes running now, from the live sessions' census */
  running?: Partial<Record<string, number>>;
  /** GPU-hours asked of Slurm in the window (GPUs x time limit); null without Slurm data */
  gpu_h?: number | null;
  last_epoch: number | null;
}
export interface LiveUsers {
  /** class -> agent type -> users with such a session running now */
  running_by_type?: Partial<Record<string, Record<string, number>>>;
  /** the busiest first; the frame carries only the top `rows.length` */
  rows: LiveUserRow[];
  rows_total: number;
  bin_s: number;
}
export interface LiveUserSession {
  host: string;
  agent_type: string | null;
  actor3: string | null;
  age_s: number | null;
  n_procs: number | null;
  threads: number | null;
  rss_mb: number | null;
  cpu_s: number | null;
  d_state: number | null;
  headless: boolean | null;
  approval: string | null;
  sandbox: string | null;
  epoch?: number | null;
  /** the trajectory this session is, today: open it with ?traj= */
  sid?: string | null;
  /** read from the program's path where the path names it */
  version?: string | null;
  /** its root agent's; null when the root is a host (the desktop app's server) */
  surface?: Surface | null;
  surface_detail?: string | null;
  /** the most, and the average, resident memory over every tick seen */
  rss_peak_mb?: number | null;
  rss_mean_mb?: number | null;
  rss_ticks?: number | null;
  /** agents running inside this session's tree, each with its own launch:
   *  a Claude Code a VS Code server started, a Codex a Claude Code started */
  agents?: LiveInnerAgent[];
  /** an agent seen running whose tree sent no census in the last 10 min */
  no_census?: boolean;
  pid?: number | null;
}
export interface LiveInnerAgent {
  pid: number;
  agent_type: string | null;
  approval: string | null;
  version: string | null;
  sandbox: string | null;
  epoch: number | null;
  surface: Surface | null;
  surface_detail: string | null;
}
export interface LiveUserDetail {
  user: string;
  row: LiveUserRow | null;
  sessions: LiveUserSession[];
  /** the account's newest exited processes, however long ago */
  recent: LiveEvent[];
  recent_depth: number;
  /** how many the service holds for this account */
  recent_held?: number;
}

/* ------------------------------------------------------------- portraits
 * How each user works with coding agents (`rc_dashboard/portraits.py`), for the
 * Users tab: `GET /api/users` (every user with an agent session in the window)
 * and `GET /api/user?name=` (one user's portrait). Composed from the window's
 * trajectories, sandbox and Slurm reducers when read, so it can never disagree
 * with those tabs. A session is one agent run: an agent's rows on two days are
 * one session here.
 */

/** A tool call's category (`phase_rules.CATS`), as the call log holds it. */
export type CallCat = 'test' | 'slurm' | 'run' | 'build' | 'edit' | 'git' | 'wait' | 'explore' | 'unknown';

/** A statement about how a user's agents were used, from one rule (`portraits.TRAITS`). */
export interface Trait {
  id: string;
  label: string;
  /** how the page colours it: crit, warn, ok, or '' for plain */
  tone: '' | 'crit' | 'warn' | 'ok';
  /** the numbers that met its rule: `9 of 12 sessions` */
  detail: string;
}
/** One trait as the catalogue states it, with its rule in words. */
export interface TraitRule {
  id: string;
  label: string;
  tone: Trait['tone'];
  rule: string;
}

/** What a user's agent sessions asked of the scheduler, folded as the HPC tab folds them. */
export interface UserSlurm {
  sessions: number;
  jobs: number;
  rejected: number;
  /** submissions that asked for a job: jobs, refusals, and ones with no id read */
  asked: number;
  queries: number;
  queries_per_job: number | null;
  cancels: number;
  parsable: number;
  parsable_pct: number | null;
  polling_sessions: number;
  polling_s: number;
  longest_loop: SlurmLoop | null;
  /** null: no GPU job had its GPUs and time limit on the command line */
  gpu_h: number | null;
  gpu_h_known: number;
}

/** What a user's agents' actions are for (`portraits.USES`): their calls grouped as a reader names a use. */
export type UseKind = 'explore' | 'develop' | 'build' | 'run' | 'hpc' | 'git';

/** How a user leverages their agents (`portraits.leverage`): each part decided by the
 *  service's rules, and null where the numbers do not say it. The page words it. */
export interface Leverage {
  /** the way most of their sessions are driven; `mixed` when none holds half, `unknown` with no launcher known */
  mode: { id: 'terminal' | 'script' | 'extension' | 'app' | 'mixed' | 'unknown'; n: number; of: number };
  /** the freedom the agents start with, of the launches that are known */
  autonomy: { id: 'bypass' | 'auto' | 'no_flag'; bypassed: number; auto: number; of: number } | null;
  /** the most sessions open at once, when that is several */
  parallel: number | null;
  /** what the agents' actions are for, the most first; empty when too few, or none read */
  uses: { id: UseKind; n: number; pct: number }[];
  /** calls that do something: waiting and calls whose command was not recorded left out */
  actions: number;
  /** how many uses from the top hold most of the actions: 1, 2, or 0 for a mix */
  mostly: 0 | 1 | 2;
  /** the calls that only wait, when they are a large share */
  waiting: { n: number; of: number; pct: number } | null;
  project: { name: string; n: number; of: number; mostly: boolean } | null;
  /** the busiest local hour, and the share of active minutes between 00:00 and 06:00 */
  when: { busiest: number; minutes: number; night_pct: number | null } | null;
}

/** One user's row: the roster's, and the head of their portrait. */
export interface UserRow {
  user: string;
  /** agent sessions (agent runs) in the window */
  sessions: number;
  /** of them, running now (the live tier) */
  running: number;
  hosts: string[];
  /** sessions by agent type, the most first ('' = untagged) */
  agent_types: Record<string, number>;
  surfaces: Partial<Record<Surface, number>>;
  approval: Partial<Record<ApprovalState, number>>;
  /** sessions by whether their tool calls ran sandboxed, as the Security tab counts them */
  sandbox: Partial<Record<SessionSandboxState, number>>;
  /** tool calls; null where no session's calls are recognized, which is not none */
  calls: number | null;
  /** sessions whose calls are recognized (Claude Code, Codex) */
  calls_sessions: number;
  calls_p50: number | null;
  /** the calls the logs hold, by category */
  cats: Partial<Record<CallCat, number>>;
  /** distinct commit messages, pull request titles and log entries the agents wrote */
  words: Partial<Record<TaskSource, number>>;
  processes: number;
  cpu_s: number;
  rd_mb: number;
  wr_mb: number;
  net_tx_mb: number;
  net_rx_mb: number;
  peak_rss_mb: number;
  /** each session open from its first record to its last: summed, median, longest */
  wall_s: number | null;
  wall_p50_s: number | null;
  wall_max_s: number | null;
  first: number | null;
  last: number | null;
  /** minutes in which one of the user's agents began a call */
  active_min: number;
  /** those minutes by local hour, 0-23 */
  hours: number[];
  /** the most sessions open at one moment */
  peak_open: number;
  /** `[project, sessions]`, the most first */
  projects: [string, number][];
  projects_total: number;
  slurm: UserSlurm | null;
  traits: Trait[];
  /** how they leverage their agents: the summary's parts */
  leverage: Leverage;
  /** a portrait's: active minutes by weekday (Monday first) and local hour */
  grid?: number[][];
}

/** The window's agent users and sessions, for a portrait to be read against. */
export interface UsersFleet {
  users: number;
  sessions: number;
  sessions_per_user_p50: number | null;
  wall_p50_s: number | null;
  calls_p50: number | null;
  active_min_p50: number | null;
  peak_open_p50: number | null;
  running: number;
  agent_types: Record<string, number>;
  surfaces: Partial<Record<Surface, number>>;
  approval: Partial<Record<ApprovalState, number>>;
  sandbox: Partial<Record<SessionSandboxState, number>>;
  cats: Partial<Record<CallCat, number>>;
  words: Partial<Record<TaskSource, number>>;
  /** users holding each trait */
  traits: Record<string, number>;
}

export interface UsersResponse extends PanelEnvelope {
  window?: { minutes: number; from: string | null; to: string | null };
  filter?: ViewFilterBlock;
  users: UserRow[];
  fleet: UsersFleet | null;
  /** agent sessions whose records named no user and no uid: no one's row, so counted here instead */
  unattributed?: number;
  traits: TraitRule[];
  rules: Record<string, number>;
}

/** One session in a portrait. */
/** One of a user's sessions. Its `category`, `uses`, and `summary` are its phase block's (`SessionKind`), null where no
 *  block was built for it. */
export interface PortraitSession extends SessionKind {
  /** its newest day's row: what the portrait opens it by, as the Agents tab's list does */
  sid: string;
  /** every day row it is */
  sids: string[];
  host: string;
  pid: number;
  agent_type: string | null;
  surface: Surface | null;
  surface_detail: string | null;
  approval: ApprovalState | null;
  sandbox: SessionSandboxState;
  first: number | null;
  last: number | null;
  wall_s: number | null;
  calls: number | null;
  processes: number;
  cpu_s: number;
  rd_mb: number;
  wr_mb: number;
  net_tx_mb: number;
  net_rx_mb: number;
  peak_rss_mb: number;
  running: boolean;
  project: string | null;
  /** its phase block's: null where no block was built for it */
  headline: string | null;
  tasks: number | null;
  quoted: number | null;
  /** seconds in each phase, from its block; null where none was built */
  phase_s: Partial<Record<PhaseKind, number>> | null;
  cats: Partial<Record<CallCat, number>>;
}

/** A task of one of the user's sessions, as its phase block names it (`Task`). */
export interface PortraitTask {
  label: string;
  source: TaskSource | null;
  ref: string;
  names: string[];
  t0: number | null;
  t1: number | null;
  n: number | null;
  open: boolean;
  /** the day row that holds it */
  sid: string;
  agent_type: string | null;
  project: string | null;
}

export interface UserPortrait extends PanelEnvelope {
  user: string;
  /** null while the window is first being read; false: no agent session in it */
  found: boolean | null;
  window?: { minutes: number; from: string | null; to: string | null };
  filter?: ViewFilterBlock;
  /** what `fleet` is: the whole window's users, or (when that view is not held) the filtered selection's */
  fleet_of?: 'window' | 'selection' | null;
  fleet?: UsersFleet | null;
  traits?: TraitRule[];
  rules?: Record<string, number>;
  row?: UserRow;
  phases?: {
    /** seconds in each phase over the sessions whose block was built */
    time_s: Partial<Record<PhaseKind, number>>;
    calls: Partial<Record<PhaseKind, number>>;
    sessions: number;
    /** sessions whose calls are recognized: blocks are built for the newest of them */
    of: number;
  };
  tasks?: {
    n: number;
    lone: number;
    quoted: Partial<Record<TaskSource, number>>;
    recent: PortraitTask[];
    listed_of: number;
  };
  sessions?: PortraitSession[];
  sessions_total?: number;
  /** `[masked command, calls]`, the most first */
  commands?: [string, number][];
  /** the service's time zone, which the hours are in */
  tz?: string;
}

/* -------------------------------------------------------------------- ws */

export type WsFrame =
  | { type: 'live'; payload: LiveResponse }
  | { type: 'feeds'; payload: FeedsResponse }
  | { type: 'rebuilt'; payload: unknown }
  | { type: 'backfill'; payload: unknown }
  | { type: 'windows'; payload: WindowsResponse };

/* ----------------------------------------------------------------- risk ---
 * The three views the retired prototype faked with a hardcoded CAL table.
 * `secrets` rows deliberately carry NO matched value and no argv -- only the
 * pattern that fired, counts, and which tools. See the reducer's `redaction`.
 */
export interface LoginComputeRow {
  tool: string;
  kind: 'compute' | 'transfer' | null;
  n: number;
  cpu_s: number;
  cpu_h: number;
  cpu_pct: number;
  users: number;
  hosts: string[];
  classes: Partial<Record<string, number>>;
}
export interface DangerousRow {
  id: string;
  command: string;
  axis: string;
  severity: 'HIGH' | 'med' | 'low';
  n: number;
  users: number;
  hosts: string[];
  cpu_s: number;
  rated: number;
  succeeded: number;
  works_pct: number | null;
  classes: Partial<Record<string, number>>;
  /** class -> agent type ("" = untagged) -> runs */
  types?: Partial<Record<string, Record<string, number>>>;
  instances?: RiskInstance[];
}
/** Extra per-axis measures the resources panel attaches to an instance. */
export interface ResourceInstance extends RiskInstance {
  dstate_wait_s?: number | null;
  dstate_episodes?: number | null;
  dstate_max_s?: number | null;
  dstate_src?: string | null;
  sched_wait_s?: number | null;
  sched_run_s?: number | null;
  maj_flt?: number | null;
  min_flt?: number | null;
  cmaj_flt?: number | null;
  nvcsw?: number | null;
  nivcsw?: number | null;
}
export interface RiskInstance {
  pid: number | null;
  ppid: number | null;
  user: string | null;
  actor3: string | null;
  agent_type: string | null;
  host: string | null;
  ts: string | null;
  tool: string | null;
  session_key: string | null;
  cpu_s: number | null;
  duration_s: number | null;
  exit_code: number | null;
  signal: number | null;
  /* absent by design on a secrets instance: the argv is the thing not to echo */
  args?: string;
}
export interface SecretRow {
  id: string;
  pattern: string;
  severity: 'HIGH' | 'med' | 'low';
  n: number;
  users: number;
  tools: string[];
  classes: Partial<Record<string, number>>;
  instances?: RiskInstance[];
  types?: Partial<Record<string, Record<string, number>>>;
}
/** One risk rule as counted with (rc_dashboard/reducers/risk.py `RuleSet.summary`). */
export interface RiskRule {
  id: string;
  label: string;
  severity: string;
  axis: string | null;
  pattern_resolved: string;
  source: 'built-in' | 'changed' | 'added';
  section?: string | null;
}
/** One section of the Risks tab, in the cluster's order (`risk.sections`). */
export interface RiskSection {
  id: string;
  kind: 'login_compute' | 'commands' | 'credentials';
  title: string;
  /** the rule ids it shows; empty for login_compute */
  rules: string[];
}
export interface RiskRules {
  profile: string | null;
  sections?: RiskSection[];
  unsectioned?: { kind: string; id: string; label: string }[];
  vars: Record<string, string>;
  dangerous: RiskRule[];
  secrets: RiskRule[];
  login_compute: { compute: string[]; transfer: string[] };
  disabled: { kind: string; id: string; label: string }[];
  changed: { kind: string; id: string; fields: string[] }[];
  added: { kind: string; id: string; label: string }[];
  invalid: { kind: string; id: string | null; error: string }[];
  unknown: { kind: string; id: string }[];
}

export interface RiskPanel extends PanelEnvelope {
  rules?: RiskRules;
  sections?: RiskSection[];
  login_compute?: {
    rows: LoginComputeRow[];
    top_procs?: RiskInstance[];
    compute_cpu_s: number;
    compute_cpu_h: number;
    share_of_all_cpu_pct: number;
    by_class: Partial<Record<string, { n: number; cpu_s: number; cpu_h: number; users: number }>>;
  };
  dangerous?: {
    rows: DangerousRow[];
    axes?: Record<string, string>;
    site_assumption?: string;
  };
  secrets?: {
    rows: SecretRow[];
    args_scanned: number;
    args_truncated: number;
    truncation_pct: number;
    redaction?: string;
    undercount?: string;
  };
  totals?: { events: number; cpu_s: number };
}

/* ------------------------------------------------- every command behind I/O
 * `/api/io/commands` (rc_dashboard/iocmds.py): a lookup over the raw records,
 * started with an I/O number's filters and polled until it has read them. */

/** What a lookup's commands can be sorted by. */
export type IoSort =
  | 'cmd' | 'tool' | 'n' | 'rd_mb' | 'wr_mb' | 'rchar_mb' | 'wchar_mb' | 'tx_mb' | 'rx_mb'
  | 'cpu_s' | 'last'
  /** read and written, or sent and received, together */
  | 'disk_mb' | 'net_mb';

/** One distinct command line and what every process that ran it moved. */
export interface IoCmdRow {
  /** masked, cut at 2000 characters (`cut`) */
  cmd: string;
  cut: boolean;
  tool: string | null;
  /** processes that ran it */
  n: number;
  /** of those, the ones with an io block */
  io_n: number;
  rd_mb: number;
  wr_mb: number;
  rchar_mb: number;
  wchar_mb: number;
  tx_mb: number;
  rx_mb: number;
  cpu_s: number;
  users: string[];
  first: string | null;
  last: string | null;
}

export interface IoCmdTotals {
  n: number;
  io_n: number;
  rd_mb: number;
  wr_mb: number;
  rchar_mb: number;
  wchar_mb: number;
  tx_mb: number;
  rx_mb: number;
  cpu_s: number;
}

export interface IoCmdJob {
  id: string;
  state: 'queued' | 'reading' | 'done' | 'error';
  error: string | null;
  query: Record<string, unknown>;
  from: string | null;
  to: string | null;
  bytes: number;
  total_bytes: number;
  scanned: number;
  matched: number;
  elapsed_s: number;
  totals?: IoCmdTotals;
  /** distinct command lines */
  commands?: number;
  /** past the service's hold: counted in `totals`, not listed */
  commands_over?: number;
  rows?: IoCmdRow[];
  offset?: number;
  limit?: number;
  sort?: IoSort;
  dir?: 'asc' | 'desc';
}

/** A process's I/O in the trajectory tree: its own, or summed over a subtree. */
export interface ProcIo {
  rd_mb: number;
  wr_mb: number;
  rchar_mb: number;
  wchar_mb: number;
  tx_mb: number;
  rx_mb: number;
}

/* ------------------------------------------------------------ node tier */

/** A process in D state when the node tier looked, and what it waited in. */
export interface NodeDState {
  pid: number | null;
  ppid: number | null;
  user: string | null;
  /** the kernel wait channel; `-` for another user's process when unprivileged */
  wchan: string | null;
  /** the kernel stack's top frames, where the collector could read it (root) */
  kstack: string[] | null;
  args: string;
}
/** One account's cgroup between the node tier's two newest snapshots. */
export interface NodeUser {
  user: string;
  uid: number | null;
  cpu_delta_s: number | null;
  gap_s: number | null;
  /** the cgroup was recreated between the snapshots: no rate, not an idle one */
  counter_reset: boolean;
  /** CPU over the gap as cores in use on average */
  cpu_cores: number | null;
  cpu_quota_cores: number | null;
  /** seconds the kernel held the account back at its quota, over the gap */
  throttled_delta_s: number | null;
  mem_gb: number | null;
  pids: number | null;
  pids_max: number | null;
}
export interface NodeHost {
  host: string;
  ts: string | null;
  epoch: number | null;
  synthetic: boolean;
  privileged: boolean | null;
  load_1: number | null;
  load_5: number | null;
  load_15: number | null;
  mem_total_gb: number | null;
  mem_used_gb: number | null;
  mem_used_pct: number | null;
  mem_available_gb: number | null;
  cpu_idle_pct: number | null;
  cpu_iowait_pct: number | null;
  procs_running: number | null;
  procs_blocked: number | null;
  d_state_procs: number;
  d_state?: NodeDState[];
  zombie: number | null;
  fork_exec_ms: number | null;
  stat_local_ms: number | null;
  getent_ms: number | null;
  logged_in_users: number;
  fds_allocated: number | null;
  snapshots: number;
  per_user: NodeUser[];
  per_user_resets: number;
  /** accounts held back at their CPU quota; null without a second snapshot */
  throttled_users?: number | null;
}
export interface NodeMount {
  host: string;
  mount: string;
  fstype: string | null;
  bad_xid: number | null;
  backlog_u: number | null;
  rd_ops_total: number | null;
  wr_ops_total: number | null;
  ops_s: number | null;
  rd_kb_s: number | null;
  rd_rtt_ms: number | null;
  rd_exe_ms: number | null;
  rd_retrans: number | null;
  wr_kb_s: number | null;
  wr_rtt_ms: number | null;
  wr_exe_ms: number | null;
  wr_retrans: number | null;
}
export interface NodeDisk {
  host: string;
  mountpoint: string;
  fstype: string | null;
  total_gb: number | null;
  used_pct: number | null;
  inode_used_pct: number | null;
  /** at or past the service's line for space or inodes (`thresholds`) */
  full?: boolean;
}
export type NodeField = 'fork_exec_ms' | 'getent_ms' | 'load_1' | 'nfs_rd_rtt_ms';
/** The node tier over the window, per host (rc_dashboard/nodehist.py). */
export interface NodeSeries {
  bin_s: number;
  /** epoch seconds at the start of each bin */
  t: number[];
  /** the usual gap between a host's snapshots */
  interval_s: number | null;
  /** how a bin of snapshots is read: a latency's worst, a level's mean */
  fields: Record<NodeField, 'max' | 'mean'>;
  hosts: Record<string, Record<NodeField | 'snapshots', (number | null)[]>>;
  /** the thread reading the node tier's files: still on its first pass, or done */
  state: 'idle' | 'reading' | 'ready';
  passes: number;
  error: string | null;
  read: { files: number; lines: number; bad: number; bytes: number };
  held_from: number | null;
  days: number;
  _error?: string;
}
export interface NodePanel extends PanelEnvelope {
  hosts?: NodeHost[];
  nfs?: NodeMount[];
  disk?: NodeDisk[];
  thresholds?: { disk_full_pct: number; inode_full_pct: number };
  series?: NodeSeries;
  privilege?: string;
  _error?: string;
}

/* ---------------------------------------------------------------- trend */

/** One class's sums over a bucket of the roll-up table (rollup.py). `cpu_n`:
 *  the processes that reported their CPU (a dropped BPF block writes null). */
export interface TrendClass {
  n: number;
  cpu_s: number;
  cpu_n: number;
  submits: number;
  /** distinct accounts over the whole bucket, never a sum of days */
  users: number;
}
/** A day, week or month of the roll-up table. With no `data_days` its maps are
 *  empty and `users` null: no record was held, which is not a quiet stretch. */
export interface TrendBucket {
  key: string;
  from: string;
  to: string;
  /** local midnight starting the bucket, and the one ending it (epoch s) */
  t: number;
  t_end: number;
  /** the range asked for starts inside it */
  cut: boolean;
  today: boolean;
  partial: boolean;
  days: number;
  data_days: number;
  complete_days: number;
  minutes: number;
  held: number;
  records: number;
  users: number | null;
  by_class: Record<string, TrendClass>;
  types: Record<string, { n: number; cpu_s: number; users: number }>;
  hosts: Record<string, { n: number; cpu_s: number }>;
}
/** `/api/trend`: the roll-up table over up to a year (rollup.py). */
export interface TrendResponse {
  enabled: boolean;
  buckets: TrendBucket[];
  by: 'day' | 'week' | 'month';
  days: number;
  from?: string;
  to?: string;
  tz?: string;
  basis?: string;
  note?: string;
  path?: string;
  grace_s?: number;
  state?: 'ok' | 'reading' | 'error';
  error?: string | null;
  covered_from?: number | null;
  flushed_at?: number | null;
  late_dropped?: number;
  table?: { days: number; oldest: string | null; newest: string | null; complete: number; written_at: number | null };
}
