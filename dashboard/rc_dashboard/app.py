"""The service: REST + WebSocket push + the built frontend.

The ingest loop, the feed refresh and the historical rebuild are background
tasks, so a slow read never blocks a request.  WebSocket clients get a frame when
something actually changed -- the page must not animate between reads, because
the retired viewer did exactly that with fabricated data.
"""
import asyncio
import json
import os
import re
import time

from fastapi import (FastAPI, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import feeds as feedmod
from . import proctree
from . import redact
from . import windows as winmod
from .aggregator import Aggregator
from .export import make_predicate, meta_header, parse_filters, scan
from .iocmds import PAGE_MAX as iocmd_page_max, IoCommands, agent_keys
from .normalize import Normalizer


def query_lists(request):
    """Query params as name -> every value given. Repeatable keys keep them all:
    `dict(multi_items())` kept only the LAST, so `class=agent&class=human` --
    which is what the page sends -- filtered the events by `human` alone."""
    qp = request.query_params
    return {k: qp.getlist(k) for k in qp.keys()}


def dealias(params, anon):
    """Query params with every `user` value mapped from its alias back to the
    account (anonymize.py). Anything that is not a known alias -- a real name
    included -- maps to a value no record carries."""
    if anon is None or not params.get("user"):
        return params
    out = dict(params)
    out["user"] = [",".join(anon.dealias_user(x) for x in str(v).split(",") if x)
                   for v in params["user"]]
    return out


def dealias_sid(sid, anon):
    """A session id as the service keys it. A terminal's (`tty:<user>@<tty>`)
    carries its account, which the page only ever saw as an alias."""
    if anon is None or not isinstance(sid, str):
        return sid
    head, sep, key = sid.rpartition("|")
    if sep and key.startswith("tty:") and "@" in key:
        who, _, tty = key[4:].partition("@")
        return "%s|tty:%s@%s" % (head, anon.dealias_user(who), tty)
    return sid


def view_filter(request, anon=None):
    """The page-wide `user` / `agent_type` filter, in `/api/events`' vocabulary."""
    try:
        return winmod.ViewFilter.from_params(dealias(query_lists(request), anon))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def create_app(cfg):
    agg = Aggregator(cfg)
    anon = agg.anon

    class AnonJSONResponse(JSONResponse):
        """Every JSON body, with its names aliased when `anonymize.enabled` is on.

        The app's default response class, so an endpoint added later is covered
        without anyone remembering to be. If anonymizing raises, so does this:
        the reader gets a 500 with no body, never the payload unmasked."""

        def render(self, content):
            if anon is not None:
                content = anon.payload(content)
            return super().render(content)

    def respond(obj, label=None):
        """A big body, anonymized and serialized on the request's worker thread:
        the default path would run both on the event loop, which a 1.8 MB panels
        payload holds for a quarter of a second. Off, the body goes back as is."""
        if anon is None:
            return obj
        return JSONResponse(anon.payload(jsonable_encoder(obj), label=label))

    def masked(payload, label=None):
        """For the exits that bypass the response class: websocket frames, the
        export stream, the gzipped trajectory body."""
        return payload if anon is None else anon.payload(payload, label=label)

    app = FastAPI(title="rc-dashboard", version="0.1.0",
                  default_response_class=AnonJSONResponse)
    app.state.cfg = cfg
    app.state.agg = agg
    # ws -> the window that client is viewing. A dict rather than a set because
    # the live frame is window-specific: one payload per DISTINCT window, not
    # one per client, so ten readers on the default window still cost one build.
    app.state.clients = {}

    async def broadcast(kind, payload):
        dead = []
        frame = json.dumps({"type": kind, "payload": masked(payload)}, default=str)
        for ws in list(app.state.clients):
            try:
                await ws.send_text(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            app.state.clients.pop(ws, None)

    async def broadcast_live():
        """A `live` frame per distinct window in use."""
        by_window = {}
        for ws, minutes in list(app.state.clients.items()):
            by_window.setdefault(minutes, []).append(ws)
        for minutes, sockets in by_window.items():
            payload = agg.live_payload(minutes)
            if anon is not None:
                # ~50 ms on a busy login node's frame: off the event loop
                payload = await asyncio.get_running_loop().run_in_executor(
                    None, masked, payload, "live")
            frame = json.dumps({"type": "live", "payload": payload}, default=str)
            for ws in sockets:
                try:
                    await ws.send_text(frame)
                except Exception:
                    app.state.clients.pop(ws, None)

    # ------------------------------------------------------------ background
    async def ingest_loop():
        interval = max(0.2, int(cfg.get("ingest.poll_ms", 1000)) / 1000.0)
        loop = asyncio.get_running_loop()
        # The backfill hands the tail every file at its end, then reads the
        # history behind that point newest first -- while this loop is already
        # polling, so the page is live from the first second and watches the past
        # fill in. A poll before the hand-off reads nothing (`agg.positioned`).
        fill = loop.run_in_executor(None, agg.backfill_now)
        filling = True
        while True:
            try:
                n = await loop.run_in_executor(None, agg.poll_once)
                if filling and fill.done():
                    filling = False
                    await broadcast("backfill", agg.backfill)
                    await broadcast_live()
                elif n or filling:
                    # While the backfill runs the bins move every cycle, new
                    # records or not.
                    await broadcast_live()
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def windows_loop():
        """Push build progress, and say when a window's panels are new.

        A rebuild can take minutes on a wide window, so the page shows a real
        percentage instead of an empty panel that looks like an empty feed.
        """
        last = None
        while True:
            await asyncio.sleep(2)
            try:
                st = agg.panels_by_window.status()
                # Change-detect on the MATERIAL fields only. `built_age_s`
                # advances every call by construction, so hashing the whole
                # payload would push a frame to every client every two seconds
                # forever -- a poll loop wearing a push loop's clothes.
                # (`drift_s` only moves when records actually land past the
                # window, but it is still left out: it would push a frame per
                # record on a busy feed, and the age readout carries it.)
                key = json.dumps(
                    [[w["minutes"], w.get("filter"), w["state"], w["progress"]["pct"],
                      w["built_at"], w["installed_at"], w["replay_dropped"], w["error"]]
                     for w in st["held"]]
                    + [st["queued"], st.get("queued_filtered")], default=str)
                if key != last:
                    last = key
                    await broadcast("windows", st)
            except Exception:
                pass

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
                await broadcast("rebuilt", {
                    "node_built_at": agg.node_built_at,
                    "windows": [w["minutes"] for w
                                in agg.panels_by_window.status()["held"]]})
            except Exception:
                pass

    @app.on_event("startup")
    async def _startup():
        agg.start()
        app.state.tasks = [asyncio.create_task(ingest_loop()),
                           asyncio.create_task(feeds_loop()),
                           asyncio.create_task(rebuild_loop()),
                           asyncio.create_task(windows_loop())]

    @app.on_event("shutdown")
    async def _shutdown():
        for t in getattr(app.state, "tasks", []):
            t.cancel()
        agg.stop()

    # ------------------------------------------------------------------- API
    @app.get("/api/config")
    def api_config():
        return cfg.as_dict(with_origins=True)

    @app.get("/api/feeds")
    def api_feeds():
        return agg.refresh_feeds()

    # Chat with an LLM: the agent this service would run, a question, a job.
    @app.get("/api/ask")
    def api_ask_info():
        return agg.ask_info()

    # `context` narrows a question to one panel or one session (Aggregator.ask);
    # the page's `user` / `agent_type` filter rides in the query string, as on
    # /api/panels, so a panel's question reads the view the reader is looking at.
    @app.post("/api/ask")
    async def api_ask(request: Request):
        body = await request.json()
        flt = view_filter(request, anon)
        ctx = body.get("context")
        if isinstance(ctx, dict) and ctx.get("session") is not None:
            ctx = dict(ctx, session=dealias_sid(ctx["session"], anon))
        try:
            return await asyncio.to_thread(
                agg.ask, str(body.get("question") or ""), body.get("window_min"),
                [h for h in (body.get("history") or []) if isinstance(h, dict)][-3:],
                ctx if isinstance(ctx, dict) else None, flt)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # The operator's suggestions marked done: kept by the service for everyone
    # until undone, and left out of the next "suggest actions" (ask.Asker). A
    # mark names a job and an index; the text is the service's own.
    @app.get("/api/suggestions/done")
    def api_done():
        return agg.asker.done_list()

    @app.post("/api/suggestions/done")
    async def api_mark_done(request: Request):
        body = await request.json()
        try:
            return agg.asker.mark_done(str(body.get("job") or ""), body.get("index"))
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/suggestions/done/{did}")
    def api_undo_done(did: str):
        try:
            return agg.asker.undo_done(did)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/ask/{jid}")
    def api_ask_job(jid: str):
        j = agg.ask_job(jid)
        if j is None:
            raise HTTPException(404, "no such question")
        return j

    @app.get("/api/collectors")
    def api_collectors():
        """Per host: is its collector OK, read from what it wrote (health.py)."""
        return agg.collectors()

    # `window_min` is optional everywhere: omitting it is the configured default
    # (`live.window_min`), never an error, so an existing bookmark or script that
    # predates the control keeps working and gets the same view it used to.
    @app.get("/api/windows")
    def api_windows():
        """What windows can be asked for, and what is built right now."""
        return dict(agg.panels_by_window.status(),
                    default_minutes=agg.window(None)["minutes"],
                    min_minutes=winmod.MIN_WINDOW_MIN)

    # `user` and `agent_type` (each repeatable, or comma-separated; `none` is
    # an untagged process) narrow every window panel and the trajectories to
    # those records. Each filter is a view of its own, built like a window; the
    # response's `filter` names it. The live tier is not narrowed (`/api/live`).
    @app.get("/api/panels")
    def api_panels(request: Request, window_min: int = Query(None)):
        return respond(agg.panels(window_min, view_filter(request, anon)), "panels")

    @app.get("/api/live")
    def api_live(window_min: int = Query(None)):
        return respond(agg.live_payload(window_min), "live")

    @app.get("/api/live/users")
    def api_live_users(window_min: int = Query(None), q: str = Query(None),
                       limit: int = Query(None, ge=0, le=5000),
                       running: str = Query(None)):
        """The user roster past the busiest rows the live frame carries;
        `running=agent:claude_code` keeps the accounts with such a session up."""
        if anon is None or not q:
            return respond(agg.live_users(window_min, q, limit, running))
        # The search matches what the reader sees -- aliases -- so a real name
        # finds nobody. Every account is asked for, then cut to `limit` here.
        out = agg.live_users(window_min, None, 5000, running)
        ql = q.lower()
        rows = [r for r in out["rows"] if ql in anon.user_alias(r["user"]).lower()]
        out["rows"] = rows[:(limit if limit is not None else agg.live.user_rows)]
        out["rows_total"] = len(rows)
        return respond(out)

    @app.get("/api/live/user")
    def api_live_user(name: str = Query(...), window_min: int = Query(None),
                      n: int = Query(25, ge=1, le=1000)):
        """One account opened up: sessions running now and its last `n` exited
        processes, however long ago."""
        if anon is not None:
            name = anon.dealias_user(name)
        return respond(redact.payload(agg.live_user(name, window_min, n)))

    @app.get("/api/trajectories")
    def api_traj(request: Request, rank: str = Query("recent"),
                 window_min: int = Query(None)):
        out = agg.trajectories(window_min, view_filter(request, anon))
        out["rank_by"] = rank if rank in out.get("ranks", {}) else "recent"
        return respond(out, "trajectories")

    @app.get("/api/trajectory")
    def api_trajectory(request: Request, sid: str = Query(...),
                       window_min: int = Query(None),
                       frm: float = Query(None, alias="from"),
                       to: float = Query(None),
                       order: str = Query("oldest")):
        """One ranked session as a process tree, re-read on demand (proctree.py).

        Sync on purpose: the read runs in the threadpool, never on the loop.
        `order` is `oldest` or `newest`: which end is listed first and drawn in
        full; anything else reads as `oldest`.

        Takes no user / agent-type filter, deliberately. The session is named by
        its `sid`, which the page only offers from the (filtered) ranking, and
        the tree is that session's whole ancestry: dropping the VS Code server
        that launched a filtered-to-Claude-Code session would cut the tree at
        its root rather than narrow it.
        """
        body = masked(redact.payload(proctree.serve(agg, dealias_sid(sid, anon), window_min,
                                                    frm, to, order)), "trajectory")
        raw, headers = proctree.response(body, request.headers.get("accept-encoding"))
        # One chunk: the body is already whole, this only lets it go out gzipped.
        return StreamingResponse(iter([raw]), media_type="application/json",
                                 headers=headers)

    @app.get("/api/health")
    def api_health():
        return {"ok": True, "records": agg.stats["records"],
                "last_poll": agg.stats["last_poll"],
                "backfill": agg.backfill,
                "node_built_at": agg.node_built_at,
                "windows": agg.panels_by_window.status(),
                # counts only: no name ever appears here
                "anonymize": anon.status() if anon is not None else {"enabled": False}}

    _roots = agg.roots
    iocmds = IoCommands(cfg, _roots)

    # Every command behind an I/O number (iocmds.py): a lookup the page starts
    # with the number's filters -- /api/events' own, plus `apid`, `host` and
    # `cwd` for a blast-radius row -- and polls. The window is the page's.
    @app.get("/api/io/commands")
    def api_io_commands(request: Request, window_min: int = Query(None),
                        apid: str = Query(None), host: str = Query(None),
                        cwd: str = Query(None)):
        f = parse_filters(dealias(query_lists(request), anon))
        f.pop("q", None)
        f["event"] = None                 # exits and truncations: iocmds.EVENTS
        if cwd and anon is not None:
            # a path the page showed carries aliases where names were
            cwd = "/".join(anon.back.get(s, s) for s in cwd.split("/"))
        minutes = agg.window(window_min)["minutes"]
        pids = [p for p in (apid or "").split(",") if p.strip().isdigit()]
        if pids and host:
            # the agents as the page drew them: their keys, from the view it shows
            flt = view_filter(request, anon)
            with agg.lock:
                view = agg.panels_by_window.peek(minutes, flt) or \
                    agg.panels_by_window.peek(minutes)
                pids = sorted(agent_keys(view, host, pids))
        j = iocmds.start(f, minutes, pids, host, cwd)
        return j.status()

    @app.get("/api/io/commands/{jid}")
    def api_io_commands_job(jid: str, offset: int = Query(0, ge=0),
                            limit: int = Query(200, ge=1, le=iocmd_page_max),
                            sort: str = Query("rd_mb"), dir: str = Query("desc")):
        j = iocmds.get(jid)
        if j is None:
            raise HTTPException(404, "no such lookup: it finished long enough ago to be dropped")
        out = j.status()
        if j.state == "done":
            out.update(rows=j.page(sort, dir != "asc", offset, limit),
                       offset=offset, limit=limit, sort=sort, dir="asc" if dir == "asc" else "desc")
        return respond(out)

    def search(pred, qv):
        """`q` over the command text the reader sees -- aliases, not names -- so
        a real name typed in finds nothing. A raw miss is a miss (masking only
        swaps names for aliases), unless the query could only match an alias or
        a mask; only those, and raw hits, pay for the rewrite."""
        aliasish = any(w in qv for w in ("user-", "lab-", "[email", "[uid")) \
            or re.fullmatch(r"[0-9a-f-]+", qv) is not None

        def ok(rec):
            if not pred(rec):
                return False
            hay = "%s %s" % (rec.get("_command") or "", redact.redact(rec.get("args") or ""))
            if not aliasish and qv not in hay.lower():
                return False
            return qv in anon.text(hay).lower()
        return ok

    def event_filters(request):
        """`/api/events`' and `/api/export`'s filters, and the `q` this module
        searches itself when names are masked (None otherwise)."""
        f = parse_filters(dealias(query_lists(request), anon))
        qv = f.pop("q", None) if anon is not None else None
        return f, qv

    @app.get("/api/events")
    def api_events(request: Request,
                   limit: int = Query(200, le=5000),
                   cursor: int = Query(0, ge=0),
                   order: str = Query("desc")):
        f, qv = event_filters(request)
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
        if qv:
            pred = search(pred, qv)
        rows, scanned, matched = [], 0, 0
        for rec, scanned, matched in scan(_roots(), norm, pred, limit=limit,
                                          offset=cursor, newest_first=newest):
            rows.append(redact.record(rec))
        rep = agg.reports["ebpf"]
        full = len(rows) >= limit
        return respond({"rows": rows,
                # None when the page did not fill: there is provably nothing after it
                "next_cursor": (cursor + len(rows)) if full else None,
                "cursor": cursor, "limit": limit,
                "order": "desc" if newest else "asc",
                "event_filter_defaulted": default_event,
                "returned": len(rows), "total_scanned": scanned,
                "truncated": full,
                "feed": feedmod.panel_meta(rep)})

    @app.get("/api/export")
    def api_export(request: Request, order: str = Query("desc")):
        f, qv = event_filters(request)
        # same default as /api/events, so "the download is the view" holds
        export_default_event = not f.get("event")
        if export_default_event:
            f["event"] = {"exit"}
        pred = make_predicate(f)
        if qv:
            pred = search(pred, qv)
        norm = Normalizer(cfg)
        cap = int(cfg.get("export.max_rows", 5000000))
        reports = agg.reports
        t0 = time.time()

        def gen():
            matched = 0
            body = []
            for rec, _scanned, matched in scan(_roots(), norm, pred, limit=cap,
                                               newest_first=order != "asc"):
                # the download is the view, credentials and names masked like it
                body.append(json.dumps(masked(redact.record(rec)), default=str))
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
            head = meta_header(dict(f, q=qv) if qv else f, reports, cfg, None, None, 0.0)
            head["_meta"]["rows"] = "see _trailer"
            yield json.dumps(masked(head), default=str) + "\n"
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
        minutes = agg.window(None)["minutes"]
        app.state.clients[websocket] = minutes
        try:
            await websocket.send_text(json.dumps(
                {"type": "live", "payload": masked(agg.live_payload(minutes))}, default=str))
            await websocket.send_text(json.dumps(
                {"type": "feeds", "payload": masked(agg.reports)}, default=str))
            await websocket.send_text(json.dumps(
                {"type": "windows", "payload": masked(agg.panels_by_window.status())},
                default=str))
            while True:
                msg = await websocket.receive_text()
                # The only client->server message: which window this reader is
                # looking at. Anything else is a keepalive ping and is ignored.
                try:
                    body = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(body, dict) or body.get("type") != "window":
                    continue
                want = agg.window(body.get("minutes"))["minutes"]
                if want == app.state.clients.get(websocket):
                    continue
                app.state.clients[websocket] = want
                # Answer immediately rather than waiting for the next poll: the
                # chart must not keep showing the previous window while the
                # picker already reads the new one.
                await websocket.send_text(json.dumps(
                    {"type": "live", "payload": masked(agg.live_payload(want))}, default=str))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            app.state.clients.pop(websocket, None)

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
