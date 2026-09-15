"""The service: REST + WebSocket push + the built frontend.

The ingest loop, the feed refresh and the historical rebuild are background
tasks, so a slow read never blocks a request.  WebSocket clients get a frame when
something actually changed -- the page must not animate between reads, because
the retired viewer did exactly that with fabricated data.
"""
import asyncio
import json
import os
import time

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import feeds as feedmod
from .aggregator import Aggregator
from .export import make_predicate, meta_header, parse_filters, scan
from .normalize import Normalizer


def create_app(cfg):
    app = FastAPI(title="rc-dashboard", version="0.1.0")
    agg = Aggregator(cfg)
    app.state.cfg = cfg
    app.state.agg = agg
    app.state.clients = set()

    async def broadcast(kind, payload):
        dead = []
        frame = json.dumps({"type": kind, "payload": payload}, default=str)
        for ws in list(app.state.clients):
            try:
                await ws.send_text(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            app.state.clients.discard(ws)

    # ------------------------------------------------------------ background
    async def ingest_loop():
        interval = max(0.2, int(cfg.get("ingest.poll_ms", 1000)) / 1000.0)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, agg.backfill_now)
        await broadcast("backfill", agg.backfill)
        await broadcast("live", agg.live_payload())
        while True:
            try:
                n = await loop.run_in_executor(None, agg.poll_once)
                if n:
                    await broadcast("live", agg.live_payload())
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def feeds_loop():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(30)
            try:
                await loop.run_in_executor(None, agg.refresh_feeds)
                await broadcast("feeds", agg.reports)
            except Exception:
                pass

    async def rebuild_loop():
        interval = max(60, int(cfg.get("rebuild.interval_s", 900)))
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(interval)
            try:
                await loop.run_in_executor(None, agg.rebuild_history)
                await broadcast("rebuilt", {"full_built_at": agg.full_built_at})
            except Exception:
                pass

    @app.on_event("startup")
    async def _startup():
        app.state.tasks = [asyncio.create_task(ingest_loop()),
                           asyncio.create_task(feeds_loop()),
                           asyncio.create_task(rebuild_loop())]

    @app.on_event("shutdown")
    async def _shutdown():
        for t in getattr(app.state, "tasks", []):
            t.cancel()

    # ------------------------------------------------------------------- API
    @app.get("/api/config")
    def api_config():
        return cfg.as_dict(with_origins=True)

    @app.get("/api/feeds")
    def api_feeds():
        return agg.refresh_feeds()

    @app.get("/api/panels")
    def api_panels():
        return agg.panels()

    @app.get("/api/live")
    def api_live():
        return agg.live_payload()

    @app.get("/api/trajectories")
    def api_traj(rank: str = Query("events")):
        out = agg.trajectories()
        out["rank_by"] = rank if rank in out.get("ranks", {}) else "events"
        return out

    @app.get("/api/health")
    def api_health():
        return {"ok": True, "records": agg.stats["records"],
                "last_poll": agg.stats["last_poll"],
                "backfill": agg.backfill,
                "full_built_at": agg.full_built_at}

    def _roots():
        return (agg.reports["ebpf"]["resolved"]
                or cfg.get("feeds.ebpf.roots") or [])

    @app.get("/api/events")
    def api_events(request: Request,
                   limit: int = Query(200, le=5000),
                   cursor: int = Query(0, ge=0),
                   order: str = Query("desc")):
        f = parse_filters(dict(request.query_params.multi_items()
                               if hasattr(request.query_params, "multi_items")
                               else request.query_params))
        pred = make_predicate(f)
        norm = Normalizer(cfg)
        newest = order != "asc"
        # The bench's columns -- command, ran, cpu, exit -- only mean anything
        # for a process exit, and an unfiltered feed is ~40% tcp/conn/residency
        # rows that would render as mostly-blank. So `exit` is the default, and
        # the response SAYS it applied one: a silently narrowed result set is
        # exactly the kind of thing that misleads. Pass `event=` (empty) or any
        # explicit value to widen.
        default_event = not f.get("event")
        if default_event:
            f["event"] = {"exit"}
            pred = make_predicate(f)
        rows, scanned, matched = [], 0, 0
        for rec, scanned, matched in scan(_roots(), norm, pred, limit=limit,
                                          offset=cursor, newest_first=newest):
            rows.append(rec)
        rep = agg.reports["ebpf"]
        full = len(rows) >= limit
        return {"rows": rows,
                # None when the page did not fill: there is provably nothing after it
                "next_cursor": (cursor + len(rows)) if full else None,
                "cursor": cursor, "limit": limit,
                "order": "desc" if newest else "asc",
                "event_filter_defaulted": default_event,
                "returned": len(rows), "total_scanned": scanned,
                "truncated": full,
                "feed": feedmod.panel_meta(rep)}

    @app.get("/api/export")
    def api_export(request: Request, order: str = Query("desc")):
        f = parse_filters(dict(request.query_params.multi_items()
                               if hasattr(request.query_params, "multi_items")
                               else request.query_params))
        # same default as /api/events, so "the download is the view" holds
        export_default_event = not f.get("event")
        if export_default_event:
            f["event"] = {"exit"}
        pred = make_predicate(f)
        norm = Normalizer(cfg)
        cap = int(cfg.get("export.max_rows", 5000000))
        reports = agg.reports
        t0 = time.time()

        def gen():
            matched = 0
            body = []
            for rec, _scanned, matched in scan(_roots(), norm, pred, limit=cap,
                                               newest_first=order != "asc"):
                body.append(json.dumps(rec, default=str))
                if len(body) >= 500:
                    yield "\n".join(body) + "\n"
                    body = []
            # the header is emitted first by the wrapper below; here we only
            # need the tail flush
            if body:
                yield "\n".join(body) + "\n"

        def stream():
            # a cheap pre-pass is not worth a second full scan, so the header
            # declares the cap and the filters and leaves `rows` to the trailer
            head = meta_header(f, reports, cfg, None, None, 0.0)
            head["_meta"]["rows"] = "see _trailer"
            yield json.dumps(head) + "\n"
            n = 0
            for chunk in gen():
                n += chunk.count("\n")
                yield chunk
            yield json.dumps({"_trailer": {
                "rows": n, "truncated": n >= cap,
                "elapsed_s": round(time.time() - t0, 2)}}) + "\n"

        stamp = time.strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            stream(), media_type="application/x-ndjson",
            headers={"Content-Disposition":
                     'attachment; filename="rc_events_%s.jsonl"' % stamp})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        app.state.clients.add(websocket)
        try:
            await websocket.send_text(json.dumps(
                {"type": "live", "payload": agg.live_payload()}, default=str))
            await websocket.send_text(json.dumps(
                {"type": "feeds", "payload": agg.reports}, default=str))
            while True:
                await websocket.receive_text()      # keepalive / client pings
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            app.state.clients.discard(websocket)

    # --------------------------------------------------------------- statics
    static = cfg.get("server.static_dir")
    if static and os.path.isdir(static):
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    else:
        @app.get("/")
        def _no_ui():
            return JSONResponse({
                "error": "frontend not built",
                "expected": static,
                "fix": "cd dashboard/web && npm ci && npm run build",
                "api": ["/api/health", "/api/feeds", "/api/live", "/api/panels",
                        "/api/trajectories", "/api/events", "/api/export"],
            }, status_code=503)
    return app
