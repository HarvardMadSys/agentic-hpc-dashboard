"""Sporadicity: where a session was launched vs where it actually reached.

The blast radius of a session is the set of directories it touched, measured
against the directory it started in.  Two sources say where a process worked,
and neither is enough alone:

* **The recorded `cwd`.**  The collector inherits it from the parent and reads
  `/proc/<pid>/cwd` only for agent roots, tty processes and submits
  (collector/ebpf_trace.py `_inherit_cwd` and the exec predicate).  An agent's
  tool calls usually have no tty, so they carry the LAUNCH directory even after
  the agent has moved: on a real day 28 of 2,430 agent sessions showed more than
  one cwd.  So for agents the cwd is the anchor, not the evidence -- while for a
  human at a terminal, whose every process is read, a change of cwd IS the
  evidence of moving.

* **argv.**  Agents move by saying so: Claude Code runs every tool call as
  `zsh -c ... eval 'cd /elsewhere && ...'`, and an absolute path in argv names a
  directory exactly, whatever the cwd.  Those are the references counted here.

What neither shows is a file a program opens on its own -- an import, a config
read, the output a script writes.  Every number here is therefore a FLOOR on
reach, and the panel says so rather than implying the argv is the whole story.

Relative paths are resolved only against a base that is known: a cwd that was
read rather than inherited, or a directory the same command line has already
`cd`'d into by absolute path.  Resolving `../x` against an inherited cwd would
invent a sibling directory the agent may never have been near, so those are
counted as unresolved instead.
"""
import collections
import posixpath
import re

from . import pct

ZONES = ("cwd", "home", "home-dot", "tool-state", "tmp", "shared", "other-home",
         "system", "other")
ZONE_MEANING = {
    "cwd": "inside the session's launch directory",
    "home": "the user's own home, outside the launch directory",
    "home-dot": "a dot-file or dot-dir in the user's home (~/.ssh, ~/.bashrc, ~/.cache)",
    "tool-state": "an agent's or editor's own state (~/.claude, ~/.codex, "
                  "~/.vscode-server, /tmp/claude-*)",
    "tmp": "/tmp, /var/tmp, /dev/shm",
    "shared": "shared or cluster storage (reducers.sporadicity.shared_prefixes)",
    "other-home": "ANOTHER user's home directory",
    "system": "/etc, /usr, /opt, /var and the other system trees",
    "other": "anywhere else",
}
# Inside the launch dir, or the agent's own state: shown, never counted as reach.
CONTAINED = ("cwd", "tool-state")
RADIUS_BUCKETS = ("0", "1", "2", "3+", "root")
RANKS = ("radius", "writes", "dirs", "other_home")
OTHER_HOME_CAP = 100
RANK_MEANING = {
    "radius": "furthest reach: levels up from the launch dir",
    "writes": "most writes outside the launch dir",
    "dirs": "most distinct directories outside the launch dir",
    "other_home": "named a directory in ANOTHER user's home (every such session)",
}

DEFAULT_HOME_ROOTS = ["/home", "/n/home[0-9]*", "/users"]
DEFAULT_SHARED = ["/n", "/scratch", "/netscratch", "/net", "/lustre", "/gpfs",
                  "/project", "/projects", "/work", "/data", "/hdd", "/madsys", "/mnt",
                  "/media", "/shared"]
TMP = ("/tmp", "/var/tmp", "/dev/shm")
SYSTEM = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
          "/opt", "/var", "/boot", "/root", "/run", "/snap", "/srv")
# The agent's or editor's own working state, in the user's home.
TOOL_STATE_HOME = frozenset({
    ".claude", ".codex", ".cursor", ".cursor-server", ".vscode", ".vscode-server",
    ".windsurf", ".windsurf-server", ".copilot", ".gemini", ".continue", ".aider"})
_TOOL_TMP = re.compile(r"^/tmp/(?:claude|codex|cursor|vscode)[-_]")

# Not directories anyone works in: device nodes and kernel views.
_PSEUDO = re.compile(r"^/(?:dev(?!/shm(?:/|$))|proc|sys)(?:/|$)")
# Executables named by path are programs, not work locations.
_PROGRAM = re.compile(r"^/(?:usr/(?:local/)?)?s?bin/.|^/usr/libexec/.")
# The agent harness's own plumbing, present on every tool call: Claude Code
# sources a shell snapshot first and writes `pwd -P` to a -cwd file last.
_HARNESS = re.compile(r"/\.claude/shell-snapshots/|^/tmp/claude-[0-9a-f]+-cwd$")

# ------------------------------------------------------------ argv parsing
_ANY_PATH = re.compile(r"[/~]|\.\.|\bcd\b|\bpushd\b")
_QUOTES = re.compile(r"""['"]""")
# Command separators. `>|` is a redirect, `2>&1` and `&>` are redirects, so the
# single `|` and `&` are only separators when they are not part of one.
_SEG = re.compile(r"&&|\|\||;|\n|\$\(|`|(?<![|>])\|(?!\|)|(?<![>&\d])&(?![>&\d])|[(){}]")
_REDIR = re.compile(r"(?:\d|&)?>>?\|?|<+")
_REDIR_OUT = re.compile(r"^(?:\d|&)?>>?\|?$")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GLOBCUT = re.compile(r"[*?\[$]")
_SHORT_ATTACHED = re.compile(r"^-[A-Za-z](/.*)$")
_USERNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
PREFIXES = frozenset({"sudo", "env", "command", "builtin", "exec", "nohup", "time",
                      "nice", "ionice", "timeout", "eval", "xargs", "stdbuf", "then",
                      "do", "else", "if", "while", "until", "!", "noglob"})
WRITE_ALL = frozenset({"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp",
                       "truncate", "shred", "unlink", "tee", "setfacl", "chattr", "mv"})
WRITE_LAST = frozenset({"cp", "ln", "install", "rsync", "scp"})
AS_DIR = frozenset({"mkdir", "rmdir"})
COMPILERS = frozenset({"gcc", "g++", "cc", "c++", "clang", "clang++", "nvcc",
                       "gfortran", "ld", "rustc", "go"})
# The first operand is a script or a pattern, not a path (`awk '/VmHWM/ {...}'`,
# `sed -i '/^SALT/d'`, `grep /api/v1`) -- unless -e / -f supplied it instead.
PATTERN_FIRST = frozenset({"awk", "gawk", "mawk", "nawk", "sed", "grep", "egrep",
                           "fgrep", "rg", "ag"})
# Flags whose value is a filter pattern (rsync --include=/input/, rg -g /Pipfile).
PATTERN_FLAGS = frozenset({"--include", "--exclude", "--filter", "--glob", "--iglob",
                           "--exclude-dir", "--include-dir", "-g"})
_JUNK = re.compile(r"[\^|]|^[^A-Za-z0-9]*$")
GIT_WRITES = frozenset({"clone", "init", "checkout", "switch", "reset", "clean",
                        "pull", "merge", "rebase", "commit", "apply", "am", "stash",
                        "restore", "rm", "mv", "add", "worktree", "submodule",
                        "cherry-pick", "revert"})


def _under(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _under_any(path, prefixes):
    return any(_under(path, p) for p in prefixes)


class _Ctx:
    """Per-command-line state: the directory the line has moved to, if known."""

    __slots__ = ("cur", "home", "refs", "tally")

    def __init__(self, base, home):
        self.cur = base
        self.home = home
        self.refs = []
        self.tally = collections.Counter()

    def resolve(self, token, cd=False):
        """Absolute path for a token, or None when it is not a local path."""
        t = token.strip(",;)]}").lstrip("([{")
        if not t or "://" in t or t.startswith("//"):
            return None, False
        globbed = False
        m = _GLOBCUT.search(t)
        if m:
            head = t[:m.start()]
            if "/" not in head:
                return None, False
            t = head.rsplit("/", 1)[0] or "/"
            globbed = True
        if t.startswith("/"):
            path = t
        elif t == "~" or t.startswith("~/"):
            if not self.home:
                return None, False
            path = self.home + t[1:]
        elif t.startswith("~"):
            name, _, rest = t[1:].partition("/")
            if not _USERNAME.match(name):
                return None, False
            root = posixpath.dirname(self.home) if self.home else "/home"
            path = "%s/%s%s" % (root, name, "/" + rest if rest else "")
        elif t in (".", "..") or t.startswith(("./", "../")) or (cd and t != "-"):
            if self.cur is None:
                self.tally["relative_unresolved"] += 1
                return None, False
            self.tally["relative_resolved"] += 1
            path = posixpath.join(self.cur, t)
        else:
            return None, False
        return posixpath.normpath(path)[:400], globbed

    def add(self, token, write=False, as_dir=False, file=False, cd=False):
        path, globbed = self.resolve(token, cd=cd)
        if path is None:
            return None
        if path != "/" and _JUNK.search(path):
            self.tally["junk"] += 1         # a regex or a separator, not a path
            return None
        if _PSEUDO.match(path):
            self.tally["pseudo"] += 1
            return None
        if _PROGRAM.match(path):
            self.tally["program"] += 1
            return None
        if _HARNESS.search(path):
            self.tally["harness"] += 1
            return None
        if not (as_dir or cd or globbed) and path != "/":
            base = posixpath.basename(path)
            if file or "." in base[1:]:
                path = posixpath.dirname(path) or "/"
        if path == "/" and not cd:
            # `tr / _`, `awk -F/`, a file at the root: a separator, not a place
            self.tally["bare_root"] += 1
            return None
        self.refs.append((path, bool(write), bool(cd)))
        return path


def _bundle_ends(tok, letter):
    """`-sSLo` takes `-o`'s argument next, as `-o` does."""
    return (tok.startswith("-") and not tok.startswith("--") and len(tok) >= 2
            and tok[1:].isalpha() and tok.endswith(letter))


def _short_c(tok):
    """`-c`, `-lc`, `-ic`: a shell flag bundle carrying a command string."""
    return (tok.startswith("-") and not tok.startswith("--") and tok[1:].isalpha()
            and "c" in tok)


def _segment(tokens, ctx):
    """One simple command: find the command word, then its path operands."""
    n = len(tokens)
    i = 0
    while i < n:
        t = tokens[i].lstrip("\\")
        if _ASSIGN.match(t):
            i += 1
            continue
        if t in PREFIXES:
            i += 1
            while i < n and (tokens[i].startswith("-") or tokens[i].isdigit()):
                i += 1
            continue
        if _REDIR_OUT.match(t) or t.startswith("<"):
            if i + 1 < n:                   # a leading redirect: `> f cmd`
                ctx.add(tokens[i + 1], write=not t.startswith("<"), file=True)
            i += 2
            continue
        cmd = posixpath.basename(t)
        if cmd in SHELLS:
            # `bash -lc <payload>`: the payload is the command that matters
            if t.startswith(("/", "~", "./", "../")):
                ctx.tally["program"] += 1
            j = i + 1
            while j < n and tokens[j].startswith("-") and not _short_c(tokens[j]):
                j += 1
            if j < n and _short_c(tokens[j]):
                i = j + 1
                continue
        break
    if i >= n:
        return
    cmdtok = tokens[i].lstrip("\\")
    cmd = posixpath.basename(cmdtok)
    if cmd not in SHELLS and cmdtok.startswith(("/", "~", "./", "../")):
        # The program, not a place it works: ~/.nvm, ~/.vscode-server and .venv
        # binaries would otherwise top the directory table on every launch.
        ctx.tally["program"] += 1

    if cmd in ("cd", "pushd"):
        rest = [x for x in tokens[i + 1:] if not _REDIR_OUT.match(x)]
        target = next((x for x in rest if not x.startswith("-")), None)
        if "-" in rest or (target is None and not ctx.home):
            ctx.cur = None
            return
        path = ctx.add(target if target is not None else "~", cd=True)
        ctx.cur = path                      # None when it could not be resolved
        return

    ops = []                                # raw operands, in order
    # `-i`, `-i.bak`, `-Ei`, `-pi`: but not `-Ilib`, whose `i` is a path
    sed_i = cmd in ("sed", "perl") and any(
        x.startswith("--in-place")
        or (x.startswith("-") and not x.startswith("--") and "i" in x[1:3])
        for x in tokens[i + 1:])
    tar_x = tar_c = False
    git_sub, git_dir = None, None
    pattern_given = cmd not in PATTERN_FIRST or (cmd == "rg" and "--files" in tokens)
    j = i + 1
    while j < n:
        t = tokens[j]
        nxt = tokens[j + 1] if j + 1 < n else None
        if _REDIR_OUT.match(t):
            if nxt is not None:
                ctx.add(nxt, write=True, file=True)
            j += 2
            continue
        if t.startswith("<"):
            if t == "<" and nxt is not None:
                ctx.add(nxt)
            j += 2                          # `<<EOF` / `<<<` consume a word too
            continue
        if t.startswith("-") and len(t) > 1:
            consumed = 1
            if cmd == "tar":
                tar_x = tar_x or t in ("--extract", "--get") or (
                    not t.startswith("--") and "x" in t)
                tar_c = tar_c or t == "--create" or (not t.startswith("--") and "c" in t)
            flag = t.split("=", 1)[0]
            if cmd in PATTERN_FIRST and (flag in ("-e", "--expression", "--regexp")
                                         or (t.startswith("-e") and len(t) > 2)):
                pattern_given = True        # inline script/pattern: never a path
                consumed = 1 if ("=" in t or len(t) > 2) else 2
            elif cmd in PATTERN_FIRST and flag in ("-f", "--file"):
                pattern_given = True        # a script file IS a path, read
                if "=" in t:
                    ctx.add(t.split("=", 1)[1], file=True)
                elif nxt is not None:
                    ctx.add(nxt, file=True)
                    consumed = 2
            elif flag in PATTERN_FLAGS:
                consumed = 1 if "=" in t else 2
            elif "=" in t and t.startswith("--"):
                ctx.add(t.split("=", 1)[1])
            elif _SHORT_ATTACHED.match(t):
                ctx.add(t[2:])
            elif nxt is not None:
                if cmd == "curl" and (t == "--output" or _bundle_ends(t, "o")):
                    ctx.add(nxt, write=True, file=True)
                    consumed = 2
                elif cmd == "wget" and t in ("-O", "--output-document"):
                    ctx.add(nxt, write=True, file=True)
                    consumed = 2
                elif cmd == "wget" and t in ("-P", "--directory-prefix"):
                    ctx.add(nxt, write=True, as_dir=True)
                    consumed = 2
                elif cmd in COMPILERS and t == "-o":
                    ctx.add(nxt, write=True, file=True)
                    consumed = 2
                elif cmd == "tar" and (t in ("-C", "--directory")):
                    ops.append(("tar_dir", nxt))
                    consumed = 2
                elif cmd == "tar" and (t in ("-f", "--file") or _bundle_ends(t, "f")):
                    ops.append(("tar_file", nxt))
                    consumed = 2
                elif cmd == "unzip" and t == "-d":
                    ctx.add(nxt, write=True, as_dir=True)
                    consumed = 2
                elif cmd in WRITE_LAST and t in ("-t", "--target-directory"):
                    ctx.add(nxt, write=True, as_dir=True)
                    consumed = 2
                elif cmd == "git" and git_sub is None and t == "-C":
                    git_dir = nxt
                    consumed = 2
                elif cmd == "git" and git_sub is None and t == "-c":
                    consumed = 2
            j += consumed
            continue
        if _ASSIGN.match(t) and not t.startswith(("/", ".", "~")):
            k, v = t.split("=", 1)
            ctx.add(v, write=(cmd == "dd" and k == "of"), file=(cmd == "dd"))
            j += 1
            continue
        if cmd == "git" and git_sub is None:
            git_sub = t
        else:
            ops.append(("op", t))
        j += 1

    if cmd == "git":
        w = git_sub in GIT_WRITES
        if git_dir is not None:
            ctx.add(git_dir, write=w, as_dir=True)
        plain = [t for kind, t in ops if kind == "op"]
        for k, t in enumerate(plain):
            dest = git_sub == "clone" and k == len(plain) - 1
            ctx.add(t, write=w, as_dir=dest)
        return
    if cmd == "tar":
        for kind, t in ops:
            if kind == "tar_dir":
                ctx.add(t, write=tar_x, as_dir=True)
            elif kind == "tar_file":
                ctx.add(t, write=tar_c and not tar_x, file=True)
            else:
                ctx.add(t)
        return
    plain = [t for kind, t in ops if kind == "op"]
    if not pattern_given:
        plain = plain[1:]
    for k, t in enumerate(plain):
        if cmd in WRITE_ALL or sed_i:
            ctx.add(t, write=True, as_dir=cmd in AS_DIR)
        elif cmd in WRITE_LAST:
            ctx.add(t, write=(k == len(plain) - 1))
        else:
            ctx.add(t)


def extract(args, base=None, home=None, truncated=False):
    """(refs, tally) for one command line.

    refs  -- [(directory, is_write, is_cd)], one per path mention
    tally -- what was seen and not kept: pseudo / program / harness / junk paths,
             and relative paths resolved or left unresolved for want of a base

    `truncated`: the collector cut this argv, so its last word is a fragment
    (`/proc/1/fd/0 ... /p`) and is dropped rather than read as a directory.
    """
    ctx = _Ctx(base, home)
    if args and truncated:
        args = args.rsplit(" ", 1)[0] if " " in args else ""
    if not args or not _ANY_PATH.search(args):
        return ctx.refs, ctx.tally
    flat = _QUOTES.sub("", args)
    for seg in _SEG.split(flat):
        if not seg.strip():
            continue
        tokens = _REDIR.sub(lambda m: " %s " % m.group(0), seg).split()
        if tokens:
            _segment(tokens, ctx)
    return ctx.refs, ctx.tally


# -------------------------------------------------------------- geometry
def radius(anchor, path):
    """(levels up from `anchor` to the directory containing `path`, escaped-to-root)."""
    a = [p for p in anchor.split("/") if p]
    b = [p for p in path.split("/") if p]
    c = 0
    while c < len(a) and c < len(b) and a[c] == b[c]:
        c += 1
    return len(a) - c, (c == 0 and len(a) > 0)


def rollup(path, depth):
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts[:depth])


def _reach(d):
    return (d["root"], d["radius"] or 0)


def _bucket(r, root):
    if root:
        return "root"
    return "3+" if r >= 3 else str(r)


class Zones:
    """Which kind of place a directory is, relative to one session."""

    def __init__(self, cfg):
        r = cfg.get("reducers.sporadicity", {}) or {}
        roots = r.get("home_roots") or DEFAULT_HOME_ROOTS
        self.home_re = re.compile(r"^(?:%s)/([^/]+)" % "|".join(roots))
        self.home_default = r.get("home_default") or "/home/{user}"
        self.shared = tuple(r.get("shared_prefixes") or DEFAULT_SHARED)

    def home_of(self, user, cwd):
        if not user:
            return None
        if cwd:
            m = self.home_re.match(cwd)
            if m and m.group(1) == user:
                return cwd[:m.end()]
        return self.home_default.format(user=user)

    def of(self, path, anchor, home, user):
        if anchor and anchor != "/" and _under(path, anchor):
            return "cwd"
        m = self.home_re.match(path)
        own = None
        if home and _under(path, home):
            own = path[len(home):]
        elif m and user and m.group(1) == user:
            own = path[m.end():]
        if own is not None:
            first = own.lstrip("/").split("/", 1)[0]
            if first in TOOL_STATE_HOME:
                return "tool-state"
            return "home-dot" if first.startswith(".") else "home"
        if _TOOL_TMP.match(path):
            return "tool-state"
        if _under_any(path, TMP):
            return "tmp"
        if m:
            # With no username there is no telling whose home this is, and
            # "another user's" is the alarming answer: say nothing instead.
            return "other-home" if user else "other"
        if _under_any(path, self.shared):
            return "shared"
        if _under_any(path, SYSTEM):
            return "system"
        return "other"


def _clean_cwd(cwd):
    if not cwd:
        return None
    if cwd.endswith(" (deleted)"):
        cwd = cwd[:-len(" (deleted)")]
    return cwd or None


def _cwd_read(rec):
    """True when this record's cwd was read from /proc, not inherited.

    Schemas up to 6 say so (`cwd_source`); schema 7 dropped the field, and the
    collector's own predicate is the next best answer: it reads the cwd for a tty
    process and for an agent root.
    """
    if "cwd_source" in rec:
        return rec.get("cwd_source") == "proc"
    return bool(rec.get("tty")) or bool(rec.get("is_agent"))


class _Session:
    __slots__ = ("sid", "kt", "host", "user", "uname", "cls", "agent_type", "agent_pid", "n",
                 "first", "last", "cwds", "root_cwd", "refs", "dropped", "home",
                 "sandbox", "approval")

    def __init__(self, rec):
        self.sid = rec["_sid"]
        self.kt = rec.get("_kt")
        self.host = rec.get("host")
        self.user = None                # display: a name, else uid:N
        self.uname = None               # the passwd name, for home ownership
        self.cls = rec["_a3"]
        self.agent_type = rec.get("agent_type")
        self.agent_pid = rec.get("agent_pid")
        self.n = 0
        self.first = None
        self.last = None
        self.cwds = collections.Counter()
        self.root_cwd = None
        self.refs = {}                  # dir -> [mentions, writes, cds]
        self.dropped = 0
        self.home = None
        self.sandbox = "unknown"
        self.approval = "unknown"


class SporadicityReducer:
    EVENTS = ("exit", "truncated")
    KEY = "sporadicity"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        r = cfg.get("reducers.sporadicity", {}) or {}
        self.top_n = r.get("top_n", 15)
        self.dirs_top_n = r.get("dirs_top_n", 40)
        self.depth = r.get("rollup_depth", 4)
        self.max_sessions = r.get("max_sessions", 50000)
        self.max_dirs = r.get("max_dirs_per_session", 128)
        self.max_cwds = 32
        self.child_max = r.get("dedup_index_max", 20000)
        self.rankable = set(r.get("include_key_types", ["apid", "sess", "tty"]))
        self.zones = Zones(cfg)
        self.sessions = {}
        self.untracked_records = 0
        self.refs_dropped = 0
        self.child_refs = {}            # (sid, ppid) -> Counter of refs
        self.child_evicted = 0
        self.dedup_dropped = 0
        self.records = collections.Counter()
        self.with_args = collections.Counter()
        self.args_truncated = collections.Counter()
        self.cwd_null = collections.Counter()
        self.cwd_read = collections.Counter()
        self.tally = collections.defaultdict(collections.Counter)

    # ---------------------------------------------------------------- feed
    def feed(self, rec):
        cls = rec["_a3"]
        self.records[cls] += 1
        args = rec.get("args") or ""
        if args:
            self.with_args[cls] += 1
            if rec.get("args_truncated"):
                self.args_truncated[cls] += 1
        cwd = _clean_cwd(rec.get("cwd"))
        if cwd is None:
            self.cwd_null[cls] += 1
        read = _cwd_read(rec)
        if read:
            self.cwd_read[cls] += 1
        sid = rec.get("_sid")
        if not sid:
            return
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= self.max_sessions:
                self.untracked_records += 1
                return
            s = self.sessions[sid] = _Session(rec)
        user = rec.get("user")
        home = self.zones.home_of(user, cwd)
        self._observe(s, rec, cwd, home)

        refs, tally = extract(args, base=cwd if read else None, home=home,
                              truncated=bool(rec.get("args_truncated")))
        tally["kept"] = len(refs)
        self.tally[cls].update(tally)

        # A shell's mention of what its own captured child already mentioned is
        # one operation, not two. The child exits first (the shell waited on it),
        # so its refs are waiting here when the shell's record arrives.
        pid, ppid = rec.get("pid"), rec.get("ppid")
        seen = self.child_refs.pop((sid, pid), None)
        if refs and ppid is not None:
            key = (sid, ppid)
            c = self.child_refs.get(key)
            if c is None:
                if len(self.child_refs) >= self.child_max:
                    self.child_refs.pop(next(iter(self.child_refs)), None)
                    self.child_evicted += 1
                c = self.child_refs[key] = collections.Counter()
            c.update(refs)
        for ref in refs:
            if seen and seen[ref] > 0:
                seen[ref] -= 1
                self.dedup_dropped += 1
                continue
            self._add(s, *ref)

    def _observe(self, s, rec, cwd, home):
        s.n += 1
        ts = rec.get("_ts_epoch")
        if ts is not None:
            s.first = ts if s.first is None else min(s.first, ts)
            s.last = ts if s.last is None else max(s.last, ts)
        if cwd:
            if cwd in s.cwds or len(s.cwds) < self.max_cwds:
                s.cwds[cwd] += 1
            else:
                s.dropped += 1
                self.refs_dropped += 1
            if rec.get("is_agent"):
                s.root_cwd = cwd
        if s.home is None and home:
            s.home = home
        ident = rec.get("user") or (
            "uid:%s" % rec["uid"] if rec.get("uid") is not None else None)
        if s.user is None and ident:
            s.user = ident
        if s.uname is None and rec.get("user"):
            s.uname = rec["user"]
        if s.host is None and rec.get("host"):
            s.host = rec["host"]
        if s.agent_type is None and rec.get("agent_type"):
            s.agent_type = rec["agent_type"]
        if s.sandbox == "unknown" and rec.get("_sandbox") not in (None, "unknown"):
            s.sandbox = rec["_sandbox"]
        if s.approval == "unknown" and rec.get("_approval") not in (None, "unknown"):
            s.approval = rec["_approval"]

    def _add(self, s, d, write, cd):
        e = s.refs.get(d)
        if e is None:
            if len(s.refs) >= self.max_dirs:
                s.dropped += 1
                self.refs_dropped += 1
                return
            e = s.refs[d] = [0, 0, 0]
        e[0] += 1
        if write:
            e[1] += 1
        if cd:
            e[2] += 1

    # -------------------------------------------------------------- result
    @staticmethod
    def _anchor(s):
        if s.root_cwd and s.root_cwd != "/":
            return s.root_cwd, "root"
        for cwd, _n in s.cwds.most_common():
            if cwd != "/":
                return cwd, "mode"
        return None, None

    def _measure(self, s):
        """Classify one session's references against its own launch dir."""
        anchor, src = self._anchor(s)
        dirs = {d: list(v) for d, v in s.refs.items()}
        # A cwd other than the anchor is a place the session worked in.
        for cwd, n in s.cwds.items():
            if cwd != anchor and cwd != "/":
                e = dirs.setdefault(cwd, [0, 0, 0])
                e[0] += n
        out = []
        for d, (refs, writes, cds) in dirs.items():
            zone = self.zones.of(d, anchor, s.home, s.uname)
            r, root = radius(anchor, d) if anchor else (None, False)
            out.append({"dir": d, "zone": zone, "refs": refs, "writes": writes,
                        "cd": cds > 0, "radius": r, "root": root})
        return anchor, src, out

    def result(self):
        by_class = {}
        rows, dir_agg = [], {}

        def cls_block(cls):
            b = by_class.get(cls)
            if b is None:
                b = by_class[cls] = {
                    "sessions": 0, "sessions_outside": 0, "sessions_write_outside": 0,
                    "sessions_other_home": 0, "no_anchor": 0,
                    "refs": collections.Counter(), "writes": collections.Counter(),
                    "sessions_by_zone": collections.Counter(),
                    "write_sessions_by_zone": collections.Counter(),
                    "radius": collections.Counter(), "write_radius": collections.Counter(),
                }
            return b

        for s in self.sessions.values():
            anchor, src, dirs = self._measure(s)
            b = cls_block(s.cls)
            for d in dirs:
                b["refs"][d["zone"]] += d["refs"]
                if d["writes"]:
                    b["writes"][d["zone"]] += d["writes"]
            if s.kt not in self.rankable:
                continue
            if anchor is None:
                b["no_anchor"] += 1         # launched at /: nothing to measure from
                continue
            # The agent's own state is its own area: in the zone mix, never reach.
            outside = [d for d in dirs if d["zone"] not in CONTAINED]
            wrote = [d for d in dirs if d["writes"] and d["zone"] != "tool-state"]
            out_w = [d for d in outside if d["writes"]]
            b["sessions"] += 1
            b["sessions_outside"] += bool(outside)
            b["sessions_write_outside"] += bool(out_w)
            b["sessions_other_home"] += any(d["zone"] == "other-home" for d in dirs)
            for z in {d["zone"] for d in dirs}:
                b["sessions_by_zone"][z] += 1
            for z in {d["zone"] for d in wrote}:
                b["write_sessions_by_zone"][z] += 1

            far = max(outside, key=_reach, default=None)
            far_w = max(wrote, key=_reach, default=None)
            far_ow = max(out_w, key=_reach, default=None)
            b["radius"][_bucket(far["radius"], far["root"]) if far else "0"] += 1
            if far_w:
                b["write_radius"][_bucket(far_w["radius"], far_w["root"])] += 1
            if not outside:
                continue

            zones = {}
            for d in outside:
                z = zones.setdefault(d["zone"], {"dirs": 0, "refs": 0, "writes": 0})
                z["dirs"] += 1
                z["refs"] += d["refs"]
                z["writes"] += d["writes"]
                key = rollup(d["dir"], self.depth)
                a = dir_agg.get(key)
                if a is None:
                    a = dir_agg[key] = {"dir": key, "zone": d["zone"], "sessions": set(),
                                        "users": set(), "classes": collections.Counter(),
                                        "refs": 0, "writes": 0, "cds": 0}
                if s.sid not in a["sessions"]:
                    a["sessions"].add(s.sid)
                    a["classes"][s.cls] += 1
                if s.user:
                    a["users"].add(s.user)
                a["refs"] += d["refs"]
                a["writes"] += d["writes"]
                a["cds"] += d["cd"]
            top = sorted(outside, key=lambda d: (-d["writes"], -d["cd"], -d["refs"], d["dir"]))
            rows.append({
                "sid": s.sid, "key_type": s.kt, "host": s.host, "user": s.user,
                "actor3": s.cls, "agent_type": s.agent_type,
                "agent_pid": s.agent_pid, "anchor": anchor, "anchor_src": src,
                "home": s.home, "n_records": s.n,
                "wall_s": round(s.last - s.first, 1) if s.first and s.last else None,
                "radius": far["radius"],
                "root_escape": far["root"],
                "write_radius": far_ow["radius"] if far_ow else None,
                "outside_dirs": len(outside),
                "outside_refs": sum(d["refs"] for d in outside),
                "outside_writes": sum(d["writes"] for d in outside),
                "other_home_dirs": sum(d["zone"] == "other-home" for d in outside),
                "zones": zones,
                "top_outside": [{k: d[k] for k in ("dir", "zone", "refs", "writes", "cd",
                                                    "radius")} for d in top[:6]],
                "more_outside": max(0, len(top) - 6),
                "dirs_dropped": s.dropped,
                "sandbox": s.sandbox, "approval": s.approval,
            })

        keyf = {"radius": lambda r: (r["root_escape"], r["radius"] or 0,
                                     r["outside_writes"], r["outside_dirs"]),
                "writes": lambda r: (r["outside_writes"], r["write_radius"] or 0,
                                     r["outside_dirs"]),
                "dirs": lambda r: (r["outside_dirs"], r["outside_refs"]),
                "other_home": lambda r: (r["other_home_dirs"], r["outside_writes"])}
        valf = {"radius": lambda r: r["radius"] or 0,
                "writes": lambda r: r["outside_writes"],
                "dirs": lambda r: r["outside_dirs"],
                "other_home": lambda r: r["other_home_dirs"]}
        ranks, shown, dom, totals = {}, {}, {}, {}
        for rk in RANKS:
            top = sorted(rows, key=keyf[rk], reverse=True)
            if rk in ("writes", "other_home"):
                top = [r for r in top if valf[rk](r)]
            totals[rk] = len(top)
            # Another user's home is the case the plate exists for: list every
            # such session (to a cap), not the top N, or the plate counts what
            # no row shows.
            top = top[:OTHER_HOME_CAP if rk == "other_home" else self.top_n]
            ranks[rk] = [r["sid"] for r in top]
            for r in top:
                shown[r["sid"]] = r
            byu = collections.Counter()
            for r in top:
                byu[r["user"]] += valf[rk](r)
            tot = sum(byu.values())
            dom[rk] = pct(byu.most_common(1)[0][1], tot) if tot else None

        # Another user's home first, for the same reason as its ranking.
        dirs = sorted(dir_agg.values(),
                      key=lambda a: (a["zone"] != "other-home", -len(a["sessions"]),
                                     -a["writes"], -a["refs"], a["dir"]))
        dir_rows = [{"dir": a["dir"], "zone": a["zone"], "sessions": len(a["sessions"]),
                     "users": len(a["users"]), "classes": dict(a["classes"]),
                     "refs": a["refs"], "writes": a["writes"], "cds": a["cds"]}
                    for a in dirs[:self.dirs_top_n]]

        def plain(b):
            return {k: (dict(v) if isinstance(v, collections.Counter) else v)
                    for k, v in b.items()}

        return {
            "zones": list(ZONES), "zone_meaning": ZONE_MEANING,
            "radius_buckets": list(RADIUS_BUCKETS), "contained": list(CONTAINED),
            "by_class": {c: plain(b) for c, b in by_class.items()},
            "ranks": ranks, "rows": shown, "rank_meaning": RANK_MEANING,
            "dominant_user_share_pct": dom, "rank_totals": totals,
            "sessions_outside_total": len(rows),
            "dirs": dir_rows, "dirs_total": len(dir_agg), "rollup_depth": self.depth,
            "coverage": {
                "records": dict(self.records), "with_args": dict(self.with_args),
                "args_truncated": dict(self.args_truncated),
                "cwd_null": dict(self.cwd_null), "cwd_read": dict(self.cwd_read),
                "tokens": {c: dict(t) for c, t in self.tally.items()},
                "dedup_dropped": self.dedup_dropped,
            },
            "buffer": {"sessions": len(self.sessions),
                       "untracked_records": self.untracked_records,
                       "refs_dropped": self.refs_dropped,
                       "dedup_index_evicted": self.child_evicted},
            "notes": {
                "evidence": "Paths come from argv -- absolute, ~, and ./ ../ where the "
                            "base directory is known -- plus each process's recorded cwd. "
                            "A file a program opens on its own (imports, configs, the "
                            "output a script writes) leaves no argv trace, so every "
                            "number here is a floor on reach.",
                "cwd": "The collector inherits cwd from the parent and reads "
                       "/proc/<pid>/cwd only for agent roots, tty processes and "
                       "submits, so an agent's tool calls carry the LAUNCH directory "
                       "even after it moved. That is why `cd` targets in shell payloads "
                       "count as references, and why a relative path resolves only "
                       "against a cwd that was read (cwd_source=proc; on schema 7, a "
                       "tty process or an agent root) or a directory the same command "
                       "line cd'd into.",
                "anchor": "Launch directory: the agent root's own cwd when its record "
                          "is in the window (anchor_src=root), else the session's most "
                          "common recorded cwd other than / (anchor_src=mode).",
                "radius": "Levels up from the launch directory to the nearest directory "
                          "that contains the reference. `root` means / is the only "
                          "common ancestor -- the session left its launch tree entirely.",
                "writes": "A write is a path handed to a mutating command (rm, mv, "
                          "mkdir, touch, chmod/chown, tee, sed -i, the destination of "
                          "cp/rsync/scp/ln/install, dd of=, tar -C on extract and -f on "
                          "create, git -C or operands of a mutating git subcommand, "
                          "curl/wget/compiler -o) or a shell redirect target. It is a "
                          "mention, not an outcome: a refused rm still counts.",
                "dedup": "A shell's mention of a path its own captured child already "
                         "mentioned is dropped (same session, child ppid == shell pid) "
                         "-- the tool-mix shell-wrapper rule, applied to paths.",
                "dropped": "pseudo = /dev, /proc, /sys; program = the command word "
                           "itself and executables under /bin, /sbin, /usr/bin, "
                           "/usr/local/bin, /usr/libexec; harness = Claude Code's shell "
                           "snapshot and its -cwd file, present on every tool call; junk "
                           "= regex-shaped tokens (awk/sed addresses that survived); "
                           "bare_root = a lone / (tr / _, awk -F/). The awk/sed/grep "
                           "script or pattern operand and rsync/rg filter globs are "
                           "skipped outright. A cut argv loses its last word.",
                "tool-state": "An agent's or editor's own state (~/.claude, ~/.codex, "
                              "~/.vscode-server, its /tmp/claude-* scratchpad) is shown in "
                              "the zone mix but never counted as reach: writing its own "
                              "transcript or scratch file is not the agent leaving its "
                              "project.",
                "sessions": "Session counts cover apid/sess/tty sessions with a launch "
                            "directory. usr-keyed records count at the reference grain "
                            "only, as in the trajectory ranking; a tree launched at / "
                            "(on a real day, ~1,100 one-process codex/vscode/claude "
                            "remote probes) is counted in no_anchor, because there is "
                            "no directory to measure its reach from.",
            },
        }
