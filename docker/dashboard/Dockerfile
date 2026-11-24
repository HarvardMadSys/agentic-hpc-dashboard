# The dashboard service in one image: the page built in a Node stage, the package
# built as a wheel (the page inside it) and installed, with the dependencies
# dashboard/uv.lock pins, in a Python stage. It reads the collector's day-files
# from /feeds/ebpf and /feeds/node (mount them read-only) and keeps what it must
# keep in /state (a volume): its cache, the session key, the anonymize salt and
# the Trend tab's roll-up table.
#
#   docker build -f docker/dashboard/Dockerfile -t rc-dashboard .
#   docker run -d --name rc-dashboard -p 127.0.0.1:8080:8080 \
#     -v /var/log/ebpfm:/feeds/ebpf:ro -v /var/log/ebpfm-login:/feeds/node:ro \
#     -v rc-dashboard-state:/state --group-add "$(getent group ebpfm | cut -d: -f3)" \
#     -e RC_DASH_PASSWORD_HASH_FILE=/state/password.hash -e TZ=America/New_York \
#     rc-dashboard
#
# Like the service everywhere, it refuses to start without a password: a hash
# file (RC_DASH_PASSWORD_HASH_FILE, read again when it changes and on SIGHUP) or
# the hash (RC_DASH_PASSWORD_HASH); RC_DASH_OPEN=1 serves the page with none.
# Make one with `docker run --rm -v rc-dashboard-state:/state rc-dashboard
# rc-dashboard new-password --dir /state/password --install /state/password.hash`.
# Every other setting is an RC_DASH_* variable, or a config file mounted in and
# named by RC_DASH_CONFIG (docker/README.md, "The dashboard").
#
# GIT_COMMIT stamps the commit it is built from into the package
# (rc_dashboard/BUILD.json), which `rc-dashboard --version` and /api/config
# report; .github/workflows/dashboard-image.yml passes it.

# ---- the page
FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY dashboard/web/package.json dashboard/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/web/ ./
RUN npm run build

# ---- the wheel, the page inside it, and the pinned requirements
FROM python:3.13-slim-bookworm AS wheel
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /src/dashboard
COPY dashboard/pyproject.toml dashboard/uv.lock ./
COPY dashboard/rc_dashboard ./rc_dashboard
COPY dashboard/risk-profiles ./risk-profiles
COPY --from=web /src/web/dist ./web/dist
ARG GIT_COMMIT=""
RUN case "$GIT_COMMIT" in \
      "") ;; \
      *[!0-9a-f]*) echo "GIT_COMMIT is not a commit: $GIT_COMMIT" >&2; exit 1 ;; \
      *) printf '{"commit": "%s"}\n' "$GIT_COMMIT" > rc_dashboard/BUILD.json ;; \
    esac \
 && uv build --wheel --out-dir /dist \
 && uv export --frozen --no-dev --no-emit-project --format requirements-txt --quiet \
      > /dist/requirements.txt

# ---- the service
FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# (the base image carries the time-zone database TZ needs)
RUN --mount=type=bind,from=wheel,source=/dist,target=/dist \
    uv venv /opt/rc-dashboard \
 && VIRTUAL_ENV=/opt/rc-dashboard uv pip install --no-cache -r /dist/requirements.txt \
 && VIRTUAL_ENV=/opt/rc-dashboard uv pip install --no-cache --no-deps /dist/*.whl \
 && rm /usr/local/bin/uv

RUN useradd --create-home --uid 10001 rc \
 && mkdir -p /state /feeds/ebpf /feeds/node \
 && chown rc /state \
 && chmod 0700 /state
USER rc
# Listening on every address inside the container: publish the port on the host's
# loopback (-p 127.0.0.1:8080:8080) and reach it through a tunnel, or put a proxy in
# front. With no password the service answers only under a loopback name, its own
# (server.host) or RC_DASH_ALLOWED_HOSTS (dashboard/README.md, "Password").
ENV PATH=/opt/rc-dashboard/bin:$PATH \
    RC_DASH_HOST=0.0.0.0 \
    RC_DASH_PORT=8080 \
    RC_DASH_EBPF_ROOTS=/feeds/ebpf \
    RC_DASH_NODE_ROOTS=/feeds/node \
    RC_DASH_CACHE_DIR=/state/cache
VOLUME ["/state"]
EXPOSE 8080
CMD ["rc-dashboard", "serve"]
