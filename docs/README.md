# Documents

The documents here are about more than one part of the tree. How to run and operate
each part is in that part's own README; the [root README](../README.md#documentation-map)
maps them all.

| document | status | covers |
|---|---|---|
| [`fleet.md`](fleet.md) | current | more than one node: picking a layout (root or not), deploying the collector, the copy and the dashboard, opening the page, watching it, and the copy's privacy |
| [`troubleshooting.md`](troubleshooting.md) | current | what you see, its likely causes in the order to check them, and the command that tells them apart: no data, a node stopped reporting, sign-in, a service that will not start, a blank page, times, one node as two |
| [`figures/`](figures/README.md) | current | every figure on the page: what it computes, from which records, by which read path, and at what cost |
| [`feed-parquet/`](feed-parquet/README.md) | current: the dashboard's history store | the eBPF tier's day-files as typed Parquet tables, one per record kind: the schema, the layout, and what converting and reading them cost |
| [`design/store-spike.md`](design/store-spike.md) | experiment: nothing in the service changed; its finding about the service is fixed | could DuckDB over the history store replace the cache layers? One panel family computed three ways, timed and diffed, with what it found and what to do next ([`dashboard/tools/store_spike.py`](../dashboard/tools/store_spike.py) runs it) |
| [`logo/`](logo/README.md) | current | the mark and the lockups, and the four copies of the glyph to keep in step |

Each document states its status at the top: an experiment is a record of a trial, not a
description of the code. A current document the code no longer matches is a bug in
the document.
