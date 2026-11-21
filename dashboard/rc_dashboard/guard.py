"""Who may talk to the service at all: the Origin of a websocket, and while
the service has no password, the Host of every request.

The service listens on 127.0.0.1, and a browser is a way in to that from
anywhere. A page on another site may open a websocket to 127.0.0.1:<port>
(websockets are not held to the same-origin rule) and read every frame. And a
page whose own site name its author points at 127.0.0.1 in DNS ("DNS
rebinding") is, to the browser, on the dashboard's origin: it reads every
route. A password stops the second, since the session cookie is sent only
under the dashboard's own name, but not the first from a page on another port
of the same host -- a dev server, a notebook, another tunnel: the cookie is
SameSite=Lax, and a browser counts every port of a host as one site. These
checks cover the rest:

* **A websocket's Origin.** A handshake whose `Origin` is not the request's
  own -- host and port other than its `Host`'s, a loopback one on another port
  included -- and not in `server.allowed_origins` is refused before it is
  accepted (uvicorn answers 403). One with no Origin (curl, a script) is not a
  browser's and passes. Vite's dev server serves the page on its own port and
  proxies /ws here, so dashboard/run.sh names its origins in dev mode, in
  `server.dev_origins` (RC_DASH_DEV_ORIGINS), which adds to the configured list.
* **The Host, with no password.** A request, HTTP or websocket, whose `Host`
  is not a loopback name (localhost, 127.0.0.0/8, ::1), the bind host
  (`server.host`), or one of `server.allowed_hosts` ("*" for any) gets a 403:
  a rebound name is none of them. With a password the check is off, so a
  tunnel or a proxy that keeps its public name in the Host (hpc/deploy)
  needs no list; the cookie already defeats rebinding.

Outside the sign-in (auth.py), so a request refused here costs no password
check, and outside every route and the static mount, so a route added later
is covered without anyone remembering to be.

`JsonOnly`, inside the sign-in: a request under /api that may change
something takes a JSON body, which another site's page cannot send without the
browser asking first.
"""
import ipaddress
import urllib.parse

from starlette.responses import JSONResponse

NO_STORE = {"cache-control": "no-store"}


def allowed_origins(cfg):
    """The origins a websocket may come from besides the request's own:
    `server.allowed_origins`, and a dev server's (`server.dev_origins`, which
    dashboard/run.sh sets) added to them, never in their place."""
    return list(cfg.get("server.allowed_origins") or ()) + list(cfg.get("server.dev_origins") or ())


def host_name(hostport):
    """The name in a Host header or an origin's netloc: `Example.org:8080` ->
    `example.org`, `[::1]:8080` -> `::1`, without a trailing dot."""
    h = (hostport or "").strip().lower()
    if h.startswith("["):
        h = h[1:].partition("]")[0]
    elif h.count(":") == 1:
        h = h.partition(":")[0]
    return h.rstrip(".")


def is_loopback(name):
    """localhost and its subdomains (a browser resolves them itself, never by
    DNS), or a loopback address."""
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class SiteGuard:
    """ASGI: the checks in the module's docstring. `check_host` is on while
    the service has no password."""

    def __init__(self, app, check_host, bind_host="", allowed_hosts=(), allowed_origins=()):
        self.app = app
        self.check_host = bool(check_host)
        names = {host_name(h) for h in allowed_hosts if str(h).strip()}
        self.any_host = "*" in names
        self.hosts = names | ({host_name(bind_host)} if bind_host else set())
        self.origins = {str(o).strip().lower().rstrip("/") for o in allowed_origins
                        if str(o).strip()}

    def host_ok(self, host):
        if host is None:                 # HTTP/1.0: no browser sends a request without one
            return True
        name = host_name(host)
        return self.any_host or is_loopback(name) or name in self.hosts

    def origin_ok(self, origin, host):
        o = origin.strip().lower().rstrip("/")
        parts = urllib.parse.urlsplit(o)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return o in self.origins     # "null": a sandboxed frame, a file
        if o in self.origins or parts.netloc in self.origins:
            return True
        return host is not None and parts.netloc == host.strip().lower()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):           # lifespan
            await self.app(scope, receive, send)
            return
        host, origins = None, []
        for k, v in scope.get("headers") or ():
            if k == b"host" and host is None:
                host = v.decode("latin-1")
            elif k == b"origin":
                origins.append(v.decode("latin-1"))
        if self.check_host and not self.host_ok(host):
            why = ("this dashboard has no password, so it answers only under a loopback name, "
                   "its own address, or one of server.allowed_hosts; %s is none of them"
                   % host_name(host)[:100])
        elif kind == "websocket" and not all(self.origin_ok(o, host) for o in origins):
            why = None
        else:
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()                              # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
        else:
            await JSONResponse({"detail": why}, status_code=403, headers=NO_STORE)(
                scope, receive, send)


class JsonOnly:
    """ASGI: a request under /api with any method but GET, HEAD and OPTIONS --
    one that may change something, or start an agent -- must say its body is
    JSON, or gets a 415 before it is read. A form or a text/plain body is what
    a page on another site can send without the browser first asking this
    service (a CORS preflight, which it never answers); a JSON one it cannot.
    Inside the sign-in, so a request without the password is told that first."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path") or ""
        if scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS") \
                and (path == "/api" or path.startswith("/api/")):
            ctype = next((v.decode("latin-1") for k, v in scope.get("headers") or ()
                          if k == b"content-type"), "")
            if ctype.split(";")[0].strip().lower() != "application/json":
                await JSONResponse({"detail": "this route takes a JSON body, sent as "
                                              "Content-Type: application/json"},
                                   status_code=415, headers=NO_STORE)(scope, receive, send)
                return
        await self.app(scope, receive, send)
