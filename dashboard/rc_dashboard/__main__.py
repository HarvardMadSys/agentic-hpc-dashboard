"""CLI: `--print-config`, `--check-feeds`, `serve`, `demo` (rc_dashboard/demo), and
`new-password`."""
import argparse
import json
import os
import sys

from . import config as configmod
from . import feeds as feedmod


def build_parser():
    ap = argparse.ArgumentParser(prog="rc-dashboard")
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "check-feeds", "print-config", "demo", "new-password"])
    ap.add_argument("--config", default=None)
    # every flag defaults to None so an unset flag never outranks the config file
    ap.add_argument("--ebpf-root", action="append", dest="ebpf_root", default=None)
    ap.add_argument("--node-root", action="append", dest="node_root", default=None)
    ap.add_argument("--ebpf-days", type=int, default=None)
    ap.add_argument("--sacct-dir", default=None)
    ap.add_argument("--jobs-csv", default=None)
    # --retention-min is the ceiling the picker can reach; --live-window-min is
    # only the view a fresh page opens on. Raising the view is free; raising
    # retention costs memory and a longer backfill.
    ap.add_argument("--retention-min", type=int, default=None)
    ap.add_argument("--live-window-min", type=int, default=None)
    ap.add_argument("--backfill-hours", type=int, default=None)
    # Pin the service's present: "auto" (the feed's newest record) or a local
    # time. For a capture copied from another cluster; see clock.py.
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--static-dir", default=None)
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--check-feeds", action="store_true")
    # `demo`: the service over a synthetic feed it writes itself (demo/__init__.py).
    # Each also reads RC_DASH_DEMO_<NAME>.
    ap.add_argument("--demo-dir", default=None,
                    help="where the demo feed and its saved builds live (.cache/demo)")
    ap.add_argument("--demo-days", type=int, default=None,
                    help="days of history to write before serving (3)")
    ap.add_argument("--demo-hosts", type=int, default=None,
                    help="fictional login nodes (2)")
    ap.add_argument("--demo-seed", default=None,
                    help="another seed is another week of made-up sessions (demo)")
    ap.add_argument("--demo-fresh", action="store_true",
                    help="discard the demo feed and write it again")
    ap.add_argument("--demo-write-only", action="store_true",
                    help="write the history and exit, without serving")
    # new-password: where the password and its netrc go (auth.DEFAULT_DIR), the
    # hosts the netrc names beside 127.0.0.1 and localhost, and whether to
    # replace a password already there.
    ap.add_argument("--dir", default=None)
    ap.add_argument("--netrc-host", action="append", default=None)
    ap.add_argument("--force", action="store_true")
    return ap


def new_password(args):
    """Make a password, write it and a netrc for curl, and print the hash the
    config needs. The password itself is never printed."""
    from . import auth
    try:
        path, stored = auth.new_password(args.dir, args.netrc_host or (), args.force)
    except (FileExistsError, ValueError) as e:
        sys.stderr.write("new-password: %s\n" % e)
        return 1
    print("Wrote a new 16-character password to %s, and a netrc for curl beside it, "
          "both mode 600." % path)
    print('Put this hash in dashboard.config.json as "server": {"auth": {"password_hash": ...}}, '
          "then restart the service. Everyone signs in again.")
    print(stored)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "new-password":
        return new_password(args)
    cfg = configmod.load(args, args.config)

    demo = None
    if args.command == "demo":
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

    if args.print_config or args.command == "print-config":
        print(json.dumps(cfg.as_dict(with_origins=True), indent=1, default=str))
        return 0
    if args.check_feeds or args.command == "check-feeds":
        # The collector's roots, and what the history store holds of their
        # sealed days (history.py); nothing is converted here.
        code = feedmod.print_table(feedmod.resolve(cfg))
        from . import history
        for line in history.describe(cfg):
            print(line)
        return code

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
    if app.state.auth is not None:
        sys.stderr.write("sign-in on: server.auth.password_hash is set; a session lasts %g days\n"
                         % (app.state.auth.lifetime_s / 86400))
    else:
        sys.stderr.write("sign-in off: server.auth.password_hash is empty, so anyone who "
                         "reaches port %s reads the page\n" % cfg.get("server.port"))
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
