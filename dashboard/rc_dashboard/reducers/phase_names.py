"""What a tool call worked on, and what the agent said about it, from its command text.

A stretch of a session is named for the thing it worked on, and best of all in the agent's own
words. Both are in the commands: a script, a Slurm job, a PR number, a commit subject, the title of
an entry the agent appends to its own log. This reads them from one call's text. These are pure
functions of the masked text, no state, for the reducer to call once for each call it keeps
(`tagset`). `names_of` and `own_words` take masked text (`redact.redact`), so what they keep cannot
hold a secret, and `tagset` masks first when it is given the raw text.

It is a heuristic over text and stated as one. On the two captures these rules were tested on,
boslogin06 (FAS RC, schema 7) on 2026-10-09 and rtx6000a (schema 8) on 2026-11-01 and 2026-11-02,
the agent rows hold 22,744 tool calls. Of them 12,541 (55%) have a name, 760 (3%) have the six
that a call keeps, and 278 (1%) have words of the agent's own (2026-11-03). The other counts below
are from an earlier sample, 12,244 tool calls of boslogin06 on 2026-10-09 and rtx6000a on
2026-11-01, which can no longer be rebuilt. On it `tagset` took about 200 us a call, and 160 of
them were `_commands` reading the text.

Names, `(kind, name)` pairs. A call keeps at most MAX_NAMES, in the order they first occur and with
no duplicates, and each is cut at NAME_CHARS, with `…`. The commands are read as `phase_rules`
splits them (`_commands`): a heredoc is data, a `$( )` or `sh -c` string is read as commands, and a
wrapper or an environment assignment is dropped. A program held in a variable is read as the
program it names. `uv run X` is read as `X`, with the heredoc and the redirections it was given:
1,029 of the 12,244 calls hold a `uv run`, and 966 of the 1,185 `python` commands under one run a
script. The line that `sbatch --wrap` gives is read as nested text is, as commands, to a depth of
3.

A file is a word with one of the extensions in `_EXTENSIONS`, and it is shown by its last two path
components, with `.`, `..`, and a bare variable or `~` left out (`./update.sh` is `update.sh`). A
glob, an expansion, a word with a space, and an assignment are not files.

  run      the script an interpreter (`python`, `bash`, `sh`, `Rscript`, `julia`, `node`) was given,
           not code (`-c`, `-e`), a module (`-m`), or stdin, past the options of that interpreter
           that take a value (julia's `-p 4`; bash's `-p` takes none); a program word that is a
           path to a file (`./update.sh`); and an `sbatch` script, which is also a `job` by its
           basename. The script is the first word after sbatch's options, whose values are passed
           over, that is no sentence, glob, or assignment and has a `/` or an extension. The line
           `--wrap` gives is no script: its commands are read for their own names.
  test     the files `pytest`, `python -m pytest`, or `-m unittest` was given, with `::test` cut off
  write    a redirect's target, the files `tee` writes, `sed -i` and `perl -pi` edit, and the
           destination of `cp` and `mv`; and in Python given to `python` as `-c` or a heredoc, the
           file `Path("x").write_text`, `open("x", "w")`, and `open("x", "a")` write. The file may
           be a name that was assigned the string or the `Path` of it (`p = "x"`, then
           `open(p, "w")`). Of the 1,139 writes that Python code makes in the earlier sample, 74
           name the file by a literal, 922 by a name that was assigned one, and 143 by a name that
           was not: 823 of the 936 calls that write with Python get a name, and literals alone
           would name 66.
  read     the files `cat`, `head`, `tail`, `less`, `nl`, `wc`, `sed -n`, `diff`, `grep`, and `rg`
           were given; not the pattern of the last two
  dir      the targets of `mkdir -p` and `git worktree add`, and the destination of `rsync` or
           `cp -r` when it ends in `/`. A brace expansion is cut at its first brace. One of
           Claude Code's own folders is none (below).
  job      `--job-name=X`, `--job-name X`, or `-J X`, on `sbatch`, `srun`, or `salloc`, before
           the script; `--wrap` gives none
  pr       the number given to `gh pr view`, `checks`, `edit`, `merge`, and `diff`, after the
           options of that verb that take a value, as `#235`
  branch   `git push <remote> <branch>` (a refspec `a:b` is `b`, so `HEAD:x` and
           `HEAD:refs/heads/x` are `x`), `git checkout -b`, `git switch -c`, and
           `gh pr create --head`. `HEAD` and `@` name none.
  worktree the slug after `.claude/worktrees/` in any word, but Claude Code's own (below)
  pkg      the packages `pip`, `python -m pip`, `uv pip`, `conda`, and `mamba` were given to
           `install` or `create`, and `uv add`, with the version, the extras, and a channel cut off
           (`gridss=2.13.2` is `gridss`); `module load X`; and the image of `singularity exec` and
           `apptainer exec`, by its basename
  scratch  the basename without its extension of a file written or read, or of an assignment's
           value, under `$SCRATCH`, `$TMPDIR`, a `tmp/` folder, or a `logs/` folder: the names an
           agent gives its own scratch (`$SCRATCH/update_claude_exe.log` is `update_claude_exe`).
           Claude Code's output of a background task, `…/claude-<uid>/…/tasks/<id>.output`, is
           none: Claude Code names it, not the agent, and 229 such ids were scratch names on
           boslogin06 on 2026-10-09
  cd       the last component of a `cd` target, unless the target is one of Claude Code's own
           folders (below)
  grep     the pattern of `grep`, `rg`, and `ugrep`, the first word that is not an option or `-e X`,
           cut at GREP_CHARS. Not with `-v`: that pattern is what a filter drops, as in
           `| grep -v Warning`, not what was looked for.
  word     any other argument of 4 to 40 characters with a letter and a digit or `_`, that is not
           a flag, a path, a number, a URL, or an assignment, at most MAX_WORDS a call: `marinQC51`.
           A word of only hex digits and dashes is dropped: a git hash (`a3844c0`), a key, a UUID.
           A timestamp (`2026-10-09T08:20`), a job step (`51643995.batch`), and the module after
           `python -m` are not words either, and neither is any argument of `cd`, `source`, and
           the other setup commands (`_SETUP`), nor of a shell test (`[[`, `[`, and `test`): its
           operands are a loop's stop condition, not its work, as in `until ...; [[ "$s" =~
           ^(COMPLETED|OUT_OF_ME|NODE_FAIL) ]]`. Where one of the readers above knows that an option
           takes a value, the value is never a word, whether or not another rule names it: the
           partition of `srun -p kempner_h100`, the test of `pytest -k test_io`, and the folder of
           `rsync --exclude __pycache__`. Slurm's queue and accounting commands (`squeue`, `sacct`,
           `sinfo`, `sstat`, `sprio`, `sshare`, and `scontrol`) give no word at all: what they are
           given is the user, the partition, the states, the format, and the jobs they ask about,
           and `squeue -u xq404` put its user in a headline on boslogin06 on 2026-10-09.

A name that holds a mask is no name, and nor is one that holds a `$` (`scripts/$s.py`,
`diag-$name`, `run_$$`, or the `feat-$` that a cut `$(date +%s)` leaves): what the shell fills in
when the command runs is not in its text. A variable that is a whole path component is left out of
the path instead (`$SCRATCH/x.py` is `x.py`). A search pattern is the one kind whose `$` can be
text: after a backslash it is a dollar sign, and one that is a regex's anchor, as in
`(idle|mix)$`, expands nothing, so those patterns are still names. A pattern with a variable or a
substitution in it (`^$jid$`), or a bare `$`, is none.

Claude Code names some things itself, and they name nothing a reader knows. Its id for a
subagent's worktree, `agent-` and hex digits (`agent-a69e2723ecfaa3a16`), is no name of any kind,
nor is a name that ends in one. Its own folders in its temporary folder are no folder made, no
folder changed into, and no scratch name: `claude-<uid>`, the project and the session under it,
and the session's `scratchpad` and `tasks`, as in `/tmp/claude-61002/-n-home02-x/0d1e2f30-…/
scratchpad`, wherever the temporary folder is. Two of them labeled tasks and headlines on
boslogin06 on 2026-10-09, `0d1e2f30-…/scratchpad` and `scratchpad`. What the agent makes inside its
scratchpad is its own, and is named as usual: `scratchpad/q.py`, and `ext` for `$SP/ext`. What
Claude Code keeps in its home is no name of any kind either: `~/.claude/projects`, a project's
folder in it, a session's transcript `<uuid>.jsonl` and the folder `<uuid>/` beside it with all
that is in it, and the project's `memory` folder and its index `MEMORY.md`. On boslogin06 on
2026-10-09 a transcript labeled a task of apid 4193830 (`-n-netscratch-…/3b2a1c0d-….jsonl`) and
`memory/MEMORY.md` one of apid 1103933, and on rtx6000a on 2026-11-01 `memory/MEMORY.md` labeled
one of apid 2559300. The notes the agent writes in `memory` are its own and are named:
`memory/project-nimbus-load-scaling.md`. To tell one of these, a relative path is read in the
folder the call last changed into, and a leading variable the call assigned is read as its value
(`_where`): `MEMORY.md` after `cd ~/.claude/projects/x/memory` is the index.

The agent's own words, `(source, line, ref)`. Only text in the command counts: `git commit -F
msg.txt` has none. A call holds at most one, the first of SOURCES that it has, and the first of
that source in its text. The line is cut at WORDS_CHARS, with `…`, after it is masked. A line with
no letter outside its variables and `$( )`s (`git commit -m "$MSG"`) is no words, nor is one with
the NUL that `_commands` leaves where it lost a heredoc inside a `$( )`.

  commit   `git commit` with its first `-m` or `--message` value (`-am` and `-qm` too, and a value
           attached to its option, `-m"Fix it"`), or with `-F -`, `--file=-`, `--file -`, or `-F-`
           and a heredoc, taking the first line of either that is text. The value of `-m "$(cat
           <<'EOF' ... EOF)"`, Claude Code's own form, is its heredoc's first line: `_commands`
           loses that heredoc, so the text is read for it first (`_heredoc_messages`), with the
           delimiter quoted, escaped, or bare, and `<<-` (2 of the earlier sample's 172 `git
           commit`). A quote that never closes, as in a record the collector cut, runs to the end
           of the line (`_quoted`).
  pr       `gh pr create` and `gh pr edit` with `--title` or `-t` (a quote that never closes runs
           to the end of the line), and `ref`, `#235` for the PR that `edit` was given
  log      a write, a redirect or `tee`, to a Markdown file one of whose stem's parts is `log`,
           `plan`, `notes`, `note`, `handoff`, `progress`, `changelog`, `journal`, `status`,
           `todo`, `requirements`, `coordination`, or `diary`, in any case. The parts are the stem
           split on `_`, `-`, and `.`, and each piece on camel case and digits: `CHAT_LOG.md`,
           `progressNotes.md`, and `plan3.md` are logs, and `methodology.md` and `worklog.md` are
           not. The text is the heredoc of `cat` or `tee` (or of the `cat` that pipes into `tee`)
           or the string of `echo` or `printf`, and the line is its first heading or bold line,
           else its first line with a word in it, with the `#`, the `**`, a leading `- ` (or `* `
           or `> `), and a leading `$(date ...)` taken off. 74 of the earlier sample's 240 calls
           with words are log entries.

What this misreads:

  * A log file's word must be a part of its stem, so one run into another word is missed:
    `worklog.md` and `devnotes.md` are no logs.
  * A Python file written through a name that was not assigned a literal (a loop, `os.path.join`)
    or through an f-string is not named.
  * A command that `srun`, `salloc`, `singularity exec`, or `mpirun` starts is read as the
    launcher's arguments, not as the interpreter and script it runs. `ugrep` has a search term,
    but its files are not read.
  * The value of an option no reader above knows of is read as any other argument, and can be a
    word: `ps -u xq404` gives `xq404` (the naming leaves a session's user out, `phase_tasks`).
  * Only a `cd` earlier in the same call places a relative path, and a `cd` in a subshell is taken
    to last: `cat MEMORY.md` in a call that did not change into the `memory` folder is a name.
  * The agent's words are what a command holds. A commit message or a log entry that the agent
    wrote with its own Edit or Write tool is not in any command.
  * A quote that never closes runs to the end of its line, or to the `;`, `&`, or `|` where
    `_commands` has already cut the command. A `$( )` with a heredoc in it that is only part of
    a value, as in `-m "Fix: $(cat <<'EOF' ...)"`, gives no words.
  * Masking is `redact`'s. A secret with no flag or known shape before it, such as a key of
    letters and digits given as an argument, is not masked and can be kept as a word, as it can be
    seen in a preview. A key of only hex digits and dashes is dropped as a word.
"""
import copy
import os
import re
import shlex

from ..redact import MASK, redact
from .phase_rules import (_GIT_VALUED, _SETUP, _SHELL_VALUE_OPTS, _UV_VALUED, _c_index, _commands,
                          _in_place, _read, _sub)

KINDS = ("run", "test", "write", "read", "dir", "job", "pr", "branch", "worktree", "pkg", "scratch",
         "cd", "grep", "word")
SOURCES = ("pr", "commit", "log")           # in precedence order
MAX_NAMES = 6               # names a call keeps
NAME_CHARS = 80             # characters of a name kept
WORDS_CHARS = 120           # characters of the agent's line kept
MAX_WORDS = 2               # `word` names among them
GREP_CHARS = 40             # characters of a search pattern kept
WORD_LEN = (4, 40)          # the length of a `word`

# ------------------------------------------------------------------ files and their names
_EXTENSIONS = frozenset(
    "py sh bash sbatch slurm md rst tex json jsonl yaml yml toml csv tsv ipynb c cc cpp h hpp rs "
    "go js ts tsx jsx R jl txt cfg ini sql html css j2 parquet".split())
_BRACES = r"\{[^{}]*(?:,|\.\.)"                         # a brace expansion, not a `${X}`
_SUBSTITUTION = r"\$(?![\w{])"                          # a `$( )`, or the bare `$` it leaves
# Not a name: a glob, a brace expansion, a command substitution, a sentence, an assignment.
_NOT_A_NAME = re.compile(r"[\s*?\[\]`=]|" + _SUBSTITUTION + "|" + _BRACES)
_UNNAMED_FOLDER = re.compile(r"[*?`]|" + _SUBSTITUTION)
_BRACE_EXPANSION = re.compile(_BRACES + ".*")
_ANCHOR = re.compile(r"\$\{?\w+\}?|~")                  # a bare variable or `~`: no part of a name
# A shell variable or substitution inside a search pattern: `$s`, `${z}`, `$(…)`, `$1`. A `\$`
# there is a dollar sign, and a `$` before nothing it could expand is an anchor (`(idle|mix)$`).
# A name of any other kind is none when it holds a `$` at all (`_expands`).
_SHELL_VARIABLE = re.compile(r"(?<!\\)\$(?:[A-Za-z_{(]|\d)")
_UNDER_SCRATCH = re.compile(r"^\$\{?(?:SCRATCH|TMPDIR)\}?/|(?:^|/)(?:tmp|logs)/")
# Claude Code's own output of a background task, `/tmp/claude-<uid>/<project>/<session>/tasks/
# <id>.output`, wherever its temporary folder is (`/private/tmp`, `$TMPDIR`, a home's `tmp`): the
# agent does not name it, and its id names nothing a reader knows.
_TASK_OUTPUT = re.compile(r"(?:^|/)claude-\d+/(?:[^/]+/)*tasks/[^/]+\.output$")
# Claude Code's own folders in the same temporary folder: `claude-<uid>`, the project and the
# session under it, and the session's `scratchpad` and `tasks`, as in
# `/tmp/claude-61002/-n-home02-x/0d1e2f30-…/scratchpad`. Claude Code names them, and not the agent,
# so a folder made, a folder changed into, or a scratch name that is one of them is no name. What
# the agent makes inside its scratchpad is its own: `scratchpad/q.py` and `$SP/ext` are names.
_CLAUDE_FOLDER = re.compile(r"(?:^|/)claude-\d+(?:/[^/]+(?:/[^/]+(?:/(?:scratchpad|tasks))?)?)?/*$")
# What Claude Code keeps in its home, `~/.claude/projects`: that folder, a project's folder in it
# (the project's path with `-` for `/`, `-n-home01-x-cbm`), a session's transcript `<uuid>.jsonl`
# and the folder `<uuid>/` beside it with all that Claude Code keeps there (its subagents'
# transcripts and the tool results it saved), and the project's `memory` folder and its index
# `MEMORY.md`. Claude Code names them, and not the agent: `-n-netscratch-…/3b2a1c0d-….jsonl`
# labeled a task of boslogin06's apid 4193830 on 2026-10-09. The notes the agent writes in `memory`
# are its own and are named: `memory/project_mark1_A_depletion.md`. The folders and the files are
# two patterns, so that a file is never taken for a folder of the same depth.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CLAUDE_HOME_FOLDER = re.compile(r"(?:^|/)\.claude/projects(?:/[^/]+(?:/(?:" + _UUID
                                 + r"(?:/.*)?|memory))?)?/*$")
_CLAUDE_HOME_FILE = re.compile(r"(?:^|/)\.claude/projects/[^/]+/(?:" + _UUID
                               + r"(?:\.jsonl|/.+)|memory/MEMORY\.md)$")
# Claude Code's own folders and its own files (`_claude_codes`). The folder patterns match a file
# at their depth too, so a file is told by the file patterns alone: `/tmp/claude-61001/rev/x.py`
# is a file the agent wrote, at the depth of a session's folder.
_CLAUDE_FOLDERS = (_CLAUDE_FOLDER, _CLAUDE_HOME_FOLDER)
_CLAUDE_FILES = (_TASK_OUTPUT, _CLAUDE_HOME_FILE)
# A variable at the start of a path, `$M` or `${M}`, which `_where` reads as the value assigned.
_LEADING_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))(?=/|$)")
# Claude Code's id for a subagent's worktree, `agent-a69e2723ecfaa3a16`: `agent-` and hex digits.
# It names nothing a reader knows, so a name that is one, or ends in one, is none (`_Names`).
_AGENT_SLUG = re.compile(r"agent-[0-9a-f]+")
_NOT_A_STEM = re.compile(r"[\s*?\[\]{}()`$]")


def _cut(text, n):
    """`text` cut to `n` characters, the last of them `…` when something was left out."""
    return text if len(text) <= n else text[:n - 1] + "…"


def _is_file(word):
    return (word[:1] not in ("", "-") and not _NOT_A_NAME.search(word)
            and os.path.splitext(word)[1][1:] in _EXTENSIONS)


def _shown(path):
    """The last two components of a path, without `.`, `..`, or a bare variable or `~`."""
    parts = [p for p in path.split("/") if p not in ("", ".", "..") and not _ANCHOR.fullmatch(p)]
    return "/".join(parts[-2:])


def _folder(path):
    """A folder's name, or "" for none: a brace expansion is cut at its first brace (the outer
    braces of one inside another go with it), and a substitution or a glob names nothing."""
    if _UNNAMED_FOLDER.search(path):
        return ""
    return _shown(_BRACE_EXPANSION.sub("", path).rstrip("{"))


def _scratch_stem(path):
    """The name an agent gave a file under scratch, or "": its basename without the extension."""
    if not _UNDER_SCRATCH.search(path):
        return ""
    stem = os.path.splitext(os.path.basename(path))[0]
    return "" if _NOT_A_STEM.search(stem) else stem


def _where(path, out):
    """`path` where the call put it: a leading variable that the call assigned earlier is read as
    its value, and a relative path is read in the folder the call last changed into. `MEMORY.md`
    after `cd ~/.claude/projects/x/memory` is `~/.claude/projects/x/memory/MEMORY.md`. It is read
    only to tell Claude Code's own folders and files (`_claude_codes`): a name is shown as the
    command gave it."""
    m = _LEADING_VARIABLE.match(path)
    if m and (m.group(1) or m.group(2)) in out.assigned:
        path = out.assigned[m.group(1) or m.group(2)] + path[m.end():]
    if out.cwd and path[:1] not in ("", "/", "~", "$"):
        path = out.cwd + "/" + path
    return os.path.normpath(path) if path else path


def _claude_codes(path, out, patterns):
    """True when `path`, where the call put it (`_where`), is one of `patterns`: Claude Code's own
    folders (`_CLAUDE_FOLDERS`: in its temporary folder, and what it keeps in
    `~/.claude/projects`), or its own files (`_CLAUDE_FILES`: a background task's output, a
    session's transcript and what is kept beside it, and the memory index). Claude Code names
    them, and not the agent, so they get no name."""
    where = _where(path, out)
    return any(p.search(where) for p in patterns)


def _positional(args, valued=frozenset(), taken=None):
    """The arguments that are not options, nor the value of one in `valued`. Those values go into
    the set `taken`, when one is given: a value an option takes is never a `word`."""
    out, i, n = [], 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a[:1] == "-" and len(a) > 1:
            if a in valued and i + 1 < n:
                if taken is not None:
                    taken.add(args[i + 1])
                i += 1
            i += 1
        else:
            out.append(a)
            i += 1
    return out


def _has_flag(args, shorts, long_):
    """True when `long_` is given, or an option cluster has one of `shorts` (`-p`, `-pv`)."""
    return any(a == long_ or (a[:1] == "-" and a[1:2] != "-" and any(c in a[1:] for c in shorts))
               for a in args)


# ------------------------------------------------------------------ what a command runs
def _effective(cmd):
    """The command `uv run` runs: a copy of `cmd` that keeps what it was given (its words, heredoc,
    redirections, pipe, and place) and has the program and arguments of what it runs. Any other
    command comes back as it is."""
    if cmd.name != "uv":
        return cmd
    sub, rest = _sub(cmd.args, _UV_VALUED)
    if sub != "run":
        return cmd
    i = 0
    while i < len(rest) and rest[i][:1] == "-":
        i += 1 + (rest[i] in _UV_VALUED and i + 1 < len(rest))
    if i >= len(rest):
        return cmd
    inner, effective = _read(rest[i:], shell=False), copy.copy(cmd)
    effective.prog, effective.name, effective.args, effective.kw = (
        inner.prog, inner.name, inner.args, inner.kw)
    return effective


# The interpreters, each with the options that give it code to run (`-c`, `-e`; a shell's `-c` is
# `_c_index`'s) and the options that take a value of their own, so that the value is not taken for
# the script. They differ: julia's `-p 4` takes a value and bash's `-p` takes none.
_INTERPRETERS = {
    "python": (frozenset({"-c"}), frozenset({"-W", "-X", "--check-hash-based-pycs"})),
    "bash": (frozenset(), _SHELL_VALUE_OPTS),
    "sh": (frozenset(), _SHELL_VALUE_OPTS),
    "Rscript": (frozenset({"-e"}), frozenset()),
    "julia": (frozenset({"-e", "--eval", "-E", "--print"}),
              frozenset({"-L", "--load", "-t", "--threads", "-p", "--procs", "-J", "--sysimage",
                         "-C", "--cpu-target", "-H", "--home", "--machine-file"})),
    "node": (frozenset({"-e", "--eval", "-p", "--print"}),
             frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
                        "--conditions"})),
}


def _invocation(cmd, taken=None):
    """How an interpreter was run, as `(how, what, rest)`: `("script", the script, its arguments)`,
    `("module", the module, its arguments)` for `python -m`, `("code", the code, [])` for `-c` and
    `-e`, and `("stdin", None, [])` when it was given none of these. The options before them are
    passed over, and the first of these ends the reading: after a script the words are its own.
    The values of the options passed over go into the set `taken`, when one is given."""
    args, n, i = cmd.args, len(cmd.args), 0
    if cmd.name in ("bash", "sh") and _c_index(args) is not None:
        return "code", args[_c_index(args)], []
    code, valued = _INTERPRETERS.get(cmd.name, ((), ()))
    while i < n:
        a = args[i]
        if a in code:
            return "code", args[i + 1] if i + 1 < n else "", []
        if a == "-m" and cmd.name == "python":
            return "module", args[i + 1] if i + 1 < n else "", args[i + 2:]
        if a == "-":
            break
        if a[:1] in ("-", "+") and len(a) > 1:
            if a in valued and i + 1 < n:
                if taken is not None:
                    taken.add(args[i + 1])
                i += 1
            i += 1
        else:
            return "script", a, args[i + 1:]
    return "stdin", None, []


def _run(cmd, out, used):
    """The script an interpreter runs, and a program word that is the path to a file."""
    if cmd.name in _INTERPRETERS:
        how, what, _ = _invocation(cmd, used)
        if how == "script" and _is_file(what):
            out("run", _shown(what))
        if how in ("script", "module"):
            used.add(what)              # a script is named, and a module is a program's name
    elif cmd.prog and "/" in cmd.prog and _is_file(cmd.prog):
        out("run", _shown(cmd.prog))


# The options of sbatch that take a value, from its manual, so that the value is not taken for the
# script: `sbatch -p test x.sbatch` has the script `x.sbatch`, not `test`. The job name (`-J`,
# `--job-name`) and `--wrap` are read on their own (`_SLURM_NAMED`). An option that takes a value
# only as `--nice=5` is no member: `sbatch --nice x.sbatch` has a script.
_SBATCH_VALUED = frozenset(
    "-A --account --acctg-freq -a --array --batch --bb --bbf -b --begin -D --chdir "
    "--cluster-constraint -M --clusters --comment -C --constraint --container --container-id "
    "-S --core-spec --cores-per-socket --cpu-freq --cpus-per-gpu -c --cpus-per-task --deadline "
    "--delay-boot -d --dependency -m --distribution -e --error -x --exclude --export "
    "--export-file -B --extra-node-info --extra --gid --gpu-bind --gpu-freq -G --gpus "
    "--gpus-per-node --gpus-per-socket --gpus-per-task --gres --gres-flags --hint -i --input "
    "--kill-on-invalid-dep -L --licenses --mail-type --mail-user --mcs-label --mem --mem-bind "
    "--mem-per-cpu --mem-per-gpu --mincpus --network -F --nodefile -w --nodelist -N --nodes "
    "-n --ntasks --ntasks-per-core --ntasks-per-gpu --ntasks-per-node --ntasks-per-socket "
    "--open-mode -o --output -p --partition --power --priority --profile -q --qos "
    "--reservation --signal --sockets-per-node --switches --thread-spec --threads-per-core "
    "-t --time --time-min --tmp --tres-per-task --uid --wait-all-nodes --wckey".split())
# srun's are sbatch's and its own. `-W` waits for sbatch and takes a time for srun. `salloc` is
# read with srun's: `salloc -p kempner_h100` gives no word, as `srun -p kempner_h100` gives none.
_SRUN_VALUED = _SBATCH_VALUED | frozenset(
    "--accel-bind --bcast-exclude --cpu-bind --epilog --het-group --jobid --launcher-opts --mpi "
    "--msg-timeout --pack-group --prolog -r --relative --slurmd-debug --task-epilog "
    "--task-prolog -T --threads --tres-bind -W --wait".split())
_SLURM_NAMED = frozenset({"--job-name", "-J", "--wrap"})
# Slurm's queue and accounting commands. What they are given is the user, the partition, the
# states, the format, and the jobs they ask about, which no task is named for: they give no `word`.
_SLURM_QUERIES = frozenset({"squeue", "sacct", "sinfo", "sstat", "sprio", "sshare", "scontrol"})
# A shell test's operands are a loop's stop condition, not its work. `[[ "$s" =~ ^(...) ]]` in a
# loop on `sacct` put two job states, `OUT_OF_ME` and `NODE_FAIL`, in a headline on boslogin06 on
# 2026-10-09. A test gives no `word`.
_SHELL_TESTS = frozenset({"[[", "[", "test"})
_HAS_EXTENSION = re.compile(r"\.[A-Za-z]\w*$")
_MAX_DEPTH = 3                  # `sbatch --wrap` lines read inside one another this deep


def _slurm_line(cmd):
    """The options of an `sbatch`, `srun`, or `salloc` command line, as `(jobs, wrapped, taken,
    rest)`: the job names `-J` and `--job-name` give, the command line `--wrap` gives, the words
    that were the values of options (the partition of `-p kempner_h100` as much as a job name),
    and the arguments after the options. The options end at the first word that is not one: what
    follows an `sbatch` script is the script's own. `salloc` takes srun's options
    (`_SRUN_VALUED`)."""
    valued = _SBATCH_VALUED if cmd.name == "sbatch" else _SRUN_VALUED
    args, n, i = cmd.args, len(cmd.args), 0
    jobs, wrapped, taken = [], "", []
    while i < n and args[i][:1] == "-":
        option, equals, value = args[i].partition("=")
        i += 1
        if not equals and i < n and (option in valued or option in _SLURM_NAMED):
            value, i = args[i], i + 1
            taken.append(value)
        elif option in _SLURM_NAMED and value:
            taken.append(value)
        if option in ("--job-name", "-J") and value:
            jobs.append(value)
        elif option == "--wrap" and value:
            wrapped = value
    return jobs, wrapped, taken, args[i:]


def _slurm(cmd, out, used):
    """The job names of an `sbatch`, `srun`, or `salloc`, and an `sbatch` script, which is a `run`
    and a `job` both. A script is the first word after the options that is no sentence, glob, or
    assignment (`_NOT_A_NAME`) and has a `/` or an extension. `--wrap` gives none: its line is
    read as commands (`_walk`)."""
    jobs, _, taken, rest = _slurm_line(cmd)
    for job in jobs:
        out("job", job)
    used.update(taken)
    script = rest[0] if rest else ""
    if (cmd.name == "sbatch" and script and not _NOT_A_NAME.search(script)
            and ("/" in script or _HAS_EXTENSION.search(script))):
        out("run", _shown(script))
        out("job", os.path.basename(script))
        used.add(script)


def _walk(cmds, depth=0):
    """The commands of a call, each as `_effective` reads it, and after an `sbatch --wrap` the
    commands of the line it wraps, read as nested text is, to a depth of `_MAX_DEPTH`."""
    for cmd in cmds:
        cmd = _effective(cmd)
        yield cmd
        wrapped = _slurm_line(cmd)[1] if cmd.name == "sbatch" else ""
        if wrapped and depth < _MAX_DEPTH:
            yield from _walk(_commands(wrapped), depth + 1)


_PYTEST_VALUED = frozenset({"-c", "-k", "-m", "-n", "-o", "-p", "--ignore", "--ignore-glob",
                            "--deselect", "--rootdir", "--confcutdir", "--junitxml", "--basetemp"})
_UNITTEST_VALUED = frozenset({"-s", "-t", "--start-directory", "--top-level-directory"})


def _tests(cmd, out, used):
    """The files a test runner was given: `pytest`, `python -m pytest`, `python -m unittest`."""
    if cmd.name in ("pytest", "py.test"):
        rest, valued = cmd.args, _PYTEST_VALUED
    else:
        how, module, rest = _invocation(cmd) if cmd.name == "python" else ("", "", [])
        if how != "module" or module not in ("pytest", "unittest"):
            return
        valued = _PYTEST_VALUED if module == "pytest" else _UNITTEST_VALUED
    for a in _positional(rest, valued, used):
        path = a.split("::", 1)[0]
        if _is_file(path):
            out("test", _shown(path))
            used.add(a)


# ------------------------------------------------------------------ what a command writes
# Python that writes a file: `Path("x").write_text(`, `name.write_text(`, and `open("x", "w")`, the
# file a literal or a name. A name is resolved from the last `name = "x"` or `name = Path("x")`
# before the write (`_python_writes`); an f-string, a call, and an attribute are not resolved.
_PY_PATH = (r"(?:pathlib\.)?Path\(\s*(?:(?P<q0>['\"])(?P<lit0>[^'\"\n]+)(?P=q0)"
            r"|(?P<var0>[A-Za-z_]\w*))\s*\)")
_PY_WRITE = re.compile(
    r"(?:" + _PY_PATH + r"|(?<![\w.])(?P<var1>[A-Za-z_]\w*))\s*\.\s*write_(?:text|bytes)\s*\("
    r"|\bopen\(\s*(?:(?P<q2>['\"])(?P<lit2>[^'\"\n]+)(?P=q2)|(?P<var2>[A-Za-z_]\w*)(?![\w.(\[]))"
    r"\s*,\s*(?:mode\s*=\s*)?(?P<q3>['\"])[wa][bt+]*(?P=q3)")
_PY_ASSIGN = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?(['\"])([^'\"\n]+)\2")


def _python_code(cmd):
    """The Python `cmd` was given to run: the string after `-c`, or a heredoc on its stdin. None
    for a script (its heredoc is its stdin) or a module."""
    if cmd.name != "python":
        return None
    how, what, _ = _invocation(cmd)
    return what if how == "code" else cmd.heredoc if how == "stdin" else None


def _python_writes(code):
    """The files Python `code` writes, in the order it writes them."""
    assigned = [(m.start(), m.group(1), m.group(3)) for m in _PY_ASSIGN.finditer(code)]
    out = []
    for m in _PY_WRITE.finditer(code):
        path = m.group("lit0") or m.group("lit2")
        if path is None:
            var = m.group("var0") or m.group("var1") or m.group("var2")
            path = next((v for at, name, v in reversed(assigned) if name == var and at < m.start()),
                        None)
        if path:
            out.append(path)
    return out


_SED_VALUED = frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"})
_SED_SCRIPT = frozenset({"-e", "-f", "--expression", "--file"})
_CP_TARGET = frozenset({"-t", "--target-directory"})


def _sed_files(args, taken=None):
    """The files `sed` was given: what follows its script, which is the first word unless `-e` or
    `-f` gave it. The values of its options go into the set `taken`, when one is given."""
    pos = _positional(args, _SED_VALUED, taken)
    return pos if _SED_SCRIPT & set(args) or any(a.startswith("--expression=") for a in args) \
        else pos[1:]


def _perl_files(args, taken=None):
    """The files `perl -pi` was given: the words that are not options, nor the code after `-e`,
    which goes into the set `taken`, when one is given."""
    out, i, n = [], 0, len(args)
    while i < n:
        a = args[i]
        if a[:1] == "-" and len(a) > 1:
            if re.fullmatch(r"-[A-Za-z]*[eE]", a) is not None and i + 1 < n:
                if taken is not None:
                    taken.add(args[i + 1])
                i += 1
            i += 1
        else:
            out.append(a)
            i += 1
    return out


def _writes(cmd, taken=None):
    """Every file a command writes, named or not: the targets of a redirect, `tee`, `sed -i`,
    `perl -pi`, `cp`, `mv`, and the files that Python code writes. The values of the options read
    on the way go into the set `taken`, when one is given."""
    name, args, out = cmd.name, cmd.args, []
    if name == "tee":
        out = _positional(args)
    elif name == "sed" and _in_place(args, "efl"):
        out = _sed_files(args, taken)
    elif name == "perl" and _in_place(args, "eEMmIFxCdDV"):
        out = _perl_files(args, taken)
    elif name in ("cp", "mv") and not any(
            a in _CP_TARGET or a.startswith("--target-directory=") for a in args):
        pos = _positional(args)
        out = pos[-1:] if len(pos) >= 2 else []         # the destination, given a source
    code = _python_code(cmd)
    return out + cmd.writes + (_python_writes(code) if code else [])


# ------------------------------------------------------------------ what a command reads
_READERS = frozenset({"cat", "head", "tail", "less", "nl", "wc", "diff"})
_SEARCHERS = frozenset({"grep", "rg", "ugrep"})
_SEARCH_VALUED = frozenset({
    "-A", "-B", "-C", "-m", "-d", "-D", "-g", "-t", "-T", "-j", "-M", "--max-count", "--include",
    "--exclude", "--exclude-dir", "--glob", "--iglob", "--type", "--type-not", "--threads",
    "--max-columns", "--max-depth", "--context", "--after-context", "--before-context", "--label"})
_PATTERN_FLAG = re.compile(r"-[A-Za-z]*e")              # `-e`, `-ie`: the pattern follows


def _search(args, values=None):
    """`(pattern, files)` of `grep` or `rg`: the pattern is `-e X`, else the first word that is not
    an option. `rg --files` and `grep -f X` have none. The values of its other options go into the
    set `values`, when one is given."""
    pattern, taken, files, i, n = None, "--files" in args, [], 0, len(args)
    while i < n:
        a = args[i]
        if a.startswith("--regexp="):
            pattern, taken = pattern or a.split("=", 1)[1], True
        elif a in ("--regexp", "--file", "-f") or _PATTERN_FLAG.fullmatch(a):
            if a not in ("--file", "-f") and i + 1 < n and pattern is None:
                pattern = args[i + 1]
            elif i + 1 < n and values is not None:
                values.add(args[i + 1])
            taken = True
            i += 1
        elif a == "--":
            files += args[i + 1:]
            break
        elif a[:1] == "-" and len(a) > 1:
            if a in _SEARCH_VALUED and i + 1 < n:
                if values is not None:
                    values.add(args[i + 1])
                i += 1
        elif taken:
            files.append(a)
        else:
            pattern, taken = a, True
        i += 1
    return pattern, files


def _reads(cmd, taken=None):
    """`(files, pattern)`: the files a command reads, named or not, and what it looked for. The
    values of the options read on the way go into the set `taken`, when one is given."""
    name, args = cmd.name, cmd.args
    if name in _READERS:
        return _positional(args), None
    if name == "sed" and not _in_place(args, "efl") and _has_flag(args, "n", "--quiet"):
        return _sed_files(args, taken), None
    if name in _SEARCHERS:
        pattern, files = _search(args, taken)
        if _has_flag(args, "v", "--invert-match"):
            pattern = None                              # what a filter drops, not what it seeks
        return ([] if name == "ugrep" else files), pattern
    return [], None


# ------------------------------------------------------------------ folders, git, and packages
_RSYNC_VALUED = frozenset({"-e", "--rsh", "--exclude", "--include", "--exclude-from",
                           "--include-from", "--files-from", "--port", "--chmod", "--chown",
                           "--log-file", "--temp-dir", "-T", "--backup-dir", "--link-dest",
                           "--max-size", "--min-size", "--bwlimit"})
# The options of `gh pr <verb>` that take a value, from its manual, so that the value is not taken
# for the number. They differ by verb: `-r` and `-m` take one for `create` and `edit`, and are
# flags for `merge`.
_GH_COMMON = frozenset({"-R", "--repo"})
_GH_VALUED = {
    "create": _GH_COMMON | frozenset(
        "-a --assignee -B --base -b --body -F --body-file -H --head -l --label -m --milestone "
        "-p --project --recover -r --reviewer -T --template -t --title".split()),
    "edit": _GH_COMMON | frozenset(
        "--add-assignee --add-label --add-project --add-reviewer -B --base -b --body "
        "-F --body-file -m --milestone --remove-assignee --remove-label --remove-project "
        "--remove-reviewer -t --title".split()),
    "view": _GH_COMMON | frozenset("-q --jq --json -t --template".split()),
    "checks": _GH_COMMON | frozenset("-i --interval -q --jq --json -t --template".split()),
    "merge": _GH_COMMON | frozenset(
        "-A --author-email -b --body -F --body-file --match-head-commit -t --subject".split()),
    "diff": _GH_COMMON | frozenset("--color -e --exclude".split()),
}
_PR_WITH_NUMBER = frozenset(_GH_VALUED) - {"create"}
_PR_NUMBER = re.compile(r"#?(\d+)")
_PUSH_VALUED = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})


def _folders(cmd, taken=None):
    """The folders a command makes: `mkdir -p`, `git worktree add`, and the destination of `rsync`
    or `cp -r` when it ends in `/`. The values of the options read on the way go into the set
    `taken`, when one is given."""
    name, args = cmd.name, cmd.args
    if name == "mkdir" and _has_flag(args, "p", "--parents"):
        return _positional(args, frozenset({"-m", "--mode"}), taken)
    if name == "git":
        sub, rest = _sub(args, _GIT_VALUED, taken)
        if sub == "worktree" and rest[:1] == ["add"]:
            return _positional(rest[1:], frozenset({"-b", "-B", "--reason"}), taken)[:1]
    elif name == "rsync" or (name == "cp" and (_has_flag(args, "rRa", "--recursive")
                                                or "--archive" in args)):
        pos = _positional(args, _RSYNC_VALUED if name == "rsync" else frozenset(), taken)
        return pos[-1:] if len(pos) >= 2 and pos[-1].endswith("/") else []
    return []


def _after_colon(ref):
    """The branch a refspec names: `HEAD:lep_calib`, `lep-calib:lep_calib`, and
    `HEAD:refs/heads/lep_calib` are `lep_calib`. `HEAD` and `@` are the branch that is checked
    out, not a name: they give "" (`git push -u origin HEAD`)."""
    branch = ref.lstrip("+").rsplit(":", 1)[-1].removeprefix("refs/heads/")
    return "" if branch in ("HEAD", "@") else branch


def _git(cmd, out, used):
    """The branches `git push`, `git checkout -b`, and `git switch -c` name."""
    sub, rest = _sub(cmd.args, _GIT_VALUED, used)
    if sub == "push":
        pos = _positional(rest, _PUSH_VALUED, used)
        if len(pos) >= 2:
            out("branch", _after_colon(pos[1]))
            used.add(pos[1])
    elif sub in ("checkout", "switch"):
        flags = ("-b", "-B") if sub == "checkout" else ("-c", "-C", "--create")
        for a, nxt in zip(rest, rest[1:]):
            if a in flags:
                out("branch", nxt)
                used.add(nxt)
                break


def _gh_pr(cmd, taken=None):
    """`(verb, rest)` of `gh pr <verb> ...`, `rest` being what follows the verb; `("", [])` for any
    other command. The values of the options before the verb go into the set `taken`, when one is
    given."""
    sub, rest = _sub(cmd.args, _GH_COMMON, taken)
    return _sub(rest, _GH_COMMON, taken) if sub == "pr" else ("", [])


def _gh(cmd, out, used):
    """The PR that `gh pr view` and the others were given by number, and the branch that
    `gh pr create` was given with `--head`."""
    verb, rest = _gh_pr(cmd, used)
    pos = _positional(rest, _GH_VALUED[verb], used) if verb in _GH_VALUED else []
    if verb in _PR_WITH_NUMBER:
        m = _PR_NUMBER.fullmatch(pos[0]) if pos else None
        if m:
            out("pr", "#" + m.group(1))
            used.add(pos[0])
    elif verb == "create":
        head = next((a.split("=", 1)[1] for a in rest if a.startswith("--head=")), None)
        if head is None:
            head = next((nxt for a, nxt in zip(rest, rest[1:]) if a in ("--head", "-H")), None)
        if head:
            out("branch", head.rsplit(":", 1)[-1])
            used.add(head)


_PIP_VALUED = frozenset({"-r", "--requirement", "-e", "--editable", "-c", "--constraint", "-i",
                         "--index-url", "--extra-index-url", "-f", "--find-links", "-t", "--target",
                         "--prefix", "--root", "--python", "--no-binary", "--only-binary", "-P",
                         "--upgrade-package"})
_CONDA_VALUED = frozenset({"-n", "--name", "-p", "--prefix", "-c", "--channel", "-f", "--file",
                           "--clone"})
_IMAGE_VALUED = frozenset({"-B", "--bind", "-H", "--home", "--pwd", "-S", "--scratch", "-W",
                           "--workdir", "--env", "--env-file", "-o", "--overlay", "--mount",
                           "--security", "--hostname"})
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PACKAGE_FILE = re.compile(r"\.(?:whl|zip|tgz|gz|egg|txt|toml|yml|yaml)$")


def _package(word):
    """The package a requirement names, `numpy` for `numpy==1.26`, `scipy[extra]>=1`, and
    `conda-forge::gridss=2.13.2`; "" for a path, a URL, or a file."""
    word = word.rsplit("::", 1)[-1]
    m = _PACKAGE.match(word)
    if "/" in word or word[:1] in ("-", ".", "~", "$") or not m or _PACKAGE_FILE.search(m.group()):
        return ""
    return m.group()


def _requirements(cmd, taken=None):
    """The words an install or a create gives its packages in, or [] for any other command. The
    values of the options read on the way go into the set `taken`, when one is given: of any
    `conda` command's, whose options mean the same in each of its subcommands, and of `pip` and
    `uv` only where they install."""
    name, args = cmd.name, cmd.args
    if name == "python":
        how, module, rest = _invocation(cmd)
        if (how, module) == ("module", "pip"):
            name, args = "pip", rest
    if name == "pip":
        sub, rest = _sub(args, _PIP_VALUED, taken)
        return _positional(rest, _PIP_VALUED, taken) if sub == "install" else []
    if name == "uv":
        sub, rest = _sub(args, _UV_VALUED, taken)
        if sub == "pip":
            sub, rest = _sub(rest, _PIP_VALUED, taken)
            sub = "add" if sub == "install" else ""
        return _positional(rest, _PIP_VALUED, taken) if sub == "add" else []
    if name in ("conda", "mamba", "micromamba"):
        sub, rest = _sub(args, _CONDA_VALUED, taken)
        pos = _positional(rest, _CONDA_VALUED, taken)
        return pos if sub in ("install", "create") else []
    return []


def _packages(cmd, out, used):
    """The packages a command installs or loads, and the image it runs."""
    name, (sub, rest) = cmd.name, _sub(cmd.args)
    if name == "module" and sub == "load":
        named = [(a, a) for a in _positional(rest)]
    elif name in ("singularity", "apptainer") and sub == "exec":
        named = [(a, os.path.basename(a)) for a in _positional(rest, _IMAGE_VALUED, used)[:1]]
    else:
        named = [(a, _package(a)) for a in _requirements(cmd, used)]
    for word, pkg in named:
        if pkg:
            out("pkg", pkg)
            used.add(word)


# ------------------------------------------------------------------ the names of one call
_WORKTREE = re.compile(r"\.claude/worktrees/([A-Za-z0-9][A-Za-z0-9_.-]*)(?=/|$)")
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+@%-]*")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d")
_JOB_STEP = re.compile(r"\d+\.(?:batch|extern|interactive)")
_HEX = re.compile(r"[0-9A-Fa-f-]+")                     # a git hash, a key, a UUID
# A number with a letter in it: `1.5e3`, `1e-4`, `0x1F`, a size such as `150G`.
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|0[xX][0-9a-fA-F]+"
                     r"|\d+(?:\.\d+)?[KMGTkmgt][iI]?[bB]?")


def _is_word(arg):
    """A recurring argument: 4 to 40 characters, with a letter and a digit or `_`. Not a flag, a
    path, a URL, a file, an assignment, a number, a timestamp, a job step, or only hex digits and
    dashes (a git hash, a key, a UUID)."""
    return (WORD_LEN[0] <= len(arg) <= WORD_LEN[1] and _WORD.fullmatch(arg) is not None
            and ".." not in arg and not _is_file(arg) and not _NUMBER.fullmatch(arg)
            and not _TIMESTAMP.match(arg) and not _JOB_STEP.fullmatch(arg)
            and not _HEX.fullmatch(arg)
            and any(c.isalpha() for c in arg) and any(c.isdigit() or c == "_" for c in arg))


def _expands(kind, text):
    """True for a name that holds what the shell fills in when the command runs: any `$` at all,
    as in `run_$$` or `feat-$`, but in a search pattern, where only a variable or a substitution
    (`_SHELL_VARIABLE`) or a bare `$` is, and an escaped `$` or an anchor is text."""
    if kind != "grep":
        return "$" in text
    return text == "$" or _SHELL_VARIABLE.search(text) is not None


class _Names:
    """The names of one call, in the order they first occur, without duplicates, and where its
    commands so far have put a path (`_where`): the folder the call last changed into, `cwd`, and
    the values it assigned to variables, `assigned`."""

    def __init__(self):
        self.seen, self.words, self.cwd, self.assigned = {}, 0, "", {}

    def __call__(self, kind, name):
        """Keep a name, folded to one line and cut, unless it is empty, holds a mask or what the
        shell fills in (`_expands`), is a bare variable or `~`, is or ends in a subagent's worktree
        id (`_AGENT_SLUG`), is a duplicate or a third word, or comes past MAX_NAMES."""
        text = " ".join(name.split()) if name else ""
        name = _cut(text, NAME_CHARS)
        if (self.full or not name or MASK in name or _ANCHOR.fullmatch(name)
                or _expands(kind, text) or _AGENT_SLUG.fullmatch(text.rsplit("/", 1)[-1])
                or (kind, name) in self.seen or (kind == "word" and self.words >= MAX_WORDS)):
            return
        self.seen[(kind, name)] = None
        self.words += kind == "word"

    @property
    def full(self):
        return len(self.seen) >= MAX_NAMES


def _file_names(out, kind, path):
    """The names a file gets: what the program made of it, and what it is under scratch. One of
    Claude Code's own files gets neither, and a file in one of its folders no scratch name
    (`_claude_codes`)."""
    if _claude_codes(path, out, _CLAUDE_FILES):
        return
    if _is_file(path):
        out(kind, _shown(path))
    if not _claude_codes(path, out, _CLAUDE_FOLDERS):
        out("scratch", _scratch_stem(path))


def _cd(cmd, out, used):
    """The last component of a `cd` target, unless it is `..`, `-`, `~`, or an expansion, or the
    target is one of Claude Code's own folders (`_claude_codes`). The target is where the call's
    later relative paths are (`_where`)."""
    target = (_positional(cmd.args) or [""])[0]
    last = target.rstrip("/").rsplit("/", 1)[-1]
    if (last not in ("", ".", "..", "-", "~") and not _NOT_A_NAME.search(last)
            and not _claude_codes(target, out, _CLAUDE_FOLDERS)):
        out("cd", last)
        used.add(target)
    if target not in ("", "-"):
        out.cwd = _where(target, out)


def _command_names(cmd, out):
    """Name what one command worked on, in the order of its words as far as the rules allow."""
    name, args, used = cmd.name, cmd.args, set()
    if name == "cd":
        _cd(cmd, out, used)
    _run(cmd, out, used)
    if name in ("sbatch", "srun", "salloc"):
        _slurm(cmd, out, used)
    _tests(cmd, out, used)
    for folder in _folders(cmd, used):
        out("dir", "" if _claude_codes(folder, out, _CLAUDE_FOLDERS) else _folder(folder))
        used.add(folder)
    if name == "git":
        _git(cmd, out, used)
    elif name == "gh":
        _gh(cmd, out, used)
    _packages(cmd, out, used)
    files, pattern = _reads(cmd, used)
    if pattern is not None and any(c.isalnum() for c in pattern):
        out("grep", _cut(pattern.strip(), GREP_CHARS))
        used.add(pattern)
    for path in files:
        _file_names(out, "read", path)
    used.update(files)
    for path in _writes(cmd, used):
        _file_names(out, "write", path)
    for word in cmd.words + cmd.writes:
        for slug in _WORKTREE.findall(word):
            out("worktree", slug)
        if _ASSIGNMENT.match(word):
            variable, value = word.split("=", 1)
            if not _claude_codes(value, out, _CLAUDE_FOLDERS + _CLAUDE_FILES):
                out("scratch", _scratch_stem(value))
            out.assigned[variable] = value
    if name not in _SETUP and name not in _SLURM_QUERIES and name not in _SHELL_TESTS:
        for a in args:
            if a not in used and _is_word(a):
                out("word", a)


def _names(cmds):
    out = _Names()
    for cmd in _walk(cmds):
        _command_names(cmd, out)
        if out.full:
            break
    return tuple(out.seen)


def names_of(masked):
    """At most MAX_NAMES `(kind, name)` pairs for what a call worked on, from its masked command
    text, in the order they first occur and without duplicates."""
    return _names(_commands(masked)) if masked else ()


# ------------------------------------------------------------------ the agent's own words
_LOG_WORDS = frozenset(("log", "plan", "notes", "note", "handoff", "progress", "changelog",
                        "journal", "status", "todo", "requirements", "coordination", "diary"))
_STEM_SPLIT = re.compile(r"[_.-]+")                    # the pieces of a file's stem (`_is_log`)
_STEM_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")    # a piece's parts: `HTTPLog`, `v2`
_HEADING = re.compile(r"^#{1,6}\s+")
_BOLD = re.compile(r"^\*\*[^*\s].*?\*\*")
_MARKER = re.compile(r"^(?:[-*>]\s+)+")                 # a bullet or a quote: `- `, `* `, `> `
_STAMP = re.compile(r"^(?:\$\(date[^)]*\)|`date[^`]*`)\s*")
# The short options of `git commit` that take no value come before the `m` or the `F` that does,
# in one word (`-am`, `-qm`), and the value may be attached (`-m"Fix it"`, `-F-`).
_MESSAGE = re.compile(r"-([aeinopqsvzh]*)m(.*)", re.S)
_FILE_FLAG = re.compile(r"-([aeinopqsvzh]*)F(.*)", re.S)
_REFERENCE = re.compile(r"\$(?:\{[^}]*\}|[A-Za-z_]\w*)")               # `${X}` and `$NAME`
_PRINTF = re.compile(r"%(?:%|[-+ #0-9.]*[sbdiuoxXeEfgGcq])")
# `-m "$(cat <<'EOF' ... EOF)"`, Claude Code's own form for a commit: the option, and the opening
# line of the heredoc, whose delimiter may be quoted, escaped, or bare.
_HEREDOC_MESSAGE = re.compile(
    r"(?<![\w-])(?:-[aeinopqsvzh]*m|--message)(?:=|[ \t]*)"
    r"\"\$\(\s*cat\s*<<-?[ \t]*(?:'(?P<d1>[^'\n]+)'|\"(?P<d2>[^\"\n]+)\"|\\?(?P<d3>\w+))[^\n]*\n")
_HEREDOC_CLOSE = re.compile(r"\s*\)\"")                 # what closes the `$( )` and its quote


def _without_references(line):
    """`line` without its variables (`$NAME`, `${...}`) and its `$( )` substitutions, nested or not.
    A substitution that never closes runs to the end of the line."""
    if "$" not in line:
        return line
    kept, i, n = [], 0, len(line)
    while i < n:
        if line.startswith("$(", i):
            depth, i = 1, i + 2
            while i < n and depth:
                depth += (line[i] == "(") - (line[i] == ")")
                i += 1
        else:
            kept.append(line[i])
            i += 1
    return _REFERENCE.sub("", "".join(kept))


def _is_text(line):
    """True for a line with a letter outside its variables and `$( )`s: words the agent wrote, and
    no reference to words that a command fills in."""
    return any(c.isalpha() for c in _without_references(line))


def _first_line(text):
    """The first line of `text` that is text (`_is_text`), or "". A NUL ends the reading:
    `_commands` leaves one where it took a heredoc out of a `$( )`, and what follows it is the rest
    of that `$( )`."""
    for line in text.split("\n"):
        line = line.strip()
        if "\x00" in line:
            break
        if _is_text(line):
            return line
    return ""


def _quoted(first, rest):
    """The value that begins with the word `first`, `rest` being the words after it. When a quote
    never closed in the line (a record the collector cut), `_commands` could not read the line and
    split it on whitespace, leaving the quote on the first word: the value then runs from after
    that quote to the word that closes it, else to the end of the line. Any other word is the
    value as it stands."""
    quote = first[:1]
    if quote not in ("'", '"') or quote in first[1:] or any(c.isspace() for c in first):
        return first
    words = [first[1:]]
    for word in rest:
        end = word.find(quote)
        words.append(word if end < 0 else word[:end])
        if end >= 0:
            break
    return " ".join(words)


def _heredoc_messages(text):
    """`text` with each `-m "$(cat <<'EOF' ... EOF)"` replaced by `-m 'the heredoc's first line'`:
    `_commands` loses the heredoc of a `$( )` inside quotes, and this is how Claude Code gives a
    commit its message (`--message` and `-am` too). A heredoc that was cut, with no delimiter or
    no `)"`, runs to the end of the text. A `$( )` that is only part of the value is not read.
    `text` itself comes back when there is none."""
    if "<<" not in text or "$(" not in text:
        return text
    parts, pos = [], 0
    for m in _HEREDOC_MESSAGE.finditer(text):
        if m.start() < pos:
            continue                                    # inside a heredoc already read
        delimiter = m.group("d1") or m.group("d2") or m.group("d3")
        lines, end, closed = [], m.end(), False
        while end < len(text) and not closed:
            stop = text.find("\n", end)
            stop = len(text) if stop < 0 else stop
            closed = text[end:stop].strip() == delimiter
            if not closed:
                lines.append(text[end:stop])
            end = min(stop + 1, len(text))
        tail = _HEREDOC_CLOSE.match(text, end) if closed else None
        if closed and not tail and text[end:].strip() not in ("", ")"):
            continue                                    # more of the value follows
        parts += [text[pos:m.start()], "-m ", shlex.quote(_first_line("\n".join(lines)))]
        pos = tail.end() if tail else len(text)
    return "".join(parts) + text[pos:] if parts else text


def _is_log(path):
    """True for a Markdown file one of whose stem's parts is one of `_LOG_WORDS`: the stem split on
    `_`, `-`, and `.`, and each piece on camel case and digits (`CHAT_LOG.md`, `progressNotes.md`,
    `notes.2026-10-09.md`). A part is matched whole, so `methodology.md` is no log."""
    stem, ext = os.path.splitext(os.path.basename(path))
    if ext.lower() != ".md":
        return False
    return any(part.lower() in _LOG_WORDS
               for piece in _STEM_SPLIT.split(stem) for part in _STEM_PART.findall(piece))


def _commit_line(cmd):
    """The subject of a `git commit`: the first line of its first `-m` or `--message`, else of the
    heredoc given with `-F -`; "" for none. The message may be attached to its option, and a quote
    that never closes runs to the end of the line (`_quoted`)."""
    sub, rest = _sub(cmd.args, _GIT_VALUED)
    if sub != "commit":
        return ""
    stdin = False
    for i, a in enumerate(rest):
        tail = rest[i + 1:]
        short, flag_f = _MESSAGE.fullmatch(a), _FILE_FLAG.fullmatch(a)
        if a == "--message" or (short and not short.group(2)):          # the next word
            return _first_line(_quoted(tail[0], tail[1:]) if tail else "")
        if a.startswith("--message=") or short:                         # attached to the option
            value = a.split("=", 1)[1] if a.startswith("--message=") else short.group(2)
            return _first_line(_quoted(value, tail))
        stdin = stdin or a == "--file=-" or (a == "--file" and tail[:1] == ["-"]) or bool(
            flag_f and (flag_f.group(2) == "-" or (not flag_f.group(2) and tail[:1] == ["-"])))
    return _first_line(cmd.heredoc) if stdin and cmd.heredoc is not None else ""


def _pr_title(cmd):
    """`(title, ref)` of `gh pr create` or `gh pr edit` given a title, else None."""
    verb, rest = _gh_pr(cmd)
    if verb not in ("create", "edit"):
        return None
    title = ""
    for i, a in enumerate(rest):
        if a.startswith("--title="):
            title = _quoted(a.split("=", 1)[1], rest[i + 1:])
        elif a in ("--title", "-t") and i + 1 < len(rest):
            title = _quoted(rest[i + 1], rest[i + 2:])
        else:
            continue
        break
    line = _first_line(title)
    pos = _positional(rest, _GH_VALUED[verb])
    ref = "#" + pos[0] if verb == "edit" and pos and pos[0].isdigit() else ""
    return (line, ref) if line else None


def _written_text(cmd):
    """The text a heredoc gives `cat` or `tee`, or an `echo` or `printf` prints; "" for none."""
    args = cmd.args
    if cmd.name in ("cat", "tee") and cmd.heredoc is not None:
        return cmd.heredoc
    if cmd.name == "echo":
        words = list(args)
        while words and re.fullmatch(r"-[neE]+", words[0]):
            words.pop(0)
        return " ".join(words).replace("\\n", "\n")
    if cmd.name == "printf" and args:
        rest = iter(args[1:])
        text = _PRINTF.sub(lambda m: "%" if m.group() == "%%" else next(rest, ""), args[0])
        return text.replace("\\n", "\n")
    return ""


def _clean(line):
    """A log line without its `#` heading mark, its `**`, and a `$(date ...)` it begins with."""
    line = _HEADING.sub("", line).replace("**", "")
    return " ".join(_STAMP.sub("", line).split())


def _entry_title(text):
    """The title of a log entry: its first heading or bold line, else its first line with a word in
    it, without the `#`, the `**`, a leading `- `, and a leading `$(date ...)`."""
    first = ""
    for line in text.split("\n"):
        core = _STAMP.sub("", _MARKER.sub("", line.strip()))
        if "\x00" in core:
            break                                       # a lost heredoc began here (`_first_line`)
        if not _is_text(core):
            continue
        if _HEADING.match(core) or _BOLD.match(core):
            return _clean(core)
        first = first or core
    return _clean(first)


def _own_words(cmds):
    found = {}
    for i, cmd in enumerate(cmds):
        if cmd.name == "git" and "commit" not in found:
            line = _commit_line(cmd)
            if line:
                found["commit"] = (line, "")
        elif cmd.name == "gh" and "pr" not in found:
            title = _pr_title(cmd)
            if title:
                found["pr"] = title
        targets = cmd.writes + (_positional(cmd.args) if cmd.name == "tee" else [])
        if "log" not in found and any(_is_log(t) for t in targets):
            feeder = cmds[i - 1] if i and cmds[i - 1].pipe_to == "tee" else None
            line = _entry_title(_written_text(cmd) or (_written_text(feeder) if feeder else ""))
            if line:
                found["log"] = (line, "")
    for source in SOURCES:
        if source in found:
            line, ref = found[source]
            return source, _cut(line, WORDS_CHARS), ref
    return None


def own_words(masked):
    """`(source, line, ref)` for the agent's own words in a call, from its masked command text, or
    None. `ref` is `#235` for `gh pr edit 235`, else "". The first of SOURCES that the call has."""
    return _own_words(_commands(_heredoc_messages(masked))) if masked else None


def tagset(text, masked=None):
    """`(names_of(m), own_words(m))`, where `m` is `masked`, or `redact(text)` when it is not given.
    `((), None)` for no text."""
    m = masked if masked is not None else redact(text)
    if not m:
        return (), None
    cmds, read = _commands(m), _heredoc_messages(m)
    return _names(cmds), _own_words(cmds if read is m else _commands(read))
