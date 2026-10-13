/* Plain words for every term a reader may not know (roadmap notes 29 and 30).
 *
 * One place, so a word reads the same wherever it appears. Each entry is one
 * line in the reader's terms, never the collector's; a raw field name keeps its
 * name and says what it counts. `<Term>` and `Plate` show a small "?" beside a
 * term that has an entry here, and the line on hover.
 */
export const GLOSS: Record<string, string> = {
  // what is counted
  running: 'running: a process alive right now, as seen in the collector’s census of live processes (taken every 60 s)',
  exited: 'exited: a process that finished; each one leaves one record when it exits, so “processes exited” counts what ran',
  session: 'session: one agent or editor and everything it started, or one person’s terminal login',
  toolcall: 'tool call: a command an agent ran directly, such as one shell command it issued; what that command started in turn is counted as processes',
  class: 'class: who ran it. agent = an AI coding agent and everything it started; human = a person’s own terminal; IDE = an editor session such as VS Code',
  sessions_now: 'sessions: agent and editor sessions with a process running on the node, seen in the last 10 minutes',
  tree: 'tree: one agent or editor process and everything it started',
  user: 'user: one account; a person who runs an agent, a shell, and an editor is one user',

  // CPU, memory, waits
  cpuh: 'CPU-hours: CPU time summed over processes; 1 CPU-h is one core busy for one hour',
  cpusmin: 'CPU seconds per minute: CPU time used in each minute; 60 means one core fully busy for that minute',
  rss: 'resident memory (RSS): the physical memory processes hold, sampled while they run',
  peakrss: 'peak memory: the most resident memory a process held at any moment',
  oncpu: 'share of lifetime on CPU: for each process, the time it actually ran on a CPU divided by how long it existed, capped at 100%. A process that mostly waited scores low',
  runqueue: 'runqueue wait: time a process was ready to run but waited for a free CPU',
  dstate: 'D-state (uninterruptible sleep): the process is stuck waiting on the system, usually storage and often a network filesystem (NFS), and cannot be interrupted',
  stall: 'stall: each time a thread went into uninterruptible sleep (D-state) and woke again',
  majflt: 'major page fault: memory the process touched had to be read from storage, often over the network, which is slow',
  minflt: 'minor page fault: memory mapped in without touching storage; cheap',
  cmajflt: 'major faults in reaped children: major faults counted in the child processes a process waited for and collected',
  nvcsw: 'gave up the CPU to wait (voluntary context switch): the process blocked on I/O, a lock, or a sleep',
  nivcsw: 'preempted (involuntary context switch): the scheduler took the CPU away to run something else',
  blkio: 'waiting on block I/O: time spent waiting for disk reads or writes to finish',
  swapin: 'waiting on swap-in: time spent bringing memory back from swap',
  reclaim: 'waiting on memory reclaim: time spent waiting for the kernel to free memory',
  lifetime: 'lifetime: how long the process existed, from start to exit',

  // quantiles
  mean: 'mean: the total divided by the number of processes',
  median: 'median: half of the processes were below this value',
  min: 'min: the single smallest value in the window',
  p1: 'p1, the 1st percentile: 1 in 100 processes were below this value',
  p10: 'p10, the 10th percentile: 1 in 10 processes were below this value',
  p90: 'p90, the 90th percentile: 9 in 10 processes were below this value',
  p99: 'p99, the 99th percentile: 99 in 100 processes were below this value',
  max: 'max: the single largest value in the window',
  quantile: 'quantiles (median, p90, p99): the value below which that share of processes fall',

  // I/O
  diskbytes: 'disk bytes: data that actually reached or came from the disk',
  syscall: 'syscall bytes: data passed through read() or write() calls; part of it is served from memory (the page cache) and never touches the disk',
  pagecache: 'page cache: recently used file data the system keeps in memory, so reading it again does not touch the disk',
  rchar: 'rchar: bytes read through read()-like calls, cached or not',
  wchar: 'wchar: bytes written through write()-like calls, before they reach the disk',
  rd: 'rd: bytes actually read from the disk (block layer)',
  wr: 'wr: bytes actually written to the disk (block layer)',
  rchar_mb: 'rchar_mb: megabytes read through read()-like calls, cached or not',
  wchar_mb: 'wchar_mb: megabytes written through write()-like calls, before they reach the disk',
  rd_mb: 'rd_mb: megabytes actually read from the disk (block layer)',
  wr_mb: 'wr_mb: megabytes actually written to the disk (block layer)',
  cacheserved: 'cache served: bytes read() returned from memory rather than from the disk',
  cachehit: 'cache hit: the share of bytes read that came from memory rather than the disk',
  tx: 'sent (tx, egress): bytes that left this machine over the network',
  rx: 'received (rx, ingress): bytes that arrived over the network',
  netcalls: 'network calls: send and receive calls the processes made',
  coverage: 'coverage: the share of processes this could be measured for; the number covers those alone',
  exttcp: 'external TCP: open network connections to other machines',

  // agents and sandboxing
  subagent: 'subagent: an agent started by another agent',
  hosted: 'hosted: an agent run by a daemon or an app server rather than started from a terminal',
  depth: 'depth: how many steps below the agent a process sits; 0 is the agent, 1 a command it ran, 2 what that command ran',
  fanout: 'fan-out: how many agents one agent started directly',
  blast: 'blast radius: where an agent ran, what it read and wrote, and what it touched outside its own folder',
  home: 'home: the folder an agent was started in; its own work is expected there',
  outsidehome: 'outside home: a path outside the folder the agent was started in, its own temp scratch included',
  otherhome: 'another user’s home: a path inside someone else’s home folder',
  shared: 'shared storage: cluster-wide storage such as /scratch or /n/…, which other people use too',
  named: 'files its commands mention: paths that appear in the text of its commands, sorted by what each command does with them; guessed from the text, not measured',
  sandbox: 'sandboxed (namespace-isolated): the command ran inside an isolated view of the system, so it could not see or change everything the user can',
  bypass: 'bypass flag: the agent was started with a flag that lets it act without asking (for example --dangerously-skip-permissions or --yolo)',
  automode: 'auto mode: a classifier approves the agent’s actions instead of asking a person',
  approval: 'approval at launch: how the agent was started: asking before acting, auto mode, or a bypass flag',
  wrapper: 'sandbox wrapper: the helper (bwrap, codex-linux-sandbox) an agent uses to run a command isolated',
  payload: 'payload: the command a sandbox wrapper was asked to run',
  verdict: 'verdict: whether a process ran isolated, read from its namespaces',
  ancestry: 'ancestry: which sandbox wrapper, if any, a process ran under',
  inherit: 'inherit: the verdict was taken from the parent, because the process itself exited before it could be read',
  none_tick: 'none:tick: read during the collector’s periodic census of live processes',
  none_exec: 'none:exec: read when the process started',
  exitedbeforeread: 'exited before read: the process was gone before its namespaces could be read',
  headless: 'headless: an agent running without a terminal, started by a script or a server',
  version: 'version: the agent’s version, read from the program’s path where the path shows it',

  // risk
  credentials: 'credentials in argv: a password or token typed into a command line, where any user on the node can read it with ps; counted, never shown',
  dangerous: 'commands that should not run here: commands this site’s rules flag on a shared login node',
  severity: 'severity: how much harm the rule’s command can do (HIGH, med, low)',
  succeeded: 'succeeded: how many of those runs exited with code 0',
  loginnode: 'login node: the shared machine people connect to; heavy compute belongs on the compute nodes',
  polling: 'polling: commands that only check on something (ps, pgrep, sleep, squeue, …)',
  exitcode: 'exit code: 0 means the command succeeded; anything else means it failed or was stopped',

  // Slurm
  gpuh: 'GPU-hours asked: GPUs requested times the time limit requested, a ceiling rather than use',
  queuecheck: 'queue check: a command that asks the scheduler about jobs (squeue, sacct, sinfo, scontrol)',
  refused: 'refused: a submission the scheduler turned down, with the reason it gave',
  parsable: '--parsable: sbatch prints only the job id, the way scripts and agents submit',
  dependency: '--dependency: the job waits for another job before it starts',
  array: '--array: one submission that runs many copies of a job',
  wrap: '--wrap: the job is a command given inline rather than a script',

  // trajectories
  trajectory: 'trajectory: one session’s commands in order, as a process tree',
  purpose: 'purpose: what kind of work each command was (polling, files, build, compute, …), as a colored strip over the session',
  keys: 'keys: how many collector session ids this row combines',
  wall: 'wall: elapsed time from the session’s first process to its last',
  sigmacpu: 'Σ CPU: CPU of the process and everything below it',
  procs: 'procs: processes in the subtree',
};

export const gloss = (k: string | undefined): string | undefined => (k ? GLOSS[k] : undefined);
