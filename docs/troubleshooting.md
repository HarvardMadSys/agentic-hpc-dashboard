# Troubleshooting

Status: current. Each entry starts from what you see, names the likely causes in the order to
check them, and gives the command that tells them apart. `rc-dashboard` is
`uv run --frozen rc-dashboard` from `dashboard/` (or the installed command, from the wheel or the
image), and `ebpfm` is `./collector/ebpfm.sh` from a checkout (the RPM installs `ebpfm`).

- [The page says "No data yet"](#the-page-says-no-data-yet)
- [A login node stopped reporting](#a-login-node-stopped-reporting)
- [`ebpfm start` said it started, but nothing is written](#ebpfm-start-said-it-started-but-nothing-is-written)
- [Signing in fails](#signing-in-fails)
- [The service will not start](#the-service-will-not-start)
- [The page is blank, or says "frontend not built"](#the-page-is-blank-or-says-frontend-not-built)
- [Live does not update](#live-does-not-update)
- [Times look wrong](#times-look-wrong)
- [One login node shows up as two](#one-login-node-shows-up-as-two)
- [A window says "reading history" for a long time](#a-window-says-reading-history-for-a-long-time)
- [Ask LLM is missing](#ask-llm-is-missing)

## The page says "No data yet"

Every tab gives way to that screen while no feed holds a record. It lists every folder the
service looked in and what it found in each: `no such folder`, `there, but holds no day-files`,
or `permission denied`.

1. **Is the collector running on the login nodes, and where does it write?** On a node:
   `sudo ebpfm status`. It names each tier's output folder and the age of its newest record, and
   exits 0 only when data is arriving (1 degraded or stale, 3 not running). Not installed:
   `sudo ./collector/ebpfm.sh install` ([collector/OPERATIONS.md](../collector/OPERATIONS.md)).
2. **Does the dashboard look there?** `rc-dashboard check-feeds` prints one line per folder it
   tried. The defaults include `/scratch/ebpfm`, `/var/log/ebpfm` and the folders named in
   `/etc/default/ebpfm`; anywhere else, name the folders with `rc-dashboard init` or
   `RC_DASH_EBPF_ROOTS` and `RC_DASH_NODE_ROOTS` ([dashboard/README.md](../dashboard/README.md#the-two-feeds)).
3. **`permission denied`?** The service's account must be in the collector's group
   (`EBPFM_GROUP`); the collector creates its folders `0750` and its files `0640`.
4. **A copy on another host** ([deploy/fleet](../deploy/fleet/README.md),
   [hpc/deploy](../hpc/deploy/README.md))? Then the folders are the copy's, and the copy may be
   what failed: see the next entry.

## A login node stopped reporting

The Feeds tab lists each collector with a verdict (`ok`, `degraded`, `stopped`, `silent`) and,
where the files are copied, the copy's own state. From a shell, `rc-dashboard status` says the
same for every host and exits 0 (ok), 1 (warn) or 2 (crit), so a timer or a monitoring system can
run it; `GET /api/status` and `GET /metrics` serve it to a scraper.

- **`copy FAILED`**: the node is fine; the copy is not. For an rsync fleet, `deploy/fleet/pull.py
  status` names the node and the error (a key that no longer works, or pulling paused below the
  free-space floor); for the ssh stream, the hourly check in [hpc/deploy](../hpc/deploy/README.md).
- **`silent` with no copy in between**: on the node, `sudo ebpfm status` (exit 3: not running).
  Its log is named there; under systemd, `journalctl -u ebpfm -u ebpfm-node`.
- **`degraded`**: running, but losing events or missing a probe: the Feeds tab's row says which,
  and what it cannot see because of it.

## `ebpfm start` said it started, but nothing is written

`start` now waits until a tier has written its first record and fails, with the log's tail, when
it does not. Older checkouts reported a tracer that had died at once as started. Check:

- `ebpfm status`: exit 0 healthy, 1 degraded or stale, 3 not running, 4 unknown.
- `ebpfm check`: one line per precondition; a `FAIL` names its fix (BCC missing, tracefs not
  mounted, kernel headers absent).
- `ebpfm config`: every setting, its value and where it came from. A value the collector cannot
  use (`EBPFM_NODE_INTERVAL=five`) is refused, by name, before anything starts.

## Signing in fails

- **"Wrong password."**: the password is in the file `rc-dashboard new-password` wrote (by
  default `~/.config/rc-dashboard/password`, mode 600), not in the config, which holds only its
  hash. To make a new one: `rc-dashboard new-password --install <hash file> --force`; the service
  reads the hash file again on its own, and everyone signs in again.
- **The service exits at start with `server.auth.password_hash: …`**: the hash is malformed, most
  often quoted or cut by a shell (`$` expands inside double quotes). Use the hash file
  (`RC_DASH_PASSWORD_HASH_FILE`), which needs no quoting.

## The service will not start

It says why and exits; the reasons a newcomer meets most:

- `refusing to serve: there is no password`: make one (`rc-dashboard new-password --install …`,
  or `rc-dashboard init`), or serve open on a machine only you can reach (`--open`).
- `<key> is not a setting; the nearest is <key>`: a typo in the config's `server`, `anonymize`
  or `ask` section stops it; elsewhere it is a warning. `rc-dashboard print-config --all` shows
  every setting and where it came from, and `rc-dashboard print-config --reference --all` every
  setting there is.
- `config file <path>, named by --config, does not exist` (or by `RC_DASH_CONFIG`): a config
  file the command line or the environment names must be there.

## The page is blank, or says "frontend not built"

The service serves the page from `dashboard/web/dist`, which `npm run build` makes; an installed
package or the dashboard image carries it already. A 503 that names `dashboard/web/dist` while
the folder exists means the service resolved another path: a relative `static_dir` resolves
against the repository root, so run `rc-dashboard print-config` and look at
`server.static_dir`. A blank page after an upgrade is an old tab's code: reload it.

## Live does not update

The head's pill reads `refreshing every 15 s` when the live stream is unavailable. The service
accepts a websocket from its own origin only: behind a reverse proxy, pass the `Host` header
through; for Vite's dev server, `RC_DASH_DEV_ORIGINS` (`dashboard/web/README.md`).

## Times look wrong

Every time on the page is in the dashboard service's zone, named in the head (`EST`,
`UTC−05:00`). A capture copied from another cluster reads in the zone of the host serving it:
start the service with `TZ=<that cluster's zone>`.

## One login node shows up as two

Collectors older than the one that added `ebpfm version` named a host two ways where its
hostname is fully qualified: the eBPF tier's unit by its full name (`node01.cluster.example.org`)
and the node tier by its short name (`node01`); the dashboard keeps a host's name as written.
Upgrade the collector (an old one answers `ebpfm version` with "unknown command"): both tiers
use the short name from then on, and the host's history splits at the change.

## A window says "reading history" for a long time

A window the service has not built is read from the day-files on disk; the picker shows the
share done. Every start rebuilds the windows it keeps, so a restart costs this again: a password
change no longer needs one (the hash file is re-read), nor does adding a node where the feed
roots are a pattern (`…/mirror/ebpf/*`). Sizing and measured times:
[hpc/deploy/README.md](../hpc/deploy/README.md).

## Ask LLM is missing

It was removed: it ran an agent CLI on the dashboard's host over the window, a read-only
sandbox rather than a confined one, and sent what it read to a model's provider. A config
that still has an `ask` section, or an `RC_DASH_ASK*` variable, starts with one warning that
says so, and the setting is ignored.

A host that had it on keeps two things it left until you remove them: `<cache.dir>/ask/`,
whose `done.json` holds the suggestions marked done as the model wrote them (with real names
in them if `ask.unmasked_answers` was on), and the agent CLI's login for the account the
dashboard runs as (`~/.codex/` for Codex). Delete the folder; unless that account uses the CLI
for anything else, log it out (`codex logout`) and revoke its key with the provider, since the
agent's reach on this host is why Ask was removed.
