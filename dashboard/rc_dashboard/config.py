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
                "days": 2,
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
            "window_min": 1440,      # 24h, matching the Overview grain
            "bin_s": 60,
            "event_tail": 500,
            "submit_tail": 50,
            "backfill_hours": 24,
            "clock_skew_s": 120,
        },
        "ingest": {"poll_ms": 1000},
        "rebuild": {"interval_s": 900},
        "filters": {
            "drop_global": ["juncheng"],     # the monitoring operator
            "drop_agent": ["flamraoui"],     # period3 agent build skew
            "classes": ["agent", "human-vscode", "human"],
            "unlabeled_class": "unlabeled",
            # the collector's own probes (node_snapshot's mpstat/iostat/sar/
            # nfsiostat/nvidia-smi shell-outs). Counted, not silent.
            "drop_self_observation": True,
        },
        "reducers": {
            "tool_mix": {"head_n": 12, "dedup_sessions_max": 200000},
            "io": {"top_n": 20},
            "trajectories": {"top_n": 15, "max_runs_shown": 60, "purpose_seq_cap": 80,
                             "idle_evict_s": 7200, "max_sessions": 50000,
                             "include_key_types": ["apid", "sess", "tty"]},
        },
        "export": {"max_rows": 5000000},
        "cache": {"dir": os.path.join(HERE, ".cache"),
                  "state_dir": os.path.join(HERE, ".state"),
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
    "live.window_min": ("RC_DASH_LIVE_WINDOW_MIN", int),
    "live.bin_s": ("RC_DASH_LIVE_BIN_S", int),
    "live.event_tail": ("RC_DASH_LIVE_EVENT_TAIL", int),
    "live.backfill_hours": ("RC_DASH_BACKFILL_HOURS", int),
    "ingest.poll_ms": ("RC_DASH_POLL_MS", int),
    "rebuild.interval_s": ("RC_DASH_REBUILD_S", int),
    "filters.drop_global": ("RC_DASH_DROP_USERS", _csv),
    "filters.drop_agent": ("RC_DASH_DROP_AGENT_USERS", _csv),
    "filters.drop_self_observation": ("RC_DASH_DROP_SELF_OBS",
                                      lambda v: v not in ("0", "false", "no")),
    "export.max_rows": ("RC_DASH_EXPORT_MAX_ROWS", int),
    "cache.dir": ("RC_DASH_CACHE_DIR", str),
    "cache.state_dir": ("RC_DASH_STATE_DIR", str),
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
    "backfill_hours": "live.backfill_hours",
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
