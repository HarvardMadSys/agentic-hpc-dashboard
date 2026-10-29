/* Regenerate `mock/api.json`.
 *
 *   node mock/gen_fixture.mjs
 *
 * This is a DEVELOPMENT fixture and it lives outside `src/` on purpose: nothing
 * it produces can reach the production bundle, and `src/` stays free of any
 * synthesised value (there is no Math.random() in this repo's `src/`, and this
 * generator has none either -- every series below is a closed-form function of
 * its index, so the fixture is byte-stable across runs).
 *
 * The numbers are shaped after the magnitudes in `findings/`, and the fixture
 * deliberately includes the awkward states the UI has to get right:
 *   - a feed that is `missing`, and a panel with `_present: false`
 *   - a feed whose lag_s exceeds its own max_age_s (stale)
 *   - rate bins that are `null` (gaps), including one long outage
 *   - accumulators with `n: 0, coverage_pct: 0` ("not measured", never 0)
 *   - an `exited before read` (key `denied`) slice, distinct from `unsandboxed`,
 *     and a session whose every sandbox attempt failed
 *   - a trajectory list dominated by one user
 */
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/* ------------------------------------------------------------- shaping */

const BINS = 1440; // 24 h at one bin per minute
const HOSTS = ['holylogin01', 'holylogin02', 'holylogin03', 'boslogin01', 'boslogin02', 'boslogin03'];
const CLASSES = ['agent', 'human', 'ide'];

/* diurnal weight: quiet at 05h, peak at 15h local */
const diurnal = (min) => {
  const h = (min / 60) % 24;
  return 0.45 + 0.55 * Math.max(0, Math.sin(((h - 5) / 24) * 2 * Math.PI));
};

/* a deterministic, bounded wobble -- a hash of the index, not a random draw */
const wob = (i, salt) => {
  const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453;
  return x - Math.floor(x); // [0,1)
};

const BASE = { agent: 210, 'ide': 96, human: 34 };

/* Two deliberate holes: a 23-minute collector outage, and scattered single
 * bins where no record landed at all. */
const OUTAGE = [612, 635];
const isGap = (i) => (i >= OUTAGE[0] && i < OUTAGE[1]) || i % 197 === 0;

function rateRows(nowEpoch) {
  const startBin = Math.floor(nowEpoch / 60) - BINS + 1;
  const rows = [];
  for (let i = 0; i < BINS; i++) {
    const epoch = (startBin + i) * 60;
    const t = new Date(epoch * 1000).toTimeString().slice(0, 5);
    const row = { t, epoch };
    if (isGap(i)) {
      for (const c of CLASSES) row[c] = null;
    } else {
      for (const c of CLASSES) {
        const d = diurnal(i % 1440);
        const spike = c === 'agent' && i % 311 === 0 ? 2.4 : 1;
        row[c] = Math.max(0, Math.round(BASE[c] * d * spike * (0.72 + 0.56 * wob(i, CLASSES.indexOf(c)))));
      }
    }
    rows.push(row);
  }
  return rows;
}

const TOOLS = [
  ['pgrep', 'poll/probe', 'poll'],
  ['ps', 'poll/probe', 'poll'],
  ['squeue', 'slurm-query', 'slurm_mon'],
  ['sbatch', 'slurm-submit', 'slurm_sub'],
  ['git', 'git', 'other'],
  ['rg', 'search', 'filesearch'],
  ['grep', 'search', 'filesearch'],
  ['find', 'search', 'filesearch'],
  ['node', 'runtime', 'runtime'],
  ['python3', 'interpreter', 'compute'],
  ['make', 'build', 'build'],
  ['rsync', 'net/data', 'data'],
  ['cat', 'files', 'filesearch'],
  ['ls', 'files', 'filesearch'],
  ['bash', 'shell/env-init', 'other'],
];

const USERS = ['thomwg11', 'aparker', 'lzhang', 'mmarthen', 'rkoshy', 'jsilva', 'dwei'];

function events(nowEpoch, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const [tool, bucket, purpose] = TOOLS[Math.floor(wob(i, 3) * TOOLS.length)];
    const cls = CLASSES[Math.floor(wob(i, 4) * 3)];
    const user = cls === 'agent' && wob(i, 9) < 0.55 ? 'thomwg11' : USERS[Math.floor(wob(i, 5) * USERS.length)];
    const epoch = nowEpoch - i * 3 - Math.floor(wob(i, 6) * 3);
    const dstate = wob(i, 11) < 0.04;
    out.push({
      ts: new Date(epoch * 1000).toTimeString().slice(0, 8),
      epoch,
      actor3: cls,
      agent_type: cls === 'agent' ? (wob(i, 7) < 0.7 ? 'claude_code' : 'codex') : cls === 'ide' ? 'vscode' : null,
      user,
      host: HOSTS[Math.floor(wob(i, 8) * HOSTS.length)],
      depth: 2 + Math.floor(wob(i, 10) * 7),
      comm: tool,
      args: `${tool} ${['-u', '--me', '-n 1', '-l', 'status', "'^def '", '-x'][Math.floor(wob(i, 12) * 7)]}`,
      tool,
      bucket,
      purpose,
      duration_s: Number((0.004 + wob(i, 13) * (purpose === 'poll' ? 0.06 : 9)).toFixed(4)),
      cpu_s: Number((wob(i, 14) * (purpose === 'poll' ? 0.008 : 2.4)).toFixed(4)),
      exit_code: wob(i, 15) < 0.08 ? (wob(i, 16) < 0.5 ? 1 : 2) : 0,
      signal: wob(i, 17) < 0.015 ? 15 : null,
      sandbox:
        cls === 'agent'
          ? wob(i, 18) < 0.42
            ? 'sandboxed'
            : wob(i, 18) < 0.66
              ? 'unsandboxed'
              : wob(i, 18) < 0.9
                ? 'denied'
                : 'unknown'
          : 'unknown',
      // per record only a session root carries an approval; its children are
      // `unknown`, and nothing outside the agent class has one (`na`)
      approval: cls === 'agent' ? (wob(i, 19) < 0.02 ? 'bypassed' : wob(i, 19) < 0.05 ? 'auto' : 'unknown') : 'na',
      session_key: `sess-${cls}-${user}-${Math.floor(i / 37)}`,
      dstate_wait_s: dstate ? Number((wob(i, 20) * 3.2).toFixed(3)) : null,
    });
  }
  return out;
}

/* ------------------------------------------------------- panel payloads */

const BUCKETS = [
  'poll/probe',
  'slurm-submit',
  'slurm-query',
  'git',
  'runtime',
  'interpreter',
  'shell/env-init',
  'search',
  'build',
  'net/data',
  'files',
  'other',
];

/* calls share / cpu share per bucket, per class -- the poll/probe row is the
 * whole reason count and cost are drawn separately. */
const MIX = {
  agent: {
    'poll/probe': [29.4, 0.4],
    'slurm-submit': [1.1, 2.6],
    'slurm-query': [8.7, 1.9],
    git: [6.2, 3.1],
    runtime: [9.4, 41.8],
    interpreter: [3.1, 27.4],
    'shell/env-init': [12.6, 2.2],
    search: [14.8, 9.6],
    build: [1.2, 6.4],
    'net/data': [1.7, 1.1],
    files: [9.9, 2.2],
    other: [1.9, 1.3],
  },
  'ide': {
    'poll/probe': [11.2, 0.6],
    'slurm-submit': [0.4, 0.9],
    'slurm-query': [3.1, 0.8],
    git: [18.4, 11.2],
    runtime: [22.6, 48.1],
    interpreter: [4.4, 19.7],
    'shell/env-init': [9.8, 2.4],
    search: [8.1, 5.2],
    build: [2.2, 7.1],
    'net/data': [2.6, 1.4],
    files: [14.1, 1.8],
    other: [3.1, 0.8],
  },
  human: {
    'poll/probe': [14.7, 1.1],
    'slurm-submit': [2.8, 3.4],
    'slurm-query': [17.2, 4.6],
    git: [7.1, 4.2],
    runtime: [3.2, 8.8],
    interpreter: [8.9, 42.6],
    'shell/env-init': [13.4, 3.1],
    search: [9.8, 6.4],
    build: [3.4, 14.2],
    'net/data': [4.2, 6.9],
    files: [12.1, 2.9],
    other: [3.2, 1.8],
  },
};

const CALLS = { agent: 4_812_446, 'ide': 1_964_118, human: 384_902 };
const CPU = { agent: 91_244.8, 'ide': 48_611.2, human: 26_408.4 };

const ENV = (feed, paths, status = 'ok') => ({
  _feed: feed,
  _status: status,
  _present: true,
  _paths: paths,
  _notice: null,
});

const quant = (n, spine, coverage = 100, sampled = false) => ({
  n,
  p10: spine[0],
  p25: spine[1],
  p50: spine[2],
  p75: spine[3],
  p90: spine[4],
  p99: spine[5],
  max: spine[6],
  mean: spine[7],
  sampled,
  sample_n: sampled ? 250_000 : null,
  coverage_pct: coverage,
});
const accum = (total, n, coverage) => ({
  total,
  n,
  coverage_pct: coverage,
  mean: n ? Number((total / n).toFixed(4)) : null,
});

const EBPFM_PATHS = ['/var/log/ebpfm/exit_2026-09-14.jsonl', '/var/log/ebpfm/exit_2026-09-13.jsonl'];

function toolMix() {
  const by_class = {};
  const head = {};
  for (const c of CLASSES) {
    const calls = CALLS[c];
    const cpu = CPU[c];
    by_class[c] = {
      calls,
      cpu_s: cpu,
      rows: BUCKETS.map((b) => {
        const [cp, cup] = MIX[c][b];
        return {
          b,
          calls: Math.round((calls * cp) / 100),
          calls_pct: cp,
          cpu_s: Number(((cpu * cup) / 100).toFixed(2)),
          cpu_pct: cup,
        };
      }),
    };
    head[c] = TOOLS.slice(0, 12).map(([t], i) => {
      const cp = Number((22 / (i + 1.3)).toFixed(2));
      return {
        tool: t,
        calls: Math.round((calls * cp) / 100),
        calls_pct: cp,
        cpu_pct: Number((cp * (t === 'pgrep' || t === 'ps' ? 0.02 : 1.4)).toFixed(2)),
      };
    });
  }
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    buckets: BUCKETS,
    by_class,
    head,
    dedup: {
      dropped: 1_884_662,
      dropped_pct: 21.4,
      rule: 'resolved shell dropped iff its captured direct child is the command it resolved to',
    },
    cpu_basis: 'self cpu_s, each process once; child_cpu_s NEVER added (it double-counts the tree)',
  };
}

function ioProcess() {
  const by_class = {};
  const shape = {
    agent: [188_442.6, 41_882.2, 918_441.2, 62_004.8, 78.4, 4_812_446, 2_884_112],
    'ide': [64_118.4, 18_204.6, 402_118.8, 24_118.2, 84.1, 1_964_118, 1_142_884],
    human: [21_884.2, 8_118.4, 61_442.6, 11_884.6, 64.4, 384_902, 188_442],
  };
  for (const c of CLASSES) {
    const [rd, wr, rchar, wchar, hit, events, withIo] = shape[c];
    by_class[c] = {
      events,
      events_with_io: withIo,
      io_coverage_pct: Number(((100 * withIo) / events).toFixed(1)),
      rd_mb: rd,
      wr_mb: wr,
      rchar_mb: rchar,
      wchar_mb: wchar,
      cache_served_mb: Number((rchar - rd).toFixed(1)),
      cache_hit_pct: hit,
      /* agent egress is measured; IDE is partly measured; human TCP
         accounting did not resolve at all -- n:0 means NOT MEASURED. */
      net_tx_mb:
        c === 'human' ? accum(null, 0, 0) : accum(c === 'agent' ? 4_882.6 : 1_142.4, c === 'agent' ? 88_442 : 24_118, c === 'agent' ? 64.2 : 22.8),
      net_rx_mb:
        c === 'human' ? accum(null, 0, 0) : accum(c === 'agent' ? 18_442.8 : 6_118.2, c === 'agent' ? 88_442 : 24_118, c === 'agent' ? 64.2 : 22.8),
      net_calls: c === 'human' ? null : c === 'agent' ? 88_442 : 24_118,
      scope: { hosts: HOSTS.length, days: 2, note: 'per-process I/O is blank for other users (ptrace_may_access)' },
      quants: {
        rd_mb: quant(withIo, [0, 0.01, 0.12, 1.8, 14.2, 412.6, 9_884.2, 6.4], Number(((100 * withIo) / events).toFixed(1))),
        wr_mb: quant(withIo, [0, 0, 0.004, 0.09, 1.4, 88.4, 4_118.6, 1.9], Number(((100 * withIo) / events).toFixed(1))),
      },
    };
  }
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    by_class,
    by_tool: TOOLS.slice(0, 12).map(([t], i) => ({
      tool: t,
      cls: CLASSES[i % 3],
      events: Math.round(400_000 / (i + 1)),
      rd_mb: Number((18_000 / (i + 1.4)).toFixed(1)),
      wr_mb: Number((4_200 / (i + 2.1)).toFixed(1)),
      rchar_mb: Number((92_000 / (i + 1.2)).toFixed(1)),
      wchar_mb: Number((6_400 / (i + 1.8)).toFixed(1)),
      cache_hit_pct: Number((94 - i * 3.1).toFixed(1)),
      io_coverage_pct: Number((88 - i * 2.4).toFixed(1)),
    })),
    by_user: USERS.map((u, i) => ({
      user: u,
      cls: i === 0 ? 'agent' : CLASSES[i % 3],
      events: Math.round(2_400_000 / (i + 1.1)),
      rd_mb: Number((88_000 / (i + 1.2)).toFixed(1)),
      wr_mb: Number((21_000 / (i + 1.6)).toFixed(1)),
      rchar_mb: Number((412_000 / (i + 1.2)).toFixed(1)),
      wchar_mb: Number((38_000 / (i + 1.6)).toFixed(1)),
      net_tx_mb: i % 3 === 2 ? null : Number((2_100 / (i + 1.3)).toFixed(1)),
      net_rx_mb: i % 3 === 2 ? null : Number((8_400 / (i + 1.3)).toFixed(1)),
      io_coverage_pct: Number((72 - i * 4.2).toFixed(1)),
    })),
    notes: {
      coverage: 'io fields come from /proc/<pid>/io and are unreadable for other users',
      cache: 'cache_served_mb = rchar - read_bytes; it is an inference, not a counter',
    },
  };
}

/* The resources time series: 10-minute bins over 24 h, the backend's width for
 * that window. Sums by exit time for the exit fields, a sampled level for the
 * resident fields, and the same collector outage as the live rate (as bins). */
function resourceSeries(nowEpoch) {
  const BIN = 600;
  const N = 144;
  const start = Math.floor(nowEpoch / BIN) - N + 1;
  const t = Array.from({ length: N }, (_, i) => (start + i) * BIN);
  const out = (i) => i * 10 >= OUTAGE[0] - 10 && i * 10 < OUTAGE[1];
  const PER_BIN = {
    agent: [2_100, 0.042, 0.0009, 0.0006, 0.018],
    'ide': [960, 0.11, 0.0011, 0.0004, 0.31],
    human: [340, 0.36, 0.0021, 0.0002, 0.42],
  };
  const RSS = { agent: 4_400, 'ide': 2_200, human: 38_500 };
  const by_class = {};
  const resident = {};
  for (const [ci, c] of CLASSES.entries()) {
    const [n, cpu, wait, dst, maj] = PER_BIN[c];
    const row = { n: [], cpu_s: [], sched_wait_s: [], dstate_wait_s: [], maj_flt: [] };
    const res = { rss_mb: [], n_procs: [], d_state: [] };
    for (let i = 0; i < N; i++) {
      const w = diurnal(i * 10) * (0.7 + 0.6 * wob(i, 40 + ci));
      const k = Math.round(n * w);
      const gap = out(i) || (c === 'human' && i % 23 === 0);
      const nfs = i > 70 && i < 76; // an NFS hiccup: every class stalls at once
      row.n.push(gap ? null : k);
      // one long-lived agent exiting: its whole life's CPU in a single bin
      row.cpu_s.push(gap ? null : Number((k * cpu * (c === 'agent' && i === 97 ? 18 : 1)).toFixed(3)));
      row.sched_wait_s.push(gap ? null : Number((k * wait * (1 + 3 * wob(i, 50 + ci))).toFixed(3)));
      row.dstate_wait_s.push(gap ? null : Number((k * dst * (nfs ? 25 : 1)).toFixed(3)));
      row.maj_flt.push(gap ? null : Math.round(k * maj));
      res.rss_mb.push(out(i) ? null : Number((RSS[c] * (0.8 + 0.4 * diurnal(i * 10))).toFixed(1)));
      res.n_procs.push(out(i) ? null : Number((40 * w).toFixed(2)));
      res.d_state.push(out(i) ? null : nfs ? 3 : 0);
    }
    by_class[c] = row;
    resident[c] = res;
  }
  const stall = (i, j) =>
    out(i) ? null : Number(((i > 70 && i < 76 ? 40 : 0.4 * wob(i, 60 + j)) / (j + 1)).toFixed(3));
  return {
    bin_s: BIN,
    t,
    by_class,
    stall_by_tool: Object.fromEntries(TOOLS.slice(0, 6).map(([tool], j) => [tool, t.map((_, i) => stall(i, j))])),
    stall_other: t.map((_, i) => stall(i, 6)),
    resident,
    resident_hosts: t.map((_, i) => (out(i) ? null : HOSTS.length)),
    skipped: { no_ts: 0, future: 0, clipped_bins: 0 },
  };
}

function resources(nowEpoch) {
  const by_class = {};
  const shape = {
    agent: [4_812_446, 91_244.8, 62_118.4, 188_442.6],
    'ide': [1_964_118, 48_611.2, 31_884.2, 44_118.8],
    human: [384_902, 26_408.4, 18_442.6, 9_884.2],
  };
  for (const c of CLASSES) {
    const [events, cpu, run, wait] = shape[c];
    const cov = c === 'agent' ? 58.4 : c === 'ide' ? 31.2 : 12.6;
    by_class[c] = {
      events,
      cpu_s: cpu,
      sched_run_s: accum(run, Math.round((events * cov) / 100), cov),
      sched_wait_s: accum(wait, Math.round((events * cov) / 100), cov),
      schedstat_ratio: Number((run / wait).toFixed(3)),
      quants: {
        cpu_s: quant(events, [0.001, 0.002, 0.006, 0.028, 0.184, 4.82, 3_118.4, 0.019], 100, events > 2_000_000),
        peak_rss_mb: quant(events, [1.8, 2.9, 5.4, 18.2, 84.6, 1_218.4, 41_882.6, 42.1], 96.8),
        sched_wait_s: quant(Math.round((events * cov) / 100), [0.0002, 0.0008, 0.004, 0.026, 0.188, 3.42, 188.6, 0.041], cov),
        duration_s: quant(events, [0.004, 0.008, 0.021, 0.14, 1.82, 118.4, 88_442.6, 2.84], 100),
        on_cpu_frac: quant(Math.round((events * cov) / 100), [0.004, 0.018, 0.11, 0.42, 0.78, 0.98, 1.0, 0.26], cov),
      },
      faults: { minor: accum(1_884_662_112, events, 100), major: accum(88_442, events, 100) },
      /* delay accounting needs CAP_NET_ADMIN on the taskstats netlink socket:
         the collector is unprivileged, so this is simply NOT MEASURED. */
      delays: { blkio_delay_s: accum(null, 0, 0), swapin_delay_s: accum(null, 0, 0) },
      dstate: {
        events: c === 'human' ? 118 : c === 'agent' ? 18_442 : 4_118,
        wait_s: accum(c === 'human' ? 88.4 : c === 'agent' ? 4_882.6 : 1_118.2, c === 'human' ? 118 : c === 'agent' ? 18_442 : 4_118, 100),
      },
    };
  }
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    by_class,
    stall_by_tool: TOOLS.slice(0, 14).map(([t], i) => ({
      tool: t,
      wait_s: Number((880 / (i + 1.7)).toFixed(2)),
      n: Math.round(4_000 / (i + 1)),
      episodes: Math.round(9_000 / (i + 1)),
      max_s: Number((4.2 / (i + 1)).toFixed(2)),
    })),
    series: resourceSeries(nowEpoch),
    scope: { hosts: HOSTS.length, days: 2, window: '2026-09-13 00:00 to 2026-09-14 18:00' },
    cpu_basis: 'self cpu_s (utime+stime of the exiting process); child_cpu_s NEVER added',
  };
}

function sandbox() {
  // Shaped after rtx6000a's 24 h on 2026-10-11: no tool call isolated, one Codex
  // session whose every sandbox attempt failed, approval mostly not a bypass.
  // Kept awkward on purpose: unknown and exited-before-read slices, a partly
  // isolated session, and approval states argv cannot settle.
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    by_class_events: {
      agent: { sandboxed: 4_118, unsandboxed: 1_790_840, denied: 412, unknown: 18_140 },
      ide: { unsandboxed: 996, unknown: 552 },
      human: { sandboxed: 16, unsandboxed: 1_548, denied: 5, unknown: 12 },
    },
    approval_by_class_events: {
      agent: { bypassed: 2, auto: 3, supervised: 6, unobservable: 2, na: 20_676, unknown: 1_788_302 },
      ide: { na: 1_548 },
      human: { na: 1_565 },
    },
    n_events: { agent: 1_813_510, ide: 1_548, human: 1_581 },
    by_class_sessions: {
      agent: { sandboxed: 1, partial: 1, failed: 2, unsandboxed: 6, unknown: 1, no_calls: 9 },
    },
    approval_by_class_sessions: {
      agent: { bypassed: 2, auto: 7, supervised: 8, unobservable: 2, unknown: 1 },
    },
    n_sessions: { agent: 20 },
    tool_calls: {
      agent: { isolated: 41, not_isolated: 708, failed: 29, no_payload: 4, unknown: 3 },
    },
    attempts: [
      { wrapper: 'codex-linux-sandbox', agent_type: 'codex', outcome: 'failed', n: 17 },
      { wrapper: 'bwrap', agent_type: 'bwrap', outcome: 'isolated', n: 41 },
      { wrapper: 'bwrap', agent_type: 'bwrap', outcome: 'failed', n: 12 },
      { wrapper: 'bwrap', agent_type: 'bwrap', outcome: 'no_payload', n: 4 },
    ],
    sessions: [
      { host: 'holylogin01', pid: 3_791_032, user: 'ada', actor3: 'agent', agent_type: 'claude_code', approval: 'auto', approval_detail: 'permission-mode=auto may-switch-to-bypass', state: 'unsandboxed', calls: { not_isolated: 328 }, calls_known: 328, isolated_pct: 0 },
      { host: 'holylogin02', pid: 1_467_449, user: 'grace', actor3: 'agent', agent_type: 'codex', approval: 'supervised', approval_detail: 'no launch flag', state: 'failed', calls: { failed: 17 }, calls_known: 17, isolated_pct: 0 },
      { host: 'holylogin01', pid: 404_264, user: 'alan', actor3: 'agent', agent_type: 'claude_code', approval: 'bypassed', approval_detail: '--permission-mode=bypassPermissions may-switch-to-bypass', state: 'partial', calls: { isolated: 41, not_isolated: 12, failed: 12 }, calls_known: 65, isolated_pct: 63.08 },
      { host: 'boslogin01', pid: 88_120, user: 'ada', actor3: 'agent', agent_type: 'codex', approval: 'unobservable', approval_detail: 'app-server', state: 'no_calls', calls: {}, calls_known: 0, isolated_pct: null },
      { host: 'boslogin02', pid: 22_018, user: 'edsger', actor3: 'agent', agent_type: 'claude_code', approval: 'unknown', approval_detail: null, state: 'unknown', calls: { unknown: 3 }, calls_known: 0, isolated_pct: null },
    ],
    sessions_total: 20,
    launch_modes: [
      { agent_type: 'claude_code', approval: 'supervised', detail: 'no launch flag', n: 7 },
      { agent_type: 'claude_code', approval: 'auto', detail: 'permission-mode=auto may-switch-to-bypass', n: 7 },
      { agent_type: 'claude_code', approval: 'bypassed', detail: '--permission-mode=bypassPermissions may-switch-to-bypass', n: 2 },
      { agent_type: 'codex', approval: 'unobservable', detail: 'app-server', n: 2 },
      { agent_type: 'claude_code', approval: 'supervised', detail: 'permission-mode=default', n: 1 },
    ],
    ancestry_matrix: [
      { ancestry: 'none', kind: 'process', sandbox: 'unsandboxed', n: 1_793_263, dstate_wait_s: 434.2, dstate_coverage_pct: 100, reading: null },
      { ancestry: 'none', kind: 'process', sandbox: 'unknown', n: 18_704, dstate_wait_s: 4.0, dstate_coverage_pct: 100, reading: null },
      { ancestry: 'bwrap', kind: 'process', sandbox: 'sandboxed', n: 4_118, dstate_wait_s: 12.4, dstate_coverage_pct: 100, reading: 'under a sandbox wrapper, namespace-isolated' },
      { ancestry: 'bwrap', kind: 'wrapper', sandbox: 'unsandboxed', n: 125, dstate_wait_s: 0, dstate_coverage_pct: 100, reading: 'wrapper, read at its own exec before it unshares -- says nothing about the call it wraps; see the tool-call outcomes' },
      { ancestry: 'codex-linux-sandbox', kind: 'wrapper', sandbox: 'unsandboxed', n: 51, dstate_wait_s: 0, dstate_coverage_pct: 100, reading: 'wrapper, read at its own exec before it unshares -- says nothing about the call it wraps; see the tool-call outcomes' },
      { ancestry: 'none', kind: 'process', sandbox: 'sandboxed', n: 16, dstate_wait_s: 0, dstate_coverage_pct: 100, reading: 'isolated with no agent sandbox ancestor: a container, apptainer, a systemd unit, or a reused pid inheriting one -- not an agent sandbox' },
      { ancestry: 'none', kind: 'process', sandbox: 'denied', n: 417, dstate_wait_s: null, dstate_coverage_pct: 0, reading: null },
    ],
    sandbox_src: { inherit: 1_791_285, 'none:tick': 1_938, 'userns:exec': 41, 'none:exec': 161, denied: 417 },
    sandbox_detail: { nnp: 22, 'seccomp=2': 16, mnt: 41, net: 41, user: 41 },
    agent_only: { agent: [{ sandbox: 'unsandboxed', approval: 'na', n: 20_412 }] },
    below_capped: 0,
    states: {
      sandbox: ['sandboxed', 'unsandboxed', 'denied', 'unknown'],
      sandbox_known: ['sandboxed', 'unsandboxed'],
      session: ['sandboxed', 'partial', 'failed', 'unsandboxed', 'unknown', 'no_calls'],
      session_known: ['sandboxed', 'partial', 'failed', 'unsandboxed'],
      tool_call: ['isolated', 'not_isolated', 'failed', 'no_payload', 'unknown'],
      tool_call_known: ['isolated', 'not_isolated', 'failed'],
      approval: ['bypassed', 'auto', 'supervised', 'unobservable', 'na', 'unknown'],
      approval_known: ['bypassed', 'auto', 'supervised'],
    },
    notes: {
      meaning: 'sandbox = NAMESPACE ISOLATION; Landlock/seccomp-only sandboxes are invisible, containers count',
      denied: "'exited before read' (key: denied) is NOT 'not isolated'",
      failed: "'sandbox attempt failed': the wrapper exited non-zero and no payload ran under it",
    },
  };
}

/* Where an agent session is driven from (normalize.surface_of), cycling through
 * every surface so the page shows each chip, an unknown one included. */
const SURFACES = {
  claude_code: [['terminal', 'interactive'], ['desktop', 'Claude desktop app'], ['extension', 'VS Code'], ['script', 'claude -p'], ['web', 'Claude Remote Control']],
  codex: [['terminal', 'interactive'], ['desktop', 'Codex app'], ['extension', 'VS Code'], ['script', 'codex exec'], ['unknown', 'shared app server']],
};
const surfaceOf = (i, agentType) => {
  const s = SURFACES[agentType]?.[i % 5];
  return { surface: s ? s[0] : null, surface_detail: s ? s[1] : null };
};

function trajectories(nowEpoch) {
  const rows = {};
  const sids = [];
  for (let i = 0; i < 28; i++) {
    const cls = i % 7 === 6 ? 'human' : i % 3 === 1 ? 'ide' : 'agent';
    const user = cls === 'agent' && i % 4 !== 3 ? 'thomwg11' : USERS[(i + 2) % USERS.length];
    const sid = `period4-${HOSTS[i % HOSTS.length]}-${user}-${1_000 + i * 37}`;
    const nEvents = Math.round(48_000 / (i * 0.4 + 1));
    const runs = [
      ['pgrep', 40 + ((i * 17) % 160)],
      ['git', 1],
      ['rg', 1 + (i % 5)],
      ['node', 2],
      ['squeue', 3 + (i % 9)],
      ['python3', 1],
      ['bash', 4],
      ['cat', 2 + (i % 3)],
      ['sbatch', 1],
      ['ps', 12 + ((i * 7) % 40)],
    ];
    const purposeRuns = [
      ['poll', 40 + ((i * 17) % 160)],
      ['other', 1],
      ['filesearch', 1 + (i % 5)],
      ['runtime', 2],
      ['slurm_mon', 3 + (i % 9)],
      ['compute', 1],
      ['other', 4],
      ['filesearch', 2 + (i % 3)],
      ['slurm_sub', 1],
      ['poll', 12 + ((i * 7) % 40)],
    ];
    rows[sid] = {
      sid,
      key_type: i % 5 === 4 ? 'cgroup' : 'session_scope',
      host: HOSTS[i % HOSTS.length],
      user,
      actor3: cls,
      agent_type: cls === 'agent' ? (i % 3 ? 'claude_code' : 'codex') : cls === 'ide' ? 'vscode' : null,
      n_events: nEvents,
      cpu_s: Number((nEvents * (0.004 + ((i % 7) * 0.02))).toFixed(2)),
      distinct_tools: 6 + ((i * 3) % 34),
      chain_runs: runs.length + (i % 11),
      wall_s: 1_800 + i * 4_400,
      peak_rss_mb: Number((188 + i * 61.4).toFixed(1)),
      io_rd_mb: Number((418.2 / (i * 0.2 + 1)).toFixed(1)),
      io_wr_mb: Number((88.4 / (i * 0.3 + 1)).toFixed(1)),
      net_tx_mb: Number((12.6 / (i * 0.5 + 1)).toFixed(2)),
      net_rx_mb: Number((96.1 / ((i % 7) * 0.4 + 1)).toFixed(2)),
      first: nowEpoch - 1_800 - i * 4_400 - i * 300,
      last: nowEpoch - i * 300,
      ...(cls === 'agent' && i % 4 === 1
        ? { running: { seen: nowEpoch - 20, n_procs: 3 + (i % 9), rss_mb: 410 + i * 12, census_at: nowEpoch - 15 } }
        : {}),
      sandbox: cls === 'agent' ? (i % 4 === 0 ? 'sandboxed' : i % 4 === 1 ? 'unsandboxed' : i % 4 === 2 ? 'denied' : 'unknown') : 'unknown',
      approval: cls === 'agent' ? (i % 3 === 0 ? 'bypassed' : 'supervised') : 'unknown',
      ...surfaceOf(i, cls === 'agent' ? (i % 3 ? 'claude_code' : 'codex') : null),
      sandbox_ancestry: cls === 'agent' && i % 4 < 2 ? 'bwrap' : 'none',
      autonomous: cls === 'agent' && i % 3 === 0,
      tool_rle: runs,
      chain_truncated: i % 6 === 0,
      purpose_rle: purposeRuns,
    };
    sids.push(sid);
  }
  const by = (k) => [...sids].sort((a, b) => (rows[b][k] ?? 0) - (rows[a][k] ?? 0));
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    window: { minutes: 1440, from: new Date((nowEpoch - 86_400) * 1000).toISOString(), to: new Date(nowEpoch * 1000).toISOString() },
    rank_by: 'recent',
    rank_meaning: 'events = process exits attributed to the session key',
    ranks: {
      recent: by('last'),
      events: by('n_events'),
      cpu_s: by('cpu_s'),
      peak_rss_mb: by('peak_rss_mb'),
      io_mb: [...sids].sort((a, b) => rows[b].io_rd_mb + rows[b].io_wr_mb - rows[a].io_rd_mb - rows[a].io_wr_mb),
      net_mb: [...sids].sort((a, b) => rows[b].net_tx_mb + rows[b].net_rx_mb - rows[a].net_tx_mb - rows[a].net_rx_mb),
      distinct_tools: by('distinct_tools'),
    },
    rows,
    by_class_counts: {
      agent: sids.filter((s) => rows[s].actor3 === 'agent').length,
      'ide': sids.filter((s) => rows[s].actor3 === 'ide').length,
      human: sids.filter((s) => rows[s].actor3 === 'human').length,
    },
    by_key_type: {
      session_scope: sids.filter((s) => rows[s].key_type === 'session_scope').length,
      cgroup: sids.filter((s) => rows[s].key_type === 'cgroup').length,
    },
    dominant_user_share_pct: 71.4,
    buffer: { capacity: 4_096, held: sids.length, evicted: 118 },
    basis: 'tool_rle is the run-length encoding of effective_comm in exit order; chains over 4096 runs are truncated',
  };
}

/* One inspected session, for `/api/trajectory`: the shapes the tree has to
 * draw right -- an agent with no exit record, tool calls with the command they
 * ran, a poll group, a 3-step cycle, a non-zero exit, a SIGKILL, a parent with
 * no record placed by depth, a reparented orphan, a hold count and a row-budget
 * count. Totals are summed from the leaves, so they add up the way the
 * backend's do. */
function trajectoryDetail(nowEpoch, traj) {
  const sid = traj.ranks.events[0];
  const t0 = nowEpoch - 5_400;
  let next = 41_000;
  const p = (o) => ({
    k: 'p', pid: next++, cmd: 'x', start: null, end: null, dur: null, cpu: null,
    n: 1, sub_cpu: null, ev: 'exit', link: 'ppid', exit_code: 0, ...o,
  });
  const run = (at, d, cmd, args, o = {}) =>
    p({ cmd, args, start: at, end: at + d, dur: d, cpu: Number((d * 0.6).toFixed(3)), ...o });
  const shell = (at, cmdline, kids, o = {}) => {
    const end = Math.max(at + 0.05, ...kids.map((k) => k.end ?? at));
    return p({ cmd: 'bash', role: 'tool-shell', cmdline, start: at, end, dur: Number((end - at).toFixed(3)), cpu: 0.004, kids, ...o });
  };
  const group = (motif, members, reps, items, extra = {}) => ({
    k: 'g', motif, period: motif.length, reps, count: motif.length * reps,
    n: motif.length * reps, start: members[0].start, end: members[members.length - 1].end,
    cpu: Number((members.reduce((a, m) => a + (m.cpu ?? 0), 0) * (reps * motif.length / members.length)).toFixed(3)),
    sub_cpu: null, dur_max: Math.max(...members.map((m) => m.dur ?? 0)), exits: { 0: motif.length * reps },
    signals: {}, alive: null, distinct: 1, samples: [{ args: members[0].args, n: motif.length * reps }],
    items, more: motif.length * reps - items.length, ...extra,
  });
  const polls = Array.from({ length: 5 }, (_, i) => run(t0 + 2 + i * 0.3, 0.004, 'git', 'git status --porcelain'));
  const cycle = [];
  for (let i = 0; i < 6; i++) {
    const at = t0 + 900 + i * 31;
    cycle.push(run(at, 0.02, 'squeue', 'squeue -j 812 -h -o %T'), run(at + 0.03, 0.001, 'grep', 'grep -q R'),
      run(at + 0.05, 30, 'sleep', 'sleep 30', { cpu: 0 }));
  }
  const cc = Array.from({ length: 6 }, (_, i) => run(t0 + 2_400 + i * 0.4, 7.5, 'cc1plus', `cc1plus -O2 src/kernel_${i}.cc`));
  const loop = Array.from({ length: 6 }, (_, i) => run(t0 + 3_000 + i * 0.2, 0.15, 'python', 'python -c import torch'));
  const tree = p({
    pid: 40_000, ppid: 39_998, cmd: 'claude', args: 'claude --resume', role: 'agent', ev: null, link: null,
    exit_code: undefined, start: nowEpoch - 3 * 86_400, end: nowEpoch - 30,
    actor3: traj.rows[sid].actor3, agent_type: traj.rows[sid].agent_type,
    user: traj.rows[sid].user, cwd: '/n/home03/thomwg11/proj',
    kids: [
      group(['git'], polls, 41, polls.slice(0, 5)),
      shell(t0 + 30, 'git status --short && git diff --stat', [
        run(t0 + 30.01, 0.01, 'git', 'git status --short'), run(t0 + 30.03, 0.02, 'git', 'git diff --stat'),
      ]),
      p({ cmd: '2.1.291', role: 'embedded-tool', args: 'rg --no-config --files --hidden /n/home03/thomwg11/proj', start: t0 + 41, end: t0 + 41.2, dur: 0.2, cpu: 0.11 }),
      shell(t0 + 900, 'while ! squeue -j 812 -h -o %T | grep -q R; do sleep 30; done', [
        group(['squeue', 'grep', 'sleep'], cycle, 40, cycle.slice(0, 6)),
      ]),
      shell(t0 + 2_100, 'cd exp2 && python train.py --hf-token [REDACTED] --epochs 1', [
        run(t0 + 2_100.1, 212.4, 'python', 'python train.py --hf-token [REDACTED] --epochs 1', { exit_code: 1 }),
      ], { cwd: '/n/home03/thomwg11/proj/exp2' }),
      shell(t0 + 2_399, 'timeout 60 make -j8', [
        p({ cmd: 'make', args: 'make -j8', start: t0 + 2_399.5, end: t0 + 2_459.5, dur: 60, cpu: 0.4, exit_code: null, signal: 9,
            kids: [group(['cc1plus'], cc, 24, cc.slice(0, 6))] }),
      ], { exit_code: 124 }),
      p({ pid: 52_211, cmd: null, ev: null, link: 'depth', gap: 1, exit_code: undefined, start: null, end: null, cpu: null, n: 0,
          kids: [group(['python'], loop, 2_000, loop.slice(0, 6)),
                 { k: 'agg', why: 'hold', n: 18_400, sub_cpu: 2_208.4, cpu_nm: null, start: t0 + 3_400, end: t0 + 5_300, cmds: [['python', 18_400]] }] }),
      run(t0 + 3_100, 1_800, 'sleep', 'sleep 1800', { ppid: 1, cpu: 0, link: 'depth', orphan: true }),
      { k: 'more', why: 'budget', count: 37, n: 112, sub_cpu: 3.8, cpu_nm: null, start: t0 + 5_000, end: t0 + 5_390, cmds: [['bash', 31], ['git ×5', 6]] },
    ],
  });
  // Sum each subtree the way the backend does: own record plus everything below.
  const fin = (e) => {
    if (e.k === 'g') {
      e.items.forEach(fin);
      e.sub_cpu = e.cpu;
      return e;
    }
    if (e.k !== 'p') return e;
    (e.kids ?? []).forEach(fin);
    const kids = e.kids ?? [];
    e.n = (e.ev ? 1 : 0) + kids.reduce((a, k) => a + (k.n ?? 0), 0);
    const c = [e.ev ? e.cpu : null, ...kids.map((k) => k.sub_cpu)].filter((v) => v != null);
    e.sub_cpu = c.length ? Number(c.reduce((a, v) => a + v, 0).toFixed(3)) : null;
    return e;
  };
  fin(tree);
  const parents = [];
  const walk = (e) => {
    if (e.k === 'p' && e.kids?.length) parents.push(e.pid);
    (e.kids ?? []).forEach(walk);
  };
  walk(tree);
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    sid,
    found: true,
    host: traj.rows[sid].host,
    owner_pid: 40_000,
    keys: 41,
    span: { first: t0, last: t0 + 5_390 },
    scan: {
      from: t0, to: t0 + 5_391, files: 1, bytes_planned: 412_000_000, bytes: 412_000_000,
      parsed: tree.n + 210, skipped: 1_284_000, records: tree.n, ticks: 88, first: t0, last: t0 + 5_390,
      stopped: null, stopped_at: null, elapsed_s: 2.4, computed_at: nowEpoch,
    },
    bounds: {
      max_records: 150_000, max_bytes: 6 * 2 ** 30, hold: 20_000, per_parent: 2_000, nodes: 6_000,
      itemised: tree.n - 18_400, counted: 18_400, shown_entries: 60, elided: 112, stubs: 2,
    },
    links: { ppid: 54, depth: 2 },
    timeline: { lo: t0, hi: t0 + 5_390 },
    tree,
    basis: { fixture: 'mock/gen_fixture.mjs: a hand-built tree with every row shape the inspector draws' },
  };
}

/* ------------------------------------------------------------- the bundle */

/* ------------------------------------------------------- agents (cascades)
 * The Agents tab: agent trees and each agent's workspace / blast radius. The
 * awkward states are here on purpose: an agent started at `/` (its home is its
 * first call's, drawn italic), a subagent whose writes land outside its
 * parent's home (only visible "with its subagents"), a write into another
 * user's home (high), a write into the agent's own /tmp scratch (low), an
 * agent with no cwd at all (outside = null, never 0), a directory nobody ran in
 * (io null), and names past the per-directory cap. */
function wsDir(path, shown, kind, o = {}) {
  return {
    path, shown, inside: kind === 'home', root: o.root ?? null, kind,
    deeper: o.deeper ?? 0, procs: o.procs ?? 0,
    io: o.procs ? { rchar_mb: o.rd ?? 0.4, wchar_mb: o.wr ?? 0.1, rd_mb: 0.02, wr_mb: o.disk ?? 0, coverage_pct: o.cov ?? 100 } : null,
    read: o.r ?? 0, write: o.w ?? 0, delete: o.d ?? 0, named: o.n ?? 0,
    names_capped: o.capped ?? 0, examples: o.ex ?? [],
    first: o.first ?? null, last: o.last ?? null,
    flag: kind !== 'home' && ((o.d ?? 0) > 0 ? 'delete' : (o.w ?? 0) > 0 ? 'write' : null) || null,
  };
}

function wsView(nowEpoch, dirs, extra = {}) {
  const sum = (k) => dirs.reduce((a, d) => a + (d[k] || 0), 0);
  const ins = dirs.filter((d) => d.inside === true);
  const outs = dirs.filter((d) => d.inside === false);
  const procs = sum('procs');
  const procsIn = ins.reduce((a, d) => a + d.procs, 0);
  const io = dirs.filter((d) => d.io);
  const ops = (ds) => ({ read: ds.reduce((a, d) => a + d.read, 0), write: ds.reduce((a, d) => a + d.write, 0), delete: ds.reduce((a, d) => a + d.delete, 0), named: ds.reduce((a, d) => a + d.named, 0) });
  const noHome = extra.noHome === true;
  const roots = {};
  for (const d of outs) {
    const g = (roots[d.root] ??= { root: d.root, kind: d.kind, procs: 0, dirs: 0, read: 0, write: 0, delete: 0, named: 0, wchar_mb: null });
    g.procs += d.procs; g.dirs += 1 + d.deeper; g.read += d.read; g.write += d.write; g.delete += d.delete; g.named += d.named;
    if (d.io) g.wchar_mb = Number(((g.wchar_mb ?? 0) + d.io.wchar_mb).toFixed(3));
  }
  const flags = [];
  for (const d of outs) {
    for (const [name, o] of d.examples) {
      if (/[wd]/.test(o)) flags.push({ path: d.path + '/' + name, shown: d.shown + '/' + name, op: o.includes('d') ? 'delete' : 'write', kind: d.kind });
    }
  }
  return {
    summary: {
      procs: procs + (extra.noCwd ?? 0), procs_no_cwd: extra.noCwd ?? 0,
      procs_inside: noHome ? null : procsIn, procs_outside: noHome ? null : procs - procsIn,
      dirs: dirs.reduce((a, d) => a + 1 + d.deeper, 0), dirs_capped: 0,
      inside_pct: noHome || !procs ? null : Number(((100 * procsIn) / procs).toFixed(2)),
      io: io.length ? { rchar_mb: Number(io.reduce((a, d) => a + d.io.rchar_mb, 0).toFixed(3)), wchar_mb: Number(io.reduce((a, d) => a + d.io.wchar_mb, 0).toFixed(3)), rd_mb: 0.1, wr_mb: 0.0, coverage_pct: 100 } : null,
      wchar_inside_pct: null,
      named: ops(dirs), outside: noHome ? null : ops(outs),
      outside_writes: noHome ? null : outs.reduce((a, d) => a + d.write + d.delete, 0),
      unresolved: extra.unresolved ?? 0,
      roots: Object.values(roots).sort((a, b) => b.write + b.delete - (a.write + a.delete) || b.procs - a.procs),
      first: nowEpoch - 80_000, last: nowEpoch - 120, keys: extra.keys ?? 40,
    },
    dirs, dirs_more: extra.more ?? { rows: 0, procs: 0 }, flags, flags_more: 0,
  };
}

function cascades(nowEpoch) {
  const H = '/home/thomwg11';
  const P = H + '/proj/climate-ml';
  const t = (h) => nowEpoch - h * 3600;
  const leadDirs = [
    wsDir(P, './', 'home', { procs: 18_204, rd: 8_211.4, wr: 1_904.2, disk: 512.6, r: 31, w: 6, n: 4, ex: [['README.md', 'r'], ['train.py', 'r']], first: t(22), last: t(0.1) }),
    wsDir(P + '/src/model', './src/model', 'home', { procs: 2_410, r: 44, w: 9, deeper: 3, capped: 12, ex: [['layers.py', 'rw'], ['attention.py', 'r']], first: t(20), last: t(0.4) }),
    wsDir(P + '/data/era5', './data/era5', 'home', { procs: 312, rd: 41_220.8, wr: 0.2, r: 8, ex: [['t2m_2020.nc', 'r']], first: t(19), last: t(3) }),
    wsDir(P + '/out', './out', 'home', { w: 14, d: 3, deeper: 9, ex: [['run12/ckpt.pt', 'w'], ['run03', 'd']], first: t(18), last: t(1) }),
    wsDir('/n/holyscratch01/lab/thomwg11/era5', '/n/holyscratch01/lab/thomwg11/era5', 'shared storage', { root: '/n/holyscratch01', procs: 96, rd: 120_400.1, wr: 88_012.5, disk: 88_000.4, w: 2, r: 1, ex: [['cache.zarr', 'w'], ['index.json', 'w']], first: t(12), last: t(2) }),
    wsDir('/home/aparker/shared-data', '~aparker/shared-data', "other user's home", { root: '~aparker', r: 2, w: 1, ex: [['labels.csv', 'w'], ['README', 'r']], first: t(6), last: t(6) }),
    wsDir('/tmp/claude-1000/-home-thomwg11-proj-climate-ml/5d1c/scratchpad', '/tmp/claude-1000/-home-thomwg11-proj-climate-ml/5d1c/scratchpad', 'temp', { root: '/tmp', w: 4, r: 4, ex: [['plot.py', 'rw'], ['out.png', 'w']], first: t(9), last: t(0.5) }),
    wsDir(H + '/.cache/huggingface/hub', '~/.cache/huggingface/hub', 'cache & tooling', { root: '~/.cache', procs: 4, r: 1, n: 2, ex: [['models--bert-base', 'r']], first: t(15), last: t(15) }),
    wsDir('/', '/', 'filesystem root', { root: '/', procs: 1_204, rd: 2.1, wr: 0.01, first: t(22), last: t(0.1) }),
  ];
  const subDirs = [
    wsDir('/scratch/thomwg11/eval', '/scratch/thomwg11/eval', 'shared storage', { root: '/scratch', procs: 820, rd: 312.4, wr: 96.1, w: 3, d: 1, r: 5, ex: [['tmp', 'd'], ['results.jsonl', 'w']], first: t(4), last: t(1.5) }),
    wsDir('/scratch/thomwg11/eval/configs', '/scratch/thomwg11/eval/configs', 'shared storage', { root: '/scratch', r: 6, ex: [['base.yaml', 'r']], first: t(4), last: t(2) }),
  ];
  const lead = wsView(nowEpoch, leadDirs, { keys: 612, unresolved: 3, more: { rows: 4, procs: 11 } });
  const leadTree = wsView(nowEpoch, [...leadDirs, ...subDirs], { keys: 688, unresolved: 3, more: { rows: 4, procs: 11 } });
  const sub = wsView(nowEpoch, [
    wsDir('/scratch/thomwg11/eval', './', 'home', { procs: 820, rd: 312.4, wr: 96.1, w: 3, d: 1, r: 5, ex: [['tmp', 'd'], ['results.jsonl', 'w']], first: t(4), last: t(1.5) }),
    wsDir('/scratch/thomwg11/eval/configs', './configs', 'home', { r: 6, ex: [['base.yaml', 'r']], first: t(4), last: t(2) }),
  ], { keys: 76 });
  const ideAgent = wsView(nowEpoch, [
    wsDir('/home/lzhang/thesis', './', 'home', { procs: 2_118, rd: 210.2, wr: 14.8, r: 12, w: 2, ex: [['chapter3.tex', 'rw']], first: t(10), last: t(0.2) }),
    wsDir('/', '/', 'filesystem root', { root: '/', procs: 64, rd: 0.3, wr: 0.0, first: t(10), last: t(9.8) }),
  ], { keys: 140 });
  const homeless = wsView(nowEpoch, [], { noHome: true, noCwd: 37, keys: 3 });

  const node = (o) => ({
    ppid: null, host: 'holylogin01', role: 'agent', agent_type: 'claude_code', user: 'thomwg11',
    command: 'claude', args: 'claude', cwd: P, implied: false, frontend: false, parent: null,
    via: null, path: [], spawned_by: null, started: '2026-09-13 09:12:40', first_seen: '2026-09-13 09:12:40',
    last_seen: '2026-09-14 08:51:02', exited: false, duration_s: null, depth: 0, agent_level: 1,
    fanout: 0, tool_calls: 18_204, cpu_s: 812.4, session_keys: 612, home: P, home_src: 'agent',
    workspace: null, cwd_vs_parent_agent: null, surface: 'terminal', surface_detail: 'interactive', ...o,
  });
  const brief = (v) => ({ dirs: v.summary.dirs, inside_pct: v.summary.inside_pct, outside_writes: v.summary.outside_writes });
  const trees = [
    {
      root: null, host: 'holylogin01', users: ['thomwg11'], agent_depth: 2, max_fanout: 1, n_nodes: 2,
      n_agents: 2, agents_nested: 1, agent_types: ['claude_code', 'codex'], tool_calls: 19_024, cpu_s: 901.2,
      nodes: [
        node({ pid: 41_200, fanout: 1, workspace: brief(lead) }),
        node({ pid: 52_877, ppid: 52_870, agent_type: 'codex', command: 'codex', args: 'codex exec --full-auto run the eval sweep', cwd: '/scratch/thomwg11/eval', parent: 41_200, via: 'ancestor', path: ['zsh'], depth: 1, agent_level: 2, tool_calls: 820, cpu_s: 88.8, session_keys: 76, home: '/scratch/thomwg11/eval', workspace: brief(sub), cwd_vs_parent_agent: 'outside', started: '2026-09-14 04:02:11', exited: true, duration_s: 9_120.4, surface: 'script', surface_detail: 'codex exec' }),
      ],
      nodes_truncated: false, hidden_nodes: 0,
    },
    {
      root: null, host: 'holylogin02', users: ['lzhang'], agent_depth: 1, max_fanout: 0, n_nodes: 1,
      n_agents: 1, agents_nested: 0, agent_types: ['claude_code'], tool_calls: 2_182, cpu_s: 140.2,
      nodes: [node({ pid: 7_731, host: 'holylogin02', user: 'lzhang', cwd: '/', args: '/home/lzhang/.vscode-server/extensions/anthropic.claude-code-2.1.308-linux-x64/resources/native-binary/claude --output-format stream-json', home: '/home/lzhang/thesis', home_src: 'first_call', tool_calls: 2_182, cpu_s: 140.2, session_keys: 140, workspace: brief(ideAgent), surface: 'extension', surface_detail: 'VS Code' })],
      nodes_truncated: false, hidden_nodes: 1,
    },
    {
      root: null, host: 'boslogin01', users: ['dwei'], agent_depth: 1, max_fanout: 0, n_nodes: 1,
      n_agents: 1, agents_nested: 0, agent_types: ['claude_code'], tool_calls: 37, cpu_s: 2.1,
      nodes: [node({ pid: 9_904, host: 'boslogin01', user: 'dwei', command: null, args: null, cwd: null, implied: true, home: null, home_src: null, tool_calls: 37, cpu_s: 2.1, session_keys: 3, workspace: brief(homeless), surface: 'unknown', surface_detail: null })],
      nodes_truncated: false, hidden_nodes: 0,
    },
  ];
  for (const tr of trees) tr.root = tr.nodes[0];
  const agent = (n, self, tree, extra = {}) => ({
    host: n.host, pid: n.pid, agent_type: n.agent_type, user: n.user, tree_root: extra.root ?? n.pid,
    parent_agent: extra.parent ?? null, subagents: extra.subs ?? [], home: n.home, home_src: n.home_src,
    home_shown: n.home ? n.home.replace('/home/' + n.user, '~') : null, self, tree,
  });
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    window: { minutes: 1440, from: '2026-09-13 09:46:40', to: '2026-09-14 09:46:40' },
    summary: { trees: 3, nested: 1, hosted: 0, agents: 4, agents_nested: 1, agents_implied: 1, max_agent_depth: 2, max_fanout: 1, trees_without_agents: 2 },
    depth_hist: { 1: 2, 2: 1 },
    fanout_hist: { 1: 1 },
    edges: [{ parent: 'claude_code agent', child: 'codex agent', n: 1 }],
    edge_via: { ancestor: 1 },
    trees,
    role_census: [
      { agent_type: 'claude_code', role: 'tool-shell', n: 14_212, cpu_s: 210.4, pct: 71.2 },
      { agent_type: 'codex', role: 'helper', n: 3_120, cpu_s: 12.2, pct: 15.63 },
      { agent_type: 'codex', role: 'probe', n: 2_210, cpu_s: 40.1, pct: 11.07 },
      { agent_type: 'claude_code', role: 'agent', n: 412, cpu_s: 1_802.6, pct: 2.06 },
      { agent_type: 'codex', role: 'sandbox', n: 6, cpu_s: 0.1, pct: 0.03 },
    ],
    buffer: { nodes: 9, cap: 20_000, dropped: 0 },
    workspace: {
      agents: [
        agent(trees[0].nodes[0], lead, leadTree, { subs: [52_877] }),
        agent(trees[1].nodes[0], ideAgent, null),
        agent(trees[0].nodes[1], sub, null, { root: 41_200, parent: 41_200 }),
        agent(trees[2].nodes[0], homeless, null),
      ],
      summary: { agents: 4, shown: 4, with_subagents: 1, with_outside_writes: 1, other_homes: 1, shared_roots: 2, unowned_procs: 1_204, agents_without_records: 0, collapse_depth: 4, rows_cap: 40 },
      buffer: { keys: 871, cap: 60_000, dir_entries: 1_406, dir_entries_cap: 400_000, dropped: 0, parse_errors: 0, capped_tokens: 0, inherited_argv: 212, probes_skipped: 5_330, fed: 41_102 },
      basis: {
        ran_in: 'MEASURED: processes whose working directory is that directory',
        bytes: 'MEASURED per process, not per file: credited to where the process ran',
        named: 'INFERRED from command lines: distinct paths, read / write / delete / named',
        not_seen: 'exact file-level reads and writes would need a collector probe (openat/unlink/rename tracepoints) that is not built',
      },
    },
    basis: {
      node: 'a process whose role is agent, infra or client (roles.py)',
      agent_depth: 'most agents on any root-to-leaf path; 1 = no agent under another agent',
      fanout: 'agents directly below an agent (or a tree’s root) with no other agent between',
      subagent: 'an agent started, through any number of steps, beneath another agent',
    },
  };
}

function build() {
  const nowEpoch = 1_789_400_000; // fixed: the fixture must be byte-stable
  const nowIso = new Date(nowEpoch * 1000).toISOString();

  const feeds = {
    ebpfm: {
      name: 'ebpfm',
      tier: 'process',
      label: 'eBPF process exits (root, every node)',
      cadence: 'event',
      configured: ['/var/log/ebpfm'],
      resolved: ['/var/log/ebpfm'],
      missing: [],
      origin: 'config file',
      env: 'RC_EBPFM_DIR',
      cli: '--ebpfm',
      exists: true,
      status: 'ok',
      required: true,
      n_files: 18,
      bytes: 41_884_226_118,
      days: ['2026-09-13', '2026-09-14'],
      hosts: HOSTS,
      newest_record_ts: nowIso,
      lag_s: 12,
      lag: '12 s',
      max_age_s: 300,
      rows: 7_161_466,
      notice: null,
      backfill: { state: 'done', pct: 100, records: 7_161_466, hours: 168 },
    },
    slurm_jobs: {
      name: 'slurm_jobs',
      tier: 'scheduler',
      label: 'scontrol show job census',
      cadence: '300 s',
      configured: ['log/slurm_jobs'],
      resolved: ['log/slurm_jobs'],
      missing: [],
      origin: 'default',
      env: null,
      cli: null,
      exists: true,
      status: 'stale',
      required: true,
      n_files: 2,
      bytes: 24_884_226_118,
      days: ['2026-09-13', '2026-09-14'],
      hosts: ['holylogin02'],
      newest_record_ts: new Date((nowEpoch - 2_400) * 1000).toISOString(),
      lag_s: 2_400,
      lag: '40 min',
      max_age_s: 600,
      rows: 1_884_226,
      notice: 'newest snapshot is 40 min old; the poller may have died (MinJobAge is 600 s)',
      backfill: null,
    },
    slurm_nodes: {
      name: 'slurm_nodes',
      tier: 'scheduler',
      label: 'scontrol show node',
      cadence: '300 s',
      configured: ['log/slurm_nodes'],
      resolved: ['log/slurm_nodes'],
      missing: [],
      origin: 'default',
      env: null,
      cli: null,
      exists: true,
      status: 'ok',
      required: false,
      n_files: 2,
      bytes: 1_884_226,
      days: ['2026-09-13', '2026-09-14'],
      hosts: ['holylogin02'],
      newest_record_ts: nowIso,
      lag_s: 188,
      lag: '3.1 min',
      max_age_s: 600,
      rows: 88_442,
      notice: null,
      backfill: null,
    },
    dcgm: {
      name: 'dcgm',
      tier: 'scheduler',
      label: 'dcgm-exporter HTTP scrape',
      cadence: '60 s',
      configured: ['/scratch/datasets/rc_measurement/current/dcgm/logs'],
      resolved: [],
      missing: ['/scratch/datasets/rc_measurement/current/dcgm/logs'],
      origin: 'config file',
      env: 'RC_DCGM_DIR',
      cli: '--dcgm',
      exists: false,
      status: 'missing',
      required: false,
      n_files: 0,
      bytes: null,
      days: [],
      hosts: [],
      newest_record_ts: null,
      lag_s: null,
      lag: null,
      max_age_s: 300,
      rows: null,
      notice: 'the dcgm collector is not running on this host; GPU device activity is unavailable',
      backfill: null,
    },
    sdiag: {
      name: 'sdiag',
      tier: 'scheduler',
      label: 'slurmctld scheduler diagnostics',
      cadence: '60 s',
      configured: ['log/sdiag'],
      resolved: [],
      missing: ['log/sdiag'],
      origin: 'default',
      env: null,
      cli: null,
      exists: false,
      status: 'empty',
      required: false,
      n_files: 0,
      bytes: 0,
      days: [],
      hosts: [],
      newest_record_ts: null,
      lag_s: null,
      lag: null,
      max_age_s: 300,
      rows: 0,
      notice: 'log/sdiag is reserved in .gitignore but the symlink does not exist',
      backfill: null,
    },
  };

  /* The plate's minute is the service's last full one: the row before the
   * minute still filling, and its total is that row's sum. */
  const rate = rateRows(nowEpoch);
  const lastFull = Object.fromEntries(CLASSES.map((c) => [c, rate[rate.length - 2][c]]));
  const counted = Object.values(lastFull).filter((v) => v != null);
  const now = {
    exits_per_min: counted.length ? counted.reduce((a, v) => a + v, 0) : null,
    exits_per_min_by_class: lastFull,
    roots: { agent: 1_405, 'ide': 428, human: 951 },
    procs: { agent: 18_442, 'ide': 4_118, human: 1_884 },
    users: { agent: 38, 'ide': 61, human: 142 },
    rss_gb: 418.6,
    autonomous_roots: 478,
  };

  const hosts = HOSTS.map((h, i) => ({
    host: h,
    roots: 118 + i * 61,
    procs: 1_884 + i * 418,
    events: 188_442 - i * 18_000,
    rss_gb: Number((41.2 + i * 18.4).toFixed(1)),
    cpu_pct: Number((188.4 + i * 61.2).toFixed(1)),
    load_1: Number((8.4 + i * 3.1).toFixed(1)),
    d_state: i === 2 ? 14 : i === 4 ? 3 : 0,
    users: 18 + i * 7,
    tcp_open_ext: 88 + i * 24,
    by_class: { agent: 61 + i * 38, 'ide': 22 + i * 12, human: 35 + i * 11 },
  }));

  const submits = Array.from({ length: 14 }, (_, i) => ({
    ts: new Date((nowEpoch - i * 418) * 1000).toTimeString().slice(0, 8),
    user: i % 3 === 0 ? 'thomwg11' : USERS[(i + 1) % USERS.length],
    agent_type: i % 4 === 3 ? null : i % 2 ? 'claude_code' : 'codex',
    tool: i % 5 === 4 ? 'salloc' : 'sbatch',
    job_id: 8_812_400 + i * 17,
    partition: ['gpu', 'sapphire', 'test', 'gpu_requeue'][i % 4],
    gpus: i % 3 === 0 ? 4 : i % 3 === 1 ? 1 : null,
    array: i % 6 === 0 ? '0-499' : null,
  }));

  const live = {
    window: { minutes: BINS, from: new Date((nowEpoch - BINS * 60) * 1000).toISOString(), to: nowIso },
    rate,
    now,
    hosts,
    events: events(nowEpoch, 400),
    submits,
    backfill: { state: 'done', pct: 100, records: 7_161_466, hours: 168 },
  };

  const config = {
    paths: {
      ebpfm_dir: { _value: '/var/log/ebpfm', _origin: 'config file: dashboard/rc_dashboard.toml' },
      slurm_jobs_dir: { _value: 'log/slurm_jobs', _origin: 'default' },
      dcgm_dir: { _value: '/scratch/datasets/rc_measurement/current/dcgm/logs', _origin: 'env RC_DCGM_DIR' },
      sdiag_dir: { _value: 'log/sdiag', _origin: 'default' },
    },
    window: {
      live_minutes: { _value: 1440, _origin: 'default' },
      bin_s: { _value: 60, _origin: 'default' },
      skew_s: { _value: 120, _origin: 'default' },
    },
    limits: {
      tail_events: { _value: 400, _origin: 'default' },
      trajectory_buffer: { _value: 4096, _origin: 'config file: dashboard/rc_dashboard.toml' },
      export_max_rows: { _value: 5_000_000, _origin: 'default' },
    },
    server: {
      host: { _value: '127.0.0.1', _origin: 'cli --host' },
      port: { _value: 8787, _origin: 'cli --port' },
      static_dir: { _value: 'dashboard/web/dist', _origin: 'default' },
    },
  };

  return {
    _generated_by: 'mock/gen_fixture.mjs -- development fixture, never served in production',
    now_epoch: nowEpoch,
    config,
    feeds,
    panels: {
      panels: {
        tool_mix: toolMix(),
        io_process: ioProcess(),
        resources: resources(nowEpoch),
        sandbox: sandbox(),
        trajectories: trajectories(nowEpoch),
        cascades: cascades(nowEpoch),
        /* An absent panel, so <EmptyState> is verifiable without the backend. */
        gpu_duty: {
          _feed: 'dcgm',
          _status: 'missing',
          _present: false,
          _paths: ['/scratch/datasets/rc_measurement/current/dcgm/logs'],
          _notice: 'the dcgm collector is not running on this host',
        },
      },
      full_built_at: nowIso,
      purposes: ['poll', 'slurm_mon', 'slurm_sub', 'build', 'git', 'data', 'compute', 'runtime', 'editor', 'filesearch', 'other'],
    },
    live,
    trajectories: trajectories(nowEpoch),
    trajectory_detail: trajectoryDetail(nowEpoch, trajectories(nowEpoch)),
  };
}

const out = join(HERE, 'api.json');
writeFileSync(out, JSON.stringify(build(), null, 1) + '\n');
console.log('wrote', out);
