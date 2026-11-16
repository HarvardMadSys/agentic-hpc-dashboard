"""JSON out of the service: serialized with orjson, on the request's worker thread, and
gzipped when the reader takes it.

FastAPI turns a dict an endpoint returns into JSON in two passes, both inside the
request's coroutine and so on the event loop: `jsonable_encoder`, a pure-Python
walk of every value, then `json.dumps`. On the panels payload (~1.8 MB on a busy
node) that held the loop for a quarter of a second or more per request, and every
other reader, the static files and the websocket frames waited behind it. A
`Response` an endpoint builds itself skips both, so `app.respond` builds one
here, where the sync endpoint already runs: on the threadpool.

The bytes mean what the old ones meant. orjson writes the same JSON as
`json.dumps` after `jsonable_encoder` for everything a payload holds -- dicts
(subclasses included), lists, tuples, strings, numbers, booleans, None, and keys
that are numbers or None (`OPT_NON_STR_KEYS` spells them as `json.dumps` does);
sets and deques become lists in their own order, as `jsonable_encoder` makes
them. A float may be spelled differently (`1e16` for `1e+16`) and parses to the
same number. Anything else -- a type orjson does not know, an integer wider than
64 bits -- goes the old way, `json.dumps(jsonable_encoder(obj))`, so no payload
is served differently, only faster. One case cannot be kept: a NaN or an
infinity, which `JSONResponse` refused (`allow_nan=False`, a 500), is served as
null.

Compression: a body of `GZIP_MIN` bytes or more goes out gzipped when the
request accepts it, compressed here on the worker thread (zlib releases the GIL)
rather than by a middleware on the event loop. JSON shrinks five to ten times,
which over an SSH tunnel is most of a panels read.
"""
import collections
import gzip
import json

import orjson
from fastapi.encoders import jsonable_encoder
from starlette.responses import Response

GZIP_MIN = 1024
GZIP_LEVEL = 5
_OPTS = orjson.OPT_NON_STR_KEYS


def _default(o):
    if isinstance(o, (set, frozenset, collections.deque)):
        return list(o)
    raise TypeError                      # anything else: the old encoder decides


def _compat(obj):
    """What FastAPI's `JSONResponse` would have written."""
    return json.dumps(jsonable_encoder(obj), ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":")).encode("utf-8")


def dumps(obj):
    """`obj` as JSON bytes."""
    try:
        return orjson.dumps(obj, default=_default, option=_OPTS)
    except TypeError:
        return _compat(obj)


def dumps_str(obj):
    """`obj` as JSON bytes as `json.dumps(obj, default=str)` writes them: anything
    JSON has no type for becomes its `str()`. The websocket frames and the
    inspector's body were written that way, and still are."""
    try:
        # dates and dataclasses to `str` too, as json.dumps does; orjson would spell them itself
        return orjson.dumps(obj, default=str, option=_OPTS | orjson.OPT_PASSTHROUGH_DATETIME
                            | orjson.OPT_PASSTHROUGH_DATACLASS)
    except TypeError:
        return json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8")


def frame(kind, payload):
    """A websocket frame, as text: `{"type": kind, "payload": payload}`."""
    return dumps_str({"type": kind, "payload": payload}).decode("utf-8")


def accepts_gzip(request):
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    ae = headers.get("accept-encoding") or ""
    return any(p.split(";")[0].strip().lower() == "gzip" for p in ae.split(","))


def response(obj, request=None, status_code=200, headers=None, encode=dumps):
    """A JSON `Response` for `obj`, serialized (and gzipped) on the calling thread."""
    body = encode(obj)
    hdrs = dict(headers or {})
    if len(body) >= GZIP_MIN and accepts_gzip(request):
        body = gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)
        hdrs["content-encoding"] = "gzip"
        hdrs["vary"] = "Accept-Encoding"
    return Response(content=body, status_code=status_code, headers=hdrs,
                    media_type="application/json")


class JSONOut(Response):
    """The app's default response class: what FastAPI hands it (an error body, a
    route that returns a plain dict) is written with orjson, as `dumps` writes."""

    media_type = "application/json"

    def render(self, content):
        return dumps(content)
