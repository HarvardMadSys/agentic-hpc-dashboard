# fleet: the collector's files, carried to the dashboard's host

The collector writes each login node's day-files on that node's own disk. The programs here
bring them to the one host that runs the dashboard, as they are written, so the dashboard
reads every node at once. [`hpc/deploy`](../deploy/README.md) is the kit that sets
them up and keeps them running; this page says what each program is, for running or
changing one outside the kit.

| file | runs on | what it does |
|---|---|---|
| [`fleet_stream.py`](fleet_stream.py) | each node (`send`) and the dashboard's host (`receive`, `status`) | the stream: one ssh connection per node carries its files, byte for byte, into a private copy the dashboard reads |
| [`seed_mirror.py`](seed_mirror.py) | the dashboard's host, once, before the receiver first starts | builds the sealed days of that copy from `.gz` files pulled another way, so the stream fetches only what is missing |
| [`pull-gz.sh`](pull-gz.sh) | a machine that logs in to the nodes with an ordinary key | pulls every node's sealed days with rsync, in the layout `seed_mirror.py` reads |

All three use the Python 3.6 standard library or plain shell, so a node's `/usr/bin/python3`
runs the sender with nothing installed; CI compiles both Python files on EL8's 3.6. The
module docstrings are the reference: the wire format, every guard and every limit.
`fleet_stream.py` is not executable in git; run it with `python3`.

## `fleet_stream.py`

```bash
python3 fleet_stream.py send [--root /scratch/ebpfm] [--poll 1] [--ping 30] [--trailer-poll 30]
python3 fleet_stream.py receive --config stream.json
python3 fleet_stream.py status --base <base>
```

- **`send`** runs on a node, started by a forced-command ssh key (the kit's step 3). It reads
  the receiver's one-line JSON hello on stdin and answers with frames on stdout: the bytes the
  collector appends (zlib), the sealed days the receiver lacks as the collector's own `.gz`,
  and each sealed day's gzip trailer to check them by; a ping after `--ping` seconds of
  silence. Under `--root` it follows no link, and sends only `.jsonl` day-files and their
  `.gz`. It exits 0, or 1 right after an `error` frame.
- **`receive`** runs on the dashboard's host and keeps one `send` running per node, writing
  `<base>/mirror/<host>/<day>/`, with `staging/` for the `.gz` days being fetched and `state/`
  for its status (`status.json` every 10 s, `traffic.jsonl` every 60 s, each host's
  `<host>.verified.json`). Its config is a JSON file:

  | key | default | |
  |---|---|---|
  | `base` | (required) | the folder it writes, its copy and its state |
  | `days` | (required) | the day folders it keeps per host, counted back from the node's own day |
  | `hosts` | (required) | each host's name, mapped to the command that runs its sender (an ssh line, or any argv that speaks the protocol) |
  | `private` | `true` | the base and its checked parents must be this account's 700 folders |
  | `base_parents` | 0 | how many of the base's parents are checked too |
  | `trip_marker` | | a file it writes when it trips or refuses to start, besides `state/TRIPPED` (the kit puts it beside the base) |
  | `keep_gz` | `false` | sealed days stay `.gz` in the copy, about 19 times less disk |
  | `free_min_gb`, `free_min_fraction` | 300, 0.10 | every link pauses below the larger of the two |
  | `stall_s` | 300 | a link whose sender sends nothing, not even its pings, for that long is closed and reconnects |
  | `backoff` | `[5, 300]` | the wait before a link reconnects, from first to longest, in seconds |
  | `dashboard_unit` | `rc-dashboard.scope` | the systemd user unit it stops when it trips |

  It **trips** when the base, or a checked parent, stops being the folder it opened at start
  (renamed, replaced, or with `private`, opened up): it stops every link, writes the marker,
  stops the dashboard, and exits 3, which the kit's `keep-running.sh` does not restart. It
  refuses to start on a base that is not safe.
- **`status`** prints each host's state and today's bytes from `<base>/state/status.json`,
  and exits 1 when there is no status yet.

## `seed_mirror.py`

```bash
python3 hpc/fleet/seed_mirror.py PULL BASE [--hosts HOST ...] [--inflate] [--free-min-gb GB]
```

Copies each `PULL/<host>/<day>/<name>.jsonl.gz` into `BASE/mirror/<host>/<day>/`, checking
every member's CRC and size as the receiver does, and lists it in
`BASE/state/<host>.verified.json`; a receiver started on `BASE` then tells each node those
days are verified. `--inflate` writes plain files instead, as a receiver without `keep_gz`
keeps them. Days go newest first, and seeding stops before free space falls under the
receiver's floor (give `--free-min-gb` the config's `free_min_gb` when it sets one). It
refuses a `BASE` whose status a receiver wrote in the last 5 minutes, so stop the receiver
first, and makes `BASE` this account's 700 folder if it is missing. It can be run again
after another pull. The last line says how many `days` the receiver's config needs to keep
what was seeded. Exit 0; 1 if a day was left for the stream to fetch (a `.gz` that did not
inflate, or no room); 2 if it refused.

## `pull-gz.sh`

```bash
SOURCE_USER=<account> SOURCE_NODES="<node> ..." hpc/fleet/pull-gz.sh PULL
(set -a; . hpc/deploy/site.env; hpc/fleet/pull-gz.sh PULL)     # the kit's settings
```

rsyncs only the sealed days, `*.jsonl.gz` under `SOURCE_ROOT` (default `/scratch/ebpfm`), from
each node into `PULL/<node>/<day>/`; the live day's plain files are left for the stream. It
also reads `SOURCE_DOMAIN` (each node is `<node>.<SOURCE_DOMAIN>`) and `SSH_KEY` (default
`~/.ssh/id_ed25519`), and needs rsync on both ends. The files name real users and show their
commands, so `PULL` is made 700 and its files 600. Run again, it fetches only what is new.
Exit 0; 1 if a node failed (the others are still pulled); 2 for a usage error.

## Tests

```bash
python3 -m unittest discover -s hpc/fleet/tests -q
```

They run the sender and the receiver against a fake collector (`tests/fakecollector.py`),
with no ssh: a link's command is the sender itself. They cover the wire format, resuming
after a cut, sealed days and their trailers, the free-space pause, the trip, a hostile
sender's frames, and `seed_mirror.py`.
