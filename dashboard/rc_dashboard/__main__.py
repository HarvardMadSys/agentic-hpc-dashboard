"""CLI: `--print-config`, `--check-feeds`, and `serve`."""
import argparse
import json
import os
import sys

from . import config as configmod
from . import feeds as feedmod


def build_parser():
    ap = argparse.ArgumentParser(prog="rc-dashboard")
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "check-feeds", "print-config"])
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
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = configmod.load(args, args.config)

    if args.print_config or args.command == "print-config":
        print(json.dumps(cfg.as_dict(with_origins=True), indent=1, default=str))
        return 0
    if args.check_feeds or args.command == "check-feeds":
        return feedmod.print_table(feedmod.resolve(cfg))

    import uvicorn
    from . import clock
    from .app import create_app
    # Before anything reads the time: the window registry starts building the
    # default window as the app is created.
    if cfg.get("clock.as_of"):
        clk = clock.install(cfg, feedmod.resolve(cfg)["ebpf"]["resolved"])
        sys.stderr.write("clock pinned to %s (%s)\n" % (clk["as_of"], clk["source"]))
    app = create_app(cfg)
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
