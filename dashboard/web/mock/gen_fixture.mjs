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
 *   - a quantile whose every value is exactly 0 (`all_zero`: not measured, never 0)
 *   - an `exited before read` (key `denied`) slice, distinct from `unsandboxed`,
 *     and a session whose every sandbox attempt failed
 *   - a trajectory list dominated by one user
 *   - rows with phases, every one: every kind, guesses, a cut call log, a
 *     running row for each live edge (`running`, `idle`, `unknown`); an agent
 *     of a type with no rules of its own (Copilot), read by the general rule;
 *     and a person and an IDE, read from their terminals (`driver: terminal`)
 *   - tasks above the phases: labels quoted from a commit, a log entry, and a
 *     pull request, and built from names; a lone stretch; gaps with a call the
 *     agent moved past still running, with a call still running that has no
 *     preview, and with none; an open task at each live edge; and one agent
 *     over two day rows
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
/* A field on every record and exactly 0 on every one: a counter the collector could not fill, sent
 * as 0s. The service flags it (`Quant.as_dict`, rc_dashboard/reducers/__init__.py), and the page
 * reads it as not measured. */
const allZero = (n, coverage = 100) => ({
  ...quant(n, [0, 0, 0, 0, 0, 0, 0, 0], coverage),
  p1: 0,
  min: 0,
  zero_pct: 100,
  all_zero: true,
  suspect: 'every value is exactly 0 -- treat as not measured, not as a measured zero',
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
  const tools = {};
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
    // the busiest tools by every resource, each row carrying all of them (reducers/tools.py `_tools`)
    const io = TOOLS.slice(0, 12).map(([t], i) => (t === 'rsync' || t === 'cat' ? 900 : 60) * (1 + wob(i, 7)));
    const net = TOOLS.slice(0, 12).map(([t], i) => (t === 'git' || t === 'rsync' ? 240 * (1 + wob(i, 9)) : 0));
    const ioTot = io.reduce((a, v) => a + v, 0) * 1.3;
    const netTot = net.reduce((a, v) => a + v, 0) * 1.3;
    tools[c] = TOOLS.slice(0, 12).map(([t, b], i) => {
      const cp = Number((22 / (i + 1.3)).toFixed(2));
      const cup = Number((cp * (t === 'pgrep' || t === 'ps' ? 0.02 : 1.4)).toFixed(2));
      const mem = 20 + (t === 'node' || t === 'python3' ? 900 : 60) * wob(i, 11);
      return {
        tool: t,
        bucket: b,
        calls: Math.round((calls * cp) / 100),
        calls_pct: cp,
        cpu_s: Number(((cpu * cup) / 100).toFixed(2)),
        cpu_pct: cup,
        io_mb: Number(io[i].toFixed(2)),
        io_pct: Number(((100 * io[i]) / ioTot).toFixed(1)),
        net_mb: Number(net[i].toFixed(2)),
        net_pct: Number(((100 * net[i]) / netTot).toFixed(1)),
        mem_mean_mb: Number(mem.toFixed(1)),
        mem_max_mb: Number((mem * (2 + wob(i, 13))).toFixed(1)),
      };
    });
  }
  return {
    ...ENV('ebpfm', EBPFM_PATHS),
    buckets: BUCKETS,
    by_class,
    tools,
    dedup: {
      dropped: 1_884_662,
      dropped_pct: 21.4,
      rule: 'resolved shell dropped iff its captured direct child is the command it resolved to',
    },
    cpu_basis: 'self cpu_s, each process once; child_cpu_s NEVER added (it double-counts the tree)',
    exits: exits(),
  };
}

/* How the tool mix's processes ended (reducers/tools.py `exits`). The awkward
 * states the panel has to word: a test runner failing most of its runs, a
 * command not found (127), a timeout's SIGKILL, grep's exit 1 that is an
 * answer, `| head`'s SIGPIPE, and records with no ending at all. */
function exits() {
  const row = (tool, bucket, calls, nonzero, answer = 0, signal = 0, pipe = 0, none = 0) =>
    ({ tool, bucket, calls, nonzero, answer, signal, pipe, none });
  const block = (tools, codes, signals) => {
    const sum = (k) => tools.reduce((a, r) => a + r[k], 0);
    const buckets = {};
    for (const r of tools) {
      const b = (buckets[r.bucket] ??= { b: r.bucket, calls: 0, nonzero: 0, answer: 0, signal: 0, pipe: 0, none: 0 });
      for (const k of ['calls', 'nonzero', 'answer', 'signal', 'pipe', 'none']) b[k] += r[k];
    }
    return {
      calls: sum('calls') * 9, nonzero: sum('nonzero'), answer: sum('answer'), signal: sum('signal'),
      pipe: sum('pipe'), none: sum('none'), codes, signals,
      tools: tools.filter((r) => r.nonzero + r.signal > 0),
      buckets: Object.values(buckets),
    };
  };
  const agent = [
    row('pytest', 'interpreter', 41_884, 18_412, 0, 211),
    row('python', 'interpreter', 188_442, 9_884, 0, 1_204, 0, 12),
    row('make', 'build', 24_118, 6_114),
    row('nvidia-smi', 'poll/probe', 4_118, 4_118),
    row('timeout', 'other', 2_884, 0, 0, 1_942),
    row('grep', 'search', 212_118, 1_884, 41_220, 0, 8_118),
    row('git', 'git', 141_204, 4_212),
    row('squeue', 'slurm-query', 88_412, 118, 0, 0, 0, 4),
    row('cat', 'files', 302_118, 412, 0, 0, 21_884),
  ];
  const human = [
    row('make', 'build', 8_118, 1_204),
    row('python', 'interpreter', 12_884, 884, 0, 412),
    row('grep', 'search', 21_412, 212, 6_118),
    row('vim', 'files', 4_118, 12, 0, 88),
  ];
  const ide = [
    row('node', 'runtime', 88_412, 1_204, 0, 2_118),
    row('git', 'git', 41_118, 884),
    row('rg', 'search', 64_118, 0, 18_442),
  ];
  return {
    by_class: {
      agent: block(agent, [[1, 29_412], [127, 4_206], [2, 6_118], [124, 1_118]], [[9, 2_412], [15, 941], [11, 4]]),
      human: block(human, [[1, 1_412], [2, 818], [130, 88]], [[2, 412], [15, 88]]),
      ide: block(ide, [[1, 2_088]], [[15, 2_118]]),
    },
    by_agent_type: {
      claude_code: block(agent.slice(0, 6), [[1, 21_884], [127, 3_118], [2, 4_412]], [[9, 1_884], [15, 412]]),
      codex: block(agent.slice(3), [[1, 7_528], [127, 1_088], [124, 1_118]], [[9, 528], [15, 529]]),
    },
    answers: ['ag', 'cmp', 'diff', 'egrep', 'fgrep', 'grep', 'rg', 'zgrep'],
    basis: 'a process\'s own wait status, after the shell dedup above',
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
  // processes per bin, then CPU, runqueue wait and major faults per process
  const PER_BIN = {
    agent: [2_100, 0.042, 0.0009, 0.018],
    'ide': [960, 0.11, 0.0011, 0.31],
    human: [340, 0.36, 0.0021, 0.42],
  };
  const RSS = { agent: 4_400, 'ide': 2_200, human: 38_500 };
  const by_class = {};
  const resident = {};
  for (const [ci, c] of CLASSES.entries()) {
    const [n, cpu, wait, maj] = PER_BIN[c];
    const row = { cpu_s: [], sched_wait_s: [], maj_flt: [] };
    const res = { rss_mb: [] };
    for (let i = 0; i < N; i++) {
      const w = diurnal(i * 10) * (0.7 + 0.6 * wob(i, 40 + ci));
      const k = Math.round(n * w);
      const gap = out(i) || (c === 'human' && i % 23 === 0);
      // one long-lived agent exiting: its whole life's CPU in a single bin
      row.cpu_s.push(gap ? null : Number((k * cpu * (c === 'agent' && i === 97 ? 18 : 1)).toFixed(3)));
      row.sched_wait_s.push(gap ? null : Number((k * wait * (1 + 3 * wob(i, 50 + ci))).toFixed(3)));
      row.maj_flt.push(gap ? null : Math.round(k * maj));
      res.rss_mb.push(out(i) ? null : Number((RSS[c] * (0.8 + 0.4 * diurnal(i * 10))).toFixed(1)));
    }
    by_class[c] = row;
    resident[c] = res;
  }
  // an NFS hiccup in bins 71 to 75: every tool stalls at once
  const stall = (i, j) =>
    out(i) ? null : Number(((i > 70 && i < 76 ? 40 : 0.4 * wob(i, 60 + j)) / (j + 1)).toFixed(3));
  return {
    bin_s: BIN,
    t,
    by_class,
    stall_by_tool: Object.fromEntries(TOOLS.slice(0, 6).map(([tool], j) => [tool, t.map((_, i) => stall(i, j))])),
    stall_other: t.map((_, i) => stall(i, 6)),
    resident,
    skipped: { no_ts: 0, future: 0, clipped_bins: 0 },
  };
}

function resources(nowEpoch) {
  const by_class = {};
  /* events, cpu_s, sched_run_s, sched_wait_s */
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
      quants: {
        cpu_s: quant(events, [0.001, 0.002, 0.006, 0.028, 0.184, 4.82, 3_118.4, 0.019], 100, events > 2_000_000),
        /* the IDE's peak memory is 0 on every exit, as in a capture whose collector read
           mm->hiwater_rss after the kernel had cleared task->mm: not measured, never 0 MB */
        peak_rss_mb: c === 'ide'
          ? allZero(events, 96.8)
          : quant(events, [1.8, 2.9, 5.4, 18.2, 84.6, 1_218.4, 41_882.6, 42.1], 96.8),
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
      meaning: 'sandboxed = ran in its own namespaces; Landlock/seccomp-only sandboxes are invisible, containers count',
      denied: "'exited before it was checked' (key: denied) is NOT 'not sandboxed'",
      failed: "'sandbox failed to start': the wrapper exited non-zero and no wrapped command ran under it",
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
  // any other agent type is a CLI as far as its argv says (normalize._surface); a person or an IDE has none
  const s = SURFACES[agentType]?.[i % 5] ?? (agentType ? ['terminal', 'interactive'] : null);
  return { surface: s ? s[0] : null, surface_detail: s ? s[1] : null };
};

/* ------------------------------------------------------------------ phases
 * What `reducers/phase_segment.py` serves on an agent row: `n_calls`, and a
 * `phases` block of a handful of stretches that tile the session. Built here
 * from the row's index alone, in closed form, so the fixture stays byte-stable.
 *
 * Across the rows: every kind; a loop with guessed slices, and a run and a
 * build that begin at one; a guess; waits on Slurm, a process, a file, a fixed sleep, and
 * something else; a mixed phase; a call that outlived its phase; an idle gap;
 * an open last phase on the running rows (the one the census calls idle has
 * none: a call still running would contradict it); housekeeping, with commands
 * under a process that never exited on some rows, among the top five shapes on
 * most of them and past it on one (`never_exited` counts them either way, R43);
 * one row whose call log was cut;
 * and a session of more than nine phases, for the line that elides its middle.
 *
 * A script is a row's phases in order: `k` the kind, `w` its share of the time
 * left once the guesses and idle gaps have theirs (those have their own length,
 * as the segmenter's rules fix them: a guess is 20 s to 5 min, an idle gap 5 min
 * or more), `n` its calls, and `top` the commands that ran most, with how many
 * calls each. `lead` makes a phase begin at a guessed slice, for the reason it
 * gives, `leadS` seconds long (a guess's own length when left out, 20 s to 5 min).
 * A row's own factor scales the counts, so rows on one script do not read alike.
 *
 * Above the phases, tasks (`reducers/phase_tasks.py`, which makes a block's `v` 2).
 * A spec that carries `task` begins one, and the specs after it are its phases
 * until the next that does, so the tasks tile the phases as the service's do. A
 * task is `{label, source, ref, names, lone}`: a `source` of `commit`, `log`, or
 * `pr` makes its label a quote, and none a label built from names; `ref` is the
 * number of a pull request edited by its number (`gh pr edit 235`), and empty for
 * one that was created (`gh pr create`), which has none yet; `lone` sets a
 * stretch of a few calls apart. A task never begins at a guess or an idle gap,
 * which belong to the task before it. An idle spec with `gap` is a cutting gap,
 * past the hour a task cuts at, and the task before it keeps that phase while the
 * task after it says the gap; one with `s` is that many seconds, which an hour
 * or less leaves inside its task. Its `alive` lists the calls the agent moved past
 * that were still running as it began, as `[preview, name]`: the preview is `null`
 * for a call still running, which has none (R65), and the name is the one the
 * service gives the call in the task's gap, which the idle phase does not carry.
 * Each script has its headline, in `PHASE_HEADLINES`.
 *
 * One agent over two day rows (`DAY_PAIR`) is the last two rows. Both serve one
 * block, of the whole session, which lists its `days`, and each task and phase has
 * the `sid` of the day that holds its start (`trajectories.day_of`). One task runs
 * across midnight. */
const BASIS = 'read from the commands the agent ran and the words it wrote in them'; // `phase_segment.BASIS`
// `trajectories.TERMINAL_BASIS`: what a block says it was read from where no agent ran the session
const TERMINAL_BASIS = 'read from the commands run in its terminals and the words typed in them';
const CUT_GAP_S = 3600; // `phase_tasks.DEFAULTS.gap_s`: a gap this long with nothing the agent waits on cuts a task
const NEVER_EXITED = 'under a process that never exited';
/* Whose calls a row's block is read from (`PhaseBlock.driver`): an agent's, by its own rules or the general rule any
 * other agent is read by, and where no agent ran the session (a person, an IDE), its terminals' commands. Every row has
 * a block. */
const driverOf = (cls) => (cls === 'agent' ? 'agent' : 'terminal');
const PHASE_SCRIPTS = [
  // reading, a guess, a test loop whose pauses are guessed, and a run that begins at a guess. The first task is
  // quoted from the commit that closes it, the second is built from names
  [
    { k: 'explore', w: 2, n: 22, task: { label: 'Read io chunks one at a time', source: 'commit', names: ['src/io.py', 'tests/test_io.py'] }, top: [['rg -n "def run_step" src/', 9], ['ls -la src/', 6], ["sed -n '1,80p' src/train.py", 5]] },
    { k: 'edit_guess', guess: 'a pause after reading, before the test run' },
    { k: 'test', w: 1, n: 6, top: [['python -m pytest tests -x -q', 4], ['ruff check src', 2]] },
    { k: 'loop', w: 4, n: 14, top: [['python -m pytest tests/test_io.py -x -q', 6]] },
    { k: 'git', w: 0.4, n: 5, top: [['git status --short', 2], ['git diff --stat', 2], ['git commit -am "Read io chunks one at a time"', 1]] },
    { k: 'explore', w: 1, n: 8, task: { label: 'Run src/train.py · config/base.yaml', names: ['src/train.py', 'config/base.yaml'] }, top: [['cat src/io.py', 3], ['rg -n "def read_chunk" src/', 3]] },
    { k: 'run', w: 1.5, n: 4, lead: 'a pause after reading, before the run', top: [['python train.py --epochs 1', 3]] },
    { k: 'edit', w: 0.7, n: 4, top: [["sed -i 's/batch=32/batch=64/' config/base.yaml", 3], ["cat > notes.md <<'EOF'", 1]] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 2], ['git diff', 1]] },
  ],
  // a Slurm job: a mixed phase, a wait on Slurm, a run that left a background job going, idle. The first task is
  // quoted from a pull request, and is followed by a cutting gap in which a call the agent moved past is still running
  [
    { k: 'explore', w: 1.5, n: 19, mix: [['explore', 8], ['run', 6], ['build', 5]], task: { label: 'Train on Slurm with an 8-hour limit', source: 'pr', ref: '#235', names: ['jobs/train.sbatch', 'kernels'] }, top: [['ls -la jobs/', 5], ['cat config/base.yaml', 3], ['make -n kernels', 3]] },
    { k: 'edit', w: 1, n: 6, top: [["cat > jobs/train.sbatch <<'EOF'", 2], ["sed -i 's/--time=1:00/--time=8:00/' jobs/train.sbatch", 3]] },
    { k: 'build', w: 1, n: 4, top: [['make -j8 kernels', 3]] },
    { k: 'slurm', w: 0.4, n: 4, top: [['sbatch jobs/train.sbatch', 1], ['squeue -u thomwg11', 2], ['scontrol show job 812', 1]] },
    { k: 'wait', w: 5, pace: ['squeue -j 812 -h -o %T', 120, 'slurm'] },
    { k: 'run', w: 1.5, n: 3, top: [['python eval.py --ckpt out/best.pt', 2], ['python plot.py out/metrics.csv', 1]], past: [['nohup python train.py --epochs 40 > train.log 2>&1 &', 9600]] },
    { k: 'idle', gap: true, alive: [['python eval.py --ckpt out/best.pt', 'eval.py']] },
    { k: 'explore', w: 0.5, n: 6, task: { label: 'Read train.log · out/', names: ['train.log', 'out/'] }, top: [['tail -n 20 train.log', 4], ['ls out/', 2]] },
    { k: 'git', w: 0.2, n: 3, top: [['git status --short', 3]] },
  ],
  // a benchmark: a guess before the tests, a build that begins at a guess (a label on a dark slot lies over
  // its hatching, and the session is short enough that the slice is wide), a wait on a process. The first task is
  // quoted from the title of an entry the agent appended to its PLAN.md, the second is built from names
  [
    { k: 'explore', w: 2, n: 30, task: { label: 'Switch the model to gelu', source: 'log', names: ['src/model.py'] }, top: [['ls -la src/', 10], ['rg -n "class Model" src/', 8], ["sed -n '1,120p' src/model.py", 6]] },
    { k: 'edit_guess', guess: 'a pause after reading, before the test run' },
    { k: 'test', w: 1, n: 6, top: [['python -m pytest -q', 4], ['ruff check src', 2]] },
    { k: 'edit', w: 1, n: 5, top: [["cat > src/model.py <<'EOF'", 3], ["sed -i 's/relu/gelu/' src/model.py", 2], ["cat >> PLAN.md <<'EOF'", 1]] },
    { k: 'build', w: 1, n: 3, lead: 'a pause after reading, before the test run', leadS: 240, top: [['cmake --build build -j 16', 3]] },
    { k: 'run', w: 2, n: 5, task: { label: 'Run bench.py', names: ['bench.py'] }, top: [['python bench.py --size 4096', 3], ['python bench.py --size 8192', 2]] },
    { k: 'wait', w: 1, pace: ['pgrep -f bench.py', 30, 'process'] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 2], ['git log --oneline -5', 1]] },
  ],
  // fourteen phases, so a line of them has to elide its middle. Two tasks built from names, a cutting gap in which a
  // call is still running that has no preview of its own and is named by the programs seen under it, and after it a
  // lone stretch
  [
    { k: 'explore', w: 1.5, n: 18, task: { label: 'Read src/app.py · README.md', names: ['src/app.py', 'README.md'] }, top: [['ls -la', 6], ['rg -n "TODO" .', 6], ['cat README.md', 3]] },
    { k: 'edit', w: 1, n: 6, top: [["sed -i 's/TODO/DONE/' src/app.py", 4]] },
    { k: 'test', w: 1, n: 5, top: [['python -m pytest -q', 5]] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 3]] },
    { k: 'explore', w: 1, n: 9, task: { label: 'Run src/app.py · logs/run.out', names: ['src/app.py', 'logs/run.out'] }, top: [['cat src/app.py', 5], ['ls tests/', 4]] },
    { k: 'edit_guess', guess: 'a pause after reading, before the run' },
    { k: 'run', w: 2.5, n: 4, top: [['python app.py --check', 4]] },
    { k: 'wait', w: 1.5, n: 4, waits: 'output', top: [['tail -n 5 logs/run.out', 4]] },
    { k: 'explore', w: 1, n: 7, top: [['rg -n "check" src/', 7]] },
    { k: 'edit', w: 1, n: 5, top: [["sed -i 's/check/verify/' src/app.py", 5]] },
    { k: 'build', w: 1, n: 4, top: [['make -j4', 4]] },
    { k: 'test', w: 1.5, n: 6, top: [['python -m pytest -q', 6]] },
    { k: 'idle', gap: true, alive: [[null, 'pgrep and sleep']] },
    { k: 'git', w: 0.3, n: 3, task: { lone: true, label: 'Commit src/app.py', names: ['src/app.py'] }, top: [['git status --short', 2], ['git diff --stat', 1]] },
  ],
  // commands that were not recorded, waits with no pace, and a wait on something else. Two tasks built from names,
  // with an idle gap of 25 minutes inside the first, which is too short to cut it
  [
    { k: 'unknown', w: 0.5, n: 3, task: { label: 'Run sweep.py · data/raw.csv', names: ['sweep.py', 'data/raw.csv'] } },
    { k: 'explore', w: 1.5, n: 12, top: [['ls -la data/', 5], ['head -n 3 data/raw.csv', 4], ['wc -l data/raw.csv', 3]] },
    { k: 'wait', w: 1, n: 2, waits: 'sleep', top: [['sleep 600', 2]] },
    { k: 'run', w: 2, n: 4, top: [['python sweep.py --grid coarse', 4]] },
    { k: 'idle', s: 1500 },
    { k: 'test', w: 1, n: 5, top: [['python -m pytest tests -q', 5]] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 3]] },
    { k: 'wait', w: 1.5, pace: ['python check_status.py', 300, 'command'], task: { label: 'Wait on check_status.py · out/summary.txt', names: ['check_status.py', 'out/summary.txt'] } },
    { k: 'explore', w: 0.6, n: 6, top: [['cat out/summary.txt', 4], ['ls out/', 2]] },
  ],
];
/* A script's headline: the project, then the names that two of its tasks or more hold. The third has none that
 * recur, so it is the project alone, and the last has neither, so the service sends `null`. */
const PHASE_HEADLINES = ['proj · src/io.py', 'proj · train.log', 'proj', 'proj · src/app.py', null];

/* One agent over two day rows. It works past midnight: a first task on the first day; one that runs across midnight
 * (the build it holds begins on the first day and ends on the second, so its sid is the first day's and its phases
 * after midnight are the second's); a cutting gap with nothing running; and a Slurm task on the second day. The task
 * across midnight is quoted from a pull request that `gh pr create` made, so it has no `ref`, and its source is the
 * bare `PR`. `i` is the index its counts and its housekeeping follow, and `midAt` the phase midnight falls in, 40% of
 * the way through it. */
const DAY_PAIR = {
  i: 28,
  user: 'thomwg11',
  host: 'boslogin02',
  apid: 4242,
  start: 52_900, // seconds before the present: midnight (EDT, where the fixture's present is 11:33) falls in `midAt`
  span: 32_400,
  midAt: 6,
  headline: 'proj · src/loader.py',
  specs: [
    { k: 'explore', w: 1.5, n: 18, task: { label: 'Edit and test src/loader.py · tests/test_loader.py', names: ['src/loader.py', 'tests/test_loader.py'] }, top: [['rg -n "def decode" src/', 8], ['ls -la src/', 6], ['cat src/loader.py', 4]] },
    { k: 'edit', w: 1, n: 7, top: [["sed -i 's/read(/seek(/' src/loader.py", 4], ["cat > tests/test_loader.py <<'EOF'", 3]] },
    { k: 'test', w: 1, n: 6, top: [['python -m pytest tests/test_loader.py -x -q', 6]] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 2], ['git diff --stat', 1]] },
    { k: 'explore', w: 1, n: 9, task: { label: 'Seek-based frame decoding', source: 'pr', ref: '', names: ['src/loader.py', 'src/video_io.py'] }, top: [['rg -n "seek" src/', 5], ['cat src/video_io.py', 4]] },
    { k: 'edit', w: 1.5, n: 10, top: [["sed -i 's/frames/seek_frames/' src/video_io.py", 6], ["cat > src/seek.py <<'EOF'", 4]] },
    { k: 'build', w: 1, n: 4, top: [['make -j8 ext', 4]] },
    { k: 'test', w: 2, n: 8, top: [['python -m pytest tests -x -q', 8]] },
    { k: 'git', w: 0.4, n: 4, top: [['git status --short', 2], ['gh pr create --title "Seek-based frame decoding"', 1], ['git push origin seek-decode', 1]] },
    { k: 'idle', gap: true, s: 7_900 },
    { k: 'slurm', w: 0.5, n: 4, task: { label: 'Run on Slurm jobs/sweep.sbatch · out/sweep', names: ['jobs/sweep.sbatch', 'out/sweep'] }, top: [['sbatch jobs/sweep.sbatch', 1], ['squeue -u thomwg11', 2], ['scontrol show job 913', 1]] },
    { k: 'wait', w: 3, pace: ['squeue -j 913 -h -o %T', 120, 'slurm'] },
    { k: 'explore', w: 1, n: 6, top: [['tail -n 20 out/sweep/log.txt', 4], ['ls out/sweep', 2]] },
    { k: 'git', w: 0.3, n: 3, top: [['git status --short', 3]] },
  ],
};
/* What a running row ends in, and what its census says, by its place among the running
 * rows. First, a call open that the census sees. Second, none: the census calls the
 * session idle, which an open call would contradict. Third, a call open with no census to
 * say. Fourth, a call open that the census sees, again. A call still running is never named
 * by a child's text (R65): `top` holds the finished calls, and `seen` the programs seen
 * under the open one. */
const LIVE_TAILS = [
  { k: 'slurm', w: 3, n: 2, open: true, seen: ['srun', 'python'], top: [['squeue -u thomwg11', 1]] },
  null,
  { k: 'run', w: 3, n: 1, open: true, seen: ['python'], top: [] },
  { k: 'test', w: 2, n: 1, open: true, seen: ['python', 'git', 'nvidia-smi'], top: [] },
];
const liveNow = (nowEpoch) => [
  { state: 'running', cmds: [['python', 2], ['srun', 1]], cat: 'slurm', at: nowEpoch - 15 },
  { state: 'idle', at: nowEpoch - 15 },
  { state: 'unknown' },
  { state: 'running', cmds: [['pytest', 1]], cat: 'test', at: nowEpoch - 15 },
];

const r3 = (x) => Number(x.toFixed(3));
const sumOf = (pairs) => pairs.reduce((a, [, c]) => a + c, 0);

/* A script's phases laid over `[t0, t1]`, in the shape `phase_segment._phase` gives them. */
function phaseList(i, t0, t1, specs) {
  const f = 1 + (i % 4) * 0.5;
  const sc = (n) => Math.max(1, Math.round(n * f));
  const guessS = 30 + ((i * 29) % 150);
  const idleS = Math.max(300, Math.round((t1 - t0) * 0.12));
  const cutS = Math.max(CUT_GAP_S + 300, idleS); // a cutting gap is past the hour
  const own = specs.map((s) => (s.k === 'edit_guess' ? guessS : s.k === 'idle' ? s.s ?? (s.gap ? cutS : idleS) : null));
  const room = Math.max(0, t1 - t0 - own.reduce((a, d) => a + (d ?? 0), 0));
  const wsum = specs.reduce((a, s, j) => a + (own[j] == null ? s.w : 0), 0);
  let at = t0;
  let began = t0; // where the phase before this one began
  return specs.map((s, j) => {
    const a = at;
    const b = j === specs.length - 1 ? t1 : a + (own[j] ?? (room * s.w) / wsum);
    at = b;
    const before = began;
    began = a;
    const p = {
      kind: s.k, t0: r3(a), t1: r3(b), n: 0, mix: [], top: [], guess: null, slices: null, rounds: null,
      waits_on: null, every_s: null, times: null, every_cmd: null, open: false, seen: null, ran_past: null,
      alive: null,
    };
    if (s.k === 'idle') {
      // calls from the phase before it that were still running as it began: a few seconds in, three seconds apart
      if (s.alive) p.alive = s.alive.map(([cmd], k) => [cmd, r3(before + 2 + 3 * k)]);
      return p;
    }
    if (s.k === 'edit_guess') return { ...p, guess: s.guess };
    const d = b - a;
    let top = (s.top ?? []).map(([c, m]) => [c, sc(m)]);
    let n = sc(s.n ?? 1);
    if (s.pace) {
      const [cmd, every, on] = s.pace;
      p.waits_on = on;
      p.every_s = every;
      p.every_cmd = cmd;
      p.times = Math.max(1, Math.round(d / every));
      top = [[cmd, p.times]];
      n = p.times;
    }
    if (s.waits) p.waits_on = s.waits;
    if (s.k === 'loop') {
      p.rounds = top[0][1];
      const cuts = Math.min(p.rounds - 3, 4);
      const len = Math.min(25 + ((i * 13) % 60), d / (2 * cuts + 2));
      p.slices = Array.from({ length: cuts }, (_, k) => {
        const mid = a + ((k + 1) * d) / (cuts + 1);
        return [r3(mid - len / 2), r3(mid + len / 2)];
      });
      p.guess = `a pause between two runs of ${top[0][0]}`;
      n = Math.max(n, p.rounds + 2);
    }
    if (s.lead) {
      p.slices = [[r3(a), r3(a + Math.min(s.leadS ?? guessS, d * 0.3))]];
      p.guess = s.lead;
    }
    p.mix = (s.mix ?? []).map(([c, m]) => [c, sc(m)]);
    p.top = top;
    p.n = Math.max(n, sumOf(top), sumOf(p.mix));
    p.open = s.open === true;
    p.seen = p.open ? s.seen ?? [] : null;
    p.ran_past = s.past ?? null;
    return p;
  });
}

/* The agent's own commands, by shape and by the minute each began in. `never` of them ran
 * under a process that never exited: counted, and in no minute, and in `never_exited` whether
 * or not their shape is among the top five (R43). */
function phaseHousekeeping(i, t0, t1, never) {
  const K = Math.max(6, Math.min(40, Math.floor((t1 - t0) / 300)));
  const bins = Array.from({ length: K }, (_, k) => [
    Math.floor((t0 + ((k + 0.5) * (t1 - t0)) / K) / 60) * 60,
    1 + ((i * 7 + k * 13) % 9),
  ]);
  const placed = sumOf(bins);
  const part = (share) => Math.floor(placed * share);
  const by = { realpath: part(0.25), uname: part(0.1), ls: part(0.06), other: part(0.04) };
  by.git = placed - by.realpath - by.uname - by.ls - by.other;
  if (never > 0) by[NEVER_EXITED] = never;
  const shapes = Object.entries(by)
    .filter(([, c]) => c > 0)
    .sort((x, y) => y[1] - x[1] || (x[0] < y[0] ? -1 : 1))
    .slice(0, 5);
  return { n: placed + never, shapes, bins, cut: never, never_exited: never };
}

/* The tasks of a script's phases `list`, as `phase_tasks.name_tasks` gives them: a spec that carries `task` begins
 * one, and its range is the phases up to the next. A range that ends in a cutting gap keeps that idle phase, and
 * the task is over where the gap began; the next task says the gap, with the calls the idle phase lists and the
 * name the spec gives each. `cutAt` is the call log's, when it cut the calls: the last task, and any that ends after
 * it, are incomplete. */
function taskList(list, specs, cutAt) {
  const starts = specs.flatMap((s, j) => (s.task ? [j] : []));
  if (starts[0] !== 0) throw new Error('the first phase of a script begins its first task');
  return starts.map((a, j) => {
    const z = j + 1 < starts.length ? starts[j + 1] : specs.length;
    const mine = list.slice(a, z);
    const d = specs[a].task;
    const t1 = specs[z - 1].gap === true ? list[z - 1].t0 : list[z - 1].t1;
    const before = a > 0 && specs[a - 1].gap === true ? list[a - 1] : null;
    return {
      kind: d.lone ? 'lone' : 'task',
      t0: list[a].t0,
      t1,
      n: mine.reduce((sum, p) => sum + p.n, 0),
      label: d.label,
      source: d.source ?? null,
      ref: d.ref ?? '',
      names: [...d.names],
      phases: [a, z],
      open: mine.some((p) => p.open),
      incomplete: cutAt != null && (j === starts.length - 1 || t1 > cutAt),
      gap: before
        ? { s: r3(before.t1 - before.t0), alive: (before.alive ?? []).map(([cmd, since], k) => [cmd, since, specs[a - 1].alive[k][1]]) }
        : null,
      sid: null,
    };
  });
}

/* The sid of the day among `days` whose span holds `t`, else of the nearest, the earlier of two the same
 * distance away (`trajectories.day_of`). */
function dayOf(days, t) {
  let best = null;
  let near = Infinity;
  for (const d of days) {
    const away = t < d.first ? d.first - t : Math.max(0, t - d.last);
    if (away < near) {
      best = d.sid;
      near = away;
    }
  }
  return best;
}

/* What kind of session a block was (`phase_summary.summarize`), from the phases a row draws: its `category`, the
 * `uses` of its work, and a `summary` of the service's first two sentences. The work is the phases' time but idle
 * gaps, waiting (a wait on Slurm is Slurm work), and commands not recorded, and a use that holds half of it or more
 * is the category, else `mixed`. The names its calls held, and how long it waited, which the service says after them,
 * are left out: the fixture's calls hold no names. */
const USE_OF = { explore: 'explore', edit: 'develop', edit_guess: 'develop', test: 'develop', loop: 'develop', build: 'build', run: 'run', slurm: 'hpc', git: 'git' };
const USES = ['explore', 'develop', 'build', 'run', 'hpc', 'git'];
const DID = { explore: 'explored code and files', develop: 'edited and tested code', build: 'built and installed software', run: 'ran programs', hpc: 'ran jobs on Slurm', git: 'managed code in git' };
/* A duration as the page writes it (`dur`, src/lib/format.ts), for the durations a summary holds. */
const durWords = (s) =>
  s < 90 ? `${s.toFixed(s < 10 ? 1 : 0)} s` : s < 5400 ? `${(s / 60).toFixed(s < 600 ? 1 : 0)} min` : s < 172800 ? `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h` : `${(s / 86400).toFixed(1)} d`;
function sessionKind(list, nCalls, tasks, driver = 'agent') {
  const time = {};
  let unrecorded = 0;
  let waited = 0;
  for (const p of list) {
    if (p.kind === 'idle') continue;
    const s = Math.max(0, p.t1 - p.t0);
    if (p.kind === 'unknown') unrecorded += s;
    const use = p.kind === 'wait' ? (p.waits_on === 'slurm' ? 'hpc' : null) : USE_OF[p.kind];
    if (use) time[use] = (time[use] ?? 0) + s;
    else if (p.kind === 'wait') waited += s;
  }
  const uses = USES.filter((u) => time[u] > 0).sort((a, b) => time[b] - time[a] || USES.indexOf(a) - USES.indexOf(b));
  const work = uses.reduce((a, u) => a + time[u], 0);
  const share = (u) => Math.round((1000 * time[u]) / work) / 10;
  const pct = (u) => `${Math.round(share(u))}%`;
  const category = !nCalls
    ? 'none'
    : unrecorded > 0 && unrecorded >= 0.5 * (work + unrecorded)
      ? 'unknown'
      : uses.length
        ? time[uses[0]] >= 0.5 * work
          ? uses[0]
          : 'mixed'
        : waited > 0
          ? 'wait'
          : 'unknown';
  const named = category === 'mixed' ? uses.slice(0, 3) : [uses[0], ...uses.slice(1, 3).filter((u) => time[u] >= 0.1 * work)];
  const what =
    category === 'mixed'
      ? `Mixed work (${durWords(work)}): ${named.map((u) => `${DID[u]} (${pct(u)})`).join(', ')}.`
      : USES.includes(category)
        ? `Mostly ${DID[category]} (${pct(category)} of ${durWords(work)} of work)${named.length > 1 ? `, then ${named.slice(1).map((u) => `${DID[u]} (${pct(u)})`).join(' and ')}` : ''}.`
        : 'Commands not recorded.';
  const k = tasks.filter((t) => t.kind === 'task').length;
  return {
    category,
    uses: uses.map((u) => [u, share(u)]),
    summary: `${what} ${nCalls.toLocaleString('en-US')} ${driver === 'terminal' ? 'commands' : 'tool calls'}${k > 1 ? ` in ${k} tasks` : ''}.`,
  };
}

/* `n_calls` and `phases` for the row `i`, whose session ran `[first, last]`. `live` is its
 * place among the running rows, or -1. `script` is `{specs, headline}` for a session that is no
 * row's own (`DAY_PAIR`), else the script of its place among the rows. `driver` is whose calls
 * they are: an agent's, or a session's terminals' where no agent ran it. */
function phaseFields(i, first, last, live, script = null, driver = 'agent') {
  const t0 = first + 12;
  const cutLog = i === 2; // the log held the first of its calls and counted the rest
  const t1 = cutLog ? t0 + (last - t0) * 0.7 : last;
  const tail = live >= 0 ? LIVE_TAILS[live % LIVE_TAILS.length] : null;
  const k = i % PHASE_SCRIPTS.length;
  const specs = [...(script?.specs ?? PHASE_SCRIPTS[k]), ...(tail ? [tail] : [])];
  const list = phaseList(i, t0, t1, specs);
  const cut = cutLog ? 120 : 0;
  const cut_at = cutLog ? r3(t1 - 20) : null;
  const n_calls = list.reduce((a, p) => a + p.n, 0) + cut;
  const tasks = taskList(list, specs, cut_at);
  tasks.forEach((t, j) => {
    for (let at = t.phases[0]; at < t.phases[1]; at++) {
      list[at].task = j;
      list[at].sid = null;
    }
  });
  return {
    n_calls,
    phases: {
      v: 2,
      basis: driver === 'terminal' ? TERMINAL_BASIS : BASIS,
      driver,
      n_calls,
      cut,
      cut_at,
      // every fourth row has commands under a process that never exited; row 14's few fall below the top five
      housekeeping: phaseHousekeeping(i, t0, t1, i % 4 !== 2 ? 0 : i === 14 ? 5 : 12),
      list,
      tasks,
      headline: script ? script.headline : PHASE_HEADLINES[k],
      ...sessionKind(list, n_calls, tasks, driver),
    },
  };
}

/* One agent over two day rows (`DAY_PAIR`): the rows' ids, first and last records, and own call counts, and the one
 * block both serve, which lists the `days` and gives each task and phase the sid of the day that holds its start.
 * Midnight falls inside the phase `midAt`. */
function dayPair(nowEpoch) {
  const first = nowEpoch - DAY_PAIR.start;
  const { n_calls, phases } = phaseFields(DAY_PAIR.i, first, first + DAY_PAIR.span, -1, DAY_PAIR);
  const mid = phases.list[DAY_PAIR.midAt].t0 + 0.4 * (phases.list[DAY_PAIR.midAt].t1 - phases.list[DAY_PAIR.midAt].t0);
  const id = (date) => `${DAY_PAIR.host}|${date}|agent|apid:${DAY_PAIR.apid}`;
  const days = [
    { sid: id('2026-09-13'), first, last: r3(mid - 4) },
    { sid: id('2026-09-14'), first: r3(mid + 3), last: first + DAY_PAIR.span },
  ];
  for (const x of [...phases.list, ...phases.tasks]) x.sid = dayOf(days, x.t0);
  phases.days = days;
  // each row counts the calls of its own day: the phases that begin on it
  const own = (d) => phases.list.filter((p) => p.sid === d.sid).reduce((a, p) => a + p.n, 0);
  return { days: days.map((d) => ({ ...d, n_calls: own(d) })), phases, n_calls };
}

function trajectories(nowEpoch) {
  const rows = {};
  const sids = [];
  const edges = liveNow(nowEpoch);
  const pair = dayPair(nowEpoch);
  let nRunning = 0; // the running rows so far: each has its own live edge
  // 28 rows of their own, then the two days of one agent (`DAY_PAIR`), whose ids are the service's own form
  for (let i = 0; i < 28 + pair.days.length; i++) {
    const day = i >= 28 ? pair.days[i - 28] : null;
    const g = day ? DAY_PAIR.i : i; // what a row's attributes follow: both days of an agent share them
    const cls = day ? 'agent' : i % 7 === 6 ? 'human' : i % 3 === 1 ? 'ide' : 'agent';
    const live = !day && cls === 'agent' && i % 4 === 1 ? nRunning++ : -1;
    const first = day ? day.first : nowEpoch - 1_800 - i * 4_400 - i * 300;
    const last = day ? day.last : nowEpoch - i * 300;
    const user = day ? DAY_PAIR.user : cls === 'agent' && i % 4 !== 3 ? 'thomwg11' : USERS[(i + 2) % USERS.length];
    const host = day ? DAY_PAIR.host : HOSTS[i % HOSTS.length];
    const sid = day ? day.sid : `period4-${host}-${user}-${1_000 + i * 37}`;
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
    // Row 3 is an agent with no rules of its own (Copilot), read by the general rule; Claude Code and Codex alternate on
    // the rest.
    const agentType = day ? 'claude_code' : cls === 'agent' ? (i === 3 ? 'copilot' : i % 3 ? 'claude_code' : 'codex') : cls === 'ide' ? (i === 4 ? 'cursor' : 'vscode') : null;
    rows[sid] = {
      sid,
      key_type: day ? 'apid' : i % 5 === 4 ? 'cgroup' : 'session_scope',
      host,
      user,
      actor3: cls,
      agent_type: agentType,
      n_events: nEvents,
      cpu_s: Number((nEvents * (0.004 + ((i % 7) * 0.02))).toFixed(2)),
      distinct_tools: 6 + ((i * 3) % 34),
      chain_runs: runs.length + (i % 11),
      wall_s: Math.round(last - first),
      peak_rss_mb: Number((188 + i * 61.4).toFixed(1)),
      io_rd_mb: Number((418.2 / (i * 0.2 + 1)).toFixed(1)),
      io_wr_mb: Number((88.4 / (i * 0.3 + 1)).toFixed(1)),
      net_tx_mb: Number((12.6 / (i * 0.5 + 1)).toFixed(2)),
      net_rx_mb: Number((96.1 / ((i % 7) * 0.4 + 1)).toFixed(2)),
      first,
      last,
      ...(live >= 0
        ? { running: { seen: nowEpoch - 20, n_procs: 3 + (i % 9), rss_mb: 410 + i * 12, census_at: nowEpoch - 15, now: edges[live % edges.length] } }
        : {}),
      sandbox: cls === 'agent' ? (g % 4 === 0 ? 'sandboxed' : g % 4 === 1 ? 'unsandboxed' : g % 4 === 2 ? 'denied' : 'unknown') : 'unknown',
      approval: cls === 'agent' ? (g % 3 === 0 ? 'bypassed' : 'supervised') : 'unknown',
      ...surfaceOf(g, cls === 'agent' ? agentType : null),
      sandbox_ancestry: cls === 'agent' && g % 4 < 2 ? 'bwrap' : 'none',
      autonomous: cls === 'agent' && g % 3 === 0,
      tool_rle: runs,
      chain_truncated: i % 6 === 0,
      purpose_rle: purposeRuns,
      // Every row carries its phases, as the service sends them (tests/test_traj_phases.py): an
      // agent's tool calls, the Copilot row's (3) by the general rule, and a person's and an
      // IDE's (the Cursor row among them) commands, read from their terminals. The two days of
      // one agent serve the one block, each with the count of its own day.
      ...(day ? { n_calls: day.n_calls, phases: pair.phases } : phaseFields(i, first, last, live, null, driverOf(cls))),
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
    basis: 'tool_rle is the run-length encoding of effective_comm in exit order; chains over 4096 runs are truncated',
  };
}

/* The navigator the service builds from every record the read matched (`TreeBuilder.nav`, proctree.py): process
 * starts per bin by purpose, stacked as the chart draws them, and the landmarks. Here it is built from the
 * hand-built tree, so it holds what the tree holds, the counted rows and groups too, and a session that has
 * a navigator can draw its chart. Bins of 30 s from `lo`; a start before it is counted and not binned. */
const NAV_PURPOSES = ['poll', 'runtime', 'filesearch', 'slurm_mon', 'slurm_sub', 'build', 'compute', 'data', 'other'];
const NAV_PURPOSE_OF = {
  squeue: 'slurm_mon', grep: 'poll', sleep: 'poll', git: 'other', python: 'compute', make: 'build',
  cc1plus: 'build', rg: 'filesearch', cat: 'filesearch', sbatch: 'slurm_sub', bash: 'runtime',
};
function navOf(tree, lo, hi) {
  const w = 30;
  const cells = new Map(); // bin * 16 + purpose index -> starts
  let before = 0;
  const add = (t, cmd, n = 1) => {
    if (t == null || !n) return;
    if (t < lo) {
      before += n;
      return;
    }
    const k = Math.floor((t - lo) / w) * 16 + NAV_PURPOSES.indexOf(NAV_PURPOSE_OF[cmd] ?? 'other');
    cells.set(k, (cells.get(k) ?? 0) + n);
  };
  // `n` starts over [a, z], an even share to each bin it touches: counted rows and groups have no start of their own
  const spread = (a, z, n, cmd) => {
    if (a == null || z == null || !n) return;
    const bins = Math.floor((z - a) / w) + 1;
    for (let i = 0; i < bins; i++) add(a + i * w, cmd, Math.floor(n / bins) + (i < n % bins ? 1 : 0));
  };
  const fails = [];
  let first = null;
  let longest = null;
  const walk = (e, root) => {
    if (e.k === 'p') {
      if (e.cmd) add(e.start, e.cmd);
      if (e.exit_code || e.signal) fails.push([e.start, e.end, e.pid, e.cmd, e.exit_code ?? null, e.signal ?? null]);
      if (!root && e.role === 'tool-shell' && (!first || e.start < first.t)) {
        first = { t: e.start, end: e.end, pid: e.pid, cmd: e.cmdline ?? e.cmd, via: 'tool' };
      }
      if (!root && e.dur != null && (!longest || e.dur > longest.dur)) {
        longest = { t: e.start, end: e.end, pid: e.pid, cmd: e.cmd, dur: e.dur };
      }
      (e.kids ?? []).forEach((k) => walk(k, false));
    } else if (e.k === 'g') {
      spread(e.start, e.end, e.count, e.motif?.[0]);
    } else if (e.k === 'agg' || e.k === 'more') {
      spread(e.start, e.end, e.n, 'other');
    }
  };
  walk(tree, true);
  fails.sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0));
  const bins = [...cells]
    .map(([k, c]) => [Math.floor(k / 16), k % 16, c])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const perMinute = new Map();
  for (const [b, , c] of bins) {
    const m = Math.floor((lo + b * w) / 60) * 60;
    perMinute.set(m, (perMinute.get(m) ?? 0) + c);
  }
  let busiest = null;
  for (const [t, n] of perMinute) if (!busiest || n > busiest.n) busiest = { t, n };
  return {
    starts: bins.reduce((a, [, , c]) => a + c, 0),
    no_start: 0,
    purposes: NAV_PURPOSES,
    density: { lo, w, n: Math.ceil((hi - lo) / w) + 1, before, bins },
    busiest,
    first_tool: first,
    longest,
    failures: { n: fails.length, listed: fails.length, items: fails },
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
    last: members[members.length - 1].start,
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
    // Bounds, as the backend serves them (proctree._bounds): with no start, the
    // earliest time of anything below it (t0); with no exit, when its last
    // child started (t1), past its last tick. A group's last member started at
    // its `last`; a count says nothing of when its last one started.
    const lows = kids.map((k) => (k.k === 'p' ? (k.start ?? k.t0) : k.start)).filter((v) => v != null);
    const forks = kids
      .filter((k) => k.k === 'g' || (k.k === 'p' && (k.link === 'ppid' || k.link === 'index')))
      .map((k) => (k.k === 'g' ? k.last : k.start))
      .filter((v) => v != null);
    if (e.start == null && e.pid != null && lows.length) e.t0 = Math.min(...lows);
    const hi = forks.length ? Math.max(...forks) : null;
    if (!e.ev && e.pid != null && hi != null && (e.end == null || hi > e.end)) e.t1 = hi;
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
    nav: navOf(tree, t0, t0 + 5_390),
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
    trees,
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

/* ------------------------------------------------------------------- users
 * The Users tab's roster and portraits (`rc_dashboard/portraits.py`), composed from this fixture's own trajectory rows
 * as the service composes them from its reducers, so a session in a portrait opens a row the Trajectories tab lists.
 * A session is an agent row, and the two day rows of one agent (`DAY_PAIR`) are one session; its calls by category
 * are its phases' calls, the agent's own words its quoted tasks, and its time at the scheduler its runs of sbatch and
 * squeue. An agent of a type whose commands are not read (the Copilot row) has calls of null, not 0. Times of day are
 * in EDT, the fixture's own zone, so the file is the same on any machine. The traits follow `portraits.RULES`, and
 * their words are the service's (`portraits.TRAITS`). */
const PORTRAIT_RULES = {
  interactive: 0.6, scripted: 0.5, app: 0.5, bypass: 0.5, auto: 0.5, sandboxed: 0.5, parallel: 3, long_s: 7200,
  short_n: 10, short_s: 600, explore: 0.6, test: 0.15, build: 0.15, slurm: 0.1, wait: 0.25, night: 0.25,
  night_min: 60, min_calls: 20, mode: 0.5, mostly: 0.5, project: 0.5,
};
// what the agents' actions are for (`portraits.USES`), and the ways of driving them (`portraits._MODES`)
const PORTRAIT_USES = [['explore', ['explore']], ['develop', ['edit', 'test']], ['build', ['build']], ['run', ['run']], ['hpc', ['slurm']], ['git', ['git']]];
const PORTRAIT_MODES = [['terminal', ['terminal']], ['script', ['script']], ['extension', ['extension']], ['app', ['desktop', 'web']]];
const PORTRAIT_TRAITS = [
  ['interactive', "works in the agent's own terminal", '', "60% or more of the sessions are driven from a terminal: the agent's own interactive screen"],
  ['scripted', 'runs agents from scripts', '', '50% or more of the sessions are driven from a script: claude -p, codex exec, an SDK, or an MCP server, with no one at a terminal'],
  ['app', 'drives agents from an app or an IDE', '', '50% or more of the sessions are driven from the desktop app, an IDE extension, or the web'],
  ['bypass', 'launches with a bypass flag', 'crit', '50% or more of the sessions whose launch is known were started with a bypass flag (--dangerously-skip-permissions, --yolo, ...)'],
  ['auto', 'launches in auto mode', 'warn', '50% or more of the sessions whose launch is known were started in auto mode'],
  ['sandboxed', 'tool calls run sandboxed', 'ok', '50% or more of the sessions whose tool calls were judged had some or all of them sandboxed'],
  ['multi_agent', 'uses more than one agent', '', 'sessions of two agent types or more'],
  ['parallel', 'keeps several sessions open at once', '', '3 sessions or more open at the same moment'],
  ['long', 'long sessions', '', 'the median session is open 2 h or longer'],
  ['short', 'many short sessions', '', '10 sessions or more, and the median one is open 10 min or less'],
  ['explorer', 'agents mostly read and search', '', "60% or more of the calls explore (ls, cat, rg, grep, find, ...), of 20 calls or more; the agent's own Read tool runs no command and is not counted"],
  ['tester', 'agents test often', '', '15% or more of the calls run tests, of 20 calls or more'],
  ['builder', 'agents build often', '', '15% or more of the calls build, compile or install, of 20 calls or more'],
  ['slurm', 'agents drive Slurm', '', 'an agent session submitted a job, or 10% or more of the calls are Slurm commands'],
  ['poller', 'agents poll', 'warn', 'an agent session polled the scheduler for 10 min or more, or 25% or more of the calls only wait (sleep, ps, pgrep, squeue loops), of 20 calls or more'],
  ['committer', 'agents commit their work', '', "an agent wrote a commit message or a pull request's title in a command"],
  ['overnight', 'agents active overnight', '', '25% or more of the minutes in which a call began fall between 00:00 and 06:00, local time, of 60 such minutes or more'],
];
const CALL_CATS = ['test', 'slurm', 'run', 'build', 'edit', 'git', 'wait', 'explore', 'unknown']; // `phase_rules.CATS`
// a phase's calls, by the category of the same name; a loop's are tests, and a guess and an idle gap hold none
const CAT_OF_PHASE = { explore: 'explore', edit: 'edit', build: 'build', test: 'test', loop: 'test', run: 'run', slurm: 'slurm', wait: 'wait', git: 'git', unknown: 'unknown' };
// whether a session's tool calls ran sandboxed (the Security tab's count), from the row's own record
const SESSION_SANDBOX = { sandboxed: 'sandboxed', unsandboxed: 'unsandboxed', denied: 'failed', unknown: 'no_calls' };
const EDT = -4 * 3600;
const localOf = (epoch) => new Date((epoch + EDT) * 1000);
const qAt = (vals, p) => {
  if (!vals.length) return null;
  const v = [...vals].sort((a, b) => a - b);
  return v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))];
};
const countOf = (xs) => {
  const m = {};
  for (const x of xs) m[x] = (m[x] ?? 0) + 1;
  return Object.fromEntries(Object.entries(m).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)));
};
const addInto = (dst, src) => {
  for (const [k, v] of Object.entries(src)) dst[k] = (dst[k] ?? 0) + v;
  return dst;
};
const runsOf = (r, tool) => r.tool_rle.filter(([t]) => t === tool).reduce((a, [, n]) => a + n, 0);

/* The agent rows as sessions: one per row, and one for the two day rows of `DAY_PAIR`, which share a block. */
function portraitSessions(traj) {
  const groups = [];
  const byBlock = new Map();
  for (const r of Object.values(traj.rows)) {
    if (r.actor3 !== 'agent') continue;
    const block = Array.isArray(r.phases?.list) ? r.phases : null;
    if (block?.days && byBlock.has(block)) {
      byBlock.get(block).rows.push(r);
      continue;
    }
    const g = { rows: [r], block };
    if (block?.days) byBlock.set(block, g);
    groups.push(g);
  }
  return groups.map(({ rows, block }, i) => {
    const head = rows[0];
    const first = Math.min(...rows.map((r) => r.first));
    const last = Math.max(...rows.map((r) => r.last));
    // every agent's calls are read (`portraits._session`): Claude Code and Codex by their own rules, any other by the general rule
    const read = block != null;
    const cats = {};
    const phaseS = {};
    const minutes = new Set();
    const commands = {};
    for (const p of block?.list ?? []) {
      phaseS[p.kind] = r3((phaseS[p.kind] ?? 0) + (p.t1 - p.t0));
      const c = CAT_OF_PHASE[p.kind];
      if (c && p.n) cats[c] = (cats[c] ?? 0) + p.n;
      // the minutes a phase's calls began in: no more than its calls, spread over it
      const m = p.n ? Math.min(p.n, Math.ceil((p.t1 - p.t0) / 60)) : 0;
      for (let k = 0; k < m; k++) minutes.add(Math.floor((p.t0 + (k * (p.t1 - p.t0)) / m) / 60));
      for (const [cmd, n] of p.top) commands[cmd] = (commands[cmd] ?? 0) + n;
    }
    const tasks = (block?.tasks ?? []).filter((t) => t.kind === 'task');
    const sbatch = rows.reduce((a, r) => a + runsOf(r, 'sbatch'), 0);
    const squeue = rows.reduce((a, r) => a + runsOf(r, 'squeue'), 0);
    return {
      _minutes: minutes,
      _commands: commands,
      _words: countOf(tasks.filter((t) => t.source).map((t) => t.source)),
      _tasks: tasks,
      _lone: (block?.tasks ?? []).filter((t) => t.kind === 'lone').length,
      _slurm: { jobs: sbatch, queries: squeue, polling: squeue >= 8, rejected: i % 11 === 5 ? 1 : 0 },
      user: head.user,
      host: head.host,
      pid: 2_000 + i * 17,
      agent_type: head.agent_type,
      surface: head.surface,
      surface_detail: head.surface_detail,
      approval: head.approval,
      sandbox: SESSION_SANDBOX[head.sandbox] ?? 'no_calls',
      first,
      last,
      wall_s: r3(last - first),
      calls: read ? (block?.days ? block.n_calls : rows.reduce((a, r) => a + (r.n_calls ?? 0), 0)) : null,
      processes: rows.reduce((a, r) => a + r.n_events, 0),
      cpu_s: r3(rows.reduce((a, r) => a + r.cpu_s, 0)),
      rd_mb: r3(rows.reduce((a, r) => a + r.io_rd_mb, 0)),
      wr_mb: r3(rows.reduce((a, r) => a + r.io_wr_mb, 0)),
      net_tx_mb: r3(rows.reduce((a, r) => a + r.net_tx_mb, 0)),
      net_rx_mb: r3(rows.reduce((a, r) => a + r.net_rx_mb, 0)),
      peak_rss_mb: Math.max(...rows.map((r) => r.peak_rss_mb)),
      running: rows.some((r) => r.running),
      sid: rows[rows.length - 1].sid,
      sids: rows.map((r) => r.sid),
      project: block?.headline ? block.headline.split(' · ')[0] : null,
      headline: block?.headline ?? null,
      category: block?.category ?? null,
      uses: block?.uses ?? null,
      summary: block?.summary ?? null,
      tasks: block ? tasks.length : null,
      quoted: block ? tasks.filter((t) => t.source).length : null,
      phase_s: block ? phaseS : null,
      cats: Object.fromEntries(CALL_CATS.filter((c) => cats[c]).map((c) => [c, cats[c]])),
    };
  });
}

/* The most sessions open at one moment; a start is counted before an end at the same instant. */
function peakOpen(ss) {
  const ev = ss.flatMap((s) => [[s.first, -1], [s.last, 1]]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let cur = 0;
  let peak = 0;
  for (const [, d] of ev) {
    cur -= d;
    peak = Math.max(peak, cur);
  }
  return peak;
}

/* The traits a row holds, as `portraits.traits` gives them. */
function portraitTraits(row) {
  const R = PORTRAIT_RULES;
  const got = {};
  const of = (k, n) => `${k} of ${n} sessions`;
  const n = row.sessions;
  const surf = row.surfaces;
  const knownS = n - (surf.unknown ?? 0);
  if (knownS) {
    if ((surf.terminal ?? 0) >= R.interactive * knownS) got.interactive = of(surf.terminal ?? 0, knownS);
    if ((surf.script ?? 0) >= R.scripted * knownS) got.scripted = of(surf.script ?? 0, knownS);
    const app = (surf.desktop ?? 0) + (surf.extension ?? 0) + (surf.web ?? 0);
    if (app >= R.app * knownS) got.app = of(app, knownS);
  }
  const ap = row.approval;
  const knownA = (ap.bypassed ?? 0) + (ap.auto ?? 0) + (ap.supervised ?? 0);
  if (knownA && (ap.bypassed ?? 0) >= R.bypass * knownA) got.bypass = of(ap.bypassed ?? 0, knownA);
  if (knownA && (ap.auto ?? 0) >= R.auto * knownA) got.auto = of(ap.auto ?? 0, knownA);
  const sb = row.sandbox;
  const knownB = ['sandboxed', 'partial', 'failed', 'unsandboxed'].reduce((a, k) => a + (sb[k] ?? 0), 0);
  const iso = (sb.sandboxed ?? 0) + (sb.partial ?? 0);
  if (knownB && iso >= R.sandboxed * knownB) got.sandboxed = of(iso, knownB);
  const types = Object.keys(row.agent_types).filter(Boolean);
  if (types.length >= 2) got.multi_agent = `${types.length} agent types`;
  if (row.peak_open >= R.parallel) got.parallel = `up to ${row.peak_open}`;
  const w = row.wall_p50_s;
  if (w != null && w >= R.long_s) got.long = `median ${(w / 3600).toFixed(1)} h`;
  if (n >= R.short_n && w != null && w <= R.short_s) got.short = `${n} sessions, median ${Math.round(w / 60)} min`;
  const logged = Object.values(row.cats).reduce((a, b) => a + b, 0);
  const share = (c) => (logged >= R.min_calls ? (row.cats[c] ?? 0) / logged : 0);
  const pctTxt = (p) => (p >= 10 ? String(Math.round(p)) : p.toFixed(1));
  const num = (v) => v.toLocaleString('en-US');
  const callsOf = (c) => `${pctTxt(100 * share(c))}% of ${num(logged)} calls`;
  if (share('explore') >= R.explore) got.explorer = callsOf('explore');
  if (share('test') >= R.test) got.tester = callsOf('test');
  if (share('build') >= R.build) got.builder = callsOf('build');
  const sl = row.slurm ?? {};
  if (sl.jobs) got.slurm = `${num(sl.jobs)} job${sl.jobs === 1 ? '' : 's'} submitted`;
  else if (share('slurm') >= R.slurm) got.slurm = callsOf('slurm');
  if (sl.polling_sessions) got.poller = `${sl.polling_sessions} session${sl.polling_sessions === 1 ? '' : 's'} polled the queue for ${(sl.polling_s / 3600).toFixed(1)} h`;
  else if (share('wait') >= R.wait) got.poller = callsOf('wait');
  const words = row.words;
  if (words.commit || words.pr) {
    got.committer = [
      words.commit ? `${words.commit} commit message${words.commit === 1 ? '' : 's'}` : null,
      words.pr ? `${words.pr} pull request${words.pr === 1 ? '' : 's'}` : null,
    ].filter(Boolean).join(', ');
  }
  if (row.active_min >= R.night_min) {
    const night = row.hours.slice(0, 6).reduce((a, b) => a + b, 0);
    if (night >= R.night * row.active_min) got.overnight = `${pctTxt((100 * night) / row.active_min)}% of ${num(row.active_min)} active minutes`;
  }
  return PORTRAIT_TRAITS.filter(([id]) => id in got).map(([id, label, tone]) => ({ id, label, tone, detail: got[id] }));
}

/* How a user leverages their agents, as `portraits.leverage` gives it: each part by PORTRAIT_RULES, null where the
 * numbers do not say it. */
function portraitLeverage(row) {
  const R = PORTRAIT_RULES;
  const n = row.sessions;
  const surf = row.surfaces;
  const knownS = n - (surf.unknown ?? 0);
  let mode = { id: 'unknown', n: 0, of: n };
  if (knownS) {
    let top = null;
    for (const [m, xs] of PORTRAIT_MODES) {
      const k = xs.reduce((a, x) => a + (surf[x] ?? 0), 0);
      if (!top || k > top[1]) top = [m, k]; // the first of a tie
    }
    mode = { id: top[1] >= R.mode * knownS ? top[0] : 'mixed', n: top[1], of: knownS };
  }
  const ap = row.approval;
  const b = ap.bypassed ?? 0;
  const au = ap.auto ?? 0;
  const knownA = b + au + (ap.supervised ?? 0);
  const autonomy = knownA
    ? { id: b >= R.bypass * knownA ? 'bypass' : au >= R.auto * knownA ? 'auto' : 'no_flag', bypassed: b, auto: au, of: knownA }
    : null;
  const cats = row.cats;
  const actions = PORTRAIT_USES.reduce((a, [, cs]) => a + cs.reduce((x, c) => x + (cats[c] ?? 0), 0), 0);
  let uses = [];
  let mostly = 0;
  if (row.calls != null && actions >= R.min_calls) {
    uses = PORTRAIT_USES.map(([u, cs]) => [u, cs.reduce((x, c) => x + (cats[c] ?? 0), 0)])
      .filter(([, k]) => k > 0)
      .map(([u, k], i) => [u, k, i])
      .sort((x, y) => y[1] - x[1] || x[2] - y[2])
      .map(([u, k]) => ({ id: u, n: k, pct: Number(((100 * k) / actions).toFixed(1)) }));
    if (uses[0].n >= R.mostly * actions) mostly = 1;
    else if (uses.length > 1 && uses[0].n + uses[1].n >= R.mostly * actions) mostly = 2;
  }
  const logged = Object.values(cats).reduce((a, x) => a + x, 0);
  const waiting = logged >= R.min_calls && (cats.wait ?? 0) >= R.wait * logged
    ? { n: cats.wait, of: logged, pct: Number(((100 * cats.wait) / logged).toFixed(1)) }
    : null;
  const project = row.projects.length
    ? { name: row.projects[0][0], n: row.projects[0][1], of: n, mostly: row.projects[0][1] >= R.project * n }
    : null;
  let when = null;
  if (row.active_min) {
    let h = 0;
    for (let i = 1; i < 24; i++) if (row.hours[i] > row.hours[h]) h = i; // the earliest of a tie
    when = {
      busiest: h,
      minutes: row.hours[h],
      night_pct: row.active_min >= R.night_min
        ? Number(((100 * row.hours.slice(0, 6).reduce((a, x) => a + x, 0)) / row.active_min).toFixed(1))
        : null,
    };
  }
  return { mode, autonomy, parallel: row.peak_open >= R.parallel ? row.peak_open : null, uses, actions, mostly, waiting, project, when };
}

/* One user's row over their sessions, the roster's and the head of their portrait (`grid` adds weekday by hour). */
function portraitRow(user, ss, grid = false) {
  const minutes = new Set(ss.flatMap((s) => [...s._minutes]));
  const hours = Array(24).fill(0);
  const cells = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const m of [...minutes].sort((a, b) => a - b)) {
    const d = localOf(m * 60);
    hours[d.getUTCHours()] += 1;
    cells[(d.getUTCDay() + 6) % 7][d.getUTCHours()] += 1;
  }
  const read = ss.filter((s) => s.calls != null);
  const cats = {};
  for (const s of ss) addInto(cats, s.cats);
  const words = {};
  for (const s of ss) addInto(words, s._words);
  const walls = ss.map((s) => s.wall_s);
  const projects = countOf(ss.map((s) => s.project).filter(Boolean));
  const polling = ss.filter((s) => s._slurm.polling);
  const jobs = ss.reduce((a, s) => a + s._slurm.jobs, 0);
  const queries = ss.reduce((a, s) => a + s._slurm.queries, 0);
  const rejected = ss.reduce((a, s) => a + s._slurm.rejected, 0);
  const pollS = polling.map((s) => r3(s.wall_s * 0.25));
  const longest = polling.length ? polling[pollS.indexOf(Math.max(...pollS))] : null;
  const slurm = jobs || queries
    ? {
        sessions: ss.filter((s) => s._slurm.jobs || s._slurm.queries).length, jobs, rejected, asked: jobs + rejected,
        queries, queries_per_job: jobs ? Number((queries / jobs).toFixed(1)) : null, cancels: 0,
        parsable: Math.round(jobs * 0.8), parsable_pct: jobs ? Number(((100 * Math.round(jobs * 0.8)) / (jobs + rejected)).toFixed(2)) : null,
        polling_sessions: polling.length, polling_s: r3(pollS.reduce((a, b) => a + b, 0)),
        longest_loop: longest
          ? { from: longest.first + 600, to: longest.first + 600 + Math.max(...pollS), polls: longest._slurm.queries,
              span_s: Math.max(...pollS), every_s: Number((Math.max(...pollS) / longest._slurm.queries).toFixed(1)), polling: true }
          : null,
        gpu_h: null, gpu_h_known: 0,
      }
    : null;
  const row = {
    user,
    sessions: ss.length,
    running: ss.filter((s) => s.running).length,
    hosts: [...new Set(ss.map((s) => s.host))].sort(),
    agent_types: countOf(ss.map((s) => s.agent_type ?? '')),
    surfaces: countOf(ss.map((s) => s.surface ?? 'unknown')),
    approval: countOf(ss.map((s) => s.approval ?? 'unknown')),
    sandbox: countOf(ss.map((s) => s.sandbox)),
    calls: read.length ? read.reduce((a, s) => a + s.calls, 0) : null,
    calls_sessions: read.length,
    calls_p50: qAt(read.map((s) => s.calls), 0.5),
    cats: Object.fromEntries(CALL_CATS.filter((c) => cats[c]).map((c) => [c, cats[c]])),
    words,
    processes: ss.reduce((a, s) => a + s.processes, 0),
    cpu_s: r3(ss.reduce((a, s) => a + s.cpu_s, 0)),
    rd_mb: r3(ss.reduce((a, s) => a + s.rd_mb, 0)),
    wr_mb: r3(ss.reduce((a, s) => a + s.wr_mb, 0)),
    net_tx_mb: r3(ss.reduce((a, s) => a + s.net_tx_mb, 0)),
    net_rx_mb: r3(ss.reduce((a, s) => a + s.net_rx_mb, 0)),
    peak_rss_mb: Math.max(...ss.map((s) => s.peak_rss_mb)),
    wall_s: r3(walls.reduce((a, b) => a + b, 0)),
    wall_p50_s: qAt(walls, 0.5),
    wall_max_s: Math.max(...walls),
    first: Math.min(...ss.map((s) => s.first)),
    last: Math.max(...ss.map((s) => s.last)),
    active_min: minutes.size,
    hours,
    peak_open: peakOpen(ss),
    projects: Object.entries(projects).slice(0, 5),
    projects_total: Object.keys(projects).length,
    slurm,
    ...(grid ? { grid: cells } : {}),
  };
  row.traits = portraitTraits(row);
  row.leverage = portraitLeverage(row);
  return row;
}

const PORTRAIT_BASIS = {
  session: 'one agent run: an agent process seen at the root of its tree, with every collector key folded into it (as in the Agents tab’s list of sessions), and its rows on several days together',
  traits: 'plain statements, each from one rule over these numbers; the rules are served beside them, and none is a judgement',
};

/* `{roster, portraits}`: what `/api/users` and `/api/user?name=` answer, by user. */
function users(nowEpoch, traj) {
  const sessions = portraitSessions(traj);
  const byUser = new Map();
  for (const s of sessions) {
    if (!byUser.has(s.user)) byUser.set(s.user, []);
    byUser.get(s.user).push(s);
  }
  const rows = [...byUser].map(([u, ss]) => portraitRow(u, ss));
  rows.sort((a, b) => b.sessions - a.sessions || (b.calls ?? 0) - (a.calls ?? 0) || (a.user < b.user ? -1 : 1));
  const total = (k) => rows.reduce((acc, r) => addInto(acc, r[k]), {});
  const fleet = {
    users: rows.length,
    sessions: sessions.length,
    sessions_per_user_p50: qAt(rows.map((r) => r.sessions), 0.5),
    wall_p50_s: qAt(sessions.map((s) => s.wall_s), 0.5),
    calls_p50: qAt(sessions.filter((s) => s.calls != null).map((s) => s.calls), 0.5),
    active_min_p50: qAt(rows.map((r) => r.active_min).filter(Boolean), 0.5),
    peak_open_p50: qAt(rows.map((r) => r.peak_open), 0.5),
    running: rows.reduce((a, r) => a + r.running, 0),
    agent_types: total('agent_types'),
    surfaces: total('surfaces'),
    approval: total('approval'),
    sandbox: total('sandbox'),
    cats: total('cats'),
    words: total('words'),
    traits: countOf(rows.flatMap((r) => r.traits.map((t) => t.id))),
  };
  const window = { minutes: 1440, from: new Date((nowEpoch - 86_400) * 1000).toISOString(), to: new Date(nowEpoch * 1000).toISOString() };
  const catalogue = PORTRAIT_TRAITS.map(([id, label, tone, rule]) => ({ id, label, tone, rule }));
  const shared = { ...ENV('ebpfm', EBPFM_PATHS), window, traits: catalogue, rules: PORTRAIT_RULES, basis: PORTRAIT_BASIS };
  const portraits = {};
  for (const [user, ss] of byUser) {
    const newest = [...ss].sort((a, b) => b.last - a.last);
    const timeS = {};
    const callsBy = {};
    for (const s of newest) addInto(timeS, s.phase_s ?? {});
    for (const s of newest) addInto(callsBy, s.cats);
    const tasks = newest.flatMap((s) =>
      s._tasks.map((t) => ({
        label: t.label, source: t.source, ref: t.ref, names: t.names, t0: t.t0, t1: t.t1, n: t.n, open: t.open,
        sid: t.sid ?? s.sid, agent_type: s.agent_type, project: s.project,
      })),
    ).sort((a, b) => b.t0 - a.t0);
    const commands = {};
    for (const s of newest) addInto(commands, s._commands);
    portraits[user] = {
      ...shared,
      user,
      found: true,
      fleet_of: 'window',
      fleet,
      row: portraitRow(user, ss, true),
      phases: {
        time_s: Object.fromEntries(Object.entries(timeS).map(([k, v]) => [k, r3(v)]).sort((a, b) => b[1] - a[1])),
        calls: callsBy,
        sessions: newest.filter((s) => s.phase_s).length,
        of: newest.filter((s) => s.calls != null).length,
      },
      tasks: {
        n: tasks.length,
        lone: newest.reduce((a, s) => a + s._lone, 0),
        quoted: countOf(tasks.filter((t) => t.source).map((t) => t.source)),
        recent: tasks.slice(0, 40),
        listed_of: tasks.length,
      },
      sessions: newest.map((s) => Object.fromEntries(Object.entries(s).filter(([k]) => !k.startsWith('_')))),
      sessions_total: newest.length,
      commands: Object.entries(commands).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, 8),
      tz: 'EDT',
    };
  }
  return { roster: { ...shared, users: rows, fleet, unattributed: 0 }, portraits };
}

/* The node tier right now, and over the window (rc_dashboard/nodehist.py).
 * What it has to show: a slow node (its probes ten times the rest), an NFS
 * hiccup in the same bins as the resources series' stall, a host whose
 * snapshots stopped for 40 minutes (a hole, not a zero), a full /tmp and a
 * /var out of file slots, an account held back at its CPU limit, one whose
 * cgroup was made again, and processes stuck in NFS waits. */
function nodePanel(nowEpoch) {
  const BIN = 600;
  const N = 144;
  const start = Math.floor(nowEpoch / BIN) - N + 1;
  const t = Array.from({ length: N }, (_, i) => (start + i) * BIN);
  const hiccup = (i) => i > 70 && i < 76;
  const holy = (h) => h.startsWith('holy');
  const slow = 'boslogin01';
  const r2 = (v) => Number(v.toFixed(2));
  const series = { hosts: {} };
  for (const [hi, h] of HOSTS.entries()) {
    const gone = (i) => h === 'boslogin02' && i >= 40 && i < 44;
    const f = (fn) => t.map((_, i) => (gone(i) ? null : fn(i)));
    series.hosts[h] = {
      fork_exec_ms: f((i) => r2((h === slow ? 14 : 1.4) * (1 + 0.3 * wob(i, 70 + hi)) * (hiccup(i) && holy(h) ? 9 : 1))),
      getent_ms: f((i) => r2((h === slow ? 220 : 4.2) * (1 + 0.4 * wob(i, 90 + hi)))),
      load_1: f((i) => r2((4 + hi * 1.8) * diurnal(i * 10) * (1 + 0.5 * wob(i, 100 + hi)))),
      nfs_rd_rtt_ms: f((i) => r2((hiccup(i) && holy(h) ? 140 : 1.2) * (1 + 0.5 * wob(i, 120 + hi)))),
      snapshots: f(() => 2),
    };
  }
  const user = (u, cores, quota, held, mem, pids, extra = {}) => ({
    user: u, uid: 20_000 + USERS.indexOf(u), cpu_delta_s: cores == null ? null : r2(cores * 300), gap_s: 300,
    counter_reset: false, cpu_cores: cores, cpu_quota_cores: quota, throttled_delta_s: held,
    mem_gb: mem, pids, pids_max: 4096, ...extra,
  });
  const hosts = HOSTS.map((h, i) => ({
    host: h, ts: new Date((nowEpoch - 60 - i * 37) * 1000).toISOString().slice(0, 19).replace('T', ' '),
    epoch: nowEpoch - 60 - i * 37, synthetic: false, privileged: true,
    load_1: r2(6 + i * 2.1), load_5: r2(5.6 + i * 1.9), load_15: r2(5.1 + i * 1.7),
    mem_total_gb: 503.4, mem_used_gb: r2(140 + i * 31), mem_used_pct: r2((100 * (140 + i * 31)) / 503.4),
    mem_available_gb: r2(503.4 - 140 - i * 31 - 12), cpu_idle_pct: r2(88 - i * 6.2), cpu_iowait_pct: i === 2 ? 11.4 : 0.3,
    procs_running: 4 + i, procs_blocked: i === 2 ? 14 : 0, btime: nowEpoch - 86_400 * (11 + i),
    d_state_procs: i === 2 ? 14 : i === 4 ? 3 : 0,
    d_state: i === 2
      ? Array.from({ length: 14 }, (_, k) => ({
          pid: 3_118_000 + k * 7, ppid: 3_117_990, user: USERS[k % 4],
          wchan: k < 9 ? 'rpc_wait_bit_killable' : k < 12 ? 'nfs_wait_on_request' : '-',
          kstack: k < 3 ? ['rpc_wait_bit_killable', '__rpc_execute', 'rpc_execute', 'nfs4_proc_getattr'] : null,
          args: k % 2 ? 'python train.py --data /n/holyscratch01/lab/era5' : 'ls -la /n/holyscratch01/lab',
        }))
      : i === 4
        ? [{ pid: 88_412, ppid: 1, user: 'rkoshy', wchan: 'folio_wait_bit_common', kstack: null, args: 'tar xzf data.tar.gz' }]
        : [],
    zombie: i === 3 ? 2 : 0,
    fork_exec_ms: h === slow ? 18.4 : r2(1.3 + i * 0.1), stat_local_ms: 0.02, getent_ms: h === slow ? 241.8 : r2(4 + i * 0.2),
    logged_in_users: 12 + i * 5, fds_allocated: 41_118 + i * 1_204, snapshots: 2,
    per_user: i === 0
      ? [user('thomwg11', 3.98, 4, 41.2, 18.4, 412), user('aparker', 0.41, 4, 0, 2.1, 38),
         user('lzhang', null, 4, null, 0.4, 6, { counter_reset: true })]
      : [user(USERS[(i + 2) % USERS.length], r2(0.2 + i * 0.3), 4, 0, r2(1.2 + i), 24 + i)],
    per_user_resets: i === 0 ? 1 : 0,
    throttled_users: i === 0 ? 1 : 0,
  }));
  const nfs = HOSTS.flatMap((h, i) => ['/n/home01', '/n/holyscratch01'].map((m, j) => ({
    host: h, mount: m, fstype: 'nfs4', bad_xid: i === 2 && j ? 12 : 0, backlog_u: i === 2 && j ? 3.2 : 0,
    rd_ops_total: 1_841_118 + i * 41_118, wr_ops_total: 412_118 + i * 8_118,
    ops_s: r2(41 + i * 8 + j * 12), rd_kb_s: r2(840 + i * 120), wr_kb_s: r2(120 + i * 40),
    // the home mount on the last host did no I/O in the sample: no round trip, not one of 0
    rd_rtt_ms: i === 5 && !j ? null : i === 2 && j ? 141.2 : r2(1.1 + j * 0.4),
    rd_exe_ms: i === 5 && !j ? null : i === 2 && j ? 188.4 : r2(1.4 + j * 0.4),
    rd_retrans: i === 5 && !j ? null : i === 2 && j ? 3 : 0,
    wr_rtt_ms: i === 5 && !j ? null : r2(4.8 + j * 1.2), wr_exe_ms: i === 5 && !j ? null : r2(5.9 + j * 1.2),
    wr_retrans: i === 5 && !j ? null : 0,
  })));
  const disk = HOSTS.flatMap((h, i) => [
    { host: h, mountpoint: '/', fstype: 'xfs', total_gb: 214.7, used_pct: r2(38 + i * 3.1), inode_used_pct: r2(6 + i) },
    { host: h, mountpoint: '/tmp', fstype: 'xfs', total_gb: 858.9, used_pct: h === 'boslogin02' ? 96.4 : r2(12 + i * 4), inode_used_pct: r2(2 + i) },
    { host: h, mountpoint: '/var', fstype: 'xfs', total_gb: 107.3, used_pct: r2(41 + i), inode_used_pct: h === 'holylogin02' ? 93.1 : r2(9 + i) },
  ].map((d) => ({ ...d, full: d.used_pct >= 90 || d.inode_used_pct >= 90 })));
  return {
    ...ENV('ebpf_node', ['/var/log/ebpfm-login']),
    hosts, nfs, disk,
    thresholds: { disk_full_pct: 90, inode_full_pct: 90 },
    series: {
      bin_s: BIN, t, interval_s: 300,
      fields: { fork_exec_ms: 'max', getent_ms: 'max', load_1: 'mean', nfs_rd_rtt_ms: 'max' },
      hosts: series.hosts, state: 'ready', passes: 4, error: null,
      read: { files: 12, lines: 6_912, bad: 0, bytes: 1_418_224_118 }, held_from: t[0] - 6 * 86_400, days: 8,
    },
    cpu_delta_basis: 'per_user_cgroup.cpu_usage_usec is CUMULATIVE; cpu_delta_s is the difference between the two newest snapshots',
    privilege: 'cgroup v2 stats are readable by the slice owner and root',
  };
}

/* Each class per day over a week, the days the mock `/api/trend` folds into
 * days, weeks and months (mock/plugin.ts): the oldest day cut where the range
 * starts, a day the collector wrote nothing in (null, not 0), and today, still
 * going. Dates in the generator's local time, like the live tab's clock
 * strings. */
function trendDays(nowEpoch) {
  const pad = (n) => String(n).padStart(2, '0');
  const now = new Date(nowEpoch * 1000);
  const days = [];
  for (let k = 7; k >= 0; k--) {
    const d0 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - k);
    const d1 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - k + 1);
    const a = d0.getTime() / 1000;
    const b = d1.getTime() / 1000;
    const lo = nowEpoch - 7 * 86_400;
    const from = Math.max(a, lo);
    const to = Math.min(b, nowEpoch);
    const minutes = Math.floor((to - from) / 60);
    const dark = k === 4;           // the collector wrote nothing that day
    const held = dark ? 0 : Math.round(minutes * (k === 2 ? 0.92 : 0.99));
    const grow = 1 + (7 - k) * 0.09;
    const part = minutes / 1440;
    const cell = (n, cpu, users) => (dark ? { n: null, cpu_s: null, users: null }
      : { n: Math.round(n * grow * part), cpu_s: Number((cpu * grow * part).toFixed(1)), users });
    days.push({
      date: `${d0.getFullYear()}-${pad(d0.getMonth() + 1)}-${pad(d0.getDate())}`,
      from, to, today: k === 0, cut: a < lo, minutes, held,
      by_class: {
        agent: cell(4_812_446 / 7, 91_244.8 / 7, 30 + (7 - k) * 2),
        human: cell(384_902 / 7, 26_408.4 / 7, 128 - k),
        ide: cell(1_964_118 / 7, 48_611.2 / 7, 58 + (k % 3)),
      },
      types: dark ? {} : {
        claude_code: { users: 19 + (7 - k), n: Math.round((2_888_000 / 7) * grow * part), cpu_s: Number(((54_000 / 7) * grow * part).toFixed(1)) },
        codex: { users: 11 + Math.floor((7 - k) / 2), n: Math.round((1_640_000 / 7) * grow * part), cpu_s: Number(((31_000 / 7) * grow * part).toFixed(1)) },
        copilot: { users: 3, n: Math.round((284_446 / 7) * part), cpu_s: Number(((6_244 / 7) * part).toFixed(1)) },
      },
    });
  }
  return days;
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
      /* every folder the service looked in, and what it found (feeds.py; `check-feeds` prints the same) */
      candidates: [
        { path: '/scratch/ebpfm', state: 'missing', note: null },
        { path: '/var/log/ebpfm', state: 'found', note: '18 files' },
      ],
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

  const rate = rateRows(nowEpoch);

  const hosts = HOSTS.map((h, i) => ({
    host: h,
    roots: 118 + i * 61,
    procs: 1_884 + i * 418,
    rss_gb: Number((41.2 + i * 18.4).toFixed(1)),
    d_state: i === 2 ? 14 : i === 4 ? 3 : 0,
    users: 18 + i * 7,
    tcp_open_ext: 88 + i * 24,
    by_class: { agent: 61 + i * 38, 'ide': 22 + i * 12, human: 35 + i * 11 },
    // what the tracked D-state processes were blocked in (residency_totals)
    d_stack_top: i === 2 ? { rpc_wait_bit_killable: 9, nfs_wait_on_request: 3, folio_wait_bit_common: 2 } : null,
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
    },
    server: {
      host: { _value: '127.0.0.1', _origin: 'cli --host' },
      port: { _value: 8787, _origin: 'cli --port' },
      static_dir: { _value: 'dashboard/web/dist', _origin: 'default' },
    },
    /* What the service says of itself (config.py `as_dict`): the zone every time on the page is
     * in, which code it is, and its host, which names the deployment where no site name is set.
     * And how this reader got in (app.py `api_config`, auth.py `reader`): here the site's single
     * sign-on signed them in, so the head names them and Sign out links to the proxy's. */
    _meta: {
      tz: { _value: { name: 'America/New_York', abbrev: 'EDT', utc_offset_s: -14400 }, _origin: 'runtime' },
      version: { _value: { version: '0.1.0', commit: 'fixture' }, _origin: 'build' },
      host: { _value: 'dash01', _origin: 'runtime' },
      auth: { _value: { method: 'proxy', user: 'jdoe', logout_url: '/oauth2/sign_out' }, _origin: 'request' },
    },
  };

  return {
    _generated_by: 'mock/gen_fixture.mjs -- development fixture, never served in production',
    now_epoch: nowEpoch,
    config,
    feeds,
    panels: stripBasis({
      panels: {
        tool_mix: toolMix(),
        io_process: ioProcess(),
        resources: resources(nowEpoch),
        sandbox: sandbox(),
        trajectories: trajectories(nowEpoch),
        cascades: cascades(nowEpoch),
        node: nodePanel(nowEpoch),
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
    }),
    live,
    trend_days: trendDays(nowEpoch),
    trajectories: stripBasis(trajectories(nowEpoch)),
    trajectory_detail: stripBasis(trajectoryDetail(nowEpoch, trajectories(nowEpoch))),
    users: stripBasis(users(nowEpoch, trajectories(nowEpoch))),
  };
}

/* The service's notes on how a panel counts -- `notes`, `basis`, every `*_basis` -- are left out
 * of the routes the page reads (`jsonout.strip_basis`): every object reached through objects
 * loses them, and lists are not walked. The fixture is what those routes send. */
const isBasis = (k) => k === 'notes' || k === 'basis' || k.endsWith('_basis');
function stripBasis(o) {
  if (!o || typeof o !== 'object' || Array.isArray(o)) return o;
  return Object.fromEntries(Object.entries(o).filter(([k]) => !isBasis(k)).map(([k, v]) => [k, stripBasis(v)]));
}

const out = join(HERE, 'api.json');
writeFileSync(out, JSON.stringify(build(), null, 1) + '\n');
console.log('wrote', out);
