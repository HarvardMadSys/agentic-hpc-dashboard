"""What each kind of session writes: its processes, as the collector records them.

A builder walks one planned session (`world.Spec`) forward in time and writes
every record it will ever produce: each process's `exit`, the agent's
`residency` ticks and its `conn`/`netio` records, and a `submit` for each
Slurm submission. It also notes, per minute, what it had running (`census`),
which is what the host's heartbeat counts.

The shapes are the collector's (records.py) and the argv is what Claude Code,
Codex, VS Code and a person at a shell really run, so every reducer reads them
as it reads a capture: a tool call is a shell with its payload beneath it, a
sandboxed one has `bwrap` or `codex-linux-sandbox` above it, a task is named by
the commit it ends with. Order is the collector's too: a child's exit is written
before the shell that waited on it.
"""
import random
from collections import defaultdict

from . import cast
from .records import Proc, costs, envelope, exit_record, line, logu

# The I/O figures a parent's exit holds of the children it reaped.
_IO = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb")

# How long a session of each kind runs, seconds (log-uniform).
DURATION = {
    "claude_terminal": (2400, 4 * 3600), "claude_nested": (3600, 3 * 3600),
    "codex_tui": (1800, 3 * 3600), "claude_desktop": (1800, 3 * 3600),
    # an editor is often left connected overnight, its git polling and all
    "vscode_claude": (3 * 3600, 16 * 3600), "vscode": (4 * 3600, 18 * 3600),
    "claude_babysit": (8 * 3600, 12 * 3600), "claude_nightly": (5400, 3 * 3600),
    "vscode_parked": (86340, 86340),
    "human_shell": (3600, 5 * 3600), "codex_exec": (180, 1200),
    "claude_print": (60, 600), "claude_rc": (3600, 4 * 3600),
    "codex_app": (3600, 5 * 3600),
}


# Sessions that start at a set time of night, whatever the person's working hours:
# the local hours they start between.
START_AT = {"claude_babysit": (19.0, 22.5), "claude_nightly": (0.5, 3.0)}


# A program and what it runs beneath itself: (command, argv, fewest, most).
CC1 = "/usr/libexec/gcc/x86_64-redhat-linux/11/cc1plus -quiet -I include src/%(f)s.cpp -O2 -o /tmp/cc%(i)04d.s"
SPAWNS = {
    "make": [("cc1plus", CC1, 4, 14), ("as", "as --64 -o build/%(f)s.o /tmp/cc%(i)04d.s", 4, 14),
             ("ld", "/usr/bin/ld -plugin -o build/nbody build/%(f)s.o -lhdf5", 1, 1)],
    "cmake": [("cc1plus", CC1, 3, 10), ("ld", "/usr/bin/ld -o build/test_%(f)s build/%(f)s.o", 1, 2)],
    "cargo": [("rustc", "rustc --crate-name %(f)s --edition=2021 src/lib.rs --crate-type lib "
               "--emit=dep-info,metadata,link -C opt-level=3", 3, 12),
              ("cc", "cc -m64 -o target/release/deps/%(f)s-%(i)x target/release/deps/%(f)s.o", 1, 1)],
    "ctest": [("test_tree", "./build/tests/test_%(f)s", 1, 3)],
    "pip": [("python3", "python3 -c import setuptools; exec(compile(open(setup.py).read()))", 1, 2),
            ("git", "git rev-parse HEAD", 0, 1), ("gcc", "gcc -pthread -fPIC -c src/%(f)s.c", 0, 4)],
    "snakemake": [("python3", "python3 -m snakemake --snakefile Snakefile --target-jobs %(f)s", 2, 6)],
    "conda": [("python3", "python3 -c from conda.cli import main", 1, 2)],
}
SRC_STEMS = ("tree", "nbody", "io", "kernel", "util", "grid", "solver", "parse")


def duration(kind, rng):
    return logu(rng, *DURATION[kind])


def job_id(t):
    """A Slurm job id for a submission at `t`: increasing, one a second."""
    return 10_000_000 + int(t % 40_000_000)


class Builder:
    """One session being written."""

    def __init__(self, spec, world):
        self.spec, self.world = spec, world
        self.p = spec.person
        self.user, self.uid = self.p["user"], self.p["uid"]
        self.host = spec.host
        self.hf = world.hostgen[spec.host]
        self.rng = random.Random(spec.seed + "|build")
        self.out = []
        self.census = defaultdict(list)
        self._pid = spec.pid_base + 1
        self.home = cast.home(self.p)
        self.proj = cast.PROJECTS[self.p["project"]]
        self.pdir = cast.project_dir(self.p)
        self.scope = "/user.slice/user-%d.slice/session-%d.scope" % (
            self.uid, 100 + (spec.idx or 0) * 7 + self.rng.randint(0, 6))
        # pid -> the I/O its exited children carried, for its own exit (`exit`);
        # and the pids whose exit is written, until a new process takes one
        self._reaped = {}
        self._written = set()

    # ------------------------------------------------------------ plumbing
    def pid(self):
        """The next pid of this session's block. Past its end it wraps, but over the
        first hundred, where the session's long-lived processes (its root, an editor's
        servers) are, so a wrapped pid is only ever a dead process's."""
        from .world import PID_BLOCK
        self._pid += self.rng.randint(1, 2)
        if self._pid >= self.spec.pid_base + PID_BLOCK - 10:
            self._pid = self.spec.pid_base + 100 + (self._pid % 997)
        self._written.discard(self._pid)
        self._reaped.pop(self._pid, None)
        return self._pid

    def emit(self, t, stream, rec):
        self.out.append((t, len(self.out), "ebpf", stream, line(rec)))

    def cpid(self, t):
        return self.hf.collector_pid(t)

    def exit(self, p):
        """`p`'s exit. Its I/O is the kernel's figure, `task->signal->ioac`: its own
        and every child it reaped -- each child whose exit was written before its
        own, which is every one it waited for. One that outlived it was reaped by
        init. The dashboard takes them back off (reducers/own_io.py)."""
        rec = exit_record(p, self.host, self.cpid(p.end), self.rng)
        io = rec.get("io")
        kids = self._reaped.pop(p.pid, None)
        if io is not None:
            if kids:
                for k, v in zip(_IO, kids):
                    io[k] = round(io[k] + v, 3)
            if p.ppid not in self._written:
                acc = self._reaped.setdefault(p.ppid, [0.0] * len(_IO))
                for i, k in enumerate(_IO):
                    acc[i] += io[k]
        self._written.add(p.pid)
        self.emit(p.end, "exits", rec)

    def child(self, parent, t, command, args, *, dur=None, scale=1.0, cwd=None, job=False, **kw):
        """A process `parent` started at `t`: it inherits who it is, and gets its
        own costs. Not written until `exit` is called for it. `job` for a command
        line a terminal's shell ran, which job control puts in a process group of
        its own (what it starts inherits that group); anything else is in its
        parent's group, as the prompt's `git` is in the shell's."""
        cpu, d, rss, rd, wr, thr = costs(self.rng, command, dur, scale)
        p = Proc(pid=self.pid(), ppid=parent.pid, uid=self.uid, user=self.user,
                 tty=parent.tty, command=command, args=args, cwd=cwd or parent.cwd,
                 actor=parent.actor, actor3=parent.actor3, attribution=parent.attribution,
                 agent_type=parent.agent_type, agent_pid=parent.agent_pid,
                 session_uuid=parent.session_uuid, tree_root_pid=parent.tree_root_pid,
                 depth=(parent.depth + 1) if parent.depth is not None else None,
                 is_agent=False,
                 # a child inherits its parent's namespaces; only a process the
                 # classifier tags, or the first one inside a wrapper, is read at exec
                 sandbox=parent.sandbox, sandbox_src="inherit" if parent.sandbox else None,
                 sandbox_detail=None, sandbox_ancestry=parent.sandbox_ancestry,
                 cgroup=parent.cgroup, pgrp=parent.pgrp, sid=parent.sid,
                 start=t, end=t + d, cpu_s=cpu, rss_mb=rss, rd_mb=rd, wr_mb=wr, threads=thr)
        if job:
            p.pgrp = p.pid
        for k, v in kw.items():
            setattr(p, k, v)
        return p

    def spawn(self, parent):
        """What a build or a test run starts beneath itself: a compiler per file, a
        rustc per crate. Each lies inside its parent's life, and is written before it."""
        rng = self.rng
        kind = SPAWNS.get(parent.command)
        if kind is None or parent.end - parent.start < 0.5:
            return []
        out = []
        for command, args, lo, hi in kind:
            n = rng.randint(lo, hi)
            for i in range(n):
                span = parent.end - parent.start
                t = parent.start + span * (0.05 + 0.85 * i / max(1, n))
                k = self.child(parent, t, command, args % {"i": i, "f": rng.choice(SRC_STEMS)},
                               dur=min(span * 0.9 / max(1, n) + 0.05, parent.end - t - 0.01))
                if k.end > k.start:
                    out.append(k)
        return out

    def alive(self, t0, t1, cls, agent_type, n_procs, threads, rss_mb, d_state=0):
        """Count this session in each minute of [t0, t1) of the host's census."""
        m = int(t0 // 60) * 60
        while m < t1:
            if m + 60 > t0:
                self.census[m].append((cls, agent_type, self.user, n_procs, threads,
                                       rss_mb, d_state))
            m += 60

    def submit_rec(self, p, tail, jid=None, **req):
        """The `submit` record the collector writes after an sbatch/srun/salloc exits."""
        rec = envelope("submit", p.end, self.host, self.cpid(p.end))
        rec.update(p.identity())
        rec.update({"partition": None, "gpus": None, "array": None, "time_limit_s": None,
                    "mem": None, "cpus_per_task": None, "req_src": None})
        rec.update(req)
        if any(req.get(k) is not None for k in ("partition", "gpus", "time_limit_s", "mem",
                                                 "array", "cpus_per_task")):
            rec["req_src"] = "argv"
        rec.update({"tool": p.command, "job_id": jid, "job_ids": [jid] if jid else None,
                    "work_dir": p.cwd, "exit_code": p.exit_code, "signal": p.signal,
                    "start": rec["ts"], "duration_s": round(p.end - p.start, 4),
                    "captured_bytes": len(tail or ""), "captured_tail": tail})
        self.emit(p.end + 0.001, "snapshot", rec)

    def done(self):
        return self.out, self.census


# ------------------------------------------------------------------ Slurm
def sbatch_args(b, script, parsable, gpu=None, array=None, hours=None):
    """argv for one submission, and the request fields the collector parses from it."""
    rng = b.rng
    part = cast.PARTITIONS["gpu" if gpu else rng.choice(("cpu", "cpu", "test"))]
    if hours is None:
        hours = rng.choice((1, 2, 4, 8, 12)) if gpu else rng.choice((1, 2, 6))
    a = ["sbatch"]
    if parsable:
        a.append("--parsable")
    a += ["-A", b.p["lab"], "-p", part,
          "-t", ("%d-00:00:00" % (hours // 24)) if hours >= 24 else "%d:00:00" % hours]
    req = {"partition": part, "time_limit_s": hours * 3600}
    if gpu:
        a.append("--gres=gpu:%d" % gpu)
        req["gpus"] = gpu
    if array:
        a.append("--array=%s" % array)
        req["array"] = array
    mem = rng.choice((None, "16G", "32G", "64G"))
    if mem:
        a.append("--mem=%s" % mem)
        req["mem"] = mem
    a.append(script)
    return " ".join(a), req


REFUSED = ("sbatch: error: QOSMaxWallDurationPerJobLimit\nsbatch: error: Batch job submission "
           "failed: Job violates accounting/QOS policy (job submit limit, user's size and/or "
           "time limits)\n")


def slurm_submit(b, parent, t, script, parsable, gpu=None, array=None, refuse=False,
                 argv=None, job=False):
    """sbatch under `parent` at `t`: its exit and its submit record. Returns (proc, job id).
    `argv` is `sbatch_args`'s answer when the caller already showed it (a tool call's text).
    `job` for one typed at a terminal's prompt (`Builder.child`)."""
    args, req = argv or sbatch_args(b, script, parsable, gpu, array, hours=72 if refuse else None)
    p = b.child(parent, t, "sbatch", args, job=job)
    if refuse:
        p.exit_code, jid, tail = 1, None, REFUSED
    else:
        jid = job_id(p.end)
        tail = ("%d\n" % jid) if parsable else ("Submitted batch job %d\n" % jid)
    b.exit(p)
    b.submit_rec(p, tail, jid, **req)
    return p, jid


def squeue_args(b, jid, agent):
    if agent:
        return b.rng.choice(("squeue -j %d -h -o %%T" % jid, "squeue --me -h -o '%i %T %M'",
                             "squeue -j %d" % jid))
    return b.rng.choice(("squeue -u %s" % b.user, "squeue --me", "squeue -u %s -t R" % b.user))


# ------------------------------------------------------------------ people
def human_shell(b):
    """A person in a terminal: an ssh login shell running what they type."""
    s, rng, p, proj = b.spec, b.rng, b.p, b.proj
    tty = "pts/%d" % rng.randint(1, 48)
    shell = Proc(pid=b.pid(), ppid=b.pid(), uid=b.uid, user=b.user, tty=tty, command="bash",
                 args="-bash", cwd=b.home, actor="human", actor3="human", attribution="tty",
                 depth=None, sandbox=None, sandbox_src=None, cgroup=b.scope,
                 start=s.start, end=s.end, cpu_s=0.0, rss_mb=5.2, threads=1)
    shell.pgrp = shell.sid = shell.pid
    t = s.start + rng.uniform(2, 20)
    jobs = []
    cpu = 0.0
    while t < s.end - 60:
        what = rng.choices(
            ("edit", "look", "git", "run", "slurm", "poll", "ssh", "move", "gpu", "risky"),
            (5, 6, 3, 3, 3 if p.get("slurm") else 0, 3 if jobs else 0, 1, 3 if p.get("mover") else 0.3,
             0.6, 0.5))[0]
        f = rng.choice(proj["files"])
        procs = []
        if what == "edit":
            procs.append(b.child(shell, t, "vim", "vim %s" % f, cwd=b.pdir, dur=logu(rng, 60, 1800), job=True))
        elif what == "look":
            for cmd, args in rng.sample([("ls", "ls -la"), ("cat", "cat %s" % f),
                                         ("less", "less %s" % f), ("grep", "grep -rn %s ." % rng.choice(proj["symbols"])),
                                         ("head", "head -50 %s" % f), ("wc", "wc -l %s" % f)], 2):
                procs.append(b.child(shell, t, cmd, args, cwd=b.pdir, job=True))
                t = procs[-1].end + rng.uniform(1, 12)
        elif what == "git":
            for args in rng.sample(["git status", "git diff", "git log --oneline -20", "git pull",
                                    "git add -u", 'git commit -m "wip"'], 2):
                procs.append(b.child(shell, t, "git", args, cwd=b.pdir, job=True))
                t = procs[-1].end + rng.uniform(2, 20)
        elif what == "run":
            if proj["lang"] == "cpp":
                procs.append(b.child(shell, t, "make", "make -j8", cwd=b.pdir, scale=3, job=True))
            elif p["lab"] == "bio_lab":
                procs.append(b.child(shell, t, "Rscript", "Rscript scripts/plot_counts.R",
                                     cwd=b.pdir, scale=4, job=True))
            else:
                procs.append(b.child(shell, t, "python3", "python3 %s" % f, cwd=b.pdir, scale=3, job=True))
        elif what == "slurm":
            script = rng.choice(proj["jobs"] or ["job.sh"])
            arr = "1-%d%%8" % rng.choice((12, 24, 48)) if "array" in script else None
            sb, jid = slurm_submit(b, shell, t, script, parsable=False, array=arr,
                                   gpu=rng.choice((None, None, 1)), job=True)
            jobs.append(jid)
            t = sb.end
        elif what == "poll":
            for _ in range(rng.randint(1, 3)):
                q = b.child(shell, t, "squeue", squeue_args(b, jobs[-1], False), job=True)
                b.exit(q)
                t = q.end + rng.uniform(5, 90)
            if rng.random() < 0.3:
                q = b.child(shell, t, "sacct", "sacct -j %d --format=JobID,State,Elapsed" % jobs[-1], job=True)
                b.exit(q)
                t = q.end
            if rng.random() < 0.08:
                q = b.child(shell, t, "scancel", "scancel %d" % jobs.pop(), job=True)
                b.exit(q)
                t = q.end
        elif what == "ssh":
            node = rng.choice(cast.COMPUTE)
            procs.append(b.child(shell, t, "ssh", "ssh %s" % node, dur=logu(rng, 20, 900), job=True))
        elif what == "move":
            src = "%s/runs/run_%02d" % (cast.scratch(p), rng.randint(1, 40))
            procs.append(b.child(shell, t, "rsync", "rsync -av %s/ %s/archive/" % (src, b.home),
                                 cwd=b.home, scale=2, job=True))
            procs.append(b.child(shell, procs[-1].end + 2, "tar", "tar czf run.tgz %s" % src,
                                 cwd=b.home, scale=2, job=True))
        elif what == "gpu":
            procs.append(b.child(shell, t, "nvidia-smi", "nvidia-smi", job=True))
        elif what == "risky":
            # what the Risk tab is for, at a person's hand; credentials arrive
            # masked, as the schema-8 collector writes them
            lab = "/scratch/%s" % p["lab"]
            cmd, args, red = rng.choice([
                ("rm", "rm -rf %s/tmp_%s" % (cast.scratch(p), rng.choice(("align", "sort", "run"))), None),
                ("chmod", "chmod -R 777 %s/shared/refs" % lab, None),
                ("wget", "wget -O data.tar.gz https://zenodo.org/records/%d/files/inputs.tar.gz"
                 % rng.randint(1000000, 9999999), None),
                ("conda", "conda install -y -c bioconda samtools", None),
                ("wandb", "wandb login --api-key=[REDACTED]", ["secret_flag"]),
                ("mysql", "mysql -u lims -p[REDACTED] lims_prod", ["db_password"]),
                ("curl", "curl -u %s:[REDACTED] https://repo.example.org/data/manifest.csv" % b.user,
                 ["curl_userpass"])])
            q = b.child(shell, t, cmd, args, cwd=b.home, scale=2 if cmd in ("wget", "conda") else 1, job=True)
            q.redacted = red
            procs.append(q)
        for q in procs:
            for k in b.spawn(q):
                b.exit(k)
            b.exit(q)
            cpu += q.cpu_s
            t = max(t, q.end)
        # the prompt coming back: __git_ps1 asks git where it is, every time
        for args in ("git rev-parse --git-dir --is-inside-git-dir --is-bare-repository "
                     "--is-inside-work-tree --short HEAD", "git status --porcelain"):
            g = b.child(shell, t + 0.01, "git", args, cwd=b.pdir)
            b.exit(g)
            t = g.end
        b.alive(t - 30, t, "human", None, 2, 2, 12.0)
        t += logu(rng, 15, 600)
    shell.end = max(t, s.end)
    shell.cpu_s = 0.002 * (shell.end - shell.start) / 60
    shell.children_cpu = cpu
    b.alive(s.start, shell.end, "human", None, 1, 1, 5.2)
    b.exit(shell)


