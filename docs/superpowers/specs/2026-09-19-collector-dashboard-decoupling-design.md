# Decoupling the collector from the dashboard

Date: 2026-09-19
Status: awaiting spec review. Parts 1, 2 and 4 follow decisions agreed in chat; Part 3
was found while writing this spec and has not been agreed.

## Problem

Two things are true of this repository that the README does not yet say.

**The dashboard cannot serve a copied feed correctly.** The deployment we want is the
dashboard running off-cluster, with the collector's JSONL moved to it by an operator's
`rsync`. `rsync` and `scp` write a temp file and rename, so every sync produces a new
inode. `FileTailSource._read_one` keys its resume offset on `(st_dev, st_ino)`
(`tail.py:103`); on an inode change it resets the offset to 0 and re-reads the whole
file. The live tier accumulates (`self.buckets.add(...)`, `reducers/live.py:80`), so
those records are counted a second time. The reset is counted in `stats["resets"]` and so
is visible, but the inflated totals are not labelled. Separately, `TimeBuckets.add()`
anchors on wall-clock `time.time()` and discards anything older than the window
(`buckets.py:56-65`), so an archive captured last month renders an empty live tier.

**The code still reaches into a repository that is not here.** This tree was extracted
from a larger measurement repo. `normalize.py` inserts `REPO/analyze` on `sys.path` and
imports `agent_lib`, `purpose_lib` and `deep_traj_lib` (`normalize.py:99-135`, the `sys.path` insert at line 108). None of
them exist here, so the import always fails and the fallbacks are the live code path.
That is materially degrading: `effective_comm` becomes `lambda comm, args: comm` and
`SHELL_COMMS` becomes empty, so `_eff` — the tool identity consumed by six reducers — is
always the raw `comm`. `bash -c "git status"` is attributed to `bash`, never `git`, and
`_shell_resolved` is permanently `False`. The purpose taxonomy reports its provenance as
`purpose_src`; `effective_comm` has no equivalent, so this degradation does not appear
anywhere on the page.

## Goals

1. The dashboard is correct and honest against three kinds of input: a live collector
   directory, a periodically re-synced replica, and a frozen archive.
2. This repository contains the dashboard and the collector and nothing that refers to
   the repository it was extracted from.

## Non-goals

- Moving bytes. The copy step is the operator's `rsync`; this repo documents the recipe
  and does not implement, schedule or supervise it.
- `PushSource`. `tail.py`'s docstring anticipates an HTTP/UDP sink. Not built here; the
  `IngestSource` seam that would host it is preserved.
- New classification logic. Shell resolution is absent and will stay absent; this work
  makes that fact visible rather than inventing a replacement.

## Constraints

These are the repository's existing integrity rules. Each one binds a decision below.

- **Null is not zero, and an absence must be visible.** A dropped record, an excluded
  record and a record that was never written are three different facts and must read as
  three different facts.
- **Nothing is fabricated, and nothing measured is asserted without its basis.** A number
  on the page whose evidence is not in this repository does not belong on the page.
- **Scope is stated.** This now applies to the dashboard's description of itself: if the
  mode is a guess, the page says it is a guess.
- **The wire value `collector = "ebpf_marthen_new"` is not renamed.** It is stamped into
  the JSONL envelope; renaming it would split feeds captured before the change from those
  captured after. Path references to the old name go; the wire value stays.

---

# Part 1 — Three ingest modes

`IngestSource` remains the seam. Each mode gets the ingest strategy that is correct for
it; the reducers are unchanged and never learn which mode fed them.

| mode | ingest | `stale` means | banner |
|---|---|---|---|
| `live` | inode-keyed tail, as today | record lag > `max_age_s` | `live` |
| `replica` | tail + content-verified resume | **sync** lag > `sync_max_age_s` | `replica (inferred), synced 4m ago` |
| `archive` | one full pass, then ingest stops; `stat` sweep continues (1.4) | never; `status: "archive"` | `archive, as of 2026-08-14 09:03` |

## 1.1 Mode selection

Mode is inferred, and `--mode live|replica|archive` overrides the inference. `--mode`
follows the existing CLI convention in `__main__.py`: it defaults to `None` so an unset
flag never outranks the config file.

Inference, evaluated during `feeds.resolve()`:

- **replica** — since the last poll, at least one path we already held state for appeared
  with a different `(st_dev, st_ino)`, or several files' mtimes advanced together in one
  poll after a quiet interval.
- **archive** — no file mtime has advanced across the observed poll history, *and* the
  newest mtime is older than `archive_after_s`, defaulting to `6 x max_age_s` (30 minutes
  for the eBPF tier).
- **live** — neither of the above.

**The two thresholds must not collide, and the collision must resolve toward warning.**
Calling a feed an archive suppresses staleness entirely, so misreading a dead live feed
or a dead sync as an archive would hide a real outage — that is the expensive direction of
error. `archive_after_s` is therefore deliberately well above `sync_max_age_s`
(`2 x max_age_s`, section 1.3): a feed that goes quiet first reads as a **stale** live
feed or replica, and only much later as an archive. When the evidence is ambiguous the
inference picks the mode that still warns.

At startup there is no poll history, so the first classification comes from on-disk mtimes
alone, and the "no mtime has advanced" test cannot yet be evaluated. Startup therefore
never infers `archive`: an operator serving a genuine archive passes `--mode archive`, and
an inferred `archive` only ever arises after the poll history shows quiescence. A revision
is logged and surfaced; the page shows the current claim.

`--mode` takes no path of its own. The feed roots come from `--ebpf-root` / `--node-root`,
config or the candidate list exactly as they do today, so serving an archive is
`rc-dashboard serve --mode archive --ebpf-root /srv/snapshot-2026-08-14`.

The inference is itself an inference, and saying so is the point. The feed report carries
three fields, not one:

- `mode` — `live` | `replica` | `archive`
- `mode_source` — `inferred` | `flag` | `config`
- `mode_evidence` — a short human string, e.g. `3 of 4 files replaced with new inodes at
  10:42`

The banner renders `replica (inferred)` against `replica (--mode)`. A reader can tell a
measurement of the deployment from a declaration about it.

## 1.2 Content-verified resume

The collector's writer is strictly append-only: `DailyWriter.write()` appends
`json + '\n'` and never rewrites. Byte *i* of a given host-day file therefore never
changes, and a faithful replica of that file has a byte-identical prefix. That is what
makes resume-by-content sound, and it is the only property it relies on.

Two things about that writer have changed since this section was written, and neither
weakens the property. The default flush mode is now `poll`, so bytes land at most one
poll round after the `write()` rather than immediately — resume reads what is on disk,
so a record still sitting in the buffer is simply not yet there to resume from. And the
ebpf tier now writes **two** files per host-day, `<host>.exits.jsonl` and
`<host>.snapshot.jsonl`. Each is independently append-only, so the property holds per
file, which is the grain resume already works at.

In `FileTailSource._read_one`, the inode gate gains a fallback rather than surrendering to
a full re-read:

| condition | action | counter |
|---|---|---|
| `(dev, ino)` match | resume at stored offset | — (today's fast path) |
| inode differs, `st_size >= offset`, prefix hash matches | resume at stored offset | `resumed_after_replace` |
| inode differs, prefix hash does not match | offset 0, full re-read | `resets` (today's behaviour) |
| `st_size < offset` | offset 0, full re-read | `resets` (today's behaviour) |

The prefix hash is the sha256 of the 4096 bytes ending at the stored offset, or of bytes
`0..offset` when the offset is smaller than that. It is computed only when the inode has
changed, so the live path pays nothing.

Per-file state gains `tail_sha` and `tail_k`. `STATE_VERSION` goes 1 -> 2; the existing
version check in `_load_state` discards incompatible state, so no migration is needed.

A partially transferred file is safe by construction. The temp-file-and-rename pattern
never exposes one at the final path. A plain `scp` writing in place can, and then either
the prefix still matches — we read fewer complete lines and catch up on the next poll —
or the size has fallen below our offset and the existing reset path fires. Both outcomes
are already correct; neither is silent.

`resumed_after_replace` is reported alongside `resets` in the existing `stats` dict. A
replica that resumes cleanly and a replica that re-read everything are different facts.

## 1.3 Two clocks, never merged

`feeds.resolve_dated()` already computes both clocks — `newest_mtime` from the filesystem
and `newest_record_ts` from the newest record — and then collapses them: `status` is
derived from record lag alone (`feeds.py:181-183`). Under a five-minute `rsync` against
the eBPF tier's `max_age_s: 300`, a perfectly healthy replica reports `stale`.

The report gains `sync_lag_s` and `sync_lag` (now minus `newest_mtime`) beside the
existing `lag_s` and `lag` (now minus `newest_record_ts`), and the two stay independent:

- **live** — `status` as today: record lag against `max_age_s`.
- **replica** — `status` from sync lag against a new `sync_max_age_s`, defaulting to
  `2 x max_age_s`. Record lag is reported separately and never folded in. A healthy sync
  carrying a dead collector's records must read as *"synced 1m ago, collector last
  reported 3h ago"* — two facts, two flags.
- **archive** — `status: "archive"`, never stale, with `as_of` set to the newest record
  timestamp.

This is `reducers/live.py:37`'s rule — zero and "the collector did not report" are
different facts — applied to the copy.

## 1.4 Archive window anchoring

`TimeBuckets.add()` takes a `now=None` parameter, matching `series()`, `evict()` and
`last_complete_bin()`, which already accept one. In archive mode the anchor is the
archive's newest record timestamp rather than wall clock, so the rolling window is the
last 24 hours *of the archive*. The page states `as of <ts>` and does not imply the window
ends now.

**Archive mode stops ingesting, not watching.** An inferred archive is a claim about
quiescence, and a mode that stopped polling entirely could never observe that claim
becoming false — data could resume and the page would go on presenting a frozen archive.
So archive mode halts reading, parsing and rebuilding, but keeps the cheap `stat` sweep
`feeds.resolve()` already performs. If any file's mtime advances, the feed leaves archive
mode, normal ingest resumes, and the transition is logged and surfaced like any other.
`--mode archive` is a declaration rather than a claim, and is not revised this way.

## 1.5 API and UI surface

`feeds.panel_meta()` is the shared carrier already merged into every panel payload
(`aggregator.py:200-214`), so `mode`, `mode_source`, `mode_evidence`, `sync_lag_s` and
`as_of` ride along without touching any reducer. `/api/feeds` exposes the full report as
it does today.

The SPA renders one banner, driven entirely by those fields. Its three states are the
three rows of the table in Part 1. `FeedsTab` gains the sync clock beside the record
clock.

---

# Part 2 — Severing the old repository

## 2.1 `normalize.py`

Delete `sys.path.insert(0, os.path.join(REPO, "analyze"))` and the `agent_lib` / `purpose_lib` /
`deep_traj_lib` probing in `_load_helpers()`. The in-repo implementations — `_fallback_actor3`,
`_BUILTIN_PURPOSE`, the `effective_comm` identity, the `_AUTONOMOUS` / `_APPROVAL_ONLY` /
`_BYPASS_BOTH` / `_SANDBOX_ON` patterns — stop being fallbacks and become the
implementation, named accordingly.

Add `eff_src` beside the existing `purpose_src`, carried in the same payloads. Both report
`rc_dashboard.builtin`. The page states that shell resolution is not performed, so
`_eff == comm` is a declared property rather than an invisible degradation.

`_fallback_actor3`'s comment — *must NOT degrade to the binary `actor`* — is the
three-actor-class rule and survives the rename verbatim.

## 2.2 `config.py`

Remove `RC_MEASUREMENT_ROOT` from all three candidate builders, and remove the
`REPO/log/ebpfm`, `REPO/log/ebpfm/logs`, `REPO/log/ebpfm_node` and `REPO/log/login`
candidates. They describe a `log/ebpfm -> ../data/ebpfm/logs` symlink layout that does not
exist here.

`EBPFM_LOG_ROOT`, `EBPFM_OUTPUT_DIR`, `EBPFM_NODE_LOG_ROOT` and `EBPFM_NODE_OUTPUT_DIR`
stay: they are the collector's own environment variables and part of the contract between
the two halves. `/var/log/ebpfm`, `/var/log/ebpfm-login` and `~/.ebpfm/*` stay.

The `REPO` constant becomes unused and is removed. `HERE` stays.

## 2.3 `dashboard.config.example.json`

Replace the `/n/netscratch/juncheng_lab/juncheng/rc_measurement/...` paths with
placeholders. They are one site's absolute paths in a file that is meant to be a legal,
copyable example.

## 2.4 Citations to the old repository

Every reference to `collect/`, `analyze/`, `findings/` and `eBPF_marthen_new/` is removed.
Three cases:

**Re-anchored.** Where the cited file is vendored in this repository, the reference points
at the local copy: `collect/common/snapshot.py` -> `collector/node_snapshot.py`
(`reducers/resources.py:159`), `collect/common/agent_classify.py` ->
`collector/lib/agent_classify.py`. The old path disappears and the reference becomes live.

**Deleted, nothing lost.** The citation is decoration on a rule stated in full beside it.
`reducers/trajectories.py:226` spells out the sessionization rule
(`session_key='apid:<agent_pid>'`, then `sess`, then `tty`, then `usr`) and then cites
`analyze/extract_trajectories.py:278-294`. The citation goes; the rule stands. Likewise
the docstring citations in `feeds.py`, `sacct.py`, `reducers/tools.py` and
`reducers/trajectories.py`.

**Deleted with the claim.** Two places assert something measured whose evidence is not in
this repository. The evidence is not here, so the claim is not made here.

- `reducers/tools.py:123` — `cpu_basis` becomes `"self cpu_s, each process once;
  child_cpu_s NEVER added"`. The `1.83x` inflation figure and its `analyze/common.py:364`
  citation both go. This is a user-visible API string; the page loses a finding, and keeps
  only what this repository can support.
- `reducers/sandbox.py:19` — the sentence attributing the D-state-on-dead-automount
  behaviour to `findings/login.md` is removed. The surrounding distinction that carries the
  logic — `ancestry=bwrap` with `sandbox=unsandboxed` is the capability probe,
  `ancestry=bwrap` with `sandbox=sandboxed` is real confinement — is a property of the data
  and stays.

## 2.5 The vendoring sync subsystem

`ebpfm.sh check` compares each vendored module against a `collect/...` or `analyze/...`
path in a repository that is never alongside it here, so `_check_vendored` can only ever
print `standalone (no repo alongside; nothing to compare)`.

- `_check_vendored` and its two call sites (`ebpfm.sh:253-254`, `ebpfm.sh:348-349`) are
  removed, along with the `-- vendored libraries` heading.
- `.vendored.sha256` is deleted. `ebpfm.sh bundle` base64-includes it (`ebpfm.sh:886`) and
  must drop it from that list.
- `VENDORED.md`'s table loses its "Copied from" and "Upstream sha256" columns and becomes
  a list of the five modules and their local changes. The sections explaining the upstream
  sync rule and how to re-vendor go with it. Why the modules are copies rather than
  symlinks stays: that reason — the folder must run on a node with no repo — is still true
  and still governs.

The collector is otherwise untouched.

## 2.6 README

`## A note on paths` exists to explain references that will no longer exist, and is
removed. The paragraph recording that `collector = "ebpf_marthen_new"` survives as a wire
value is kept and moved, because that fact outlives the cleanup and is the one place the
old name still legitimately appears.

`## Requirements` and the architecture diagram gain nothing. The `## Quick start` "Serve"
section gains the replica recipe (Part 4).

---

# Part 3 — The declared schema contract

The two halves share a path convention (the `EBPFM_*` variables of section 2.2) and one
data contract: the on-disk JSONL schema. The path convention is checked every time a feed
resolves. The data contract is checked nowhere, and it is currently mis-stated — found
while writing this spec.

The collector emits `SCHEMA_VERSION = 6` (`collector/ebpf_trace.py:98`) — version 6 folded
the per-connection `tcp` and `accept` records into a `conns` array. The dashboard declares
its contract as 5 in three places: `rc_dashboard/__init__.py:1`, `normalize.py:3` and the
root README. Its guards are `sv >= 5`, so v6 records flow through the v5 path unremarked.

**No panel is broken by this.** No reducer subscribes to `tcp` or `accept` — every
`EVENTS` tuple in `reducers/` lists only `exit`, `truncated`, `residency`,
`residency_totals` and `submit` — and the dashboard contains no reference to `conns`. The
per-connection records were never consumed, so folding them changed nothing downstream.

It is therefore a documentation defect today rather than a data defect. The defect worth
fixing is that **nothing would have told us either way**. Two halves coupled by a schema,
with one side's declared contract two versions stale and no check between them, is the
coupling this work exists to address.

- The accepted range is stated as 5–6 in `__init__.py`, `normalize.py` and the root README.
  **Two of the three were done by hand during spec review**: `__init__.py:1` and
  `normalize.py:1-23` now declare 6 and document what v6 changed, including that a capture
  mixing 5 and 6 carries connection data in two shapes. Only the root README's
  `SCHEMA_VERSION = 5` remains.
- `Normalizer` already tallies `schema_versions` in its stats. That tally is promoted from
  an internal counter to a reported fact: a record whose version falls outside the accepted
  range is counted and named in the feed report, exactly as `excluded_pre_v4` already is.
  An unrecognised schema version becomes a visible state instead of a silent pass-through.
- A test asserts that a record at an unknown version is counted and surfaced — neither
  dropped silently nor silently accepted.

This is the smallest change that makes the contract self-checking, and it is the natural
home for a future v7: the dashboard will say it saw one.

**Scope note.** This part was found during spec review and is beyond what was agreed in
chat. Cut it if this work should stay to transport and cleanup; nothing else in the spec
depends on it.

---

# Part 4 — Tests and documentation

## 4.1 Tests

`rc_dashboard` has no Python tests. This work establishes the suite, using `unittest` to
match `collector/tests/`, run as `python3 -m unittest discover -s tests -q` from
`dashboard/`.

Written before the code they cover:

| test | asserts |
|---|---|
| resume across inode change | identical prefix, new inode -> no record is yielded twice |
| **double-count regression** | ingest a file, replace it with a longer copy at a new inode, ingest again -> bucket totals equal a single ingest of the final file |
| prefix mismatch | different content at the same offset -> full re-read, `resets` incremented |
| short replica | `st_size < offset` -> full re-read, `resets` incremented |
| mode inference | quiescent / batch-replaced / appending fixtures -> `archive` / `replica` / `live`, with `mode_source == "inferred"` |
| `--mode` override | flag beats inference, `mode_source == "flag"` |
| healthy replica is not stale | 5-minute sync, `max_age_s: 300` -> `status == "ok"`, sync and record lags reported separately |
| dead sync, fresh records | sync lag beyond threshold -> stale, record lag still reported independently |
| archive anchoring | month-old archive -> live tier is populated, window anchored on newest record, `as_of` set |
| archive exits on resume | a file's mtime advances under an inferred archive -> mode leaves `archive`, ingest resumes |
| `eff_src` present | payload declares `rc_dashboard.builtin` |
| unknown schema version | a record outside the accepted 5–6 range is counted and named in the report, neither dropped nor silently accepted |
| no old-repo references | see below |

The last one is an executable form of Goal 2, in the spirit of `npm run check:no-random`.
It greps `dashboard/rc_dashboard/`, `dashboard/dashboard.config.example.json`,
`dashboard/pyproject.toml` and `collector/` for `collect/`, `analyze/`, `findings/`,
`eBPF_marthen_new` and `RC_MEASUREMENT_ROOT`, and fails on any hit. Exactly one exemption
is allowed and is asserted positively rather than merely skipped: the wire value
`COLLECTOR = 'ebpf_marthen_new'` at `collector/ebpf_trace.py:97`, which
`collector/tests/test_ebpfm.py:732` already pins. An exemption that is itself tested
cannot quietly widen.

## 4.2 Documentation

`dashboard/README.md` gains a section on serving a moved feed: the three modes, what each
one claims, and the recommended recipe.

```bash
rsync -a --inplace --append-verify login0{1,2,3}:/var/log/ebpfm/ /srv/feed/
```

`--inplace --append-verify` keeps inodes stable and makes the resume in 1.2 degenerate to
an ordinary append. It is documented because it is cheaper, **not** because anything
depends on it: 1.2 is correct under plain `rsync -a`, under `scp`, and under a hand-copied
directory. An operator who gets the flags wrong loses efficiency, never correctness.

---

# Risks

**The inferred mode can be wrong.** A collector that writes in slow bursts can look like a
replica; a replica synced continuously can look live. The mitigation is not a better
heuristic, it is labelling: `mode_source` and `mode_evidence` travel with `mode`
everywhere it is shown, and `--mode` is always available. A wrong guess that says it is a
guess is survivable; a wrong guess presented as fact is not.

**The prefix hash assumes append-only.** If the collector ever rewrites a closed day-file,
the 4 KiB window could match while later bytes differ, and records would be skipped
silently — the one failure mode this design must not have. `DailyWriter` appends and
nothing else, and section 2.5 changes only `ebpfm.sh` and the vendoring manifest — never
`DailyWriter` or any other write path.

`DailyWriter` has since been changed, which is the check this note exists to force: the
day is now split into an `exits` and a `snapshot` file and the default flush mode is
`poll`. The assumption survives it. Both files are append-only, and the split only
multiplies the number of files resume tracks — which it keys per path already, so the
grain is unchanged. It is recorded here because it is the load-bearing one: any future
change to how the collector writes must be checked against 1.2, as that one was.

**Dropping the `1.83x` figure removes a real finding from the page.** It is the correct
call under the rule that nothing is asserted without its basis, and it is reversible — if
the supporting analysis is ever vendored here, the claim can return with a live citation.
