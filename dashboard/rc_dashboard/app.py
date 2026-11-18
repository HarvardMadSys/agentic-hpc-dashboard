"""The service: REST + WebSocket push + the built frontend.

The ingest loop, the feed refresh and the historical rebuild are background
tasks, so a slow read never blocks a request.  WebSocket clients get a frame when
something actually changed -- the page must not animate between reads, because
the retired viewer did exactly that with fabricated data.
"""
import asyncio
import collections
import gzip
import json
import os
import re
import sys
import threading
import time

from fastapi import (FastAPI, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import Response

from . import annotate as annotatemod
from . import auth as authmod
from . import history as historymod
from . import feeds as feedmod
from . import gcwatch
from . import jsonout
from . import keysearch
from . import proctree
from . import redact
from . import roles, tool_buckets
from . import windows as winmod
from .aggregator import Aggregator
from .export import make_predicate, meta_header, parse_filters, scan
from .iocmds import PAGE_MAX as iocmd_page_max, IoCommands, agent_keys
from .normalize import Normalizer
from .reducers import phase_names, phase_rules


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


# The per-record memos (each a pure function of its text, memoized up to a
# length and a count): what /api/health says of them, so their bounds can be
# checked against a site's real command lines.
MEMOS = (("redact", lambda: redact._redact_memo), ("role_of", lambda: roles._role_memo),
         ("tagset", lambda: phase_names.tagset), ("tool_bucket", lambda: tool_buckets.bucket),
         ("category_of", lambda: phase_rules.category_of),
         ("category_of_argv", lambda: phase_rules.category_of_argv))


def memo_status():
    """For /api/health: each memo's hits, misses and size against its bound."""
    out = {}
    for name, get in MEMOS:
        ci = get().cache_info()
        out[name] = {"hits": ci.hits, "misses": ci.misses, "size": ci.currsize,
                     "max": ci.maxsize}
    return out


# The push to the page (`/ws`). Each reader has a sender of its own, with a
# mailbox holding the newest frame of each kind (`Reader`): whoever makes a frame
# -- the ingest loop, the builds' progress, the feed refresh -- posts it and goes
# on. They sent it to each reader in turn, so one reader that stopped reading
# held them all, the ingest loop with them: nothing was read for as long as it
# stayed. A reader that has not taken a frame WS_SEND_TIMEOUT_S after it was
# sent is dropped, and no more than WS_MAX_CLIENTS are served at once.
WS_SEND_TIMEOUT_S = 5.0
WS_MAX_CLIENTS = 64
# `/api/health` says `ok` false, and why (`api_health`): once the tail has not
# moved -- no poll ended, no batch handed over -- for STALE_POLLS poll intervals,
# and STALE_MIN_S at least; once a background loop's last LOOP_FAILS rounds all
# raised; once a view could not take a record in FEED_FAILS of the tail's
# batches in a row.
STALE_POLLS = 5
STALE_MIN_S = 5.0
LOOP_FAILS = 3
FEED_FAILS = 3


class Reader:
    """One websocket reader: the frames waiting for it, the newest of each kind,
    and the task that sends them (`run`). A newer frame of a kind replaces one
    still waiting, so a reader that falls behind skips to the newest and nothing
    queues without bound; frames go out in the order their kinds were last
    posted."""

    def __init__(self, ws, minutes):
        self.ws = ws
        self.minutes = minutes          # the window it is viewing
        self.frames = {}                # kind -> text
        self.wake = asyncio.Event()
        self.task = None
        self.slow = False               # dropped for taking no frame in time
        self.gone = None                # why it was dropped, once it is

    def post(self, kind, frame):
        if self.gone is None:
            self.frames.pop(kind, None)
            self.frames[kind] = frame
            self.wake.set()

    async def run(self, timeout, drop):
        """Send what is posted, a frame at a time; `drop(self, why)` once one
        takes longer than `timeout` s to go out, or the socket fails."""
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                while self.frames:
                    kind = next(iter(self.frames))
                    await asyncio.wait_for(self.ws.send_text(self.frames.pop(kind)), timeout)
        except asyncio.TimeoutError:
            self.slow = True
            drop(self, "took over %g s to take a frame" % timeout)
        except Exception as e:
            drop(self, "%s: %s" % (type(e).__name__, e))


class CachedStatic(StaticFiles):
    """The built page, with the caching its file names allow.

    vite names every asset by its content hash (`assets/index-<hash>.js`), so a
    browser may keep one for a year and never ask again; `index.html` names the
    assets, so it is asked for again on every load (`no-cache` is "revalidate",
    and the ETag makes that a 304). A JavaScript or CSS asset also goes out
    gzipped to a browser that takes it: compressed once per file and kept, so
    the first load over an SSH tunnel moves a third of the bytes.

    Starlette calls `file_response` on the event loop, so nothing is compressed
    there: `warm()` compresses every asset on a worker thread at start, and an
    asset not compressed yet (one that changed since) goes out as it is while a
    thread compresses it for the next reader."""

    GZIP_TYPES = (".js", ".css", ".svg", ".json", ".map")
    _gz = {}                            # path -> ((mtime_ns, size), gzipped bytes)
    _gz_pending = set()                 # paths a thread is compressing now
    _gz_lock = threading.Lock()

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        path = scope.get("path") or ""
        if "/assets/" in path:
            resp.headers["cache-control"] = "public, max-age=31536000, immutable"
            if resp.status_code == 200 and str(full_path).endswith(self.GZIP_TYPES):
                resp.headers["vary"] = "Accept-Encoding"
                if jsonout.gzip_ok(Headers(scope=scope).get("accept-encoding")):
                    body = self._gzipped(str(full_path), stat_result)
                    if body is not None:
                        hdrs = {k: v for k, v in resp.headers.items()
                                if k not in ("content-length", "content-encoding")}
                        hdrs["content-encoding"] = "gzip"
                        return Response(body, status_code=200, headers=hdrs)
        elif path.endswith(".html") or path.endswith("/") or "." not in path.rsplit("/", 1)[-1]:
            resp.headers["cache-control"] = "no-cache"
        return resp

    def _gzipped(self, path, st):
        """The file's gzipped bytes if they are kept for this version of it; if
        not, None -- the reader gets the file as it is -- and a thread starts
        compressing it."""
        hit = self._gz.get(path)
        if hit is not None and hit[0] == (st.st_mtime_ns, st.st_size):
            return hit[1]
        with self._gz_lock:
            if path in self._gz_pending:
                return None
            self._gz_pending.add(path)
        threading.Thread(target=self._compress, args=(path,), name="static-gzip",
                         daemon=True).start()
        return None

    def _compress(self, path):
        try:
            with open(path, "rb") as fh:
                before = os.fstat(fh.fileno())
                data = fh.read()
                after = os.fstat(fh.fileno())
            ident = (after.st_mtime_ns, after.st_size)
            # written to while read: leave it to the next reader
            if (before.st_mtime_ns, before.st_size) == ident and len(data) == after.st_size:
                body = gzip.compress(data, compresslevel=9, mtime=0)
                with self._gz_lock:
                    self._gz[path] = (ident, body)
        except OSError:
            pass
        finally:
            with self._gz_lock:
                self._gz_pending.discard(path)

    def warm(self):
        """Compress every asset now, on the calling thread (a worker at start).
        Returns how many."""
        try:
            root = os.path.join(self.directory, "assets")
            names = sorted(os.listdir(root))
        except (OSError, TypeError):
            return 0
        n = 0
        for name in names:
            p = os.path.join(root, name)
            if name.endswith(self.GZIP_TYPES) and os.path.isfile(p):
                self._compress(p)
                n += 1
        return n


def create_app(cfg):
    # First, so a malformed password hash stops the service before it reads anything.
    gate = authmod.from_config(cfg)
    gcwatch.install()
    # The event loop gives up the GIL at every read and write and waits to get
    # it back; beside a build or the backfill it waited the whole interval.
    switch = float(cfg.get("server.gil_switch_ms", 1.0) or 0)
    if switch > 0:
        sys.setswitchinterval(switch / 1000.0)
    agg = Aggregator(cfg)
    # The sealed day-files' Parquet tables (history.py): made in the background,
    # from the start, and taken by the readers file by file as each is ready.
    store = agg.history = historymod.HistoryStore(cfg)
    anon = agg.anon

    class AnonJSONResponse(jsonout.JSONOut):
        """Every JSON body, with its names aliased when `anonymize.enabled` is on.

        The app's default response class, so an endpoint added later is covered
        without anyone remembering to be. If anonymizing raises, so does this:
        the reader gets a 500 with no body, never the payload unmasked."""

        def render(self, content):
            if anon is not None:
                content = anon.payload(content)
            return super().render(content)

    def respond(obj, label=None, request=None):
        """A body anonymized (when names are masked), serialized with orjson and
        gzipped for a reader that takes it -- all on the request's worker thread.
        Returned as a plain dict, FastAPI would encode and serialize it on the
        event loop, which a 1.8 MB panels payload held for a quarter of a second
        while every other request and the websocket waited (`jsonout`)."""
        if anon is not None:
            obj = anon.payload(obj, label=label)
        return jsonout.response(obj, request)

    # The redacted copy of each inspector tree served lately, by the tree object
    # itself: `proctree.serve` hands back its cached tree on a hit, and redacting
    # a few MB of command lines again for every open is most of what a hit costs.
    # A few only: each is as big as the tree.
    trees = collections.OrderedDict()
    trees_lock = threading.Lock()

    def redacted(body):
        """`redact.payload(body)`, with the tree's copy reused while it is the same tree."""
        out = {}
        for k, v in body.items():
            if k == "tree" and isinstance(v, (dict, list)):
                with trees_lock:
                    hit = trees.get(id(v))
                if hit is not None and hit[0] is v:
                    red = hit[1]
                else:
                    red = redact.payload(v)
                    with trees_lock:
                        trees[id(v)] = (v, red)
                        while len(trees) > 4:
                            trees.popitem(last=False)
                out[k] = red
            else:
                out[k] = redact._text(v) if k in redact.TEXT_KEYS else redact.payload(v)
        return out

    def masked(payload, label=None):
        """For the exits that bypass the response class: websocket frames, the
        export stream, the gzipped trajectory body."""
        return payload if anon is None else anon.payload(payload, label=label)

    app = FastAPI(title="rc-dashboard", version="0.1.0",
                  default_response_class=AnonJSONResponse)
    app.state.cfg = cfg
    app.state.agg = agg
    app.state.auth = gate
    # ws -> its `Reader`, which says the window that reader is viewing: the live
    # frame is window-specific, one payload per DISTINCT window, not one per
    # client, so ten readers on the default window still cost one build.
    app.state.clients = {}
    # readers dropped for taking no frame in time, and turned away at the cap
    ws_stats = app.state.ws_stats = {"dropped": 0, "refused": 0, "last_drop": None}
    closing = set()                 # the dropped readers' closes, held while they run

    def frame_of(kind, payload):
        return jsonout.frame(kind, masked(payload))

    def drop(reader, why):
        """`reader` takes no more frames (`Reader.run`): it is let go, and its
        handler ends at its next message. One that took no frame in time is
        told to close, in the background and for as long as a frame may take:
        a socket that does not drain may never take the close either."""
        reader.gone = why
        reader.frames.clear()
        if app.state.clients.get(reader.ws) is reader:
            del app.state.clients[reader.ws]
        if reader.slow:
            ws_stats["dropped"] += 1
            ws_stats["last_drop"] = {"at": time.time(), "why": why}
            task = asyncio.ensure_future(close_slow(reader.ws))
            closing.add(task)
            task.add_done_callback(closing.discard)

    async def close_slow(ws):
        try:
            await asyncio.wait_for(ws.close(code=1008, reason="too slow"), WS_SEND_TIMEOUT_S)
        except Exception:
            pass

    async def broadcast(kind, payload):
        """Every reader gets the frame: masked and serialized off the event loop,
        and posted to each reader's own sender (`Reader`), never sent from here."""
        frame = await asyncio.get_running_loop().run_in_executor(None, frame_of, kind, payload)
        for reader in list(app.state.clients.values()):
            reader.post(kind, frame)

    async def broadcast_live():
        """A `live` frame per distinct window in use, posted to its readers.

        Made in the executor (`Aggregator.live_frame`): taking the live tier needs
        the aggregator's lock, and a build, a hold of the tail or a re-rank can
        keep it for seconds. On the event loop that wait stalled every request,
        every static file and every other socket with it."""
        by_window = {}
        for reader in list(app.state.clients.values()):
            by_window.setdefault(reader.minutes, []).append(reader)
        loop = asyncio.get_running_loop()
        for minutes, readers in by_window.items():
            frame = await loop.run_in_executor(None, agg.live_frame, minutes)
            for reader in readers:
                if reader.minutes == minutes:   # not one that moved on meanwhile
                    reader.post("live", frame)

    # ------------------------------------------------------------ background
    def poll():
        return agg.poll_once()

    interval = max(0.2, int(cfg.get("ingest.poll_ms", 1000)) / 1000.0)
    # Each background loop's last error, and how many, for /api/health: kept and
    # logged, never passed over. A loop whose every round raised went unseen,
    # and /api/health said nothing was wrong.
    # `streak` is its rounds in a row that raised, which a round that does not
    # ends: `ok` goes false at LOOP_FAILS.
    loops = app.state.loops = {name: {"started": None, "errors": 0, "error": None,
                                      "error_at": None, "streak": 0}
                               for name in ("ingest", "windows", "feeds", "rebuild")}
    logged = {}

    def failed(name, e):
        """`e` ended a round of loop `name`: kept, and logged when it changes and
        once a minute while it repeats."""
        now = time.time()
        st = loops[name]
        msg = "%s: %s" % (type(e).__name__, e)
        st["errors"] += 1
        st["streak"] += 1
        if msg != st["error"] or now - logged.get(name, 0.0) >= 60:
            print("%s loop: %s" % (name, msg), file=sys.stderr, flush=True)
            logged[name] = now
        st["error"], st["error_at"] = msg, now

    def went(name):
        """A round of loop `name` ended without an error."""
        loops[name]["streak"] = 0

    async def ingest_loop():
        loop = asyncio.get_running_loop()
        loops["ingest"]["started"] = time.time()
        # The backfill hands the tail every file at its end, then reads the
        # history behind that point newest first -- while this loop is already
        # polling, so the page is live from the first second and watches the past
        # fill in. A poll before the hand-off reads nothing (`agg.positioned`).
        fill = loop.run_in_executor(None, agg.backfill_now)
        filling = True
        while True:
            try:
                n = await loop.run_in_executor(None, poll)
                if filling and fill.done():
                    filling = False
                    await broadcast("backfill", agg.backfill)
                    await broadcast_live()
                    # The live tier holds its history now: what it took on stays,
                    # so the collector stops walking it (`gcwatch`).
                    await loop.run_in_executor(None, gcwatch.settle)
                elif n or filling:
                    # While the backfill runs the bins move every cycle, new
                    # records or not.
                    await broadcast_live()
                went("ingest")
            except Exception as e:
                failed("ingest", e)
            await asyncio.sleep(interval)

    async def windows_loop():
        """Push build progress, and say when a window's panels are new.

        A rebuild can take minutes on a wide window, so the page shows a real
        percentage instead of an empty panel that looks like an empty feed.
        """
        last = None
        installed = None
        loop = asyncio.get_running_loop()
        loops["windows"]["started"] = time.time()
        while True:
            await asyncio.sleep(2)
            try:
                # off the loop: status() takes the aggregator's lock
                st = await loop.run_in_executor(None, agg.panels_by_window.status)
                # A window's fresh reducers went in: most of what the heap will keep
                # for the next hours is new, so freeze it (`gcwatch`).
                now_in = sorted((w.get("installed_at") or 0) for w in st["held"])
                if installed is not None and now_in != installed and any(now_in):
                    await loop.run_in_executor(None, gcwatch.settle)
                installed = now_in
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
                went("windows")
            except Exception as e:
                failed("windows", e)

    async def feeds_loop():
        loop = asyncio.get_running_loop()
        loops["feeds"]["started"] = time.time()
        while True:
            await asyncio.sleep(30)
            try:
                await loop.run_in_executor(None, agg.refresh_feeds)
                await broadcast("feeds", agg.reports)
                went("feeds")
            except Exception as e:
                failed("feeds", e)

    async def rebuild_loop():
        interval = max(60, int(cfg.get("rebuild.interval_s", 900)))
        loop = asyncio.get_running_loop()
        loops["rebuild"]["started"] = time.time()
        while True:
            await asyncio.sleep(interval)
            try:
                await loop.run_in_executor(None, agg.rebuild_history)
                # also the hourly full collection that bounds what freezing keeps
                await loop.run_in_executor(None, gcwatch.settle)
                st = await loop.run_in_executor(None, agg.panels_by_window.status)
                await broadcast("rebuilt", {
                    "node_built_at": agg.node_built_at,
                    "windows": [w["minutes"] for w in st["held"]]})
                went("rebuild")
            except Exception as e:
                failed("rebuild", e)

    @app.on_event("startup")
    async def _startup():
        store.start()                   # nothing waits for it (history.py)
        agg.start()
        app.state.tasks = [asyncio.create_task(ingest_loop()),
                           asyncio.create_task(feeds_loop()),
                           asyncio.create_task(rebuild_loop()),
                           asyncio.create_task(windows_loop())]
        web = getattr(app.state, "static", None)
        if web is not None:
            # every asset gzipped before a page asks for it, off the event loop
            asyncio.get_running_loop().run_in_executor(None, web.warm)
        # uvicorn binds the port only once this returns, so waiting here keeps
        # a reader from ever landing on a window still being merged; the
        # loops above run meanwhile (`panels.preprocess_wait_s`).
        wait = float(cfg.get("panels.preprocess_wait_s") or 0)
        if cfg.get("panels.preprocess") and wait > 0:
            t0 = time.time()
            done = await asyncio.get_running_loop().run_in_executor(
                None, agg.wait_preprocessed, wait)
            print("preprocess: %s in %.1f s; listening" % (
                "every window built" if done else
                "not finished, the builds carry on", time.time() - t0), flush=True)

    @app.on_event("shutdown")
    async def _shutdown():
        for t in getattr(app.state, "tasks", []):
            t.cancel()
        agg.stop()
        store.stop()
        keysearch.shutdown()            # the inspector's workers (keysearch.py)

    # ------------------------------------------------------------------- API
    @app.get("/api/config")
    def api_config(request: Request):
        return respond(cfg.as_dict(with_origins=True), request=request)

    @app.get("/api/feeds")
    def api_feeds(request: Request):
        return respond(agg.refresh_feeds(), request=request)

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

    # What each tool call of a session was for, in a model's words (annotate.py).
    # The calls are the service's own read of the session -- the tree the page
    # was served, masked as it was -- never command lines the page posts, so
    # the endpoint cannot be used to send a model anything else.
    annotator = app.state.annotator = annotatemod.Annotator(cfg)

    @app.get("/api/annotate")
    def api_annotate_info():
        return annotatemod.describe(cfg)

    @app.post("/api/annotate")
    async def api_annotate(request: Request):
        """`{sid, window_min, from, to, order, run}`: the session's tool calls,
        what is already known of them, and -- with `run` -- a job asking about
        the rest. Calls another request is already asking about are answered by
        its job: `job` is that one when this request started none, and `also`
        names any others, so every reader of the session gets the answers. The body is the tree's own text, masked once: it is sent as
        it is, so the page matches each call to its row exactly."""
        body = await request.json()
        sid = body.get("sid")
        if not isinstance(sid, str) or not sid:
            raise HTTPException(400, "name a session (sid)")

        def num(k):
            v = body.get(k)
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        def work():
            info = annotatemod.describe(cfg)
            tree = masked(redacted(proctree.serve(
                agg, dealias_sid(sid, anon), num("window_min"), num("from"), num("to"),
                "newest" if body.get("order") == "newest" else "oldest")), "annotate")
            calls, total = annotatemod.calls_in(tree, info["max_calls"])
            known = annotator.known([c["cmd"] for c in calls]) if info["enabled"] else {}
            job = None
            if body.get("run") and info["enabled"]:
                row = tree.get("row") if isinstance(tree.get("row"), dict) else {}
                job = annotator.submit(calls, row.get("agent_type"), about=sid)
            # calls another request is asking about already: their jobs, to follow
            also = annotator.following(calls, job["id"] if job else None) \
                if info["enabled"] else []
            if job is None and also:
                job = annotator.view(also.pop(0))
            # the calls themselves, so the page keeps only their answers out of
            # a followed job that another session started
            return {"info": info, "calls": len(calls), "total": total,
                    "cmds": [c["cmd"] for c in calls],
                    "known": known, "job": job, "also": also}

        try:
            out = await asyncio.to_thread(work)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return jsonout.response(out, request)

    @app.get("/api/annotate/{jid}")
    def api_annotate_job(jid: str, request: Request):
        j = annotator.view(jid)
        if j is None:
            raise HTTPException(404, "no such annotation job")
        return jsonout.response(j, request)

    @app.get("/api/collectors")
    def api_collectors(request: Request):
        """Per host: is its collector OK, read from what it wrote (health.py)."""
        return respond(agg.collectors(), request=request)

    # `window_min` is optional everywhere: omitting it is the configured default
    # (`live.window_min`), never an error, so an existing bookmark or script that
    # predates the control keeps working and gets the same view it used to.
    @app.get("/api/windows")
    def api_windows(request: Request):
        """What windows can be asked for, and what is built right now."""
        return respond(dict(agg.panels_by_window.status(),
                            default_minutes=agg.window(None)["minutes"],
                            min_minutes=winmod.MIN_WINDOW_MIN), request=request)

    # `user` and `agent_type` (each repeatable, or comma-separated; `none` is
    # an untagged process) narrow every window panel and the trajectories to
    # those records. Each filter is a view of its own, built like a window; the
    # response's `filter` names it. The live tier is not narrowed (`/api/live`).
    @app.get("/api/panels")
    def api_panels(request: Request, window_min: int = Query(None)):
        return respond(agg.panels(window_min, view_filter(request, anon)), "panels", request)

    @app.get("/api/live")
    def api_live(request: Request, window_min: int = Query(None)):
        return respond(agg.live_payload(window_min), "live", request)

    @app.get("/api/live/users")
    def api_live_users(request: Request, window_min: int = Query(None), q: str = Query(None),
                       limit: int = Query(None, ge=0, le=5000),
                       running: str = Query(None)):
        """The user roster past the busiest rows the live frame carries;
        `running=agent:claude_code` keeps the accounts with such a session up."""
        if anon is None or not q:
            return respond(agg.live_users(window_min, q, limit, running), request=request)
        # The search matches what the reader sees -- aliases -- so a real name
        # finds nobody. Every account is asked for, then cut to `limit` here.
        out = agg.live_users(window_min, None, 5000, running)
        ql = q.lower()
        rows = [r for r in out["rows"] if ql in anon.user_alias(r["user"]).lower()]
        out["rows"] = rows[:(limit if limit is not None else agg.live.user_rows)]
        out["rows_total"] = len(rows)
        return respond(out, request=request)

    @app.get("/api/live/user")
    def api_live_user(request: Request, name: str = Query(...), window_min: int = Query(None),
                      n: int = Query(25, ge=1, le=1000)):
        """One account opened up: sessions running now and its last `n` exited
        processes, however long ago."""
        if anon is not None:
            name = anon.dealias_user(name)
        return respond(redact.payload(agg.live_user(name, window_min, n)), request=request)

    @app.get("/api/users")
    def api_users(request: Request, window_min: int = Query(None)):
        """Every user with an agent session in the window, and how each works
        with their agents (portraits.py); narrowed by `user` / `agent_type` as
        the trajectories are."""
        return respond(redact.payload(agg.users(window_min, view_filter(request, anon))),
                       "users", request)

    @app.get("/api/user")
    def api_user(request: Request, name: str = Query(...), window_min: int = Query(None)):
        """One user's portrait: their sessions, rhythm, work, tasks and traits.
        `name` is the alias the page shows when names are anonymized; a real
        name then finds nobody, as on /api/live/user."""
        if anon is not None:
            name = anon.dealias_user(name)
        return respond(redact.payload(agg.user(name, window_min, view_filter(request, anon))),
                       "user", request)

    @app.get("/api/trajectories")
    def api_traj(request: Request, rank: str = Query("recent"),
                 window_min: int = Query(None)):
        out = agg.trajectories(window_min, view_filter(request, anon))
        out["rank_by"] = rank if rank in out.get("ranks", {}) else "recent"
        return respond(out, "trajectories", request)

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
        body = masked(redacted(proctree.serve(agg, dealias_sid(sid, anon), window_min,
                                              frm, to, order)), "trajectory")
        # serialized as it always was (`default=str`), with orjson, here on the worker
        return jsonout.response(body, request, headers={"vary": "Accept-Encoding"},
                                encode=jsonout.dumps_str)

    def ingest_health(now):
        """Whether the tail is moving: `ok` false once no poll has ended and no
        batch been handed over (`polled_at`) for STALE_POLLS poll intervals, and
        STALE_MIN_S at least -- a poll that hangs, or raises every time. It said
        `ok` with a `last_poll` two minutes old."""
        stale = max(STALE_MIN_S, STALE_POLLS * interval)
        at = agg.stats.get("polled_at") or loops["ingest"]["started"] or now
        age = max(0.0, now - at)
        return {"ok": age <= stale, "age_s": round(age, 1), "stale_after_s": round(stale, 1),
                "feed_errors": agg.stats["feed_errors"],
                "feed_error_streak": agg.stats["feed_error_streak"],
                "feed_error": agg.stats["feed_error"]}

    def unwell(ingest):
        """Why the service is not well, or nothing (`api_health`)."""
        why = []
        if not ingest["ok"]:
            why.append("the tail has not moved for %.0f s" % ingest["age_s"])
        for name, st in loops.items():
            if st["streak"] >= LOOP_FAILS:
                why.append("the %s loop failed its last %d rounds: %s"
                           % (name, st["streak"], st["error"]))
        if ingest["feed_error_streak"] >= FEED_FAILS:
            why.append("a view could not take a record in %d of the tail's batches in a "
                       "row: %s" % (ingest["feed_error_streak"], ingest["feed_error"]))
        return why

    @app.get("/api/health")
    def api_health(request: Request):
        """`ok` false, and `why`, when the service is not doing its work:

        - the tail has not moved -- no poll ended, no batch handed over -- for
          STALE_POLLS poll intervals, and STALE_MIN_S at least (5 s by default);
        - a background loop's last LOOP_FAILS (3) rounds all raised: the ingest
          loop's after 3 polls, the windows loop's after 6 s, the feeds loop's
          after 90 s, the rebuild loop's after 3 rebuild intervals;
        - a view could not take a record in FEED_FAILS (3) of the tail's batches
          in a row.

        One error alone flips nothing: a round that goes through ends a loop's
        streak, and a batch every view took ends the views' (each is reported,
        in `loops` and `ingest`, all the same). It said `ok` whatever the loops
        and the views met."""
        now = time.time()
        ingest = ingest_health(now)
        why = unwell(ingest)
        return respond({"ok": not why, "why": why, "ingest": ingest,
                        # each background loop's last error, and how many
                        "loops": {k: dict(v) for k, v in loops.items()},
                        "websocket": dict(ws_stats, readers=len(app.state.clients),
                                          max=WS_MAX_CLIENTS,
                                          send_timeout_s=WS_SEND_TIMEOUT_S),
                        "records": agg.stats["records"],
                        "last_poll": agg.stats["last_poll"],
                        "backfill": agg.backfill,
                        "node_built_at": agg.node_built_at,
                        "windows": agg.panels_by_window.status(),
                        # counts only: no name ever appears here
                        "anonymize": anon.status() if anon is not None else {"enabled": False},
                        # how long the garbage collector held the process (`gcwatch`)
                        "gc": gcwatch.status(),
                        # the per-record memos against their bounds (`memo_status`)
                        "memo": memo_status()},
                       request=request)

    _roots = agg.roots
    # which twin of a stream written in both formats the event views read
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
        return respond(j.status(), request=request)

    @app.get("/api/io/commands/{jid}")
    def api_io_commands_job(request: Request, jid: str, offset: int = Query(0, ge=0),
                            limit: int = Query(200, ge=1, le=iocmd_page_max),
                            sort: str = Query("rd_mb"), dir: str = Query("desc")):
        j = iocmds.get(jid)
        if j is None:
            raise HTTPException(404, "no such lookup: it finished long enough ago to be dropped")
        out = j.status()
        if j.state == "done":
            out.update(rows=j.page(sort, dir != "asc", offset, limit),
                       offset=offset, limit=limit, sort=sort, dir="asc" if dir == "asc" else "desc")
        return respond(out, request=request)

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
                "feed": feedmod.panel_meta(rep)}, request=request)

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

    def first_frames(minutes):
        """What a reader gets on connecting, made off the event loop."""
        return (agg.live_frame(minutes), frame_of("feeds", dict(agg.reports)),
                frame_of("windows", agg.panels_by_window.status()))

    async def full(websocket):
        """Turn a reader away at WS_MAX_CLIENTS (1013, try again later): the page
        retries, then reads by polling (`useLive`)."""
        if len(app.state.clients) < WS_MAX_CLIENTS:
            return False
        ws_stats["refused"] += 1
        await websocket.close(code=1013, reason="too many readers")
        return True

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        if await full(websocket):
            return
        minutes = agg.window(None)["minutes"]
        loop = asyncio.get_running_loop()
        reader = None
        try:
            first = await loop.run_in_executor(None, first_frames, minutes)
            if await full(websocket):
                return
            # Its own sender sends every frame it gets, these first, `live` first.
            reader = Reader(websocket, minutes)
            for kind, frame in zip(("live", "feeds", "windows"), first):
                reader.post(kind, frame)
            app.state.clients[websocket] = reader
            reader.task = loop.create_task(reader.run(WS_SEND_TIMEOUT_S, drop))
            while reader.gone is None:
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
                if want == reader.minutes:
                    continue
                reader.minutes = want
                # Answer immediately rather than waiting for the next poll: the
                # chart must not keep showing the previous window while the
                # picker already reads the new one.
                frame = await loop.run_in_executor(None, agg.live_frame, want)
                if reader.minutes == want:
                    reader.post("live", frame)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if reader is not None:
                if app.state.clients.get(websocket) is reader:
                    del app.state.clients[websocket]
                if reader.task is not None:
                    reader.task.cancel()

    # --------------------------------------------------------------- statics
    static = cfg.get("server.static_dir")
    if static and os.path.isdir(static):
        app.state.static = CachedStatic(directory=static, html=True)
        app.mount("/", app.state.static, name="web")
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

    # -------------------------------------------------------------- sign-in
    # Around every route above and the static mount, so a route added later is
    # covered without anyone remembering to be (auth.py).
    if gate is not None:
        app.add_middleware(authmod.AuthMiddleware, gate=gate,
                           site=str(cfg.get("site.name") or ""))
    return app
