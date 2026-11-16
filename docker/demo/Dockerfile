# The dashboard's demo mode in one image: the real service over a synthetic feed
# it writes itself (dashboard/rc_dashboard/demo). No collector, no cluster, no
# root. Everything it serves is made up, and every page says "fake data".
#
#   docker build -f docker/demo/Dockerfile -t rc-dashboard-demo .
#   docker run --rm -p 8080:8080 rc-dashboard-demo         # http://127.0.0.1:8080
#
# deploy/cloudflare runs this image as a Cloudflare Container behind a Worker.
#
# The feed lives inside the container (/tmp/demo) and is written afresh at each
# start: RC_DASH_DEMO_DAYS (2 here) is how much history that is, and so how long
# a cold start takes before the service listens. Every window is then built
# from it, also before the port opens (panels.preprocess, set by `demo`). TZ is whose working day the
# fictional people keep. Ask LLM is off: there is no agent CLI in the image, and
# a public demo must not spend anyone's model budget.

# ---- the page: built once, served by the service as static files
FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY dashboard/web/package.json dashboard/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/web/ ./
RUN npm run build

# ---- the service, with the dependencies uv.lock pins
FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    TZ=America/New_York
# (the base image carries the time-zone database TZ needs)
WORKDIR /app/dashboard
COPY dashboard/pyproject.toml dashboard/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY dashboard/rc_dashboard ./rc_dashboard
COPY --from=web /src/web/dist ./web/dist

RUN useradd --create-home --uid 10001 demo \
 && mkdir -p /tmp/demo \
 && chown demo /tmp/demo
USER demo
ENV RC_DASH_HOST=0.0.0.0 \
    RC_DASH_PORT=8080 \
    RC_DASH_DEMO_DIR=/tmp/demo \
    RC_DASH_DEMO_DAYS=2 \
    RC_DASH_ASK=0
EXPOSE 8080
CMD ["/app/dashboard/.venv/bin/python", "-m", "rc_dashboard", "demo"]
