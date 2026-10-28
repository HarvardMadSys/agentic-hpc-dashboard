"""Workspace and blast radius: where each agent ran, and what its commands named.

The collector records no file paths. What it does record, per process, is enough
for three readings of "where an agent works", and they are kept apart because
they are not the same kind of evidence:

  ran in   MEASURED. The process's working directory (`cwd`). The collector
           reads it at exec for agent-tagged processes and processes with a
           terminal and inherits it otherwise, so `cd sub && make` inside one
           shell command is credited to the shell's directory, not to `sub`.
  bytes    MEASURED, per process and not per file: the syscall bytes
           (`io.rchar_mb` / `wchar_mb`, pipes and terminals included) and disk
           bytes (`rd_mb` / `wr_mb`) of the processes that ran in a directory,
           credited to where they RAN. A process in `./` that writes to
           `/scratch` puts its bytes on `./`.
  named    INFERRED from command lines: paths a command names, classified by
           what the command does with them -- `cat` reads, `>` and `cp`'s
           destination write, `rm` deletes, anything else path-like is just
           named. Naming a path is not proof of touching it: a command can
           fail, a path can be a pattern.

Exact file-level reads and writes would need a collector probe (openat /
unlink / rename tracepoints) that is not built. Claude Code's Read, Edit and
Write tools and Codex's apply_patch run inside the agent's own process and
leave no command line at all, so their file access is not seen here.

An agent's home is its own cwd -- the directory it was started in -- except
`/`: an IDE-hosted agent runs there, and under `/` everything is "inside", so
the first call's directory off the root is used and labelled as such (the rule
the retired drift anchor used). A write or delete named outside the home is the
risk reading this exists for: an agent changing things beyond the project it
was started in.

Cost. Every exit record of a window reaches `feed` (~1.8 M a day on rtx6000a,
most of them one runaway `multiprocessing` loop), so the per-record path is a
few dict operations: only agent-class records with a session key are kept,
each distinct argv is parsed once and cached -- 460 k of the 480 k exits in
rtx6000a's busiest hour of 2026-10-10 shared one -- and an argv a fork merely
inherited is not read again. The parser is a single-pass regex tokenizer,
never `shlex`. Measured inside a real 24 h build of 2026-10-10 (1.89 M
records, ~90 s): 2.4 s in `feed`, about 1.4 us a record.

Memory. One KeyStat per collector session key -- one per Claude tool call --
with at most `max_dirs` directories each and `max_dir_entries` across all of
them; past either, entries are counted, not kept. A day held 7 900 keys and
21 000 entries (2.4 MB pickled), after version probes and helpers at the top
of their own tree -- which no agent is above -- were dropped on arrival.

Folding. Records are kept per collector session key (`agent_pid`), and each key
is folded into its agent at result time through the window's ProcIndex,
exactly as trajectory rows are: the chain from a tool call's shell up to its
agent is only known once the shell's own record arrives, after its children's.
"""
import posixpath
import re
import sys
import time

from ..proctree import ran
from ..redact import redact

R, W, D, N = 1, 2, 4, 8
OPS = (("read", R), ("write", W), ("delete", D), ("named", N))
_EV = ("exit", "truncated")

# Roles whose argv is the agent launching itself: a version probe, a daemon, a
# client, an IDE server. Their paths are the agent's own install, not its work.
_SKIP_ROLES = frozenset({"infra", "client", "probe", "ide"})
_ADD_DIR = re.compile(r"(?:^|\s)--add-dir[ =](\S+)")

MAX_ITEMS = 32              # path tokens kept per command line; past it, counted
MAX_NAMES = 64              # distinct paths kept per directory per key / agent
CACHE_MAX = 20000           # distinct argv parses held; cleared, not evicted, when full
NAME_CUT = 120              # characters of a path kept under its directory


# ---------------------------------------------------------------- tokenizer
# One shell word: quoted runs, escapes and plain characters glued together.
_WORD = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s;&|()<>'"`\\])+"""
_SH = re.compile(
    r"(?P<sep>&&|\|\||;;|[;&|\n(){}]|\$\(|`|<\()"
    r"|(?P<redir>\d*(?:&>>?|>>?\|?|<(?![<(])))[ \t]*(?P<rt>" + _WORD + ")?"
    r"|(?P<word>" + _WORD + ")"
    r"|(?P<junk>['\"\\])")
_UNQ = re.compile(r"""'([^']*)'|"((?:[^"\\]|\\.)*)"|\\(.)""")
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\1")
_HERESTR = re.compile(r"""<<<[ \t]*(?:'[^']*'|"(?:[^"\\]|\\.)*"|\S+)""")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# host:path, user@host:path, a git object `HEAD:file` -- not local paths
_REMOTE = re.compile(r"^[\w.+-]*(?:@[\w.-]+)?:")
_NOT_PATH = re.compile(
    r"^(?:(?=.*\d)[\d.,:/+x-]+|\{\}|[+\\;]+"
    r"|(?:application|text|image|audio|video|multipart|font|model)/[\w.+-]+)$")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,6}$")
# The characters a path on a command line is made of, with at least one letter
# or digit: `==`, `{`, `(key` and `"asset"` are a program's pieces, not files.
_FILEISH = re.compile(r"^[\w.~@%+,:=*?\[\]/-]*[A-Za-z0-9_][\w.~@%+,:=*?\[\]/-]*$")
# Interpreters run scripts from here the way a shell runs a binary: the
# program, not the work (`/bin/sh /usr/bin/lsb_release -a`, every Codex call).
_SYSTEM_EXEC = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/snap/", "/opt/")
_GLOB = re.compile(r"[*?\[]")
_SHORT_C = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")      # -c, -lc, -ec: a shell's script flag

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh"})
_INTERP = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|Rscript|node|nodejs|bun|deno|ruby|"
                     r"julia|php|lua[\d.]*|tclsh|perl)$")
_KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
                       "!", "{", "}", "[[", "]]", "esac", "in", "function", "coproc",
                       "select"})
_NO_PATHS = frozenset({
    "echo", "printf", "print", "export", "unset", "alias", "unalias", "local", "declare",
    "typeset", "readonly", "read", "true", "false", "exit", "return", "sleep", "kill",
    "pkill", "pgrep", "killall", "ps", "date", "whoami", "hostname", "id", "which",
    "type", "hash", "set", "shopt", "setopt", "unsetopt", "ulimit", "umask", "wait", "trap",
    "eval", "ssh", "squeue", "sacct", "scancel", "sinfo", "scontrol", "sprio", "sstat",
    "nvidia-smi", "module", "ml", "seq", "expr", "bc", "basename", "dirname", "nproc",
    "uname", "history", "jobs", "fg", "bg", "disown", "getconf", "getopt", "lsb_release",
    "tr", "yes", "clear", "tput", "stty", "sync", "free", "uptime", "top", "htop", "w",
    "who", "groups", "getent", "locale", "printenv", "case", "shift", "break", "continue",
    "let",
})

# Read-only commands, each with the flags that take a value (so the value is not
# read as a file).
_READ = {
    "cat": "", "tac": "", "nl": "-b -s -w -v -i", "less": "", "more": "",
    "head": "-n -c", "tail": "-n -c -s --pid --sleep-interval", "wc": "",
    "diff": "-U -C -x -X -I -L --label --exclude --unified --context",
    "cmp": "-i -n", "stat": "-c --format --printf", "file": "-m -f -F",
    "md5sum": "", "sha1sum": "", "sha256sum": "", "sha512sum": "", "cksum": "",
    "b2sum": "", "od": "-A -t -N -j -w", "xxd": "-l -s -c -g", "hexdump": "-n -s -e",
    "strings": "-n -t", "readlink": "", "realpath": "--relative-to --relative-base",
    "source": "", ".": "", "column": "-s -t -c", "zcat": "", "bzcat": "", "xzcat": "",
    "zless": "", "bat": "-l -r --line-range --language", "cut": "-d -f -c -b",
    "paste": "-d", "join": "-t -1 -2 -j -o", "comm": "", "uniq": "-f -s -w",
    "shuf": "-n -i", "fold": "-w", "fmt": "-w", "iconv": "-f -t", "base64": "-w",
    "h5dump": "-d", "h5ls": "", "ncdump": "-v", "nm": "", "objdump": "", "ldd": "",
    "pdftotext": "", "identify": "", "ffprobe": "", "lsof": "", "sqlite3": "",
    "tokei": "", "cloc": "",
}
# Read-only commands whose operands are usually directories.
_DIRREAD = {
    "ls": "-I -w -T --ignore --hide --format --sort --time-style --color --block-size",
    "du": "-d -B -t --max-depth --block-size --threshold --exclude", "tree": "-L -I -P -o",
    "exa": "-L -I", "eza": "-L -I", "df": "-t -x -B --output", "dir": "",
}
# A pattern (or a program) first, files after.
_GREP_V = "-e -f -m -A -B -C -d -D --include --exclude --exclude-dir --color --label --regexp --file --max-count --context --after-context --before-context"
_RG_V = _GREP_V + " -g -t -T -r -j -M -E --glob --iglob --type --type-not --replace --threads --sort --sortr --colors --encoding --max-columns --max-depth --type-add --pre --pre-glob --ignore-file --path-separator --max-filesize"
_PATTERN_FIRST = {
    "grep": _GREP_V, "egrep": _GREP_V, "fgrep": _GREP_V, "zgrep": _GREP_V,
    "rg": _RG_V, "ag": _RG_V + " -G --file-search-regex", "ack": _RG_V,
    "ugrep": _RG_V + " -O -K -N -Q", "ug": _RG_V + " -O -K -N -Q",
    "awk": "-F -v -f", "gawk": "-F -v -f", "mawk": "-F -v -f",
    "jq": "-f --from-file --indent --tab -L", "yq": "-f --from-file -o -I",
    "fd": "-e -E -t -d -x -X -j -S -o -c --extension --exclude --type --max-depth "
          "--exec --exec-batch --threads --size --owner --color --max-results "
          "--changed-within --changed-before --base-directory --search-path",
}
_PERL_INPLACE = re.compile(r"^-[pnlaw0]*i")
_PERL_E = re.compile(r"^-[a-zA-Z0-9]*[eE]$")   # -e, -ne, -lane: the program is next
_WRITE = {
    "touch": "-d -t -r --date --reference", "tee": "", "truncate": "-s -r --size --reference",
    "mkfifo": "-m", "chmod": "--reference", "chown": "--reference", "chgrp": "--reference",
    "setfacl": "-m -x -M -X", "chattr": "",
}
_MODE_FIRST = frozenset({"chmod", "chown", "chgrp", "setfacl", "chattr"})
_DELETE = {"rm": "", "unlink": "", "shred": "-n -s --iterations --size", "trash": "",
           "trash-put": ""}
_INPLACE = frozenset({"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd",
                      "unzstd", "lz4", "pigz"})
# to standard output, listing or testing: the file is only read
_INPLACE_RO = frozenset({"-c", "--stdout", "--to-stdout", "-l", "--list", "-t", "--test"})
_COPY = {   # command -> (operand op, destination op, value flags)
    "cp": (R, W, "-t -S --target-directory --suffix"),
    "mv": (W, W, "-t -S --target-directory --suffix"),
    "ln": (N, W, "-t -S --target-directory --suffix"),
    "install": (R, W, "-t -m -o -g -S --target-directory --mode --owner --group"),
    "rsync": (R, W, "-e -f -T -B -M --rsh --filter --exclude --include --files-from "
                    "--exclude-from --include-from --chmod --chown --password-file "
                    "--log-file --backup-dir --partial-dir --temp-dir --compare-dest "
                    "--link-dest --bwlimit --timeout --port --out-format"),
    "scp": (R, W, "-P -i -o -F -c -l -J -S"),
}
_WRAPPERS = {   # wrapper -> (value flags, positional operands before the command)
    "sudo": ("-u -g -C -D -h -p -U -r -t", 0), "env": ("-u -C -S --chdir --unset", 0),
    "nohup": ("", 0), "time": ("-f -o --format --output", 0), "nice": ("-n --adjustment", 0),
    "ionice": ("-c -n -p --class --classdata", 0), "timeout": ("-s -k --signal --kill-after", 1),
    "stdbuf": ("-i -o -e", 0), "chrt": ("", 1), "taskset": ("-c", 1), "setsid": ("", 0),
    "unbuffer": ("", 0), "exec": ("-a", 0), "command": ("", 0), "builtin": ("", 0),
    "xargs": ("-I -i -n -P -L -d -E -s -a --arg-file --delimiter --max-args --max-procs "
              "--replace", 0),
    "watch": ("-n -d --interval", 0), "flock": ("-w -E --timeout --conflict-exit-code", 1),
    "srun": ("-o -e -D -J -p -t -N -n -c -A -q -w -x --mem --gres --output --error "
             "--chdir --job-name --partition --time --nodes --ntasks --cpus-per-task "
             "--account --qos --nodelist --exclude --mem-per-cpu --gpus", 0),
    "mpirun": ("-n -np -N -H -x --host --map-by --bind-to --hostfile", 0),
    "mpiexec": ("-n -np -N -H -x --host --map-by --bind-to --hostfile", 0),
    "numactl": ("-C -N -m -p -i --cpunodebind --membind --physcpubind --interleave", 0),
    "strace": ("-o -e -p -s -P --output", 0), "ltrace": ("-o -e -p -s", 0),
    "valgrind": ("", 0), "uvx": ("--with --python -p --from", 0), "npx": ("-p --package", 0),
    "bunx": ("", 0), "conda": ("-n -p --name --prefix --cwd", 0), "poetry": ("", 0),
    "pipenv": ("", 0), "pixi": ("-e --environment --manifest-path", 0),
    "apptainer": ("-B -H -W --bind --home --pwd --overlay --workdir --env", 1),
    "singularity": ("-B -H -W --bind --home --pwd --overlay --workdir --env", 1),
    "uv": ("--with --python -p --project --directory --env-file --extra --group "
           "--package --index --index-url --python-preference", 0),
}
# flags whose value a wrapper reads or writes, and how
_WRAP_PATHS = {"-o": W, "-e": W, "--output": W, "--error": W, "-D": N, "--chdir": N,
               "-C": N, "--directory": N, "--project": N, "--cwd": N, "-B": N, "--bind": N,
               "-a": R, "--arg-file": R, "--env-file": R, "-p": N, "--prefix": N}
_RUN_SUB = {"conda": "run", "poetry": "run", "pipenv": "run", "pixi": "run", "uv": "run",
            "apptainer": ("exec", "run"), "singularity": ("exec", "run")}


def _vset(spec):
    return frozenset(spec.split())


for _t in (_READ, _DIRREAD, _PATTERN_FIRST, _WRITE, _DELETE):
    for _k in _t:
        _t[_k] = _vset(_t[_k])
_COPY = {k: (a, b, _vset(c)) for k, (a, b, c) in _COPY.items()}
_WRAPPERS = {k: (_vset(a), b) for k, (a, b) in _WRAPPERS.items()}


def _unq(w):
    if "'" in w or '"' in w or "\\" in w:
        return _UNQ.sub(lambda m: m.group(1) if m.group(1) is not None else
                        (m.group(2) if m.group(2) is not None else m.group(3)), w)
    return w


def _strip_heredocs(s):
    """A heredoc's body is data (usually code), not commands: cut it out."""
    s = _HERESTR.sub(" ", s)
    if "<<" not in s:
        return s
    out, pending = [], []
    for line in s.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        delims = [m.group(2) for m in _HEREDOC.finditer(line)]
        if delims:
            line = _HEREDOC.sub(" ", line)
            pending = delims
        out.append(line)
    return "\n".join(out)


def _ok(t, bare):
    """Whether a token can be a local path.

    `bare` True: the command takes paths as operands, so `README.md` or `build`
    counts. "file": the operand may instead be a fragment of a program or a
    pattern split on its spaces (`awk '{print $1}' f` arrives as `awk {print $1}
    f`), so it must look like a file -- a `/`, an extension. False: anything
    must look like a path to count."""
    if not t or t[0] == "-" or len(t) > 400:
        return False
    if t in (".", "..", "/"):
        # a whole directory -- unless operands cannot be trusted, where a lone
        # `/` is far likelier a piece of `s/a /b /` split on its spaces
        return bare != "file"
    rest = t
    if t[0] == "$":
        if not t.startswith(("$HOME/", "${HOME}/")) and t not in ("$HOME", "${HOME}"):
            return False
        rest = t[t.index("}") + 1:] if t[1] == "{" else t[5:]
        if not rest:
            return True
    if _FILEISH.match(rest) is None:
        return False                # quotes, parens, operators: code, not a path
    if t.startswith(("./", "../")):
        return True
    if bare is not True and "/" not in t and t[0] != "~" and (
            bare is False or _EXT.search(t) is None):
        return False
    if t[0] != "/" and t[0] != "~" and _REMOTE.match(t):
        return False
    if t.startswith("//"):
        return False                # `s/^x //g` split on its space, not a path
    return _NOT_PATH.match(t) is None


def _scan(args, vflags):
    """(operands, {flag: value}) -- flags dropped, the values of `vflags` kept."""
    pos, fv, i, n = [], {}, 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if len(a) > 1 and a[0] == "-":
            if a.startswith("--") and "=" in a:
                k, v = a.split("=", 1)
                fv[k] = v
            elif a in vflags:
                if i + 1 < n:
                    fv[a] = args[i + 1]
                i += 1
            elif len(a) > 2 and a[1] != "-" and a[:2] in vflags:
                fv[a[:2]] = a[2:]          # -n5, -o/tmp/out
        else:
            pos.append(a)
        i += 1
    return pos, fv


class _Out:
    """The items one command line yields, capped."""
    __slots__ = ("items", "capped", "strict")

    def __init__(self, strict=False):
        self.items = []
        self.capped = 0
        # argv with a multi-line argument (a script) split on whitespace: no
        # operand position can be trusted, so a bare word must look like a file
        self.strict = strict

    def add(self, op, tok, isdir, base, bare=True):
        if bare is True and self.strict:
            bare = "file"
        if not _ok(tok, bare):
            return
        if len(self.items) >= MAX_ITEMS:
            self.capped += 1
            return
        self.items.append((op, tok, isdir or tok.endswith("/") or tok in (".", "..", "/"),
                           base))


def _named(out, args, base):
    """Anything path-like on a command line the parser does not know."""
    for a in args:
        if a.startswith("--") and "=" in a:
            a = a.split("=", 1)[1]
        elif "=" in a and a[0] not in "/.~":
            a = a.split("=", 1)[1]
        out.add(N, a, False, base, bare=False)


def _command(ws, out, base, depth, shell):
    """One simple command's words (unquoted). Returns the base for what follows
    -- a literal `cd` moves it, an unresolvable one makes it unknown ("\\0")."""
    i, n = 0, len(ws)
    while i < n:
        w = ws[i]
        if _ASSIGN.match(w):
            v = w.split("=", 1)[1]
            out.add(N, v, False, base, bare=False)
            i += 1
            continue
        if w in _KEYWORDS:
            i += 1
            continue
        b = w.rsplit("/", 1)[-1]
        wr = _WRAPPERS.get(b)
        if wr is None:
            break
        sub = _RUN_SUB.get(b)
        i += 1
        if sub is not None:
            if i < n and (ws[i] == sub or (isinstance(sub, tuple) and ws[i] in sub)):
                i += 1
            elif b in ("uv", "conda", "poetry", "pipenv", "pixi", "apptainer", "singularity"):
                _named(out, ws[i:], base)      # `uv pip install -r x`, `conda env create`
                return base
        vflags, npos = wr
        while i < n and len(ws[i]) > 1 and ws[i][0] == "-":
            a = ws[i]
            if a.startswith("--") and "=" in a:
                k, v = a.split("=", 1)
                if k in _WRAP_PATHS:
                    out.add(_WRAP_PATHS[k], v.split(":", 1)[0], k in ("-D", "--chdir",
                                                                      "--directory", "--cwd"),
                            base)
            elif a in vflags:
                if a in _WRAP_PATHS and i + 1 < n:
                    out.add(_WRAP_PATHS[a], ws[i + 1].split(":", 1)[0],
                            a in ("-D", "--chdir", "-C", "--directory", "--cwd"), base)
                i += 1
            i += 1
        for _ in range(npos):
            if i < n:
                if b in ("flock",):
                    out.add(W, ws[i], False, base)
                elif b in ("apptainer", "singularity"):
                    out.add(R, ws[i], False, base)
                i += 1
    if i >= n:
        return base
    w = ws[i]
    b = w.rsplit("/", 1)[-1]
    args = ws[i + 1:]
    if "/" in w and w[0] != "/" and _ok(w, False):
        out.add(R, w, False, base)                # ./run.sh, scripts/x.sh: a project script

    if w == "for":
        if "in" in args:
            _named(out, args[args.index("in") + 1:], base)
        return base
    if b in ("cd", "pushd"):
        if "-" in args:
            return "\0"                          # `cd -`: wherever it was before
        pos = [a for a in args if a[:1] != "-"]
        if not pos:
            return "~" if b == "cd" else base     # a bare `cd` goes home
        t = pos[0]
        if not _ok(t, True):
            return "\0"
        out.add(N, t, True, base)
        if base == "\0" and t[0] not in "/~$":
            return "\0"
        return t if base is None or t[0] in "/~$" else posixpath.join(base, t)
    if b in _NO_PATHS:
        return base
    if b in _READ:
        for a in _scan(args, _READ[b])[0]:
            out.add(R, a, False, base)
        return base
    if b in _DIRREAD:
        pos = _scan(args, _DIRREAD[b])[0]
        for a in pos:
            out.add(R, a, _EXT.search(a) is None, base)
        if not pos and b != "df":
            out.add(R, ".", True, base)           # `ls` alone lists where it runs
        return base
    if b == "find":
        return _find(args, out, base)
    if b in _PATTERN_FIRST:
        pos, fv = _scan(args, _PATTERN_FIRST[b])
        # the pattern came by flag, so every operand is a file (fd's -e is an extension)
        prog = b != "fd" and any(k in fv for k in ("-e", "-f", "--regexp", "--file",
                                                   "--from-file"))
        if b in ("rg", "ugrep", "ug", "fd") and "--files" in args:
            prog = True
        if b in ("awk", "gawk", "mawk"):
            pos = [a for a in pos if not _ASSIGN.match(a)]
        if not prog:
            pos = pos[1:]
        for k in ("-f", "--file", "--from-file"):
            if k in fv:
                out.add(R, fv[k], False, base)
        for a in pos:
            out.add(R, a, False, base, bare=True if shell else "file")
        if not pos and any(a in ("-r", "-R", "--recursive") or
                           (a[:1] == "-" and a[1:2] != "-" and "r" in a[1:].lower()
                            and b in ("grep", "egrep", "fgrep", "ugrep", "ug"))
                           for a in args):
            # an explicit recursive search with no operand searches where it runs;
            # without -r, grep reads its standard input
            out.add(R, ".", True, base)
        return base
    flags = [a for a in args if a[:1] == "-"] if b in ("sed", "perl") else ()
    if b == "sed" or (b == "perl" and any(_PERL_E.match(a) or _PERL_INPLACE.match(a)
                                          for a in flags)):
        # a one-liner: the expression first (or by -e), then the files it reads,
        # or rewrites in place with -i; a perl script file is an interpreter run
        inplace = any(a.startswith(("-i", "--in-place")) if b == "sed"
                      else _PERL_INPLACE.match(a) is not None for a in flags)
        pos, fv = _scan(args, frozenset({"-e", "-f", "--expression", "--file", "-l", "-s",
                                         "-I", "-M", "-m"}))
        if not any(k in fv for k in ("-e", "-f", "--expression", "--file")):
            pos = pos[1:]                          # the script
        for a in pos:
            out.add(W if inplace else R, a, False, base, bare=True if shell else "file")
        return base
    if b in _WRITE:
        pos = _scan(args, _WRITE[b])[0]
        if b in _MODE_FIRST and "--reference" not in " ".join(args):
            pos = pos[1:]
        for a in pos:
            out.add(W, a, False, base)
        return base
    if b == "sort":
        pos, fv = _scan(args, frozenset({"-k", "-t", "-S", "-T", "-o", "--output", "--key",
                                         "--field-separator", "--buffer-size",
                                         "--temporary-directory", "--parallel"}))
        for a in pos:
            out.add(R, a, False, base)
        for k in ("-o", "--output"):
            if k in fv:
                out.add(W, fv[k], False, base)
        return base
    if b == "mkdir":
        for a in _scan(args, frozenset({"-m", "--mode"}))[0]:
            out.add(W, a, True, base)
        return base
    if b == "mktemp":
        pos, fv = _scan(args, frozenset({"-p", "--tmpdir", "-t", "--suffix"}))
        for a in pos:
            out.add(W, a, False, base)
        if "-p" in fv or "--tmpdir" in fv:
            out.add(W, fv.get("-p") or fv.get("--tmpdir"), True, base)
        return base
    if b in _DELETE:
        for a in _scan(args, _DELETE[b])[0]:
            out.add(D, a, False, base)
        return base
    if b == "rmdir":
        for a in _scan(args, frozenset())[0]:
            out.add(D, a, True, base)
        return base
    if b in _INPLACE:
        ro = any(a in _INPLACE_RO or (a[:1] == "-" and a[1:2] != "-" and
                                      any(c in a[1:] for c in "clt")) for a in args)
        for a in _scan(args, frozenset({"-S", "--suffix", "-b"}))[0]:
            out.add(R if ro else W, a, False, base)
        return base
    if b in _COPY:
        src_op, dst_op, vflags = _COPY[b]
        pos, fv = _scan(args, vflags)
        tgt = fv.get("-t") or fv.get("--target-directory")
        if tgt:
            out.add(dst_op, tgt, True, base)
            srcs = pos
        elif len(pos) >= 2:
            out.add(dst_op, pos[-1], len(pos) > 2, base)
            srcs = pos[:-1]
        else:
            srcs = pos
        for a in srcs:
            out.add(src_op, a, False, base)
        return base
    if b == "tar":
        return _tar(args, out, base)
    if b == "unzip":
        pos, fv = _scan(args, frozenset({"-d", "-x", "-P"}))
        if pos:
            out.add(R, pos[0], False, base)
        if "-l" not in args and "-t" not in args:
            out.add(W, fv.get("-d", "."), True, base)
        return base
    if b == "zip":
        pos = _scan(args, frozenset({"-x", "-i", "-P", "-b", "-n", "-t", "-tt"}))[0]
        if pos:
            out.add(W, pos[0], False, base)
        for a in pos[1:]:
            out.add(R, a, False, base)
        return base
    if b == "dd":
        for a in args:
            if a.startswith("if="):
                out.add(R, a[3:], False, base)
            elif a.startswith("of="):
                out.add(W, a[3:], False, base)
        return base
    if b == "git":
        return _git(args, out, base)
    if b in ("wget", "curl"):
        pos, fv = _scan(args, frozenset({"-O", "-o", "-P", "-T", "-d", "-F", "-H", "-X",
                                         "-u", "-A", "-e", "-x", "-w", "-b", "-c", "-K",
                                         "--output", "--output-document", "--upload-file",
                                         "--directory-prefix", "--data", "--data-binary",
                                         "--header", "--user", "--config"}))
        if b == "wget":
            for k in ("-O", "--output-document"):
                if k in fv and fv[k] != "-":
                    out.add(W, fv[k], False, base)
            for k in ("-P", "--directory-prefix"):
                if k in fv:
                    out.add(W, fv[k], True, base)
        else:
            for k in ("-o", "--output"):
                if k in fv and fv[k] != "-":
                    out.add(W, fv[k], False, base)
            for k in ("-T", "--upload-file"):
                if k in fv:
                    out.add(R, fv[k], False, base)
            for k in ("-d", "--data", "--data-binary", "-F"):
                v = fv.get(k) or ""
                if "@" in v:
                    out.add(R, v.split("@", 1)[1], False, base)
        return base
    if b in ("make", "gmake", "ninja", "cmake"):
        pos, fv = _scan(args, frozenset({"-C", "-f", "-j", "-S", "-B", "--build",
                                         "--directory", "--file", "-l"}))
        for k, op, isdir in (("-C", R, True), ("--directory", R, True), ("-f", R, False),
                             ("--file", R, False), ("-S", R, True), ("-B", W, True),
                             ("--build", W, True)):
            if k in fv:
                out.add(op, fv[k], isdir, base)
        return base
    if b in ("sbatch", "salloc"):
        pos, fv = _scan(args, _WRAPPERS["srun"][0] | frozenset({"--wrap", "-a", "--array",
                                                               "-d", "--dependency",
                                                               "--mail-user", "--mail-type"}))
        for k in ("-o", "--output", "-e", "--error"):
            if k in fv:
                out.add(W, fv[k], False, base)
        for k in ("-D", "--chdir"):
            if k in fv:
                out.add(N, fv[k], True, base)
        if "--wrap" in fv and depth < 3:
            base = _script(fv["--wrap"], out, base, depth + 1)
        elif pos:
            out.add(R, pos[0], False, base)
            _named(out, pos[1:], base)
        return base
    if b in ("bwrap", "codex-linux-sandbox", "codex-linux-san"):
        # the sandbox's own setup binds half the filesystem; only its payload ran
        if "--" in args and depth < 3:
            _command(args[args.index("--") + 1:], out, base, depth + 1, False)
        return base
    if b in _SHELLS:
        j, script = 0, None
        while j < len(args) and args[j][:1] in ("-", "+"):
            if _SHORT_C.match(args[j]):
                rest = args[j + 1:]
                script = (rest[0] if shell else " ".join(rest)) if rest else None
                break
            j += 1
        if script is not None:
            if depth < 3:
                _script(script, out, base, depth + 1)
        elif j < len(args) and not args[j].startswith(_SYSTEM_EXEC):
            out.add(R, args[j], False, base)
        return base
    if _INTERP.match(b):
        stop = {"-c", "-e", "-E", "--eval", "-p", "--print", "-m", "-"}
        if b == "perl":
            stop = {"-e", "-E", "-"}
        j = 0
        while j < len(args):
            a = args[j]
            if a in stop or (b.startswith("python") and a.startswith(("-c", "-m"))
                             and len(a) > 2):
                if a in ("-m",) or a.startswith("-m"):
                    _named(out, args[j + 2:], base)
                return base
            if a[:1] != "-":
                break
            if a in ("-W", "-X", "-r", "--require", "-I", "-M", "--import", "--project"):
                j += 1
            j += 1
        if j < len(args):
            if b in ("deno", "bun") and args[j] in ("run", "test", "x") and j + 1 < len(args):
                j += 1
            if not args[j].startswith(_SYSTEM_EXEC):
                out.add(R, args[j], False, base)
            _named(out, args[j + 1:], base)
        return base
    _named(out, args, base)
    return base


_FIND_OPTS = frozenset({"-H", "-L", "-P"})


def _find(args, out, base):
    """Starting points up to the first expression token; `-delete`, or an
    `-exec rm`, makes them deletions."""
    roots, i = [], 0
    while i < len(args):
        a = args[i]
        if a in _FIND_OPTS or a.startswith("-O"):
            i += 1
            continue
        if a == "-D":
            i += 2
            continue
        if a[:1] in ("-", "(", "!", ")") or a == "\\(":
            break
        roots.append(a)
        i += 1
    expr = args[i:]
    op = R
    if "-delete" in expr:
        op = D
    else:
        for j, a in enumerate(expr):
            if a in ("-exec", "-execdir", "-ok", "-okdir") and j + 1 < len(expr) and \
                    expr[j + 1].rsplit("/", 1)[-1] in ("rm", "unlink", "shred"):
                op = D
                break
    for k in ("-fprint", "-fprint0", "-fprintf", "-fls"):
        if k in expr and expr.index(k) + 1 < len(expr):
            out.add(W, expr[expr.index(k) + 1], False, base)
    for a in roots or ["."]:
        out.add(op, a, True, base)
    return base


def _tar(args, out, base):
    mode, archive, cdir, pos, i = None, None, None, [], 0
    first = True
    while i < len(args):
        a = args[i]
        if first and a[:1] != "-" and re.match(r"^[A-Za-z]+$", a):
            a = "-" + a                      # `tar xzf a.tgz`: bundled old-style
        first = False
        if a in ("--extract", "--get"):
            mode = "x"
        elif a == "--create":
            mode = "c"
        elif a in ("--list",):
            mode = "t"
        elif a.startswith("--file="):
            archive = a[7:]
        elif a.startswith("--directory="):
            cdir = a[12:]
        elif a in ("--file", "-f"):
            archive = args[i + 1] if i + 1 < len(args) else None
            i += 1
        elif a in ("--directory", "-C"):
            cdir = args[i + 1] if i + 1 < len(args) else None
            i += 1
        elif a.startswith("--"):
            pass
        elif a[:1] == "-" and len(a) > 1:
            letters = a[1:]
            for c in "xct":
                if c in letters:
                    mode = mode or c
            if "f" in letters:
                rest = letters[letters.index("f") + 1:]
                if rest:
                    archive = rest
                elif i + 1 < len(args):
                    archive = args[i + 1]
                    i += 1
            if "C" in letters and i + 1 < len(args):
                cdir = args[i + 1]
                i += 1
        else:
            pos.append(a)
        i += 1
    if archive and archive != "-":
        out.add(W if mode == "c" else R, archive, False, base)
    if mode == "x":
        out.add(W, cdir or ".", True, base)
    else:
        if cdir:
            out.add(R, cdir, True, base)
        for a in pos:
            out.add(R if mode == "c" else N, a, False, base if not cdir else
                    (cdir if base is None else posixpath.join(base, cdir)))
    return base


def _git(args, out, base):
    i = 0
    while i < len(args) and args[i][:1] == "-":
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            out.add(R, args[i + 1], True, base)
            base = args[i + 1] if base is None or args[i + 1][0] in "/~" \
                else posixpath.join(base, args[i + 1])
            i += 1
        elif a in ("-c", "--namespace", "--exec-path") and i + 1 < len(args):
            i += 1
        elif a.startswith(("--git-dir=", "--work-tree=")):
            out.add(N, a.split("=", 1)[1], True, base)
        i += 1
    if i >= len(args):
        return base
    sub, rest = args[i], args[i + 1:]
    if sub == "clone":
        pos = _scan(rest, frozenset({"-b", "--branch", "-o", "--origin", "--depth",
                                     "-c", "--config", "--reference", "-j", "--filter",
                                     "--template", "--separate-git-dir"}))[0]
        if len(pos) >= 2:
            out.add(W, pos[1], True, base)
        return base
    paths = rest[rest.index("--") + 1:] if "--" in rest else []
    if sub in ("rm", "mv", "add", "restore", "checkout") and not paths:
        if sub in ("rm", "mv", "add", "restore"):
            paths = _scan(rest, frozenset({"-m", "--pathspec-from-file", "-s", "--source"}))[0]
    op = {"rm": D if "--cached" not in rest else N, "mv": W, "restore": W,
          "checkout": W, "add": N}.get(sub, N)
    for a in paths:
        out.add(op, a, False, base, bare=sub in ("rm", "mv", "add", "restore", "checkout"))
    return base


def _script(s, out, base, depth):
    """A shell script: commands, separators and redirections, in one regex pass."""
    if "<<" in s:
        s = _strip_heredocs(s)
    words = []
    for m in _SH.finditer(s):
        g = m.group("word")
        if g is not None:
            words.append(_unq(g))
            continue
        op = m.group("redir")
        if op is not None:
            t = m.group("rt")
            if t is None:
                continue
            t = _unq(t)
            if t[:1] == "&" or t[:1].isdigit() and len(t) == 1:
                continue                         # 2>&1, >&2: a descriptor, not a file
            out.add(R if op.endswith("<") else W, t, False, base, bare=True)
        elif m.group("sep") is not None:
            if words:
                base = _command(words, out, base, depth, True)
                words = []
    if words:
        base = _command(words, out, base, depth, True)
    return base


def plan(role, args):
    """The paths one command line names, unresolved: a tuple of
    `(op, token, isdir, base)`, `base` being where a literal `cd` left the
    command (None: the process's own cwd; "\\0": somewhere unknown).

    Independent of the record's cwd and user, so it is cached per argv.
    """
    if not args or role in _SKIP_ROLES:
        return (), 0
    out = _Out()
    if role == "agent":
        # the agent's own launch line: only the directories it was granted
        for m in _ADD_DIR.finditer(args):
            out.add(N, m.group(1), True, None)
        return tuple(out.items), out.capped
    if role == "tool-shell" or "/.claude/shell-snapshots/" in args:
        # Claude Code's `zsh -c source <snapshot> && ... && eval '<cmd>' ... &&
        # pwd -P >| /tmp/claude-...-cwd`, sandboxed or not: only the eval'd
        # command is the agent's; the rest is boilerplate that would name the
        # snapshot and a temp file on every call. `ran` is the inspector's own
        # reading of it (proctree.py), so the two cannot disagree.
        cmd = ran("tool-shell", args)
        if cmd is not None:
            _script(cmd, out, None, 1)
        return tuple(out.items), out.capped
    if role == "sandbox":
        # a wrapper's payload, after its ` -- `: an argv again
        cmd = ran(role, args)
        if cmd is not None:
            _command(cmd.split(), out, None, 1, False)
        return tuple(out.items), out.capped
    # An exec'd argv, joined with spaces by the collector: whitespace-split, no
    # shell syntax -- `;` or `>` inside an argument is a character, not an operator.
    out.strict = "\n" in args
    _command(args.split(), out, None, 0, False)
    return tuple(out.items), out.capped


# ------------------------------------------------------------- resolution
_HOME_IN_ARGS = re.compile(r"(/[^\s'\"]+?)/\.(?:claude|codex)/")


def resolve(tok, base, cwd, home, inherited=False):
    """An absolute, normalised path, or None when it cannot be placed.

    `inherited`: the collector did not read this process's cwd, it copied its
    parent's at fork -- which is before a shell's own `cd`, and before the
    chdir an agent's Bash tool makes into the directory its previous call left
    it in. Such a cwd still places `README.md` (an agent mostly works where it
    started), but not `../data/x`: climbing out of a directory the process may
    never have been in is how `/home/marthen` plus `../data` became another
    user's home on rtx6000a."""
    if tok[0] == "~":
        if tok == "~" or tok.startswith("~/"):
            if not home:
                return None
            tok = home + tok[1:]
        else:
            user, _, rest = tok[1:].partition("/")
            tok = "/home/%s/%s" % (user, rest)
    elif tok[0] == "$":
        if not home:
            return None
        tok = home + tok[tok.index("}") + 1 if tok.startswith("${") else 5:]
    if tok[0] != "/":
        if base == "\0":
            return None
        if base:
            b = resolve(base, None, cwd, home) if base[0] in "~$" else base
            if b is None:
                return None
            if b[0] == "/":
                tok = posixpath.join(b, tok)
            elif cwd and cwd != "/":
                tok = posixpath.join(cwd, b, tok)    # `cd sub`, `git -C ..`: still the cwd's
            else:
                return None
        elif cwd and cwd != "/":
            tok = posixpath.join(cwd, tok)
        else:
            # No cwd, or `/`: an agent's processes are at `/` when they inherited
            # an IDE's or a daemon's directory rather than ran there (the
            # collector inherits cwd at fork, before the child's own chdir), and
            # `ls src` from there is not `/src`. Counted as unplaced instead.
            return None
        if tok[0] != "/":
            return None
        if inherited and (base is None or base[0] not in "/~$"):
            # placed on the copied cwd: it may sit inside it, not climb out of it
            p = posixpath.normpath(tok)
            if not under(p, cwd):
                return None
    p = posixpath.normpath(tok)
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    if p.startswith(("/dev/", "/proc/", "/sys/")) or p in ("/dev", "/proc", "/sys"):
        return None
    return p


def split(path, isdir):
    """(directory, name within it). A glob in a middle component cuts the
    directory there: `src/*/x.py` is somewhere under `src`."""
    if isdir:
        d, name = path, "."
    else:
        d, name = posixpath.split(path)
        d = d or "/"
    if _GLOB.search(d):
        parts = d.split("/")
        k = next(i for i, p in enumerate(parts) if _GLOB.search(p))
        cut = "/".join(parts[:k]) or "/"
        name = posixpath.relpath(path, cut) if not isdir else posixpath.relpath(d, cut)
        d = cut
    return d, name[:NAME_CUT]


def under(path, anchor):
    """`path` is `anchor` or below it -- `/a/proj-old` is NOT under `/a/proj`."""
    return path == anchor or anchor == "/" or path.startswith(anchor.rstrip("/") + "/")


# --------------------------------------------------------------- reducer
class KeyStat:
    """One collector session key's processes: where they ran and what they named."""
    __slots__ = ("root", "user", "procs", "first", "last", "cwd0_t", "cwd0", "dirs",
                 "nocwd", "over", "unresolved", "src_n", "src_read")

    def __init__(self, root, user):
        self.root = root
        self.user = user
        self.procs = 0
        self.first = None
        self.last = None
        self.cwd0_t = None          # the first call's directory off `/`, and when
        self.cwd0 = None
        # dir -> [procs, io_n, rchar, wchar, rd, wr, first, last, {name: ops}, names_over]
        self.dirs = {}
        self.nocwd = 0              # processes whose cwd was not recorded
        self.over = 0               # records past the per-key directory cap
        self.unresolved = 0         # named relative paths with no cwd to resolve against
        self.src_n = 0              # processes whose record says where its cwd came from
        self.src_read = 0           # ... read at exec (`proc`), not inherited from the parent

    # Saved as a bare tuple: a day holds ~20 000 of these (one per tool call),
    # and a per-object dict of slot names was a third of their pickled size.
    def __reduce__(self):
        return (_keystat, tuple(getattr(self, k) for k in self.__slots__))


def _keystat(*vals):
    st = KeyStat.__new__(KeyStat)
    for k, v in zip(KeyStat.__slots__, vals):
        setattr(st, k, v)
    return st


def _dir_entry():
    return [0, 0, 0.0, 0.0, 0.0, 0.0, None, None, None, 0]


def _touch(d, e):
    if e is not None:
        if d[6] is None or e < d[6]:
            d[6] = e
        if d[7] is None or e > d[7]:
            d[7] = e


def _add_name(d, name, ops, cap=MAX_NAMES):
    names = d[8]
    if names is None:
        names = d[8] = {}
    cur = names.get(name)
    if cur is not None:
        names[name] = cur | ops
    elif len(names) < cap:
        names[name] = ops
    else:
        d[9] += 1                   # past the cap: counted, not itemised


class WorkspaceReducer:
    EVENTS = _EV
    KEY = "workspace"
    # Derived from argv, and rebuilt on demand: not state worth saving.
    SNAPSHOT_SKIP = ("_cache", "_admitted")

    def __init__(self, cfg, classes=None, window_s=None, procs=None):
        r = cfg.get("reducers.workspace", {}) or {}
        self.max_keys = int(r.get("max_keys", 150000))
        self.max_dirs = int(r.get("max_dirs", 300))
        self.max_entries = int(r.get("max_dir_entries", 400000))
        self.depth = int(r.get("collapse_depth", 4))
        self.max_agents = int(r.get("max_agents", 30))
        self.max_rows = int(r.get("max_dirs_shown", 40))
        self.window_s = window_s or int(cfg.get("live.window_min", 1440)) * 60
        self.procs = procs
        self.keys = {}              # (host, key pid) -> KeyStat
        self.entries = 0            # directory entries across every key
        self.homes = {}             # user -> home, read off the agent's own paths
        self.dropped = 0            # records whose key could not be admitted
        self.capped_tokens = 0      # path tokens past MAX_ITEMS on one command line
        self.parse_errors = 0
        self.inherited = 0          # records whose argv a fork inherited: not re-read
        self.probes = 0             # probes and self-rooted helpers, not kept
        self.fed = 0
        self._cache = {}
        # Keys an earlier hour admitted (`seed`): a probe record of one of
        # those is not new, so it is kept, as one pass keeps it.
        self._admitted = {}

    # ------------------------------------------------------------- feed
    def feed(self, rec):
        # The hot path: every exit of the window comes through here, so it is
        # written for the common record -- the same key, the same directory and
        # the same argv as a thousand before it -- at a few dict lookups.
        get = rec.get
        if get("_a3") != "agent" or get("_kt") != "apid":
            return
        key = (get("host"), get("_kv"))
        st = self.keys.get(key)
        if st is None:
            role = get("_role")
            if (role == "probe" or (role == "helper" and get("tree_root_pid") == get("pid"))) \
                    and key not in self._admitted:
                # A version probe, or a helper at the top of its own tree (an
                # IDE's `sh -c ... codex --version` poll, ~1 100 a day per
                # user): no agent is above either, so neither can fold into
                # one, and keeping them was most of the keys a day holds.
                self.probes += 1
                return
            if len(self.keys) >= self.max_keys:
                self._evict()
                if len(self.keys) >= self.max_keys:
                    self.dropped += 1   # counted, never silent
                    return
            user = get("user")
            st = self.keys[key] = KeyStat(get("tree_root_pid"),
                                          sys.intern(user) if user else user)
        self.fed += 1
        st.procs += 1
        e = get("_ts_epoch")
        if e is not None:
            if st.last is None or e > st.last:
                st.last = e
            if st.first is None or e < st.first:
                st.first = e
        cwd = get("cwd")
        src = get("cwd_source")
        if src is not None:             # absent from schema 7: the field was dropped
            st.src_n += 1
            if src == "proc":
                st.src_read += 1
        if not cwd:
            cwd = None
            st.nocwd += 1
        else:
            d = st.dirs.get(cwd)
            if d is None:
                d = self._entry(st, cwd)
            if d is not None:
                d[0] += 1
                io = get("io")
                if io:
                    rc = io.get("rchar_mb")
                    if rc is not None:
                        d[1] += 1
                        d[2] += rc
                        d[3] += io.get("wchar_mb") or 0.0
                        d[4] += io.get("rd_mb") or 0.0
                        d[5] += io.get("wr_mb") or 0.0
                if e is not None:
                    if d[7] is None or e > d[7]:
                        d[7] = e
                    if d[6] is None or e < d[6]:
                        d[6] = e
            if e is not None and cwd != "/" and (st.cwd0_t is None or e < st.cwd0_t):
                st.cwd0_t, st.cwd0 = e, cwd
        args = get("args")
        if not args:
            return
        role = get("_role")
        # untagged processes -- nearly all of them -- are keyed on argv alone
        ck = args if role is None else (role, args)
        got = self._cache.get(ck)
        if got is None:
            try:
                items, capped = plan(role, args)
                got = [items, capped, 0, None]    # + the last resolution: (cwd, home)
            except Exception:
                got = False             # a parser bug: cached, so counted per record
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[ck] = got
            user = get("user")
            if user and user not in self.homes and ("/.claude/" in args or "/.codex/" in args):
                m = _HOME_IN_ARGS.search(args)
                if m:
                    self.homes[user] = m.group(1)
        if got is False:
            # Counted per record, like everything else here, so a resumed window
            # (whose argv cache was not saved) counts what a fresh one does.
            self.parse_errors += 1
            return
        if not got[0] and not got[1]:
            return                      # nothing path-like: most command lines
        if get("argv_source") == "inherit" or get("fork_only"):
            # A fork that never exec'd carries its parent's argv: the parent's
            # own record names those paths once already.
            self.inherited += 1
            return
        if got[1]:
            self.capped_tokens += got[1]
        if not got[0]:
            return
        home = self.home_of(st.user)
        inherited = src == "inherit"
        memo = got[3]
        if memo is not None and memo[0] == cwd and memo[1] == home and memo[2] == inherited:
            placed = memo[3]
        else:
            placed = []
            for op, tok, isdir, base in got[0]:
                p = resolve(tok, base, cwd, home, inherited)
                placed.append(None if p is None else split(p, isdir) + (op,))
            got[3] = (cwd, home, inherited, placed)
        for x in placed:
            if x is None:
                st.unresolved += 1
                continue
            d = st.dirs.get(x[0])
            if d is None:
                d = self._entry(st, x[0])
                if d is None:
                    continue
            _add_name(d, x[1], x[2])
            _touch(d, e)

    # ------------------------------------------------------------ merge
    # A window is merged from saved hour states (`hours`). A key's processes
    # merge key by key, as `feed` would have added them. Two things cross an
    # hour, and are carried into each hour's reduction: `homes`, since a home
    # read off one hour's argv places the next hour's paths, and which keys
    # are already admitted, since `feed` skips a probe only on a key's first
    # record.
    CARRY_AGED = ("keys",)

    def carry(self):
        keys = self._admitted
        keys.update(dict.fromkeys(self.keys))
        return {"homes": self.homes, "keys": keys, "touched": {"keys": set(self.keys)}}

    def seed(self, c):
        self.homes = dict(c["homes"])
        self._admitted = c.get("keys") or {}

    def strip(self):
        self._admitted = {}

    def install(self, c):
        for u, h in c["homes"].items():
            self.homes.setdefault(u, h)
        self._admitted = c.get("keys") or {}

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed.

        Exact until a cap is reached. Past `max_dirs` a key's directory, or past
        `max_dir_entries` the window's, is counted in `over` once per process
        that ran there and name it held, where `feed` counts every record and
        every name; past `MAX_NAMES` a directory's name is counted once, where
        `feed` counts every mention. A day reaches none of them.
        """
        for u, h in o.homes.items():
            self.homes.setdefault(u, h)
        for key, st in o.keys.items():
            cur = self.keys.get(key)
            if cur is None:
                if len(self.keys) >= self.max_keys:
                    self._evict()
                    if len(self.keys) >= self.max_keys:
                        self.dropped += st.procs
                        continue
                cur = self.keys[key] = KeyStat(st.root, st.user)
            self._merge_key(cur, st)
        self.dropped += o.dropped
        self.capped_tokens += o.capped_tokens
        self.parse_errors += o.parse_errors
        self.inherited += o.inherited
        self.probes += o.probes
        self.fed += o.fed

    def _merge_key(self, cur, st):
        cur.procs += st.procs
        if st.first is not None and (cur.first is None or st.first < cur.first):
            cur.first = st.first
        if st.last is not None and (cur.last is None or st.last > cur.last):
            cur.last = st.last
        if st.cwd0_t is not None and (cur.cwd0_t is None or st.cwd0_t < cur.cwd0_t):
            cur.cwd0_t, cur.cwd0 = st.cwd0_t, st.cwd0
        cur.nocwd += st.nocwd
        cur.over += st.over
        cur.unresolved += st.unresolved
        cur.src_n += st.src_n
        cur.src_read += st.src_read
        for path, d in st.dirs.items():
            c = cur.dirs.get(path)
            if c is None:
                if len(cur.dirs) >= self.max_dirs or self.entries >= self.max_entries:
                    cur.over += d[0] + len(d[8] or ())
                    continue
                cur.dirs[sys.intern(path)] = d
                self.entries += 1
                continue
            for i in range(6):
                c[i] += d[i]
            _touch(c, d[6])
            _touch(c, d[7])
            c[9] += d[9]
            if d[8]:
                for name, ops in d[8].items():
                    _add_name(c, name, ops)

    def _entry(self, st, path):
        d = st.dirs.get(path)
        if d is None:
            if len(st.dirs) >= self.max_dirs or self.entries >= self.max_entries:
                st.over += 1
                return None
            # Interned: the same directory recurs across thousands of keys (one
            # per tool call), and an interned string is held -- and pickled into
            # the saved window -- once.
            path = sys.intern(path)
            d = st.dirs[path] = _dir_entry()
            self.entries += 1
        return d

    def _evict(self):
        cutoff = time.time() - self.window_s
        dead = [k for k, s in self.keys.items() if (s.last or 0) < cutoff]
        for k in dead:
            self.entries -= len(self.keys.pop(k).dirs)

    def home_of(self, user):
        """A user's home directory: read off their agent's own paths when one
        named it (`/home/u/.claude/...`), else assumed `/home/<user>`."""
        if not user:
            return None
        h = self.homes.get(user)
        return h if h else "/home/%s" % user

    # ----------------------------------------------------------- result
    def fold(self, since):
        """(host, agent pid) -> merged key state, folding every key through the
        process index as trajectory rows are."""
        agents = {}
        for (host, kv), st in self.keys.items():
            if (st.last or 0) < since:
                continue
            try:
                kpid = int(kv)
            except (TypeError, ValueError):
                continue                # not a pid: nothing to fold it into
            if self.procs is not None:
                owner, _via = self.procs.owner(host, kpid, st.root)
            else:
                owner = kpid
            acc = agents.get((host, owner))
            if acc is None:
                acc = agents[(host, owner)] = _Acc()
            acc.merge(st)
        return agents

    def view(self, accs, home, user):
        """One agent's (or one agent and its subagents') blast radius."""
        acc = _Acc.combine(accs)
        uhome = self.home_of(user)
        rows = {}
        raw = 0
        for path, d in acc.dirs.items():
            raw += 1
            inside = under(path, home) if home else None
            c, anchor, root, kind = self._collapse(path, home, uhome, inside)
            r = rows.get(c)
            if r is None:
                r = rows[c] = _row(c, inside, anchor, root, kind)
            r["raw"] += 1
            _fold_in(r, path, d[:8] + [None, d[9]], d[8])
        # A directory with many quiet subdirectories -- one per run, one per
        # timestamp -- keeps its busiest few and folds the rest into itself.
        changed = True
        while changed:
            changed = False
            kids = {}
            for c, r in rows.items():
                par = posixpath.dirname(c)
                if c != r["anchor"] and par != c and under(par, r["anchor"]):
                    kids.setdefault(par, []).append(c)
            for par, cs in kids.items():
                # a project's own top level (src, tests, docs, data ...) is the
                # map a reader wants, so it gets more room than a run directory
                top = par == rows[cs[0]]["anchor"]
                if len(cs) <= (SIBLINGS_TOP if top else SIBLINGS_MAX):
                    continue
                keep = SIBLINGS_TOP // 2 if top else SIBLINGS_KEEP
                cs.sort(key=lambda c: (-_activity(rows[c]), c))
                tgt = rows.get(par)
                if tgt is None:
                    r0 = rows[cs[0]]
                    tgt = rows[par] = _row(par, r0["inside"], r0["anchor"], r0["root"],
                                           r0["kind"])
                for c in cs[keep:]:
                    r = rows.pop(c)
                    tgt["raw"] += r["raw"]
                    _fold_in(tgt, c, r["d"], r["names"])
                changed = True

        out_rows = []
        tot = {"procs": 0, "procs_in": 0, "io_n": 0, "rchar": 0.0, "wchar": 0.0,
               "rd": 0.0, "wr": 0.0, "wchar_in": 0.0}
        named = dict.fromkeys(("read", "write", "delete", "named"), 0)
        outside = dict.fromkeys(("read", "write", "delete", "named"), 0)
        roots = {}
        flags = []
        for c, r in rows.items():
            agg = r["d"]
            counts = {k: sum(1 for v in r["names"].values() if v & bit) for k, bit in OPS}
            for k in counts:
                named[k] += counts[k]
                if r["inside"] is False:
                    outside[k] += counts[k]
            tot["procs"] += agg[0]
            tot["io_n"] += agg[1]
            tot["rchar"] += agg[2]
            tot["wchar"] += agg[3]
            tot["rd"] += agg[4]
            tot["wr"] += agg[5]
            if r["inside"]:
                tot["procs_in"] += agg[0]
                tot["wchar_in"] += agg[3]
            flag = None
            if r["inside"] is False and (counts["write"] or counts["delete"]):
                flag = "delete" if counts["delete"] else "write"
                for name, ops in sorted(r["names"].items()):
                    if ops & (W | D) and len(flags) < 400:
                        flags.append({"path": redact(_join(c, name)),
                                      "shown": redact(self._shown(_join(c, name), home,
                                                                  uhome)),
                                      "op": "delete" if ops & D else "write",
                                      "kind": r["kind"]})
            if r["inside"] is False and r["root"]:
                g = roots.get(r["root"])
                if g is None:
                    g = roots[r["root"]] = {"root": r["root"], "kind": r["kind"], "procs": 0,
                                            "dirs": 0, "read": 0, "write": 0, "delete": 0,
                                            "named": 0, "wchar_mb": 0.0, "io_n": 0}
                g["procs"] += agg[0]
                g["dirs"] += r["raw"]
                g["wchar_mb"] += agg[3]
                g["io_n"] += agg[1]
                for k in counts:
                    g[k] += counts[k]
            examples = sorted(r["names"].items(),
                              key=lambda kv: (-(kv[1] & (W | D)), kv[0]))[:4]
            out_rows.append({
                "path": redact(c), "shown": redact(self._shown(c, home, uhome)),
                "inside": r["inside"], "root": r["root"], "kind": r["kind"],
                "deeper": r["raw"] - (1 if c in acc.dirs else 0),
                "procs": agg[0],
                "io": _io(agg),
                "read": counts["read"], "write": counts["write"],
                "delete": counts["delete"], "named": counts["named"],
                "names_capped": agg[9],
                "examples": [[redact(n), _opstr(o)] for n, o in examples],
                "first": _r(agg[6]), "last": _r(agg[7]),
                "flag": flag,
                "_score": _activity(r),
            })
        # By activity; a flagged row is never cut, however quiet, because it is
        # the reading this panel exists for.
        out_rows.sort(key=lambda x: (-x["_score"], x["path"]))
        shown = out_rows[:self.max_rows]
        rest = out_rows[self.max_rows:]
        shown += [x for x in rest if x["flag"]]
        cut = [x for x in rest if not x["flag"]]
        for x in out_rows:
            del x["_score"]
        flags.sort(key=lambda f: (_SEVERITY.get(f["kind"], 3), f["op"] != "delete",
                                  f["path"]))
        known = tot["procs"]            # processes placed in a directory (not past a cap)
        for g in roots.values():
            g["wchar_mb"] = round(g["wchar_mb"], 3) if g["io_n"] else None
            del g["io_n"]
        summary = {
            "procs": acc.procs,
            "procs_no_cwd": acc.nocwd,
            # how much of `ran in` is the process's own cwd: read at exec, rather
            # than copied from its parent at fork. Null when no record said.
            "cwd_read_pct": (round(100.0 * acc.src_read / acc.src_n, 1)
                             if acc.src_n else None),
            "dirs": raw,
            "dirs_capped": acc.over,
            "inside_pct": (round(100.0 * tot["procs_in"] / known, 2)
                           if home and known else None),
            "procs_inside": tot["procs_in"] if home else None,
            "procs_outside": (known - tot["procs_in"]) if home else None,
            "io": _io([tot["procs"], tot["io_n"], tot["rchar"], tot["wchar"], tot["rd"],
                       tot["wr"]]),
            "wchar_inside_pct": (round(100.0 * tot["wchar_in"] / tot["wchar"], 2)
                                 if home and tot["io_n"] and tot["wchar"] > 0 else None),
            "named": named,
            # Null, not zero, without a home: "outside" has nothing to be outside of.
            "outside": outside if home else None,
            "outside_writes": (outside["write"] + outside["delete"]) if home else None,
            "unresolved": acc.unresolved,
            "roots": sorted(roots.values(), key=lambda g: (-(g["write"] + g["delete"]),
                                                           -g["procs"], g["root"])),
            "first": _r(acc.first), "last": _r(acc.last), "keys": acc.keys,
        }
        return {"summary": summary, "dirs": shown,
                "dirs_more": {"rows": len(cut), "procs": sum(x["procs"] for x in cut)},
                "flags": flags[:16], "flags_more": max(0, len(flags) - 16)}

    def _collapse(self, path, home, uhome, inside):
        """(shown directory, its anchor, root label, root kind): `path` folded to
        at most `depth` levels below the home, or below its top-level root."""
        if inside:
            anchor, root, kind = home, None, "home"
        else:
            anchor, root, kind = _root(path, uhome)
        if anchor == "/" or not anchor:
            base_n = 0
        else:
            base_n = anchor.rstrip("/").count("/")
        parts = path.rstrip("/").split("/") if path != "/" else [""]
        keep = base_n + 1 + self.depth
        if len(parts) > keep:
            path = "/".join(parts[:keep])
        return path, anchor or "/", root, kind

    def _shown(self, path, home, uhome):
        if home and under(path, home) and home != "/":
            rel = path[len(home.rstrip("/")):]
            return "." + rel if rel else "./"
        return tilde(path, uhome)

    def result(self):
        """The buffer and parse counters. The per-agent views are composed by the
        cascades reducer, which owns the agent tree they follow."""
        return {"keys": len(self.keys), "cap": self.max_keys, "dir_entries": self.entries,
                "dir_entries_cap": self.max_entries, "dropped": self.dropped,
                "parse_errors": self.parse_errors,
                "capped_tokens": self.capped_tokens, "inherited_argv": self.inherited,
                "probes_skipped": self.probes,
                "fed": self.fed}


SIBLINGS_MAX = 6            # subdirectory rows one directory keeps before folding
SIBLINGS_KEEP = 3           # ... and how many of its busiest it keeps when it does
SIBLINGS_TOP = 16           # the same, directly below the home or a top-level root


def _row(path, inside, anchor, root, kind):
    return {"path": path, "inside": inside, "anchor": anchor, "root": root, "kind": kind,
            "raw": 0, "d": _dir_entry(), "names": {}}


def _activity(r):
    return r["d"][0] + len(r["names"]) * 3


def _fold_in(r, path, d, names):
    """Directory `path`'s counters and names into row `r`, its names re-rooted
    on the row's directory."""
    agg = r["d"]
    for i in range(6):
        agg[i] += d[i]
    _touch(agg, d[6])
    _touch(agg, d[7])
    agg[9] += d[9]
    if names:
        pre = "" if path == r["path"] else posixpath.relpath(path, r["path"]) + "/"
        mine = r["names"]
        for name, ops in names.items():
            full = name if not pre else (pre[:-1] if name == "." else pre + name)
            cur = mine.get(full)
            if cur is not None:
                mine[full] = cur | ops
            elif len(mine) < MAX_NAMES * 2:
                mine[full] = ops
            else:
                agg[9] += 1


class _Acc:
    """An agent's keys merged: KeyStat's fields, summed."""
    __slots__ = ("procs", "nocwd", "dirs", "over", "unresolved", "first", "last",
                 "cwd0_t", "cwd0", "keys", "src_n", "src_read")

    def __init__(self):
        self.procs = 0
        self.nocwd = 0
        self.dirs = {}
        self.over = 0
        self.unresolved = 0
        self.first = None
        self.last = None
        self.cwd0_t = None
        self.cwd0 = None
        self.keys = 0
        self.src_n = 0
        self.src_read = 0

    def merge(self, st):
        self.keys += st.keys if isinstance(st, _Acc) else 1
        self.procs += st.procs
        self.nocwd += st.nocwd
        self.over += st.over
        self.unresolved += st.unresolved
        self.src_n += st.src_n
        self.src_read += st.src_read
        if st.first is not None and (self.first is None or st.first < self.first):
            self.first = st.first
        if st.last is not None and (self.last is None or st.last > self.last):
            self.last = st.last
        if st.cwd0_t is not None and (self.cwd0_t is None or st.cwd0_t < self.cwd0_t):
            self.cwd0_t, self.cwd0 = st.cwd0_t, st.cwd0
        for path, d in st.dirs.items():
            cur = self.dirs.get(path)
            if cur is None:
                cur = self.dirs[path] = _dir_entry()
            for i in range(6):
                cur[i] += d[i]
            _touch(cur, d[6])
            _touch(cur, d[7])
            cur[9] += d[9]
            if d[8]:
                for name, ops in d[8].items():
                    _add_name(cur, name, ops)

    @classmethod
    def combine(cls, accs):
        if len(accs) == 1:
            return accs[0]
        out = cls()
        for a in accs:
            out.merge(a)
        return out


_N_HOME = re.compile(r"^/n/home[^/]*/([^/]+)")

# How alarming a write outside the home is, by where it landed: first listed first.
_SEVERITY = {"other user's home": 0, "system": 1, "filesystem root": 1,
             "shared storage": 2, "own home": 3, "other": 3, "cache & tooling": 4,
             "agent state": 5, "temp": 6}


def _root(path, uhome):
    """The top-level place a directory outside the home belongs to, and what
    kind of place it is: (root path, label, kind)."""
    if uhome and under(path, uhome):
        rest = path[len(uhome.rstrip("/")):].strip("/").split("/", 1)[0]
        if not rest:
            return uhome, "~", "own home"
        kind = ("agent state" if rest in (".claude", ".codex", ".cursor", ".gemini",
                                          ".copilot")
                else "cache & tooling" if rest in (".cache", ".local", ".conda", ".npm",
                                                   ".nvm", ".cargo", ".rustup", ".pyenv",
                                                   ".vscode-server", ".config", ".ipython",
                                                   ".jupyter", ".triton", ".huggingface")
                else "own home")
        return uhome.rstrip("/") + "/" + rest, "~/" + rest, kind
    parts = path.split("/")
    top = parts[1] if len(parts) > 1 else ""
    if not top:
        return "/", "/", "filesystem root"
    if top == "home" and len(parts) > 2 and parts[2]:
        return "/home/" + parts[2], "~" + parts[2], "other user's home"
    # FAS homes are /n/homeNN/<user>. Read as shared storage before, so on that
    # cluster "reached another user's home" could never count anything: a day
    # of boslogin06 has agents naming paths in other users' homes (roadmap
    # note 33). A name that is the agent's own user is its own home, even when
    # its home was never read off an argv and so is not `uhome`.
    m = _N_HOME.match(path)
    if m:
        root = path[:m.end()]
        if uhome and m.group(1) == uhome.rstrip("/").rsplit("/", 1)[-1]:
            return root, "~", "own home"
        return root, "~" + m.group(1), "other user's home"
    if top in ("tmp", "dev") or path.startswith(("/var/tmp", "/dev/shm", "/run/user")):
        r = "/var/tmp" if path.startswith("/var/tmp") else "/tmp"
        return r, r, "temp"
    if top in ("n", "net", "mnt", "media", "nfs", "lustre", "gpfs") and len(parts) > 2:
        return "/%s/%s" % (top, parts[2]), "/%s/%s" % (top, parts[2]), "shared storage"
    if top in ("scratch", "data", "datasets", "work", "project", "projects", "shared",
               "group", "groups", "lab", "labs"):
        return "/" + top, "/" + top, "shared storage"
    if top in ("usr", "etc", "opt", "bin", "sbin", "lib", "lib64", "lib32", "boot", "var",
               "srv", "snap", "root", "run", "sys", "proc"):
        return "/" + top, "/" + top, "system"
    return "/" + top, "/" + top, "other"


def tilde(path, uhome):
    """`/home/<user>` as `~`, another user's home as `~<them>`."""
    if uhome and under(path, uhome):
        return "~" + path[len(uhome.rstrip("/")):]
    if path.startswith("/home/"):
        return "~" + path[6:]
    m = _N_HOME.match(path)
    if m:
        return "~" + path[m.start(1):]
    return path


def _join(d, name):
    return d if name == "." else (d.rstrip("/") + "/" + name)


def _opstr(ops):
    return "".join(c for c, bit in (("r", R), ("w", W), ("d", D), ("n", N)) if ops & bit)


def _io(d):
    """Bytes of the processes that ran there, or None when none was measured."""
    if not d[1]:
        return None
    return {"rchar_mb": round(d[2], 3), "wchar_mb": round(d[3], 3),
            "rd_mb": round(d[4], 3), "wr_mb": round(d[5], 3),
            "coverage_pct": round(100.0 * d[1] / d[0], 1) if d[0] else None}


def _r(e):
    return round(e, 1) if e is not None else None
