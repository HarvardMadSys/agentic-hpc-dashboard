"""Resolved configuration: CLI > env > config file > built-in default.

JSON, not TOML.  The service carries its own uv venv so `tomllib` would be
available to *it*, but the format is shared with the rest of the repo -- every
collector writes JSONL and the retired builder's contract was `data.json` -- and
`--print-config` has to round-trip it.  Keys beginning `//` are comments and are
stripped on load, so the committed example file is itself a legal config.

Precedence is resolved PER LEAF, not per section, so `RC_DASH_EBPF_ROOTS` can
override one key without restating the rest of `feeds.ebpf`.  Every CLI flag
defaults to None and is treated as absent when None -- an argparse default would
otherwise silently outrank the config file.

Path defaults are CANDIDATE LISTS, probed in order against the filesystem.  A
feed reports what it looked for as well as what it found, which is what the UI's
empty states name.
"""
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MISSING = object()


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v else default


def _pathsep(v):
    return [p for p in (v or "").split(os.pathsep) if p]


def _csv(v):
    return [p.strip() for p in (v or "").split(",") if p.strip()]


# ---------------------------------------------------------------- defaults
def _ebpf_candidates():
    """Where the root eBPF tier's day-files might live, most specific first.

    `EBPFM_LOG_ROOT` before `EBPFM_OUTPUT_DIR` deliberately: collect/healthcheck.py
    uses the distinct LOG_ROOT name so an operator shell that repoints the
    collector cannot silently repoint the readers too.  The repo symlink
    `log/ebpfm -> ../data/ebpfm/logs` already contains `/logs`, so the repo-side
    layout is `log/ebpfm/<date>/<host>.jsonl`.
    """
    c = []
    for e in ("EBPFM_LOG_ROOT", "EBPFM_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    base = _env("RC_MEASUREMENT_ROOT")
    if base:
        c.append(os.path.join(base, "ebpfm", "logs"))
    c += [os.path.join(REPO, "log", "ebpfm"),
          os.path.join(REPO, "log", "ebpfm", "logs"),
          "/var/log/ebpfm",
          os.path.expanduser("~/.ebpfm/ebpf")]
    return c


def _node_candidates():
    """The unprivileged node tier.  `log/login` is last but load-bearing:
    node_snapshot.py is a vendored copy of collect/common/snapshot.py, so the
    existing login feed is schema-compatible and works before `ebpfm --node` is
    deployed anywhere."""
    c = []
    for e in ("EBPFM_NODE_LOG_ROOT", "EBPFM_NODE_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    base = _env("RC_MEASUREMENT_ROOT")
    if base:
        c.append(os.path.join(base, "ebpfm", "node", "logs"))
    c += [os.path.join(REPO, "log", "ebpfm_node"),
          "/var/log/ebpfm-login",
          os.path.expanduser("~/.ebpfm/login"),
          os.path.join(REPO, "log", "login")]
    return c


def defaults():
    base = _env("RC_MEASUREMENT_ROOT")
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
                "max_age_s": 900,
                "required": False,
            },
            "sacct": {
                "dir": REPO,
                "pattern": "SlurmData_*.csv",
                "max_days": 0,
                "jobs_csv": None,
                "required": False,
            },
            "sacctmgr": {"dir": REPO, "pattern": "sacctmgr_*.jsonl", "required": False},
            "domains": {"path": os.path.join(REPO, "dataclean", "mapping", "map_user.csv"),
                        "required": False},
            "user_actor": {"path": os.path.join(REPO, "dataclean", "user.csv"),
                           "required": False},
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
            "event_tail": 500,
            "submit_tail": 50,
            # Fill retention, not a day of it.  Read on EVERY start, newest
            # first, from the collector's files -- the bins are never restored
            # from anything this service wrote -- so the page cannot depend on
            # whether the service was running while the data was collected.
            # 0 starts empty and fills forward.
            "backfill_hours": 168,
            "clock_skew_s": 120,
        },
        "ingest": {"poll_ms": 1000},
        # The service's present. "" is the wall clock; "auto" or a local time
        # pins (and freezes) it, for a feed captured elsewhere and copied here.
        # See clock.py.
        "clock": {"as_of": ""},
        "rebuild": {"interval_s": 900},
        # A cluster without Slurm turns this off and the Slurm tab goes away;
        # nothing else depends on it (the tool mix keeps its two scheduler
        # kinds, which simply stay empty there).
        "slurm": {"enabled": True},
        # Chat with an LLM about the window on screen (ask.py). Each question
        # runs the agent CLI on this host as the service's user -- the preset
        # `codex` uses that user's own Codex login -- with a compact, masked
        # snapshot of the window's panels. That snapshot leaves the host.
        "ask": {
            "enabled": True,
            "agent": "codex",          # a preset (ask.PRESETS), or set `command`
            # argv, or one shell-style string; {workdir} {answer_file} {model}
            # are filled in, the prompt arrives on stdin, and with no
            # {answer_file} the command's stdout is the answer
            "command": None,
            "model": "",               # empty: the agent's own default
            "timeout_s": 240,
            "max_concurrent": 1,       # a second question waits, not a second agent
            "context_kb": 120,         # the snapshot's size budget
            "include_commands": False,  # command lines stay out unless set
            # The answerer's instructions. Empty: the built-in (ask.DEFAULT_SYSTEM).
            # A file is re-read on every question, so it can be tuned live and
            # wins over the inline text. {deployment} {site} {window} are filled in.
            "system_prompt": "",
            "system_prompt_file": "",
            # the short map of where things are in the snapshot, sent after the
            # system prompt; turn off only if your prompt describes the data itself
            "snapshot_guide": True,
            # every row the model is given carries a ref it cites after each
            # figure, and the page links each to its row (ask.CITE_GUIDE)
            "citations": True,
        },
        # Which deployment this is, on every page: two dashboards side by side
        # -- a frozen capture from one cluster, the live feed of another -- must
        # not be mistaken for each other.
        # `description`: what this deployment watches, in a few words ("FASRC
        # login nodes", "a GPU workstation"). The chat agent is told it rather
        # than assuming any kind of machine; empty says nothing.
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
        },
        "filters": {
            "drop_global": ["juncheng"],     # the monitoring operator
            "drop_agent": ["flamraoui"],     # period3 agent build skew
            "classes": ["agent", "human", "ide"],
            "unlabeled_class": "unlabeled",
            # the collector's own probes (node_snapshot's mpstat/iostat/sar/
            # nfsiostat/nvidia-smi shell-outs). Counted, not silent.
            "drop_self_observation": True,
        },
        "reducers": {
            "tool_mix": {"head_n": 12, "dedup_sessions_max": 200000},
            "io": {"top_n": 20},
            # `live_s`: how stale the ranked sessions may get while the view
            # is fed live -- the panel re-reads them that often, and they are
            # re-ranked on the read, not only at the next rebuild
            "trajectories": {"top_n": 15, "max_runs_shown": 60, "purpose_seq_cap": 80,
                             "idle_evict_s": 7200, "max_sessions": 50000,
                             "include_key_types": ["apid", "sess", "tty"], "live_s": 5},
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
                 "title": "Credentials exposed in argv"},
            ],
        },
        "export": {"max_rows": 5000000},
        # Replace every username, lab or Slurm account name, email address and
        # numeric user ID in what the service sends out with a stable alias
        # (anonymize.py). Off by default: a deployment for the cluster's own
        # staff shows real names unless told otherwise. `salt` empty: one is
        # generated into <cache.dir>/anonymize.salt and reused; changing it
        # reshuffles every alias. The patterns name where homes and lab trees
        # sit on this cluster -- see anonymize.HOME_PATTERNS for the defaults.
        # What the page opens on, before a reader changes it. Nothing here
        # changes a number, so the saved builds ignore the section.
        # user_columns: the Active users table's columns (agents, ides, running,
        # exited, cpu, read, written, sent, received, gpu, last; `net` is sent
        # and received); a reader's own choice is kept in their browser.
        "ui": {
            # What the hosts this dashboard watches are called on the page:
            # "Login nodes right now". A workstation fleet says "hosts".
            "node_noun": "login nodes",
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
        "cache": {"dir": os.path.join(HERE, ".cache"),
                  "reducer_version": 3},
        "server": {"host": "127.0.0.1", "port": 8080,
                   "static_dir": os.path.join(HERE, "web", "dist")},
    }


# leaf path -> (env var, coercion).  Flat RC_DASH_* names, per repo convention.
ENV_MAP = {
    "feeds.ebpf.roots": ("RC_DASH_EBPF_ROOTS", _pathsep),
    "feeds.ebpf.days": ("RC_DASH_EBPF_DAYS", int),
    "feeds.ebpf.max_age_s": ("RC_DASH_EBPF_MAX_AGE_S", int),
    "feeds.ebpf_node.roots": ("RC_DASH_NODE_ROOTS", _pathsep),
    "feeds.ebpf_node.days": ("RC_DASH_NODE_DAYS", int),
    "feeds.sacct.dir": ("RC_DASH_SACCT_DIR", str),
    "feeds.sacct.pattern": ("RC_DASH_SACCT_PATTERN", str),
    "feeds.sacct.max_days": ("RC_DASH_SACCT_MAX_DAYS", int),
    "feeds.sacct.jobs_csv": ("RC_DASH_JOBS_CSV", str),
    "feeds.sacctmgr.dir": ("RC_DASH_SACCTMGR_DIR", str),
    "feeds.domains.path": ("RC_DASH_DOMAINS_CSV", str),
    "feeds.user_actor.path": ("RC_DASH_USER_CSV", str),
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
    "panels.cache_windows": ("RC_DASH_CACHE_WINDOWS",
                             lambda v: v not in ("0", "false", "no")),
    "panels.merge_reduce": ("RC_DASH_MERGE_REDUCE",
                            lambda v: v not in ("0", "false", "no")),
    "ingest.poll_ms": ("RC_DASH_POLL_MS", int),
    "clock.as_of": ("RC_DASH_AS_OF", str),
    "rebuild.interval_s": ("RC_DASH_REBUILD_S", int),
    "filters.drop_global": ("RC_DASH_DROP_USERS", _csv),
    "filters.drop_agent": ("RC_DASH_DROP_AGENT_USERS", _csv),
    "filters.drop_self_observation": ("RC_DASH_DROP_SELF_OBS",
                                      lambda v: v not in ("0", "false", "no")),
    "export.max_rows": ("RC_DASH_EXPORT_MAX_ROWS", int),
    "risk.profile": ("RC_DASH_RISK_PROFILE", str),
    "slurm.enabled": ("RC_DASH_SLURM", lambda v: v not in ("0", "false", "no", "off")),
    "ask.enabled": ("RC_DASH_ASK", lambda v: v not in ("0", "false", "no", "off")),
    "anonymize.enabled": ("RC_DASH_ANONYMIZE",
                          lambda v: v not in ("0", "false", "no", "off")),
    "ask.agent": ("RC_DASH_ASK_AGENT", str),
    "ask.model": ("RC_DASH_ASK_MODEL", str),
    "ask.command": ("RC_DASH_ASK_CMD", str),
    "ask.system_prompt_file": ("RC_DASH_ASK_PROMPT_FILE", str),
    "site.name": ("RC_DASH_SITE_NAME", str),
    "ui.node_noun": ("RC_DASH_NODE_NOUN", str),
    "site.description": ("RC_DASH_SITE_DESC", str),
    "cache.dir": ("RC_DASH_CACHE_DIR", str),
    "server.host": ("RC_DASH_HOST", str),
    "server.port": ("RC_DASH_PORT", int),
    "server.static_dir": ("RC_DASH_STATIC_DIR", str),
}

# retired names still honoured, with a one-line warning
ALIASES = {"RC_JOBS_CSV": "RC_DASH_JOBS_CSV", "RC_USER_CSV": "RC_DASH_USER_CSV"}

# CLI dest -> leaf path
CLI_MAP = {
    "ebpf_root": "feeds.ebpf.roots",
    "ebpf_days": "feeds.ebpf.days",
    "node_root": "feeds.ebpf_node.roots",
    "sacct_dir": "feeds.sacct.dir",
    "jobs_csv": "feeds.sacct.jobs_csv",
    "live_window_min": "live.window_min",
    "retention_min": "live.retention_min",
    "backfill_hours": "live.backfill_hours",
    "as_of": "clock.as_of",
    "port": "server.port",
    "host": "server.host",
    "static_dir": "server.static_dir",
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
        if not with_origins:
            return self._t
        out = {}
        for path, val in self._leaves():
            entry = {"_value": val, "_origin": self.origin(path)}
            if path == "anonymize.salt" and val:
                # With the salt, a list of candidate names turns every alias back.
                entry["_value"] = "(hidden)"
            if path.endswith(".roots") or path.endswith(".dir") or path.endswith(".path"):
                cand = val if isinstance(val, list) else [val]
                entry["_existing"] = [p for p in cand if p and os.path.exists(p)]
            _set(out, path, entry)
        # Metadata joins the tree as proper {_value, _origin} leaves rather than
        # as bare scalars: a consumer walking this tree would otherwise have to
        # special-case two shapes, and a bare null reads as "descend into me".
        import sys as _s
        out["_meta"] = {
            "config_file": {"_value": self.config_file,
                            "_origin": "cli/env" if self.config_file else "none"},
            "python": {"_value": _s.version.split()[0], "_origin": "runtime"},
        }
        return out


def find_config_file(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("RC_DASH_CONFIG"):
        return os.environ["RC_DASH_CONFIG"]
    p = os.path.join(HERE, "dashboard.config.json")
    return p if os.path.exists(p) else None


def load(args=None, config_path=None):
    """Build the resolved Config.  `args` is an argparse Namespace or None."""
    import sys
    tree = defaults()
    origins = {}

    path = find_config_file(config_path)
    if path and os.path.exists(path):
        with open(path) as fh:
            filetree = strip_comments(json.load(fh))
        # relative paths in the file resolve against the repo, absolute stand
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

    for alias, real in ALIASES.items():
        if os.environ.get(alias) and not os.environ.get(real):
            sys.stderr.write("warning: %s is retired; use %s\n" % (alias, real))
            os.environ[real] = os.environ[alias]

    for dotted, (var, coerce) in ENV_MAP.items():
        raw = os.environ.get(var)
        if raw:
            _set(tree, dotted, coerce(raw))
            origins[dotted] = "env:%s" % var

    if args is not None:
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
