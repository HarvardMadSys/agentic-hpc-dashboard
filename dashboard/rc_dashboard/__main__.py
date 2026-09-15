"""CLI: `--print-config`, `--check-feeds`, and `serve`."""
import argparse
import json
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
    ap.add_argument("--live-window-min", type=int, default=None)
    ap.add_argument("--backfill-hours", type=int, default=None)
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
    from .app import create_app
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.get("server.host"), port=int(cfg.get("server.port")),
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
