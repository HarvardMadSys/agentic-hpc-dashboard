/* Types for the rc_dashboard HTTP/WS contract.
 *
 * Every panel payload is `PanelEnvelope & <shape>`: the envelope is what makes
 * "zero is not missing" renderable, so it is never optional. Numeric leaves are
 * `number | null` wherever the backend can legitimately not know the value --
 * `null` is absent, `0` is measured.
 */

/* --------------------------------------------------------------- classes */

/** The three physical classes. NEVER fold vscode into agent.
 *  `unlabeled` is a residual bin rendered alongside them, never merged into them. */
export type Cls = 'agent' | 'human-vscode' | 'human';

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

/** One built (or building) window in the service's registry. */
export interface WindowStatus {
  minutes: number;
  label: string;
  state: 'queued' | 'building' | 'ready' | 'error';
  error: string | null;
  /** A previous reduction for this window is still available and on screen.
   *  Separates a first build (nothing to show) from a drift re-build. */
  has_payload: boolean;
  progress: {
    pct: number;
    records: number;
    bytes: number;
    total_bytes: number;
    files: number;
    elapsed_s: number;
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
}

export interface WindowsResponse {
  held: WindowStatus[];
  max_windows: number;
  queued: number[];
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
  shell_kinds: Record<string, Record<string, number>>;
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
  rd_mb?: number | null;
  wr_mb?: number | null;
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
  faults: Record<string, unknown>;
  delays: Record<string, unknown>;
  dstate: Record<string, unknown>;
}
export interface StallByTool {
  tool: string;
  cls?: string;
  events?: number;
  sched_wait_s?: number | null;
  sched_run_s?: number | null;
  schedstat_ratio?: number | null;
  dstate_wait_s?: number | null;
  coverage_pct?: number | null;
  [k: string]: unknown;
}
export interface Resources extends PanelEnvelope {
  by_class: Record<string, ResClassBlock>;
  stall_by_tool: StallByTool[];
  scope: Record<string, unknown>;
  cpu_basis: string;
  worst_stall?: ResourceInstance[];
  worst_fault?: ResourceInstance[];
  worst_wait?: ResourceInstance[];
}

/* -------------------------------------------------------------- sandbox */

export type SandboxState = 'sandboxed' | 'unsandboxed' | 'denied' | 'unknown';
export type ApprovalState = 'supervised' | 'bypassed' | 'unknown';

export interface AncestryRow {
  ancestry: string;
  sandbox: SandboxState;
  n: number;
  dstate_wait_s: number | null;
  dstate_coverage_pct: number | null;
  reading: string;
}
export interface Sandbox extends PanelEnvelope {
  by_class_events: Record<string, Record<string, number>>;
  approval_by_class_events: Record<string, Record<string, number>>;
  by_class_trees: Record<string, Record<string, number>>;
  approval_by_class_trees: Record<string, Record<string, number>>;
  ancestry_matrix: AncestryRow[];
  sandbox_src: Record<string, number>;
  sandbox_detail: Record<string, unknown>;
  agent_only: Record<string, unknown>;
  states: { sandbox: string[]; approval: string[] };
  notes: Record<string, string>;
}

/* --------------------------------------------------------- trajectories */

export type TrajRank = 'events' | 'cpu_s' | 'distinct_tools' | 'chain_runs';
export const TRAJ_RANKS: TrajRank[] = ['events', 'cpu_s', 'distinct_tools', 'chain_runs'];

export interface TrajRow {
  sid: string;
  key_type: string;
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
  users?: string[];
  n_users?: number;
  pids?: number[];
  agent_pid?: string | null;
}
export interface Trajectories extends PanelEnvelope {
  window: unknown;
  rank_by: TrajRank;
  rank_meaning: string;
  ranks: Record<string, string[]>;
  rows: Record<string, TrajRow>;
  by_class_counts: Record<string, number>;
  by_key_type: Record<string, number>;
  dominant_user_share_pct?: Partial<Record<TrajRank, number | null>> | null;
  buffer: unknown;
  basis: string;
}

/* ---------------------------------------------------------------- panels */

export interface PanelsResponse {
  panels: Record<string, PanelEnvelope & Record<string, unknown>>;
  full_built_at: string | null;
  node_built_at: string | null;
  window: WindowSpec;
  window_status: WindowStatus;
  purposes: string[] | Record<string, string> | null;
}

/* ------------------------------------------------------------------ live */

/** A rate sample. A class key that is `null` is a GAP -- never plot it as 0. */
export interface RateRow {
  t: string;
  epoch: number;
  agent: number | null;
  'human-vscode': number | null;
  human: number | null;
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
  backfill: Record<string, unknown> | null;
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
}
export interface RiskPanel extends PanelEnvelope {
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
