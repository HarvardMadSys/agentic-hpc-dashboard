"""Coding agents' sessions, as their processes look to the collector (schema 8).

An agent is a ROOT -- the runtime that talks to the model -- and the tool calls it
makes. Claude Code runs each Bash call in a shell that sources its saved
environment and `eval`s the command (`snap`), and its Grep and Glob as its own
binary re-executed as `rg`; Codex runs `bash -lc <command>`, under
`codex-linux-sandbox` and `bwrap` when it sandboxes. Each payload is a process of
its own beneath the shell, written before the shell that waited on it.

What a session does is a run of TASKS, each the cadence a real one has (and that
reducers/phase_tasks.py was tuned on): open the files, look around, edit, run the
tests until they pass, maybe a long run or a Slurm job polled to the end, and a
commit -- whose subject, a pull request's title or a log entry is then the task's
label, in the agent's own words. Edits made with the agent's own Edit tool leave
no command, as they do on a cluster.

Only the top-most root of a tree writes `residency`; a nested agent (a
`claude -p` from a tool call, a Claude in VS Code, a Remote Control worker) is
seen through its own `conn`/`netio` records and its exit. Records follow the
collector's rules in schema 8: a tool shell is not tagged (`is_agent` false), a
child inherits its parent's namespaces, prompts on a command line read
`[REDACTED-PROMPT]` and credentials `[REDACTED]`.
"""
import hashlib
import heapq
import shlex
import time
from collections import Counter

from . import cast
from .records import Proc, endpoint, envelope, exit_record, logu
from .sessions import DURATION, Builder, human_shell, sbatch_args, slurm_submit, squeue_args

CLAUDE_VER = "2.1.311"
CODEX_VER = "0.165.0"
# Where each agent's model is: its API's address, the provider the collector's
# CIDR table names for it, and the host its argv would name.
API = {"claude_code": ("160.79.104.10", "anthropic", "api.anthropic.com"),
       "codex": ("162.159.140.229", "openai", "api.openai.com")}

BUILTINS = frozenset({"cd", "export", "source", ".", "echo", "printf", "test", "[", "[[", "true",
                      "false", "set", "shopt", "unset", "local", "read", "wait", "exit", "for",
                      "while", "until", "do", "done", "if", "then", "fi", "else", "elif", "time",
                      "nohup", "env", "trap", "eval"})
SEPARATORS = frozenset({"&&", "||", ";", "|", "&", ";;"})
REDIRECTS = frozenset({">", ">>", "<", ">|", ">&", "<&", "&>", "<<<"})


def q(cmd):
    """One shell word, as Claude Code quotes the command it `eval`s."""
    return "'" + cmd.replace("'", "'\"'\"'") + "'"


def _strip_heredocs(cmd):
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        if "<<" in ln:
            tag = ln.split("<<", 1)[1].strip().lstrip("-").strip("'\"").split()[0:1]
            if tag:
                i += 1
                while i < len(lines) and lines[i].strip() != tag[0]:
                    i += 1
                out[-1] = ln.split("<<", 1)[0]
        i += 1
    return "\n".join(out)


def programs(cmd):
    """The programs a command line runs: [[(command, argv), ...] per pipeline], and
    whether the last one was put in the background."""
    body = _strip_heredocs(cmd).replace("\n", " ; ")
    lex = shlex.shlex(body, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        toks = body.split()
    groups, pipe, cur, bg = [], [], [], False
    skip = False
    for i, tk in enumerate(toks):
        if skip:
            skip = False
            continue
        if tk in SEPARATORS:
            if cur:
                pipe.append(cur)
            cur = []
            if tk != "|":
                if pipe:
                    groups.append(pipe)
                pipe = []
                bg = tk == "&"
            continue
        if tk in REDIRECTS or tk in ("<<",):
            skip = True
            continue
        if tk.isdigit() and i + 1 < len(toks) and toks[i + 1] in REDIRECTS:
            continue
        cur.append(tk)
    if cur:
        pipe.append(cur)
    if pipe:
        groups.append(pipe)
        bg = False
    out = []
    for g in groups:
        stage = []
        for words in g:
            while words and ("=" in words[0] and not words[0].startswith("-")
                             or words[0] in ("nohup", "env", "time", "exec")):
                words = words[1:]
            if not words or words[0] in BUILTINS:
                continue
            name = words[0].rsplit("/", 1)[-1]
            if name.startswith("python"):
                name = "python3"
            stage.append((name, " ".join(words)))
        if stage:
            out.append(stage)
    return out, bg


def _uuid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


class Live:
    """Which of `procs` -- (start, end, command, rss, threads) -- run at each of a
    rising series of times: one sweep, not a scan of every process per tick."""

    def __init__(self, procs):
        self.procs = sorted(procs)
        self.i = 0
        self.heap = []

    def at(self, t):
        while self.i < len(self.procs) and self.procs[self.i][0] <= t:
            heapq.heappush(self.heap, (self.procs[self.i][1], self.i))
            self.i += 1
        while self.heap and self.heap[0][0] <= t:
            heapq.heappop(self.heap)
        return [self.procs[j] for _end, j in sorted(self.heap, key=lambda x: x[1])]


class Agent:
    """One agent runtime: its root process, the tool calls it makes, and its ticks."""

    def __init__(self, b, atype, command, args, start, end, *, tty=None, cwd=None,
                 parent=None, tree_root=None, approval="supervised", redacted=None,
                 sandbox_calls=None, sockets=True, residency=True):
        self.b, self.atype = b, atype
        rng = b.rng
        pid = b.pid()
        bypass = approval == "bypassed"
        self.root = Proc(
            pid=pid, ppid=parent.pid if parent is not None else b.pid(), uid=b.uid, user=b.user,
            tty=tty, command=command, args=args, cwd=cwd or b.pdir,
            actor="agent", actor3="agent", attribution="ancestry", agent_type=atype,
            agent_pid=pid, session_uuid=_uuid(b.hf.boot_id, pid, start),
            tree_root_pid=tree_root or pid, depth=0, is_agent=True, autonomous=bypass,
            approval_mode="bypassed" if bypass else None, approval_src="argv" if bypass else None,
            sandbox="unsandboxed", sandbox_src="none:exec", cgroup=b.scope, redacted=redacted,
            start=start, end=end, rss_mb=round(rng.uniform(280, 520), 1),
            threads=rng.randint(18, 34) if atype == "claude_code" else rng.randint(10, 24))
        self.root.pgrp = self.root.sid = self.root.pid
        self.rate = rng.uniform(0.015, 0.05)            # the runtime's own CPU, per second
        self.procs = []                                 # (start, end, command, rss, threads)
        self.busy = []                                  # when a call ran: the model is talking
        self.residency, self.sockets = residency, sockets
        self.sandbox_calls = sandbox_calls              # None, "bwrap" or "codex"
        tag = "%x" % (rng.getrandbits(48))
        self.snapfile = "%s/.claude/shell-snapshots/snapshot-bash-%d-%s.sh" % (
            b.home, int(start * 1000), tag[:6])
        self.cwdfile = "/tmp/claude-%s-cwd" % tag[-4:]
        self.codex_bin = "%s/.codex/packages/standalone/releases/%s-x86_64-unknown-linux-musl/bin/codex" % (
            b.home, CODEX_VER)
        self.cls_bin = "%s/.codex/tmp/arg0/codex-arg0%s/codex-linux-sandbox" % (b.home, tag[:4])
        self.net_tx = self.net_rx = self.calls = 0
        self.nested = []                                # agents started beneath this one

    # ----------------------------------------------------------- helpers
    def snap(self):
        return ("/bin/bash -c source %s 2>/dev/null || true && shopt -u extglob 2>/dev/null "
                "|| true && eval " % self.snapfile)

    def _kids(self, parent, t, cmd, exit_code, scale, dur):
        """The processes `cmd` runs under `parent`, from `t`: pipeline stages side by
        side, `&&` groups one after another. Returns (procs, end, background)."""
        b = self.b
        groups, bg = programs(cmd)
        procs, tt = [], t + 0.004
        for gi, stage in enumerate(groups):
            last = gi == len(groups) - 1
            ends = []
            for name, argv in stage:
                d = dur if (dur is not None and last and name == stage[0][0]) else None
                if name == "sleep":                     # a sleep lasts what it says
                    try:
                        d = float(argv.split()[1])
                    except (IndexError, ValueError):
                        pass
                k = b.child(parent, tt, name, argv, dur=d, scale=scale)
                if last and exit_code and name == stage[0][0]:
                    k.exit_code = exit_code
                procs.extend(b.spawn(k))
                procs.append(k)
                ends.append(k.end)
            if not (last and bg):
                tt = max(ends) + 0.003
        end = max([p.end for p in procs if not (bg and p.start >= tt - 0.004)] + [t + 0.01])
        return procs, end, bg

    def _write(self, procs, parent):
        """Exits in the collector's order: every child before its parent."""
        for p in sorted(procs, key=lambda x: x.end):
            self.b.exit(p)
            self.procs.append((p.start, p.end, p.command, p.rss_mb or 0, p.threads or 1))
        self.b.exit(parent)
        self.procs.append((parent.start, parent.end, parent.command, parent.rss_mb or 0, 1))

    # ------------------------------------------------------------- calls
    def call(self, t, cmd, *, exit_code=0, scale=1.0, dur=None, sandbox=None, redacted=None):
        """One tool call running `cmd`, the way this agent runs one. Returns its end."""
        if t >= self.root.end - 2:
            return t
        sandbox = self.sandbox_calls if sandbox is None else sandbox
        self.calls += 1
        if self.atype == "codex":
            end = self._codex(t, cmd, exit_code, scale, dur, sandbox, redacted)
        else:
            end = self._claude(t, cmd, exit_code, scale, dur, sandbox, redacted)
        self.busy.append((t, end))
        return end

    def _claude(self, t, cmd, exit_code, scale, dur, sandbox, redacted):
        b, root = self.b, self.root
        text = self.snap() + q(cmd) + " < /dev/null && pwd -P >| " + self.cwdfile
        top = root
        wrapper = None
        if sandbox == "bwrap":
            wrapper = b.child(root, t, "bwrap", "/usr/bin/bwrap --new-session --die-with-parent "
                              "--unshare-net --unshare-pid --ro-bind / / --bind %s %s --dev /dev "
                              "--proc /proc -- %s" % (root.cwd, root.cwd, text), dur=0.01)
            wrapper.sandbox, wrapper.sandbox_src = "unsandboxed", "none:exec"
            wrapper.sandbox_ancestry = "bwrap"
            top = wrapper
        shell = b.child(top, t + 0.003, "bash", text, dur=0.01, redacted=redacted)
        shell.sandbox_src = "none:exec"                 # tagged: read at exec
        if wrapper is not None:
            shell.sandbox, shell.sandbox_src = "sandboxed", "userns:exec"
            shell.sandbox_detail = "mnt,net,nnp,pid,user"
        kids, end, _bg = self._kids(shell, t + 0.006, cmd, exit_code, scale, dur)
        shell.end = max(shell.start + 0.01, end + 0.004)
        shell.exit_code = exit_code if len(kids) <= 1 else 0
        shell.cpu_s = round(0.004 + 0.002 * len(kids), 4)
        shell.children_cpu = sum(k.cpu_s or 0 for k in kids)
        if wrapper is not None:
            wrapper.end = shell.end + 0.003
            wrapper.cpu_s = 0.006
            self._write(kids + [shell], wrapper)
        else:
            self._write(kids, shell)
        return (wrapper or shell).end

    def _codex(self, t, cmd, exit_code, scale, dur, sandbox, redacted):
        b, root = self.b, self.root
        if sandbox == "codex":
            wrap = b.child(root, t, "codex-linux-sandbox", "%s --sandbox-policy-cwd %s --command-cwd "
                           "%s -- /bin/bash -lc %s" % (self.cls_bin, root.cwd, root.cwd, cmd), dur=0.01)
            wrap.sandbox, wrap.sandbox_src, wrap.sandbox_ancestry = "unsandboxed", "none:exec", "codex-linux-sandbox"
            bw = b.child(wrap, t + 0.002, "bwrap", "bwrap --new-session --die-with-parent --ro-bind / / "
                         "--dev /dev --bind %s %s --unshare-user --unshare-pid --unshare-net --proc "
                         "/proc -- %s --sandbox-policy-cwd %s --command-cwd %s "
                         "--apply-seccomp-then-exec -- /bin/bash -lc %s"
                         % (root.cwd, root.cwd, self.cls_bin, root.cwd, root.cwd, cmd), dur=0.01)
            bw.sandbox, bw.sandbox_src = "unsandboxed", "none:exec"
            shell = b.child(bw, t + 0.004, "bash", "/bin/bash -lc " + cmd, dur=0.01, redacted=redacted)
            shell.sandbox, shell.sandbox_src = "sandboxed", "userns:exec"
            shell.sandbox_detail = "mnt,net,nnp,pid,seccomp=2,user"
            kids, end, _bg = self._kids(shell, t + 0.006, cmd, exit_code, scale, dur)
            shell.end = end + 0.003
            bw.end = shell.end + 0.002
            wrap.end = bw.end + 0.002
            for x, n in ((shell, 2), (bw, 1), (wrap, 1)):
                x.exit_code = exit_code if len(kids) <= 1 else 0
                x.cpu_s = 0.003 * n
            for k in kids:
                self.b.exit(k)
                self.procs.append((k.start, k.end, k.command, k.rss_mb or 0, k.threads or 1))
            self.b.exit(shell)
            self.b.exit(bw)
            self.b.exit(wrap)
            return wrap.end
        shell = b.child(root, t, "bash", "/usr/bin/bash -lc " + cmd, dur=0.01, redacted=redacted)
        kids, end, _bg = self._kids(shell, t + 0.004, cmd, exit_code, scale, dur)
        shell.end = end + 0.003
        shell.exit_code = exit_code if len(kids) <= 1 else 0
        shell.cpu_s = 0.003
        self._write(kids, shell)
        return shell.end

    def sandbox_failed(self, t, cmd):
        """A sandboxed Codex call whose sandbox never came up: bwrap exits 1 with
        nothing under it, and so does the wrapper. Codex then asks to run it outside."""
        b, root = self.b, self.root
        wrap = b.child(root, t, "codex-linux-sandbox", "%s --sandbox-policy-cwd %s --command-cwd %s "
                       "-- /bin/bash -lc %s" % (self.cls_bin, root.cwd, root.cwd, cmd), dur=0.02)
        wrap.sandbox, wrap.sandbox_src, wrap.sandbox_ancestry = "unsandboxed", "none:exec", "codex-linux-sandbox"
        bw = b.child(wrap, t + 0.002, "bwrap", "bwrap --new-session --die-with-parent --ro-bind / / "
                     "--unshare-user --unshare-pid --unshare-net -- /bin/bash -lc %s" % cmd, dur=0.004)
        bw.sandbox, bw.sandbox_src = "unsandboxed", "none:exec"
        bw.exit_code = wrap.exit_code = 1
        wrap.end = bw.end + 0.004
        self.b.exit(bw)
        self.b.exit(wrap)
        self.procs.append((wrap.start, wrap.end, "codex-linux-sandbox", 6, 1))
        return wrap.end

    def fs_helper(self, t):
        """Codex writing a file through its sandboxed filesystem helper: a seen edit."""
        b, root = self.b, self.root
        wrap = b.child(root, t, "codex-linux-sandbox", "%s --sandbox-policy-cwd %s --command-cwd / "
                       "--apply-seccomp-then-exec -- %s --codex-run-as-fs-helper"
                       % (self.cls_bin, root.cwd, self.codex_bin), dur=0.05)
        wrap.sandbox, wrap.sandbox_src, wrap.sandbox_ancestry = "unsandboxed", "none:exec", "codex-linux-sandbox"
        h = b.child(wrap, t + 0.003, "codex", "%s --codex-run-as-fs-helper" % self.codex_bin, dur=0.04)
        h.sandbox, h.sandbox_src, h.sandbox_detail = "unsandboxed", "none:exec", "nnp,seccomp=2"
        h.rd_mb, h.wr_mb = 0.0, round(b.rng.uniform(0.002, 0.03), 3)
        wrap.end = h.end + 0.002
        self.b.exit(h)
        self.b.exit(wrap)
        return wrap.end

    def grep(self, t, pattern, path, glob=False):
        """Claude Code's Grep/Glob: its own binary re-executed as ripgrep."""
        b = self.b
        args = ("rg --no-config --files --hidden -g %s %s" % (pattern, path) if glob
                else "rg --no-config -n %s %s" % (pattern, path))
        p = b.child(self.root, t, CLAUDE_VER, args)
        p.sandbox_src = "none:exec"
        b.exit(p)
        self.procs.append((p.start, p.end, p.command, p.rss_mb or 0, p.threads or 1))
        self.busy.append((t, p.end))
        return p.end

    def chore(self, t, command, args):
        """What the runtime runs for itself: never a tool call."""
        p = self.b.child(self.root, t, command, args)
        self.b.exit(p)
        return p.end

    def submit(self, t, script, gpu=None, array=None, refuse=False):
        """An agent's sbatch, in a tool call: --parsable, as a program reading the id."""
        b = self.b
        argv = sbatch_args(b, script, True, gpu, array, hours=72 if refuse else None)
        if self.atype == "codex":
            shell = b.child(self.root, t, "bash", "/usr/bin/bash -lc " + argv[0], dur=0.01)
        else:
            shell = b.child(self.root, t, "bash", self.snap() + q(argv[0]) + " < /dev/null && pwd -P >| "
                            + self.cwdfile, dur=0.01)
            shell.sandbox_src = "none:exec"
        sb, jid = slurm_submit(b, shell, t + 0.005, script, True, gpu, array, refuse, argv=argv)
        shell.end = sb.end + 0.004
        shell.exit_code = sb.exit_code
        b.exit(shell)
        self.procs.append((shell.start, shell.end, "bash", 4, 1))
        self.busy.append((t, shell.end))
        return shell.end, jid

    # ------------------------------------------------------------- ticks
    @classmethod
    def around(cls, b, proc, rate):
        """A tree whose root is not an agent session -- the desktop app's server --
        that ticks as one: its own residency, the agents beneath it counted in."""
        self = cls.__new__(cls)
        self.b, self.atype, self.root, self.rate = b, proc.agent_type, proc, rate
        self.procs, self.busy, self.nested = [], [], []
        self.residency, self.sockets, self.sandbox_calls = True, False, None
        self.net_tx = self.net_rx = self.calls = 0
        return self

    def finish(self):
        """The root's ticks over its life, then its own exit (if it ended in time)."""
        b, root, rng = self.b, self.root, self.b.rng
        ip, provider, host = API[self.atype]
        busy = sorted(self.busy)
        bi = 0
        first = root.start + rng.uniform(30, 75)
        prev = root.start
        sock_tx = sock_rx = 0
        tree = list(self.procs)
        for n in self.nested:
            tree += n.procs + [(n.root.start, n.root.end, n.root.command, n.root.rss_mb, n.root.threads)]
        running = Live(tree)
        for T in b.hf.ticks(first, root.end):
            calls = 0
            while bi < len(busy) and busy[bi][0] < T:
                if busy[bi][0] >= prev:
                    calls += 1
                bi += 1
            tx = 900 + calls * rng.randint(6000, 14000) if calls else rng.randint(300, 1200)
            rx = 1500 + calls * rng.randint(9000, 42000) if calls else rng.randint(400, 2200)
            sock_tx, sock_rx = sock_tx + tx, sock_rx + rx
            self.net_tx += tx
            self.net_rx += rx
            live = running.at(T)
            n_procs = 1 + len(live)
            threads = root.threads + sum(x[4] for x in live)
            rss = round(root.rss_mb + sum(x[3] for x in live), 1)
            if self.residency:
                rec = envelope("residency", T + 0.01, b.host, b.cpid(T))
                rec.update(root.identity())
                byc = Counter([root.command] + [x[2] for x in live])
                rec.update({
                    "root_alive": True, "age_s": round(T - root.start, 1), "headless": root.tty is None,
                    "n_procs": n_procs, "threads": threads, "rss_mb": rss,
                    "cpu_s": round(self.rate * (T - root.start), 2), "d_state": 0,
                    "max_depth": 1 + max([1] + [2 for x in live]), "agent_types": [self.atype],
                    "by_command": dict(byc.most_common(15)), "d_stack": None,
                    "state_counts": {"S": max(1, n_procs - (1 if live else 0)), "R": 1 if live else 0}
                    if live else {"S": n_procs},
                    "top_procs": [{"pid": root.pid, "command": root.command, "rss_mb": root.rss_mb,
                                   "cpu_s": round(self.rate * (T - root.start), 2),
                                   "cpu_delta_s": round(self.rate * 60, 2), "state": "S",
                                   "threads": root.threads}],
                    "tcp_open_ext": 1 if self.sockets else 0, "tcp_open": 2 if self.sockets else 1})
                b.emit(T + 0.01, "snapshot", rec)
            if self.sockets:
                rec = envelope("conn", T + 0.02, b.host, b.cpid(T))
                rec.update(root.identity())
                rec.update({"family": "inet", "proto": "tcp", "saddr": b.hf.ip, "sport": 40000 + root.pid % 20000,
                            "daddr": ip, "dport": 443, "external": True, "provider": provider,
                            "inbound": False, "age_s": round(min(T - root.start, T - first + 60), 1),
                            "established": True, "stats_src": "netlink", "state": "ESTABLISHED",
                            "rtt_ms": round(rng.uniform(8, 24), 2), "retrans": 0,
                            "rx_bytes": sock_rx, "tx_bytes": sock_tx,
                            "idle_send_s": round(rng.uniform(0.2, 40), 1),
                            "idle_recv_s": round(rng.uniform(0.2, 40), 1),
                            "segs_in": sock_rx // 1400 + 3, "segs_out": sock_tx // 1400 + 3,
                            "rqueue": 0, "wqueue": 0})
                b.emit(T + 0.02, "snapshot", rec)
            rec = envelope("netio", T + 0.03, b.host, b.cpid(T))
            rec.update(root.identity())
            rec.update({"reason": "tick", "interval_s": round(T - prev, 1),
                        "endpoints": [endpoint("tcp", ip, 443, tx, rx, 3 + calls * 9, provider)],
                        "unattributed": None})
            b.emit(T + 0.03, "snapshot", rec)
            if self.residency:
                b.census[int(T // 60) * 60].append(("agent", self.atype, b.user, n_procs, threads, rss, 0))
            prev = T
        if root.end <= b.spec.end:
            root.cpu_s = round(self.rate * (root.end - root.start), 2)
            root.children_cpu = round(sum(x[1] - x[0] for x in self.procs) * 0.3, 2)
            root.rss_mb = round(root.rss_mb * rng.uniform(1.05, 1.4), 1)
            root.rd_mb, root.wr_mb = round(rng.uniform(5, 60), 3), round(rng.uniform(2, 40), 3)
            ext = [endpoint("tcp", ip, 443, self.net_tx, self.net_rx, 10 + self.calls * 9, provider, host)]
            root.net = {"tx": self.net_tx, "rx": self.net_rx, "calls": ext[0]["calls"],
                        "endpoints": ext, "in_series": True}
            root.conns = {"n": 1, "accepted": 0, "established": 1,
                          "failed": 0, "external": 1, "rx_bytes": self.net_rx,
                          "tx_bytes": self.net_tx, "conn_s_total": round(root.end - root.start, 1),
                          "connect_ms_p50": 31.2, "connect_ms_max": 61.0, "providers": {provider: 1},
                          "peers": [{"daddr": ip, "dport": 443, "n": 1, "external": True,
                                     "provider": provider, "inbound": 0, "failed": 0,
                                     "rx_bytes": self.net_rx, "tx_bytes": self.net_tx}],
                          "peers_total": 1}
            b.exit(root)


# ----------------------------------------------------------- what tasks run
def tests_cmd(lang, tst, flags=""):
    name = tst.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if lang == "cpp":
        return "cmake --build build -j 8 && ctest --test-dir build -R %s --output-on-failure" % name
    if lang == "rust":
        return "cargo test --test %s%s" % (name, flags)
    return "python -m pytest %s -q%s" % (tst, flags)


def long_run(b, lang):
    """A run long enough to matter, on the login node: a benchmark, an evaluation."""
    if lang == "cpp":
        return "./build/nbody --steps 4000 --out runs/scan.h5"
    if lang == "rust":
        return "cargo bench --bench passes"
    return "python %s --config configs/base.yaml" % b.rng.choice(
        [f for f in b.proj["files"] if f.endswith(".py")] or ["run.py"])


def task(ag, t, limit, topic, opts):
    """One task of a session, from its opening read to its commit. Returns its end,
    or the time it stopped because the session ran out."""
    b, rng, proj = ag.b, ag.b.rng, ag.b.proj
    title, src, tst = topic
    lang = proj["lang"]
    claude = ag.atype == "claude_code"

    def gap(lo, hi):
        return rng.uniform(lo, hi)

    def go(t, cmd, **kw):
        if t >= limit:
            return t
        end = ag.call(t, cmd, **kw)
        if claude and rng.random() < 0.25:
            ag.chore(end + rng.uniform(0.3, 2.0), "git",
                     "git -c core.fsmonitor= -c core.hooksPath=/dev/null status --porcelain")
        return end

    # 1. open: the files the task is about, and how their tests stand
    t = go(t, "sed -n '1,120p' %s && %s 2>&1 | tail -5" % (src, tests_cmd(lang, tst)), exit_code=1)
    if opts.get("log"):
        t = go(t + gap(8, 30), "cat >> %s <<'EOF'\n\n## %s\n- %s\nEOF" % (
            opts["log"], title, rng.choice(("started", "picking this up", "plan: read, fix, test"))))
    # 2. look around
    sym = rng.choice(proj["symbols"])
    looks = ["rg -n '%s' src" % sym if lang != "python" or "src/" in src else "rg -n '%s' ." % sym,
             "sed -n '120,240p' %s" % src, "cat %s" % tst, "git log --oneline -5 -- %s" % src,
             "head -60 %s" % tst, "wc -l %s %s" % (src, tst), "git status", "ls %s" % (src.rsplit("/", 1)[0] if "/" in src else ".")]
    for cmd in rng.sample(looks, rng.randint(5, 7)):
        if claude and rng.random() < 0.3:
            t = ag.grep(t + gap(8, 30), sym, b.pdir)
        else:
            t = go(t + gap(8, 30), cmd)
        if t >= limit:
            return t
    # 3. sometimes the person is away a while
    if rng.random() < 0.25:
        t += gap(360, 900)
    # 4. edit: mostly with the agent's own tool, which leaves no command
    if ag.atype == "codex" and ag.sandbox_calls == "codex":
        for _ in range(rng.randint(2, 5)):
            t = ag.fs_helper(t + gap(20, 60))
    elif rng.random() < 0.35:
        for f in (src, tst):
            t = go(t + gap(20, 60), "sed -i 's/%s/%s/' %s" % (sym, sym + "_v2", f))
    else:
        t += gap(60, 180)
    # 5. run the tests until they pass
    n = rng.randint(4, 7)
    for i in range(n):
        flags = rng.choice(("", "", " -x", " -k %s" % sym.lower())) if lang == "python" else ""
        t = go(t + gap(45, 180), "%s 2>&1 | tail -20" % tests_cmd(lang, tst, flags),
               exit_code=0 if i >= n - 2 else 1, scale=rng.uniform(0.5, 2.0))
        if t >= limit:
            return t
    # 6. maybe one long run, on the login node
    if rng.random() < opts.get("long", 0.35):
        t = go(t + gap(20, 90), long_run(b, lang), dur=rng.uniform(320, 700), scale=200 if opts.get("heavy") else 60)
    # now and then a subagent, from a tool call: a second opinion, a log summarised
    if claude and rng.random() < opts.get("nest", 0.0) and t < limit - 600:
        if rng.random() < 0.4:
            t = _nested_codex(ag, t + gap(20, 90))
        else:
            t = _nested_claude(ag, t + gap(20, 90), b.pdir)
    # 7. maybe a Slurm job, polled to the end
    if opts.get("slurm") and rng.random() < 0.6:
        t = slurm_task(ag, t + gap(30, 120), limit, opts)
    # 8. close: the commit (and a pull request) in the agent's own words
    if t < limit:
        t = go(t + gap(20, 60), 'git add -A && git commit -m "%s"' % title)
        branch = "feat/" + "-".join(title.lower().split()[:3])
        t = go(t + gap(10, 40), "git push origin %s" % branch)
        if opts.get("pr") and rng.random() < 0.6:
            t = go(t + gap(10, 40), 'gh pr create --title "%s" --body "%s"' % (
                title, rng.choice(("Adds tests.", "See the commit for details.", "Fixes the flaky case."))))
    return t


def slurm_task(ag, t, limit, opts):
    """Submit with --parsable, poll the queue until the job ends, read its output."""
    b, rng, proj = ag.b, ag.b.rng, ag.b.proj
    script = rng.choice(proj["jobs"] or ["job.sh"])
    gpu = rng.choice((1, 2, 4)) if "train" in script or "hmc" in script or "sweep" in script else None
    arr = "0-%d%%4" % rng.choice((7, 15)) if "sweep" in script or "array" in script else None
    if rng.random() < 0.12:                             # asks for too long, is refused, asks again
        t, _ = ag.submit(t, script, gpu, arr, refuse=True)
        t = ag.call(t + rng.uniform(20, 60), "sinfo -p gpu -o '%P %l'")
    t, jid = ag.submit(t + rng.uniform(5, 30), script, gpu, arr)
    if jid is None:
        return t
    every = rng.uniform(60, 125)
    for _ in range(rng.randint(6, 11)):
        if t >= limit:
            return t
        t = ag.call(t + every + rng.uniform(-4, 4), squeue_args(b, jid, True))
    t = ag.call(t + rng.uniform(10, 40), "sacct -j %d --format=JobID,State,Elapsed,MaxRSS" % jid)
    log = "logs/%s-%d.out" % (script.rsplit("/", 1)[-1].split(".")[0], jid)
    for _ in range(rng.randint(2, 4)):
        t = ag.call(t + rng.uniform(8, 30), "tail -n 50 %s" % log)
    return t


def topics(b, n):
    """Up to `n` of the project's topics, none twice: a session that runs out of
    them has done its work."""
    ts = list(b.proj["topics"])
    b.rng.shuffle(ts)
    return ts[:n]


def side_effects(ag, t):
    """What an agent leaves outside its project: run directories on scratch, logs in
    /tmp, a package installed, a dotfile changed. Read by the Agents tab's blast
    radius and the Risk tab; a session launched with a bypass flag does more of it."""
    b, rng, p = ag.b, ag.b.rng, ag.b.p
    run = "run_%02d" % rng.randint(1, 60)
    scratch = cast.scratch(p)
    pool = [("mkdir -p %s/runs/%s && cp configs/base.yaml %s/runs/%s/" % (scratch, run, scratch, run), {}),
            ("python %s --dry-run > /tmp/%s-%s.log 2>&1" % (
                next((f for f in b.proj["files"] if f.endswith(".py")), "run.py"), b.user, run), {})]
    if b.proj["lang"] == "python":
        pool.append(("pip install -e .", {"scale": 1.5}))
    elif b.proj["lang"] == "cpp":
        pool.append(("rm -rf build && cmake -B build -DCMAKE_BUILD_TYPE=Release", {}))
    if ag.root.approval_mode == "bypassed":
        other = rng.choice([x["user"] for x in cast.PEOPLE[:12] if x["lab"] == p["lab"]
                            and x["user"] != b.user] or ["ada"])
        pool += [("cp results/summary.csv /scratch/%s/shared/" % p["lab"], {}),
                 ("echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc", {}),
                 ("rm -rf ~/.cache/pip", {}),
                 ("cp results/summary.csv /home/%s/shared/" % other, {}),
                 ("git push --force origin main", {})]
    for cmd, kw in rng.sample(pool, min(len(pool), rng.randint(1, 3))):
        t = ag.call(t + rng.uniform(10, 60), cmd, **kw)
    return t


# ------------------------------------------------------------ session kinds
def _work(ag, t, opts, risky=None):
    """Tasks back to back until the session's time is up, then the root's exit."""
    b, rng = ag.b, ag.b.rng
    limit = ag.root.end - 30
    effects_at = rng.randint(0, 2) if rng.random() < (0.8 if ag.root.approval_mode else 0.45) else -1
    for k, topic in enumerate(topics(b, 6)):
        if t >= limit - 600:
            break
        if risky and k == 0:
            t = risky(ag, t)
        if k == effects_at:
            t = side_effects(ag, t)
        t = task(ag, t, limit, topic, opts)
        t += rng.uniform(30, 240)
        if rng.random() < 0.15:                         # lunch, a meeting
            t += rng.uniform(1800, 4800)
    return t


def _launch(b, approval, extra=""):
    flag = {"bypassed": " --dangerously-skip-permissions", "auto": " --permission-mode auto"}.get(approval, "")
    return "claude" + flag + extra


def _risky_claude(ag, t):
    """What makes the Risk tab: a GPU check, a package install, compute on the login node."""
    b, rng = ag.b, ag.b.rng
    p = b.p
    if p.get("login_compute"):
        t = ag.call(t + 10, "pip install --user xesmf==0.8.7", scale=1.5, exit_code=rng.choice((0, 0, 1)))
        t = ag.call(t + 20, "conda create -y -n regrid python=3.11 xarray dask", scale=2)
        # the whole year on the login node, not in a job: what the panel is for
        t = ag.call(t + 30, "python regrid.py --year 2023 --workers 16", dur=rng.uniform(1800, 5400),
                    scale=600)
    else:
        t = ag.call(t + 10, "nvidia-smi")
        if rng.random() < 0.5:
            t = ag.call(t + 15, "ssh %s nvidia-smi" % rng.choice(cast.COMPUTE))
    return t


def claude_terminal(b):
    s, p, rng = b.spec, b.p, b.rng
    appr = p.get("approval", "supervised")
    ag = Agent(b, "claude_code", "claude", _launch(b, appr), s.start, s.end,
               tty="pts/%d" % rng.randint(1, 48), approval=appr,
               sandbox_calls="bwrap" if p.get("sandboxed") else None)
    opts = {"slurm": p.get("slurm"), "log": p.get("keeps_log"), "pr": p["user"] in ("ada", "edsger"),
            "heavy": p.get("login_compute"), "long": 0.5 if p.get("login_compute") else 0.3,
            "nest": 0.08}
    t = s.start + rng.uniform(5, 40)
    t = ag.chore(t, "git", "git -c core.fsmonitor= -c core.hooksPath=/dev/null status --porcelain")
    _work(ag, t, opts, risky=_risky_claude if (p.get("login_compute") or rng.random() < 0.5) else None)
    ag.finish()


def claude_nested(b):
    """A Claude session that hands work to other agents from its tool calls."""
    s, p, rng = b.spec, b.p, b.rng
    ag = Agent(b, "claude_code", "claude", _launch(b, p.get("approval")), s.start, s.end,
               tty="pts/%d" % rng.randint(1, 48), approval=p.get("approval"))
    opts = {"slurm": p.get("slurm"), "pr": True}
    t = s.start + rng.uniform(5, 40)
    limit = ag.root.end - 30
    tp = topics(b, 3)
    t = task(ag, t, limit, tp[0], opts)
    # a subagent: `claude -p` reads the run's logs and reports back
    if t < limit - 900:
        t = _nested_claude(ag, t + rng.uniform(30, 120), "%s/runs" % cast.scratch(p))
    if t < limit - 900:
        t = _nested_codex(ag, t + rng.uniform(30, 120))
    for topic in tp[1:]:
        if t >= limit - 600:
            break
        t = task(ag, t + rng.uniform(30, 240), limit, topic, opts)
    ag.finish()


def _nested_claude(ag, t, where):
    b, rng = ag.b, ag.b.rng
    cmd = "cd %s && claude -p [REDACTED-PROMPT] --output-format text" % where
    shell = b.child(ag.root, t, "bash", ag.snap() + q(cmd) + " < /dev/null && pwd -P >| " + ag.cwdfile,
                    redacted=["prompt"])
    shell.sandbox_src = "none:exec"
    dur = rng.uniform(70, 240)
    sub = Agent(b, "claude_code", "claude", "claude -p [REDACTED-PROMPT] --output-format text",
                t + 0.01, t + dur, cwd=where, parent=shell, tree_root=ag.root.pid,
                redacted=["prompt"], residency=False)
    tt = t + rng.uniform(4, 10)
    for c in ("ls -la", "tail -n 200 slurm-*.out", "rg -n 'Error|Traceback' .", "du -sh ."):
        tt = sub.call(tt + rng.uniform(3, 15), c)
    sub.root.end = max(sub.root.end, tt + 2)
    sub.finish()
    ag.nested.append(sub)
    shell.end = sub.root.end + 0.01
    shell.cpu_s = 0.01
    b.exit(shell)
    ag.procs.append((shell.start, shell.end, "bash", 4, 1))
    ag.busy.append((t, shell.end))
    return shell.end


def _nested_codex(ag, t):
    b, rng = ag.b, ag.b.rng
    cmd = "codex exec --full-auto [REDACTED-PROMPT]"
    shell = b.child(ag.root, t, "bash", ag.snap() + q(cmd) + " < /dev/null && pwd -P >| " + ag.cwdfile,
                    redacted=["prompt"])
    shell.sandbox_src = "none:exec"
    sub = Agent(b, "codex", "codex", cmd, t + 0.01, t + rng.uniform(60, 200), parent=shell,
                tree_root=ag.root.pid, redacted=["prompt"], residency=False)
    tt = t + rng.uniform(3, 8)
    for c in ("rg -n TODO src", "sed -n '1,80p' %s" % b.proj["files"][0], tests_cmd(b.proj["lang"], b.proj["tests"][0])):
        tt = sub.call(tt + rng.uniform(4, 20), c)
    sub.root.end = max(sub.root.end, tt + 2)
    sub.finish()
    ag.nested.append(sub)
    shell.end = sub.root.end + 0.01
    shell.cpu_s = 0.01
    b.exit(shell)
    ag.procs.append((shell.start, shell.end, "bash", 4, 1))
    return shell.end


def claude_babysit(b):
    """An evening session left to watch a job overnight: a short fix, a submission,
    then hours of `sleep && squeue` until the job ends, and the results in the
    morning. The agent polls; the person is asleep."""
    s, p, rng = b.spec, b.p, b.rng
    appr = p.get("approval", "supervised")
    ag = Agent(b, "claude_code", "claude", _launch(b, appr), s.start, s.end,
               tty="pts/%d" % rng.randint(1, 48), approval=appr)
    limit = ag.root.end - 30
    topic = topics(b, 1)[0]
    t = task(ag, s.start + rng.uniform(5, 40), s.start + 3000, topic, {"long": 0.0})
    script = (b.proj["jobs"] or ["job.sh"])[0]
    t, jid = ag.submit(t + rng.uniform(30, 90), script, gpu=rng.choice((1, 2, 4)))
    every = rng.choice((300, 600, 900))
    while jid is not None and t < limit - 2400:
        t = ag.call(t + rng.uniform(3, 12), "sleep %d && squeue -j %d -h -o %%T" % (every, jid))
        if rng.random() < 0.15:
            t = ag.call(t + rng.uniform(3, 10), "tail -n 20 logs/%s-%d.out" % (
                script.rsplit("/", 1)[-1].split(".")[0], jid))
    if jid is not None and t < limit - 300:
        t = ag.call(t + 10, "sacct -j %d --format=JobID,State,Elapsed,MaxRSS" % jid)
        t = ag.call(t + 20, "tail -n 50 logs/%s-%d.out" % (script.rsplit("/", 1)[-1].split(".")[0], jid))
        ag.call(t + 40, 'git add -A && git commit -m "Record the overnight run %d"' % jid)
    ag.finish()


def codex_tui(b):
    """Codex in a terminal, sandboxed: most calls isolated, some whose sandbox failed."""
    s, p, rng = b.spec, b.p, b.rng
    ag = Agent(b, "codex", "codex", "codex --sandbox workspace-write", s.start, s.end,
               tty="pts/%d" % rng.randint(1, 48), sandbox_calls="codex")
    t = s.start + rng.uniform(3, 20)
    for c, a in (("lsb_release", "/bin/sh /usr/bin/lsb_release -a"), ("getconf", "getconf LONG_BIT"),
                 ("git", "git -c safe.bareRepository=explicit rev-parse HEAD")):
        t = ag.chore(t + 0.2, c, a)
    opts = {"slurm": p.get("slurm"), "long": 0.4}

    def risky(ag, t):
        t = ag.sandbox_failed(t + 5, "make -j16")
        return ag.call(t + rng.uniform(20, 60), "make -j16", sandbox=False, scale=4)
    _work(ag, t, opts, risky=risky)
    ag.finish()


def codex_app(b):
    """The Codex app over SSH: an app-server whose approval policy arrives per thread."""
    s, rng = b.spec, b.rng
    ag = Agent(b, "codex", "codex", "%s/.local/bin/codex -c features.code_mode_host=true app-server "
               "--listen unix://%s/.codex/app/%x.sock" % (b.home, b.home, rng.getrandbits(32)),
               s.start, s.end, approval="unobservable")
    _work(ag, s.start + rng.uniform(5, 30), {"long": 0.3})
    ag.finish()


def codex_exec(b):
    """`codex exec` from a script: a short, unattended run."""
    s, rng = b.spec, b.rng
    bypass = rng.random() < 0.4
    args = ("codex exec --dangerously-bypass-approvals-and-sandbox [REDACTED-PROMPT]" if bypass
            else "codex exec --full-auto [REDACTED-PROMPT]")
    ag = Agent(b, "codex", "codex", args, s.start, s.end, redacted=["prompt"],
               approval="bypassed" if bypass else "supervised")
    t = s.start + rng.uniform(2, 6)
    lang = b.proj["lang"]
    for cmd in ("git status --short", "rg -n TODO src", "sed -n '1,80p' %s" % b.proj["files"][0],
                tests_cmd(lang, b.proj["tests"][0]), "git diff --stat"):
        t = ag.call(t + rng.uniform(4, 25), cmd)
    # the run logs to W&B with a key on its command line: masked by the collector
    t = ag.call(t + 5, "env WANDB_API_KEY=[REDACTED] python3 scripts/eval.py --log", scale=3,
                redacted=["secret_assign"])
    if rng.random() < 0.3:
        t = ag.call(t + 5, "huggingface-cli download google/vit-base-patch16-224 --local-dir models/vit",
                    scale=2)
    ag.finish()


def claude_nightly(b):
    """A nightly run from cron: Claude, scripted and with its permission checks off,
    working through the backlog while the lab sleeps."""
    s, rng = b.spec, b.rng
    ag = Agent(b, "claude_code", "claude", "claude --dangerously-skip-permissions -p [REDACTED-PROMPT] "
               "--output-format stream-json --verbose", s.start, s.end, redacted=["prompt"],
               approval="bypassed")
    _work(ag, s.start + rng.uniform(3, 15), {"long": 0.3, "pr": True})
    ag.finish()


def claude_print(b):
    """`claude -p` from a script: one prompt, a few calls, an answer on stdout."""
    s, rng = b.spec, b.rng
    bypass = rng.random() < 0.5
    args = "claude%s -p [REDACTED-PROMPT] --output-format json" % (
        " --dangerously-skip-permissions" if bypass else "")
    ag = Agent(b, "claude_code", "claude", args, s.start, s.end, redacted=["prompt"],
               approval="bypassed" if bypass else "supervised")
    t = s.start + rng.uniform(2, 6)
    for cmd in ("git log --oneline -10", "cat configs/base.yaml", "ls runs | tail -5"):
        t = ag.call(t + rng.uniform(4, 25), cmd)
    if rng.random() < 0.4:
        t = ag.call(t + 5, 'curl -s -H "Authorization: Bearer [REDACTED]" '
                    "https://api.github.com/repos/demo-org/vit-pruning/actions/runs",
                    redacted=["auth_header"])
    ag.finish()


def claude_desktop(b):
    """The Claude desktop app's Code tab over SSH: its remote server, and the
    `ccd-cli` sessions it starts."""
    s, p, rng = b.spec, b.p, b.rng
    srv = Proc(pid=b.pid(), ppid=1, uid=b.uid, user=b.user, tty=None, command="server",
               args="%s/.claude/remote/srv/be1c1ca50b7b27c5/server --serve --socket "
                    "%s/.claude/remote/run/%x/rpc.sock" % (b.home, b.home, rng.getrandbits(32)),
               cwd=b.home, actor="agent", actor3="agent", attribution="ancestry",
               agent_type="claude_code", is_agent=True, sandbox="unsandboxed", sandbox_src="none:exec",
               cgroup=b.scope, start=s.start, end=s.end, rss_mb=round(rng.uniform(60, 120), 1),
               threads=12, cpu_s=round((s.end - s.start) * 0.002, 2))
    srv.agent_pid = srv.tree_root_pid = srv.pid
    srv.session_uuid = _uuid(b.hf.boot_id, srv.pid, s.start)
    host = Agent.around(b, srv, 0.002)                  # the server ticks as the tree's root
    appr = p.get("approval", "auto")
    t = s.start + rng.uniform(20, 90)
    while t < s.end - 1800:
        dur = min(s.end - t - 60, rng.uniform(2400, 7200))
        mode = {"auto": "auto", "bypassed": "bypassPermissions"}.get(appr, "acceptEdits")
        sess = Agent(b, "claude_code", "%s-3f6ee6c72f6a" % CLAUDE_VER,
                     "%s/.claude/remote/ccd-cli/%s-3f6ee6c72f6a --output-format stream-json --verbose "
                     "--input-format stream-json --permission-mode %s" % (b.home, CLAUDE_VER, mode),
                     t, t + dur, parent=srv, tree_root=srv.pid, approval=appr,
                     residency=False, sockets=False)
        _work(sess, t + rng.uniform(5, 30), {"slurm": p.get("slurm"), "long": 0.3})
        sess.finish()
        host.nested.append(sess)
        t = sess.root.end + rng.uniform(300, 1800)
    host.finish()


def claude_rc(b):
    """Remote Control: `claude rc` in a terminal, a worker per session opened from
    a phone or a browser."""
    s, rng = b.spec, b.rng
    rc = Agent(b, "claude_code", "claude", "claude rc", s.start, s.end, tty="pts/%d" % rng.randint(1, 48))
    rc.rate = 0.003
    t = s.start + rng.uniform(60, 300)
    while t < s.end - 1200:
        cse = "cse_01%s" % ("%x" % rng.getrandbits(64))[:12]
        dur = min(s.end - t - 60, rng.uniform(1200, 4000))
        w = Agent(b, "claude_code", CLAUDE_VER,
                  "%s/.local/share/claude/versions/%s --print --sdk-url https://api.anthropic.com/v1/code/"
                  "sessions/%s --session-id %s" % (b.home, CLAUDE_VER, cse, cse),
                  t, t + dur, parent=rc.root, tree_root=rc.root.pid, residency=False)
        _work(w, t + rng.uniform(5, 30), {"long": 0.2})
        w.finish()
        rc.nested.append(w)
        t = w.root.end + rng.uniform(600, 2400)
    rc.finish()


# ------------------------------------------------------------------- IDEs
VSCODE = "Stable-1e3c0a8a7d2b9f4c1e6a3b5d7f9e2c4a6b8d0f1e"


def _vscode_tree(b, s):
    """The VS Code server a person's editor runs on the login node, its helpers,
    and its integrated terminal."""
    rng = b.rng
    base = "%s/.vscode-server/cli/servers/%s/server" % (b.home, VSCODE)
    srv = Proc(pid=b.pid(), ppid=b.pid(), uid=b.uid, user=b.user, tty=None, command="node",
               args="%s/node %s/out/server-main.js --connection-token=remotessh --accept-server-license-terms "
                    "--start-server --enable-remote-auto-shutdown --socket-path=/tmp/code-%x"
                    % (base, base, rng.getrandbits(48)),
               cwd=b.home, actor="agent", actor3="human-vscode", attribution="ancestry",
               agent_type="vscode", is_agent=True, sandbox="unsandboxed", sandbox_src="none:exec",
               cgroup=b.scope, start=s.start, end=s.end, rss_mb=round(rng.uniform(150, 260), 1),
               threads=11)
    srv.agent_pid = srv.tree_root_pid = srv.pid
    srv.depth = 0
    srv.session_uuid = _uuid(b.hf.boot_id, srv.pid, s.start)
    ext = b.child(srv, s.start + 2, "node", "%s/node %s/out/bootstrap-fork --type=extensionHost "
                  "--transformURIs --useHostProxy=false" % (base, base), dur=s.end - s.start - 3)
    ext.rss_mb, ext.threads = round(rng.uniform(300, 700), 1), 13
    ext.sandbox_src = "none:exec"
    pty = b.child(srv, s.start + 2.5, "node", "%s/node %s/out/bootstrap-fork --type=ptyHost "
                  "--logsPath %s/.vscode-server/data/logs" % (base, base, b.home), dur=s.end - s.start - 4)
    pty.sandbox_src = "none:exec"
    return srv, ext, pty


def _vscode_helpers(b, ag_like, srv, ext, pty, t0, t1):
    """What VS Code runs on its own: git status for the source control view, search."""
    rng = b.rng
    t = t0 + 10
    while t < t1 - 20:
        what = rng.random()
        if what < 0.7:
            p = b.child(ext, t, "git", "git status -z -uall", cwd=b.pdir)
        elif what < 0.85:
            p = b.child(ext, t, "rg", "%s/.vscode-server/cli/servers/%s/server/node_modules/@vscode/"
                        "ripgrep/bin/rg --files --hidden --case-sensitive --no-require-git -g !**/.git"
                        % (b.home, VSCODE), cwd=b.pdir)
        else:
            p = b.child(ext, t, "git", "git fetch", cwd=b.pdir, scale=3)
        b.exit(p)
        ag_like.procs.append((p.start, p.end, p.command, p.rss_mb or 0, 1))
        # more often while someone is at the window than through the night
        day = 9 <= time.localtime(t).tm_hour < 19
        t = p.end + (logu(rng, 15, 240) if day else logu(rng, 40, 240))


def _vscode_terminal(b, ag_like, pty, t0, t1, hours=None):
    """The person typing in VS Code's terminal: IDE-class, as it runs in the IDE's tree.
    `hours` keeps a parked server's terminal to the working day."""
    rng = b.rng
    sh = b.child(pty, t0, "bash", "/bin/bash --init-file %s/.vscode-server/cli/servers/%s/server/out/vs/"
                 "workbench/contrib/terminal/common/scripts/shellIntegration-bash.sh"
                 % (b.home, VSCODE), dur=t1 - t0, cwd=b.pdir)
    sh.tty = "pts/%d" % rng.randint(1, 48)
    t = t0 + 20
    lang = b.proj["lang"]
    while t < t1 - 60:
        if hours and not hours[0] <= time.localtime(t).tm_hour < hours[1]:
            t += 900
            continue
        cmd, args = rng.choice([
            ("git", "git status"), ("git", "git diff"), ("ls", "ls"),
            ("python3", "python3 %s" % b.proj["files"][0]) if lang == "python" else ("cargo", "cargo build"),
            ("python3", "python3 -m http.server 8000") if b.user == "john" else ("make", "make -j4"),
            ("jupyter", "jupyter lab --no-browser --port 8890") if b.user == "john" else ("git", "git log -5")])
        p = b.child(sh, t, cmd, args, dur=logu(rng, 300, 3600) if args.startswith(("python3 -m http", "jupyter"))
                    else None, scale=2 if cmd in ("cargo", "make") else 1)
        p.end = min(p.end, t1 - 5)
        b.exit(p)
        ag_like.procs.append((p.start, p.end, p.command, p.rss_mb or 0, p.threads or 1))
        t = p.end + logu(rng, 60, 1200)
    b.exit(sh)


class _Tree:
    """A tree that is not an agent session (VS Code): ticks like one, with no calls."""

    def __init__(self, b, root):
        self.b, self.root = b, root
        self.procs, self.busy, self.nested = [], [], []

    def ticks(self, cls, atype):
        b, root, rng = self.b, self.root, self.b.rng
        tree_live = Live(self.procs)
        others = []
        for n in self.nested:
            others += n.procs + [(n.root.start, n.root.end, n.root.command, n.root.rss_mb, n.root.threads)]
        agents_live = Live(others)
        for T in b.hf.ticks(root.start + rng.uniform(30, 75), root.end):
            live = tree_live.at(T)
            nested = agents_live.at(T)
            n_all = 1 + len(live) + len(nested)
            thr = root.threads + sum(x[4] for x in live + nested)
            rss = round(root.rss_mb + sum(x[3] for x in live + nested), 1)
            rec = envelope("residency", T + 0.01, b.host, b.cpid(T))
            rec.update(root.identity())
            rec.update({"root_alive": True, "age_s": round(T - root.start, 1), "headless": True,
                        "n_procs": n_all, "threads": thr, "rss_mb": rss,
                        "cpu_s": round(0.01 * (T - root.start), 2), "d_state": 0, "max_depth": 3,
                        "agent_types": sorted({atype} | ({"claude_code"} if nested else set())),
                        "by_command": dict(Counter([root.command] + [x[2] for x in live + nested]).most_common(15)),
                        "d_stack": None, "state_counts": {"S": n_all}, "top_procs": [],
                        "tcp_open_ext": 1, "tcp_open": 3})
            b.emit(T + 0.01, "snapshot", rec)
            own = 1 + len(live)
            b.census[int(T // 60) * 60].append((cls, atype, b.user, own, root.threads + sum(x[4] for x in live),
                                                round(root.rss_mb + sum(x[3] for x in live), 1), 0))
            if nested:
                b.census[int(T // 60) * 60].append(("agent", "claude_code", b.user, len(nested),
                                                    sum(x[4] for x in nested),
                                                    round(sum(x[3] for x in nested), 1), 0))


def vscode(b, with_claude=False, parked=False):
    s, p, rng = b.spec, b.p, b.rng
    srv, ext, pty = _vscode_tree(b, s)
    tree = _Tree(b, srv)
    for x in (ext, pty):
        tree.procs.append((x.start, x.end, x.command, x.rss_mb or 0, x.threads or 1))
    _vscode_helpers(b, tree, srv, ext, pty, s.start, s.end)
    _vscode_terminal(b, tree, pty, s.start + rng.uniform(60, 600), s.end - 30,
                     hours=(9, 18) if parked else None)
    if with_claude:
        appr = p.get("approval", "supervised")
        mode = {"auto": " --permission-mode auto", "bypassed": " --permission-mode bypassPermissions"}.get(appr, "")
        t = s.start + rng.uniform(300, 1800)
        while t < s.end - 2400:
            dur = min(s.end - t - 120, rng.uniform(2400, 6000))
            cl = Agent(b, "claude_code", "claude",
                       "%s/.vscode-server/extensions/anthropic.claude-code-%s-linux-x64/resources/"
                       "native-binary/claude --output-format stream-json --verbose --input-format "
                       "stream-json --permission-prompt-tool stdio%s" % (b.home, CLAUDE_VER, mode),
                       t, t + dur, parent=ext, tree_root=srv.pid, approval=appr, residency=False)
            _work(cl, t + rng.uniform(10, 60), {"pr": True, "long": 0.3})
            cl.finish()
            tree.nested.append(cl)
            t = cl.root.end + rng.uniform(600, 3000)
    tree.ticks("human-vscode", "vscode")
    ext.cpu_s = round((s.end - s.start) * 0.004, 2)
    pty.cpu_s = round((s.end - s.start) * 0.0005, 2)
    b.exit(pty)
    b.exit(ext)
    srv.cpu_s = round((s.end - s.start) * 0.003, 2)
    b.exit(srv)


KINDS = {
    "claude_babysit": claude_babysit, "claude_nightly": claude_nightly,
    "claude_terminal": claude_terminal, "claude_nested": claude_nested, "codex_tui": codex_tui,
    "claude_desktop": claude_desktop, "vscode_claude": lambda b: vscode(b, True),
    "vscode": vscode, "vscode_parked": lambda b: vscode(b, parked=True), "human_shell": human_shell, "codex_exec": codex_exec,
    "claude_print": claude_print, "claude_rc": claude_rc, "codex_app": codex_app,
}
assert set(KINDS) == set(DURATION)


def build(spec, world):
    """Every record `spec`'s session writes, and its per-minute census."""
    b = Builder(spec, world)
    KINDS[spec.kind](b)
    # nothing after the session's own end (and its last exit's moment): a window
    # past it must find nothing new, however the windows were cut
    out = [r for r in b.out if r[0] < spec.end + 1.0]
    return out, b.census
