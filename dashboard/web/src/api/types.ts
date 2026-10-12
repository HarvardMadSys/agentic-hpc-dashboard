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

/* ----------------------------------------------------------------- feeds */

export type FeedStatus = 'ok' | 'stale' | 'empty' | 'missing' | 'degraded';

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
}
export type FeedsResponse = Record<string, FeedReport>;

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
  replay_dropped: number;
  covers_note?: string;
  /** Where these reducers came from. A `snapshot` view was saved by an earlier
   *  build -- `built_at` is that build's -- and caught up from the day-files. */
  cache?: {
    source: 'scan' | 'snapshot' | 'hours' | null;
    /** Why a saved build was or was not used, or why this one was not saved. */
    note: string | null;
    saved_at: string | null;
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
  p10: number | null;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  p90: number | null;
  p99: number | null;
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
}
export interface ToolClassBlock {
  calls: number;
  cpu_s: number | null;
  rows: ToolBucketRow[];
}
export interface ToolHeadRow {
  tool: string;
  calls: number;
  calls_pct: number | null;
  cpu_pct: number | null;
}
export interface ToolMix extends PanelEnvelope {
  buckets: string[];
  by_class: Record<string, ToolClassBlock>;
  head: Record<string, ToolHeadRow[]>;
  /** distinct tools each class ran (the head lists only the top few) */
  n_tools?: Record<string, number>;
  /** class -> agent type ("" = untagged) -> its share of the class's calls and CPU */
  by_type?: Record<string, Record<string, { calls: number; calls_pct: number | null; cpu_s: number; cpu_pct: number | null }>>;
  dedup: { dropped: number; dropped_pct: number | null; rule: string };
  cpu_basis: string;
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
  net_calls: Accum | null;
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
export interface IoProcess extends PanelEnvelope {
  by_class: Record<string, IoClassBlock>;
  by_tool: IoByTool[];
  by_user: IoByUser[];
  notes: Record<string, string>;
}

/* ------------------------------------------------------------ resources */

export interface ResClassBlock {
  events: number;
  cpu_s: number | null;
  sched_run_s: Accum;
  sched_wait_s: Accum;
  schedstat_ratio: number | null;
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
  by_class: Record<string, Record<'n' | 'cpu_s' | 'sched_wait_s' | 'dstate_wait_s' | 'maj_flt'
    | 'rchar_mb' | 'rd_mb' | 'wchar_mb' | 'wr_mb', Bins>>;
  /** D-state seconds per bin for the heaviest stallers; 0 where measured and none */
  stall_by_tool: Record<string, Bins>;
  stall_other: Bins;
  /** sampled from residency ticks: per host mean over the bin, summed over hosts */
  resident: Record<string, Record<'rss_mb' | 'n_procs' | 'd_state', Bins>>;
  resident_hosts: (number | null)[];
  skipped: { no_ts: number; future: number; clipped_bins: number };
}
export interface Resources extends PanelEnvelope {
  by_class: Record<string, ResClassBlock>;
  stall_by_tool: StallByTool[];
  scope: Record<string, unknown>;
  cpu_basis: string;
  series?: ResSeries;
  worst_stall?: ResourceInstance[];
  worst_fault?: ResourceInstance[];
  worst_wait?: ResourceInstance[];
  /** waits longer than the process lived, set aside as not measured: class -> field -> n */
  impossible_waits?: Record<string, Record<string, number>>;
  impossible_basis?: string;
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

export interface AncestryRow {
  ancestry: string;
  /** `wrapper` (bwrap / codex-linux-sandbox, read before it unshares) or `process` */
  kind: 'wrapper' | 'process';
  sandbox: SandboxState;
  n: number;
  dstate_wait_s: number | null;
  dstate_coverage_pct: number | null;
  reading: string | null;
}
export interface SandboxSessionRow {
  host: string;
  pid: number | null;
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
  ancestry_matrix: AncestryRow[];
  sandbox_src: Record<string, number>;
  sandbox_detail: Record<string, unknown>;
  agent_only: Record<string, unknown>;
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
  notes: Record<string, string>;
}

/* --------------------------------------------------------- trajectories */

export type TrajRank = 'events' | 'cpu_s' | 'distinct_tools' | 'chain_runs';
export const TRAJ_RANKS: TrajRank[] = ['events', 'cpu_s', 'distinct_tools', 'chain_runs'];

/** How a session's collector keys were attributed to it, most certain first
 * (`rc_dashboard/procindex.py: VIA`). A row reports its least certain key. */
export type OwnerVia = 'self' | 'ancestor' | 'parent' | 'tree_root' | 'root' | 'unknown';
export const OWNER_VIA: OwnerVia[] = ['self', 'ancestor', 'parent', 'tree_root', 'root', 'unknown'];

export interface TrajRow {
  sid: string;
  key_type: string;
  /** collector session keys folded into this row; 1 when nothing was folded */
  fragments?: number;
  owner_via?: OwnerVia | null;
  drift?: Drift;
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
  io_rd_mb: number | null;
  io_wr_mb: number | null;
  sandbox: SandboxState | null;
  approval: ApprovalState | null;
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
}
export interface Trajectories extends PanelEnvelope {
  window: unknown;
  filter?: ViewFilterBlock;
  rank_by: TrajRank;
  rank_meaning: string;
  ranks: Record<string, string[]>;
  rows: Record<string, TrajRow>;
  by_class_counts: Record<string, number>;
  by_key_type: Record<string, number>;
  fold?: {
    raw_sessions: number;
    sessions: number;
    by_via: Partial<Record<OwnerVia, number>>;
    index: { entries: number; cap: number; evicted: number } | null;
  };
  dominant_user_share_pct?: Partial<Record<TrajRank, number | null>> | null;
  /** Over every session in the window, per class (not just the listed rows). */
  summary?: Record<string, TrajSummary>;
  buffer: unknown;
  basis: string;
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
  dur: number | null;
  /** own cpu_s; null is unmeasured */
  cpu: number | null;
  /** processes in this subtree, itself included when it has a record */
  n: number;
  /** own + everything below it; null when none of it was measured */
  sub_cpu: number | null;
  /** processes in the subtree whose cpu_s was null */
  cpu_nm?: number;
  /** `exit`, `truncated` (alive when the collector stopped), or null: no record in the read */
  ev: 'exit' | 'truncated' | null;
  link: ProcLink | null;
  /** levels between it and its parent that were never recorded */
  gap?: number;
  /** its record names init: it outlived its parent */
  orphan?: boolean;
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
  dur_max: number | null;
  exits: Record<string, number>;
  signals: Record<string, number>;
  alive: number | null;
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
  basis?: Record<string, string>;
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

/** Two process-level proxies for an agent leaving its task, carried by
 *  trajectory rows (`rc_dashboard/reducers/trajectories.py: drift`). Neither
 *  sees the context. The Agents page does not use them: it shows where an
 *  agent worked instead (`Workspace`). */
export interface Drift {
  n: number;
  capped: boolean;
  min_n: number;
  /** Jensen-Shannon divergence, first vs last third of the purpose mix; null below min_n */
  mix_jsd: number | null;
  mix_first: [string, number][] | null;
  mix_last: [string, number][] | null;
  cwd_coverage_pct: number | null;
  cwd_anchor: string | null;
  cwd_anchor_src: 'agent' | 'first_call' | null;
  cwd_outside_pct: number | null;
  cwd_distinct: number;
  cwd_top: [string, number][];
  cwd_first: string | null;
  cwd_last: string | null;
}

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
  basis?: Record<string, string>;
}

export interface Cascades extends PanelEnvelope {
  window?: { minutes: number; from: string | null; to: string | null };
  summary?: {
    trees: number;
    nested: number;
    hosted: number;
    agents: number;
    agents_nested: number;
    agents_implied: number;
    max_agent_depth: number | null;
    max_fanout: number | null;
    trees_without_agents: number;
  };
  depth_hist?: Record<string, number>;
  fanout_hist?: Record<string, number>;
  edges?: { parent: string; child: string; n: number }[];
  edge_via?: Partial<Record<CascadeVia, number>>;
  trees?: CascadeTree[];
  role_census?: { agent_type: string | null; role: string; n: number; cpu_s: number; pct: number }[];
  buffer?: { nodes: number; cap: number; dropped: number };
  workspace?: Workspace | null;
  basis?: Record<string, unknown>;
}

/* ---------------------------------------------------------------- panels */

export interface PanelsResponse {
  panels: Record<string, PanelEnvelope & Record<string, unknown>>;
  full_built_at: string | null;
  node_built_at: string | null;
  window: WindowSpec;
  window_status: WindowStatus;
  filter?: ViewFilterBlock;
  facets?: Facets | null;
  purposes: string[] | Record<string, string> | null;
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
export interface LiveNow {
  [k: string]: number | string | null | undefined;
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
  state: 'idle' | 'running' | 'done' | 'skipped' | 'error';
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
  cancels: number;
  runs: number;
  runs_failed: number;
  fails: { why: string; n: number }[];
  /** the latest job's command line, credentials masked */
  args: string | null;
}

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
}

export interface SlurmPanel extends PanelEnvelope {
  by_class?: Record<string, SlurmClass>;
  sessions?: SlurmSession[];
  sessions_total?: number;
  series?: { bin_s: number; t: number[]; by_class: Record<string, { jobs: (number | null)[]; queries: (number | null)[] }> };
  rejections?: { cls: string; tool: string; why: string; n: number; eg: { user: string | null; ts: string | null; args: string } | null }[];
  basis?: Record<string, unknown>;
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
  now: LiveNow;
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
  net_mb?: number;
}
export interface LiveUserRow {
  user: string;
  /** processes that exited in the window, every class */
  n: number;
  cpu_s: number;
  rd_mb?: number;
  wr_mb?: number;
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
  /** distinct accounts: a person in three classes is one */
  total: number;
  by_class: Partial<Record<string, number>>;
  with_live_agent: number;
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
  /** the most, and the average, resident memory over every tick seen */
  rss_peak_mb?: number | null;
  rss_mean_mb?: number | null;
  rss_ticks?: number | null;
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

/* ---------------------------------------------------------------- events */

export interface EventsResponse {
  rows: LiveEvent[];
  next_cursor: number | string | null;
  total_scanned: number | null;
  truncated: boolean;
  order?: string;
  cursor?: number;
  limit?: number;
  event_filter_defaulted?: boolean;
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
/** One section of the Risk tab, in the cluster's order (`risk.sections`). */
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
    basis?: string;
  };
  dangerous?: {
    rows: DangerousRow[];
    axes?: Record<string, string>;
    site_assumption?: string;
    basis?: string;
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

/* ------------------------------------------------------------------ ask */

/** The agent behind "Chat with LLM", and whether it can run on this host. */
export interface AskInfo {
  enabled: boolean;
  agent: string;
  model: string | null;
  command: string;
  available: boolean;
  error: string | null;
  timeout_s: number;
}
/** One question, as the service runs it. */
export interface AskJob {
  id: string;
  state: 'queued' | 'running' | 'done' | 'error';
  question: string;
  window: string;
  agent: string;
  model: string | null;
  progress: string | null;
  answer: string | null;
  error: string | null;
  elapsed_s: number;
  snapshot_kb: number;
}
