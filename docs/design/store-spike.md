# Store spike: could one columnar store replace the cache layers?

*Status: an experiment, which changed nothing in the service. Its one finding about the service
itself is fixed (item 1 of [What to do with it](#what-to-do-with-it)).*

An experiment, not a feature: it changed nothing in the service. The question was whether the
dashboard's three cache layers (saved window builds, hours, payloads) could give way to one
query engine over the store it already keeps: the history store's Parquet replicas of the
sealed days and the newest day's plain JSONL. The test was one panel family, the Resources
reducer's payload (the `resources` key of `/api/panels`): the window's series by class, CPU,
runqueue wait, major faults, disk and network bytes, resident memory, D-state by tool, and the
per-class totals, distributions and worst processes.

[`dashboard/tools/store_spike.py`](../../dashboard/tools/store_spike.py) runs the whole
experiment (`uv run --frozen --with duckdb python tools/store_spike.py run --days 8 --hosts 3`,
the run below, from `dashboard/`;
DuckDB is a throwaway dependency, and `uv.lock` is left alone).

## Method

One demo feed, 8 days of 3 hosts (`demo --demo-write-only`), every eBPF day but the newest sealed
as `.gz` and converted by the history store, so the sealed days have Parquet replicas and the
newest day is plain JSONL, as on a deployment. Each window (60, 1440 and 10080 minutes, at the
service's bin widths) was computed three ways, each in a fresh process, cold and then warm:

| path | what |
|---|---|
| **sql** | DuckDB over the replicas and the newest day's JSONL, every rule restated in SQL: classes, the null gating and impossible waits, the bins, gaps kept as nulls, the own-I/O join with the hour chain's reach and ageing, the quantile sketch. Their constants and regular expressions are read from the service's modules, so the two cannot drift; rows a query returns are masked by `redact.py`, as on every way out of the service. |
| **service** | `rc-dashboard serve` under a pinned clock: an empty cache that builds only that window, then restarted over what it saved; and once more with all three windows in one cache, the default 24 h first, as a deployment holds them. |
| **reducer** | the service's own `ResourceReducer` alone, fed by its own window scan: what this one family costs in Python, without the ten other reducers a service build runs. |

Then every value of each payload was compared with the others'.

The feed: 198,418 eBPF records (195,685 sealed, 2,733 plain), 69,052 of them exits; 11.7 MB of
sealed `.gz` (273 MB inflated), 3.6 MB plain; a Parquet store of 12.7 MB, 8.2 MB of it in the two
tables read here. One machine: 4 CPUs, 15 GB, Linux 6.18, DuckDB 2.0.1, Python 3.13.

## Results

| window | service cold build | service warm | service RSS cold / warm (peak) | reducer alone cold / warm (peak RSS) | sql cold / warm, 4 threads | sql 1 thread cold | sql peak RSS | DuckDB peak buffer |
|---|---|---|---|---|---|---|---|---|
| 60 min | 0.40 s (scan) | 0.30 s (snapshot) | 513 / 650 MB (689 / 665) | 0.11 / 0.02 s (71 MB) | 0.35 / 0.35 s | 0.37 s | 197 MB | 72 MB |
| 1440 min | 9.50 s (hours) | 0.60 s | 698 / 530 MB (698 / 677) | 0.83 / 0.59 s (100 MB) | 0.46 / 0.46 s | 0.49 s | 226 MB | 105 MB |
| 10080 min | 54.1 s (hours) | 4.00 s | 356 / 375 MB (718 / 722) | 4.53 / 3.37 s (344 MB) | 1.08 / 1.06 s | 1.41 s | 457 MB | 270 MB |

The service's RSS is the whole process, every reducer and the live tier included; the reducer's
and the SQL's are this family alone. The service's cache on disk: 0.9 MB for the hour, 1.9 MB for
the day, 12.6 MB for the week (11.9 MB of it hours); the Parquet store, 12.7 MB, serves both.

**Correctness.** The SQL gives the service's numbers:

| window | sql vs the service | |
|---|---|---|
| 60 min | 2,823 equal, 1 tie | |
| 1440 min | 6,836 equal, 22 ties, 8 other | the 8: two rows of `stall_by_tool` (`node` and `server`) with the same sort key, in the other order |
| 10080 min | 7,720 equal, 58 ties | |

A tie is a value that differs only in its last rounded digit, the SQL's unrounded value at a
rounding tie: float sums taken in another order. The SQL matched the reducer alone the same way.

**A finding about the service itself.** Its own week window is not the same in every cache: built
alone, and built in one cache after the day and the hour windows, it differs in one bin of the
agent class, bin 148 of the week, about 20 hours back and so inside the day window built first:
read bytes 77.4 MB alone against 668.6 MB after the others (`rchar_mb`; `rd_mb`, `wchar_mb` and
`wr_mb` the same way). The SQL agrees with the window built alone. An hour's reduction should not
depend on which window built it first; it looks like the own-I/O chain of an hour reduced for the
day window, whose reach and ageing span the hours before it, is kept and reused by the week window.
That is a research-integrity question (a number that depends on the cache's history), worth its own
investigation before anything here is acted on.

**Fixed.** The cause was the hours layer (`hours.HourStore.ensure`), not the join itself. The day
window, built first, saved its 24 hours (its edge hour and the 23 whole ones after it) from a join
state begun afresh at its edge hour. The week's run, begun six days further back, reached them
holding what the children that exited before that edge had carried, for their parents, and reduced
only the first of the 24 again with it: the other 23 kept the day's. A parent that exited in them
kept its reaped children's bytes, which the week had already counted at the children's own exits,
so it counted them twice, in every panel that sums disk bytes. Each saved hour now says where its
join state began, a run reduces again every saved hour after it whose state began later, and a run
stopped part-way resumes where it stopped ([Hourly merge-reduce](../../dashboard/README.md#hourly-merge-reduce);
`BuildOrderTest` in `dashboard/tests/test_hours.py` builds the week both ways). Run again after the
fix (`run --days 8 --hosts 3`, a new feed of 193,433 eBPF records): the week built in one cache
after the day and the hour equals the week built alone in all 7,778 values, where a run before the
fix on a feed made the same way had 8 differ; the day (6,866) and the hour (3,674) do too, each warm
restart equals its cold build, and the SQL still gives the service's numbers (the week's 7,718
equal, 60 ties). The cost is the day's hours once more: built after the day, the week reduces 168
hours, as many as it does built alone, where it reduced 145 before the fix.

What is left: the saved hours are one chain, begun at the edge hour of the widest window built so
far, so the week (retention's) holds still and a narrower window moves once. Built first, the day
takes off only the children that exited after its own edge hour; from its next build after a week
it is the week's slice, and a parent whose children exited before the day's edge has them taken
off, their bytes then in no bin of the day (in `BuildOrderTest`, the `claude` reads 1005 MB in the
day built first and 5 MB in every build after the week). Beginning every chain at retention's edge,
as `panels.preprocess` does, would leave no window moving with what was built before it, at the
cost of reducing all of retention before the first window shows: seven times the day's hours, and
here 45.9 s against the day's 7.8 s.

## What it does not show

- **Scale.** The feed is about a thousandth of a busy site's: 8 login nodes write 35 to 47 GB of
  JSON a day ([memory and disk](../../hpc/deploy/README.md#memory-and-disk)). Nothing here says
  how either path grows to that.
- **The other families.** Resources is a window aggregate, the kind SQL is best at. Ten other
  reducers build the page; the session chains, cascades and phases carry state from one record to
  the next, which the SQL would have to restate as window functions or not at all.
- **The page's filters** (class, user, agent type) and the live tier's newest minutes as they
  arrive were not exercised: the SQL reads the files as they are when it runs.
- **The cost of a dependency**: DuckDB is a 66 MB native library once installed, one more thing
  every install carries and every Python the dashboard supports must have a wheel for.
- **The own-I/O join's grouping.** The service folds a child's figure into an entry of its user's
  whose earliest start is within `START_SLACK_S` of its own (`own_io._entry_for`), merges the two
  closest entries past `PER_PID`, and accepts or refuses each entry whole on its earliest start.
  The SQL applies the start cutoff child by child. Two children of one user that began within a
  second of each other around a parent's start less `START_SLACK_S` can come out differently, and
  so can a pid past `PER_PID` entries. The grouping depends on the order the children arrive, which
  no window query restates; a recursive query could. The feed here had no such case (the diffs
  above are exact), so the numbers stand, but a feed that has one would be misread.

## What to do with it

Nothing in production yet. The evidence says a query engine over the store the service already
keeps computes a window aggregate exactly, 20 to 50 times faster on a cold build of a long window,
in less memory than the service's whole process, with no cache to keep or invalidate. That is
enough to justify the next experiment and not enough to replace anything:

1. Find out why the service's week window depends on what its cache built before (above), and fix
   it; until then, any comparison is against a moving target. Done for the week: a wider window's
   run reduced again only the first of the hours a narrower window had saved, and now reduces them
   all (above), so the week holds still. A narrower window still moves once, when a wider one is
   first built, unless every chain begins at retention's edge (above): compare it after the week.
2. Run the spike on a captured feed at a site's scale (`--reuse` over a copy of real day-files),
   with the history store converted, for the long windows only.
3. If it holds, move the long-window aggregate families (Resources, the Trend tab's daily sums) to
   queries over the store, keep the chain reducers as they are, and drop the hours layer for those
   families: the part of the cache that costs the most to build and to keep consistent.
