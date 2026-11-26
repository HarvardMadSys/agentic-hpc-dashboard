"""Resolved configuration: CLI > env > config file > built-in default.

A config file is TOML (`.toml`, read with the standard library's `tomllib`) or
JSON (anything else), checked the same way either way. In JSON, keys beginning
`//` are comments and are stripped on load, so the committed example file is
itself a legal config. With no file named, `dashboard/dashboard.config.json` or
`dashboard/dashboard.toml` is read if it exists; both existing is refused
(`find_config_file`). A relative path in a file is under the repository's root,
or, installed without a checkout, beside the file (`_resolve_paths`).

Precedence is resolved PER LEAF, not per section, so `RC_DASH_EBPF_ROOTS` can
override one key without restating the rest of `feeds.ebpf`.  Every CLI flag
defaults to None and is treated as absent when None -- an argparse default would
otherwise silently outrank the config file.

Path defaults are CANDIDATE LISTS, probed in order against the filesystem.  A
feed reports what it looked for as well as what it found, which is what the UI's
empty states name. A root may be a glob pattern (`expand_root`).

Nothing here falls back to a default quietly where that would leave names
unmasked or the page open: a config file named by `--config` or RC_DASH_CONFIG
must exist, a yes/no setting must read as yes or no (`_flag`), and an unknown
key under `server` or `anonymize` stops the service (`check_file`).
"""
import datetime
import difflib
import fnmatch
import glob
import json
import os
import re
import textwrap
import tomllib

PKG = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(PKG)
REPO = os.path.dirname(HERE)
# Run from the repository's own tree -- a clone, a release made from one
# (hpc/deploy), an image that copies it (docker/demo) -- or installed on its own,
# from a wheel: then `HERE` is site-packages, and the page, the cache and a
# config file's relative paths are found elsewhere (`_static_default`,
# `_cache_default`, `_resolve_paths`). Decided once, from where this file is.
CHECKOUT = os.path.isfile(os.path.join(HERE, "pyproject.toml"))
# The collector's own settings file, as its systemd unit reads it
# (EnvironmentFile=, packaging/rpm/ebpfm.default): where it writes, when set there.
EBPFM_DEFAULTS = "/etc/default/ebpfm"
# A root, or a transport file, holding one of these is a glob pattern (`expand_root`).
GLOB_CHARS = "*?["

_MISSING = object()
# `history` (history.py) unless set.
HISTORY = {"enabled": True, "dir": "", "workers": 0, "days": 0}
# `ingest.workers`: processes reducing the hours (hours.py); 1 is one pass in
# the service's own process.
INGEST_WORKERS = 1
# `ingest.reducer_groups`: processes per node's hour, one per reducer group (1 or 2).
INGEST_REDUCER_GROUPS = 2

# `reducers.own_io.max_parents`: the parents each own-I/O join holds
# (reducers/own_io.py, whose REAPED_MAX a test holds equal).
OWN_IO_MAX_PARENTS = 2000


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v else default


def _pathsep(v):
    return [p for p in (v or "").split(os.pathsep) if p]


def _csv(v):
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _words(v):
    """Names separated by commas, spaces, or both."""
    return [p for p in re.split(r"[\s,]+", v or "") if p]


def _flag(v):
    """A yes/no setting written as text -- an env var, or a string in the
    config file: 1/0, true/false, yes/no or on/off, in any case, with spaces
    around. Anything else is refused rather than read as yes: `False`, read as
    yes, once left a feature on that was meant to be off."""
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError("%r is not one of 1/0, true/false, yes/no, on/off" % v)


# ---------------------------------------------------------------- defaults
def env_file(path):
    """The settings in a systemd EnvironmentFile, as systemd reads them: KEY=VALUE
    lines, `#` and `;` lines comments, whitespace around the value dropped, and
    one pair of quotes around it; the last assignment of a key wins. {} when the
    file is missing or unreadable."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(1 << 20).splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, sep, val = s.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def _collector_named(key):
    """`key` as the collector's settings file names it ([] when it does not)."""
    v = env_file(EBPFM_DEFAULTS).get(key)
    return [v] if v else []


def _ebpf_candidates():
    """Where the root eBPF tier's day-files might live, most specific first.

    `EBPFM_LOG_ROOT` before `EBPFM_OUTPUT_DIR` deliberately: collect/healthcheck.py
    uses the distinct LOG_ROOT name so an operator shell that repoints the
    collector cannot silently repoint the readers too.  The repo symlink
    `log/ebpfm -> ../data/ebpfm/logs` already contains `/logs`, so the repo-side
    layout is `log/ebpfm/<date>/<host>.jsonl`. Then the folder the collector's
    own settings file names (`EBPFM_DEFAULTS`), and the collector's defaults: a
    root collector writes to /scratch/ebpfm where /scratch is writable, else to
    /var/log/ebpfm (collector/ebpfm.sh), and an unprivileged one to ~/.ebpfm/ebpf.
    """
    c = []
    for e in ("EBPFM_LOG_ROOT", "EBPFM_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    base = _env("RC_MEASUREMENT_ROOT")
    if base:
        c.append(os.path.join(base, "ebpfm", "logs"))
    c += _collector_named("EBPFM_OUTPUT_DIR")
    c += [os.path.join(REPO, "log", "ebpfm"),
          os.path.join(REPO, "log", "ebpfm", "logs"),
          "/scratch/ebpfm",
          "/var/log/ebpfm",
          os.path.expanduser("~/.ebpfm/ebpf")]
    return _unique(c)


def _node_candidates():
    """The unprivileged node tier.  `log/login` is last but load-bearing:
    node_snapshot.py is a vendored copy of collect/common/snapshot.py, so the
    login feed that poller writes is schema-compatible, and is read where the
    collector's node tier is not deployed."""
    c = []
    for e in ("EBPFM_NODE_LOG_ROOT", "EBPFM_NODE_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    base = _env("RC_MEASUREMENT_ROOT")
    if base:
        c.append(os.path.join(base, "ebpfm", "node", "logs"))
    c += _collector_named("EBPFM_NODE_OUTPUT_DIR")
    c += [os.path.join(REPO, "log", "ebpfm_node"),
          "/scratch/ebpfm-login",
          "/var/log/ebpfm-login",
          os.path.expanduser("~/.ebpfm/login"),
          os.path.join(REPO, "log", "login")]
    return _unique(c)


def _unique(paths):
    """`paths` in order, each once (a folder named twice is still read once)."""
    seen, out = set(), []
    for p in paths:
        k = os.path.normpath(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def is_pattern(path):
    """Whether a root (or a transport file) is a glob pattern."""
    return isinstance(path, str) and any(c in path for c in GLOB_CHARS)


def expand_root(path):
    """The folders a root names now: a glob pattern's matches that are folders,
    sorted (`*`, `?`, `[...]`, as the shell has them; matched again each time it
    is asked, so a folder made later is found), or the root itself -- as it is
    when a folder of that very name exists (`logs[1]`)."""
    if not is_pattern(path) or os.path.isdir(path):
        return [path]
    return sorted(p for p in glob.glob(os.path.expanduser(path)) if os.path.isdir(p))


def _cache_default():
    """`<checkout>/dashboard/.cache`; installed without a checkout, the user's
    state folder: the cache holds the anonymize salt and the session key, which
    are not rebuilt."""
    if CHECKOUT:
        return os.path.join(HERE, ".cache")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "rc-dashboard")


def _static_default():
    """The built page: `dashboard/web/dist` in a checkout, else the copy a wheel
    carries inside the package (pyproject.toml puts web/dist there)."""
    return os.path.join(HERE, "web", "dist") if CHECKOUT else os.path.join(PKG, "web")


def defaults():
    return {
        "version": 1,
        "feeds": {
            "ebpf": {
                "roots": _ebpf_candidates(),
                # One date dir per day of retention, plus one: hosts roll over
                # independently, so the oldest day in a 7-day window is still
                # being written by a straggler when the newest has moved on.
                "days": 8,
                "max_age_s": 300,       # residency_totals ticks every 60s -> 5x
                "required": True,
            },
            "ebpf_node": {
                "roots": _node_candidates(),
                "days": 2,
                "snapshots_per_host": 2,   # cpu_usage_usec is cumulative: need a delta
                # day-dirs the node history reads (nodehist.py); 0 is enough
                # for retention, whatever `days` above
                "series_days": 0,
                "max_age_s": 900,
                "required": False,
            },
        },
        "live": {
            # RETENTION is what the process holds; WINDOW_MIN is only the view a
            # fresh page opens on.  Any window up to retention is an exact slice
            # of the same bins, so the two must not be conflated: raising the
            # view costs nothing, raising retention costs memory and backfill.
            "retention_min": 10080,  # 7 days of per-minute bins, ~hundreds of KB
            "window_min": 1440,      # the default view: 24h, the Overview grain
            # Offered in the picker.  Server-side so the ceiling and the options
            # cannot disagree -- anything above retention is dropped from the
            # list rather than offered and then clamped.
            "window_presets_min": [60, 360, 720, 1440, 4320, 10080],
            "bin_s": 60,
            "event_tail": 100,
            "submit_tail": 50,
            # Fill retention, not a day of it.  Read on EVERY start, newest
            # first, from the collector's files -- the bins are never restored
            # from anything this service wrote -- so the page cannot depend on
            # whether the service was running while the data was collected.
            # 0 starts empty and fills forward.
            "backfill_hours": 168,
            "clock_skew_s": 120,
        },
        "ingest": {
            "poll_ms": 1000,
            # Processes reducing the hours (hours.py): 1 reduces each hour in
            # one pass in the service's own process; N >= 2 reduces each
            # node's share of an hour in a worker process of its own, at most
            # N at once, and merges them (one node never uses more than one,
            # so past the node count more do nothing); 0 is one per CPU the
            # service may use. Going from 1 to more, or back, reduces the
            # hours afresh: a merged hour's "first N" lists and ties can
            # differ from one pass's, its counts and sums cannot.
            "workers": INGEST_WORKERS,
            # With workers >= 2, each node's hour in this many processes: 2
            # splits its reducers into the two groups that share nothing (the
            # session reducers and their process index; the counting panels),
            # each reading the node's records itself, so the busiest node's
            # hours take two cores. The states put back together are one
            # pass's, so this changes no number (and no saved hour).
            "reducer_groups": INGEST_REDUCER_GROUPS,
        },
        # Every sealed day-file of the eBPF tier kept as Parquet (history.py),
        # under `dir` (<cache.dir>/history unless set), converted in the
        # background in `workers` processes (0: up to 8, as the CPUs allow); a
        # sealed file's selective reads and time searches are answered from it.
        # `days`: the newest dated dirs kept, 0 for every one the source holds.
        "history": dict(HISTORY),
        # The roll-up table the Trend tab draws (rollup.py): each local day's
        # processes, CPU, users and submissions by class, agent type and host,
        # summed as records are ingested and kept past retention, in SQLite
        # under `dir` (<cache.dir>/rollup unless set). It is the only copy of a
        # day whose day-files are gone, so a `dir` outside the cache is safer.
        "rollup": {"enabled": True, "dir": ""},
        # The service's present. "" is the wall clock; "auto" or a local time
        # pins (and freezes) it, for a feed captured elsewhere and copied here.
        # See clock.py.
        "clock": {"as_of": ""},
        "rebuild": {"interval_s": 900},
        # The login-node health panel's lines (reducers.resources.NodeReducer):
        # a local filesystem at or past either is flagged full.
        "node": {"disk_full_pct": 90, "inode_full_pct": 90},
        # A cluster without Slurm turns this off and the HPC tab goes away;
        # nothing else depends on it (the tool mix keeps its two scheduler
        # kinds, which simply stay empty there).
        "slurm": {"enabled": True},
        # Which deployment this is, on every page: two dashboards side by side
        # -- a frozen capture from one cluster, the live feed of another -- must
        # not be mistaken for each other.
        # `description`: what this deployment watches, in a few words ("FASRC
        # login nodes", "a GPU workstation"); empty says nothing.
        "site": {"name": "", "description": ""},
        "panels": {
            # Historical reducers hold no time index, so a window change rebuilds
            # them from the rows.  Each built window costs reducer state, so the
            # registry is an LRU rather than unbounded. Five holds the default,
            # the prewarmed short windows and one wide one a reader opened; a
            # sixth evicts the least recently asked-for (which then resumes from
            # its saved copy rather than re-reading).
            "max_windows": 5,
            # Built in the background after the default window, so switching to
            # them is instant. Only the cheap ones: a 7d build reads the whole
            # week, and is left to the reader who asks for it.
            "prewarm_min": [60, 360, 720],
            # A built view keeps ingesting, so it drifts past its own window.
            # Re-scan once drift reaches this share of the window -- proportional
            # by design: a 1h view refreshes often and cheaply, a 7d view rarely
            # and expensively, and both read the same multiple of the feed.
            "max_drift_pct": 25,
            # Save every built window to <cache.dir>/windows, with how far into
            # each day-file it read, so a restart or a return to an evicted
            # window reads only what the collector wrote since. Used only while
            # younger than the drift budget above, and only by the same code
            # and config; ~12 MB per day of window on one login node.
            "cache_windows": True,
            "cache_keep": 8,
            # Reduce each record ONCE, into the state of its epoch hour, saved
            # under <cache.dir>/hours; a window is then its first part-hour
            # read as before, its whole hours merged from their saved states,
            # and the open hour read as before (hours.py). A window switch, a
            # drift refresh or a restart costs the edges and a merge, not a
            # re-read. A view filtered to users or agent types is still read.
            "merge_reduce": True,
            # An hour is closed, and reduced, once the newest record is this far
            # past its end: the collector writes a poll round at a time and
            # hosts' clocks disagree. Never less than live.clock_skew_s plus a
            # poll round. A record that comes later still is merged into its
            # hour's saved state, not lost.
            "hour_grace_s": 300,
            # Save each whole epoch day as one merged state too, so a 7 d window
            # merges ~7 states rather than ~168.
            "day_cache": True,
            # Pause between two hours' reductions, so a backlog of hours (the
            # first 7 d window after a code change) never holds the live tier
            # off for the length of the read.
            "hour_pause_s": 0.02,
            # Preprocess everything at start-up rather than as readers ask:
            # reduce every closed hour in retention and merge its whole days,
            # then build and hold EVERY preset window, the widest included
            # (max_windows is raised to fit them), so no window switch waits on
            # a reduction. Off by default: on a busy node a held 7 d window is
            # up to ~1 GB. The demo turns it on.
            "preprocess": False,
            # With preprocess on, hold the port closed for up to this long
            # while it runs, so the first page already has every window built
            # instead of a "merging" bar. 0 serves at once, preprocessing
            # behind. A deadline, not a requirement: past it the service
            # listens and the builds carry on.
            "preprocess_wait_s": 0,
        },
        "filters": {
            # Accounts whose records are dropped, and counted (`dropped_users`):
            # drop_global, every record of theirs (RC_DASH_DROP_USERS,
            # comma-separated); drop_agent, only those classed agent
            # (RC_DASH_DROP_AGENT_USERS). A site names its own -- the account its
            # monitoring runs as, say -- and none is dropped by default.
            "drop_global": [],
            "drop_agent": [],
            "classes": ["agent", "human", "ide"],
            "unlabeled_class": "unlabeled",
            # the collector's own probes (node_snapshot's mpstat/iostat/sar/
            # nfsiostat/nvidia-smi shell-outs). Counted, not silent.
            "drop_self_observation": True,
        },
        "reducers": {
            "tool_mix": {"head_n": 12, "dedup_sessions_max": 200000},
            "io": {"top_n": 20},
            # Abnormal activity at the scheduler (reducers/slurm.py, ABNORMAL,
            # which a test holds equal): a session breaks a rule when its own
            # count reaches the threshold. Fast polling: its longest stretch of
            # queue checks at most `poll_gap_s` apart lasts `poll_min_s` or more
            # (polling every 2-3 min while a job runs is how an agent waits, and
            # breaks nothing). Then the most queue checks, or jobs submitted, in
            # any 60 s; submissions the scheduler refused; scancel calls; srun or
            # salloc runs that failed. Each a count of 1 or more (seconds for the
            # two polling bounds), or the service refuses to start.
            "slurm": {"abnormal": {"poll_gap_s": 30, "poll_min_s": 1800, "query_burst": 30,
                                   "submit_burst": 20, "refused": 3, "cancels": 10,
                                   "failed_runs": 5}},
            # Each process's own disk bytes (reducers/own_io.py): the parents
            # one join holds for their own exit, the one given to least
            # recently dropped first (an agent runtime, or what started one,
            # passed over), counted (a panel's `own_io.evicted`) and said on
            # stderr, at most hourly a panel. A service holds some 36 joins:
            # at one entry a parent (~0.5 KB) 2000 is ~40 MB in all, at worst
            # (eight, ~2.5 KB) 164 MB. The repo's streams hold under 500 at
            # once; a busy login node may need more, at 19 to 80 KB for each
            # one added (README, "Disk I/O is each process's own"). A count of
            # one or more: 0 is refused at start, as is anything not a count.
            "own_io": {"max_parents": OWN_IO_MAX_PARENTS},
            # `live_s`: how stale the ranked sessions may get while the view
            # is fed live -- the panel re-reads them that often, and they are
            # re-ranked on the read, not only at the next rebuild.
            # `max_sessions`: session keys held per window, about 6 KB each.
            # Schema 7 keys each tool call apart, and a FAS login node makes
            # up to ~105 000 in a day: a busy day is held whole.
            "trajectories": {"top_n": 15, "max_runs_shown": 60, "purpose_seq_cap": 80,
                             "idle_evict_s": 7200, "max_sessions": 150000,
                             "include_key_types": ["apid", "sess", "tty"], "live_s": 5,
                             # An agent row's phases: reducers/phase_segment.py documents
                             # each threshold. Its DEFAULTS, tuned on two real days
                             # (rc_dashboard/phase_report.py); a test holds the two equal.
                             "phases": {"idle_s": 300, "guess_min_s": 20, "absorb_calls": 6,
                                        "absorb_s": 300, "poll_min": 5, "poll_cv": 0.25,
                                        "poll_cv_strict": 0.10, "step_s": 1.0,
                                        "top_commands": 3, "max_calls": 20000,
                                        "hk_bin_cap": 10080, "pend_max": 50000,
                                        "app_read_s": 2.0},
                             # The tasks above them: reducers/phase_tasks.py documents each
                             # threshold. Its DEFAULTS, tuned on seven hand-labeled sessions
                             # (rc_dashboard/phase_report.py); a test holds the two equal.
                             "tasks": {"gap_s": 3600, "lone_calls": 8, "lone_s": 7200,
                                       "new_steps": 5, "new_window_s": 1800,
                                       "close_overlap": 0.5, "close_series_s": 300,
                                       "min_s": 600, "min_calls": 5, "cap": 10,
                                       "headline_names": 3},
                             # What kind of session a block was, and its summary:
                             # reducers/phase_summary.py documents each threshold. Its
                             # DEFAULTS, read against eight FAS RC login nodes
                             # (rc_dashboard/phase_report.py); a test holds the two equal.
                             "summary": {"mostly": 0.5, "also": 0.1}},
        },
        # What counts as risky differs by cluster. The rules are built in
        # (reducers/risk.py, which documents every key); this switches them
        # off, changes or adds to them, and sets the cluster's variables.
        # `profile` is a JSON file of the same shape merged over this section
        # at load, so a cluster's rules travel as one file (risk-profiles/).
        "risk": {
            "profile": "",
            "vars": {"compute_hosts": r"holy|bos|node|gpu|c\d{3,}"},
            "dangerous": {"disable": [], "override": {}, "add": []},
            "secrets": {"disable": [], "override": {}, "add": []},
            "login_compute": {"compute": ["build", "interpreter"], "transfer": ["net/data"]},
            # The Risk tab's sections, in order: kind login_compute, commands or
            # credentials; `rules` a list of ids, or "rest". See reducers/risk.py.
            "sections": [
                {"id": "login_compute", "kind": "login_compute",
                 "title": "Compute on a login node"},
                {"id": "commands", "kind": "commands", "rules": "rest",
                 "title": "Commands that should not run here"},
                {"id": "credentials", "kind": "credentials", "rules": "rest",
                 "title": "Credentials on a command line"},
            ],
        },
        # Replace every username, lab or Slurm account name, email address and
        # numeric user ID in what the service sends out with a stable alias
        # (anonymize.py). Off by default: a deployment for the cluster's own
        # staff shows real names unless told otherwise. `salt` empty: one is
        # generated into <cache.dir>/anonymize.salt and reused; set, it is 16
        # bytes or more. Changing it reshuffles every alias. The patterns name
        # where homes and lab trees sit on this cluster -- see
        # anonymize.HOME_PATTERNS for the defaults.
        # What the page opens on, before a reader changes it. Nothing here
        # changes a number, so the saved builds ignore the section.
        # user_columns: the Active users table's columns (agents, ides, running,
        # exited, cpu, read, written, sent, received, gpu, last; `net` is sent
        # and received); a reader's own choice is kept in their browser.
        "ui": {
            # What the hosts this dashboard watches are called on the page:
            # "Login nodes right now". A workstation fleet says "hosts".
            "node_noun": "login nodes",
            # The Internal tab -- figures whose reading is not settled yet --
            # listed in the rail. Off: a reader sees only the settled tabs.
            "show_internal": False,
            # How often each tab is opened, counted on this host for the site's
            # own use (usage.py, `rc-dashboard usage`): a tab's key and a count a
            # day, never a user, a window or a filter. Off: the page sends nothing.
            "count_views": True,
            "user_columns": ["agents", "ides", "running", "exited", "cpu", "read", "written", "last"],
            # What a trajectory's tree hides when "hide routine" is on, beside
            # runs of the same command: a command, a command and its first
            # argument ("git status"), or "shell-snapshot" for an agent's shell
            # sourcing its saved environment. A shell that ran only these, or
            # one of them piped through filters (`ps | grep root`), goes too.
            "routine_commands": [
                "lsb_release", "getconf", "uname", "sleep", "pgrep", "ps", "tr", "cut",
                "dirname", "basename", "readlink", "realpath", "which", "hostname", "id",
                "whoami", "date", "tty", "stty", "tput", "locale", "nproc", "true", "false",
                "git status", "git rev-parse", "git config", "git symbolic-ref",
                "shell-snapshot",
            ],
        },
        "anonymize": {
            "enabled": False,
            "salt": "",
            "labs": True,
            "emails": True,
            "uids": True,
            "keep": ["root"],
            "home_patterns": [],
            "lab_patterns": [],
        },
        # reducer_version: read by no code, but in both fingerprints (hours.py,
        # snapshots.py), so raising it discards every saved hour and build.
        "cache": {"dir": _cache_default(),
                  "reducer_version": 3},
        # Is the collection working (status.py: `rc-dashboard status`,
        # /api/status, /metrics)? `transport_files`: the state files of what
        # copies the collectors' files here -- deploy/fleet's pull.py
        # (<mirror>/.state/<node>.json) and hpc/fleet's receiver
        # (<base>/state/status.json) -- as paths or glob patterns.
        # `stale_s`: a host's newest record, or its copy's last good pass,
        # older than this is a problem. Neither changes a number, so no saved
        # build or hour is keyed on them (`UNREDUCED`).
        "status": {"transport_files": [], "stale_s": 900},
        "server": {"host": "127.0.0.1", "port": 8080,
                   "static_dir": _static_default(),
                   # Sign-in (auth.py): a salted scrypt hash of the one password
                   # readers sign in with, made by `rc-dashboard new-password`,
                   # kept in a file of its own (`password_hash_file`, re-read
                   # when it changes) or given here (`password_hash`), never
                   # both. A session lasts `session_days`. With no hash, `serve`
                   # refuses to start unless `open` (--open, RC_DASH_OPEN): an
                   # open page is read by anyone who reaches the port, which on
                   # a login node is every user of it. A demo serves made-up data
                   # and is open unless a hash is set.
                   # Or the site's single sign-on (auth.Proxy): a reverse proxy in
                   # front of the service signs the reader in and names them in
                   # the header `proxy_header` names (X-Forwarded-User); empty, it
                   # is off. The header is believed only with the secret in
                   # `proxy_secret_file`, which the proxy sends on every request
                   # (X-RC-Proxy-Secret), and only from a connection whose own
                   # address is in `proxy_trusted` (addresses or networks).
                   # With `proxy_groups`, the user must be in one of them, as
                   # `proxy_groups_header` lists theirs. Under it Sign out goes to
                   # `proxy_logout_url`, and with none is not offered. A request it
                   # does not sign in falls back to the password, if there is one.
                   "auth": {"password_hash": "", "password_hash_file": "",
                            "session_days": 30, "open": False,
                            "proxy_header": "", "proxy_groups_header": "",
                            "proxy_groups": [], "proxy_trusted": ["127.0.0.1", "::1"],
                            "proxy_logout_url": "", "proxy_secret_file": ""},
                   # Who may connect (guard.py). A websocket opened by a page on
                   # any origin but the dashboard's own -- another port of the
                   # same host included -- is refused, unless its origin is
                   # listed here ("https://dash.example.org"): for a proxy that
                   # rewrites the Host.
                   "allowed_origins": [],
                   # The origins of a dev server whose page proxies /ws here, added
                   # to allowed_origins, never in their place: dashboard/run.sh sets
                   # it (RC_DASH_DEV_ORIGINS) to Vite's in dev mode.
                   "dev_origins": [],
                   # With no password, a request is answered only under a
                   # loopback name, `host` above, or a name listed here ("*": any):
                   # another site's page cannot read the dashboard by pointing its
                   # own name at 127.0.0.1 (DNS rebinding). Unused with a password,
                   # unless proxy sign-in is on: such a page could send the header.
                   "allowed_hosts": [],
                   # Name the endpoints on the Network bytes panel by reverse
                   # DNS (endpoints.py): `workers` lookups at a time, from this
                   # host, off the request path, kept in <cache.dir>/rdns.json.
                   # Off: names come only from the records' command lines.
                   "rdns": {"enabled": True, "workers": 4},
                   # How long a thread may hold the GIL while another waits for
                   # it (`sys.setswitchinterval`; Python's own is 5 ms). Builds,
                   # the backfill and the tail are pure Python and keep the GIL;
                   # the event loop gives it up at every read and write and
                   # waited up to this long for it each time. 1 ms: on the bench
                   # feed a warm start's default window was ready in 16 s, not 29,
                   # and a static file's p99 fell from 594 ms to 248 ms. 0 keeps
                   # Python's.
                   "gil_switch_ms": 1.0},
    }


# leaf path -> (env var, coercion).  Flat RC_DASH_* names, per repo convention.
ENV_MAP = {
    "feeds.ebpf.roots": ("RC_DASH_EBPF_ROOTS", _pathsep),
    "feeds.ebpf.days": ("RC_DASH_EBPF_DAYS", int),
    "feeds.ebpf.max_age_s": ("RC_DASH_EBPF_MAX_AGE_S", int),
    "feeds.ebpf_node.roots": ("RC_DASH_NODE_ROOTS", _pathsep),
    "feeds.ebpf_node.days": ("RC_DASH_NODE_DAYS", int),
    "live.retention_min": ("RC_DASH_RETENTION_MIN", int),
    "live.window_min": ("RC_DASH_LIVE_WINDOW_MIN", int),
    "live.window_presets_min": ("RC_DASH_WINDOW_PRESETS",
                                lambda v: [int(x) for x in _csv(v)]),
    "live.bin_s": ("RC_DASH_LIVE_BIN_S", int),
    "live.event_tail": ("RC_DASH_LIVE_EVENT_TAIL", int),
    "live.backfill_hours": ("RC_DASH_BACKFILL_HOURS", int),
    "panels.max_windows": ("RC_DASH_MAX_WINDOWS", int),
    "panels.prewarm_min": ("RC_DASH_PREWARM", lambda v: [int(x) for x in _csv(v)]),
    "panels.max_drift_pct": ("RC_DASH_MAX_DRIFT_PCT", int),
    "panels.cache_windows": ("RC_DASH_CACHE_WINDOWS", _flag),
    "panels.merge_reduce": ("RC_DASH_MERGE_REDUCE", _flag),
    "panels.preprocess": ("RC_DASH_PREPROCESS", _flag),
    "panels.preprocess_wait_s": ("RC_DASH_PREPROCESS_WAIT_S", float),
    "ingest.poll_ms": ("RC_DASH_POLL_MS", int),
    "history.enabled": ("RC_DASH_HISTORY", _flag),
    "history.dir": ("RC_DASH_HISTORY_DIR", str),
    "history.workers": ("RC_DASH_HISTORY_WORKERS", int),
    "history.days": ("RC_DASH_HISTORY_DAYS", int),
    "rollup.enabled": ("RC_DASH_ROLLUP", _flag),
    "rollup.dir": ("RC_DASH_ROLLUP_DIR", str),
    "ingest.workers": ("RC_DASH_INGEST_WORKERS", int),
    "ingest.reducer_groups": ("RC_DASH_INGEST_REDUCER_GROUPS", int),
    "clock.as_of": ("RC_DASH_AS_OF", str),
    "rebuild.interval_s": ("RC_DASH_REBUILD_S", int),
    "filters.drop_global": ("RC_DASH_DROP_USERS", _csv),
    "filters.drop_agent": ("RC_DASH_DROP_AGENT_USERS", _csv),
    "filters.drop_self_observation": ("RC_DASH_DROP_SELF_OBS", _flag),
    "risk.profile": ("RC_DASH_RISK_PROFILE", str),
    "slurm.enabled": ("RC_DASH_SLURM", _flag),
    "anonymize.enabled": ("RC_DASH_ANONYMIZE", _flag),
    "site.name": ("RC_DASH_SITE_NAME", str),
    "ui.node_noun": ("RC_DASH_NODE_NOUN", str),
    "site.description": ("RC_DASH_SITE_DESC", str),
    "cache.dir": ("RC_DASH_CACHE_DIR", str),
    "server.host": ("RC_DASH_HOST", str),
    "server.port": ("RC_DASH_PORT", int),
    "server.static_dir": ("RC_DASH_STATIC_DIR", str),
    "server.auth.password_hash": ("RC_DASH_PASSWORD_HASH", str),
    "server.auth.password_hash_file": ("RC_DASH_PASSWORD_HASH_FILE", str),
    "server.auth.open": ("RC_DASH_OPEN", _flag),
    "server.auth.proxy_header": ("RC_DASH_PROXY_HEADER", str),
    "server.auth.proxy_groups_header": ("RC_DASH_PROXY_GROUPS_HEADER", str),
    "server.auth.proxy_groups": ("RC_DASH_PROXY_GROUPS", _words),
    "server.auth.proxy_trusted": ("RC_DASH_PROXY_TRUSTED", _words),
    "server.auth.proxy_logout_url": ("RC_DASH_PROXY_LOGOUT_URL", str),
    "server.auth.proxy_secret_file": ("RC_DASH_PROXY_SECRET_FILE", str),
    "ui.show_internal": ("RC_DASH_SHOW_INTERNAL", _flag),
    "ui.count_views": ("RC_DASH_COUNT_VIEWS", _flag),
    "status.transport_files": ("RC_DASH_TRANSPORT_FILES", _csv),
    "status.stale_s": ("RC_DASH_STATUS_STALE_S", int),
    "server.allowed_origins": ("RC_DASH_ALLOWED_ORIGINS", _csv),
    "server.dev_origins": ("RC_DASH_DEV_ORIGINS", _csv),
    "server.allowed_hosts": ("RC_DASH_ALLOWED_HOSTS", _csv),
    "server.rdns.enabled": ("RC_DASH_RDNS", _flag),
    "server.gil_switch_ms": ("RC_DASH_GIL_SWITCH_MS", float),
}

# Secrets: /api/config and --print-config say whether each is set, never what it is.
HIDDEN = ("anonymize.salt", "server.auth.password_hash")

# The feeds removed once nothing read them -- the scheduler's accounting (sacct,
# sacctmgr) and the domain and user CSVs -- and every way there was to set one.
# Each is named in a warning and ignored, never refused, so a unit, a wrapper or
# a config file written for them still starts (`load`).
RETIRED_FEEDS = ("sacct", "sacctmgr", "domains", "user_actor")
RETIRED_ENV = ("RC_DASH_SACCT_DIR", "RC_DASH_SACCT_PATTERN", "RC_DASH_SACCT_MAX_DAYS",
               "RC_DASH_JOBS_CSV", "RC_DASH_SACCTMGR_DIR", "RC_DASH_DOMAINS_CSV",
               "RC_DASH_USER_CSV", "RC_JOBS_CSV", "RC_USER_CSV")
RETIRED_CLI = ("sacct_dir", "jobs_csv")
RETIRED_WHY = "is retired, with the feed it set, which nothing read; it is ignored"
# The sections of features since removed, each with why it is ignored, and every
# variable there was to set one. Each is named in a warning and dropped, never
# refused, as a retired feed is: a unit, a wrapper or a config file written for
# one still starts, and nothing of it reaches /api/config -- where a key under
# `annotate` or `ask` (an agent's command, its instructions) was hidden -- or a
# saved build's fingerprint (`load`).
RETIRED_SECTIONS = {
    "annotate": "is retired, with the tool-call purposes a model wrote; it is ignored",
    "export": "is retired, with the Search & export tab and its download; it is ignored",
    "ask": "is retired: Ask LLM was removed in this version, and the setting is ignored",
}
RETIRED_SECTION_ENV = {
    "RC_DASH_ANNOTATE": "annotate", "RC_DASH_ANNOTATE_PROVIDER": "annotate",
    "RC_DASH_ANNOTATE_MODEL": "annotate", "RC_DASH_ANNOTATE_BASE_URL": "annotate",
    "RC_DASH_ANNOTATE_KEY_ENV": "annotate", "RC_DASH_ANNOTATE_KEY_FILE": "annotate",
    "RC_DASH_ANNOTATE_CMD": "annotate",
    "RC_DASH_EXPORT_MAX_ROWS": "export",
    "RC_DASH_ASK": "ask", "RC_DASH_ASK_AGENT": "ask", "RC_DASH_ASK_MODEL": "ask",
    "RC_DASH_ASK_CMD": "ask", "RC_DASH_ASK_ENV_PASSTHROUGH": "ask",
    "RC_DASH_ASK_PROMPT_FILE": "ask",
}

# CLI dest -> leaf path
CLI_MAP = {
    "ebpf_root": "feeds.ebpf.roots",
    "ebpf_days": "feeds.ebpf.days",
    "node_root": "feeds.ebpf_node.roots",
    "live_window_min": "live.window_min",
    "retention_min": "live.retention_min",
    "backfill_hours": "live.backfill_hours",
    "as_of": "clock.as_of",
    "port": "server.port",
    "host": "server.host",
    "static_dir": "server.static_dir",
    "open": "server.auth.open",
}


def strip_comments(obj):
    """Drop every `//`-prefixed key, recursively, so the example file loads."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("//")}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def _get(tree, dotted, default=_MISSING):
    cur = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(tree, dotted, value):
    parts = dotted.split(".")
    cur = tree
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# Sections where an unknown key stops the service instead of being ignored: a
# misspelt key there would leave names unmasked or the page open.
STRICT = ("server", "anonymize")
# Keys the code reads with a default of its own, so not in defaults(): known,
# never warned about. Shell patterns over dotted paths; a key that matches is
# known with everything under it.
CODE_KEYS = ("live.user_*", "reducers.cascades", "reducers.workspace", "reducers.sessions",
             "reducers.trajectories.inspect_*", "risk.vars")


def _code_key(path):
    return any(fnmatch.fnmatchcase(path, pat) or path.startswith(pat + ".") for pat in CODE_KEYS)


def check_file(filetree, where):
    """The config file's tree checked against the settings there are, in place:
    a yes/no setting written as a string is read by `_flag` (`"false"` was
    true), or as 0 or 1.

    An unknown key under `server` or `anonymize` -- or a top-level key one slip
    away from one of them, `anonymise` -- stops the service, naming the nearest
    key there is, as does a section there given as a single value
    (`"anonymize": true`): each would leave names unmasked or the page open,
    without a word. Elsewhere an unknown key is a warning, since
    an old file may carry the keys of a feature since removed. Returns the
    warnings; raises SystemExit with every error."""
    errors, warnings = [], []

    def walk(node, dflt, prefix):
        for k in list(node):
            v, path = node[k], prefix + k
            strict = path.split(".")[0] in STRICT
            if k not in dflt:
                if _code_key(path):
                    continue
                if not prefix:
                    strict = strict or bool(difflib.get_close_matches(k, STRICT, 1, 0.7))
                near = difflib.get_close_matches(k, list(dflt), 1, 0.0 if strict else 0.6)
                (errors if strict else warnings).append("%s is not a setting%s" % (
                    path, "; the nearest is %s%s" % (prefix, near[0]) if near else ""))
                continue
            d = dflt[k]
            if isinstance(d, dict) and d:
                if isinstance(v, dict):
                    walk(v, d, path + ".")
                elif strict:
                    errors.append("%s: %s is not an object of its settings (%s)" % (
                        path, json.dumps(v), ", ".join(sorted(d))))
            elif isinstance(d, bool) and not isinstance(v, bool):
                if isinstance(v, str):
                    try:
                        node[k] = _flag(v)
                    except ValueError as e:
                        errors.append("%s: %s" % (path, e))
                elif type(v) is int and v in (0, 1):
                    node[k] = bool(v)
                else:
                    errors.append("%s: %s is not true or false" % (path, json.dumps(v)))

    walk(filetree, defaults(), "")
    if errors:
        raise SystemExit("config file %s:\n  %s" % (where, "\n  ".join(errors)))
    return warnings


# Sections no reduction reads, left out of the tree the saved builds' and hours'
# fingerprints are taken over (`as_dict()`, snapshots.fingerprint,
# hours.fingerprint): one added, or set, discards no saved state. Each new
# section of the kind belongs here rather than in those modules' own lists,
# which are in their code fingerprints.
UNREDUCED = ("status",)


class Config:
    """Resolved config plus, for every leaf, where its value came from."""

    def __init__(self, tree, origins, config_file):
        self._t = tree
        self._o = origins
        self.config_file = config_file

    def get(self, dotted, default=None):
        v = _get(self._t, dotted, _MISSING)
        return default if v is _MISSING else v

    def origin(self, dotted):
        return self._o.get(dotted, "default")

    def set(self, dotted, value, origin):
        """Override one leaf after load, naming who did (`--print-config` shows
        it). For a mode that must win over every source, as a demo's feed must."""
        _set(self._t, dotted, value)
        self._o[dotted] = origin

    @property
    def tree(self):
        return self._t

    def _leaves(self, node=None, prefix=""):
        node = self._t if node is None else node
        for k, v in node.items():
            path = "%s.%s" % (prefix, k) if prefix else k
            if isinstance(v, dict):
                yield from self._leaves(v, path)
            else:
                yield path, v

    def as_dict(self, with_origins=False):
        """The resolved tree. Without origins, the tree the saved states'
        fingerprints are taken over, less the sections no reduction reads
        (`UNREDUCED`); with them, every leaf as {_value, _origin}, and `_meta`."""
        if not with_origins:
            return {k: v for k, v in self._t.items() if k not in UNREDUCED}
        out = {}
        for path, val in self._leaves():
            entry = {"_value": val, "_origin": self.origin(path)}
            if path in HIDDEN and val:
                # With the salt, a list of candidate names turns every alias
                # back. The password hash is no use to a reader either.
                entry["_value"] = "(hidden)"
            if path.endswith(".roots") or path.endswith(".dir") or path.endswith(".path") \
                    or path == "status.transport_files":
                # what each names now: a pattern's matches (`expand_root`)
                cand = val if isinstance(val, list) else [val]
                entry["_existing"] = [m for p in cand if isinstance(p, str) and p
                                      for m in _existing(p, path.endswith(".roots"))]
            _set(out, path, entry)
        # Metadata joins the tree as proper {_value, _origin} leaves rather than
        # as bare scalars: a consumer walking this tree would otherwise have to
        # special-case two shapes, and a bare null reads as "descend into me".
        import sys as _s
        from . import about
        ver = about.version()
        out["_meta"] = {
            "config_file": {"_value": self.config_file,
                            "_origin": "cli/env" if self.config_file else "none"},
            "python": {"_value": _s.version.split()[0], "_origin": "runtime"},
            # what is running: the package's version, and the commit it was
            # built from -- a build stamp, else the checkout's HEAD -- or null
            "version": {"_value": {"version": ver["version"], "commit": ver["commit"]},
                        "_origin": ver["origin"]},
            # the zone the page formats every time in: the service's own
            "tz": {"_value": about.zone(), "_origin": "runtime"},
            # the dashboard's own host, named when site.name is empty
            "host": {"_value": about.host(), "_origin": "runtime"},
        }
        return out


def _existing(path, folders=False):
    """What `path` names that exists: a glob pattern's matches (with `folders`,
    only those that are folders, as a root's are), or the path."""
    if is_pattern(path):
        found = sorted(glob.glob(os.path.expanduser(path)))
        return [p for p in found if os.path.isdir(p)] if folders else found
    return [path] if os.path.exists(path) else []


# The config files read when none is named, in `HERE` (dashboard/ in a checkout).
DEFAULT_FILES = ("dashboard.config.json", "dashboard.toml")


def find_config_file(explicit=None):
    """The config file to read: `--config`, else RC_DASH_CONFIG, else whichever
    of DEFAULT_FILES exists in dashboard/. Both existing is refused: which one is
    read must never be an accident of this function."""
    if explicit:
        return explicit
    if os.environ.get("RC_DASH_CONFIG"):
        return os.environ["RC_DASH_CONFIG"]
    there = [os.path.join(HERE, n) for n in DEFAULT_FILES if os.path.exists(os.path.join(HERE, n))]
    if len(there) > 1:
        newer = max(there, key=lambda p: os.stat(p).st_mtime)
        raise SystemExit(
            "both %s exist, and the service reads one config file: keep the one you edit "
            "-- %s was changed last -- and move the other away (`rc-dashboard init` writes "
            "dashboard.toml), or name one with --config or RC_DASH_CONFIG"
            % (" and ".join(there), os.path.basename(newer)))
    return there[0] if there else None


def _plain(node):
    """A TOML tree with its dates and times as the text they were written as, so
    every value is one JSON has: `clock.as_of = 2026-11-02T09:00:00` reads as
    "2026-11-02 09:00:00"."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, datetime.datetime):
        return node.isoformat(sep=" ")
    if isinstance(node, (datetime.date, datetime.time)):
        return node.isoformat()
    return node


def read_file(path):
    """A config file's tree, comments stripped: TOML for a `.toml` file, JSON
    otherwise. Stops the service, naming the file, when it does not parse or is
    not a table of settings."""
    toml = path.lower().endswith(".toml")
    try:
        if toml:
            with open(path, "rb") as fh:
                tree = _plain(tomllib.load(fh))
        else:
            with open(path) as fh:
                tree = json.load(fh)
    except (OSError, ValueError) as e:          # tomllib.TOMLDecodeError is a ValueError
        raise SystemExit("config file %s: %s" % (path, e))
    if not isinstance(tree, dict):
        raise SystemExit("config file %s: not a JSON object" % path)
    return strip_comments(tree)


# Settings that name a file or a folder. In a config file, a relative one is
# under the repository's root -- `dashboard/.cache`, `dashboard/web/dist` -- or,
# installed without a checkout, beside the file; `~` is a home. Not
# `risk.profile`, which has always been under dashboard/ (`load`).
PATH_KEYS = ("feeds.ebpf.roots", "feeds.ebpf_node.roots", "cache.dir", "history.dir",
             "rollup.dir", "server.static_dir", "server.auth.password_hash_file",
             "server.auth.proxy_secret_file", "status.transport_files")


def path_base(config_file):
    """What a relative path in `config_file` is under (PATH_KEYS)."""
    return REPO if CHECKOUT else os.path.dirname(os.path.abspath(config_file))


def _resolve_paths(filetree, config_file):
    """PATH_KEYS in a config file's tree made absolute, in place (an empty value
    stays empty: it means the default)."""
    base = path_base(config_file)

    def one(p):
        if not isinstance(p, str) or not p:
            return p
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    for key in PATH_KEYS:
        val = _get(filetree, key, _MISSING)
        if val is _MISSING:
            continue
        _set(filetree, key, [one(p) for p in val] if isinstance(val, list) else one(val))


def load(args=None, config_path=None):
    """Build the resolved Config.  `args` is an argparse Namespace or None."""
    import sys
    tree = defaults()
    origins = {}

    path = find_config_file(config_path)
    if path and not os.path.exists(path):
        # Only a file someone named gets here (the default is used when it
        # exists): starting on the defaults instead would not be what they asked.
        raise SystemExit("config file %s, named by %s, does not exist" % (
            path, "--config" if config_path else "RC_DASH_CONFIG"))
    if path:
        filetree = read_file(path)
        # dropped, not merged: a retired feed's block, or a removed feature's
        # section, would show in /api/config and in every saved build's
        # fingerprint, while nothing reads it
        file_feeds = filetree.get("feeds")
        for name in RETIRED_FEEDS:
            if isinstance(file_feeds, dict) and name in file_feeds:
                del file_feeds[name]
                sys.stderr.write("warning: config file %s: feeds.%s %s\n"
                                 % (path, name, RETIRED_WHY))
        for name, why in RETIRED_SECTIONS.items():
            if name in filetree:
                del filetree[name]
                sys.stderr.write("warning: config file %s: %s %s\n" % (path, name, why))
        for w in check_file(filetree, path):
            sys.stderr.write("warning: config file %s: %s; it is ignored\n" % (path, w))
        # relative paths in the file resolve against the repo, absolute stand
        _resolve_paths(filetree, path)
        flat = {}

        def walk(node, prefix=""):
            for k, v in node.items():
                p = "%s.%s" % (prefix, k) if prefix else k
                if isinstance(v, dict):
                    walk(v, p)
                else:
                    flat[p] = v
        walk(filetree)
        for p, v in flat.items():
            origins[p] = "file:%s" % path
        _deep_merge(tree, filetree)

    for var in RETIRED_ENV:
        if os.environ.get(var):
            sys.stderr.write("warning: %s %s\n" % (var, RETIRED_WHY))
    for var, name in RETIRED_SECTION_ENV.items():
        if os.environ.get(var):
            sys.stderr.write("warning: %s %s\n" % (var, RETIRED_SECTIONS[name]))

    for dotted, (var, coerce) in ENV_MAP.items():
        raw = os.environ.get(var)
        if raw:
            try:
                val = coerce(raw)
            except (TypeError, ValueError) as e:
                raise SystemExit("%s: %s" % (var, e))
            _set(tree, dotted, val)
            origins[dotted] = "env:%s" % var

    if args is not None:
        for dest in RETIRED_CLI:
            if getattr(args, dest, None) is not None:
                sys.stderr.write("warning: --%s %s\n" % (dest.replace("_", "-"), RETIRED_WHY))
        for dest, dotted in CLI_MAP.items():
            val = getattr(args, dest, None)
            if val is None or val == []:
                continue        # argparse default == absent, never an override
            _set(tree, dotted, val)
            origins[dotted] = "cli:--%s" % dest.replace("_", "-")

    # A risk profile: a cluster's rules in one file, merged over the `risk`
    # section. Read here, not by the reducer, so its CONTENT is in the resolved
    # config -- and so in every saved window's fingerprint, where a path alone
    # would let changed rules resume old counts.
    prof = _get(tree, "risk.profile", "")
    if prof:
        pth = prof if os.path.isabs(prof) else os.path.join(HERE, prof)
        if not CHECKOUT and not os.path.exists(pth) and not os.path.isabs(prof):
            # installed from a wheel, which carries risk-profiles/ in the package
            pth = os.path.join(PKG, prof)
        try:
            with open(pth) as fh:
                ptree = strip_comments(json.load(fh))
        except (OSError, ValueError) as e:
            raise SystemExit("risk.profile %s: %s" % (pth, e))
        ptree.pop("profile", None)
        _deep_merge(tree["risk"], ptree)

        def mark(node, prefix):
            for k, v in node.items():
                if isinstance(v, dict) and v:
                    mark(v, prefix + "." + k)
                else:
                    origins[prefix + "." + k] = "profile:%s" % pth
        mark(ptree, "risk")

    # The processes reducing the hours (hours.py): a count, or the service
    # refuses to start rather than guess.
    workers = _get(tree, "ingest.workers", INGEST_WORKERS)
    if type(workers) is not int or not 0 <= workers <= 256:
        raise SystemExit("ingest.workers: %r is not a process count 0..256 (0: one per CPU this "
                         "process may use; 1: one pass in this process) (set by %s)" % (
                             workers, origins.get("ingest.workers", "default")))
    groups = _get(tree, "ingest.reducer_groups", INGEST_REDUCER_GROUPS)
    if type(groups) is not int or groups not in (1, 2):
        raise SystemExit("ingest.reducer_groups: %r is not 1 or 2 (set by %s)" % (
            groups, origins.get("ingest.reducer_groups", "default")))

    # Who may connect (guard.py), and whom the proxy sign-in believes (auth.Proxy):
    # lists of names, or the service refuses to start rather than take a string
    # for a list of its letters. What each name must be, auth.proxy_from_config says.
    for key in ("server.allowed_origins", "server.dev_origins", "server.allowed_hosts",
                "server.auth.proxy_groups", "server.auth.proxy_trusted"):
        val = _get(tree, key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise SystemExit("%s: %r is not a list of names (set by %s)" % (
                key, val, origins.get(key, "default")))
    for key in ("server.auth.proxy_header", "server.auth.proxy_groups_header",
                "server.auth.proxy_logout_url", "server.auth.proxy_secret_file"):
        val = _get(tree, key, "")
        if not isinstance(val, str):
            raise SystemExit("%s: %r is not text (set by %s)" % (
                key, val, origins.get(key, "default")))
    # The parents each own-I/O join holds (reducers/own_io.py): a count of one
    # or more, or the service refuses to start rather than guess -- 0 is neither
    # "unbounded" nor "no join". Null is the default.
    key = "reducers.own_io.max_parents"
    cap = _get(tree, key, OWN_IO_MAX_PARENTS)
    if cap is None:
        _set(tree, key, OWN_IO_MAX_PARENTS)
    elif type(cap) is not int or cap < 1:
        raise SystemExit("%s: %r is not a count of parents, 1 or more (set by %s)" % (
            key, cap, origins.get(key, "default")))

    # What makes a session's Slurm activity abnormal (reducers/slurm.py): each a
    # count of 1 or more, or the service refuses to start -- 0 would flag every
    # session, and a string would compare with nothing.
    for name in ("poll_gap_s", "poll_min_s", "query_burst", "submit_burst", "refused",
                 "cancels", "failed_runs"):
        key = "reducers.slurm.abnormal." + name
        val = _get(tree, key, None)
        if type(val) is not int or val < 1:
            raise SystemExit("%s: %r is not a count, 1 or more (set by %s)" % (
                key, val, origins.get(key, "default")))

    # The history store (history.py): a switch, a directory, a worker count and
    # a day bound, or the service refuses to start rather than guess.
    hist = _get(tree, "history", None)
    if not isinstance(hist, dict):
        raise SystemExit("history: %r is not an object (set by %s)" % (
            hist, origins.get("history", "default")))
    for key, ok, what in (
            ("enabled", lambda v: isinstance(v, bool), "true or false"),
            ("dir", lambda v: v is None or isinstance(v, str), "a path, or empty"),
            ("days", lambda v: type(v) is int and 0 <= v <= 100000,
             "a count of dated dirs 0..100000 (0: every one the source holds)"),
            ("workers", lambda v: type(v) is int and 0 <= v <= 256,
             "a process count 0..256 (0: up to 8, as the CPUs allow)")):
        dotted = "history." + key
        val = hist.get(key, HISTORY[key])
        if not ok(val):
            raise SystemExit("%s: %r is not %s (set by %s)" % (
                dotted, val, what, origins.get(dotted, "default")))
        hist[key] = "" if (key == "dir" and val is None) else val

    # What `status` reads (status.py): a list of paths or patterns, and a number
    # of seconds, or the service refuses to start rather than take a string for
    # a list of its letters.
    val = _get(tree, "status.transport_files", [])
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise SystemExit("status.transport_files: %r is not a list of paths or glob patterns "
                         "(set by %s)" % (val, origins.get("status.transport_files", "default")))
    val = _get(tree, "status.stale_s", 900)
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not val > 0:
        raise SystemExit("status.stale_s: %r is not a number of seconds above 0 (set by %s)" % (
            val, origins.get("status.stale_s", "default")))
    val = _get(tree, "server.auth.password_hash_file", "")
    if not isinstance(val, str):
        raise SystemExit("server.auth.password_hash_file: %r is not a path (set by %s)" % (
            val, origins.get("server.auth.password_hash_file", "default")))

    # The roll-up table (rollup.py): a switch and a directory.
    roll = _get(tree, "rollup", None)
    if not isinstance(roll, dict):
        raise SystemExit("rollup: %r is not an object (set by %s)" % (
            roll, origins.get("rollup", "default")))
    for key, ok, what, dflt in (
            ("enabled", lambda v: isinstance(v, bool), "true or false", True),
            ("dir", lambda v: v is None or isinstance(v, str), "a path, or empty", "")):
        dotted = "rollup." + key
        val = roll.get(key, dflt)
        if not ok(val):
            raise SystemExit("%s: %r is not %s (set by %s)" % (
                dotted, val, what, origins.get(dotted, "default")))
        roll[key] = "" if (key == "dir" and val is None) else val

    # candidate-probe annotation for the multi-root feeds
    for feed in ("ebpf", "ebpf_node"):
        key = "feeds.%s.roots" % feed
        if origins.get(key):
            continue
        cand = _get(tree, key, [])
        hit = next((i for i, p in enumerate(cand) if os.path.isdir(p)), None)
        origins[key] = ("default(candidate %d/%d)" % (hit + 1, len(cand))
                        if hit is not None else "default(no candidate exists)")

    return Config(tree, origins, path)


# ------------------------------------------------------------- the reference
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


_TOML_ESC = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _toml_str(s):
    """`s` as a TOML basic string: `"`, `\\` and the control characters TOML forbids as
    they are (U+0000-U+001F, U+007F) escaped, every other character as itself. json.dumps
    did this once, but escapes a character past U+FFFF as a UTF-16 pair (`\\ud83d\\ude80`
    for 🚀), which TOML refuses: a site named with an emoji wrote a config that would not
    load."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch in _TOML_ESC:
            out.append(_TOML_ESC[ch])
        elif o < 0x20 or o == 0x7F:
            out.append("\\u%04x" % o)
        elif 0xD800 <= o <= 0xDFFF:
            raise ValueError("%r holds bytes that are not UTF-8, which a TOML file cannot" % s)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(k):
    return k if _BARE_KEY.fullmatch(k) else _toml_str(k)


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join("%s = %s" % (_toml_key(k), _toml_value(x))
                               for k, x in v.items()) + "}"
    raise TypeError("no TOML for %r" % (v,))


def _is_table(v):
    """Written as a [table] of its own: a section with something in it."""
    return isinstance(v, dict) and bool(v)


def _is_array_of_tables(v):
    """Written as [[tables]]: a list of sections (risk.sections)."""
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


# The settings a site chooses, each with its line of help: the ones the README's table,
# `init`'s starter and `print-config --reference` name. Every other key is internal: it
# loads from the file, a variable or a flag as it always did, but it tunes the reducers,
# caches and workers, and its default is the answer; `print-config --reference --all`
# lists every one. A new setting is internal unless every site must choose it.
PUBLIC = (
    ("feeds.ebpf.roots", "where the collector's eBPF tier writes (its EBPFM_OUTPUT_DIR); "
                         "every candidate that exists is read, and a glob may name many"),
    ("feeds.ebpf_node.roots", "where its node tier writes (EBPFM_NODE_OUTPUT_DIR), the same way"),
    ("site.name", "the name every page and the sign-in form show; empty is this host's name"),
    ("site.description", "a few words on what it watches, shown under the name"),
    ("slurm.enabled", "the HPC tab, and reducing for it: false on servers without Slurm"),
    ("ui.node_noun", "what every page calls the hosts, such as \"servers\""),
    ("anonymize.enabled", "an alias for every user name, on every way out of the service"),
    ("filters.drop_global", "accounts whose every record is dropped, such as the deployment's own"),
    ("live.retention_min", "minutes held in memory, and the widest window the page offers: "
                           "more costs memory and a longer refill"),
    ("history.days", "the newest days kept as Parquet beside the day-files: 0 keeps every one"),
    ("cache.dir", "where the saved builds, the history store, the roll-up table and the "
                  "anonymization salt are kept"),
    ("risk.profile", "a cluster's risk rules, merged over the built-in ones: a file under "
                     "dashboard/, such as risk-profiles/gpu-dev.json"),
    ("clock.as_of", "the present pinned for a capture from elsewhere: a local time, or auto "
                    "for its newest record; empty is the wall clock"),
    ("server.host", "the address to listen on: 127.0.0.1 behind a proxy or an ssh tunnel"),
    ("server.port", "the port to listen on"),
    ("server.allowed_hosts", "names the page may be reached by besides loopback and "
                             "server.host, such as a proxy's"),
    ("server.auth.password_hash_file", "the password's hash (rc-dashboard new-password "
                                       "--install), read again whenever it changes"),
    ("server.auth.open", "no password: only where every account that can reach the port may "
                         "read the data"),
    ("server.auth.proxy_header", "the header a reverse proxy names the signed-in user in, "
                                 "for the site's single sign-on; empty is off"),
    ("server.auth.proxy_secret_file", "the secret that proxy sends on every request, which a "
                                      "request needs for its user header to count"),
)
PUBLIC_KEYS = tuple(k for k, _ in PUBLIC)


def is_public(dotted):
    return dotted in PUBLIC_KEYS


def public_view(tree):
    """print-config's tree, {_value, _origin} leaves, cut to what a site reads: every public
    setting, each internal one that is not at its default, and `_meta`. Returns it and how
    many internal settings were left out."""
    left = [0]

    def walk(node, prefix):
        out = {}
        for k, v in node.items():
            path = "%s.%s" % (prefix, k) if prefix else k
            if path == "_meta":
                out[k] = v
            elif isinstance(v, dict) and "_origin" in v and "_value" in v:
                if is_public(path) or v["_origin"] != "default":
                    out[k] = v
                else:
                    left[0] += 1
            elif isinstance(v, dict):
                sub = walk(v, path)
                if sub:
                    out[k] = sub
        return out
    return walk(tree, ""), left[0]


def reference_toml(everything=False):
    """The settings a site chooses (`PUBLIC`), each with its line of help and its built-in
    default on this host, as a TOML config file would set it, each with the RC_DASH_*
    variable that sets it too: `rc-dashboard print-config --reference`; with `everything`
    (`--all`), every setting there is. Made from `defaults()`, so it cannot drift from the
    code (tests/test_config.py reads it back). A setting whose default is unset (null,
    which TOML cannot write) is a comment."""
    env_of = {k: var for k, (var, _c) in ENV_MAP.items()}
    helps = dict(PUBLIC)
    tree = defaults()
    if not everything:
        cut = {}
        for path in PUBLIC_KEYS:
            _set(cut, path, _get(tree, path))
        internal = sum(1 for _ in _leaf_paths(tree)) - len(PUBLIC_KEYS)
        tree = cut
    out = ["# %s on this host."
           % ("Every rc-dashboard setting, with its built-in default" if everything
              else "The settings a site chooses, each with its built-in default"),
           "# Generated by `rc-dashboard print-config --reference%s`; dashboard/README.md"
           % (" --all" if everything else ""),
           "# says more. Set any of them in dashboard/dashboard.toml (or the file --config or",
           "# RC_DASH_CONFIG names): a key left out keeps its default, and per key a flag",
           "# beats its RC_DASH_* variable, which beats the file. A relative path in the",
           "# file is under the repository's root."]
    if not everything:
        out += ["# %d more settings are internal: they tune the reducers, caches and workers,"
                % internal,
                "# and their defaults are the answer. `--all` lists them."]

    def emit(node, prefix):
        scalars = [(k, v) for k, v in node.items()
                   if not _is_table(v) and not _is_array_of_tables(v)]
        if prefix and scalars:
            out.extend(["", "[%s]" % ".".join(_toml_key(p) for p in prefix)])
        for k, v in scalars:
            dotted = ".".join(prefix + [k])
            var = env_of.get(dotted)
            note = "  # %s" % var if var else ""
            if dotted in helps:
                out.extend("# " + line for line in textwrap.wrap(helps[dotted], 86))
            if v is None:
                out.append("# %s = (unset)%s" % (_toml_key(k), note))
            else:
                out.append("%s = %s%s" % (_toml_key(k), _toml_value(v), note))
        for k, v in node.items():
            if _is_table(v):
                emit(v, prefix + [k])
        for k, v in node.items():
            if _is_array_of_tables(v):
                for item in v:
                    out.extend(["", "[[%s]]" % ".".join(_toml_key(p) for p in prefix + [k])])
                    out.extend("%s = %s" % (_toml_key(kk), _toml_value(vv))
                               for kk, vv in item.items())

    emit(tree, [])
    return "\n".join(out) + "\n"


def _leaf_paths(node, prefix=""):
    """Every setting's dotted name in a defaults() tree: a section with something in it is
    no setting, its leaves are."""
    for k, v in node.items():
        path = "%s.%s" % (prefix, k) if prefix else k
        if _is_table(v):
            yield from _leaf_paths(v, path)
        else:
            yield path
