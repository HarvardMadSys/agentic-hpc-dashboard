# Feed schema 9: a proposal

**Status: proposal.** Nothing in the collector or the dashboard changes with this
document. [`prototype.py`](prototype.py) converts a schema-8 feed into the format below,
expands it back to prove nothing is lost, and measures it. Every number here comes from
that script, run over the dashboard's synthetic demo feed (4 hosts, 72 h, 64,901
records), unless it is marked as a real capture. **Before deciding, run it on a real
capture** (§7): the synthetic feed has the collector's shapes, not a login node's mix.
One thing it left open has since been built: a columnar copy of sealed days (§4.3, open
question 5) is the dashboard's history store, [docs/feed-parquet](../feed-parquet/README.md),
made for the schema-8 feed as it is.

## In short

The feed's one structural gap is that **no record says a process started**. So every
record about a process carries the whole identity block (30 keys), and readers rebuild
what one record could have stated:

- The dashboard reads `conn` and `netio` ticks only to recover a running agent's
  identity.
- It rebuilds process trees from child-first exits, guessing around pid reuse.
- It finds a session's approval verdict by locating the root's own record.

Schema 9 keeps JSONL, the day files, `ts_epoch` on every line, explicit nulls and every
measurement. It changes five things:

1. **A process key on every record.** `pk` is `<pid>.<start ticks since boot>`, and
   `ppk` is the parent's key, so tree edges are exact and pid reuse cannot merge two
   processes.
2. **Sessions and long-lived processes are defined once**, in a third small stream per
   host per day (`procs`). Its `session` and `proc` records are written before the first
   record that refers to them.
3. **Tick records refer, they don't repeat.** `residency`, `conn`, `netio` and `submit`
   carry `pk`/`sess` in place of the identity block. `exit` and `truncated` stay
   self-contained.
4. **Derived, constant and always-null fields go.** So does the second copy of
   per-endpoint network bytes.
5. **One unit per quantity, one envelope per tier.** Sizes are in bytes, times are
   epoch seconds, and the node tier uses the same envelope keys as the eBPF tier.

What it measures (synthetic, vs schema 8 as the collector writes it):

| | plain (the live file, what readers parse) | gzip (a sealed day) |
|---|---|---|
| whole feed | **64.3 %** | **88.0 %** |
| `snapshot` stream | 55.0 % | 77.8 % |
| `exits` stream | 80.4 % | 96.5 % |
| dashboard parse, per exit (orjson) | 3.96 µs vs 4.92 µs | |
| round trip, schema 9 back to schema 8 | **0 undeclared differences** in 64,901 records | |

Be careful with the gzip column. The exits stream dominates a real node (boslogin06 wrote
**1.49 GB** of exits on 2026-10-08, 66 MB gzipped, per `dashboard/rc_dashboard/gz.py:20`).
On that stream, schema 9 saves about a fifth of the plain bytes but almost nothing at
rest, because gzip already removes most repetition. **Schema 9 is mainly a fix to the
structure.** If the goal is disk size or analysis speed over many host-days, the better
tool is a columnar copy of sealed days (§4.3), built as a separate step.

---

## 1. What the feed looks like now

### 1.1 Where the bytes are

Each host writes two files per day (`<host>.exits.jsonl`, `<host>.snapshot.jsonl`). In a
24 h, 2-host synthetic day, the shares by event are:

| event | share of bytes | grain |
|---|---:|---|
| `exit` | 40.0 % | per process exit (unbounded: the fork rate) |
| `residency` | 28.3 % | per agent tree per tick |
| `residency_totals` | 14.2 % | per host per tick |
| `conn` | 6.0 % | per standing external socket per tick |
| `netio` | 5.7 % | per networked process per tick |
| node tier | 5.6 % | per host per 5 min |
| `submit`, `meta`, `stop` | 0.1 % | rare |

On a real node the exits stream is most of the volume, and on a build node it is
essentially all of it (`collector/README.md`).

Inside a record, by field category:

| | `exit` | `residency` | `conn` | `netio` |
|---|---:|---:|---:|---:|
| per-process identity | 31.5 % | 51.4 % | 38.7 % | 44.8 % |
| session-level identity | 9.9 % | 11.8 % | 13.5 % | 15.4 % |
| derivable or always null | 7.7 % | 5.8 % | 6.0 % | 6.8 % |
| envelope constants | 5.1 % | 5.9 % | 6.6 % | 7.5 % |
| measurements and the rest | 45.8 % | 25.0 % | 35.1 % | 25.3 % |
| *of all bytes, JSON key names* | *58.0 %* | *46.9 %* | *53.9 %* | *53.0 %* |

### 1.2 Findings

**F1. Identity rides on every record about a process.** `_base_identity`
(`collector/ebpf_trace.py:3813`) stamps 30 keys on every `exit`, `truncated`,
`residency`, `conn`, `netio` and `submit`. On tick records, identity is half to
two-thirds of the bytes. The collector's own README says as much (`collector/README.md:715`):
*"identity, and so `args`, rides on every record about a process, including each tick's
`residency`, `conn` and `netio`, so one long command line is repeated all day."*

**F2. Nothing says a process started, so readers reconstruct it.**
- `LiveReducer` reads `conn`/`netio` "for one thing: the identity block of a running
  agent root" (`dashboard/rc_dashboard/reducers/live.py:207`). `windows.py:90` and
  `reducers/sandbox.py:44` say the same.
- `ProcIndex` (`dashboard/rc_dashboard/procindex.py`) rebuilds parent links from records
  that arrive child-first. It refuses links across users and against start order,
  because pids are reused: madsys-gpu1 wrapped on 2026-10-09, and a recycled pid made
  one user's agent the parent of another user's server.
- `session_uuid` already digests `(boot_id, pid, start ticks)` to survive restarts and
  pid reuse (`ebpf_trace.py:1996`), but only for agent roots. Every other process is
  keyed by a reusable pid.

**F3. Session facts are copied onto every process.** In all 39 sessions of the 24 h
synthetic day,
`agent_type`, `agent_pid`, `tree_root_pid`, `session_key` and `session_uuid` never vary
within a session. `autonomous` is set only on the root: it is null on all 6,779 child
records. The dashboard's rule 4 (`normalize.py:60`) says approval is "a property of the
agent SESSION", yet the feed has no session record to hold it. `sandbox.py:44` explains
why it must read the root's own record: a first-record-wins rule made the answer depend
on exit order.

**F4. On an exit, some fields are derivable, constant per capture, or always null.**
- `ts` restates `ts_epoch` as a local, naive string.
- `session_key` is `"apid:" + agent_pid`.
- `cpu_pct` is `cpu_s / duration_s`.
- `cpu_scope` and `io.scope` both restate `meta.cpu_scope`.
- `samples` and `state_last` are always null (`normalize.py`, `ALWAYS_NULL`).
- `net_endpoints.n` is the length of its list plus `overflow.n`, the endpoints cut
  past the cap.

Together these are 7.7 % of an exit's bytes.

A `truncated` record, written for each live process at stop (`flush_live`), is built
differently. It has no `cpu_pct`, its `state_last` is the process's last `/proc` state,
and its counters come from `/proc` (`io.scope` is `proc`). So a field can be dropped only
where its value matches what the rule says, not just because of its name.

**F5. Network bytes are written up to three times on an exit.** They appear in
`net_tx_bytes`/`net_rx_bytes`, in `conns.peers[]` (addr, port, provider, bytes) and in
`net_endpoints.endpoints[]` (addr, port, provider, bytes). All 83 of 83 synthetic
`peers` equal an endpoint row. The dashboard reads only `net_endpoints` (`IoReducer`, for
the network endpoints panel); nothing reads `conns` or `peers`.

**F6. Units and times are inconsistent.**
- `peak_rss_mb` divides by 1,048,576 (MiB) (`ebpf_trace.py:4157`), while `io.rd_mb`
  divides by 1e6 (MB) (`ebpf_trace.py:4184`). Both say `_mb`.
- An exit's `start` is a local, naive, second-granular string with no epoch, so it cannot
  be placed against another host without `meta.tz`.
- The node tier writes `timestamp`/`hostname` and has no `event`, while the eBPF tier
  writes `ts`/`host`/`event`.

**F7. Envelope constants sit on every line.** `collector_pid` is on all records but is
read only from `meta`, `stop` and `residency_totals` (`dashboard/rc_dashboard/health.py:105`).

**F8. The writer pads its own JSON.** `DailyWriter` writes through `_JsonlSink`, which
writes `json.dumps(rec)` with the default `", "`/`": "` separators (`ebpf_trace.py:2302`). Compact separators alone are
**91.0 %** of the plain bytes, with no other change.

---

## 2. Schema 9

### 2.1 Three streams per host per day

| file | events | grain |
|---|---|---|
| `<date>/<host>.exits.jsonl` | `exit`, `truncated` | per process exit, as now. **Self-contained**: identity inline |
| `<date>/<host>.snapshot.jsonl` | `meta`, `stop`, `residency`, `residency_totals`, `conn`, `netio`, `submit` | as now, but per-process records carry `pk` (and `sess`) in place of identity |
| `<date>/<host>.procs.jsonl` | `session`, `proc` | **new**. One `session` per session, one `proc` per process a snapshot record names, per day. 305 records over 16 host-days here |

The dashboard already globs `<date>/*.jsonl`, so the third file is picked up as-is.
Reducers ignore events not in their `EVENTS`.

### 2.2 Envelope

Every line carries `event`, `ts_epoch`, `host` and `schema_version`. It drops `ts`, which
is rebuilt from `ts_epoch` and `meta.tz`. `collector_pid` stays only on the run records
that `health.py` reads (`meta`, `stop`, `residency_totals`).

### 2.3 Keys: `pk`, `ppk`, `sess`

- **`pk`** = `"<pid>.<start>"`. `start` is the process's start in clock ticks since boot:
  `task->start_time` in USER_HZ, the unit of `/proc/<pid>/stat` field 22. The key is
  unique per host per boot (`meta.boot_id` names the boot) and survives `exec`. It also
  stays readable: `grep '"pk":"205005\.'` finds a pid.
  - Where the collector gets it:
    - The exit probe already reads `task->start_time`.
    - The exec probe would read the same field: one more kernel read, no `/proc`.
    - Processes seeded at startup have it from `/proc/<pid>/stat`, which `session_uuid`
      already reads.
    - A process that forked and never exec'd is the remaining case, since
      `sched_process_fork` does not hand the probe the child's task. It costs one
      `/proc/<pid>/stat` read, and only when a record is about to name the process, as
      `cwd` is read today. Nothing is added to the per-event path (design principle 2).
  - The node tier can compute the same key from `/proc/<pid>/stat`. That would join a
    process across the two tiers for the first time.
- **`ppk`** is the `pk` of the parent that forked the process, which is the parent that
  `ppid` already holds since schema 8. It comes from that parent's entry in the
  collector's process table, which seeding at startup and fork tracking keep complete.
  This makes the tree exact, with no child-first walk and no refusing links on user or
  start order.
- **`sess`** is today's `session_uuid` unchanged, so session keys do not move across the
  8/9 boundary. It is non-null whenever a process is in an agent tree.

### 2.4 Definitions: `session` and `proc`

```json
{"event":"session","ts_epoch":1794732168.16,"host":"demo-login02","schema_version":9,
 "sess":"642bf1d3f9e5c153","agent_type":"claude_code",
 "root_pk":"205005.135928230","tree_root_pk":"205003.135923662",
 "autonomous":false,"approval_mode":null,"approval_src":null}
```

`proc` holds the per-process identity: the 24 schema-8 identity keys that are not
session facts, plus `pk`, `ppk`, `sess` and `start_epoch`.

**The rule:** a definition is written to `procs` before the first record that refers to
it in that host's files for that day. A `proc` is written again when the process's
identity changes, which on Linux means an `exec`. Each day's `procs` file is therefore
complete for that day: no reader needs yesterday's file. A long-lived process is defined
again on each day it appears, which keeps definitions bounded by sessions and
long-lived processes, not by the fork rate.

**Readers:** to resolve host H on day D, load all of H's `D/procs.jsonl` (it is small).
The collector flushes `procs` first in each round. A tail that reads files one at a time
can still see a reference up to one poll before its definition. A reader holds such a
record until the next poll, and counts any that never resolve (`unresolved`). It never
guesses; this follows the existing rule that loss is counted.

### 2.5 `exit` and `truncated`

These stay self-contained, so a reader of the exits stream alone has each process's
identity. Changes from schema 8:

| change | fields |
|---|---|
| added | `pk`, `ppk`, `sess`, `start_epoch` (replaces `start`, and is null wherever `start` was) |
| moved to `session` | `agent_type`, `agent_pid` (as `root_pk`), `tree_root_pid` (as `tree_root_pk`), `session_uuid` (as `sess`), `autonomous` |
| dropped, derivable | `ts`, `session_key`, `cpu_pct` (on an `exit`), `net_endpoints.n` |
| dropped where it is the capture's | `collector_pid`; `cpu_scope` and `io.scope` when they equal `meta`'s, kept otherwise (a `truncated` record's `/proc` counters keep `io.scope: proc`) |
| dropped when null | `samples`, `state_last` (a `truncated` record's `state_last` is kept) |
| renamed, because the unit changed | `peak_rss_mb` → `peak_rss_bytes`; `rss_mb` (a `truncated` record's) → `rss_bytes`; `io.{rd,wr,rchar,wchar}_mb` → `io.*_bytes` |
| merged | `conns.peers[]` → each TCP row of `net_endpoints.endpoints[]` that agrees on a peer's bytes gains its `conns` and `failed` |

`actor3` stays inline even though it can be derived from `actor` and the session's
`agent_type`. It is the class axis every filter reads, and keeping it means an exit is
classed correctly even when its session definition is missing. `approval_mode` and
`approval_src` stay per process, because they are read from each process's own argv
(`_approval_of`). The session record carries the root's values.

`conns` keeps its connection statistics, which only the TCP birth map knows: `n`,
`accepted`, `established`, `failed`, `conn_s_total`, the connect latencies,
`peers_total`, and `providers`. `providers` counts every connection by provider,
failed ones and those past the top-N `peers` included, which no endpoint row can rebuild.
It also keeps its `rx_bytes`/`tx_bytes`, which come from `tcp_info`, a
second measurement independent of the endpoint table. For the same reason a peer merges
into an endpoint row only when the row agrees on its bytes. A peer whose bytes differ, or
that matches no row, is kept whole in `conns.peers_unmatched`, not dropped.

A short-lived tool call, schema 9:

```json
{"event":"exit","ts_epoch":1794732168.163393,"host":"demo-login02","schema_version":9,
 "pk":"205008.135929015","ppk":"205007.135929014","sess":"642bf1d3f9e5c153",
 "actor":"agent","actor3":"agent","attribution":"ancestry","pid":205008,"ppid":205007,
 "uid":21004,"user":"emmy","tty":null,"command":"sed","args":"sed -n 1,120p hmc/action.py",
 "args_redacted":null,"argv_source":"kernel","args_len":27,"args_truncated":false,
 "cwd":"/home/emmy/lattice-qcd","sandbox":"unsandboxed","sandbox_src":"inherit",
 "sandbox_detail":null,"sandbox_ancestry":null,"approval_mode":null,"approval_src":null,
 "depth":2,"is_agent":false,"cgroup":"/user.slice/user-21004.slice/session-111.scope",
 "start_epoch":1794732168.147,"fork_only":false,"duration_s":0.0159,"duration_src":"exec",
 "cpu_s":0.003,"child_cpu_s":0.0,"peak_rss_bytes":1981809,"threads":1, "...": "...",
 "io":{"rd_bytes":987000,"wr_bytes":399000,"rchar_bytes":1187000,"wchar_bytes":463000},
 "conns":null,"net_endpoints":null}
```

### 2.6 Tick records refer

`residency`, `netio` and `submit` carry `pk` and `sess` in place of the identity block.
`conn` does the same for a tracked owner. For a socket whose owner was never tracked, it
keeps today's stub inline (`pk: null`, `pid`, `uid`, `user`, `command`), since there is
no process to define. `residency`'s `pk` is the tree root, as its identity block is
today.

```json
{"event":"netio","ts_epoch":1794732194.03,"host":"demo-login02","schema_version":9,
 "pk":"205005.135928230","sess":"642bf1d3f9e5c153","reason":"tick","interval_s":33.7,
 "endpoints":[{"proto":"tcp","addr":"160.79.104.10","port":443,"host":null,"inbound":false,
               "external":true,"provider":"anthropic","tx_bytes":21602,"rx_bytes":21920,
               "calls":21}],"unattributed":null}
```

The same record in schema 8 is 1,270 bytes. 849 of them are the agent's identity
block, repeated on every tick.

### 2.7 Units and times

- **Sizes are integer bytes, everywhere.** That covers `peak_rss_bytes`, `io.*_bytes`,
  `residency.rss_bytes` and `top_procs[].rss_bytes`. The collector holds bytes or pages
  natively, so writing bytes removes a division and a rounding, and leaves no MB/MiB
  question.
- **Durations stay in float seconds (`_s`).** Instants are epoch seconds: `ts_epoch`,
  `start_epoch`.
- A field whose unit changes gets a new name. That way a reader for an older schema
  cannot misread it silently.

### 2.8 The node tier

The node tier gets the same envelope: `event: "node"`, `host` (was `hostname`) and
`ts_epoch`, with `timestamp` dropped. It keeps its own `schema_version` series, which
`event` disambiguates. The node tier is about 5 % of the bytes and bounded at one record
per 5 min, so this change is for consistency, not size.

### 2.9 Invariants kept

- **Null is not zero.** Every key stays present with an explicit null; the key set per
  event is fixed, as `_stub_identity`'s docstring requires (`ebpf_trace.py:3862`).
- **Three actor classes.** `actor3` stays on every exit.
- **Nothing is fabricated.** A missing definition is counted, never inferred.
- **Self-describing lines.** `schema_version` and `host` are on every line, so a capture
  that spans 8 and 9 reads line by line.
- **Binary search by time.** `export.seek_to_epoch` binary-searches on `ts_epoch`, which
  every line still carries.

---

## 3. Measured

`prototype.py run` over the demo feed (4 hosts × 72 h). Schema 8 is shown as
`DailyWriter` writes it (default separators). Schema 9 is written with compact
separators.

| stream | records | v8 plain | v9 plain | v8 gzip | v9 gzip |
|---|---:|---:|---:|---:|---:|
| `exits` | 20,146 | 35.0 MB | 28.2 MB (80.4 %) | 2.2 MB | 2.2 MB (96.5 %) |
| `snapshot` | 44,755 | 63.2 MB | 34.7 MB (55.0 %) | 2.0 MB | 1.6 MB (77.8 %) |
| `procs` | 305 | | 181 KB | | 18 KB |
| **all** | | **98.2 MB** | **63.1 MB (64.3 %)** | **4.3 MB** | **3.7 MB (88.0 %)** |

The 24 h, 2-host run gives the same picture: 61.6 % plain and 87.8 % gzip.

Per exit record, best of 3 over 20,146 records (Python 3.13):

| | schema 8 | schema 9 |
|---|---:|---:|
| bytes | 1,739 | 1,399 |
| dashboard parse, `orjson.loads` | 4.92 µs | 3.96 µs |
| dashboard parse, `json.loads` | 13.17 µs | 12.01 µs |
| collector encode, `json.dumps` | 14.85 µs | 13.75 µs |

The encode figure is noisy. Across five runs schema 9 was faster in four, and slower in
one (17.3 vs 15.0 µs). Read it as "no worse", not as a saving.

**Round trip.** Every schema-9 record was expanded back into a schema-8 record, using
only the files just written, as a reader would. All 64,901 matched their originals field
by field, except for the differences declared below:

| declared difference | records | why |
|---|---:|---|
| `cpu_pct` | 277 | rebuilt from rounded `cpu_s`/`duration_s`, so the last digit moves |
| `conns.peers` | 83 | merged into endpoint rows; all 83 peers matched a row on addr, port and both byte counts (one that did not would be kept whole, and one in neither place fails the run) |
| `ts` | 1 | rebuilt from `ts_epoch`; the collector stamps them separately |

**Projection.** This assumes a real exit record is composed like the synthetic one. On
that assumption, boslogin06's 2026-10-08 exits file would go from 1.49 GB to about
**1.20 GB plain**, and from 66 MB to about **64 MB gzipped**. Running §7 on that file
would replace this guess with a measurement.

---

## 4. Alternatives considered

### 4.1 Omit null fields

Writing only non-null keys: plain 70.8 % on exits (vs 80.4 %), gzip 93.6 % (vs 96.5 %).
Over the whole feed this saves another 4.6 points plain and 1.7 points gzip. **Not
proposed.** The project decided that absent and null must not be confused
(`_stub_identity`), and this small gain does not justify reopening that decision.

### 4.2 Positional rows

One `columns` header line per file, then each record as a JSON array. This gives 37.1 %
plain and 78.9 % gzip on exits, the smallest JSON here. But turning a row back into a
dict takes as long as parsing a named schema-9 record (4.75 vs 3.96 µs). It also loses
`jq '.cwd'`, `grep '"user":"x"'`, and reading a line on its own. **Not proposed.**

### 4.3 Parquet for sealed days

The schema-9 exits as one Parquet file (zstd) come to **1.55 MB, 69 % of the gzipped
schema-8 stream**. Parquet's real advantage is reading only the columns a window build
needs, which this test does not measure. It does not fit the collector: EL8's Python 3.6
has no supported pyarrow, and a live file must stay appendable. It fits the dashboard
side. A command could write `<date>/<host>.exits.parquet` next to the sealed `.gz`,
from schema 9's flat records, for window builds and analysis scripts. That would be a
separate step after schema 9, judged on a real capture.

### 4.4 Binary row formats (msgpack, CBOR, protobuf)

Not measured. These need a dependency on EL8's Python 3.6. They lose `cat` and `jq` on a
login node during an incident, which is when the files are read by hand. And gzip already
takes most of what they would save at rest.

---

## 5. What changes, where

**Collector** (`collector/ebpf_trace.py`):
- Split `_base_identity` into the per-process part and the session facts.
- Compute `pk` from `task->start_time`: already read in the exit probe, one more field
  in the exec probe, and `/proc/<pid>/stat` for seeded and fork-only processes. Take
  `ppk` from the parent's table entry.
- Keep a per-day "defined" set, and write `session` and `proc` records to a third
  `DailyWriter` stream, flushed first.
- Trim `_envelope`. Write bytes. Merge `conns.peers` into endpoint rows. Use compact
  separators.
- Give `node_snapshot.py` the shared envelope.
- Bump `SCHEMA_VERSION` to 9.

**Dashboard**, in an order that keeps it working throughout:
1. **A resolver at the normalize seam.** Load each host-day's `procs` file, and expand
   schema-9 records into the schema-8 shape the reducers consume today.
   `prototype.py`'s `to_v8` is that resolver, and it is already proven lossless. Mixed
   8/9 captures work line by line on `schema_version`.
2. **The tail reads `procs` first** in each poll, holds forward references for one
   poll, and counts unresolved ones.
3. **`ProcIndex` keys on `(host, pk)` and links on `ppk`.** The pid-reuse refusals and
   the child-first walk go away.
4. **`LiveReducer` and `SandboxReducer` read `session` records** for agent type,
   launch flags and approval, and stop reading `conn` and `netio` for identity.
   `windows.EVENTS` can then drop those two events from the readers that only wanted
   identity.

The demo generator (`dashboard/rc_dashboard/demo/records.py`) follows the collector, as
it does today.

---

## 6. Open questions

1. **Drop `ts`?** Nothing in this repo needs it: `normalize.epoch_of` prefers
   `ts_epoch`. But the `analyze/` scripts this project grew from may still parse it.
   Keeping it costs 1.7 % of plain bytes and almost nothing gzipped.
2. **`sess` as today's digest, or as the root's `pk`?** The digest keeps session keys
   stable across the version change. The root's `pk` (with `host`) is shorter and
   readable, and makes `root_pk` redundant.
3. **Are `conns` bytes and endpoint bytes equal on real data?** They come from different
   probes (`tcp_info` vs send/recv). The prototype checks every `peers` entry against
   the endpoint rows. On a real capture, a mismatch shows up as `peers_unmatched`.
4. **`pk` from `start_time` vs `/proc` field 22.** The first is `CLOCK_MONOTONIC` and
   the second is boot time, so they differ only after a suspend, which login nodes do
   not do. Confirm on EL8's 4.18 (`real_start_time`) before the node tier relies on the
   join.
5. **The Parquet archive (§4.3):** whether it is wanted, and on which host it would run.

---

## 7. Reproducing

```bash
# a synthetic schema-8 feed (needs the dashboard importable: run from the repo)
python3 docs/feed-format-v9/prototype.py demo /tmp/feed --hours 72 --hosts 4

# convert, verify, measure (stdlib only; orjson, zstandard and pyarrow add the
# orjson timing, the zstd column and the Parquet size)
python3 docs/feed-format-v9/prototype.py run /tmp/feed/feed/ebpf /tmp/v9 --node /tmp/feed/feed/node
```

The numbers above come from that feed. The demo's nightly collector restarts write no
`truncated` record, and its exits' scopes always match its `meta`. Without those, a
run does not exercise the rules for either. `demo --truncated` adds both: each host-day
ends with a collector stop, written as `flush_live` writes one, and the first host's
capture is `leader`-scoped. Run it after any change to the prototype; it must still
report 0 undeclared differences:

```bash
python3 docs/feed-format-v9/prototype.py demo /tmp/feed-t --hours 30 --hosts 2 --truncated
python3 docs/feed-format-v9/prototype.py run /tmp/feed-t/feed/ebpf /tmp/v9-t --node /tmp/feed-t/feed/node
```

On a real capture, point `run` at the collector's root and give it the collector's time
zone, for example
`prototype.py run /var/log/ebpfm /tmp/v9 --node /var/log/ebpfm-login --tz America/New_York`.
Schema 8's `ts` and `start` are naive local strings, so they can only be rebuilt in the
zone they were written in. A wrong `--tz` moves no instant (`start_epoch` comes from
`ts_epoch - duration_s`), but the report then shows every `ts` and `start` as a declared
difference.

The prototype reads plain or `.gz` day files. The first pass keeps an index of every
process and session over the whole feed; on a multi-week, many-node capture, memory
grows with that index. The second pass holds one host-day at a time, and verifies from
the files it just wrote, read back from disk. The prototype exits 1 if any field differs
in a way not declared above, or if a record refers to a definition that its day's
`procs` file does not hold.

A schema-8 record has no start ticks, so the prototype rebuilds `pk`/`ppk` from
`ts_epoch - duration_s` and `ts_epoch - age_s`. That gets the keys' length right for
measuring; the keys themselves are never compared. A process with no start from any exit
or residency gets a guessed start, kept apart from the real lives so that it can never
take a real process's key. The report counts those (129 here), counts pid reuse from
real lives only, and counts hosts whose boot time had to be guessed.
