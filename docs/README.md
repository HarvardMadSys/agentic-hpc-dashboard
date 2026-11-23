# Documents

The documents here are about more than one part of the tree. How to run and operate
each part is in that part's own README; the [root README](../README.md#documentation-map)
maps them all.

| document | status | covers |
|---|---|---|
| [`troubleshooting.md`](troubleshooting.md) | current | what you see, its likely causes in the order to check them, and the command that tells them apart: no data, a node stopped reporting, sign-in, a service that will not start, a blank page, times, one node as two |
| [`figures/`](figures/README.md) | current | every figure on the page: what it computes, from which records, by which read path, and at what cost |
| [`feed-parquet/`](feed-parquet/README.md) | current: the dashboard's history store | the eBPF tier's day-files as typed Parquet tables, one per record kind: the schema, the layout, and what converting and reading them cost |
| [`feed-format-v9/`](feed-format-v9/README.md) | proposal: nothing in the collector or the dashboard implements it | a schema-9 feed, with a process key on every record and sessions defined once, and [`prototype.py`](feed-format-v9/prototype.py), which converts a schema-8 feed into it, proves the round trip and measures it |
| [`plans/`](plans/) | landed: kept as the record of how the work was done | implementation plans; [2026-11-17, EBR out, Parquet in](plans/2026-11-17-ebr-to-parquet.md) |
| [`logo/`](logo/README.md) | current | the mark and the lockups, and the four copies of the glyph to keep in step |

Each document states its status at the top: a proposal or a landed plan is a record of a
decision, not a description of the code. A current document the code no longer matches
is a bug in the document.
