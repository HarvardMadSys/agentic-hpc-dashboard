# Vendored modules

`collector/` is **standalone**: it has no `../` imports, so the folder can be
`scp`'d to any node — or handed to an admin as a single `ebpfm.sh bundle` file — and
run without the rest of the repo. The price is five copied modules.

| Vendored as | Copied from | Upstream sha256 at vendor time | Local changes |
|---|---|---|---|
| `lib/procparse.py` | `collect/common/procparse.py` | `f4ecb417…92292f` | **added `read_cwd(pid)`** (readlink only) |
| `lib/agent_classify.py` | `collect/common/agent_classify.py` | `0d6528d0…20f9f3` | none |
| `lib/provider_cidrs.py` | `collect/nettcp/provider_cidrs.py` | `13f4b613…e6bad8` | none |
| `node_snapshot.py` | `collect/common/snapshot.py` | `469489b0…799522` | none |
| `lib/slurm_args.py` | `analyze/switch_submitline_time.py` | see `.vendored.sha256` | **`parse_cli_time` returns `None`, not `numpy.nan`** (the collector has no numpy, and null is the honest absent value); the flag parsers are new |

`node_snapshot.py` is the **node tier** (`ebpfm.sh start` runs it unprivileged on a
300 s loop). It is vendored rather than imported for the same reason as the rest: the
bundle has to run on a node with no repo. `collect/common/snapshot.py` stays canonical —
`collect/compute/` still uses it directly, and it is the file to edit.

Vendored from repo commit `9a76f14`; the three sources were last touched upstream in
`fb6c9ab` (2026-08-01). Full hashes are in `.vendored.sha256`, which is the machine-readable
form of the third column.

## Why copies and not symlinks

A symlink breaks the moment the folder is copied off the machine, which is the one
thing this folder exists to support. `sys.path` manipulation has the same problem.

## The cost, stated plainly

`CLAUDE.md` records that the agent-classification ruleset **already exists in three
copies** that must stay in sync — `collect/common/agent_classify.py`,
`collect/common/snapshot.py`'s inline copy, and `analyze/agent_lib.py` (the last being
canonical for the label set). This directory adds a **fourth**. That is a real
maintenance debt, not an oversight.

## The sync rule

`ebpfm.sh check` compares each recorded upstream sha256 against the file in the repo
**when a repo is visible alongside** this folder:

```
-- vendored libraries
  [ OK ] procparse.py         upstream unchanged
  [WARN] agent_classify.py    UPSTREAM MOVED — re-vendor (collect/common/agent_classify.py)
```

It deliberately checks *upstream movement*, not a plain diff — `procparse.py` carries a
local addition, so a diff would always differ and the warning would be noise.

On a node with no repo alongside it prints `standalone (no repo alongside; nothing to
compare)`, which is the expected state on a deployed collector.

**To re-vendor** after a WARN:

```bash
cp ../collect/common/agent_classify.py lib/agent_classify.py   # re-apply local changes if any
sha256sum ../collect/common/agent_classify.py                  # update .vendored.sha256
```

If the label set itself changes, `analyze/agent_lib.py` is canonical — reconcile against
that one, not against whichever copy changed first.
