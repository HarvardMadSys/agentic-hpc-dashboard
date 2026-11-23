"""The command line: `rc-dashboard <command> [flags]`, or `python -m rc_dashboard`.

    serve         the service (the default command)
    demo          the service over a synthetic feed it writes itself (rc_dashboard/demo)
    init          a first config, a password, and the commands to run next (init_cmd.py)
    check-feeds   where the collector's files were looked for, and what was found
    status        is the collection working: per host, for a monitor (status.py)
    print-config  the resolved settings, each with where it came from
    new-password  a password for the page, and its hash (auth.py)
    backup        the state that cannot be rebuilt, into a tar (backup.py)
    restore       ... and back
    rollup        older days into the Trend tab's table (rollup.py)
    version       the version and the commit it was built from (about.py)

`rc-dashboard <command> --help` says what each does, with examples. Every flag
that sets a setting defaults to None, so a flag not given never outranks the
config file (config.CLI_MAP).

What still works from before the commands had parsers of their own (`CLI`):

* a flag before the command or after it: `--config x.toml serve` is `serve
  --config x.toml` (the command word is found wherever it is and put first);
* no command at all is `serve`;
* `--check-feeds` and `--print-config` as flags of any command that reads the
  config (hidden), each doing what its command does;
* `--sacct-dir` and `--jobs-csv`, retired: taken, named in a warning, ignored.
"""
import argparse
import json
import os
import signal
import sys
import textwrap

from . import config as configmod
from . import feeds as feedmod

H = argparse.SUPPRESS
RAW = argparse.RawDescriptionHelpFormatter


class _Version(argparse.Action):
    """`--version`: the version line, worked out only when asked for."""

    def __init__(self, option_strings, dest=H, default=H, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0,
                         help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        from . import about
        sys.stdout.write(about.version_line() + "\n")
        parser.exit()


class CLI(argparse.ArgumentParser):
    """The top-level parser. Before parsing, the command word is found wherever
    it stands among the flags and put first, and `serve` stands in for none."""

    def parse_known_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(self.arrange(args), namespace)

    def _sub(self):
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                return a
        return None

    def arrange(self, args):
        sub = self._sub()
        if sub is None:
            return args
        takes = {}                      # every option of every command: does it take a value
        for p in sub.choices.values():
            for a in p._actions:
                for o in a.option_strings:
                    takes[o] = takes.get(o, False) or a.nargs != 0
        for a in self._actions:
            for o in a.option_strings:
                takes[o] = takes.get(o, False) or a.nargs != 0
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                break
            if a.startswith("-") and a != "-":
                if "=" in a or not self._takes(a, takes):
                    i += 1
                else:
                    i += 2
                continue
            if a in sub.choices:
                return [a] + args[:i] + args[i + 1:]
            return args                 # a word that is no command: argparse names it
        flags = args[:i]
        if flags and all(a in ("-h", "--help", "--version") for a in flags):
            return args                 # the top level's own help, or the version
        return ["serve"] + args

    @staticmethod
    def _takes(opt, takes):
        if opt in takes:
            return takes[opt]
        if opt.startswith("--"):        # an abbreviation, as argparse allows
            hits = [v for o, v in takes.items() if o.startswith(opt)]
            return any(hits)
        return False


def _settings(p, serve=True):
    """The flags of a command that reads the config: --config, and each setting
    that has a flag of its own, each also a config key and an RC_DASH_* variable."""
    g = p.add_argument_group("settings", textwrap.fill(
        "Each also a config key and an RC_DASH_* variable. Per key, a flag beats its "
        "variable, which beats the config file, which beats the built-in default.", 77))
    g.add_argument("--config", metavar="FILE",
                   help="the config file, TOML (.toml) or JSON (RC_DASH_CONFIG); default: "
                        "dashboard/dashboard.toml or dashboard/dashboard.config.json, "
                        "whichever exists")
    g.add_argument("--ebpf-root", action="append", dest="ebpf_root", metavar="DIR",
                   help="the collector's eBPF tier folder (its EBPFM_OUTPUT_DIR), in place of "
                        "the default candidates; repeat for more, or give a glob pattern "
                        "(feeds.ebpf.roots, RC_DASH_EBPF_ROOTS)")
    g.add_argument("--node-root", action="append", dest="node_root", metavar="DIR",
                   help="the collector's node tier folder (EBPFM_NODE_OUTPUT_DIR), as "
                        "--ebpf-root (feeds.ebpf_node.roots, RC_DASH_NODE_ROOTS)")
    g.add_argument("--ebpf-days", type=int, metavar="N",
                   help="dated folders of the eBPF tier read: retention's days and one "
                        "(feeds.ebpf.days, RC_DASH_EBPF_DAYS; 8)")
    g.add_argument("--as-of", metavar="TIME",
                   help="pin the service's present, for a capture copied from another "
                        "cluster: auto (just after its newest record) or a local time "
                        "YYYY-MM-DD HH:MM:SS (clock.as_of, RC_DASH_AS_OF)")
    if serve:
        g.add_argument("--retention-min", type=int, metavar="MIN",
                       help="minutes of history held, the widest window offered "
                            "(live.retention_min, RC_DASH_RETENTION_MIN; 10080)")
        g.add_argument("--live-window-min", type=int, metavar="MIN",
                       help="the window a fresh page opens on (live.window_min, "
                            "RC_DASH_LIVE_WINDOW_MIN; 1440)")
        g.add_argument("--backfill-hours", type=int, metavar="H",
                       help="hours of history read back at each start (live.backfill_hours, "
                            "RC_DASH_BACKFILL_HOURS; 168)")
        g.add_argument("--host", metavar="ADDR",
                       help="the address to listen on (server.host, RC_DASH_HOST; 127.0.0.1)")
        g.add_argument("--port", type=int, metavar="PORT",
                       help="the port to listen on (server.port, RC_DASH_PORT; 8080)")
        g.add_argument("--static-dir", metavar="DIR",
                       help="the built page (server.static_dir, RC_DASH_STATIC_DIR; "
                            "dashboard/web/dist)")
        g.add_argument("--open", action="store_true", default=None,
                       help="serve without a password, to anyone who reaches the port "
                            "(server.auth.open, RC_DASH_OPEN=1)")
    # Hidden: the old flag spellings of two commands, and the retired feeds' flags
    # (config.RETIRED_CLI: taken, named in a warning and ignored, so a unit or a
    # wrapper that still passes one starts).
    p.add_argument("--print-config", action="store_true", help=H)
    p.add_argument("--check-feeds", action="store_true", help=H)
    p.add_argument("--sacct-dir", default=None, help=H)
    p.add_argument("--jobs-csv", default=None, help=H)


def _cmd(sub, name, summary, description, examples):
    # the examples as written, the description wrapped as the flags' help is
    p = sub.add_parser(name, help=summary, description=textwrap.fill(description, 79),
                       epilog="examples:\n" + examples, formatter_class=RAW)
    p.add_argument("--version", action=_Version, help=H)
    return p


def build_parser():
    ap = CLI(prog="rc-dashboard", formatter_class=RAW,
             description="The live agent-behaviour dashboard over the ebpfm collector's files. "
                         "With no command, `serve`.",
             epilog="examples:\n"
                    "  rc-dashboard init                  a first config and password, asked\n"
                    "  rc-dashboard check-feeds           where the collector's files are\n"
                    "  rc-dashboard serve --port 8080     the service\n"
                    "  rc-dashboard status --json         for a monitor; exit 0, 1 or 2\n"
                    "  rc-dashboard <command> --help      each command's flags\n"
                    "\nFrom a checkout: cd dashboard && uv run --frozen rc-dashboard ...\n"
                    "(python -m rc_dashboard ... is the same).")
    ap.add_argument("--version", action=_Version, help="print the version and exit")
    sub = ap.add_subparsers(dest="command", metavar="<command>", parser_class=argparse.ArgumentParser)

    p = _cmd(sub, "serve", "the service (the default command)",
             "Serve the page and its API over the collector's files. Refuses to start without "
             "a password (server.auth.password_hash_file or server.auth.password_hash) unless "
             "--open.",
             "  rc-dashboard serve\n"
             "  rc-dashboard serve --config /etc/rc-dashboard/dashboard.toml --port 8081\n"
             "  RC_DASH_EBPF_ROOTS=/var/log/ebpfm rc-dashboard serve\n"
             "  rc-dashboard serve --as-of auto --ebpf-root /data/capture   # a copied capture")
    _settings(p)

    p = _cmd(sub, "demo", "the service over a synthetic feed it writes itself",
             "Write a few days of made-up collector records for fictional login nodes, keep "
             "appending to them, and serve the unchanged service over them. Every record says "
             "it is synthetic, and so does every page. Open unless a password is set.",
             "  rc-dashboard demo                  http://127.0.0.1:8080\n"
             "  rc-dashboard demo --demo-days 7 --demo-hosts 4 --port 8087")
    _settings(p)
    g = p.add_argument_group("demo", "Each also RC_DASH_DEMO_<NAME>.")
    g.add_argument("--demo-dir", default=None, metavar="DIR",
                   help="where the demo feed and its saved builds live (.cache/demo)")
    g.add_argument("--demo-days", type=int, default=None, metavar="N",
                   help="days of history written before serving, 1..14 (3)")
    g.add_argument("--demo-hosts", type=int, default=None, metavar="N",
                   help="fictional login nodes, 1..6 (2)")
    g.add_argument("--demo-seed", default=None, metavar="TEXT",
                   help="another seed is another set of made-up sessions (demo)")
    g.add_argument("--demo-fresh", action="store_true",
                   help="discard the demo feed and write it again")
    g.add_argument("--demo-write-only", action="store_true",
                   help="write the history and exit, without serving")

    p = _cmd(sub, "init", "write a first config, make a password, say what to run next",
             "Find the collector's folders (the default candidates, /etc/default/ebpfm), see "
             "whether this host runs Slurm, make a password and its hash file, write a short "
             "TOML config, check the feeds, and print the serve and ssh tunnel commands. Asks "
             "for each value unless --yes; never replaces a config without --force.",
             "  rc-dashboard init\n"
             "  rc-dashboard init --yes --ebpf-root /scratch/ebpfm --node-root "
             "/scratch/ebpfm-login \\\n"
             "      --site-name 'cluster X login nodes' --no-slurm")
    p.add_argument("--yes", action="store_true",
                   help="ask nothing: take each value from its flag, else what was found")
    p.add_argument("--ebpf-root", action="append", dest="ebpf_root", metavar="DIR",
                   help="the collector's eBPF tier folder; repeat for more, or a glob pattern")
    p.add_argument("--node-root", action="append", dest="node_root", metavar="DIR",
                   help="the collector's node tier folder; repeat for more")
    p.add_argument("--site-name", metavar="TEXT",
                   help="what every page calls this deployment (site.name; default: this "
                        "host's name)")
    p.add_argument("--slurm", dest="slurm", action="store_const", const=True, default=None,
                   help="this cluster runs Slurm: the HPC tab is on (default: as found, sbatch "
                        "on PATH or /etc/slurm)")
    p.add_argument("--no-slurm", dest="slurm", action="store_const", const=False,
                   help="this cluster runs no Slurm: the HPC tab is off")
    p.add_argument("--config", metavar="PATH",
                   help="where to write the config (default: dashboard/dashboard.toml in a "
                        "checkout, else ~/.config/rc-dashboard/dashboard.toml)")
    p.add_argument("--password-dir", metavar="DIR",
                   help="where the password, its netrc and its hash file go "
                        "(~/.config/rc-dashboard)")
    p.add_argument("--port", type=int, metavar="PORT", help="the port to serve on (8080)")
    p.add_argument("--force", action="store_true",
                   help="replace a config or a password already there")

    p = _cmd(sub, "check-feeds", "where the collector's files were looked for, and what was found",
             "Resolve both feeds as `serve` would and print, per tier, its status, and every "
             "place it looked: found (N files), empty, missing, or permission denied (with "
             "who you are and the folder's owner, group and mode). Then what the history store "
             "holds of the sealed days. Exit 1 when the required eBPF feed is missing or empty.",
             "  rc-dashboard check-feeds\n"
             "  rc-dashboard check-feeds --ebpf-root '/data/mirror/*/ebpf'")
    _settings(p, serve=False)

    p = _cmd(sub, "status", "is the collection working: per host, for a monitor",
             "From the config `serve` reads, with no service running: per host, the "
             "collector's verdict (from its own heartbeat, start and stop records), its newest "
             "record's age per tier, and the copy's state (status.transport_files). Exit 0 ok, "
             "1 warn, 2 crit. The running service answers the same at /api/status and /metrics.",
             "  rc-dashboard status\n"
             "  rc-dashboard status --json --stale 30m\n"
             "  RC_DASH_TRANSPORT_FILES='/var/lib/rc-dashboard/mirror/.state/*.json' "
             "rc-dashboard status")
    p.add_argument("--json", action="store_true",
                   help="one JSON document: {level, why, hosts} (dashboard/README.md, Status)")
    p.add_argument("--stale", metavar="DURATION",
                   help="a newest record, or a copy's last good pass, older than this is a "
                        "problem: 300, 90s, 15m, 2h, 1d (status.stale_s; 15m)")
    _settings(p, serve=False)

    p = _cmd(sub, "print-config", "the resolved settings, each with where it came from",
             "Print every setting as JSON, each {_value, _origin}: a flag, a variable, the "
             "config file, or the default; a path also lists what exists of it (_existing). "
             "--reference prints every setting with its built-in default as TOML instead.",
             "  rc-dashboard print-config\n"
             "  rc-dashboard print-config --reference > dashboard.full.toml")
    p.add_argument("--reference", action="store_true",
                   help="every setting with its built-in default, as a TOML config file, with "
                        "the RC_DASH_* variable of each")
    _settings(p)

    p = _cmd(sub, "new-password", "a password for the page, and its hash",
             "Make a 16-character password, write it and a netrc for curl to --dir (both "
             "0600), and write its hash to the hash file --install names (0640), or print the "
             "hash. The service reads the hash file again when it changes, so a new password "
             "needs no restart; everyone signs in again.",
             "  rc-dashboard new-password --install ~/.config/rc-dashboard/password.hash\n"
             "  sudo rc-dashboard new-password --install /etc/rc-dashboard/password.hash "
             "--dir /root/.config/rc-dashboard --force\n"
             "  rc-dashboard new-password --netrc-host dash.example.org")
    p.add_argument("--install", metavar="HASHFILE",
                   help="write the hash to this file, for server.auth.password_hash_file "
                        "(RC_DASH_PASSWORD_HASH_FILE); folders on the way are made")
    p.add_argument("--dir", default=None, metavar="DIR",
                   help="where the password and its netrc go (~/.config/rc-dashboard)")
    p.add_argument("--netrc-host", action="append", default=None, metavar="HOST",
                   help="a host the netrc names besides 127.0.0.1 and localhost; repeat for more")
    p.add_argument("--force", action="store_true",
                   help="replace a password or a hash file already there, signing everyone out")

    p = _cmd(sub, "backup", "the state that cannot be rebuilt, into a tar",
             "Copy into one tar what is not rebuilt from the collector's files: the Trend "
             "tab's roll-up table (with SQLite's backup, so a running service is fine), the "
             "anonymize salt, the session key, the config file, the password hash file, and "
             "Ask's suggestions marked done.",
             "  rc-dashboard backup --to /backups/rc-dashboard-$(date +%F).tar")
    p.add_argument("--to", required=True, metavar="FILE.tar", help="the tar to write (0600)")
    p.add_argument("--force", action="store_true", help="replace a tar already there")
    _settings(p, serve=False)

    p = _cmd(sub, "restore", "put a backup's state back",
             "Put each file of a backup where the config says it goes now: the config file "
             "first (to --config, else where it was), then the rest where that config puts "
             "them. Refuses to replace a file that is there without --force. Stop the service "
             "first.",
             "  rc-dashboard restore --from /backups/rc-dashboard-2026-11-23.tar\n"
             "  rc-dashboard restore --from backup.tar --config /etc/rc-dashboard/dashboard.toml "
             "--force")
    p.add_argument("--from", required=True, dest="src", metavar="FILE.tar",
                   help="the tar `backup` wrote")
    p.add_argument("--force", action="store_true", help="replace files already there")
    _settings(p, serve=False)

    p = _cmd(sub, "rollup", "older days into the Trend tab's table",
             "Read every dated folder near a day the roll-up table lacks, oldest first, and "
             "write each day once the folders either side of it are read too. A full parse of "
             "every record: run once, by hand.",
             "  rc-dashboard rollup --rollup-days 365\n"
             "  rc-dashboard rollup --rollup-days 30 --force")
    p.add_argument("--rollup-days", type=int, default=365, metavar="N",
                   help="the days back to fill from the day-files on disk (365)")
    p.add_argument("--force", action="store_true",
                   help="read again the days the table already holds complete")
    _settings(p, serve=False)

    _cmd(sub, "version", "the version, and the commit it was built from",
         "Print `rc-dashboard <version> (<commit>)`: the commit from a build stamp, else the "
         "checkout's git HEAD; none when neither is known.",
         "  rc-dashboard version\n"
         "  rc-dashboard --version")
    return ap


# What `serve` says when it will not start open (`refusal`).
NO_PASSWORD = """\
refusing to serve: there is no password (neither server.auth.password_hash_file nor
server.auth.password_hash is set), so anyone who reaches port %(port)s -- on a login
node, every user of it -- would read every user's commands. Either:
  - make one: `rc-dashboard new-password --install ~/.config/rc-dashboard/password.hash`
    writes it to a file only you can read and its hash to that hash file. Name the hash
    file in the config, "server": {"auth": {"password_hash_file": "<file>"}} (in TOML,
    [server.auth] password_hash_file = "<file>"), or in RC_DASH_PASSWORD_HASH_FILE, then
    start again: the service reads the file again when it changes, so a new password
    needs no restart. Or put the hash itself in the config as
    "server": {"auth": {"password_hash": "<hash>"}} (or in RC_DASH_PASSWORD_HASH); or
  - serve it open anyway, on a machine only you can reach: --open (RC_DASH_OPEN=1,
    or "server": {"auth": {"open": true}}).
"""


def refusal(cfg):
    """Why the service must not start, or None. With no password it starts only
    when told to (`server.auth.open`); a demo is (demo.configure)."""
    from . import auth
    if auth.has_password(cfg) or cfg.get("server.auth.open"):
        return None
    return NO_PASSWORD % {"port": cfg.get("server.port")}


def new_password(args):
    """Make a password, write it and a netrc for curl, and write its hash to a
    hash file (--install) or print it. The password itself is never printed."""
    from . import auth
    try:
        path, stored = auth.new_password(args.dir, args.netrc_host or (), args.force,
                                         hash_file=args.install)
    except (FileExistsError, ValueError) as e:
        sys.stderr.write("new-password: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("new-password: %s: %s\n" % (getattr(e, "filename", None) or "",
                                                     e.strerror or e))
        return 1
    print("Wrote a new 16-character password to %s, and a netrc for curl beside it, "
          "both mode 600." % path)
    if args.install:
        hf = os.path.abspath(os.path.expanduser(args.install))
        print("Wrote its hash to %s (mode 640). Name that file as the service's hash file:"
              % hf)
        print('  server.auth.password_hash_file = "%s"   (TOML, under [server.auth]; or' % hf)
        print('  "server": {"auth": {"password_hash_file": "%s"}} in JSON, or' % hf)
        print("  RC_DASH_PASSWORD_HASH_FILE=%s)" % hf)
        print("A service that already reads it takes the new hash at the next sign-in, with "
              "no restart; everyone signs in again.")
        print(stored)
        return 0
    print("Give the service its hash, then start it again; everyone signs in again:")
    print("  - in a hash file (the way): save the line below to a file and name it as "
          "server.auth.password_hash_file")
    print("    in the config, or RC_DASH_PASSWORD_HASH_FILE. `rc-dashboard new-password "
          "--install FILE` writes it for you,")
    print("    and the service reads the file again when it changes, so a new password needs "
          "no restart;")
    print("  - or the hash itself: server.auth.password_hash in the config, or "
          "RC_DASH_PASSWORD_HASH.")
    print(stored)
    return 0


def check_feeds(cfg):
    """`check-feeds`: the collector's roots, every place each feed looked, and what
    the history store holds of their sealed days; nothing is converted here."""
    code = feedmod.print_table(feedmod.resolve(cfg))
    from . import history
    try:
        lines = history.describe(cfg)
    except Exception as e:              # a store that cannot be read stops no check
        lines = ["  history ebpf      could not be read: %s: %s" % (type(e).__name__, e)]
    for line in lines:
        print(line)
    return code


def status(args, cfg):
    """`status`: the collection's state per host; exit 0 ok, 1 warn, 2 crit."""
    from . import status as statusmod
    try:
        stale = statusmod.parse_duration(args.stale) if args.stale else None
    except ValueError as e:
        sys.stderr.write("status: --stale %s\n" % e)
        return 2
    if cfg.get("clock.as_of"):
        from . import clock
        clock.install(cfg, feedmod.resolve(cfg)["ebpf"]["resolved"])
    from . import auth
    doc = statusmod.collect(cfg, stale_s=stale, wall=auth.wall())
    if args.json:
        print(json.dumps(doc, indent=1, sort_keys=True))
    else:
        print(statusmod.text(doc))
    return statusmod.EXIT[doc["level"]]


def _hup(gate):
    """SIGHUP: the hash file read again, and what came of it said."""
    if not gate.reload() and gate.reload_error is None:
        sys.stderr.write("sign-in: SIGHUP: the hash in %s is the one in use\n" % gate.hash_file)
        sys.stderr.flush()


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd == "version":
        from . import about
        print(about.version_line())
        return 0
    if cmd == "new-password":
        return new_password(args)
    if cmd == "init":
        from . import init_cmd
        return init_cmd.main(args)
    if cmd == "print-config" and getattr(args, "reference", False):
        sys.stdout.write(configmod.reference_toml())
        return 0
    if cmd == "restore" and not (args.print_config or args.check_feeds):
        # before the config is read: the config it reads may be the one it restores
        from . import backup
        try:
            return backup.restore(args, args.src, force=args.force)
        except backup.BackupError as e:
            sys.stderr.write("restore: %s\n" % e)
            return 1
    cfg = configmod.load(args, args.config)

    demo = None
    if cmd == "demo":
        from . import demo as demomod
        # A demo reads only the feed it writes: a root or a pinned clock named
        # here would be ignored, so it is refused rather than dropped quietly.
        given = [f for f, v in (("--ebpf-root", args.ebpf_root), ("--node-root", args.node_root),
                                ("--as-of", args.as_of)) if v]
        if given:
            sys.stderr.write("demo reads only the synthetic feed it writes; drop %s\n"
                             % ", ".join(given))
            return 2
        opts = demomod.options(args)
        demomod.configure(cfg, opts)
        if not args.print_config:
            demo = demomod.Demo(opts).prepare()
            if args.demo_write_only:
                sys.stderr.write("demo: feed written to %s\n" % demo.feed.base)
                return 0

    if args.print_config or cmd == "print-config":
        print(json.dumps(cfg.as_dict(with_origins=True), indent=1, default=str))
        return 0
    if args.check_feeds or cmd == "check-feeds":
        return check_feeds(cfg)
    if cmd == "status":
        return status(args, cfg)
    if cmd == "backup":
        from . import backup
        try:
            return backup.backup(cfg, args.to, force=args.force)
        except backup.BackupError as e:
            sys.stderr.write("backup: %s\n" % e)
            return 1

    if cmd == "rollup":
        from . import rollup
        if not rollup.enabled(cfg):
            sys.stderr.write("rollup: off (rollup.enabled)\n")
            return 2
        if cfg.get("clock.as_of"):
            # the service's present, so both pick the same days (clock.py)
            from . import clock
            clk = clock.install(cfg, feedmod.resolve(cfg)["ebpf"]["resolved"])
            sys.stderr.write("clock pinned to %s (%s)\n" % (clk["as_of"], clk["source"]))
        n = rollup.build_from_files(cfg, days=args.rollup_days, force=args.force)
        sys.stderr.write("rollup: %d day(s) written to %s\n" % (n, rollup.path_of(cfg)))
        return 0

    why = refusal(cfg)
    if why:
        sys.stderr.write(why)
        return 2

    import uvicorn
    from . import clock
    from .app import create_app
    # Before anything reads the time: the window registry starts building the
    # default window as the app is created.
    if cfg.get("clock.as_of"):
        clk = clock.install(cfg, feedmod.resolve(cfg)["ebpf"]["resolved"])
        sys.stderr.write("clock pinned to %s (%s)\n" % (clk["as_of"], clk["source"]))
    if demo is not None:
        # Before the app positions its tail: what lands before that is read by
        # the backfill, what lands after by the tail, and nothing by both.
        demo.start_live()
        sys.stderr.write("demo: serving synthetic data on http://%s:%s\n"
                         % (cfg.get("server.host"), cfg.get("server.port")))
    app = create_app(cfg)
    gate = app.state.auth
    if gate is not None:
        sys.stderr.write("sign-in on: %s; a session lasts %g days\n" % (
            "the password's hash is read from %s, again when it changes and on SIGHUP"
            % gate.hash_file if getattr(gate, "hash_file", None)
            else "server.auth.password_hash is set", gate.lifetime_s / 86400))
        if getattr(gate, "hash_file", None):
            # SIGHUP reads the hash file again (auth.Gate.reload). Only with a
            # hash file: otherwise SIGHUP still ends the service, as it did.
            signal.signal(signal.SIGHUP, lambda *_a: _hup(gate))
    else:
        sys.stderr.write("sign-in off (server.auth.open, set by %s): anyone who reaches port "
                         "%s reads the page\n" % (cfg.origin("server.auth.open"),
                                                  cfg.get("server.port")))
    # An open websocket (every page holds one) must not hold a restart hostage.
    try:
        uvicorn.run(app, host=cfg.get("server.host"), port=int(cfg.get("server.port")),
                    log_level="info", timeout_graceful_shutdown=3)
        code = 0
    except SystemExit as e:              # uvicorn's own exit, e.g. a failed bind
        code = e.code if isinstance(e.code, int) else 1
    # Then leave at once. The backfill and the node-tier read run on executor
    # threads, which the interpreter joins at exit; a restart during either
    # outlived the dev runner's grace, was SIGKILLed only through its `uv run`
    # wrapper, and the orphan kept the port -- the next server failed to bind
    # and hung, and every request was a 502. Nothing is lost: window snapshots
    # are written to a temp file and renamed, and everything else is rebuilt
    # from the collector's files on the next start.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
