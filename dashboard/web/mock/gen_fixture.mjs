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
 *   - a `denied` sandbox slice, distinct from `unsandboxed`
 *   - a trajectory list dominated by one user
 */
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/* ------------------------------------------------------------- shaping */

const BINS = 1440; // 24 h at one bin per minute
const HOSTS = ['holylogin01', 'holylogin02', 'holylogin03', 'boslogin01', 'boslogin02', 'boslogin03'];
const CLASSES = ['agent', 'human-vscode', 'human'];

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

const BASE = { agent: 210, 'human-vscode': 96, human: 34 };

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
      agent_type: cls === 'agent' ? (wob(i, 7) < 0.7 ? 'claude_code' : 'codex') : cls === 'human-vscode' ? 'vscode' : null,
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
      approval: cls === 'agent' ? (wob(i, 19) < 0.31 ? 'bypassed' : 'supervised') : 'unknown',
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
  'human-vscode': {
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

const CALLS = { agent: 4_812_446, 'human-vscode': 1_964_118, human: 384_902 };
const CPU = { agent: 91_244.8, 'human-vscode': 48_611.2, human: 26_408.4 };

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
    shell_kinds: {
      agent: { 'bash -lc': 3_114_882, 'bash -c': 812_004, 'sh -c': 94_221, interactive: 1_442 },
      'human-vscode': { 'bash -lc': 402_118, 'bash -c': 288_904, 'sh -c': 41_002, interactive: 96_884 },
      human: { 'bash -lc': 18_224, 'bash -c': 9_116, 'sh -c': 2_884, interactive: 141_006 },
    },
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
    'human-vscode': [64_118.4, 18_204.6, 402_118.8, 24_118.2, 84.1, 1_964_118, 1_142_884],
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
      /* agent egress is measured; human-vscode is partly measured; human TCP
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

function resources() {
  const by_class = {};
  const shape = {
    agent: [4_812_446, 91_244.8, 62_118.4, 188_442.6],
    'human-vscode': [1_964_118, 48_611.2, 31_884.2, 44_118.8],
    human: [384_902, 26_408.4, 18_442.6, 9_884.2],
  };
  for (const c of CLASSES) {
    const [events, cpu, run, wait] = shape[c];
    const cov = c === 'agent' ? 58.4 : c === 'human-vscode' ? 31.2 : 12.6;
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
      cls: CLASSES[i % 3],
      events: Math.round(400_000 / (i + 1)),
      sched_wait_s: Number((18_000 / (i + 1.3)).toFixed(1)),
      sched_run_s: Number((42_000 / (i + 1.1)).toFixed(1)),
      schedstat_ratio: Number((2.3 + i * 0.4).toFixed(2)),
      dstate_wait_s: i % 4 === 3 ? null : Number((880 / (i + 1.7)).toFixed(1)),
      coverage_pct: Number(Math.max(4, 68 - i * 4.8).toFixed(1)),
    })),
    scope: { hosts: HOSTS.length, days: 2, window: '2026-09-13 00:00 to 2026-09-14 18:00' },
    cpu_basis: 'self cpu_s (utime+stime of the exiting process); child_cpu_s NEVER added',
  };
}

function sandbox() {
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    by_class_events: {
      agent: { sandboxed: 2_018_442, unsandboxed: 1_142_884, denied: 1_488_226, unknown: 162_894 },
      'human-vscode': { sandboxed: 18_442, unsandboxed: 884_112, denied: 962_118, unknown: 99_446 },
      human: { sandboxed: 0, unsandboxed: 188_442, denied: 182_118, unknown: 14_342 },
    },
    approval_by_class_events: {
      agent: { supervised: 3_118_442, bypassed: 1_488_226, unknown: 205_778 },
      'human-vscode': { supervised: 0, bypassed: 0, unknown: 1_964_118 },
      human: { supervised: 0, bypassed: 0, unknown: 384_902 },
    },
    by_class_trees: {
      agent: { sandboxed: 412, unsandboxed: 288, denied: 664, unknown: 41 },
      'human-vscode': { sandboxed: 4, unsandboxed: 188, denied: 214, unknown: 22 },
      human: { sandboxed: 0, unsandboxed: 488, denied: 402, unknown: 61 },
    },
    approval_by_class_trees: {
      agent: { supervised: 884, bypassed: 478, unknown: 43 },
      'human-vscode': { supervised: 0, bypassed: 0, unknown: 428 },
      human: { supervised: 0, bypassed: 0, unknown: 951 },
    },
    ancestry_matrix: [
      {
        ancestry: 'bwrap',
        sandbox: 'sandboxed',
        n: 1_884_226,
        dstate_wait_s: 4_118.6,
        dstate_coverage_pct: 62.4,
        reading: 'real confinement: a bwrap ancestor and a confined mount namespace',
      },
      {
        ancestry: 'bwrap',
        sandbox: 'unsandboxed',
        n: 188_442,
        dstate_wait_s: 218.4,
        dstate_coverage_pct: 58.1,
        reading: 'capability probe: bwrap ran, the namespace did not take',
      },
      {
        ancestry: 'none',
        sandbox: 'unsandboxed',
        n: 2_118_884,
        dstate_wait_s: 1_884.2,
        dstate_coverage_pct: 41.2,
        reading: 'unconfined, as reported',
      },
      {
        ancestry: 'none',
        sandbox: 'denied',
        n: 2_632_462,
        dstate_wait_s: null,
        dstate_coverage_pct: 0,
        reading: 'unreadable: another user’s process, not evidence of anything',
      },
    ],
    sandbox_src: { argv: 1_884_226, cgroup: 918_442, ns: 662_118, unreadable: 2_632_462 },
    sandbox_detail: { seccomp_filtered_renderers: 884_226, note: 'every Electron renderer is seccomp-filtered' },
    agent_only: { claude_code: { sandboxed_pct: 48.2 }, codex: { sandboxed_pct: 88.4 } },
    states: { sandbox: ['sandboxed', 'unsandboxed', 'denied', 'unknown'], approval: ['supervised', 'bypassed', 'unknown'] },
    notes: {
      denied: '`denied` is the collector failing to read the field, NOT a process running unconfined',
      axes: 'sandbox and approval_mode are independent: a confined tree can still bypass approvals',
    },
  };
}

/* Sporadicity: launch dir vs reach. Awkward states on purpose: one class
 * (human-vscode) that never writes outside, a session in another user's home,
 * a ranking dominated by one user, and trees launched at / counted apart. */
function sporadicity() {
  const zones = ['cwd', 'home', 'home-dot', 'tool-state', 'tmp', 'shared', 'other-home', 'system', 'other'];
  const lab = '/n/holylabs/LABS/kempner_lab';
  const proj = (u, p) => `/n/home0${(u.length % 4) + 1}/${u}/${p}`;
  const rows = {};
  const ranks = { radius: [], writes: [], dirs: [], other_home: [] };
  const spec = [
    // user, class, project, [dir, zone, refs, writes, cd, radius], sandbox, approval
    ['thomwg11', 'agent', 'llm-eval', [
      [`${lab}/Users/thomwg11/ckpt`, 'shared', 41, 18, true, 3],
      ['/n/netscratch/kempner_lab/thomwg11/runs', 'shared', 22, 22, false, 3],
      ['/tmp/eval-cache', 'tmp', 9, 4, false, 3],
    ], 'unsandboxed', 'bypassed'],
    ['thomwg11', 'agent', 'llm-eval', [
      ['/n/home04/thomwg11/llm-eval-old', 'home', 12, 3, true, 1],
      ['/n/home04/thomwg11/.cache/huggingface', 'home-dot', 6, 0, false, 1],
    ], 'sandboxed', 'supervised'],
    ['aparker', 'agent', 'genomics/pipeline', [
      ['/n/home02/lzhang/shared-refs', 'other-home', 3, 0, true, 2],
      [`${lab}/ref/hg38`, 'shared', 14, 0, false, 4],
    ], 'unsandboxed', 'supervised'],
    ['thomwg11', 'agent', 'sweeps', [
      ['/n/netscratch/kempner_lab/thomwg11/sweep-12', 'shared', 88, 61, false, 3],
    ], 'denied', 'bypassed'],
    ['dwei', 'human', 'thesis', [
      ['/n/home03/dwei/data', 'home', 7, 0, false, 1],
      ['/scratch/dwei/tmp', 'shared', 4, 2, false, 3],
    ], 'unknown', 'unknown'],
    ['jsilva', 'human-vscode', 'webapp', [
      ['/tmp/vite-cache', 'tmp', 5, 0, false, 3],
    ], 'unknown', 'unknown'],
  ];
  spec.forEach(([user, cls, p, dirs, sandbox, approval], i) => {
    const sid = `holylogin0${(i % 3) + 1}|2026-09-14|${cls}|apid:${41000 + i * 37}`;
    const outside = dirs;
    const far = outside.reduce((a, d) => Math.max(a, d[5]), 0);
    const anchor = proj(user, p);
    const depth = anchor.split('/').filter(Boolean).length;
    const zoneAgg = {};
    for (const [, z, r, w] of outside) {
      const e = (zoneAgg[z] ||= { dirs: 0, refs: 0, writes: 0 });
      e.dirs += 1;
      e.refs += r;
      e.writes += w;
    }
    rows[sid] = {
      sid, key_type: cls === 'agent' ? 'apid' : 'tty', host: `holylogin0${(i % 3) + 1}`, user,
      actor3: cls, agent_type: cls === 'agent' ? (i % 2 ? 'codex' : 'claude_code') : cls === 'human-vscode' ? 'vscode' : null,
      agent_pid: cls === 'agent' ? 41000 + i * 37 : null, anchor, anchor_src: i % 3 ? 'root' : 'mode',
      home: proj(user, '').replace(/\/$/, ''), n_records: 120 + i * 53, wall_s: 1800 + i * 611,
      radius: far, root_escape: far >= depth, write_radius: outside.some((d) => d[3]) ? Math.max(...outside.filter((d) => d[3]).map((d) => d[5])) : null,
      outside_dirs: outside.length, outside_refs: outside.reduce((a, d) => a + d[2], 0),
      outside_writes: outside.reduce((a, d) => a + d[3], 0),
      other_home_dirs: outside.filter((d) => d[1] === 'other-home').length, zones: zoneAgg,
      top_outside: outside.map(([dir, zone, refs, writes, cd, radius]) => ({ dir, zone, refs, writes, cd, radius })),
      more_outside: i === 0 ? 4 : 0, dirs_dropped: 0, sandbox, approval,
    };
  });
  const order = (k) => Object.values(rows).sort((a, b) => b[k] - a[k] || a.sid.localeCompare(b.sid)).map((r) => r.sid);
  ranks.radius = order('radius');
  ranks.writes = order('outside_writes').filter((sid) => rows[sid].outside_writes > 0);
  ranks.dirs = order('outside_dirs');
  ranks.other_home = order('other_home_dirs').filter((sid) => rows[sid].other_home_dirs > 0);
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    zones,
    zone_meaning: {
      cwd: "inside the session's launch directory",
      'tool-state': "an agent's or editor's own state (~/.claude, ~/.codex, ~/.vscode-server, /tmp/claude-*)",
      'other-home': "ANOTHER user's home directory",
    },
    radius_buckets: ['0', '1', '2', '3+', 'root'],
    contained: ['cwd', 'tool-state'],
    by_class: {
      agent: {
        sessions: 1_412, sessions_outside: 318, sessions_write_outside: 61, sessions_other_home: 4, no_anchor: 1_096,
        refs: { cwd: 48_812, home: 1_204, 'home-dot': 3_118, 'tool-state': 22_441, tmp: 2_206, shared: 9_884, 'other-home': 12, system: 1_877, other: 140 },
        writes: { cwd: 6_118, home: 88, 'home-dot': 41, 'tool-state': 4_002, tmp: 611, shared: 1_204 },
        sessions_by_zone: { cwd: 1_301, home: 88, 'home-dot': 212, 'tool-state': 1_122, tmp: 141, shared: 96, 'other-home': 4, system: 77, other: 9 },
        write_sessions_by_zone: { cwd: 744, home: 12, 'home-dot': 9, 'tool-state': 1_011, tmp: 33, shared: 21 },
        radius: { '0': 1_094, '1': 61, '2': 72, '3+': 58, root: 127 },
        write_radius: { '0': 744, '1': 9, '2': 14, '3+': 18, root: 20 },
      },
      'human-vscode': {
        sessions: 488, sessions_outside: 21, sessions_write_outside: 0, sessions_other_home: 0, no_anchor: 5_911,
        refs: { cwd: 4_118, 'tool-state': 18_224, tmp: 61, system: 44 },
        writes: { cwd: 212, 'tool-state': 88 },
        sessions_by_zone: { cwd: 462, 'tool-state': 488, tmp: 14, system: 9 },
        write_sessions_by_zone: { cwd: 41, 'tool-state': 102 },
        radius: { '0': 467, root: 21 },
        write_radius: { '0': 41 },
      },
      human: {
        sessions: 61, sessions_outside: 38, sessions_write_outside: 9, sessions_other_home: 0, no_anchor: 18,
        refs: { cwd: 2_204, home: 611, 'home-dot': 41, tmp: 188, shared: 3_012, system: 902 },
        writes: { cwd: 140, home: 12, tmp: 9, shared: 466 },
        sessions_by_zone: { cwd: 58, home: 22, 'home-dot': 6, tmp: 14, shared: 19, system: 31 },
        write_sessions_by_zone: { cwd: 33, home: 3, tmp: 4, shared: 7 },
        radius: { '0': 23, '1': 9, '2': 4, '3+': 3, root: 22 },
        write_radius: { '0': 33, '1': 2, root: 7 },
      },
    },
    ranks,
    rows,
    rank_meaning: {
      radius: 'furthest reach: levels up from the launch dir',
      writes: 'most writes outside the launch dir',
      dirs: 'most distinct directories outside the launch dir',
      other_home: "named a directory in ANOTHER user's home (every such session)",
    },
    dominant_user_share_pct: { radius: 61.5, writes: 82.4, dirs: 57.1, other_home: 100 },
    sessions_outside_total: 377,
    dirs: [
      { dir: '/n/home02/lzhang/shared-refs', zone: 'other-home', sessions: 4, users: 1, classes: { agent: 4 }, refs: 12, writes: 0, cds: 4 },
      { dir: '/n/netscratch/kempner_lab/thomwg11', zone: 'shared', sessions: 41, users: 1, classes: { agent: 41 }, refs: 1_204, writes: 612, cds: 18 },
      { dir: `${lab}/ref`, zone: 'shared', sessions: 22, users: 4, classes: { agent: 17, human: 5 }, refs: 388, writes: 0, cds: 6 },
      { dir: '/tmp/eval-cache', zone: 'tmp', sessions: 14, users: 2, classes: { agent: 14 }, refs: 96, writes: 41, cds: 0 },
      { dir: '/tmp/vite-cache', zone: 'tmp', sessions: 3, users: 1, classes: { 'human-vscode': 3 }, refs: 15, writes: 0, cds: 0 },
    ],
    dirs_total: 211,
    rollup_depth: 4,
    coverage: {
      records: { agent: 4_812_446, 'human-vscode': 1_964_118, human: 384_902 },
      with_args: { agent: 4_766_102, 'human-vscode': 1_958_004, human: 381_118 },
      args_truncated: { agent: 9_118, 'human-vscode': 402 },
      cwd_null: { agent: 211, human: 3_012 },
      cwd_read: { agent: 1_288_414, 'human-vscode': 14_882, human: 241_906 },
      tokens: {
        agent: { kept: 101_244, program: 3_884_102, pseudo: 611_884, harness: 188_412, junk: 402, bare_root: 18_442, relative_resolved: 14_118, relative_unresolved: 9_884 },
        'human-vscode': { kept: 22_488, program: 1_611_402, pseudo: 1_002_118, relative_resolved: 204, relative_unresolved: 112 },
        human: { kept: 8_011, program: 141_204, pseudo: 1_402, bare_root: 611, relative_resolved: 88, relative_unresolved: 9_412 },
      },
      dedup_dropped: 412_884,
    },
    buffer: { sessions: 9_611, untracked_records: 0, refs_dropped: 0, dedup_index_evicted: 0 },
    notes: {
      evidence: 'Paths come from argv plus each process\'s recorded cwd; every number is a floor on reach.',
      radius: 'Levels up from the launch directory to the nearest directory containing the reference; `root` means / is the only common ancestor.',
    },
  };
}

function trajectories(nowEpoch) {
  const rows = {};
  const sids = [];
  for (let i = 0; i < 28; i++) {
    const cls = i % 7 === 6 ? 'human' : i % 3 === 1 ? 'human-vscode' : 'agent';
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
      agent_type: cls === 'agent' ? (i % 3 ? 'claude_code' : 'codex') : cls === 'human-vscode' ? 'vscode' : null,
      n_events: nEvents,
      cpu_s: Number((nEvents * (0.004 + ((i % 7) * 0.02))).toFixed(2)),
      distinct_tools: 6 + ((i * 3) % 34),
      chain_runs: runs.length + (i % 11),
      wall_s: 1_800 + i * 4_400,
      peak_rss_mb: Number((188 + i * 61.4).toFixed(1)),
      io_rd_mb: Number((418.2 / (i * 0.2 + 1)).toFixed(1)),
      io_wr_mb: Number((88.4 / (i * 0.3 + 1)).toFixed(1)),
      sandbox: cls === 'agent' ? (i % 4 === 0 ? 'sandboxed' : i % 4 === 1 ? 'unsandboxed' : i % 4 === 2 ? 'denied' : 'unknown') : 'unknown',
      approval: cls === 'agent' ? (i % 3 === 0 ? 'bypassed' : 'supervised') : 'unknown',
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
    rank_by: 'events',
    rank_meaning: 'events = process exits attributed to the session key',
    ranks: { events: by('n_events'), cpu_s: by('cpu_s'), distinct_tools: by('distinct_tools'), chain_runs: by('chain_runs') },
    rows,
    by_class_counts: {
      agent: sids.filter((s) => rows[s].actor3 === 'agent').length,
      'human-vscode': sids.filter((s) => rows[s].actor3 === 'human-vscode').length,
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

/* ------------------------------------------------------------- the bundle */

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

  const now = {
    exits_per_min: 344,
    roots: { agent: 1_405, 'human-vscode': 428, human: 951 },
    procs: { agent: 18_442, 'human-vscode': 4_118, human: 1_884 },
    users: { agent: 38, 'human-vscode': 61, human: 142 },
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
    by_class: { agent: 61 + i * 38, 'human-vscode': 22 + i * 12, human: 35 + i * 11 },
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
    rate: rateRows(nowEpoch),
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
        resources: resources(),
        sandbox: sandbox(),
        sporadicity: sporadicity(),
        trajectories: trajectories(nowEpoch),
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
  };
}

const out = join(HERE, 'api.json');
writeFileSync(out, JSON.stringify(build(), null, 1) + '\n');
console.log('wrote', out);
