"""What a tool call was, named from the command it ran.

Phases are read from an agent's tool calls, so the first step is to name each call from its
command text alone. These are pure functions, no state, one of CATS out: the reducer, the live
census and the segmenter all read a command through them, so a session's row and its inspector
cannot disagree about what `cd x && make && pytest -x | tail` is. `category_of` and
`category_of_argv` are memoized (command lines repeat: about 4% of a day's records are distinct).

It is a heuristic over text, calibrated on two captures and stated as one. The figures below are
from boslogin06 (FAS RC, schema 7) on 2026-10-28: 375,826 exit records whose argv carries a Claude
Code or Codex shell command. They are records, not calls: a subshell inherits its parent's argv, and
a polling loop leaves one per round.

Where the command is (`shell_text`):

  Claude Code's Bash tool   `zsh -c source <snapshot> && ... && eval '<cmd>' < /dev/null && pwd -P
                            >| /tmp/claude-<id>-cwd`. The command is the `eval` word, unquoted:
                            `'"'"'` or `'\\''` for an inner quote, double quotes, or none at all (a
                            command with no metacharacter is not quoted). The same wrapper with no
                            snapshot path, `bash -c -l shopt -u extglob ... && eval '<cmd>'`, is
                            read alike: 253 k records here against 84 k that name a snapshot.
  Codex                     a `-c` or `-lc` string, as recorded. Options may stand on either side
                            of `-c` (`-lc`, `-c -l`, `-o pipefail -c`), so the string is the first
                            word that is not an option.
  no command                a snapshot script that runs nothing, `bash script.sh`, `bash -l -i`, and
                            an argv recorded only as far as `bash -c -l` (10,593 records).

How a command is read:

  * It is split into simple commands on `;` `&&` `||` `|` `&`, parentheses and newlines. A heredoc
    body is data, never commands: 12% of commands carry one, and in 8% of those a body line starts
    like a command (`python`, `git`, `rm`). A `$( )` inside double quotes (37% of commands hold a
    `$(`), a `sh -c` string (8%) and an `eval` are read as commands too. Backticks are not (0.3%),
    and a quoted `|` standing alone as a word is read as a pipe.
  * Some words look like commands and are not, and a program no table names counts as run (below),
    so they are left out: the patterns of a `case` (`RUNNING|PENDING)`), the inside of `$(( ))`
    and `[[ ]]`, a function's own name and calls to it, and an awk `$(NF-1)`. The command around
    an unquoted `$( )` stays one command: `echo $(date) done` has no command named `done`, and a
    tail glued to the `)` is part of the word before it (`D=$(pwd)/data` is an assignment, not a
    call to `/data`).
  * Env assignments and the wrappers `timeout`, `time`, `nohup`, `env`, `command`, `exec`, `nice`,
    `stdbuf`, `xargs`, `setsid`, `watch`, `sudo`, `ionice`, `taskset`, and `numactl` are dropped
    with their flags and numbers (and `taskset`'s mask). What decides is the program and its
    subcommand, from the tables below.
  * A program held in a variable (`$PY`, `${PYTHON}`, `"$PY"`) is read by the first rule that
    applies. The same text assigned it earlier, bare, after `export`, or quoted
    (`PY=../venv/bin/python; $PY -m pytest`): the first word of the value is the program, so
    `S='sbatch --parsable'` is `sbatch`. (An assignment before a program is that program's
    environment: `PY=x make` sets nothing for the commands after it.) Failing that, its name is
    Python's (`py`, `python3`, `MY_PY`, `VENV_PYTHON`, in any case) or its first argument is (`-m`,
    `-c`, `-`, or a `.py` file), and it is `python`, read by the Python rules below, a heredoc and
    `-c` included. Nothing else is read: `$CMD --flag x` stays `unknown`. On boslogin06 on
    2026-10-28, 3.5% of the records (13,139, the Codex remote wrapper left out) have a command
    whose program is a variable. 10,868 of them hold one read from an assignment, and 1,329 hold
    one that no rule reads. 8,731 records (2.3%) change category: 8,013 into `run`, 550 into
    `edit`, 155 into `test`, and 13 into `git`.
  * A call that holds several commands takes the first of CATS any of them has: a call is named for
    its most consequential step, so `cd x && make && pytest -x | tail` is a test.
  * Setup never names a call: `cd`, `export`, `source`, `module`, an assignment. `cd` leads 20% of
    these commands and a setup command leads 65%, so naming a call by its first command would call
    most of them setup. A call with nothing else in it is `explore`.
  * A `while` or `until` loop that sleeps is a wait, whatever it holds, and so is `watch`: it waits
    its whole duration (25% of commands are such loops). A `for` loop is not: a pause between
    `sbatch`es is not a wait. `waits_on` says what a wait waits on, from its condition. A call that
    begins by sleeping a minute or more (`sleep 590; ...`, past `cd` and assignments, and not sent
    to the background) is a wait too, whatever follows it: `sleep 600; python train.py` reads as
    waiting, on the sleep, and `sleep 120 && squeue -u $USER` as waiting on Slurm.
  * `unknown` is a command that cannot be read: no text, a program held in a variable that no rule
    above reads, or a shell with nothing to run. The Codex remote wrapper, whose command is in
    `$CODEX_REMOTE_PAYLOAD` (59,811 records), is read here as `unknown` too, but as a direct child
    of Codex it is never a call: it is housekeeping, `remote launch` (R51, `classify_direct`). A
    command that can be read is never `unknown`. A program no table names is
    `run`: on a login node a bare name nobody listed (`lmp`, `gmx`, a lab tool) is most often
    something being run, and the explore table holds the utilities that are not (`stty`, `seq`,
    `pdftotext`, `tar`, `zcat`, `pkill`). Across boslogin06, boslogin05, holylogin05 (2026-10-28)
    and rtx6000a (2026-10-29), 0.4% of 472,580 records change category because of it, and they
    are `codex`, `rsync`, `ssh`, `tmux`, and lab tools (`plink2`, `nequip-package`).
  * `python -c` and a Python heredoc are `edit` when the code writes a file, `test` when it imports
    pytest or unittest, else `run`. A script is named by its name (`check_env.sh` is a test,
    `build.sh` a build, `build_dataset.py` is data and runs). `curl` and `wget` build when they
    write a file or pipe into `tar` or a shell, and read otherwise.

An argv is not shell text: the collector joins it with spaces, so `passed|failed` in a `ugrep` argv
is one argument. `category_of_argv` reads a shell through to its command, a wrapper's shell too
(`sudo -n bash -c pytest -q`), and any other program as one command with no shell syntax.

A record cut short (`args_truncated`) is read from what was recorded; these functions are not told
it was cut. Every rule reads text, so a command held in a variable that no rule above reads, a
function defined elsewhere, or a script it ran is only as named as its first word.

Which of an agent's processes are tool calls (`parent_is_agent`, `classify_direct`, `call_text`):

A tool call is one command the model asked for, and whatever runs under it is part of it, so only
an agent's direct children are read. From schema 8 a record names its agent's pid, and a direct
child is a process whose parent that is, unless the process index holds that pid and says it is a
tool call's process: a tool shell that outlived its agent is its own `agent_pid`, and what its loop
runs is its call's. Through schema 7 that pid is the nearest process the classifier tagged, which
is a tool shell for everything the shell ran, so the parent's role is asked of the index, and the
tree root stands in when it lacks it. A direct child is a call, the agent's own housekeeping, or
neither: an agent type outside PHASE_AGENTS has no recognized calls, because a wrong reading is
worse than none. It is read from argv on every schema, since a schema-8 tool shell is untagged and
has no `_role`.

  Claude Code's Bash tool   a shell whose argv names a shell snapshot or, with none, is Claude's own
                            cwd capture (`eval '<cmd>' ... pwd -P >| /tmp/claude-<id>-cwd`):
                            1,747 of 12,414 on boslogin06 have no snapshot. Only a shell is one:
                            a `cat` of a snapshot is housekeeping.
  Claude's Grep and Glob    the agent binary (`claude`, a version name, or `claude.exe`, Claude's
                            binary under that file name, which `roles.py` does not know) run as
                            `rg`, `ugrep`, or `bfs`. On boslogin06 `claude.exe` names
                            `nanoclaw/.tools/bin/claude.exe` in its own argv and is about 4,000
                            direct children. Under a tool shell the same re-exec is part of that
                            shell's call, not a call of its own.
  Codex                     any direct child, or the command under its sandbox wrapper
                            (`codex-linux-sandbox`, `bwrap`). A shell given a command string is a
                            `shell` call and anything else a `bare` one, because Codex runs most of
                            what the model asks for with no shell (`cat AGENTS.md`, `rg`, `git`).
  housekeeping              Claude: every other direct child (hooks, the desktop app's `git`, and
                            its `realpath`), named by its program. Codex: `lsb_release`,
                            `getconf`, a `git` run with `-c safe.bareRepository=explicit`,
                            `sh -c printf "%s" "$HOME"`, the `flock` check, `sh -s`, the agent's own
                            `--version`, `auth`, and `login` probes, the check that sources the
                            shell snapshot it wrote, the remote launcher's login shell and what its
                            profile scripts ran, what the app-server daemon runs when a client
                            connects (`git --version`, `pwd -P`, its `ps -p` and control-socket
                            chores), and the desktop app's reads of its plugins' icons (`cat` of one
                            file in an `assets/` folder under `.codex/.tmp/plugins/`,
                            `.codex/.tmp/bundled-marketplaces/`, or `.codex/plugins/cache/`, and
                            the same `cat` in Codex's process-setup stage; never a sandboxed
                            call). A row is a claim that the model did not ask for it.

The Codex fs helper (`codex --codex-run-as-fs-helper`) is a `bare` call. The sandbox wrappers
above it carry the flag too, since it is the command they wrap, and `is_fs_helper` is false for
them.

What this misreads, in records, from boslogin06 (schema 7) on 2026-10-28 and rtx6000a (schema 8) on
2026-10-29:

  * A Claude shell whose argv names no command cannot be read as a Bash call, so it is housekeeping:
    argv recorded only as far as `bash -c` or `bash -c -l` (800 on boslogin06), or not at all
    (schema 7, before argv arrived: 1,486 more `bash`).
  * A tool shell that outlived its agent (`ppid` 1) is its own `agent_pid` in schema 8. Its loop's
    children are refused once the index holds the shell and read as the agent's own before then:
    7,457 of rtx6000a's 10,573 Claude direct children are the `pgrep` and `sleep` of two such
    shells, and with the index fed the whole day all are refused. Only a tool call's root is: the
    `infra` servers (the Claude Desktop server and Codex's daemon, 2,874 direct children) keep
    their chores, which stay housekeeping by shape.
  * Codex's remote launcher starts a login shell that becomes the runtime, so the shell's own
    subshells and its profile scripts' helpers are direct children of it (`_REMOTE_LAUNCH`,
    `_LOGIN_STARTUP`): 316,197 of rtx6000a's 316,262 Codex calls before they were housekeeping. A
    helper only a user's own profile runs (`loginctl enable-linger`, a `touch` wrapper) is still
    read as a call, and so is a subshell whose argv was not recorded (schema 7: 21,484 `bare`
    calls with no argv on boslogin06, `unknown`).
  * Codex's fs helper is its own agent in schema 7 (`roles.py` tags the `codex` binary, and the
    helper is one), so its writes are its own row's and never a call of the session that asked
    for them: no `codex fs helper` call on boslogin06 on 2026-10-28.
"""
import functools
import math
import os
import re
import shlex
import zlib

from ..procindex import _NO_PARENT
from ..roles import (_AGENT_EXES, _EMBEDDED, _SANDBOX_PREFIX, _SHELLS, _TOOL_SHELL, _VERSION_EXE,
                     role_of)

# In priority order: a call that holds several commands takes the first category any of them has.
CATS = ("test", "slurm", "run", "build", "edit", "git", "wait", "explore", "unknown")

_MAX_TEXT = 100000          # characters of one command read; the longest argv seen was 58 k
_MAX_DEPTH = 3              # `$( )`, `sh -c` and `eval` read inside one another this deep
_LOOKAHEAD = 40             # lines a quoted string may run on, looking for its closing quote

# ------------------------------------------------------------------ the tables
# Programs, by what a call that runs one is. A program no table names is `run`.
_SLURM = ("sbatch srun salloc scancel squeue sacct scontrol sinfo sstat seff sacctmgr sprio sshare "
          "sreport sdiag sattach")
_TEST = ("pytest py.test tox nox ctest ruff flake8 mypy pyright pylint pyflakes pycodestyle tsc "
         "eslint shellcheck")
_RUN = ("ipython pypy Rscript R julia matlab octave torchrun accelerate deepspeed horovodrun "
        "mpirun mpiexec node deno java ruby lua")
_BUILD = ("cmake ninja gcc g++ cc c++ clang clang++ gfortran mpicc mpicxx mpic++ mpif90 mpifort "
          "icc icpc ifort icx icpx ifx nvcc nvc nvc++ nvfortran ld ar ranlib javac rustc "
          "autoreconf configure meson scons latex pdflatex xelatex lualatex luatex latexmk bibtex "
          "dvips dvipng gdown aria2c")
_EDIT = ("cp mv rm rmdir mkdir touch chmod chown chgrp ln patch truncate unlink shred install "
         "sudoedit")
_WAIT = "sleep wait watch pgrep pidof ps inotifywait"
_GIT = "gh"                         # `git` itself is read by its subcommand
_EXPLORE = ("ls ll la dir cat head tail nl less more bat rg grep egrep fgrep zgrep ugrep ag ack "
            "bfs fd find locate tree wc jq yq diff cmp comm stat file du df pwd echo printf which "
            "type whereis whoami id hostname uname date uptime env printenv nice readlink "
            "realpath basename dirname sort uniq cut awk gawk tr column paste join xxd od hexdump "
            "strings md5sum sha256sum cksum lscpu free nproc lsblk nvidia-smi rocm-smi lsb_release "
            "getconf groups quota top htop test [ [[ command read sed "
            # utilities that are not run: seen falling to "no table names it" in the captures
            "stty tput clear seq shuf fold bc expr rev tac sha1sum sha224sum sha384sum sha512sum "
            "b2sum shasum pdftotext pdfinfo pdftoppm pdfimages pdftocairo ffprobe identify "
            "kpsewhich zcat bzcat xzcat tar unzip pkill killall mktemp ss netstat ip lsof findmnt "
            "dig whois nslookup host ping jobstats who w users last lastlog loginctl logger getopt")
# Setup changes the shell or the environment and does nothing a phase could name.
_SETUP = frozenset(
    "cd pushd popd export source . module ml set unset setopt unsetopt shopt alias unalias umask "
    "ulimit trap declare local readonly typeset exit return break continue true false : "
    "let builtin compgen complete compopt bind caller dirs disown enable fc fg bg getopts help "
    "history jobs logout mapfile readarray shift suspend times hash".split())


def _table(*groups):
    """{program: category} from (category, "names ...") pairs; a program in two tables is a bug."""
    out = {}
    for cat, names in groups:
        for name in names.split():
            if name in out or name in _SETUP:
                raise ValueError("phase_rules: %r is in two tables" % name)
            out[name] = cat
    return out


_PROGRAMS = _table(("slurm", _SLURM), ("test", _TEST), ("run", _RUN), ("build", _BUILD),
                   ("edit", _EDIT), ("git", _GIT), ("wait", _WAIT), ("explore", _EXPLORE))
_SLURM_NAMES = frozenset(_SLURM.split())

# Wrappers run the command after them, so that command names the call. Each maps to its options
# that take a separate value (the value is not the command).
_WRAPPERS = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-a", "-s", "-E", "--max-args", "--max-procs",
                        "--max-lines", "--delimiter", "--arg-file", "--max-chars", "--eof"}),
    "watch": frozenset({"-n", "--interval"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-U", "--other-user", "--host", "-p",
                       "--prompt", "-C", "--close-from", "-D", "--chdir", "-R", "--chroot", "-r",
                       "--role", "-t", "--type", "-T", "--command-timeout"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u",
                         "--uid"}),
    "taskset": frozenset(),
    "numactl": frozenset({"-C", "--physcpubind", "-N", "--cpunodebind", "-m", "--membind", "-p",
                          "--preferred", "-P", "--preferred-many", "-i", "--interleave", "-w",
                          "--weighted-interleave"}),
}
# Besides its options, `env` and `sudo` take assignments before the command (`sudo VAR=1 cmd`), and
# `taskset` its mask, the one word before it (`taskset 0x3 cmd`, `taskset -c 0-3 cmd`). With `-p`
# (`taskset -p 0x3 1234`), `ionice -p 1234`, or `numactl --hardware` there is no command after them,
# and `sudo -e` is `sudoedit`: it edits the files after it.
_ASSIGNING = frozenset({"env", "sudo"})
_KEYWORDS = frozenset({"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!",
                       "{", "}", "esac"})
_HEADERS = frozenset({"for", "case", "select", "function"})   # the line is a header, not a command
_INFO = frozenset({"--version", "-V", "--help", "-h"})        # asking, not running

_PROGRAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# A program held in a variable: `$NAME` or `${NAME}`, in double quotes or not. (The words of a line
# `_lex` split have no quotes left; those of a line it could not split, `_lines`, keep theirs.)
# `_ASSIGNED` is `NAME=value`, and not `NAME+=value`, which adds to what the name held.
_VARIABLE = re.compile(r'^("?)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))\1$')
_ASSIGNED = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_PYTHON_NAME = re.compile(r"(?i)^(py|python[0-9.]*|\w*_py|\w*python)$")
_PYTHON_FIRST = frozenset({"-m", "-c", "-"})        # what Python is given first, and a `.py` file
_NUMBER = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_VERSIONED = re.compile(r"^(python|pip|pypy)[0-9.]*$")
_SCRIPT_EXT = re.compile(r"\.(?:sh|bash|zsh|py|pl|rb|jl|js|R|r|m)$")
_DASH_C = re.compile(r"-[A-Za-z]*c$")

# A script's name says what it is: the words of its name, split on anything but a letter or digit.
_TEST_WORDS = frozenset({"test", "tests", "testing", "pytest", "unittest", "check", "checks",
                         "verify", "lint"})
_BUILD_WORDS = frozenset({"build", "install", "setup", "compile", "configure", "bootstrap",
                          "fetch", "download"})

# Python that writes a file: open() with a mode that writes, and the calls that write or delete.
_FILE_MODE = r"['\"][rwxabt+]*[wax+][rwxabt+]*['\"]"
_PY_WRITES = re.compile(
    r"\bopen\s*\((?:[^()]|\([^()]*\))*?(?:,\s*|\bmode\s*=\s*)" + _FILE_MODE +
    r"|\.open\s*\(\s*" + _FILE_MODE +
    r"|\.write_(?:text|bytes)\s*\(|\bjson\.dump\s*\(|\bpickle\.dump\s*\("
    r"|\.to_(?:csv|parquet|excel|pickle|hdf|feather)\s*\("
    r"|\b(?:np|numpy)\.save\w*\s*\(|\btorch\.save\s*\(|\.savefig\s*\("
    r"|\bshutil\.(?:copy\w*|move|rmtree)\s*\("
    r"|\bos\.(?:remove|unlink|rename|replace|makedirs|mkdir|rmdir)\s*\("
    r"|\.(?:mkdir|unlink|touch)\s*\(")
_PY_TESTS = re.compile(r"\b(?:import|from)\s+(?:pytest|unittest)\b")
_PY_TEST_MODULES = frozenset({"pytest", "unittest", "tox", "nox", "nose", "doctest", "mypy", "ruff",
                              "flake8", "pylint", "pyright", "pyflakes", "pycodestyle"})

_FETCH_SINKS = frozenset({"tar", "sh", "bash", "zsh", "dash"})     # `curl ... | tar xz`
_NOT_FILES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "-"})
_FILE_TESTS = frozenset({"-e", "-f", "-s", "-d", "-r", "-w", "-x", "-L", "-h", "-S", "-p"})
_SHELL_VALUE_OPTS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})

# ------------------------------------------------------------------ a shell's command text
_CLAUDE_CWD = re.compile(r"pwd -P >\|\s*/tmp/claude-[0-9a-f]+-cwd")
_EVAL = re.compile(r"\beval\s+(?=\S)")
_WORD = re.compile(r"\S+")


def _first_word(s):
    """The first shell word of `s`, unquoted. When its quote never closes the record was cut short,
    and what it holds is kept."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\n":
            break
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                out.append(s[i + 1:])
                break
            out.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n and s[i + 1] in '"\\$`':
                    i += 1
                out.append(s[i])
                i += 1
            i += 1
        elif c == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _c_index(toks):
    """Index in `toks`, a shell's arguments, of the string `-c` runs, else None. Options may stand
    on either side of `-c` (`-lc`, `-c -l`, `-o pipefail -c`), so it is the first word that is not
    one."""
    seen = skip = False
    for i, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t == "--":
            return i + 1 if seen and i + 1 < len(toks) else None
        if t[:1] in ("-", "+") and len(t) > 1:
            if t[0] == "-" and t[1] != "-" and "c" in t:
                seen = True
            skip = t in _SHELL_VALUE_OPTS
            continue
        return i if seen else None
    return None


def shell_text(command, args):
    """The command line a shell was asked to run, from its argv, or None: it is not a shell, or it
    was not given a command (`bash script.sh`, `bash -l -i`).

    Claude Code's wrapper gives its `eval` word, unquoted; any other shell gives the `-c` or `-lc`
    string after its options, exactly as recorded.
    """
    if not args or not args.strip():
        return None
    if (command or os.path.basename(args.split(None, 1)[0])) not in _SHELLS:
        return None
    if _TOOL_SHELL.search(args) or _CLAUDE_CWD.search(args):
        m = _EVAL.search(args)
        return _first_word(args[m.end():]) if m else None
    toks = list(_WORD.finditer(args))[1:]
    i = _c_index([m.group() for m in toks])
    return None if i is None else args[toks[i].start():]


# ------------------------------------------------------------------ simple commands
_PUNCT = ";&|()<>"
_SEPARATOR = frozenset(";&|()")
_HEREDOC = "__HEREDOC__"
_HEREDOC_OP = re.compile(r"(?<!<)<<(-?)\s*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_HELD = re.compile(r"\x00(\d+)\x00")    # stands for a heredoc operator while its line is read


def _lex(line):
    """`(words, glued)` for one line: its words and operators, and for each whether it follows the
    one before with no space between (the `/data` of `$(pwd)/data`). None when a quote is left
    open."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace_split = True
    lex.commenters = ""                 # `#` is a comment only at a word; `_lines` cuts those
    toks, glued, near = [], [], False
    try:
        while True:
            tok = lex.get_token()
            if tok is None:
                return toks, glued
            toks.append(tok)
            glued.append(near)
            # shlex has read one character past the word: when that was a space the next is apart
            near = line[lex.instream.tell() - 1] not in lex.whitespace
    except ValueError:
        return None


def _is_separator(tok):
    return bool(tok) and all(c in _SEPARATOR for c in tok)


def _is_redirect(tok):
    return ("<" in tok or ">" in tok) and all(c in _PUNCT for c in tok)


def _lines(text):
    """`(tokens, glued, bodies)` for each logical line of `text`: its words and operators, whether
    each follows the one before with no space, and the bodies of the heredocs it opens, which are
    the lines that follow it. A line that ends in a backslash goes on, and so does one with an
    open quote, for up to `_LOOKAHEAD` lines. A text longer than `_MAX_TEXT` is cut at a line, not
    in a word: half a word would read as a program."""
    if len(text) > _MAX_TEXT:
        k = text.rfind("\n", 0, _MAX_TEXT)
        text = text[:k if k > 0 else _MAX_TEXT]
    lines = text.split("\n")
    n, i = len(lines), 0
    while i < n:
        line = lines[i]
        i += 1
        while line.endswith("\\") and not line.endswith("\\\\") and i < n:
            line = line[:-1] + " " + lines[i]
            i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        bodies = []

        def cut(m):                     # a heredoc: its body is the lines up to its terminator
            nonlocal i
            end = next(g for g in m.groups()[1:] if g is not None)
            body = []
            while i < n:
                l = lines[i]
                i += 1
                if (l.lstrip("\t") if m.group(1) else l) == end:
                    break
                body.append(l)
            bodies.append("\n".join(body))
            return " \x00%d\x00 " % (len(bodies) - 1)

        if "<<" in line:
            line = _HEREDOC_OP.sub(cut, line)
        first, resume, lexed, k = line, i, _lex(line), 0
        while lexed is None and i < n and k < _LOOKAHEAD:   # an open quote runs on to the next line
            line += "\n" + lines[i]
            i += 1
            k += 1
            lexed = _lex(line)
        if lexed is None:
            i, toks = resume, first.split()
            lexed = toks, [False] * len(toks)
        yield lexed[0], lexed[1], bodies


_SEGMENT = re.compile(r"\(|\)|[;&|]+")


class _Splitter:
    """Cuts tokens into simple commands. Some words look like a command and are not one, so it
    tracks them: the patterns of a `case` (`RUNNING|PENDING) ...`), the inside of `$(( ))`, the
    parentheses and `|` of a `[[ x =~ ^(a|b)$ ]]`, and a function's name where it is defined. The
    command around an unquoted `$( )` is kept whole: what follows the `)` goes on with it (`echo
    $(date) done` is one command, `A=$(date) make` an assignment and a command), and a word glued
    to the `)` is part of the word before it (`D=$(pwd)/data` is an assignment, not a call to
    `/data`).

    Each command keeps how it was run (R61): sent to the background, by its own `&` or by the
    `&` after the subshell `( )` or group `{ }` that holds it; and inside a `$( )`, whose
    command comes after the one around it."""

    def __init__(self):
        self.out, self.defs = [], set()
        self.cur, self.entry = [], None         # the command being read, and its entry in `out`
        # open parens: (kind, outer command, its entry, the index in `out` where it began)
        self.stack = []
        self.braces = []                        # open `{` groups: the index in `out` of each
        self.group = None                       # a group just closed: where in `out` it began
        self.cases, self.pat, self.test, self.glue = 0, False, False, False

    def _in_sub(self):
        return any(k == "$" for k, _c, _e, _i in self.stack)

    def _arith(self):
        return bool(self.stack) and self.stack[-1][0] == "(("

    def flush(self, piped=False, kind=None, bg=False):
        """End the command being read. An entry in `out` is `[words, piped, kind, bg, sub]`: it
        pipes into the next, a function's name ("def"), sent to the background, and inside a
        `$( )`."""
        if self.cur:
            if self.entry is None:
                self.out.append([self.cur, piped, kind, bg, self._in_sub()])
            else:
                if piped and self.entry is self.out[-1]:
                    self.entry[1] = True        # in `out` already: a `$(` put it there
                self.entry[3] = self.entry[3] or bg
        self.cur, self.entry = [], None

    def _suspend(self, kind):
        """Open a `$(` or `$((` inside the command being read. The command goes into `out` now, so
        that its place is kept, and is taken up again where the parenthesis closes."""
        if self.cur and self.entry is None:
            self.entry = [self.cur, False, None, False, self._in_sub()]
            self.out.append(self.entry)
        self.stack.append((kind, self.cur, self.entry, len(self.out)))
        self.cur, self.entry = [], None

    def word(self, t, glued=False):
        """A word. `glued` means it follows, with no space, the `)` of a `$( )` or `$(( ))`."""
        if self._arith():
            return
        if t == "esac" and self.cases and not self.cur:
            self.cases, self.pat = self.cases - 1, False
            return
        if self.pat:
            return                              # a pattern of a case clause is not a command
        if glued and self.cur:
            self.cur[-1] += t                   # one word with what came before: `$(pwd)/data`
            return
        if t == "in" and len(self.cur) == 2 and self.cur[0] == "case":
            self.cases, self.pat, self.cur = self.cases + 1, True, []
            return
        if self.cur == ["function"]:            # `function name { ... }`: the body is commands
            self.defs.add(t)
            self.cur.append(t)
            self.flush(kind="def")
            return
        if t == "[[" and all(w in _KEYWORDS for w in self.cur):
            self.test = True                    # `[[` where a command starts, `if [[` included
        elif t == "]]":
            self.test = False
        elif t == "{" and all(w in _KEYWORDS for w in self.cur):
            self.braces.append(len(self.out))   # a group opens where a command starts
        elif t == "}" and self.braces and all(w in _KEYWORDS for w in self.cur):
            self.group = self.braces.pop()
        self.cur.append(t)

    def heredoc(self, body):
        if not (self._arith() or self.pat):
            self.cur += [_HEREDOC, body]

    def run(self, tok):
        """A run of `;` `&` `|` `(` `)`: each parenthesis opens or closes, the rest ends a
        command."""
        if self.test:
            return                              # inside `[[ ]]` they are the test's own
        segs = _SEGMENT.findall(tok)
        if segs[:2] == ["(", "("] and not (self.pat or self._arith()):
            self._suspend("((")                 # `$((` or `((`: arithmetic
            self.stack.append(self.stack[-1])
            segs = segs[2:]
        k = 0
        while k < len(segs):
            seg = segs[k]
            if seg == "(":
                if segs[k + 1:k + 2] == [")"] and self._defines():
                    self.defs.add(self.cur[0])
                    self.flush(kind="def")
                    k += 1                      # the `)` of `name()`
                else:
                    self._open()
            elif seg == ")":
                self._close()
            else:
                self._operator(seg)
            k += 1

    def _defines(self):
        return (len(self.cur) == 1 and self.entry is None and _PROGRAM_NAME.match(self.cur[0])
                and not self.pat and not self._arith() and self.cur[0] not in _KEYWORDS | _HEADERS)

    def _open(self):
        if self.pat:
            return                              # the parenthesis of a pattern, `(a|b)`
        if self._arith():
            self.stack.append(("((", [], None, len(self.out)))
        elif self.cur and self.cur[-1].endswith("$"):
            self._suspend("$")
        else:
            self.flush()
            self.group = None
            self.stack.append(("(", [], None, len(self.out)))

    def _close(self):
        if self.pat:
            self.pat = False                    # the `)` that ends a case pattern
            return
        if not self.stack:
            self.flush()
            return
        self.flush()                            # what the parenthesis held: a command, or nothing
        kind, cur, entry, began = self.stack.pop()
        if kind == "(":
            self.group = began                  # a subshell: an `&` after it sends it all
            return
        if kind == "((":
            if self._arith():
                return                          # still inside the arithmetic
            if not cur:                         # `(( i++ ))` is a command
                self.out.append([["let"], False, None, False, self._in_sub()])
        self.cur, self.entry, self.glue = cur, entry, True  # the command around it goes on

    def _operator(self, op):
        if self._arith():
            return
        self.flush(op in ("|", "|&"), bg=op == "&")
        if op == "&" and self.group is not None:
            for e in self.out[self.group:]:     # the subshell or group, and what follows its
                e[3] = True                     # close (`( ... ) > log &`)
        self.group = None
        self.glue = False
        if self.cases and not self.stack and op in (";;", ";&", ";;&"):
            self.pat = True

    def end_line(self):
        while self._arith():
            self.stack.pop()                    # a `((` not closed on its line was not arithmetic
        self.flush()
        self.group = None
        self.test = self.glue = False


def _split(text):
    """`([(words, piped, kind, bg, sub)], defs)`: the simple commands of `text`; whether each pipes
    into the next; `kind` (None, or "def" for a function's name where it is defined); whether it
    was sent to the background (its own `&`, or one after the subshell or group holding it);
    whether it is inside a `$( )`; and the functions the text defines."""
    sp = _Splitter()
    for toks, glued, bodies in _lines(text):
        for t, g in zip(toks, glued):
            near, sp.glue = g and sp.glue, False        # glued to the `)` that closed a `$( )`?
            held = _HELD.fullmatch(t)
            if held and int(held.group(1)) < len(bodies):
                sp.heredoc(bodies[int(held.group(1))])
            elif _is_separator(t):
                sp.run(t)
            else:
                sp.word(t, near)
        sp.end_line()
    return [tuple(e) for e in sp.out], sp.defs


def split_simple(text):
    """The simple commands of a shell text, each a list of its words.

    Split on `;`, `&&`, `||`, `|`, `&`, parentheses and newlines. Quotes protect what they hold. A
    redirection stays in its command as its operator and its target (`2>&1` is `2`, `>&`, `1`). A
    heredoc is the lines up to its terminator, and its command gets the word `__HEREDOC__` and then
    the body as one word: it is data, never read as commands. The patterns of a `case` and the
    inside of `$(( ))` are not commands and are left out. The command around an unquoted `$( )`
    stays whole, and comes before the command inside: `echo $(date) done` is `echo $ done` and
    `date`; a word glued to the `)` joins the word before it (`D=$(pwd)/data` is `D=$/data`). A
    quoted string may run over lines; a line whose quote never closes is split on whitespace.
    """
    return [words for words, *_ in _split(text or "")[0]]


# ------------------------------------------------------------------ reading one command
class _Cmd:
    """One simple command, read: its program and arguments once what runs it is dropped."""
    __slots__ = ("words", "prog", "name", "args", "kw", "writes", "heredoc", "pipe_to", "bg",
                 "sub")

    def __init__(self, words, prog, args, kw, writes, heredoc):
        self.words, self.prog, self.args, self.kw = words, prog, args, kw
        self.writes, self.heredoc, self.pipe_to = writes, heredoc, None
        self.bg = self.sub = False      # sent to the background; inside a `$( )` (`_split`)
        self.name = _canon(os.path.basename(prog)) if prog else None


def _canon(name):
    m = _VERSIONED.match(name)
    return m.group(1) if m else name


def _unwrap(words):
    """`(program, its arguments, the keywords and wrappers dropped before it)`. Environment
    assignments go; a wrapper goes with its flags and numbers (`timeout -s KILL 10m`); a bare
    wrapper (`env`) is the program; and the line of a `for` or `case` has none."""
    kw, i, n, assigned = [], 0, len(words), False
    while i < n:
        w = words[i]
        if _ASSIGN.match(w):
            assigned = True
            i += 1
            continue
        if w in _HEADERS:
            return None, [], tuple(kw) + (w,)
        if w in _KEYWORDS:
            kw.append(w)
            i += 1
            continue
        name = _canon(os.path.basename(w))
        valued = _WRAPPERS.get(name)
        if valued is None or (name == "command" and words[i + 1:i + 2] in (["-v"], ["-V"])):
            break
        kw.append(name)
        i += 1
        mask = name == "taskset"
        while i < n:
            a = words[i]
            if a[:1] == "-" and len(a) > 1:
                if mask and (a == "--pid" or (a[1] != "-" and "p" in a)):
                    i = n                               # a running process's affinity
                    break
                if name == "sudo" and a in ("-e", "--edit"):
                    return "sudoedit", words[i + 1:], tuple(kw[:-1])
                i += 1 + (a in valued and i + 1 < n)
            elif mask:
                mask = False
                i += 1
            elif _NUMBER.match(a) or (name in _ASSIGNING and _ASSIGN.match(a)):
                i += 1
            else:
                break
    if i >= n:
        if kw and kw[-1] in _WRAPPERS:
            return kw[-1], [], tuple(kw[:-1])
        bare = "export" if assigned and not kw else None    # an assignment alone is setup
        return bare, [], tuple(kw)
    return words[i], words[i + 1:], tuple(kw)


def _opaque(prog):
    """True for a program word that names no program: an expansion with no path in it (`$PY`, or
    the `$` that stands for a `$( )`) or a backtick."""
    return prog[:1] in ("$", "`") and "/" not in prog


def _held_program(prog, args, env):
    """The program a program word names when it is held in a variable (`$PY`, `${PYTHON}`,
    `"$PY"`); any other word comes back as it is. `env` is what the text assigned before the
    command (`_assigned`). The first rule that applies reads it:

      1. The text assigned it earlier: the first word of the value is the program.
      2. Its name is Python's (`PY`, `py`, `python3`, `MTB_PY`, `VENV_PYTHON`): `python`.
      3. Its first argument is Python's (`-m`, `-c`, `-`, or a `.py` file): `python`.
      4. Otherwise it stays as it is, and `_classify` reads nothing.

    A value with no program in it (empty, or a `$( )` or another variable) does not apply: the
    next rule reads the word."""
    m = _VARIABLE.match(prog)
    if m is None:
        return prog
    name = m.group(2) or m.group(3)
    held = env.get(name) if env else None
    if held and not _opaque(held):
        return held
    first = args[0] if args else ""
    if _PYTHON_NAME.match(name) or first in _PYTHON_FIRST or first.endswith(".py"):
        return "python"
    return prog


def _read(toks, shell=True, env=None):
    """A `_Cmd` from the words of one simple command. `shell` is False for an argv, which has no
    shell syntax: a `>` in it is a character of an argument, and a `$` is no expansion. `env` is
    what the commands before this one in its text assigned (`_assigned`), for `_held_program`."""
    words, writes, heredoc = [], [], None
    if shell:
        i, n = 0, len(toks)
        while i < n:
            t = toks[i]
            if t == _HEREDOC:
                heredoc = toks[i + 1] if i + 1 < n else ""
                i += 2
            elif t.isdigit() and i + 1 < n and _is_redirect(toks[i + 1]):
                i += 1                  # the `2` of `2>&1` names a descriptor, it is not a word
            elif _is_redirect(t):
                tgt = toks[i + 1] if i + 1 < n else None
                if t in (">", ">>", ">|", "&>", "&>>") and tgt and tgt not in _NOT_FILES \
                        and not tgt.startswith("/dev/fd/"):
                    writes.append(tgt)
                i += 2
            else:
                words.append(t)
                i += 1
    else:
        words = list(toks)
    prog, args, kw = _unwrap(words)
    if shell and prog:
        prog = _held_program(prog, args, env)
    return _Cmd(words, prog, args, kw, writes, heredoc)


def _substitutions(word):
    """The command texts inside the `$( )`s of one word."""
    out, i = [], 0
    while True:
        i = word.find("$(", i)
        if i < 0:
            return out
        j, depth = i + 2, 1
        while j < len(word) and depth:
            depth += (word[j] == "(") - (word[j] == ")")
            j += 1
        if word[i + 2:i + 3] != "(":        # `$((` is arithmetic
            out.append(word[i + 2:j - 1 if depth == 0 else j])
        i = j


def _is_command_text(text):
    """A `$( )` that holds one word is a command only when that word is a path or a table names it:
    `$(date)` is, and `$(NF-1)` in a quoted awk program is a field."""
    words = text.split()
    if len(words) != 1 or "/" in words[0]:
        return True
    name = _canon(words[0])
    return name in _PROGRAMS or name in _RULES or name in _SETUP


def _nested(cmd):
    """Texts of other commands that this one runs: its substitutions, an `eval`, a `sh -c` string.
    (A `$( )` outside quotes was split into commands of its own already; this finds the ones inside
    double quotes.)"""
    out = []
    for w in cmd.words:
        if "$(" in w:
            out += [s for s in _substitutions(w) if _is_command_text(s)]
    if cmd.name == "eval":
        out.append(" ".join(cmd.args))
    elif cmd.name in _SHELLS:
        i = _c_index(cmd.args)
        if i is not None:
            out.append(cmd.args[i])
    return out


def _assigned(cmd):
    """`{NAME: the first word of its value}` for the variables a command sets for the ones after
    it: `NAME=value` standing alone, or after `export`. (`_unwrap` names an assignment alone
    `export`.) An assignment before a program is that program's environment and no more: `PY=x
    make` sets nothing, and `$PY` after it is not `x`."""
    out = {}
    if cmd.name == "export":
        for w in cmd.words:
            m = _ASSIGNED.match(w)
            if m:
                value = m.group(2).split()
                out[m.group(1)] = value[0] if value else ""
    return out


def _commands(text, depth=0, known=frozenset()):
    """Every simple command `text` runs, those nested inside it too. `known` is the functions an
    enclosing text defined: a call to one is not a program. A program held in a variable is read
    from what the text assigned before it (`_held_program`). A nested text is read on its own, with
    none of the assignments of the text around it."""
    parts, defs = _split(text)
    defs = defs | known
    cmds, env = [], {}
    for words, _, kind, bg, sub in parts:
        cmd = _read(words, env=env)
        if kind or cmd.name in defs:        # a function's name where it is defined, or a call
            cmd.prog = cmd.name = None       # to it: not a program
        cmd.bg, cmd.sub = bg, sub
        cmds.append(cmd)
        env.update(_assigned(cmd))
    for (_, piped, *_), cmd, nxt in zip(parts, cmds, cmds[1:] + [None]):
        if piped and nxt is not None:
            cmd.pipe_to = nxt.name
    out = list(cmds)
    if depth < _MAX_DEPTH:
        for cmd in cmds:
            for inner in _nested(cmd):
                out += _commands(inner, depth + 1, defs)
    return out


# ------------------------------------------------------------------ naming one command
def _sub(args, valued=frozenset(), taken=None):
    """The first argument that is not an option, and what follows it. The values of the options in
    `valued` before it go into the set `taken`, when one is given."""
    i, n = 0, len(args)
    while i < n and args[i][:1] == "-":
        if args[i] in valued and i + 1 < n:
            if taken is not None:
                taken.add(args[i + 1])
            i += 1
        i += 1
    return (args[i], args[i + 1:]) if i < n else ("", [])


def _words(name):
    return frozenset(re.split(r"[^A-Za-z0-9]+", name.lower()))


def _script_cat(script, rest=()):
    """A script, named by its name: test words make it a test, build words a build (not for Python,
    whose `build_dataset.py` is data), anything else runs."""
    stem = os.path.basename(script)
    base, ext = os.path.splitext(stem)
    if stem == "setup.py":
        return "test" if rest and rest[0] == "test" else "build"
    w = _words(base)
    if w & _TEST_WORDS:
        return "test"
    if ext != ".py" and w & _BUILD_WORDS:
        return "build"
    return "run"


def _inner(tokens, valued=frozenset()):
    """What a runner (`uv run`, `conda run`, `npx`) runs, named as if it were run alone."""
    i = 0
    while i < len(tokens) and tokens[i][:1] == "-":
        i += 1 + (tokens[i] in valued and i + 1 < len(tokens))
    prog, args, kw = _unwrap(tokens[i:])
    if prog is None:
        return "run"
    cat = _classify(_Cmd(tokens[i:], prog, args, kw, [], None))
    return "explore" if cat in (None, "setup") else cat


def _code_cat(code):
    """Python code given on the command line or in a heredoc: edit when it writes a file."""
    if _PY_WRITES.search(code):
        return "edit"
    return "test" if _PY_TESTS.search(code) else "run"


def _python(args, cmd):
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "-":                                        # the script is on stdin
            break
        if a == "-m":
            mod = args[i + 1] if i + 1 < n else ""
            top = mod.split(".")[0]
            if top in _PY_TEST_MODULES:
                return "test"
            if top == "pip":
                return _pip(args[i + 2:], cmd)
            if top in ("venv", "ensurepip", "build", "virtualenv", "compileall"):
                return "build"
            return "explore" if mod == "json.tool" else "run"
        if _DASH_C.match(a):
            return _code_cat(" ".join(args[i + 1:]))
        if a in ("-W", "-X"):
            i += 2
        elif a[:1] == "-":
            i += 1
        else:
            return _script_cat(a, args[i + 1:])     # a script was named: a heredoc is its stdin
    return _code_cat(cmd.heredoc) if cmd.heredoc is not None else "run"


def _in_place(args, valued):
    """True when an option cluster has `i` before any option that takes a value: `-i`, `-pi`, `-Ei`,
    `-i.bak`. (`-Mstrict` has an `i` too, but `M` took the rest of the word.)"""
    for a in args:
        if a == "--in-place" or a.startswith("--in-place="):
            return True
        if a[:1] == "-" and a[1:2] not in ("-", ""):
            for ch in a[1:]:
                if ch == "i":
                    return True
                if ch in valued:
                    break
    return False


def _sed(args, cmd):
    return "edit" if _in_place(args, "efl") else "explore"


def _perl(args, cmd):
    return "edit" if _in_place(args, "eEMmIFxCdDV") else "run"


def _writer(args, cmd):
    """cat, echo and printf write a file only when redirected to one."""
    return "edit" if cmd.writes else "explore"


def _tee(args, cmd):
    return "edit" if any(a[:1] != "-" for a in args) else "explore"


def _find(args, cmd):
    return "edit" if "-delete" in args else "explore"


def _tail(args, cmd):
    follow = any(a == "--follow" or a.startswith("--follow=") or re.match(r"^-[A-Za-z0-9]*[fF]", a)
                 for a in args)
    return "wait" if follow else "explore"


def _kill(args, cmd):
    return "wait" if "-0" in args else "explore"            # signal 0 only asks whether it is there


def _shell(args, cmd):
    """A shell given a script runs that script. Given `-c` its string is read as commands by
    `_nested`, and given neither it ran nothing that can be read."""
    if _c_index(args) is not None:
        return None
    i = 0
    while i < len(args) and args[i][:1] in ("-", "+"):
        i += 1 + (args[i] in _SHELL_VALUE_OPTS)
    return _script_cat(args[i], args[i + 1:]) if i < len(args) else None


def _fetch(args, cmd):
    """curl and wget fetch to a file with `-o`, `-O`, `--output`, a `>`, or a pipe into `tar` or a
    shell; to `-` or /dev/null they only read."""
    if cmd.writes or cmd.pipe_to in _FETCH_SINKS:
        return "build"
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("--remote-name", "--remote-name-all"):
            return "build"
        if a in ("--output", "--output-document"):
            tgt = nxt
        elif a.startswith(("--output=", "--output-document=")):
            tgt = a.split("=", 1)[1]
        else:
            m = re.match(r"^-([A-Za-z]*)([oO])(.*)$", a)
            if not m:
                continue
            tgt = m.group(3) or nxt
        if tgt not in _NOT_FILES:
            return "build"
    return "explore"


_GIT_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                         "--config-env"})
_GIT_WRITES = frozenset("add commit push pull fetch clone checkout switch merge rebase reset "
                        "restore rm mv cherry-pick revert init am clean gc lfs".split())
# Subcommands that read or write by their own subcommand: (the ones that read, what none gives).
_GIT_BOTH = {"stash": (("list", "show"), "git"), "bisect": (("log", "visualize"), "explore"),
             "worktree": (("list",), "explore"), "submodule": (("status", "summary"), "explore")}


def _git(args, cmd):
    """Saving and syncing is git, reading is explore, and a subcommand no table names reads."""
    sub, rest = _sub(args, _GIT_VALUED)
    pos = [a for a in rest if a[:1] != "-"]
    flags = set(rest)
    if sub == "apply":
        return "edit"
    if sub in _GIT_WRITES:
        return "git"
    if sub in _GIT_BOTH:
        reads, bare = _GIT_BOTH[sub]
        return bare if not pos else "explore" if pos[0] in reads else "git"
    if sub == "branch":
        if {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-u",
                "--set-upstream-to", "--unset-upstream"} & flags:
            return "git"
        listing = {"-l", "--list", "--show-current", "--contains", "--merged",
                   "--no-merged"} & flags
        return "git" if pos and not listing else "explore"
    if sub == "tag":
        if {"-d", "--delete", "-a", "-s", "-f", "-m"} & flags:
            return "git"
        return "git" if pos and not {"-l", "--list"} & flags else "explore"
    if sub == "remote":
        return "git" if pos and pos[0] in ("add", "remove", "rm", "rename", "set-url", "set-head",
                                           "set-branches", "prune", "update") else "explore"
    if sub == "config":
        reading = {"--get", "--get-all", "--get-regexp", "-l", "--list"} & flags
        return "explore" if reading or len(pos) <= 1 else "git"
    return "explore"


def _pip(args, cmd):
    return "build" if _sub(args)[0] in ("install", "uninstall", "download", "wheel") else "explore"


_UV_VALUED = frozenset({"--with", "--with-requirements", "--with-editable", "--python", "-p",
                        "--project", "--directory", "--package", "--extra", "--group", "--env-file",
                        "--index", "--index-url", "--find-links", "-f", "--config-file"})


def _uv(args, cmd):
    sub, rest = _sub(args, _UV_VALUED)
    if sub == "run":
        return _inner(rest, _UV_VALUED)
    if sub == "pip":
        return _pip(rest, cmd)
    if sub in ("sync", "add", "remove", "lock", "venv", "build", "init", "publish"):
        return "build"
    if sub == "tool":
        s2, r2 = _sub(rest)
        if s2 == "run":
            return _inner(r2, _UV_VALUED)
        return "build" if s2 in ("install", "uninstall", "upgrade") else "explore"
    if sub == "python":
        return "build" if _sub(rest)[0] in ("install", "uninstall") else "explore"
    return "explore"


def _uvx(args, cmd):
    return _inner(args, _UV_VALUED)


_CONDA_VALUED = frozenset({"-n", "--name", "-p", "--prefix", "-c", "--channel", "-f", "--file"})


def _conda(args, cmd):
    sub, rest = _sub(args, _CONDA_VALUED)
    if sub in ("activate", "deactivate", "init", "hook") or sub.startswith("shell."):
        return "setup"                              # `eval "$(conda shell.bash hook)"` is setup
    if sub == "run":
        return _inner(rest, _CONDA_VALUED)
    if sub == "env":
        return "build" if _sub(rest, _CONDA_VALUED)[0] in ("create", "update", "remove") \
            else "explore"
    return "build" if sub in ("install", "create", "update", "upgrade", "remove", "uninstall",
                              "clean", "build") else "explore"


def _spack(args, cmd):
    sub, rest = _sub(args)
    if sub in ("load", "unload"):
        return "setup"
    if sub == "env":
        s2 = _sub(rest)[0]
        return "setup" if s2 in ("activate", "deactivate") \
            else "build" if s2 in ("create", "remove") else "explore"
    return "build" if sub in ("install", "uninstall", "add", "remove", "concretize", "develop",
                              "clean", "gc", "mirror", "buildcache", "bootstrap") else "explore"


def _named_script(name):
    """An npm script, named by its words."""
    w = _words(name)
    if not name:
        return "explore"
    return "test" if w & _TEST_WORDS else "build" if w & _BUILD_WORDS else "run"


def _node(args, cmd):
    sub, rest = _sub(args)
    if sub in ("test", "t", "tst"):
        return "test"
    if sub in ("run", "run-script"):
        return _named_script(_sub(rest)[0])
    if sub in ("install", "i", "ci", "add", "update", "upgrade", "up", "uninstall", "remove", "rm",
               "un", "rebuild", "link", "unlink", "prune", "dedupe", "pack", "publish", "build"):
        return "build"
    if sub in ("start", "exec", "x", "dlx", "create", "init"):
        return "run"
    cat = _named_script(sub)                                # `yarn lint`
    return cat if cat in ("test", "build") else "explore"


def _npx(args, cmd):
    return _inner(args, frozenset({"-p", "--package", "-c", "--call"}))


def _cargo(args, cmd):
    sub = _sub(args)[0]
    if sub in ("test", "t", "check", "c", "clippy", "nextest"):
        return "test"
    if sub in ("build", "b", "install", "fetch", "add", "update", "clean", "doc", "vendor",
               "package", "publish", "remove", "rm"):
        return "build"
    return "run" if sub in ("run", "r", "bench") else "explore"


def _go(args, cmd):
    sub = _sub(args)[0]
    if sub in ("test", "vet"):
        return "test"
    if sub in ("build", "install", "get", "mod", "generate", "work", "clean"):
        return "build"
    return "run" if sub == "run" else "explore"


def _make(args, cmd):
    """Plain `make` builds; a target says otherwise (`make test`, `make run`)."""
    targets, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a[:1] == "-":
            skip = a in ("-C", "-f", "-I", "-o", "-W", "--directory", "--file", "--include-dir")
        elif not (_NUMBER.match(a) or "=" in a):
            targets.append(a)
    if any(_words(t) & _TEST_WORDS for t in targets):
        return "test"
    return "run" if "run" in targets else "build"


def _container(args, cmd):
    sub = _sub(args)[0]
    if sub in ("exec", "run", "shell", "instance"):
        return "run"
    return "build" if sub in ("build", "pull", "push") else "explore"


def _hf(args, cmd):
    return "build" if _sub(args)[0] == "download" else "explore"


_RULES = {
    "python": _python, "perl": _perl, "sed": _sed, "cat": _writer, "echo": _writer,
    "printf": _writer, "tee": _tee, "find": _find, "tail": _tail, "kill": _kill, "bash": _shell,
    "sh": _shell, "zsh": _shell, "dash": _shell, "curl": _fetch, "wget": _fetch, "git": _git,
    "pip": _pip, "uv": _uv, "uvx": _uvx, "conda": _conda, "mamba": _conda, "micromamba": _conda,
    "spack": _spack, "npm": _node, "pnpm": _node, "yarn": _node, "bun": _node, "npx": _npx,
    "cargo": _cargo, "go": _go, "make": _make, "gmake": _make, "apptainer": _container,
    "singularity": _container, "hf": _hf, "huggingface-cli": _hf,
}


def _classify(cmd):
    """One command's category; "setup" when it only sets the shell up; None when there is nothing to
    read (no program, a program held in a variable that `_held_program` could not read, or a
    fragment such as `-ge` or `]`)."""
    prog, name, args = cmd.prog, cmd.name, cmd.args
    if not prog or _opaque(prog):
        return None
    if len(args) == 1 and args[0] in _INFO and name not in _SETUP:
        return "explore"                                    # `python --version` runs nothing
    if name in _SETUP:
        return "setup"
    if name == "eval":
        return None                                         # what it runs is read by `_nested`
    rule = _RULES.get(name)
    if rule is not None:
        return rule(args, cmd)
    if name in _PROGRAMS:
        return _PROGRAMS[name]
    if name in _WRAPPERS:
        return "setup"                                      # a wrapper with nothing to run
    if "/" in prog or _SCRIPT_EXT.search(name):
        return _script_cat(prog, args)
    if not _PROGRAM_NAME.match(name):
        return None                                         # `-ge`, `n+1`, `]`: not a program
    return "run"                                            # a program no table names is run


# A call that begins by sleeping this long waits, whatever follows (R58): on boslogin06 on
# 2026-10-28 a session ran `sleep 590; W=...; ... > file` again and again, and the write named each
# call an edit that lasted ten minutes.
LONG_SLEEP_S = 60
_SLEEP_ARG = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
_SLEEP_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _sleep_s(args):
    """How long `sleep ARGS` sleeps, as GNU sleep reads it: each argument a number with an optional
    s, m, h, or d, summed, and `infinity` forever. None when an argument is not one (`$T`)."""
    if not args:
        return None
    total = 0.0
    for a in args:
        if a in ("infinity", "inf"):
            return math.inf
        m = _SLEEP_ARG.match(a)
        if m is None:
            return None
        total += float(m.group(1)) * _SLEEP_UNIT[m.group(2)]
    return total


def _sleeps_first(cmds):
    """True when a call's first command, past `cd` and assignments, sleeps LONG_SLEEP_S or more.

    A command sent to the background never begins the call by sleeping (R61): a sleep in the
    background is not one (`sleep 600 & python train.py`, a `( sleep 3600 && kill -9 $$ ) &`
    watchdog), and a command in the background is still the call's first, so a job launched
    before a pause (`nohup srun ... & sleep 90; tail log`) keeps its call's name. An assignment
    holding a substitution is passed over like any, with the command inside it (`X=$(date +%s);
    sleep 600; ...`)."""
    for c in cmds:
        if c.sub:
            continue
        if c.name in ("cd", "export") and not c.bg:     # an assignment alone is read as `export`
            continue
        if c.bg or c.name != "sleep":
            return False
        s = _sleep_s(c.args)
        return s is not None and s >= LONG_SLEEP_S
    return False


def _category(cmds):
    seen, setup, loop, sleeps, watch = set(), False, False, False, False
    for c in cmds:
        loop = loop or "while" in c.kw or "until" in c.kw
        watch = watch or "watch" in c.kw
        sleeps = sleeps or c.name == "sleep"
        cat = _classify(c)
        if cat == "setup":
            setup = True
        elif cat:
            seen.add(cat)
    if watch or (loop and sleeps) or _sleeps_first(cmds):
        return "wait"                   # it waits its whole duration, whatever else it holds
    for cat in CATS[:-1]:
        if cat in seen:
            return cat
    return "explore" if setup else "unknown"


# Characters of a text the rule caches hold: a longer one is read each time it comes, so each
# cache holds 65,536 texts of 4 KB at most. On boslogin06 a Claude Bash call's argv is 2.6 KB at the
# 99th percentile and 6.8 KB at most (the final review's measure); a text is read only as far as
# _MAX_TEXT in any case.
CACHED_TEXT = 4096


def _cached(fn):
    """`functools.lru_cache(65536)` over `fn`, with a call whose texts pass CACHED_TEXT characters
    read around it. `cache_info` and `cache_clear` are the cache's, and `__wrapped__` is `fn`."""
    cache = functools.lru_cache(maxsize=65536)(fn)

    @functools.wraps(fn)
    def call(*args):
        if sum(len(a) for a in args if isinstance(a, str)) > CACHED_TEXT:
            return fn(*args)
        return cache(*args)

    call.cache_info, call.cache_clear = cache.cache_info, cache.cache_clear
    return call


@_cached
def category_of(text):
    """One of CATS for a shell command text. A call that holds several commands takes the first of
    CATS any of them has; a loop that sleeps is a wait whatever it holds; setup-only commands name a
    call only when nothing else is in it; and a text with no command to read is `unknown`. A text
    is read as far as _MAX_TEXT (100,000 characters), cut at the last line before it (`_lines`).

    Cached: a command line recurs (a poll runs the same one every round) and this runs on millions
    of records a day. It is a pure function of the text, so the cache cannot go stale. A text past
    CACHED_TEXT characters is read around the cache (`_cached`)."""
    if not text or not text.strip() or "CODEX_REMOTE_PAYLOAD" in text:
        return "unknown"
    return _category(_commands(text))


@_cached
def category_of_argv(command, args):
    """One of CATS for a process's own argv: a shell is read through to its command, and any other
    program is named as one command with no shell syntax (the collector joins argv with spaces, so a
    `|` in it is a character of an argument). With no `args` the `command` name is the argv.

    A wrapper's shell is read through too (`sudo -n bash -c pytest -q`, `nohup sh -c ...`).

    Cached on `(command, args)`, like `category_of`, and around the cache past CACHED_TEXT."""
    text = shell_text(command, args)
    if text is not None:
        return category_of(text)
    if "CODEX_REMOTE_PAYLOAD" in (args or ""):
        return "unknown"
    toks = (args or "").split() or ([command] if command else [])
    if not toks:
        return "unknown"
    cmd = _read(toks, shell=False)
    # `watch` waits whatever it runs (`_category`), so its shell is not read through
    if cmd.kw and cmd.name in _SHELLS and "watch" not in cmd.kw:
        at = len(toks) - len(cmd.args) - 1                  # the shell's word, past the wrappers
        text = shell_text(cmd.name, args[list(_WORD.finditer(args))[at].start():])
        if text is not None:
            return category_of(text)
    return _category([cmd])


# ------------------------------------------------------------------ what a wait waits on
_PROCESS = frozenset({"pgrep", "ps", "pidof", "wait"})
_READS = frozenset({"ls", "ll", "grep", "egrep", "fgrep", "zgrep", "rg", "ugrep", "tail", "head",
                    "cat", "wc", "stat", "find"})
_FILE_PROBES = frozenset({"test", "[", "[["})


def waits_on(text, cat):
    """What a call that waits waits on: "slurm", "process", "output" (a file or a log), "sleep"
    (only time), or "command" (it polls something else). The most specific thing the command says:
    a loop is read by its condition, so `until [ -f out ]; do sleep 5; done` waits on output, not on
    time. A call that begins by sleeping a minute or more (R58) waits on what the rest of it names,
    Slurm, a process, or a file or its output, and else on the fixed sleep (R60): `sleep 120 &&
    squeue -u $USER` polls Slurm, and `sleep 600; python train.py` waits on time."""
    if cat == "slurm":
        return "slurm"
    cmds = _commands(text or "")
    first = _sleeps_first(cmds)
    names = {c.name for c in cmds if c.name}
    if names & _SLURM_NAMES:
        return "slurm"
    if names & _PROCESS or any(c.name == "kill" and "-0" in c.args for c in cmds):
        return "process"
    if any(c.name in _READS or (c.name in _FILE_PROBES and _FILE_TESTS & set(c.args))
           for c in cmds):
        return "output"
    if first:
        return "sleep"                  # it begins with the sleep that names it a wait (R58)
    if "sleep" in names and not any("while" in c.kw or "until" in c.kw for c in cmds):
        return "sleep"
    return "command"


def norm_hash(text):
    """A stable hash of a command with its whitespace collapsed; 0 for no text. crc32, not `hash()`:
    it is kept in snapshots, so it must not change between runs."""
    s = " ".join((text or "").split())
    return zlib.crc32(s.encode("utf-8", "replace")) if s else 0


# ------------------------------------------------------------------ which processes are tool calls
PHASE_AGENTS = frozenset({"claude_code", "codex"})          # agent types whose calls are recognized

# The agent's executable by name, as `roles.role_of` reads it, and `claude.exe`, which `roles.py`
# does not know: Claude's binary under that file name. On boslogin06 its own argv names
# `nanoclaw/.tools/bin/claude.exe` (1,637 of its 21,095 records, none with an IDE path), and its
# Grep and Glob re-run it as `ugrep`, `bfs`, and `rg` (about 4,000 direct children). It is added
# here and not there so the fold and the other panels are unchanged.
_AGENT_BINARIES = _AGENT_EXES | {"claude.exe"}
# The roles of a process that is one tool call's, as `roles.py` lists them. What runs under a root
# with one of these is that call's, so the root has no direct children. Any other root runs the
# agent's own chores: the servers that host sessions (`infra`) are not an agent runtime, and their
# `git`, `realpath`, and update checks are housekeeping by shape.
_TOOL_CALL_ROLES = frozenset({"tool-shell", "sandbox", "embedded-tool"})
_FS_HELPER = "--codex-run-as-fs-helper"
_SANDBOX_ARGV0 = frozenset({"bwrap", "codex-linux-sandbox"})     # as `roles.role_of` reads argv[0]
_SAFE_BARE = "-c safe.bareRepository=explicit"                   # on each `git` Codex runs itself
_PRINTF_HOME = re.compile(r"""^printf\s+["']?%s["']?\s+["']?\$HOME["']?\s*$""")
_FLOCK_CHECK = re.compile(r"if\s+command\s+-v\s+flock\b")
# Codex checks the shell snapshot it wrote by sourcing it, `set -e; . "<home>/.codex/..."` (all 39
# such texts on the two days): `source` or `.` as a command of the list, at the text's start or after
# `;`, `&`, `|`, a newline, `(`, or `{`, and not as an argument (`cp -r . ~/.codex/backup`). Nor
# after `if`: a newer Codex sources the snapshot so before the model's own command, `if . <snapshot>
# >/dev/null 2>&1; then :; fi ... exec bash -c '<command>'`, which is a tool call (2 on boslogin06 on
# 2026-10-28).
_CODEX_SOURCED = re.compile(r"""(?:^|[;&|\n({])\s*(?:source|\.)\s+["']?\S*/\.codex/""")
_SH_S = re.compile(r"^\S+\s+-s(?:\s|$)")                         # `sh -s`: a script on stdin
# Codex's remote launcher. Its VS Code extension starts Codex over SSH as a login shell that execs
# the extension's payload, `bash -l -i -c 'CODEX_HOME=...; exec /bin/sh -c "$CODEX_REMOTE_PAYLOAD"'`,
# and the payload is the extension's own: `command -v codex`, `codex --version`, `codex app-server
# proxy`. The shell's pid becomes that probe or that server, the runtime, so what the shell ran
# while it started is a direct child of it: its own subshells, which carry its argv, and the
# helpers of the profile scripts it read. On rtx6000a (schema 8) on 2026-10-29 that was 238,028
# subshells and 78,169 helpers, all of one shape each, across 13,409 launches: 316,197 of the
# day's 316,262 Codex direct children (the `phase_report` census).
_REMOTE_LAUNCH = re.compile(r"\$\{?CODEX_(?:REMOTE_PAYLOAD|INSTALL_DIR)\b")   # and its payload's PATH
# Two more of Codex's own: the sandbox's self-test, a wrapper that runs `true` and nothing else (805
# calls on boslogin06 on 2026-10-28, its `/usr/bin/true` a child of `bwrap`; on rtx6000a in the
# wrapper's own argv, after its last mount: `bwrap ... --ro-bind / / /bin/true`), and the script
# that writes its shell snapshot (29 there, 19 on rtx6000a), which `snapshot check` then sources.
_SANDBOX_PROBE = re.compile(r"(?:\s--|\s/)\s+(?:\S*/)?true$")
_TRUE = re.compile(r"^(?:\S*/)?true$")
_SNAPSHOT_WRITE = re.compile(r"""echo ['"]# Snapshot file['"]""")
# Profile helpers named by program alone, for a record whose argv was not kept (schema 7): nobody
# runs these by hand (1,761 `grepconf.sh` with no argv on boslogin06 that day).
_STARTUP_PROGRAMS = frozenset({"grepconf.sh", "locale-check", "lesspipe", "manpath", "dircolors"})
# The profile scripts' helpers, by their whole argv (whitespace collapsed): Ubuntu's (`manpath`,
# `dircolors -b`, `lesspipe`, `locale`, `locale-check`, the `groups` of bash.bashrc's sudo hint),
# RHEL's (`grepconf.sh -c`, puppet's PATH and MANPATH checks), and nvm's awk programs. No
# argument varies but a locale and nvm's directory, and the model asks for none of them.
_LOGIN_STARTUP = re.compile(
    r"^(?:(?:\S*/)?(?:manpath|locale|groups|dircolors -b|locale-check \S+)"
    r"|(?:\S*/)?(?:ba)?sh (?:\S*/)?(?:lesspipe|grepconf\.sh -c)"
    r"|grep (?:--color=auto )?-q /opt/puppetlabs/(?:bin|puppet/share/man)"
    r"|awk -v NVM_DIR=\S+ -v RS=: .*"
    r'|awk BEGIN \{ (?:for \(name in ENVIRON\) if \(toupper\(name\) == "NPM_CONFIG_PREFIX"\)'
    r'|if \(ARGV\[1\] == "" \|\| ARGV\[2\] == ""\) exit\(1\)).*)$')
# More of a RHEL login shell's, as they ran under boslogin06's 93 open calls on 2026-10-28:
# /etc/profile's and /etc/bashrc's `/usr/bin/id -un` (5,848), `-u` (3,834), and `-gn` (3,647), and
# `/usr/bin/hostname`; profile.d's `flatpak --installations` (4,533); grepconf.sh's own `grep -qsi
# ^COLOR.*none /etc/GREP_COLORS` (11,091); and Lmod's shell check, `/usr/bin/expr /usr/bin/bash :
# -*\(.*\)` (3,846) and `/usr/bin/basename /usr/bin/bash` (3,790). Whole, with the absolute paths
# the scripts give: a model runs `id -un` or `hostname`. Read for what `seen` names
# (`login_helper`, R72), not for what Codex runs itself (`_codex_chore` keeps R51's shapes).
_PROFILE_HELPERS = re.compile(
    r"^(?:/usr/bin/id -(?:u|ru|un|gn)|/usr/bin/hostname|/usr/bin/hostnamectl --transient"
    r"|flatpak --installations|grep -qsi \^COLOR\.\*none /etc/GREP_COLORS"
    r"|/usr/bin/expr \S+ : -\*\\\(\.\*\\\)|/usr/bin/basename /usr/bin/(?:ba)?sh)$")
# The same by name where schema 7 kept no argv: under those calls 3,557 `id` had none, beside
# 13,329 with /etc/profile's. A model's argv-less `id` there is left out of `seen` too, and of
# nothing else.
_PROFILE_PROGRAMS = frozenset({"id", "hostname", "hostnamectl", "flatpak"})
# What Codex's app-server daemon runs for itself when a client connects or it checks a process: on
# rtx6000a on 2026-10-29, at the same seconds as its `printf $HOME`, under the daemons that ran no
# tool call (apids 3150044 and 2591891) and the proxies the launcher started. `git --version` asks
# for git, it does no git work: it is a version check, as `codex --version` is a probe.
_DAEMON_CHORES = (
    ("version check", re.compile(r"^(?:\S*/)?git (?:--version|version)$")),
    ("path check", re.compile(r"^(?:\S*/)?pwd -P$")),
    ("process check", re.compile(r"^(?:\S*/)?ps -p \d+ -o stat= -o lstart=$")),
    ("control socket", re.compile(r"^(?:\S*/)?rm -f -- \S*/\.codex/app-server-control/\S+$")),
)
# The desktop app reads the icons of the plugins it lists with `cat`, hundreds of them in a minute:
# `/bin/cat <home>/.codex/.tmp/plugins/plugins/<name>/assets/<icon>`, or the same under
# `.codex/.tmp/bundled-marketplaces/`, or under `.codex/plugins/cache/` for the plugins a user has
# installed. The model asked for none of them. Only the app's own argv is taken, whole, so a model's
# `rg`, `sed`, or `cat` of a plugin's skill, or its `python3` of a plugin's script, stays a call:
#   `cat <file>` and nothing else, the file in an `assets/` folder under one of those three, and
#   `/proc/self/exe --codex-run-as-process-setup ... /bin/cat /bin/cat <file>`, the same `cat`
#   caught in Codex's process-setup stage, before its exec.
# An argv that names the sandbox is never taken, whatever it ends in. The setup stage of a sandboxed
# call is `exe` in both its command and its argv[0], the two things `_is_sandbox` reads, and names
# the sandbox only further on.
# On boslogin06 on 2026-10-09 there were 592 under the first two folders (575 `cat`, 17 in the setup
# stage) and 103 under the third, all one user's. 541 of the 592 were one session's, 529 of them in
# bursts at 10:09, 14:39, and 16:08. That session's `cat` with no argv (schema 7 kept none for 228)
# cannot be told from the model's own, so it stays a call.
_ASSET_FILE = (r"/(?:\S*/)?\.codex/(?:\.tmp/(?:plugins|bundled-marketplaces)|plugins/cache)/"
               r"(?:\S*/)?assets/\S+")
_APP_ASSETS = re.compile(
    r"^(?:(?:\S*/)?cat|/proc/self/exe --codex-run-as-process-setup (?:\S+ )+?/bin/cat /bin/cat) "
    + _ASSET_FILE + "$")
_SANDBOXED = re.compile(r"codex-linux-sandbox|bwrap")


def _argv(rec):
    """`(command, args, argv0)` of a record: the program's name as the kernel gave it, its argv as
    recorded, and the basename of argv[0]. They differ when a script runs under its interpreter
    (`lsb_release` under `/bin/sh`) and when a binary is re-run under another name (Claude as
    `ugrep`, Codex as `codex-linux-sandbox`). The name falls back to argv[0] when none was kept."""
    args = rec.get("args") or ""
    first = _WORD.search(args)
    argv0 = os.path.basename(first.group()) if first else ""
    return rec.get("_command") or rec.get("command") or rec.get("comm") or argv0, args, argv0


def _is_sandbox(command, argv0):
    """The per-call confinement wrapper, by the rule `roles.role_of` uses for `sandbox`."""
    return command.startswith(_SANDBOX_PREFIX) or argv0 in _SANDBOX_ARGV0


def login_helper(command, args):
    """Whether a process is a login shell's startup helper, which a call runs though the model asked
    for no such thing: by its whole argv (R51's `_LOGIN_STARTUP`, and `_PROFILE_HELPERS`), or by
    name where schema 7 kept no argv of its own (`_STARTUP_PROGRAMS`, `_PROFILE_PROGRAMS`). A
    pending entry keeps it in its program list, as loop detection reads that, and counts it apart
    (P_HELP), so `seen` leaves it out (R72).

    Schema 7 can keep the parent's argv in a short process's place: under 2522045's open call on
    boslogin06 on 2026-10-28, 17 `grepconf.sh` came with the call's own `bash -c squeue ...`. So
    R51's names, which nobody runs by hand, are helpers whatever argv came with them, and an `id`,
    `hostname`, or `flatpak` is one when the argv is not its own (none, or another program's)."""
    name = os.path.basename(command or "")
    if name in _STARTUP_PROGRAMS:
        return True
    flat = " ".join((args or "").split())
    if name in _PROFILE_PROGRAMS and os.path.basename(flat.split(" ", 1)[0]) != name:
        return True
    return bool(flat) and bool(_LOGIN_STARTUP.match(flat) or _PROFILE_HELPERS.match(flat))


def program_name(command):
    """A program as `seen` names it: its basename, and `claude` for Claude's native binary,
    installed as its version (`…/claude/versions/2.1.311`, so the kernel's comm is `2.1.311`:
    `roles._VERSION_EXE`). None for none. The program list itself keeps the basename (R72)."""
    name = os.path.basename(command or "")
    if not name:
        return None
    return "claude" if _VERSION_EXE.match(name) else name


def parent_is_agent(rec, procs):
    """True when the process's parent is its agent runtime, so it is one of the agent's direct
    children: a tool call or the agent's own housekeeping. Whatever runs under a child is that
    child's. `procs` is a `ProcIndex` or None, and only `procs.get(host, pid)` is used, which gives
    `(ppid, role, ...)` or None.

    From schema 8 a record carries its agent's pid (`agent_pid`) whatever its depth, so a direct
    child is the one whose parent that is (the runtime, whose pid that is, is not its own child).
    But `agent_pid` is not always a runtime: a tool shell that outlived its agent is its own, and
    what its loop runs is that shell's call's, not the agent's. So when the index has `agent_pid`
    and its role is a tool call's (`tool-shell`, `sandbox`, `embedded-tool`), nothing is a direct
    child of it. Any other root keeps the rule, the servers that host sessions (`infra`) included,
    since what they run is the agent's own chores. With no entry the record's word stands.

    Through schema 7 `agent_pid` is the nearest process the classifier tagged, which is a tool shell
    for everything it runs, so it cannot say. The index is asked what the parent is, and when it
    has no entry the tree root stands in: it is the agent unless the agent is nested under another,
    or the parent is a tool shell that outlived its agent and became its own root."""
    ppid = rec.get("ppid")
    if ppid in _NO_PARENT:
        return False
    if (rec.get("_sv") or 0) >= 8:
        agent_pid = rec.get("agent_pid")
        if ppid != agent_pid or rec.get("pid") == agent_pid:
            return False
        held = procs.get(rec.get("host"), agent_pid) if procs is not None else None
        return held is None or held[1] not in _TOOL_CALL_ROLES
    held = procs.get(rec.get("host"), ppid) if procs is not None else None
    if held is not None:
        return held[1] == "agent"
    return ppid == rec.get("tree_root_pid")


def _codex_chore(command, args, text):
    """The shape of a command Codex runs for itself, not for the model, or None. `text` is the shell
    string of `command`, if it is a shell given one. A shape is counted in the housekeeping lane and
    never drawn in a phase, so add one only for a command the model did not ask for."""
    name = os.path.basename(command)
    if name in ("lsb_release", "getconf"):
        return name
    flat = " ".join(args.split())
    if name == "git":
        if _SAFE_BARE in args:
            return "git (codex)"
        return "version check" if _DAEMON_CHORES[0][1].match(flat) else None
    if name == "sh":
        if _SH_S.match(args):
            return "sh -s"
        if text is not None and _PRINTF_HOME.match(text):
            return "printf $HOME"
        if text is not None and _FLOCK_CHECK.match(text):
            return "flock check"
    if text is not None:
        if _REMOTE_LAUNCH.search(text):
            return "remote launch"
        if _SNAPSHOT_WRITE.search(text):
            return "snapshot write"
        return "snapshot check" if _CODEX_SOURCED.search(text) else None
    first = _WORD.search(args)
    sandbox = _is_sandbox(command, os.path.basename(first.group()) if first else "")
    if sandbox and _SANDBOX_PROBE.search(flat):
        return "sandbox probe"
    if _LOGIN_STARTUP.match(flat) or (not flat and name in _STARTUP_PROGRAMS):
        return "login shell startup"
    for shape, pattern in _DAEMON_CHORES[1:]:
        if pattern.match(flat):
            return shape
    if _APP_ASSETS.match(flat) and not (sandbox or _SANDBOXED.search(flat)):
        return "app assets"                             # never a sandboxed call
    return "probe" if role_of("codex", command, args) == "probe" else None


def is_sandbox_probe(payload):
    """True when what a sandbox wrapper ran, by its argv, is `true` alone: Codex testing its
    sandbox, not a call."""
    return bool(payload) and bool(_TRUE.match(" ".join(payload.split())))


def codex_chore_of(argv):
    """The shape of a Codex chore known only by its argv, as what ran under a process that never
    exited is (`PendingJoin`'s P_TEXT), or None. Its program is argv[0]'s name."""
    if not argv:
        return None
    words = argv.split()
    command = os.path.basename(_first_word(argv.strip()))
    if command in _SHELLS and len(words) > 1 and not words[1].startswith("-"):
        command = os.path.basename(words[1])        # a script under its shell: `/bin/sh .../lsb_release`
    return _codex_chore(command, argv, shell_text(command, argv))


def _no_command(command, args):
    """True for a shell whose argv was not kept (`""`), or was kept only as far as the shell's
    own options (`/bin/bash -c`, `/bin/bash -c -l`, `zsh -o pipefail -c`): a shell that names no
    command it was given, and no script."""
    if command not in _SHELLS:
        return False
    words = args.split()
    if not words:
        return True
    if os.path.basename(words[0]) not in _SHELLS:
        return False
    skip = False
    for w in words[1:]:
        if skip:
            skip = False
        elif w[:1] in ("-", "+") and len(w) > 1:
            skip = w in _SHELL_VALUE_OPTS
        else:
            return False
    return True


def classify_direct(rec, agent_type):
    """What a direct child of an agent is: `("call", kind)`, `("housekeeping", shape)`, or
    `("none", "")` for an `agent_type` outside PHASE_AGENTS, whose calls are not recognized.

    The kinds of call:
      bash      Claude Code's Bash tool: a shell whose argv names a shell snapshot or, with none,
                is Claude's own cwd capture (`eval '<cmd>' ... pwd -P >| /tmp/claude-<id>-cwd`)
      search    Claude's Grep or Glob: its binary re-run as `rg`, `ugrep`, or `bfs`
      wrapper   the sandbox that confines a call (`codex-linux-sandbox`, `bwrap`); the call is what
                runs under it, so its text is that command's
      shell     Codex: a shell given a command string
      bare      Codex: any other command, run with no shell

    Every other direct child of Claude is housekeeping, named by its program (hooks, the desktop
    app's `git`, and its `realpath`), except a shell whose argv was not kept, or was kept only as
    far as its own options (`_no_command`): schema 7 can record a tool shell's exit before its argv
    (spec finding 7), and the spec's Unknown row is such a call, which still ran and took time
    (R64). On boslogin06 on 2026-10-28 that adds 2,038 calls: the direct children of a Claude
    runtime that are such shells. (The final review counted 19,858 with no argv and 1,726 cut at
    their options, over records under their `agent_pid`, which schema 7 sets to the nearest tagged
    process: they included shells that tool shells started.) A shell that kept a word past its
    options (a hook, `bash hooks/x.sh`) stays housekeeping. Codex's own commands are tested first
    (`_codex_chore`), and what is left is the model's: Codex runs most of what the model asks for
    without a shell, and one with no argv is already an unknown `bare` call.

    Reads argv, so it does not depend on the record's `_role`, which a schema-8 tool shell lacks."""
    if agent_type not in PHASE_AGENTS:
        return ("none", "")
    command, args, argv0 = _argv(rec)
    if agent_type == "claude_code":
        if command in _SHELLS and (_TOOL_SHELL.search(args) or _CLAUDE_CWD.search(args)):
            return ("call", "bash")
        if _no_command(command, args):
            return ("call", "bash")             # no text: `call_text` gives None, read unknown
        if (command in _AGENT_BINARIES or _VERSION_EXE.match(command)) and argv0 in _EMBEDDED:
            return ("call", "search")            # the agent binary re-run as its bundled search
        if _is_sandbox(command, argv0):
            return ("call", "wrapper")
        return ("housekeeping", os.path.basename(command) or "unnamed")
    text = shell_text(command, args)
    chore = _codex_chore(command, args, text)
    if chore:
        return ("housekeeping", chore)
    if _is_sandbox(command, argv0):
        return ("call", "wrapper")
    return ("call", "bare" if text is None else "shell")


def is_fs_helper(rec):
    """True for the process Codex re-runs as its file-writing helper: `codex
    --codex-run-as-fs-helper`. The sandbox wrappers above it carry the flag too, since it is the
    command they wrap and so is in their argv, and they are not the helper."""
    command, args, argv0 = _argv(rec)
    return _FS_HELPER in args and not _is_sandbox(command, argv0)


def call_text(rec, kind):
    """The command a call ran, from the record's own argv, or None: the eval body of Claude's Bash
    tool (`bash`), a shell's command string (`shell`), or the whole argv (`search`, `bare`). A
    `wrapper` has none of its own, its text is its payload's, and neither has a record that kept
    no command: an empty argv, a snapshot script that runs nothing, or a quote cut off."""
    command, args, _ = _argv(rec)
    if kind in ("bash", "shell"):
        text = shell_text(command, args)
    elif kind in ("search", "bare"):
        text = args
    else:
        return None
    return (text.strip() or None) if text else None
