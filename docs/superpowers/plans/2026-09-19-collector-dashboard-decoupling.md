# Collector/Dashboard Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard correct and honest against a live collector directory, a periodically re-synced replica and a frozen archive, and remove every reference to the measurement repository this tree was extracted from.

**Architecture:** `IngestSource` stays the seam; a new stateful `ModeDetector` classifies each feed as `live`/`replica`/`archive` from successive feed reports, and each mode gets the ingest strategy correct for it — inode-keyed tail, content-verified resume, or a single pass with a continuing `stat` sweep. Freshness becomes two independent clocks (file mtime, record timestamp) that are never merged. Separately, the `sys.path` reach into `analyze/`, the old repo's path defaults, its citations, and the inert vendoring-sync subsystem are removed, guarded by an executable check.

**Tech Stack:** Python 3.11+ (`fastapi`, `uvicorn`, stdlib `unittest`, `hashlib`), React 18 + TypeScript (Vite, no charting library), bash (`ebpfm.sh`).

**Spec:** `docs/superpowers/specs/2026-09-19-collector-dashboard-decoupling-design.md`

## Global Constraints

- **Null is not zero, and an absence must be visible.** A dropped record, an excluded record and a record that was never written are three different facts and must read as three different facts. Every drop is counted and reported, never silent.
- **Nothing measured is asserted without its basis.** A number on the page whose evidence is not in this repository does not belong on the page.
- **Scope is stated.** If the mode is a guess, the page says it is a guess.
- **Three actor classes, never two** — `agent` / `human-vscode` / `human`. `_fallback_actor3`'s rule (*must NOT degrade to the binary `actor`*) is preserved verbatim through any rename.
- **The wire value `COLLECTOR = 'ebpf_marthen_new'` (`collector/ebpf_trace.py:97`) is never renamed.** It is stamped into the JSONL envelope. Path references to the old name go; the wire value stays, and `collector/tests/test_ebpfm.py:732` pins it.
- **`sandbox` and `approval` are independent axes**, and `denied` is its own state, never folded into `unsandboxed`.
- **Dependencies stay as they are:** `fastapi`, `uvicorn` and nothing else for the service; `react`, `react-dom` and nothing else at runtime for the page. Tests use stdlib `unittest` only, matching `collector/tests/`.
- **Python floor is 3.11** (`dashboard/pyproject.toml`).
- Dashboard tests run from `dashboard/` as `python3 -m unittest discover -s tests -q`.
- Frontend checks are `npm run typecheck` and `npm run check:no-random`, run from `dashboard/web/`.

## File Structure

**Created**

| path | responsibility |
|---|---|
| `dashboard/tests/__init__.py` | marks the test package |
| `dashboard/tests/support.py` | shared fixtures: `FakeCfg`, JSONL day-file builders, `rsync`-style replace helpers |
| `dashboard/tests/test_selfcontained.py` | the executable form of Goal 2 — no old-repo references anywhere |
| `dashboard/tests/test_normalize.py` | `eff_src`, the promoted builtin classifiers, schema counting |
| `dashboard/tests/test_tail.py` | content-verified resume and the double-count regression |
| `dashboard/tests/test_modes.py` | `ModeDetector` classification and labelling |
| `dashboard/tests/test_feeds.py` | two clocks, mode-aware status |
| `dashboard/tests/test_buckets.py` | archive window anchoring |
| `dashboard/rc_dashboard/modes.py` | `ModeDetector` — the only place a mode is decided |

**Modified**

| path | change |
|---|---|
| `dashboard/rc_dashboard/normalize.py` | drop `sys.path`/`agent_lib` probing; promote builtins; add `eff_src` |
| `dashboard/rc_dashboard/config.py` | drop `RC_MEASUREMENT_ROOT`, `REPO`, old-layout candidates; add mode keys |
| `dashboard/rc_dashboard/sacct.py` | drop `REPO`-derived defaults |
| `dashboard/rc_dashboard/tail.py` | content-verified resume; `STATE_VERSION` 1 -> 2 |
| `dashboard/rc_dashboard/feeds.py` | `sync_lag_s`; mode-aware `status`; mode fields in `panel_meta` |
| `dashboard/rc_dashboard/buckets.py` | `add()` gains `now=None` |
| `dashboard/rc_dashboard/aggregator.py` | own the `ModeDetector`; skip ingest in archive mode; anchor the window |
| `dashboard/rc_dashboard/__main__.py` | `--mode` flag |
| `dashboard/rc_dashboard/reducers/{tools,trajectories,sandbox,resources}.py` | citations; two dropped claims |
| `dashboard/dashboard.config.example.json` | de-site-specify |
| `dashboard/web/src/api/types.ts` | mode + sync-clock fields |
| `dashboard/web/src/App.tsx` | mode-aware `headline()`; the banner |
| `dashboard/web/src/tabs/FeedsTab.tsx` | sync clock beside record clock |
| `dashboard/README.md`, `README.md` | modes, the rsync recipe, `SCHEMA_VERSION` |
| `collector/ebpfm.sh`, `collector/VENDORED.md` | remove the inert vendoring sync |

**Deleted:** `collector/.vendored.sha256`

---

# Phase A — Sever the old repository

Phase A runs first: it deletes `config.py` defaults that Phase B would otherwise
build on. Phase A is behaviour-preserving apart from two deliberate removals
(Task 4).

---

### Task 1: Test harness, and make `normalize.py` self-contained

**Files:**
- Create: `dashboard/tests/__init__.py`, `dashboard/tests/support.py`, `dashboard/tests/test_normalize.py`
- Modify: `dashboard/rc_dashboard/normalize.py:39` (drop `REPO`), `:99-135` (`_load_helpers`), `:254-266` (`Normalizer.__init__`)

**Interfaces:**
- Consumes: nothing.
- Produces: `dashboard/tests/support.py` exporting `FakeCfg(**overrides)` with a `.get(dotted, default=None)` method. `Normalizer` gains the attribute `eff_src: str` and the stats key `"eff_src"`.

- [ ] **Step 1: Create the test package and shared fixture**

`dashboard/tests/__init__.py` is empty. Create `dashboard/tests/support.py`:

```python
"""Shared test fixtures.  Stdlib only, matching collector/tests/."""
import os


class FakeCfg:
    """A Config stand-in.  Real `config.load()` probes for a config file on
    disk, which would make tests depend on the developer's working tree."""

    def __init__(self, **overrides):
        self._t = {
            "filters": {
                "drop_global": [],
                "drop_agent": [],
                "unlabeled_class": "unlabeled",
                "drop_self_observation": True,
            },
        }
        self._t.update(overrides)

    def get(self, dotted, default=None):
        node = self._t
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def origin(self, dotted):
        return "default"


def pkg_dir():
    """Absolute path of the rc_dashboard package."""
    import rc_dashboard
    return os.path.dirname(os.path.abspath(rc_dashboard.__file__))


def source_of(module_filename):
    """Read a module's own source, for structural assertions."""
    with open(os.path.join(pkg_dir(), module_filename)) as fh:
        return fh.read()
```

- [ ] **Step 2: Write the failing tests**

Create `dashboard/tests/test_normalize.py`:

```python
import unittest

from rc_dashboard.normalize import Normalizer
from tests.support import FakeCfg, source_of


class TestSelfContainedNormalize(unittest.TestCase):
    def test_no_sys_path_reaching(self):
        src = source_of("normalize.py")
        self.assertNotIn("sys.path.insert", src)
        self.assertNotIn("agent_lib", src)
        self.assertNotIn("purpose_lib", src)
        self.assertNotIn("deep_traj_lib", src)

    def test_eff_src_is_declared(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.eff_src, "rc_dashboard.builtin")

    def test_eff_src_is_reported_in_stats(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.stats["eff_src"], "rc_dashboard.builtin")

    def test_purpose_src_still_reported(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.purpose_src, "rc_dashboard.builtin")


class TestActorThreeClasses(unittest.TestCase):
    """The three-class rule survives the promotion from fallback."""

    def test_vscode_is_its_own_class(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.actor3("agent", "vscode"), "human-vscode")

    def test_real_agent_stays_agent(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.actor3("agent", "claude_code"), "agent")

    def test_unknown_agent_type_is_human(self):
        n = Normalizer(FakeCfg())
        self.assertEqual(n.actor3("agent", "something_else"), "human")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: FAIL. `test_no_sys_path_reaching` fails on the `sys.path.insert` still present; `test_eff_src_is_declared` fails with `AttributeError: 'Normalizer' object has no attribute 'eff_src'`; `test_purpose_src_still_reported` fails because `purpose_src` is currently the string `"builtin"`.

- [ ] **Step 4: Replace `_load_helpers` with the promoted builtins**

In `dashboard/rc_dashboard/normalize.py`, delete the `REPO = ...` line at the top of the module (line 39) and replace the whole of `_load_helpers()` (lines 99-135) with:

```python
# The classifiers below are this repository's own.  They were written as
# fallbacks for an `analyze/` tree that is not vendored here and never will be,
# so the fallback was always the live code path; naming them as the
# implementation makes that visible rather than accidental.
#
# Shell resolution is NOT performed: `effective_comm` is the identity, so
# `bash -c "git status"` is attributed to `bash`.  That is a real limitation and
# `eff_src` exists so the page states it rather than implying resolution
# happened.
EFF_SRC = "rc_dashboard.builtin"
PURPOSE_SRC = "rc_dashboard.builtin"
SHELL_COMMS = frozenset()


def effective_comm(comm, args):
    """Identity.  See EFF_SRC: no shell resolution is performed here."""
    return comm


def is_autonomous(args):
    return bool(args and _AUTONOMOUS.search(args))


def purpose_of(comm):
    return _BUILTIN_PURPOSE.get(comm or "", "other")
```

Delete the `import sys` at the top of the module if nothing else uses it (check with `grep -n "sys\." dashboard/rc_dashboard/normalize.py`).

- [ ] **Step 5: Rename `_fallback_actor3` and wire the Normalizer**

Rename `_fallback_actor3` to `actor3`, keeping its docstring's rule but restating why it exists:

```python
def actor3(actor, agent_type):
    """Three actor classes, never two -- must NOT degrade to the binary `actor`.

    Folding VS Code into "agent" is what produced a retracted GPU near-parity
    claim, so `human-vscode` is its own class and an unrecognised agent_type is
    `human`, never `agent`.
    """
    if actor == "agent":
        if agent_type in _REAL_AGENTS or agent_type == "bwrap":
            return "agent"
        if agent_type == "vscode":
            return "human-vscode"
        return "human"
    return actor
```

Replace the first two statements of `Normalizer.__init__` (the `_load_helpers()` unpack) with direct binding:

```python
    def __init__(self, cfg):
        self.effective_comm = effective_comm
        self.actor3 = actor3
        self.is_autonomous = is_autonomous
        self.shell_comms = SHELL_COMMS
        self.purpose_of = purpose_of
        self.purpose_src = PURPOSE_SRC
        self.eff_src = EFF_SRC
        self.purposes = None
        f = cfg.get("filters", {})
```

and add `"eff_src": self.eff_src,` to the `self.stats` dict beside `"purpose_src"`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 7 tests.

- [ ] **Step 7: Verify the service still starts**

```bash
cd dashboard && uv run python -m rc_dashboard print-config > /dev/null && echo OK
```

Expected: `OK`. This exercises the import path end to end.

- [ ] **Step 8: Commit**

```bash
git add dashboard/tests dashboard/rc_dashboard/normalize.py
git commit -m "Promote normalize's builtin classifiers; add eff_src

The sys.path reach into analyze/ never resolved in this repo, so the
fallbacks were always the live path. Name them as the implementation and
report eff_src beside purpose_src, so the page states that shell
resolution is not performed instead of implying it happened."
```

---

### Task 2: Remove the old repository's path defaults

**Files:**
- Modify: `dashboard/rc_dashboard/config.py:21` (`REPO`), `:41-80` (candidates), `:101-112` (four feed defaults)
- Modify: `dashboard/rc_dashboard/sacct.py:94`, `:292`, `:976`
- Test: `dashboard/tests/test_config.py` (create)

**Interfaces:**
- Consumes: `tests.support.FakeCfg`.
- Produces: `config.defaults()` with no `REPO`-derived values. `feeds.sacct.dir`, `feeds.sacctmgr.dir`, `feeds.domains.path` and `feeds.user_actor.path` default to `None`.

**Note on spec drift:** the spec says "`REPO` becomes unused and is removed". `REPO` also backs four feed defaults and three uses in `sacct.py`, all of them old-repo layout (`REPO/dataclean/mapping/map_user.csv`). This task covers those too. All four feeds are `required: False`, and `resolve_globbed`/`resolve_file` already treat a `None` path as missing, so defaulting to `None` reports an honest "not configured" instead of pointing at a repo-root path that means nothing.

- [ ] **Step 1: Write the failing test**

Create `dashboard/tests/test_config.py`:

```python
import unittest

from rc_dashboard import config
from tests.support import source_of


class TestNoOldRepoPaths(unittest.TestCase):
    def test_no_rc_measurement_root(self):
        self.assertNotIn("RC_MEASUREMENT_ROOT", source_of("config.py"))

    def test_no_repo_constant(self):
        src = source_of("config.py")
        self.assertNotIn("REPO =", src)

    def test_ebpf_candidates_are_absolute_or_home(self):
        """Every candidate names a real deployment location, not a repo layout."""
        for cand in config.defaults()["feeds"]["ebpf"]["roots"]:
            self.assertNotIn("/log/ebpfm", cand.replace("/var/log/ebpfm", ""))

    def test_optional_feeds_default_to_unset(self):
        d = config.defaults()["feeds"]
        self.assertIsNone(d["sacct"]["dir"])
        self.assertIsNone(d["sacctmgr"]["dir"])
        self.assertIsNone(d["domains"]["path"])
        self.assertIsNone(d["user_actor"]["path"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd dashboard && python3 -m unittest tests.test_config -v
```

Expected: FAIL on all four — `RC_MEASUREMENT_ROOT` and `REPO =` are present, and the four feeds default to `REPO`-derived paths.

- [ ] **Step 3: Rewrite the two candidate builders**

In `dashboard/rc_dashboard/config.py`, delete line 21 (`REPO = ...`) and replace `_ebpf_candidates()` and `_node_candidates()` with:

```python
def _ebpf_candidates():
    """Where the root eBPF tier's day-files might live, most specific first.

    `EBPFM_LOG_ROOT` before `EBPFM_OUTPUT_DIR` deliberately: the distinct
    LOG_ROOT name means an operator shell that repoints the collector cannot
    silently repoint the readers too.
    """
    c = []
    for e in ("EBPFM_LOG_ROOT", "EBPFM_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    c += ["/var/log/ebpfm", os.path.expanduser("~/.ebpfm/ebpf")]
    return c


def _node_candidates():
    """The unprivileged node tier."""
    c = []
    for e in ("EBPFM_NODE_LOG_ROOT", "EBPFM_NODE_OUTPUT_DIR"):
        if os.environ.get(e):
            c.append(os.environ[e])
    c += ["/var/log/ebpfm-login", os.path.expanduser("~/.ebpfm/login")]
    return c
```

- [ ] **Step 4: Unset the four optional feed defaults**

In `defaults()`, delete the `base = _env("RC_MEASUREMENT_ROOT")` line and change the four feed blocks to:

```python
            "sacct": {
                "dir": None,
                "pattern": "SlurmData_*.csv",
                "max_days": 0,
                "jobs_csv": None,
                "required": False,
            },
            "sacctmgr": {"dir": None, "pattern": "sacctmgr_*.jsonl", "required": False},
            "domains": {"path": None, "required": False},
            "user_actor": {"path": None, "required": False},
```

- [ ] **Step 5: Remove `REPO` from `sacct.py`**

Delete `REPO = os.path.dirname(HERE)` (line 94). At line 292 change `find_sacct_inputs(dirpath or REPO, pattern)` to `find_sacct_inputs(dirpath, pattern)`, and make `find_sacct_inputs` return an empty list for a falsy `dirpath` — add this as its first statement:

```python
    if not dirpath:
        return []
```

At line 976 change the default to an explicit absence:

```python
    if not path:
        return {}
```

(replacing `path = path or os.path.join(REPO, "dataclean", "mapping", "map_user.csv")`; keep the rest of the function unchanged).

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 11 tests.

- [ ] **Step 7: Verify feed resolution still reports honestly**

```bash
cd dashboard && uv run python -m rc_dashboard check-feeds; echo "exit=$?"
```

Expected: a table. Off-cluster the eBPF row is `MISSING` and `exit=1` — that is correct and unchanged. The `sacct`, `sacctmgr`, `domains` and `user_actor` rows must read `(0 candidate(s), none exist)` rather than naming a repo path.

- [ ] **Step 8: Commit**

```bash
git add dashboard/rc_dashboard/config.py dashboard/rc_dashboard/sacct.py dashboard/tests/test_config.py
git commit -m "Drop the old repo's path defaults from feed discovery

RC_MEASUREMENT_ROOT, REPO/log/* and the four REPO-derived optional feed
defaults all describe a layout that does not exist here. The optional
feeds now default to unset, which resolves as an honest 'not configured'
rather than pointing at a repo-root path that means nothing."
```

---

### Task 3: De-site-specify the example config

**Files:**
- Modify: `dashboard/dashboard.config.example.json`
- Test: extend `dashboard/tests/test_config.py`

**Interfaces:**
- Consumes: `config.strip_comments`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/tests/test_config.py`:

```python
class TestExampleConfig(unittest.TestCase):
    def _example(self):
        import json
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "dashboard.config.example.json")) as fh:
            return fh.read()

    def test_no_site_specific_paths(self):
        raw = self._example()
        self.assertNotIn("/n/netscratch", raw)
        self.assertNotIn("juncheng_lab", raw)

    def test_no_old_repo_paths(self):
        raw = self._example()
        for token in ("rc_measurement", "collect/", "analyze/"):
            self.assertNotIn(token, raw)

    def test_example_is_itself_a_legal_config(self):
        import json
        from rc_dashboard.config import strip_comments
        tree = strip_comments(json.loads(self._example()))
        self.assertEqual(tree.get("version"), 1)
        self.assertIn("feeds", tree)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd dashboard && python3 -m unittest tests.test_config -v
```

Expected: FAIL on `test_no_site_specific_paths` and `test_no_old_repo_paths`.

- [ ] **Step 3: Replace the site paths with placeholders**

Edit `dashboard/dashboard.config.example.json`. Replace every `/n/netscratch/juncheng_lab/juncheng/rc_measurement/...` value with the deployment default, and rewrite the `//`-comments that name `collect/` modules so they describe the contract instead of citing a file:

- `feeds.ebpf.roots` -> `["/var/log/ebpfm"]`
- `feeds.ebpf_node.roots` -> `["/var/log/ebpfm-login"]`
- `feeds.sacct.dir` -> `"/path/to/sacct/day-files"`
- comment `"threshold collect/healthcheck.py uses for 'ebpfm'."` -> `"staleness threshold for the ebpf tier, in seconds."`
- comment `"collect/common/snapshot.py, so log/login is schema-compatible and works"` -> `"the node tier writes the same schema as the collector's node_snapshot.py."`
- comment `"collect/slurm_query.bash writes ONE FILE PER DAY into DEST_DIR, named"` -> `"the sacct exporter writes ONE FILE PER DAY into this directory, named"`
- comment `"Standard repo exclusions -- see analyze/tool_calls_distribution.py:53."` -> `"Standard exclusions."`

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 14 tests.

- [ ] **Step 5: Verify the example loads as a real config**

```bash
cd dashboard && uv run python -m rc_dashboard --config dashboard.config.example.json print-config | head -5
```

Expected: JSON, no exception.

- [ ] **Step 6: Commit**

```bash
git add dashboard/dashboard.config.example.json dashboard/tests/test_config.py
git commit -m "De-site-specify the example config

The example carried one site's absolute netscratch paths and cited
collect/ modules by filename. It is meant to be copyable; it now uses
deployment defaults and describes the contract instead of citing files."
```

---

### Task 4: Remove the old repository's citations

**Files:**
- Modify: `dashboard/rc_dashboard/reducers/resources.py:159`, `reducers/tools.py:11`, `:123-125`, `reducers/trajectories.py:4-5`, `:225-226`, `reducers/sandbox.py:19`, `reducers/risk.py:9`, `:49`, `feeds.py:9-11`, `:299`, `sacct.py:9`, `:157`, `:334`, `normalize.py:193`, `reducers/__init__.py`
- Test: `dashboard/tests/test_selfcontained.py` (create)

**Interfaces:**
- Consumes: `tests.support.source_of`.
- Produces: nothing new. Two API strings change value (below).

**Two deliberate removals.** Both are user-visible and both lose a real claim, which is the agreed call: evidence that is not in this repository cannot support an assertion made from it.

- [ ] **Step 1: Write the failing test**

Create `dashboard/tests/test_selfcontained.py`:

```python
"""The executable form of Goal 2: nothing here refers to the repo this tree
was extracted from.  In the spirit of `npm run check:no-random`."""
import os
import unittest

FORBIDDEN = ("collect/", "analyze/", "findings/", "eBPF_marthen_new",
             "RC_MEASUREMENT_ROOT")

# The one exemption, asserted positively below so it cannot quietly widen.
WIRE_VALUE = "ebpf_marthen_new"


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python_sources():
    root = os.path.join(_repo_root(), "dashboard", "rc_dashboard")
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


class TestNoOldRepoReferences(unittest.TestCase):
    def test_python_sources_are_clean(self):
        offenders = []
        for path in _python_sources():
            with open(path) as fh:
                text = fh.read()
            for token in FORBIDDEN:
                if token in text:
                    offenders.append("%s: %s" % (os.path.relpath(path), token))
        self.assertEqual(offenders, [], "old-repo references: %r" % offenders)

    def test_wire_value_is_preserved(self):
        """The one allowed occurrence of the old name, and it must still be there."""
        path = os.path.join(_repo_root(), "collector", "ebpf_trace.py")
        with open(path) as fh:
            self.assertIn("COLLECTOR = '%s'" % WIRE_VALUE, fh.read())


class TestDroppedClaims(unittest.TestCase):
    """Claims whose evidence is not in this repo are not made here."""

    def test_cpu_basis_states_the_rule_without_the_figure(self):
        from rc_dashboard.reducers.tools import ToolMixReducer
        from tests.support import FakeCfg
        r = ToolMixReducer(FakeCfg(), ["agent", "human-vscode", "human"])
        basis = r.result()["cpu_basis"]
        self.assertIn("child_cpu_s NEVER added", basis)
        self.assertNotIn("1.83", basis)
        self.assertNotIn("analyze/", basis)

    def test_sessionize_basis_states_the_rule_without_the_citation(self):
        from rc_dashboard.reducers.trajectories import TrajectoryReducer
        from tests.support import FakeCfg
        r = TrajectoryReducer(FakeCfg(), ["agent", "human-vscode", "human"])
        basis = r.result()["basis"]["sessionize"]
        self.assertIn("apid", basis)
        self.assertNotIn("extract_trajectories", basis)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd dashboard && python3 -m unittest tests.test_selfcontained -v
```

Expected: FAIL. `test_python_sources_are_clean` lists roughly fifteen offenders; both `TestDroppedClaims` tests fail on the present citations.

If `ToolMixReducer(FakeCfg(), [...])` raises because it reads a config key `FakeCfg` does not carry, add that key to `FakeCfg`'s tree in `tests/support.py` with the value from `config.defaults()` — e.g. `"reducers": {"tool_mix": {"head_n": 12, "dedup_sessions_max": 200000}, "io": {"top_n": 20}, "trajectories": {"top_n": 15, "max_runs_shown": 60, "purpose_seq_cap": 80, "idle_evict_s": 7200, "max_sessions": 50000, "include_key_types": ["apid", "sess", "tty"]}}`.

- [ ] **Step 3: Re-anchor the two references whose target is vendored here**

`reducers/resources.py:159` — the schema authority is in this repo:

```python
    Field names are the real `collector/node_snapshot.py` schema (37 top-level
```

Do the same for any comment citing `collect/common/agent_classify.py`, which is
`collector/lib/agent_classify.py` here.

- [ ] **Step 4: Delete the two claims whose evidence is not here**

`reducers/tools.py:123-125`:

```python
            "cpu_basis": "self cpu_s, each process once; child_cpu_s NEVER added",
```

`reducers/sandbox.py` — in the module docstring, replace the sentence citing `findings/login.md` so the distinction survives without the unattributed claim:

```
signal: Claude Code's bwrap use is a capability probe (`bwrap --ro-bind / /`).
ancestry=bwrap with sandbox=unsandboxed is the probe; ancestry=bwrap with
sandbox=sandboxed is real confinement.
```

- [ ] **Step 5: Delete the remaining citations, keeping every rule**

Work through the offender list from Step 2. In each case remove only the path reference; the rule, field list or reason beside it stays. Specifically:

- `reducers/trajectories.py:225-226` — drop ` (analyze/extract_trajectories.py:278-294)` from the `sessionize` string; the rule before it is already complete.
- `reducers/trajectories.py:4-5` — drop the citation, keep the sessionizing description.
- `reducers/tools.py:11` — drop the citation, keep the dedup rule.
- `reducers/risk.py:9`, `:49` — drop the `eBPF_marthen_new/README.md` and `findings/` citations, keep the login-node scope statement.
- `feeds.py:9-11`, `:299` — drop the `collect/healthcheck.py` shaping note; keep "so operators can read it".
- `sacct.py:9`, `:157`, `:334` — replace `collect/slurm_query.bash` with "the sacct exporter" in all three, keeping the instruction to re-pull.
- `normalize.py:193` — drop the `analyze/extract_trajectories.py:278-294` citation, keep the `(key_type, key_value)` description.
- `reducers/__init__.py` — drop any remaining citation.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 18 tests.

- [ ] **Step 7: Commit**

```bash
git add dashboard/rc_dashboard dashboard/tests/test_selfcontained.py
git commit -m "Remove the old repo's citations; drop two unsupported claims

Re-anchored where the cited file is vendored here. Deleted elsewhere,
keeping every rule the citation decorated. Two claims went with their
citations: the 1.83x CPU-inflation figure in cpu_basis and the
D-state-on-dead-automount attribution in sandbox.py. Both rest on
evidence that is not in this repository, so they are not asserted from
it. Guarded by tests/test_selfcontained.py."
```

---

### Task 5: Remove the inert vendoring sync subsystem

**Files:**
- Modify: `collector/ebpfm.sh:253-254`, `:348-349`, `:393-411`, `:886`
- Modify: `collector/VENDORED.md`
- Delete: `collector/.vendored.sha256`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. `ebpfm.sh check` loses one section of output.

- [ ] **Step 1: Confirm the current behaviour, so the change is measured**

```bash
cd collector && ./ebpfm.sh check 2>&1 | grep -A 2 "vendored libraries"
```

Expected: the `-- vendored libraries` heading followed by `[ OK ] vendored  standalone (no repo alongside; nothing to compare)`. That line is the whole point: it can never say anything else here.

- [ ] **Step 2: Remove the function and its call sites**

In `collector/ebpfm.sh`, delete the `_check_vendored()` function (lines 393-411) and both call sites, each of which is a `printf` heading plus the call:

```bash
        printf -- '-- vendored libraries\n'
        _check_vendored
```

at lines 253-254, and the same pair at 348-349.

- [ ] **Step 3: Drop the manifest from the bundle verb**

At `collector/ebpfm.sh:886`, remove `"$base/.vendored.sha256"` from the file list, keeping `"$base/VENDORED.md"`.

- [ ] **Step 4: Delete the manifest**

```bash
git rm collector/.vendored.sha256
```

- [ ] **Step 5: Reduce VENDORED.md to what the modules are**

Replace the table's "Copied from" and "Upstream sha256 at vendor time" columns with a two-column table of `Module` and `Local changes`, and delete the "The sync rule" and "To re-vendor" sections along with the `Vendored from repo commit ...` provenance line. Keep the "Why copies and not symlinks" section verbatim — a symlink still breaks when the folder is copied off the machine, which is still the reason.

- [ ] **Step 6: Verify `check` still runs clean**

```bash
cd collector && ./ebpfm.sh check 2>&1 | tail -20; echo "exit=$?"
```

Expected: the same report minus the `-- vendored libraries` section. Off-cluster several rows will FAIL for missing BCC and kernel headers — that is expected and unrelated. What must be true: no `command not found`, no reference to `.vendored.sha256`.

- [ ] **Step 7: Verify the bundle verb still works**

```bash
cd collector && ./ebpfm.sh bundle > /tmp/ebpfm-bundle-test.sh && wc -c /tmp/ebpfm-bundle-test.sh && rm /tmp/ebpfm-bundle-test.sh
```

Expected: a non-empty file, no error about the missing manifest.

- [ ] **Step 8: Verify the collector's own tests still pass**

```bash
cd collector && python3 -m unittest discover -s tests -q
```

Expected: PASS, 72 tests.

- [ ] **Step 9: Commit**

```bash
git add collector/
git commit -m "Remove the inert vendoring sync check

ebpfm.sh check compared each vendored module against a collect/ or
analyze/ path in a repo that is never alongside it here, so it could
only ever print 'standalone (nothing to compare)'. The manifest, the
check and the re-vendor instructions go. Why the modules are copies
rather than symlinks stays: that reason still holds."
```

---

### Task 6: READMEs, and the repo-wide guard

**Files:**
- Modify: `README.md`, `dashboard/README.md`
- Modify: `dashboard/tests/test_selfcontained.py`

**Interfaces:**
- Consumes: `tests/test_selfcontained.py` from Task 4.
- Produces: the guard extended to Markdown and shell, which is what makes Goal 2 checkable rather than asserted.

- [ ] **Step 1: Extend the guard to docs and shell**

In `dashboard/tests/test_selfcontained.py`, add a second walker and test:

```python
def _doc_and_shell_sources():
    root = _repo_root()
    for sub in ("dashboard", "collector"):
        for dirpath, dirs, files in os.walk(os.path.join(root, sub)):
            dirs[:] = [d for d in dirs
                       if d not in (".venv", "node_modules", ".git", "dist",
                                    "archive", ".cache", ".state")]
            for f in files:
                if f.endswith((".md", ".sh", ".json")):
                    yield os.path.join(dirpath, f)
    yield os.path.join(root, "README.md")


class TestDocsAreClean(unittest.TestCase):
    def test_docs_and_shell_are_clean(self):
        offenders = []
        for path in _doc_and_shell_sources():
            with open(path, errors="replace") as fh:
                text = fh.read()
            for token in FORBIDDEN:
                if token in text:
                    offenders.append("%s: %s" % (os.path.relpath(path, _repo_root()), token))
        self.assertEqual(offenders, [], "old-repo references: %r" % offenders)
```

`dashboard/archive/` is excluded: it is the retired static-HTML demo, kept verbatim as a historical record.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd dashboard && python3 -m unittest tests.test_selfcontained -v
```

Expected: FAIL, listing `README.md`, `collector/README.md`, `collector/COVERAGE.md`, `collector/VENDORED.md` and `dashboard/README.md`.

- [ ] **Step 3: Remove the root README's "A note on paths"**

Delete the whole `## A note on paths` section. Keep the paragraph about the surviving wire value and move it under `## The rules both halves obey` as a final bullet:

```markdown
- **The wire value outlives the rename.** The collector folder was once
  `eBPF_marthen_new/` and is [`collector/`](collector/) here, but the JSONL
  envelope still stamps `collector = "ebpf_marthen_new"`. It is a wire value, so
  renaming it would split feeds captured before the change from those captured
  after. Nothing reads the field; it is provenance for whoever opens the data
  later.
```

- [ ] **Step 4: Correct the root README's schema version**

Change `(\`SCHEMA_VERSION = 5\`)` to `(\`SCHEMA_VERSION\` 5–6; the collector emits 6)`. This is the one outstanding item from spec Part 3 — `__init__.py` and `normalize.py` were already updated by hand.

- [ ] **Step 5: Clear the remaining docs**

Work through the offender list from Step 2, removing each old-repo path. In `collector/README.md` and `collector/COVERAGE.md` these are prose references to sibling trees; delete the reference and keep the sentence's claim where it stands on its own, exactly as in Task 4.

- [ ] **Step 6: Run the full suite to verify it passes**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 19 tests. **Phase A is complete when this is green.**

- [ ] **Step 7: Commit**

```bash
git add README.md dashboard/README.md collector/ dashboard/tests/test_selfcontained.py
git commit -m "Clear the last old-repo references from the docs

The root README's 'A note on paths' existed to explain references that
no longer exist. The surviving wire value moves to the rules list, where
it belongs. The guard now covers Markdown, shell and JSON as well as
Python, so Goal 2 is checkable rather than asserted."
```

---

### Task 7: Report a schema version we do not know

**Files:**
- Modify: `dashboard/rc_dashboard/normalize.py` (`schema_of`, `Normalizer.stats`, `normalize`)
- Modify: `dashboard/rc_dashboard/aggregator.py:153-165` (`refresh_feeds`)
- Test: extend `dashboard/tests/test_normalize.py`

**Interfaces:**
- Consumes: `tests.support.FakeCfg`.
- Produces: `normalize.ACCEPTED_SCHEMA = (5, 6)`; `Normalizer.stats` gains `"unknown_schema"` (a `{version: count}` dict); the `ebpf` feed report gains `rep["excluded"]["unknown_schema"]`.

**Why:** the collector emits `SCHEMA_VERSION = 6` and the dashboard's guards are
`sv >= 5`, so a v7 record would flow through the v6 path unremarked. Nothing
would say so. `schema_versions` already tallies what arrived; this makes a
version outside the accepted range a reported fact rather than an internal
counter. A record is never dropped for this — it is counted and named, because
an unrecognised version is a thing to look at, not a thing to discard.

**Scope note:** this is spec Part 3, which was flagged as unagreed. `__init__.py`
and `normalize.py` already declare 5–6, and Task 6 fixes the root README. This
task is the remaining piece. Skip it if Part 3 was cut; nothing else depends
on it.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/tests/test_normalize.py`:

```python
class TestSchemaContract(unittest.TestCase):
    def _rec(self, sv):
        return {"event": "exit", "schema_version": sv, "ts": "2026-09-19 10:00:00",
                "host": "login01", "user": "alice", "comm": "git",
                "actor": "agent", "agent_type": "claude_code"}

    def test_accepted_range_is_five_to_six(self):
        from rc_dashboard.normalize import ACCEPTED_SCHEMA
        self.assertEqual(ACCEPTED_SCHEMA, (5, 6))

    def test_a_known_version_is_not_flagged(self):
        n = Normalizer(FakeCfg())
        n.normalize(self._rec(6))
        self.assertEqual(n.stats["unknown_schema"], {})

    def test_an_unknown_version_is_counted_and_named(self):
        n = Normalizer(FakeCfg())
        n.normalize(self._rec(7))
        self.assertEqual(n.stats["unknown_schema"], {7: 1})

    def test_an_unknown_version_is_not_dropped(self):
        """Counted, not discarded: an unrecognised version is a thing to look
        at, not a thing to throw away."""
        n = Normalizer(FakeCfg())
        self.assertIsNotNone(n.normalize(self._rec(7)))

    def test_still_tallies_every_version_seen(self):
        n = Normalizer(FakeCfg())
        n.normalize(self._rec(6))
        n.normalize(self._rec(7))
        self.assertEqual(n.stats["schema_versions"], {6: 1, 7: 1})
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_normalize -v
```

Expected: FAIL with `ImportError: cannot import name 'ACCEPTED_SCHEMA'` and
`KeyError: 'unknown_schema'`.

- [ ] **Step 3: Declare the accepted range**

In `dashboard/rc_dashboard/normalize.py`, beside `schema_of`:

```python
# The contract with the collector, stated so a drift is visible. 6 is what
# `collector/ebpf_trace.py` emits today; 5 still reads because nothing this
# module does with a v5 record is wrong for a v6 one.
ACCEPTED_SCHEMA = (5, 6)
```

- [ ] **Step 4: Count what falls outside it**

Add `"unknown_schema": {},` to `Normalizer.__init__`'s `self.stats` dict. In
`normalize()`, where `sv = schema_of(rec)` is already computed and tallied into
`self.stats["schema_versions"]`, add immediately after:

```python
        if sv not in ACCEPTED_SCHEMA:
            u = self.stats["unknown_schema"]
            u[sv] = u.get(sv, 0) + 1
```

Do not return early: the record continues through the pipeline. A version we do
not recognise is reported, not discarded.

- [ ] **Step 5: Surface it on the feed report**

In `aggregator.py`'s `refresh_feeds`, extend the `ebpf` block's `rep["excluded"]`:

```python
                    rep["excluded"] = {
                        "users": self.norm.stats["dropped_users"],
                        "self_observation": self.norm.stats["dropped_self_observation"],
                        "unknown_schema": dict(self.norm.stats["unknown_schema"]),
                    }
```

`unknown_schema` sits beside the other exclusions although nothing is excluded
by it — it is the same kind of fact, and the panel that renders exclusions is
where an operator already looks for "something arrived that we did not expect".

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 24 tests.

- [ ] **Step 7: Commit**

```bash
git add dashboard/rc_dashboard/normalize.py dashboard/rc_dashboard/aggregator.py dashboard/tests/test_normalize.py
git commit -m "Report a schema version outside the accepted range

The collector emits 6 and the guards are sv >= 5, so a v7 record would
pass through unremarked and nothing would say so. Versions outside 5-6
are counted and named on the feed report. Never dropped: an
unrecognised version is a thing to look at, not a thing to discard."
```

---

# Phase B — Three ingest modes

---

### Task 8: Content-verified resume

**Files:**
- Modify: `dashboard/rc_dashboard/tail.py:1-26` (header, `STATE_VERSION`), `:44-46` (`stats`), `:96-110` (the inode gate), `:145-149` (`_remember`)
- Test: `dashboard/tests/test_tail.py` (create)

**Interfaces:**
- Consumes: `tests.support`.
- Produces: `tail.TAIL_K: int = 4096`; `tail._prefix_sha(path, offset, k=TAIL_K) -> str | None`; `FileTailSource.stats` gains the key `"resumed_after_replace"`; per-file state gains `"tail_sha"` and `"tail_k"`; `STATE_VERSION == 2`.

- [ ] **Step 1: Add the day-file fixtures**

Append to `dashboard/tests/support.py`:

```python
import json
import shutil
import tempfile


class FeedDir:
    """A collector output root: <root>/<YYYY-MM-DD>/<host>.<stream>.jsonl.

    The ebpf tier splits each day into `exits` (exit, truncated) and `snapshot`
    (everything else), so a fixture writing one file per host-day no longer
    resembles a real feed. `stream` picks which of the pair this helper drives
    and `self.path` is that file, which leaves `resync` and `corrupt_prefix`
    below unchanged by the split.
    """

    def __init__(self, date="2026-09-19", host="login01", stream="exits"):
        self.root = tempfile.mkdtemp(prefix="rcdash-feed-")
        self.date, self.host, self.stream = date, host, stream
        self.daydir = os.path.join(self.root, date)
        os.makedirs(self.daydir)
        self.path = os.path.join(self.daydir, "%s.%s.jsonl" % (host, stream))
        self.n = 0

    def append(self, count=1, event="exit"):
        """Append `count` records, then flush once -- a collector poll round."""
        with open(self.path, "a") as fh:
            for _ in range(count):
                self.n += 1
                fh.write(json.dumps({
                    "event": event, "schema_version": 6, "seq": self.n,
                    "ts": "2026-09-19 10:%02d:00" % (self.n % 60),
                    "host": self.host, "user": "alice", "comm": "git",
                    "actor": "agent", "agent_type": "claude_code",
                }) + "\n")
            fh.flush()
        return self

    def resync(self, extra=0):
        """Replace the file the way rsync/scp do: write a copy, rename over.

        The point of the helper is the rename: the content is a superset of
        what was there, but the inode is new.
        """
        if extra:
            self.append(extra)
        tmp = self.path + ".tmp"
        shutil.copyfile(self.path, tmp)
        os.replace(tmp, self.path)
        return self

    def corrupt_prefix(self):
        """Replace the file with same-length but different content at the front."""
        with open(self.path, "rb") as fh:
            blob = fh.read()
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(b"x" * 64 + blob[64:])
        os.replace(tmp, self.path)
        return self

    def truncate_to(self, nbytes):
        tmp = self.path + ".tmp"
        with open(self.path, "rb") as fh:
            blob = fh.read(nbytes)
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, self.path)
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
```

- [ ] **Step 2: Write the failing tests**

Create `dashboard/tests/test_tail.py`:

```python
import unittest

from rc_dashboard.tail import FileTailSource
from tests.support import FeedDir


class TailResumeTest(unittest.TestCase):
    def setUp(self):
        self.feed = FeedDir()
        self.addCleanup(self.feed.cleanup)

    def src(self):
        return FileTailSource([self.feed.root], days=2)

    def test_plain_append_is_not_re_read(self):
        s = self.src()
        self.feed.append(3)
        self.assertEqual(len(list(s.poll())), 3)
        self.feed.append(2)
        self.assertEqual(len(list(s.poll())), 2)

    def test_resync_with_no_new_records_yields_nothing(self):
        """An rsync that changed the inode but not the content is a no-op."""
        s = self.src()
        self.feed.append(3)
        self.assertEqual(len(list(s.poll())), 3)
        self.feed.resync()
        self.assertEqual(list(s.poll()), [])
        self.assertEqual(s.stats["resumed_after_replace"], 1)
        self.assertEqual(s.stats["resets"], 0)

    def test_resync_yields_only_the_new_records(self):
        s = self.src()
        self.feed.append(3)
        list(s.poll())
        self.feed.resync(extra=2)
        got = list(s.poll())
        self.assertEqual([r["seq"] for r in got], [4, 5])

    def test_prefix_mismatch_forces_a_counted_full_re_read(self):
        s = self.src()
        self.feed.append(3)
        list(s.poll())
        self.feed.corrupt_prefix()
        got = list(s.poll())
        self.assertEqual(len(got), 3)
        self.assertEqual(s.stats["resets"], 1)
        self.assertEqual(s.stats["resumed_after_replace"], 0)

    def test_shorter_replica_forces_a_counted_full_re_read(self):
        s = self.src()
        self.feed.append(4)
        list(s.poll())
        self.feed.truncate_to(80)
        list(s.poll())
        self.assertEqual(s.stats["resets"], 1)

    def test_state_version_is_two(self):
        from rc_dashboard import tail
        self.assertEqual(tail.STATE_VERSION, 2)


class DoubleCountRegressionTest(unittest.TestCase):
    """The bug this whole phase exists to fix: a sync must not inflate totals."""

    def setUp(self):
        self.feed = FeedDir()
        self.addCleanup(self.feed.cleanup)

    def test_sync_does_not_double_count(self):
        incremental = FileTailSource([self.feed.root], days=2)
        self.feed.append(5)
        seen = [r["seq"] for r in incremental.poll()]
        self.feed.resync(extra=5)
        seen += [r["seq"] for r in incremental.poll()]

        oneshot = FileTailSource([self.feed.root], days=2)
        once = [r["seq"] for r in oneshot.poll()]

        self.assertEqual(sorted(seen), sorted(once))
        self.assertEqual(len(seen), len(set(seen)), "a record was yielded twice")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_tail -v
```

Expected: FAIL. `test_resync_with_no_new_records_yields_nothing` fails with `KeyError: 'resumed_after_replace'`; `test_resync_yields_only_the_new_records` returns seqs `[1,2,3,4,5]` instead of `[4,5]`; `test_double_count` finds duplicates; `test_state_version_is_two` sees 1.

- [ ] **Step 4: Add the hash helper and bump the state version**

In `dashboard/rc_dashboard/tail.py`, add `import hashlib` beside the other imports, change `STATE_VERSION = 1` to `STATE_VERSION = 2`, and add after it:

```python
TAIL_K = 4096            # bytes hashed before the resume offset


def _prefix_sha(path, offset, k=TAIL_K):
    """sha256 of the k bytes ending at `offset`, or None if unavailable.

    The collector's writer is strictly append-only -- `DailyWriter.write()`
    appends a line and never rewrites; under the default poll flush mode the
    bytes land at most one round later -- so byte i of a host-day file never
    changes and a faithful replica has a byte-identical prefix.
    Matching that window is what makes a resume across a new inode sound.
    """
    if offset <= 0:
        return None
    start = max(0, offset - k)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read(offset - start)
    except OSError:
        return None
    if len(blob) != offset - start:
        return None
    return hashlib.sha256(blob).hexdigest()


def _verified_offset(path, st, rec):
    """The offset we may safely resume from on a file whose inode changed."""
    off = rec.get("offset", 0)
    if off <= 0 or st.st_size < off or not rec.get("tail_sha"):
        return None
    return off if _prefix_sha(path, off, rec.get("tail_k", TAIL_K)) == rec["tail_sha"] else None
```

- [ ] **Step 5: Add the counter**

In `FileTailSource.__init__`, add `"resumed_after_replace": 0,` to the `self.stats` dict.

- [ ] **Step 6: Replace the inode gate**

In `_read_one`, replace the `elif rec:` branch:

```python
            elif rec:
                # Replaced. rsync and scp rename into place, so a re-synced file
                # has a new inode on every sync; that alone is not a reason to
                # re-read it. Verify the prefix instead.
                resume = _verified_offset(path, st, rec)
                if resume is None:
                    self.stats["resets"] += 1
                else:
                    offset = resume
                    self.stats["resumed_after_replace"] += 1
```

- [ ] **Step 7: Store the fingerprint**

Replace `_remember`:

```python
    def _remember(self, path, st, offset, prev):
        self.state[path] = {"dev": st.st_dev, "inode": st.st_ino,
                            "size": st.st_size, "offset": offset,
                            "mtime": st.st_mtime,
                            "tail_k": TAIL_K,
                            "tail_sha": _prefix_sha(path, offset)}
```

- [ ] **Step 8: Record why in the module docstring**

Add to the header docstring's rules list:

```
* A re-synced file arrives with a NEW INODE, because rsync and scp rename into
  place. Resuming is therefore keyed on content as well: `tail_sha` fingerprints
  the 4 KiB before the stored offset, and a match resumes rather than re-reads.
  Without this a five-minute rsync re-feeds the whole file every sync and the
  live tier, which accumulates, counts every record again.
```

- [ ] **Step 9: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 31 tests.

- [ ] **Step 10: Commit**

```bash
git add dashboard/rc_dashboard/tail.py dashboard/tests/test_tail.py dashboard/tests/support.py
git commit -m "Resume a re-synced file by content, not by inode

rsync and scp rename into place, so every sync gives the file a new
inode; the tailer reset to offset 0 and re-read it, and the live tier
accumulates, so the records were counted twice. Fingerprint the 4 KiB
before the offset and resume when it matches. Sound because the
collector's writer is strictly append-only."
```

---

### Task 9: The mode detector

**Files:**
- Create: `dashboard/rc_dashboard/modes.py`, `dashboard/tests/test_modes.py`
- Modify: `dashboard/rc_dashboard/feeds.py:164-166` (add `ino` to file entries)

**Interfaces:**
- Consumes: a feed report's `files` list, whose entries gain `"ino"`.
- Produces: `modes.LIVE`, `modes.REPLICA`, `modes.ARCHIVE` (the strings `"live"`, `"replica"`, `"archive"`); `modes.ModeDetector(archive_after_s=1800)` with `.observe(report, now=None) -> str`, `.declare(mode, source="flag")`, and the attributes `.mode`, `.source`, `.evidence`.

**Note on spec drift:** the spec places inference "during `feeds.resolve()`". `resolve()` is stateless and called fresh on every 30 s poll, and the question — *what changed since last time?* — cannot be answered from one report. The detector is therefore its own stateful object, owned by the `Aggregator` (Task 13). `resolve()` stays pure and the detector becomes independently testable, which is what the spec's tests ask for.

- [ ] **Step 1: Add the inode to feed file entries**

In `dashboard/rc_dashboard/feeds.py`, in `resolve_dated`, extend the appended dict:

```python
        rep["files"].append({"path": p, "size": st.st_size, "mtime": st.st_mtime,
                             "ino": st.st_ino,
                             "host": host_of(p)})
```

`host_of()` replaced `os.path.basename(p)[:-6]` when the collector began writing
two files per host-day: the bare stem is no longer the hostname. Only the `ino`
key is being added here — leave the `host` expression alone.

- [ ] **Step 2: Write the failing tests**

Create `dashboard/tests/test_modes.py`:

```python
import unittest

from rc_dashboard.modes import ARCHIVE, LIVE, REPLICA, ModeDetector


def report(files):
    """A minimal feed report: [(path, ino, mtime, size), ...]."""
    return {"files": [{"path": p, "ino": i, "mtime": m, "size": s}
                      for (p, i, m, s) in files]}


class ModeDetectorTest(unittest.TestCase):
    def test_starts_live_and_says_it_has_no_evidence(self):
        d = ModeDetector()
        self.assertEqual(d.mode, LIVE)
        self.assertEqual(d.source, "inferred")

    def test_appending_in_place_is_live(self):
        d = ModeDetector()
        d.observe(report([("a", 1, 1000.0, 10)]), now=1000.0)
        self.assertEqual(d.observe(report([("a", 1, 1060.0, 20)]), now=1060.0), LIVE)

    def test_new_inode_for_a_known_path_is_a_replica(self):
        d = ModeDetector()
        d.observe(report([("a", 1, 1000.0, 10)]), now=1000.0)
        self.assertEqual(d.observe(report([("a", 2, 1300.0, 20)]), now=1300.0), REPLICA)
        self.assertIn("inode", d.evidence)

    def test_quiescence_becomes_archive_only_after_archive_after_s(self):
        d = ModeDetector(archive_after_s=1800)
        d.observe(report([("a", 1, 1000.0, 10)]), now=1000.0)
        # quiet, but not yet long enough: still live, and still warning
        self.assertEqual(d.observe(report([("a", 1, 1000.0, 10)]), now=2000.0), LIVE)
        self.assertEqual(d.observe(report([("a", 1, 1000.0, 10)]), now=3000.0), ARCHIVE)

    def test_startup_never_infers_archive(self):
        """One poll is not evidence of quiescence, however old the file is."""
        d = ModeDetector(archive_after_s=1800)
        self.assertEqual(d.observe(report([("a", 1, 0.0, 10)]), now=999999.0), LIVE)

    def test_archive_exits_when_data_resumes(self):
        d = ModeDetector(archive_after_s=1800)
        d.observe(report([("a", 1, 1000.0, 10)]), now=1000.0)
        d.observe(report([("a", 1, 1000.0, 10)]), now=3000.0)
        self.assertEqual(d.mode, ARCHIVE)
        self.assertEqual(d.observe(report([("a", 1, 3100.0, 20)]), now=3100.0), LIVE)

    def test_a_declared_mode_is_never_revised(self):
        d = ModeDetector()
        d.declare(ARCHIVE, source="flag")
        d.observe(report([("a", 1, 1000.0, 10)]), now=1000.0)
        d.observe(report([("a", 2, 2000.0, 20)]), now=2000.0)
        self.assertEqual(d.mode, ARCHIVE)
        self.assertEqual(d.source, "flag")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_modes -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rc_dashboard.modes'`.

- [ ] **Step 4: Write the detector**

Create `dashboard/rc_dashboard/modes.py`:

```python
"""Which kind of input is this: a live collector, a synced replica, or a frozen
archive?  One object decides, and it always reports how it decided.

The classification is an inference, so it is labelled as one.  `mode_source`
travels with `mode` everywhere the page shows it, and `--mode` overrides.  A
wrong guess that says it is a guess is survivable; a wrong guess presented as
fact is not.

KNOWN LIMIT: an `rsync --inplace` replica preserves inodes and appends, so it is
indistinguishable here from a live collector writing in bursts.  It is reported
as `live`, which keeps the record-lag warning -- the safe error.  An operator
running `--inplace` passes `--mode replica`.
"""
import time

LIVE = "live"
REPLICA = "replica"
ARCHIVE = "archive"


class ModeDetector:
    """Stateful, because the question is about change between polls.

    `feeds.resolve()` is pure and cannot answer it; one report shows what is on
    disk, never what moved.
    """

    def __init__(self, archive_after_s=1800):
        self.archive_after_s = archive_after_s
        self.mode = LIVE
        self.source = "inferred"
        self.evidence = "no polls yet"
        self._prev = {}

    def declare(self, mode, source="flag"):
        """An operator's statement. Never revised by observation."""
        self.mode, self.source = mode, source
        self.evidence = "declared via %s" % source

    def observe(self, report, now=None):
        now = time.time() if now is None else now
        if self.source != "inferred":
            return self.mode

        cur = {f["path"]: (f.get("ino"), f["mtime"], f["size"])
               for f in (report.get("files") or [])}
        known = [p for p in cur if p in self._prev]
        reinoded = [p for p in known if cur[p][0] != self._prev[p][0]]
        advanced = [p for p in known if cur[p][1:] != self._prev[p][1:]]
        had_history = bool(self._prev)
        self._prev = cur

        if reinoded:
            self._set(REPLICA, "%d of %d file(s) replaced with a new inode"
                      % (len(reinoded), len(cur)))
        elif advanced:
            self._set(LIVE, "%d of %d file(s) grew in place"
                      % (len(advanced), len(cur)))
        elif had_history and cur:
            # Nothing moved. Only call it an archive once it has been quiet long
            # enough that a stale warning would have fired first -- misfiling an
            # outage as an archive would suppress the warning entirely.
            newest = max(m for _i, m, _s in cur.values())
            quiet = now - newest
            if quiet > self.archive_after_s:
                self._set(ARCHIVE, "no file has advanced; newest is %d s old"
                          % int(quiet))
        return self.mode

    def _set(self, mode, evidence):
        self.mode, self.evidence = mode, evidence
        self.source = "inferred"
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 38 tests.

- [ ] **Step 6: Commit**

```bash
git add dashboard/rc_dashboard/modes.py dashboard/rc_dashboard/feeds.py dashboard/tests/test_modes.py
git commit -m "Add ModeDetector: live, replica or archive, and how it decided

Inference lives in one stateful object rather than in feeds.resolve(),
which is pure and cannot see what moved between polls. Archive is only
inferred after a quiet period well past the stale threshold, so an
outage warns before it is reframed as an archive."
```

---

### Task 10: Two clocks, never merged

**Files:**
- Modify: `dashboard/rc_dashboard/feeds.py:92-103` (`_blank`), `:173-184` (the lags and status), `:284-289` (`panel_meta`)
- Test: `dashboard/tests/test_feeds.py` (create)

**Interfaces:**
- Consumes: `modes.LIVE/REPLICA/ARCHIVE`.
- Produces: `feeds.apply_mode(rep, mode, source, evidence, sync_max_age_s=None) -> dict`. Reports gain `sync_lag_s`, `sync_lag`, `mode`, `mode_source`, `mode_evidence`, `as_of`. `panel_meta` gains `_mode`, `_mode_source`, `_sync_lag_s`, `_as_of`.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/tests/test_feeds.py`:

```python
import unittest

from rc_dashboard import feeds as feedmod
from rc_dashboard.modes import ARCHIVE, LIVE, REPLICA


def rep(status="ok", lag_s=120.0, sync_lag_s=60.0, max_age_s=300):
    return {"name": "ebpf", "status": status, "exists": True, "n_files": 2,
            "lag_s": lag_s, "sync_lag_s": sync_lag_s, "max_age_s": max_age_s,
            "newest_record_ts": "2026-09-19 10:00:00", "configured": [],
            "notice": None, "env": None, "cli": None}


class TwoClocksTest(unittest.TestCase):
    def test_healthy_replica_is_not_stale(self):
        """A 5-minute rsync against max_age_s=300 must not read as stale."""
        r = feedmod.apply_mode(rep(lag_s=420.0, sync_lag_s=60.0),
                               REPLICA, "inferred", "x")
        self.assertEqual(r["status"], "ok")

    def test_replica_record_lag_is_still_reported(self):
        r = feedmod.apply_mode(rep(lag_s=10800.0, sync_lag_s=60.0),
                               REPLICA, "inferred", "x")
        self.assertEqual(r["status"], "ok")          # the sync is healthy
        self.assertEqual(r["lag_s"], 10800.0)        # and the collector is not
        self.assertEqual(r["sync_lag_s"], 60.0)

    def test_dead_sync_is_stale(self):
        r = feedmod.apply_mode(rep(lag_s=60.0, sync_lag_s=5000.0),
                               REPLICA, "inferred", "x")
        self.assertEqual(r["status"], "stale")

    def test_archive_is_never_stale_and_states_as_of(self):
        r = feedmod.apply_mode(rep(lag_s=9e6, sync_lag_s=9e6),
                               ARCHIVE, "flag", "x")
        self.assertEqual(r["status"], "archive")
        self.assertEqual(r["as_of"], "2026-09-19 10:00:00")

    def test_live_status_is_untouched(self):
        r = feedmod.apply_mode(rep(status="stale"), LIVE, "inferred", "x")
        self.assertEqual(r["status"], "stale")

    def test_a_missing_feed_is_never_relabelled(self):
        r = feedmod.apply_mode({"name": "ebpf", "status": "missing",
                                "exists": False, "n_files": 0, "lag_s": None,
                                "sync_lag_s": None, "max_age_s": 300,
                                "newest_record_ts": None},
                               REPLICA, "inferred", "x")
        self.assertEqual(r["status"], "missing")

    def test_mode_fields_are_carried(self):
        r = feedmod.apply_mode(rep(), REPLICA, "inferred", "2 of 3 replaced")
        self.assertEqual(r["mode"], "replica")
        self.assertEqual(r["mode_source"], "inferred")
        self.assertEqual(r["mode_evidence"], "2 of 3 replaced")


class PanelMetaTest(unittest.TestCase):
    def test_panel_meta_carries_the_mode(self):
        r = feedmod.apply_mode(rep(), REPLICA, "inferred", "x")
        meta = feedmod.panel_meta(r)
        self.assertEqual(meta["_mode"], "replica")
        self.assertEqual(meta["_mode_source"], "inferred")
        self.assertEqual(meta["_sync_lag_s"], 60.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_feeds -v
```

Expected: FAIL with `AttributeError: module 'rc_dashboard.feeds' has no attribute 'apply_mode'`.

- [ ] **Step 3: Add the new keys to the blank report**

In `_blank()`, add to the returned dict:

```python
            "sync_lag_s": None, "sync_lag": None, "as_of": None,
            "mode": "live", "mode_source": "inferred", "mode_evidence": None,
```

- [ ] **Step 4: Compute the sync clock in `resolve_dated`**

After the existing `rep["newest_mtime"] = ...` line, add:

```python
    rep["sync_lag_s"] = (datetime.now()
                         - datetime.fromtimestamp(newest["mtime"])).total_seconds()
    rep["sync_lag"] = fmt_age(rep["sync_lag_s"])
```

Leave the existing record-lag block and the `status` line exactly as they are: that remains the live rule, and `apply_mode` refines it when the mode is not live.

- [ ] **Step 5: Add `apply_mode`**

Add to `dashboard/rc_dashboard/feeds.py`, above `panel_meta`:

```python
def apply_mode(rep, mode, source, evidence, sync_max_age_s=None):
    """Refine a resolved report once the mode is known.

    Two clocks, and they are never merged.  `lag_s` is how old the newest
    MEASUREMENT is; `sync_lag_s` is how old our COPY is.  A healthy sync
    carrying a dead collector's records must read as "synced 1m ago, collector
    last reported 3h ago" -- two facts, two flags -- because "zero" and "the
    collector did not report" are different facts and so are these.
    """
    rep["mode"] = mode
    rep["mode_source"] = source
    rep["mode_evidence"] = evidence

    # A feed with nothing in it is missing or empty whatever the mode is.
    if not (rep.get("exists") and rep.get("n_files")):
        return rep

    if mode == "archive":
        rep["status"] = "archive"
        rep["as_of"] = rep.get("newest_record_ts")
    elif mode == "replica":
        limit = sync_max_age_s or (2 * (rep.get("max_age_s") or 300))
        lag = rep.get("sync_lag_s")
        rep["status"] = "stale" if (lag is not None and lag > limit) else "ok"
    return rep
```

- [ ] **Step 6: Carry the mode into every panel**

Extend `panel_meta`:

```python
def panel_meta(rep):
    """The block every panel carries so one renderer handles every empty state."""
    return {"_feed": rep["name"], "_status": rep["status"],
            "_present": rep["status"] in ("ok", "stale", "degraded", "archive"),
            "_paths": rep["configured"], "_notice": rep["notice"],
            "_env": rep["env"], "_cli": rep["cli"],
            "_mode": rep.get("mode", "live"),
            "_mode_source": rep.get("mode_source", "inferred"),
            "_sync_lag_s": rep.get("sync_lag_s"),
            "_as_of": rep.get("as_of")}
```

Note `"archive"` joining the `_present` tuple: an archive has data, so its panels render rather than showing an empty state.

- [ ] **Step 7: Show both clocks in `--check-feeds`**

In `print_table`, add a `sync` column between `lag` and `path`, using `r.get("sync_lag") or "-"`, and widen the header to match. An operator reading the table must be able to tell a dead sync from a dead collector.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 46 tests.

- [ ] **Step 9: Commit**

```bash
git add dashboard/rc_dashboard/feeds.py dashboard/tests/test_feeds.py
git commit -m "Two freshness clocks, reported independently

The report already carried both newest_mtime and newest_record_ts and
then collapsed them into one status, so a healthy 5-minute rsync against
max_age_s=300 read as stale. Sync lag and record lag are now separate
facts with separate thresholds, and apply_mode refines status once the
mode is known."
```

---

### Task 11: Anchor the window for an archive

**Files:**
- Modify: `dashboard/rc_dashboard/buckets.py:38-46` (`__init__`), `:50-73` (`add`), `:76-113` (`evict`, `series`, `last_complete_bin`)
- Test: `dashboard/tests/test_buckets.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `TimeBuckets.anchor: float | None` (default `None`) and `TimeBuckets._now() -> float`.

**Note on spec drift:** the spec says `add()` gains a `now=None` parameter. `add()` takes `**counters`, so a keyword named `now` could collide with a counter, and `live.py` calls `add()` from several places that would all need threading. An `anchor` attribute is collision-free, set once by the aggregator, and gives `evict`/`series`/`last_complete_bin` the same default for free. The behaviour the spec asks for is unchanged: in archive mode the window anchors on the archive's newest record.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/tests/test_buckets.py`:

```python
import time
import unittest

from rc_dashboard.buckets import TimeBuckets

MONTH_AGO = time.time() - 30 * 86400


def rec(epoch):
    return {"_ts_epoch": epoch, "event": "exit"}


class ArchiveAnchorTest(unittest.TestCase):
    def test_old_records_are_dropped_without_an_anchor(self):
        """Today's behaviour, and why an archive renders empty."""
        b = TimeBuckets()
        self.assertFalse(b.add(rec(MONTH_AGO), "agent"))

    def test_an_anchored_window_accepts_them(self):
        b = TimeBuckets()
        b.anchor = MONTH_AGO
        self.assertTrue(b.add(rec(MONTH_AGO), "agent"))

    def test_the_anchor_still_bounds_the_window(self):
        """Anchoring moves the window, it does not remove it."""
        b = TimeBuckets(window_s=3600)
        b.anchor = MONTH_AGO
        self.assertFalse(b.add(rec(MONTH_AGO - 7200), "agent"))

    def test_series_defaults_to_the_anchor(self):
        b = TimeBuckets(window_s=3600, bin_s=60)
        b.anchor = MONTH_AGO
        b.add(rec(MONTH_AGO), "agent")
        self.assertEqual(sum(v or 0 for v in b.series(["agent"])["agent"]), 1)

    def test_no_anchor_means_wall_clock(self):
        b = TimeBuckets()
        self.assertIsNone(b.anchor)
        self.assertTrue(b.add(rec(time.time()), "agent"))


if __name__ == "__main__":
    unittest.main()
```

If `series()` returns a different shape than `{cls: [...]}`, adjust `test_series_defaults_to_the_anchor` to that shape — read `buckets.py:82-110` first and assert on the real return value rather than changing the code to fit the test.

- [ ] **Step 2: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_buckets -v
```

Expected: FAIL. `test_an_anchored_window_accepts_them` fails because `anchor` is not an attribute and `add` consults `time.time()` regardless.

- [ ] **Step 3: Add the anchor**

In `TimeBuckets.__init__`, add:

```python
        self.anchor = None      # archive mode: anchor the window on the data
```

and add the accessor:

```python
    def _now(self):
        """Wall clock, or the archive's own newest record when anchored.

        An archive captured last month is not 'late'; it ends when it ends. With
        no anchor the window would reject every record in it and the live tier
        would render empty.
        """
        return time.time() if self.anchor is None else self.anchor
```

- [ ] **Step 4: Use it**

In `add()`, change `now = time.time()` to `now = self._now()`.

In `evict()`, `series()` and `last_complete_bin()`, change `now = now or time.time()` to `now = self._now() if now is None else now`. (`or` also replaced a caller's explicit `0`; `is None` is the correct test and the behaviour is otherwise identical.)

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 51 tests.

- [ ] **Step 6: Commit**

```bash
git add dashboard/rc_dashboard/buckets.py dashboard/tests/test_buckets.py
git commit -m "Let the rolling window anchor on the data, not the wall clock

TimeBuckets.add() compared every record against time.time() and dropped
anything older than the window, so pointing the dashboard at a month-old
archive rendered an empty live tier. An optional anchor moves the window
onto the archive's own newest record; unset, behaviour is unchanged."
```

---

### Task 12: Config keys and the `--mode` flag

**Files:**
- Modify: `dashboard/rc_dashboard/config.py` (`defaults`, `ENV_MAP`, `CLI_MAP`), `dashboard/rc_dashboard/__main__.py:12-28`
- Test: extend `dashboard/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: config leaves `ingest.mode` (default `None`, meaning infer), `ingest.archive_after_s` (default `1800`), `feeds.ebpf.sync_max_age_s` (default `None`, meaning `2 x max_age_s`). CLI flag `--mode`, env var `RC_DASH_MODE`.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/tests/test_config.py`:

```python
class TestModeConfig(unittest.TestCase):
    def test_mode_defaults_to_inference(self):
        self.assertIsNone(config.defaults()["ingest"]["mode"])

    def test_archive_after_default(self):
        self.assertEqual(config.defaults()["ingest"]["archive_after_s"], 1800)

    def test_sync_max_age_defaults_to_unset(self):
        self.assertIsNone(config.defaults()["feeds"]["ebpf"]["sync_max_age_s"])

    def test_mode_has_an_env_var(self):
        self.assertIn("ingest.mode", config.ENV_MAP)
        self.assertEqual(config.ENV_MAP["ingest.mode"][0], "RC_DASH_MODE")

    def test_mode_is_wired_to_the_cli(self):
        self.assertEqual(config.CLI_MAP["mode"], "ingest.mode")


class TestModeCli(unittest.TestCase):
    def test_mode_flag_parses_and_defaults_to_none(self):
        from rc_dashboard.__main__ import build_parser
        self.assertIsNone(build_parser().parse_args([]).mode)
        self.assertEqual(build_parser().parse_args(["--mode", "replica"]).mode,
                         "replica")

    def test_mode_flag_rejects_an_unknown_mode(self):
        from rc_dashboard.__main__ import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--mode", "nonsense"])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd dashboard && python3 -m unittest tests.test_config -v
```

Expected: FAIL with `KeyError: 'mode'` and `AttributeError: 'Namespace' object has no attribute 'mode'`.

- [ ] **Step 3: Add the config leaves**

In `config.defaults()`, change the `ingest` block and add the feed key:

```python
        "ingest": {"poll_ms": 1000,
                   # None = infer. See rc_dashboard/modes.py.
                   "mode": None,
                   # Quiet for this long before a feed is called an archive.
                   # Well above sync_max_age_s on purpose: an outage must warn
                   # before it is reframed as an archive.
                   "archive_after_s": 1800},
```

and inside `feeds.ebpf`, beside `max_age_s`:

```python
                "sync_max_age_s": None,   # None = 2 x max_age_s
```

- [ ] **Step 4: Wire env and CLI**

Add to `ENV_MAP`:

```python
    "ingest.mode": ("RC_DASH_MODE", str),
    "ingest.archive_after_s": ("RC_DASH_ARCHIVE_AFTER_S", int),
    "feeds.ebpf.sync_max_age_s": ("RC_DASH_EBPF_SYNC_MAX_AGE_S", int),
```

Add to `CLI_MAP`:

```python
    "mode": "ingest.mode",
```

- [ ] **Step 5: Add the flag**

In `dashboard/rc_dashboard/__main__.py`, in `build_parser`, beside the other flags:

```python
    ap.add_argument("--mode", default=None,
                    choices=["live", "replica", "archive"],
                    help="override the inferred input mode (default: infer)")
```

The `default=None` matters: every flag here defaults to None so an unset flag never outranks the config file.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 58 tests.

- [ ] **Step 7: Verify the flag reaches the config**

```bash
cd dashboard && uv run python -m rc_dashboard --mode archive print-config | grep -A 2 '"mode"'
```

Expected: the resolved value `archive` with origin `cli`.

- [ ] **Step 8: Commit**

```bash
git add dashboard/rc_dashboard/config.py dashboard/rc_dashboard/__main__.py dashboard/tests/test_config.py
git commit -m "Add --mode, RC_DASH_MODE and the archive/sync thresholds

Mode defaults to None, meaning infer. archive_after_s sits well above
sync_max_age_s so a feed that goes quiet warns as stale long before it
is reclassified as an archive."
```

---

### Task 13: Wire the detector into the aggregator

**Files:**
- Modify: `dashboard/rc_dashboard/aggregator.py:38-56` (`__init__`), `:119-125` (`poll_once`), `:153-165` (`refresh_feeds`)
- Test: `dashboard/tests/test_aggregator_modes.py` (create)

**Interfaces:**
- Consumes: `modes.ModeDetector`, `feeds.apply_mode`, `TimeBuckets.anchor`.
- Produces: `Aggregator.detector: ModeDetector`; `Aggregator.mode -> str`; `poll_once()` returns `0` without reading in archive mode.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/tests/test_aggregator_modes.py`:

```python
import unittest

from rc_dashboard.modes import ARCHIVE, REPLICA


class FakeSource:
    def __init__(self):
        self.polls = 0
        self.stats = {}

    def poll(self, cold_start_allowed=True):
        self.polls += 1
        return iter(())

    def prune_state(self):
        pass

    def save_state(self):
        pass


class ArchiveSkipsIngestTest(unittest.TestCase):
    def _agg(self):
        from rc_dashboard import config
        from rc_dashboard.aggregator import Aggregator
        cfg = config.Config(config.defaults(), {}, None)
        agg = Aggregator(cfg)
        agg.source = FakeSource()
        return agg

    def test_a_declared_archive_does_not_re_read(self):
        agg = self._agg()
        agg.detector.declare(ARCHIVE, source="flag")
        agg.poll_once()
        self.assertEqual(agg.source.polls, 0)

    def test_a_live_feed_does_read(self):
        agg = self._agg()
        agg.poll_once()
        self.assertEqual(agg.source.polls, 1)

    def test_mode_is_exposed(self):
        agg = self._agg()
        agg.detector.declare(REPLICA, source="flag")
        self.assertEqual(agg.mode, "replica")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd dashboard && python3 -m unittest tests.test_aggregator_modes -v
```

Expected: FAIL with `AttributeError: 'Aggregator' object has no attribute 'detector'`.

- [ ] **Step 3: Construct the detector**

In `Aggregator.__init__`, after `self.reports = feedmod.resolve(cfg)`:

```python
        self.detector = ModeDetector(
            archive_after_s=int(cfg.get("ingest.archive_after_s", 1800)))
        declared = cfg.get("ingest.mode")
        if declared:
            # Config origins are "cli:--mode", "env:RC_DASH_MODE", "file:<path>".
            # The page only needs to know whether a human said so on the command
            # line or it came from configuration, so collapse to the two the
            # FeedMode/ModeSource union carries.
            origin = cfg.origin("ingest.mode") or ""
            self.detector.declare(
                declared, source="flag" if origin.startswith("cli:") else "config")
```

and add `from .modes import ARCHIVE, ModeDetector` to the imports.

Add the accessor:

```python
    @property
    def mode(self):
        return self.detector.mode
```

- [ ] **Step 4: Skip ingest in archive mode**

Replace `poll_once`:

```python
    def poll_once(self):
        # An archive does not grow. refresh_feeds() keeps its cheap stat sweep,
        # so if data does resume the detector leaves archive mode and this
        # starts reading again -- a mode that stopped watching could never
        # observe its own claim becoming false.
        if self.detector.mode == ARCHIVE:
            return 0
        with self.lock:
            n = self.consume(self.source.poll())
            self.source.prune_state()
            self.source.save_state()
            self.stats["last_poll"] = time.time()
        return n
```

- [ ] **Step 5: Run the detector and apply the mode on every feed refresh**

In `refresh_feeds`, after `self.reports = feedmod.resolve(self.cfg)`:

```python
            ebpf = self.reports.get("ebpf") or {}
            self.detector.observe(ebpf)
            for rep in self.reports.values():
                feedmod.apply_mode(rep, self.detector.mode, self.detector.source,
                                   self.detector.evidence,
                                   self.cfg.get("feeds.%s.sync_max_age_s" % rep["name"]))
            self._anchor_window(ebpf)
```

and add the anchoring helper:

```python
    def _anchor_window(self, ebpf_report):
        """In archive mode the window ends where the data ends."""
        anchor = None
        if self.detector.mode == ARCHIVE:
            files = ebpf_report.get("files") or []
            if files:
                anchor = max(f["mtime"] for f in files)
        for r in self.live.values():
            buckets = getattr(r, "buckets", None)
            if buckets is not None:
                buckets.anchor = anchor
```

The anchor uses the newest file mtime rather than the newest record timestamp: it is already parsed, always present, and within one write interval of the newest record. If the two ever disagree materially, prefer `newest_record_ts` parsed via `feeds.TS_FORMAT`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 61 tests.

- [ ] **Step 7: Verify the service starts and reports a mode**

```bash
cd dashboard && uv run python -m rc_dashboard check-feeds 2>&1 | head -5
```

Expected: the table renders, with the sync column from Task 10.

- [ ] **Step 8: Commit**

```bash
git add dashboard/rc_dashboard/aggregator.py dashboard/tests/test_aggregator_modes.py
git commit -m "Own the mode detector in the aggregator

refresh_feeds feeds the detector and applies the resulting mode to every
report; poll_once stops reading in archive mode while the stat sweep
continues, so a resumed feed leaves archive mode instead of being frozen
forever."
```

---

### Task 14: Say which mode on the page

**Files:**
- Modify: `dashboard/web/src/api/types.ts:33-69`, `dashboard/web/src/App.tsx:38-60` (`headline`) and the header render, `dashboard/web/src/tabs/FeedsTab.tsx`

**Interfaces:**
- Consumes: the API fields from Tasks 10 and 13.
- Produces: `FeedReport` gains `newest_mtime`, `sync_lag_s`, `sync_lag`, `mode`, `mode_source`, `mode_evidence`, `as_of`; `FeedStatus` gains `'archive'`; `PanelEnvelope` gains `_mode`, `_mode_source`, `_sync_lag_s`, `_as_of`.

**Why this is not only a banner:** `headline()` recomputes staleness client-side as `lag_s > max_age_s`, duplicating the server's live rule. Left alone it would keep crying stale on a healthy replica even though the server says `ok`.

- [ ] **Step 1: Extend the types**

In `dashboard/web/src/api/types.ts`:

```ts
export type FeedStatus = 'ok' | 'stale' | 'empty' | 'missing' | 'degraded' | 'archive';
export type FeedMode = 'live' | 'replica' | 'archive';
export type ModeSource = 'inferred' | 'flag' | 'config';
```

Add to `interface FeedReport`:

```ts
  newest_mtime: string | null;
  sync_lag_s: number | null;
  sync_lag: string | null;
  mode: FeedMode;
  mode_source: ModeSource;
  mode_evidence: string | null;
  as_of: string | null;
```

Add to `interface PanelEnvelope`:

```ts
  _mode: FeedMode;
  _mode_source: ModeSource;
  _sync_lag_s: number | null;
  _as_of: string | null;
```

- [ ] **Step 2: Make `headline()` mode-aware**

Replace the `stale` computation in `dashboard/web/src/App.tsx`'s `headline()`:

```ts
  // Staleness is mode-dependent, and the server has already decided it. A
  // replica's records are necessarily older than its last sync, so judging one
  // by `lag_s` alone reports every healthy replica as stale.
  const stale = pool.filter((f) => {
    if (f.mode === 'archive') return false;
    if (f.mode === 'replica') return f.status === 'stale';
    return f.lag_s != null && f.max_age_s != null && f.lag_s > f.max_age_s;
  });
```

and extend the `bad` chain with `pool.find((f) => f.status === 'archive') ? undefined : ...` — an archive is not a fault, so it must not be picked up as one. Concretely, leave `bad` as it is: `'archive'` is not in the chain's list of statuses, so it is already ignored.

- [ ] **Step 3: Render the banner**

Add above the tab content in `App.tsx`, driven entirely by the feed report:

```tsx
function ModeBanner({ feeds }: { feeds: Record<string, FeedReport> | null }) {
  const f = feeds?.ebpf;
  if (!f || f.mode === 'live') return null;   // live is the unremarkable case
  const how =
    f.mode_source === 'inferred' ? 'inferred'
    : f.mode_source === 'flag' ? '--mode'
    : 'configured';
  const detail =
    f.mode === 'archive'
      ? `as of ${f.as_of ?? 'unknown'}`
      : `synced ${f.sync_lag ?? '?'} ago`;
  return (
    <div className="mode-banner" role="status">
      <strong>{f.mode}</strong> <span className="mode-how">({how})</span> — {detail}
      {f.mode_evidence ? <span className="mode-why"> · {f.mode_evidence}</span> : null}
    </div>
  );
}
```

Render it as `<ModeBanner feeds={feeds} />` immediately inside the main content area, and import `FeedReport` if it is not already imported.

- [ ] **Step 4: Style it**

In `dashboard/web/src/styles.css`, following the existing pill/panel conventions:

```css
.mode-banner {
  padding: 6px 12px;
  margin-bottom: 10px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--warn);
  background: var(--panel);
  font-size: 13px;
}
.mode-banner .mode-how { opacity: 0.75; }
.mode-banner .mode-why { opacity: 0.6; }
```

If `--line`, `--warn` or `--panel` are not the variable names this stylesheet uses, substitute the equivalents already defined at the top of the file rather than introducing new ones.

- [ ] **Step 5: Show both clocks in FeedsTab**

In `dashboard/web/src/tabs/FeedsTab.tsx`, add a column beside the existing record-lag column:

```tsx
<td title="how old our copy is">{f.sync_lag ?? '—'}</td>
```

with a `Synced` header cell beside the existing lag header. The two must sit side by side and stay separately labelled: a dead sync and a dead collector are different faults.

- [ ] **Step 6: Typecheck and verify the integrity rule**

```bash
cd dashboard/web && npm run typecheck && npm run check:no-random
```

Expected: both pass. `typecheck` will fail first if any of the new fields are missing from `types.ts`.

- [ ] **Step 7: Verify against the dev fixture**

```bash
cd dashboard/web && npm run build
```

Expected: a clean build. The Vite dev fixture answers `/api/*` from `mock/api.json`; if the mock lacks the new fields the banner simply does not render, which is the correct behaviour for a live feed.

- [ ] **Step 8: Commit**

```bash
git add dashboard/web/src
git commit -m "Say which mode the page is showing, and how we know

headline() recomputed staleness client-side with the live rule, so a
healthy replica read as stale even when the server said ok. It is now
mode-aware, the banner states replica/archive with inferred vs declared,
and FeedsTab shows the sync clock beside the record clock."
```

---

### Task 15: Document serving a moved feed

**Files:**
- Modify: `dashboard/README.md`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Add the modes section to the dashboard README**

Add to `dashboard/README.md`:

```markdown
## Serving a feed from somewhere else

The dashboard does not need to run where the collector runs. Copy the collector's
output to the machine serving the page and point the dashboard at the copy.

```bash
rsync -a --inplace --append-verify login0{1,2,3}:/var/log/ebpfm/ /srv/feed/
```

`--inplace --append-verify` keeps inodes stable, which makes the resume an
ordinary append. It is recommended because it is cheaper, **not** because
anything depends on it: ingest is correct under plain `rsync -a`, under `scp`,
and under a hand-copied directory. Getting the flags wrong costs efficiency,
never correctness.

Three modes, inferred from what the files do, overridable with `--mode`:

| mode | what it means | when a feed is stale |
|---|---|---|
| `live` | the collector is writing here | the newest record is older than `max_age_s` |
| `replica` | a copy, refreshed periodically | the last **sync** is older than `sync_max_age_s` (default `2 x max_age_s`) |
| `archive` | a frozen capture | never — the page states `as of <timestamp>` |

Two clocks are reported and never merged. `lag` is how old the newest
measurement is; `sync` is how old the copy is. A healthy sync carrying a dead
collector's records reads as *"synced 1m ago, collector last reported 3h ago"* —
two facts, two flags.

The banner says whether the mode was inferred or declared. One case the
inference cannot resolve: an `rsync --inplace` replica preserves inodes and
appends, so it is indistinguishable from a live collector and is reported as
`live`, keeping the record-lag warning. Pass `--mode replica` for that setup.

```bash
rc-dashboard serve --mode archive --ebpf-root /srv/snapshot-2026-08-14
```
```

- [ ] **Step 2: Point the root README at it**

In the root `README.md`, under **Serve**, add after the existing invocation:

```markdown
The dashboard does not need to run where the collector runs — copy the output
across and point it at the copy. See
[`dashboard/README.md`](dashboard/README.md) for the `rsync` recipe and the
three input modes.
```

- [ ] **Step 3: Verify the guard still passes**

```bash
cd dashboard && python3 -m unittest discover -s tests -q
```

Expected: PASS, 61 tests. The Task 6 guard covers Markdown, so a stray old-repo path in the new prose fails here.

- [ ] **Step 4: Run everything**

```bash
cd dashboard && python3 -m unittest discover -s tests -q \
  && cd web && npm run typecheck && npm run check:no-random \
  && cd ../../collector && python3 -m unittest discover -s tests -q
```

Expected: all three green — 56 dashboard tests, a clean typecheck, 72 collector tests.

- [ ] **Step 5: Commit**

```bash
git add README.md dashboard/README.md
git commit -m "Document serving a moved feed

The three modes, what each claims, the two clocks, the rsync recipe and
the one case the inference cannot resolve."
```

---

## Notes for the implementer

**Test counts are cumulative and approximate.** They are a signal, not an
assertion: if your count differs because you split a test, that is fine. What
matters is that the suite is green and that the test you just wrote failed
before your change and passes after it.

**If a step's code does not fit the surrounding file**, follow the file. These
snippets were written against the tree at `ad19a41` and the repository is
under active edit; match the existing style, naming and comment density rather
than pasting verbatim.

**Two places deliberately lose information** (Task 4): the `1.83x` CPU figure and
the D-state attribution. If you find yourself wanting to keep them, re-read the
spec's Part 2.4 — the decision is that a claim whose evidence is not in this
repository is not asserted from it.

**The one rule that governs the whole of Phase B:** an absence must be visible.
Every reset, every resumed replace, every excluded record and every inferred
mode is counted and reported. If a change of yours makes something disappear
quietly, it is wrong even if the numbers look better.
