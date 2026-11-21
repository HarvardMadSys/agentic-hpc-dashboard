/* Plain words for every term a reader may not know (roadmap notes 29 and 30).
 *
 * One place, so a word reads the same wherever it appears. Each entry is one
 * line in the reader's terms, never the collector's; a raw field name keeps its
 * name and says what it counts. `<Term>` and `Plate` show a small "?" beside a
 * term that has an entry here, and the line on hover.
 *
 * One word per thing. A label says the word on the left, and the words on the
 * right are not used on the page for that thing (web/README.md, "Words", is the
 * same table). Some keys below are older than their words (`payload`, `home`,
 * `trajectory`, `keys`); the line a key holds says the word the page uses.
 *
 *   class: agent, human, IDE   who ran it                    not: who ran it, people, editor
 *   agent, agent type          an AI coding agent (one running copy of one), and
 *                              which it is                   not: a run of one (a session)
 *   session                    one run of an agent or an IDE and all it started,
 *                              or a person's terminal login  not: trajectory, tree, run
 *   process tree               a session's processes, drawn  not: trajectory
 *   process                    one program that ran          not: event, exits (a count)
 *   tool call                  a command an agent issued     not: call, command of its own
 *   login node                 a machine the collector watches, in the site's own
 *                              word (ui.node_noun, nodes.ts) not: host, machine
 *   start folder               where an agent was started    not: home (a user's home folder)
 *   driven from                terminal, script, IDE extension, desktop app,
 *                              web or phone, unknown         not: launcher, surface, CLI/app
 *   approval at launch         bypass flag, auto mode, no bypass flag, can't tell
 *                                                            not: approval bypassed, not observable
 *   sandboxed                  ran in its own namespaces     not: namespace-isolated, isolated, unsandboxed
 *   sandbox failed to start                                  not: attempt failed (no payload ran)
 *   wrapped command            what a sandbox wrapper ran    not: payload
 *   memory, peak memory        resident memory (RSS)         not: resident, memory in use
 *   disk read, disk written    bytes that reached the disk   not: rd, wr
 *   read requested,
 *   write requested            bytes through read()/write()  not: syscall, rchar, wchar
 *   sent, received             network bytes                 not: tx, rx, egress, ingress
 *   median                                                   not: p50
 *   pieces                     collector keys joined in a row not: keys
 *   feed                       a stream the page reads: process tracer, node metrics, ...
 *                                                            not: data source, tier, provenance
 *   fake data                  the demo's made-up records    not: synthetic data
 */
import { table } from './lookup';
export const GLOSS: Record<string, string> = table({
  // what is counted
  running: 'running: a process alive right now, as seen in the collector’s census of live processes (taken every 60 s)',
  exited: 'exited: a process that finished; each one leaves one record when it exits, so “processes exited” counts what ran',
  session: 'session: one run of an agent or an IDE and everything it started, or one person’s terminal login',
  toolcall: 'tool call: a command an agent issued itself, such as one shell command; what that command started in turn is counted as processes',
  class: 'class: who ran it. agent = an AI coding agent and everything it started; human = a person’s own terminal; IDE = an editor such as VS Code and everything it started',
  sessions_now: 'sessions: agent and IDE sessions with a process running on the login node, seen in the last 10 minutes',
  tree: 'process tree: one session’s processes, each drawn under the process that started it',
  user: 'user: one account; a person who runs an agent, a shell, and an IDE is one user',

  // CPU, memory, waits
  cpuh: 'CPU-hours: CPU time summed over processes; 1 CPU-h is one core busy for one hour',
  cpusmin: 'CPU seconds per minute: CPU time used in each minute; 60 means one core fully busy for that minute',
  rss: 'memory (resident memory, RSS): the physical memory processes hold, sampled while they run',
  peakrss: 'peak memory: the most memory a process held at any moment',
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

  // I/O: what reached the disk, and what programs asked for (the gap is the page cache)
  diskbytes: 'disk: bytes that actually reached or came from the disk',
  syscall: 'requested: bytes programs read or wrote through read() and write() calls; part of what is read is served from memory (the page cache) and never touches the disk',
  pagecache: 'page cache: recently used file data the system keeps in memory, so reading it again does not touch the disk',
  rchar: 'read requested: bytes programs read through read() calls, served from memory (the page cache) or from the disk',
  wchar: 'write requested: bytes programs passed to write() calls, before they reach the disk',
  rd: 'disk read: bytes actually read from the disk (the block layer)',
  wr: 'disk written: bytes actually written to the disk (the block layer)',
  rchar_mb: 'read requested, in MB: megabytes programs read through read() calls, served from memory or from the disk',
  wchar_mb: 'write requested, in MB: megabytes programs passed to write() calls, before they reach the disk',
  rd_mb: 'disk read, in MB: megabytes actually read from the disk (the block layer)',
  wr_mb: 'disk written, in MB: megabytes actually written to the disk (the block layer)',
  cacheserved: 'cache served: bytes read() returned from memory rather than from the disk',
  cachehit: 'cache hit: the share of bytes read that came from memory rather than the disk',
  tx: 'sent: bytes that left this login node over the network (tx, egress)',
  rx: 'received: bytes that arrived over the network (rx, ingress)',
  netcalls: 'network calls: send and receive calls the processes made',
  coverage: 'coverage: the share of processes this could be measured for; the number covers those alone',
  exttcp: 'external TCP: open network connections to other machines',

  // agents and sandboxing
  subagent: 'subagent: an agent session started by another agent',
  hosted: 'hosted: an agent run by a daemon or an app server rather than started from a terminal',
  depth: 'depth: how many steps below the agent a process sits; 0 is the agent, 1 a command it ran, 2 what that command ran',
  fanout: 'fan-out: how many agents one agent started directly',
  blast: 'blast radius: where an agent ran, what it read and wrote, and what it touched outside its start folder',
  home: 'start folder: the folder an agent was started in; its own work is expected there',
  outsidehome: 'outside its start folder: a path outside the folder the agent was started in, its own temp scratch included',
  otherhome: 'another user’s home: a path inside someone else’s home folder',
  shared: 'shared storage: cluster-wide storage such as /scratch or /n/…, which other people use too',
  named: 'files its commands mention: paths that appear in the text of its commands, sorted by what each command does with them; guessed from the text, not measured',
  sandbox: 'sandboxed: the command ran in its own namespaces, an isolated view of files, processes or the network that it could not see past (what bwrap and Codex’s sandbox set up). Read from each process’s namespaces, so a Landlock- or seccomp-only sandbox does not show, and a container counts',
  bypass: 'bypass flag: the agent was started with a flag that lets it act without asking (for example --dangerously-skip-permissions or --yolo)',
  automode: 'auto mode: a classifier approves the agent’s actions instead of asking a person',
  approval: 'approval at launch: what the agent’s command line says about asking before it acts: a bypass flag, auto mode, or no bypass flag. A settings file and a switch made mid-session are not seen, so “no bypass flag” is not “asks first”',
  wrapper: 'sandbox wrapper: the helper (bwrap, codex-linux-sandbox) an agent uses to run a command in a sandbox',
  payload: 'wrapped command: the command a sandbox wrapper was asked to run',
  verdict: 'sandboxed?: whether a process ran in a sandbox, read from its namespaces',
  ancestry: 'under wrapper: which sandbox wrapper, if any, a process ran under',
  inherit: 'from its parent: the process exited before it could be checked, so its parent’s answer stands in',
  none_tick: 'census: checked during the collector’s periodic census of live processes',
  none_exec: 'at start: checked when the process started',
  userns_exec: 'at start, user namespace: checked when the process started, inside a user namespace',
  exitedbeforeread: 'exited before it could be checked: the process was gone before its namespaces could be read',
  headless: 'from a script: a program runs the agent (claude -p, codex exec, an SDK), with no one at a terminal',
  version: 'version: the agent’s version, read from the program’s path where the path shows it',
  surface: 'driven from: where an agent session is driven from, read off the agent’s own command line: a terminal, a script, an IDE extension, the desktop app, or the web or a phone',

  // risk
  credentials: 'credentials in argv: a password or token typed into a command line, where any user on the login node can read it with ps; counted, never shown',
  dangerous: 'commands that should not run here: commands this site’s rules flag on a shared login node',
  severity: 'severity: how much harm the rule’s command can do (HIGH, med, low)',
  succeeded: 'succeeded: how many of those runs exited with code 0',
  loginnode: 'login node: the shared machine people connect to; heavy compute belongs on the compute nodes',
  polling: 'polling: commands that only check on something (ps, pgrep, sleep, squeue, …)',
  exitcode: 'exit code: 0 means the command succeeded; anything else means it failed or was stopped',

  // login-node health (the node tier)
  load: 'load average: how many processes were running or waiting to run, averaged over the last 1, 5 and 15 minutes; compare it with the node’s cores',
  iowait: 'I/O wait: the share of CPU time spent idle while waiting on disk or network storage; high while a file server is slow',
  forkexec: 'start a process: how long the node took to start a tiny program, measured by the collector every snapshot; what a person feels as a sluggish shell',
  statlocal: 'stat a local file: how long a local filesystem took to answer one lookup',
  getent: 'look up an account: how long the node took to resolve a user name (getent); slow when the directory service is',
  nfsrtt: 'NFS round trip: how long the file server took to answer, from request to reply, averaged over the collector’s short sample; the slowest mount is shown',
  retrans: 'retransmissions: NFS requests sent again because the server did not answer in time, in the collector’s short sample',
  badxid: 'bad replies: replies from the file server that matched no request, since the mount; a sign of a struggling server or network',
  backlog: 'backlog: requests waiting to be sent to the file server',
  inodes: 'inodes: the filesystem’s file slots; when they run out no new file can be made, however much space is left',
  cgroupcpu: 'CPU in use: the account’s CPU time between the two newest snapshots, as cores busy on average',
  quota: 'CPU limit: the most cores the account may use at once on this node; none set reads —',
  throttled: 'held back: seconds the kernel paused the account’s processes because they had used their CPU limit, between the two newest snapshots',
  pids: 'processes: the account’s processes and threads now, and the most it may have',
  wchan: 'waiting in: the kernel function a stuck process is parked in; nfs and rpc names point at a file server. Another user’s reads “-” unless the collector runs as root',
  zombie: 'zombie: a process that has finished but whose parent has not collected it; harmless in small numbers',

  // Slurm
  gpuh: 'GPU-hours asked: GPUs requested times the time limit requested, a ceiling rather than use',
  queuecheck: 'queue check: a command that asks the scheduler about jobs (squeue, sacct, sinfo, scontrol)',
  refused: 'refused: a submission the scheduler turned down, with the reason it gave',
  parsable: '--parsable: sbatch prints only the job id, the way scripts and agents submit',
  dependency: '--dependency: the job waits for another job before it starts',
  array: '--array: one submission that runs many copies of a job',
  wrap: '--wrap: the job is a command given inline rather than a script',

  // sessions
  trajectory: 'process tree: one session’s processes, each drawn under the process that started it, with its tool calls in order',
  purpose: 'purpose: what kind of work each process was (polling, files, build, compute, …), as a coloured strip over the session',
  tasks: 'tasks: what a session was working on. A task can start after an hour or more in which it waited on nothing, when its work moves to something new, or after a commit or pull request. A quoted label is words written in its commands: the agent’s own, or a person’s. Any other label is a verb and the names its commands used. A dashed block is a few calls hours from other work.',
  phases: 'phases: what a session was doing, read from the commands it ran: exploring, editing, building, testing, running, Slurm jobs, waiting, git, and idle. An agent’s are its tool calls; a session no agent ran, a person’s login or an editor’s terminal, is read from the commands its terminals ran, each command line one call. Reading and editing inside an agent or in an editor’s window, and thinking, leave no command, so an edit shows only when a command makes it or, hatched, as a guess',
  keys: 'pieces: how many pieces the collector recorded this session in, joined into this one row',
  wall: 'length: elapsed time from the session’s first process to its last',
  sigmacpu: 'Σ CPU: CPU of the process and everything below it',
  procs: 'procs: processes in the subtree',
});

export const gloss = (k: string | undefined): string | undefined => (k ? GLOSS[k] : undefined);
