"""Run the service and record what every thread was doing whenever the event loop stalls.

    RC_DASH_*=... .venv/bin/python bench/stallwatch.py OUT.txt [--after 0.5]

The service as `python -m rc_dashboard serve` runs it, plus one coroutine that
re-arms `faulthandler.dump_traceback_later` every 100 ms. faulthandler's
watchdog is a C thread that needs no GIL, so when the loop has not re-armed it
for `--after` seconds -- because some thread holds the GIL in one long C call,
or the loop itself is busy -- it writes every thread's Python stack to OUT, and
that stack names the culprit. A diagnostic for the bench, not part of the service.
"""
import asyncio
import faulthandler
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main():
    out_path = sys.argv[1]
    after = float(sys.argv[sys.argv.index("--after") + 1]) if "--after" in sys.argv else 0.5
    if "--switch-ms" in sys.argv:
        # an experiment: how often a thread waiting for the GIL gets it (default 5 ms)
        sys.setswitchinterval(float(sys.argv[sys.argv.index("--switch-ms") + 1]) / 1000.0)
    sink = open(out_path, "w", buffering=1)

    from rc_dashboard import app as appmod
    real_create = appmod.create_app

    def create_app(cfg):
        app = real_create(cfg)

        @app.on_event("startup")
        async def _watch():
            async def tick():
                while True:
                    faulthandler.dump_traceback_later(after, repeat=False, file=sink)
                    await asyncio.sleep(0.1)
            app.state.stallwatch = asyncio.create_task(tick())
        return app
    appmod.create_app = create_app
    from rc_dashboard.__main__ import main as serve
    sys.argv = [sys.argv[0], "serve"]
    serve()


if __name__ == "__main__":
    main()
