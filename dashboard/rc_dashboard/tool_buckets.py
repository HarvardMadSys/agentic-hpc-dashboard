"""VENDORED from rc_measurement/analyze/tool_bucket_lib.py (sha256 f20658c5add28e25...).

The dashboard used to import this from an `analyze/` directory beside it. That
only existed when the dashboard lived inside the research repo; standalone, the
import failed and every tool fell back to the bucket `other` -- the tool mix
read "100% other" for every class, and the login-node-compute panel, which
selects by bucket, was empty. So the one file is copied here, verbatim except
that `agent_lib` stays optional (no sys.path edit; `shellenv_sub` degrades to
"shell (other)" without it). tests/test_tool_buckets.py checks it against the
original whenever the original is on this machine, so the two cannot drift.
"""
"""Tool-call bucketing: an `effective_comm` token -> one of 13 buckets.

Extracted from analyze/tool_calls_distribution.py so it can be imported without
numpy, pyarrow or matplotlib -- that module pulls all three at import time, which
puts it out of reach of a collector-side or service-side consumer. The figure
script now imports from here, so the dashboard's numbers and the published
figure's cannot drift.

Vocabulary and priority order are VERBATIM from the original; the only change is
that `SHELLS` is resolved locally when agent_lib is unavailable.
"""
import os
import re
import sys

try:
    import agent_lib as L              # present only beside the research repo's analyze/
    SHELL_COMMS = L.SHELL_COMMS
except Exception:                      # stdlib-only fallback
    L = None
    SHELL_COMMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"})

BUCKETS = ("poll/probe", "slurm-submit", "slurm-query", "git", "runtime",
           "interpreter", "shell/env-init", "search", "build", "net/data",
           "files", "other")

# Tool-call taxonomy. Priority-ordered; first match wins. Operates on the
# effective_comm token (shells already peeled to their real inner command).
# `comm` is capped at 15 chars by the tracer, so several signatures are prefixes.
# --------------------------------------------------------------------------- #
SHELLS = SHELL_COMMS
POLL = {
    "ps", "pgrep", "pidof", "top", "htop", "free", "vmstat", "iostat", "mpstat",
    "uptime", "sar", "watch", "dcgmi", "sleep", "lsb_release", "uname", "id",
    "whoami", "tty", "arch", "nproc", "getconf", "locale", "date", "sysctl",
    "which", "command", "type", "readlink", "getfattr", "stat", "setopt",
    "env", "printenv", "tput", "clear", "ldconfig",
}
SLURM_Q = {"squeue", "sacct", "sstat", "sinfo", "scontrol", "sprio", "sshare",
           "sreport", "seff", "showq", "sacctmgr", "sview", "sdiag"}
SLURM_S = {"srun", "salloc", "scancel", "sbcast", "sattach"}   # sbatch* via prefix
SEARCH = {"grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "find", "fd",
          "locate", "zgrep"}
BUILD = {"make", "cmake", "ninja", "gcc", "g++", "c++", "cc", "cc1", "cc1plus",
         "clang", "clang++", "as", "ld", "ld.gold", "ld.lld", "collect2",
         "gfortran", "ifort", "ifx", "icx", "icc", "icpc", "icpx", "mpicc",
         "mpicxx", "mpic++", "mpif90", "mpif77", "mpifort", "mpiifort", "mpiifx",
         "mpiicpc", "mpiicc", "fortcom", "xfortcom", "nvcc", "cicc", "ptxas",
         "nvfortran", "rustc", "cargo", "pdflatex", "xelatex", "lualatex", "latexmk",
         "bibtex", "cpp", "nvc", "nvc++", "xilink", "xild", "xiar", "ar", "ranlib",
         "cabal", "ghc", "javac", "go", "tsc", "gp", "flang", "pgcc", "pgf90",
         "pgfortran", "xicc", "xicpc", "lld", "ld.lld", "gmake", "gold", "lld-link"}
PY = {"python", "python2", "python3", "ipython", "pytest", "wandb", "jupyter",
      "jupyter-lab", "jupyterlab", "ipykernel", "conda-python",
      "R", "Rscript", "r", "rscript", "perl", "julia", "ruby", "matlab", "octave",
      "mathematica", "mathkernel", "sage", "stata", "sas", "java"}  # -> "interpreter"
ENV = {"conda", "mamba", "micromamba", "pip", "pip3", "uv", "npm", "npx", "yarn",
       "pnpm", "lmod", "module", "ml", "modulecmd", "lua", "podman", "docker",
       "singularity", "apptainer", "spack", "apt", "dnf", "yum", "brew"}
DATA = {"rsync", "wget", "curl", "scp", "sftp", "aria2c", "s3cmd", "aws", "gsutil",
        "rclone", "tar", "gzip", "gunzip", "bzip2", "xz", "zip", "unzip", "zcat",
        "dd", "git-lfs", "globus", "ftp", "lftp",
        "ssh", "sshd", "mosh", "mosh-server", "telnet", "ssh-agent", "sshpass",
        "autossh"}   # bucket -> "net/data" (remote-exec + transfer)
FILES = {"cat", "ls", "head", "tail", "wc", "sed", "awk", "gawk", "tr", "cut",
         "sort", "uniq", "cp", "mv", "rm", "mkdir", "rmdir", "touch", "ln",
         "chmod", "chown", "chgrp", "du", "df", "echo", "printf", "nl", "base64",
         "expr", "basename", "dirname", "tee", "xargs", "jq", "yq", "less", "more",
         "diff", "patch", "file", "realpath", "mktemp", "split", "paste", "comm",
         "join", "fold", "rev", "seq", "tac", "truncate", "md5sum", "sha256sum",
         "cksum", "od", "hexdump", "strings", "pr", "column", "[", "test"}
EDITOR = {"vim", "nvim", "vi", "view", "nano", "emacs", "emacsclient", "code",
          "code-insiders", "joe", "micro", "ed", "pico", "sublime", "gedit"}
RUNTIME_EXACT = {"node", "claude", "codex", "cursor", "bwrap", "deno", "bun",
                 "electron", "MainThread"}


def bucket(ec: str) -> str:
    """Map an effective_comm token to a tool-call bucket."""
    if not ec:
        return "other"
    t = ec.strip().strip('"').strip("'")
    tl = t.lower()
    # prefix rules for tracer-truncated / family names (checked before exact sets)
    if tl.startswith(("cpuusage", "nvidia-sm", "powerline", "starship")):
        return "poll/probe"                           # incl. shell-prompt renderers
    if tl.startswith("sbatch"):
        return "slurm-submit"
    if tl.startswith("git"):                          # git, git-remote-http(s)
        return "git"
    if tl.startswith(("node", "codex", "login-node", "command-guard",
                      "check_complete", "tokio-rt", "copilot", "claude", "cursor")):
        return "runtime"                              # node-MainThread, codex-linux-san …
    if tl.startswith("wandb"):                        # wandb-service(…)
        return "interpreter"
    if re.match(r"python[0-9.]*$", tl):
        return "interpreter"
    if t in SHELLS:
        return "shell/env-init"
    if t in ENV:                                      # conda/lmod/module: ~90% activation
        return "shell/env-init"                       # (not package installs) -> session setup
    if t in POLL:
        return "poll/probe"
    if t in SLURM_Q:
        return "slurm-query"
    if t in SLURM_S:
        return "slurm-submit"
    if t in SEARCH:
        return "search"
    if t in BUILD:
        return "build"
    if t in PY:
        return "interpreter"
    if t in DATA:
        return "net/data"
    if t in FILES or t in EDITOR:                     # CLI editors folded into files
        return "files"
    if t in RUNTIME_EXACT:
        return "runtime"
    return "other"


def shellenv_sub(cm, ec, a):
    """Sub-label an event that landed in the shell/env-init bucket."""
    if cm in SHELLS and ec in SHELLS:          # an unresolved shell process
        if L is None:                          # no agent_lib: the kind is unknowable
            return "shell (other)"
        kind, _ = L.shell_real(a)
        return {"login": "login/interactive shell", "loop": "wait-loop",
                "probe": "env-probe", "unresolved": "unresolved (truncated)",
                "cmd": "shell", "other": "shell (other)"}.get(kind, "shell (other)")
    # else it is an env/module command (conda/lmod/module/lua/pip/…)
    low = a.lower()
    if ("shell" in low or "printenv" in low or "activate" in low or "lmod" in low
            or " load" in low or "modulecmd" in low or ec in ("lua", "lmod",
            "modulecmd", "module", "ml")):
        return "env activation / module load"
    if _INSTALL.search(a):
        return "package install/build"
    return "env other"
