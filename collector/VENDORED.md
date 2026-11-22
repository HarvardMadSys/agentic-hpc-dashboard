# Vendored modules

`collector/` is **standalone**: it has no `../` imports, so the folder, or the one file
`ebpfm.sh bundle` writes, runs on any node without the rest of the repo. The cost is six
copied modules. They are copies, not symlinks or `sys.path` changes, because either of those
breaks as soon as the folder is copied off the machine, which is what it exists for.

| Vendored as | Copied from | Upstream sha256 at vendor time | Local changes |
|---|---|---|---|
| `lib/procparse.py` | `collect/common/procparse.py` | `f4ecb417…42292f` | **added `read_cwd(pid)`** (readlink only) |
| `lib/agent_classify.py` | `collect/common/agent_classify.py` | `0d6528d0…20f9f3` | **`classify()` reads the program, not every word** ([README](README.md#agent-tags-the-program-not-a-mention)): argv[0], past wrappers (`env`, `nohup`, …), a package runner's package, an interpreter's script, a shell's `-c` command's first word, and every word of a `bwrap`; plus the runtime artifacts only an agent writes, where it writes them (Claude Code's tool-call shell; `CURSOR_SANDBOX` and `WINDSURF_STATE` in the environment a program is run with; `windsurf-terminal` as a program). Added patterns: `/claude/versions/`, `@anthropic-ai/claude-code`, `claude-code/cli.js`; under `/.claude/` only Claude Code's installs match (`local/`, `remote/`, its `cli.js`), not a hook, a status line or a project's `.claude/worktrees/`. Upstream, `snapshot.py`'s copy and `analyze/agent_lib.py` want the same change |
| `lib/provider_cidrs.py` | `collect/nettcp/provider_cidrs.py` | `13f4b613…e6bad8` | none |
| `node_snapshot.py` | `collect/common/snapshot.py` | `469489b0…799522` | **GPU sections dropped**: `nvidia-smi` is never run, and `gpu`/`gpu_pmon`/`gpu_compute_apps` are always null, kept in place because other code depends on the key order (login nodes have no GPUs; upstream still collects them on compute nodes). **Argv fields masked** with `lib/redact.py` (`EBPFM_REDACT`), and `ts_epoch`, `tz`, `redact` APPENDED after every existing key, so the order is unchanged. **Its inline classifier** (`classify_agent`, for `proc_tree.agents`) is a copy of `lib/agent_classify.py`'s `classify()`, local change included; `ClassifyTest` in `tests/test_ebpfm.py` checks that the two agree |
| `lib/redact.py` (credential rules only) | `dashboard/rc_dashboard/redact.py` | — | **`MASK`, and everything from `_NOT_A_REF` to the end of `RULES` verbatim** (the rules, their helpers, `TEXT_MAX`, `CUT`, `cut_text`). Prompt masking and the entry points are new. Whenever the dashboard is alongside, `tests/test_redact.py` checks that this span is still identical, word for word |
| `lib/slurm_args.py` | `analyze/switch_submitline_time.py` | see `.vendored.sha256` | **`parse_cli_time` returns `None`, not `numpy.nan`**, because the collector has no numpy and null is the correct value for "absent". The flag parsers are new |

Full hashes are in `.vendored.sha256`, the machine-readable form of the third column.
`collect/common/snapshot.py` remains the canonical copy of the node tier, and the file to
edit: compute nodes still run it directly.

## The cost, stated plainly

The agent-classification rules already exist upstream in three copies that must stay in
sync: `collect/common/agent_classify.py`, the inline copy in `collect/common/snapshot.py`,
and `analyze/agent_lib.py` (canonical for the label set). This folder carries two more,
`lib/agent_classify.py` and `node_snapshot.py`'s inline copy. That is real maintenance debt,
taken on knowingly.

## The sync rule

When this folder sits in the measurement repository, beside `collect/` and `analyze/`,
`ebpfm.sh check` compares each recorded upstream sha256 against the file there:

```
-- vendored libraries
  ok    procparse.py                   upstream unchanged
  WARN  agent_classify.py              UPSTREAM MOVED — re-vendor (collect/common/agent_classify.py)
```

It checks for *upstream movement* rather than diffing the two files, because a file with a
local change would always differ. Anywhere else, a deployed collector or this repository, it
prints `standalone (no repo alongside; nothing to compare)`.

**To re-vendor** after a WARN:

```bash
cp ../collect/common/agent_classify.py lib/agent_classify.py   # re-apply local changes if any
sha256sum ../collect/common/agent_classify.py                  # update .vendored.sha256
```

If the label set itself changes, `analyze/agent_lib.py` is canonical. Reconcile against that
file, not against whichever copy happened to change first.
