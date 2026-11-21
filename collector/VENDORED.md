# Vendored modules

`collector/` is **standalone**. It has no `../` imports, so you can `scp` the folder
to any node (or hand it to an admin as a single `ebpfm.sh bundle` file) and run it
without the rest of the repo. The cost is six copied modules.

| Vendored as | Copied from | Upstream sha256 at vendor time | Local changes |
|---|---|---|---|
| `lib/procparse.py` | `collect/common/procparse.py` | `f4ecb417…42292f` | **added `read_cwd(pid)`** (readlink only) |
| `lib/agent_classify.py` | `collect/common/agent_classify.py` | `0d6528d0…20f9f3` | **`classify()` reads the program, not every word**: argv[0], past wrappers (`env`, `nohup`, …), a package runner's package, an interpreter's script, a shell's `-c` command's first word, and every word of a `bwrap`; plus the runtime artifacts only an agent writes, where it writes them (Claude Code's tool-call shell; `CURSOR_SANDBOX` and `WINDSURF_STATE` in the environment a program is run with (leading or `env` assignments, a bwrap's `--setenv`); `windsurf-terminal` as a program). Upstream matches its patterns anywhere, so a human's `vim ~/.claude/CLAUDE.md` is an agent there. Added patterns: `/claude/versions/`, `@anthropic-ai/claude-code`, `claude-code/cli.js`; under `/.claude/` only Claude Code's installs match (`local/`, `remote/`, its `cli.js`), not a hook, a status line or a project's `.claude/worktrees/`. Upstream, `snapshot.py`'s copy and `analyze/agent_lib.py` want the same change |
| `lib/provider_cidrs.py` | `collect/nettcp/provider_cidrs.py` | `13f4b613…e6bad8` | none |
| `node_snapshot.py` | `collect/common/snapshot.py` | `469489b0…799522` | **GPU sections dropped.** `nvidia-smi` is never run, and `gpu`/`gpu_pmon`/`gpu_compute_apps` are always null. The keys stay in place because other code depends on the key order. Login nodes have no GPUs; upstream still collects them for compute nodes. **Schema 8 of the eBPF tier:** argv fields are masked with `lib/redact.py` (`EBPFM_REDACT`), and `ts_epoch`, `tz`, `redact` are APPENDED after every existing key, so the existing order is unchanged. **Its inline classifier (`classify_agent`, for `proc_tree.agents`) is a copy of `lib/agent_classify.py`'s `classify()`**, including the local change; `ClassifyTest` in `tests/test_ebpfm.py` checks that the two stay equal |
| `lib/redact.py` (credential rules only) | `dashboard/rc_dashboard/redact.py` | — | **`MASK`, and everything from `_NOT_A_REF` to the end of `RULES` verbatim** (the rules, their helpers, `TEXT_MAX`, `CUT`, `cut_text`). Prompt masking and the entry points are new. Whenever the dashboard is alongside, `tests/test_redact.py` checks that this span is still identical, word for word |
| `lib/slurm_args.py` | `analyze/switch_submitline_time.py` | see `.vendored.sha256` | **`parse_cli_time` returns `None`, not `numpy.nan`**, because the collector has no numpy and null is the correct value for "absent". The flag parsers are new |

`node_snapshot.py` is the **node tier** (`ebpfm.sh start` runs it unprivileged on a
300 s loop). It is vendored rather than imported for the same reason as the other
modules: the bundle has to run on a node with no repo. `collect/common/snapshot.py`
remains the canonical copy. `collect/compute/` still uses it directly, and it is the
file to edit.

The modules were vendored from repo commit `9a76f14`. The three sources were last
changed upstream in `fb6c9ab` (2026-08-01). Full hashes are in `.vendored.sha256`, the
machine-readable form of the third column.

## Why copies and not symlinks

A symlink breaks as soon as the folder is copied off the machine, and being copied off
the machine is exactly what this folder exists for. `sys.path` manipulation has the
same problem.

## The cost, stated plainly

`CLAUDE.md` records that the agent-classification ruleset **already exists in three
copies** that must stay in sync: `collect/common/agent_classify.py`, the inline copy in
`collect/common/snapshot.py`, and `analyze/agent_lib.py` (which is canonical for the
label set). This directory adds a **fourth**. That is real maintenance debt, and we took
it on knowingly.

## The sync rule

**When a repo is visible alongside** this folder, `ebpfm.sh check` compares each
recorded upstream sha256 against the corresponding file in the repo:

```
-- vendored libraries
  ok    procparse.py                   upstream unchanged
  WARN  agent_classify.py              UPSTREAM MOVED — re-vendor (collect/common/agent_classify.py)
```

It checks for *upstream movement* on purpose, rather than diffing the two files.
`procparse.py` carries a local addition, so a diff would always show a difference and
the warning would be noise.

On a node with no repo alongside, it prints `standalone (no repo alongside; nothing to
compare)`. That is the expected state on a deployed collector.

**To re-vendor** after a WARN:

```bash
cp ../collect/common/agent_classify.py lib/agent_classify.py   # re-apply local changes if any
sha256sum ../collect/common/agent_classify.py                  # update .vendored.sha256
```

If the label set itself changes, `analyze/agent_lib.py` is canonical. Reconcile against
that file, not against whichever copy happened to change first.
