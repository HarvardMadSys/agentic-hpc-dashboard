# The demo on Cloudflare

The dashboard's [demo mode](../../dashboard/README.md#demo-mode) on a public URL,
**<https://hpc-dashboard.agentic-system.org>**: this Worker fronts one
[Cloudflare Container](https://developers.cloudflare.com/containers/) running
`rc_dashboard demo`, built from [`docker/demo/Dockerfile`](../../docker/demo/Dockerfile).
Every visitor watches the same live demo, and every page says *synthetic data*: the
records are made up for fictional login nodes and users, and nothing in them was measured.

## Deploy

Needs the Cloudflare account that holds the `agentic-system.org` zone, on the Workers Paid
plan (Containers run there); Docker running locally (`wrangler deploy` builds the image,
`linux/amd64`); and Node 20 or later.

```bash
cd deploy/cloudflare
npm ci
npx wrangler login
npx wrangler deploy      # builds docker/demo/Dockerfile, pushes it, deploys the Worker
npx wrangler secret put DEMO_PASSWORD   # the demo's password; until it is set, nobody gets in
```

**The password.** Visitors sign in once and stay signed in for 30 days on that browser (a
cookie). Without it, a page shows a sign-in page that says to write to
admin@agentic-system.org (`CONTACT` in `src/index.ts`), and an API call gets a 401; neither
wakes the container. A wrong password is answered a second late. To change the password, run
`npx wrangler secret put DEMO_PASSWORD` again: it takes effect at once, no deploy needed, and
signs everyone out. With no secret set the demo stays closed.

The demo is then at <https://hpc-dashboard.agentic-system.org>, and at the
`https://agent-dashboard-demo.<your-subdomain>.workers.dev` URL the deploy prints. The
hostname is the Worker's [Custom Domain](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
(`routes` in `wrangler.jsonc`): the first deploy creates its DNS record and certificate,
which can take a few minutes to answer, and the container takes a few minutes to provision.
Until then, and after every idle spell, the page says the demo is starting and reloads
itself. Plain `http://` reaches it too, or is redirected to `https://` if the zone has
*Always Use HTTPS* on.

## What runs

| | |
|---|---|
| the address | `hpc-dashboard.agentic-system.org` (a Custom Domain), and the `workers.dev` URL |
| the Worker (`src/index.ts`) | sends every request, the page's websocket included, to one container instance named `demo` |
| the container | `rc_dashboard demo` on port 8080: writes 2 days of history (`RC_DASH_DEMO_DAYS`), then serves and keeps appending in real time |
| instance | `basic` (1/4 vCPU, 1 GiB); the demo holds ~130 MB |
| at most | one instance (`max_instances: 1`) |
| outbound internet | none (`enableInternet = false`); Ask LLM is off in the image |
| access | a password (the `DEMO_PASSWORD` secret), checked by the Worker before anything reaches the container |
| visits | counted by [Statcounter](https://statcounter.com/): the Worker adds its code (`STATCOUNTER` in `src/index.ts`) to the end of each dashboard page, never to the "starting" page, which reloads itself, nor to an API reply |
| time zone | `America/New_York` (`TZ` in the Dockerfile): whose working day the fictional people keep |

**Cold start.** The container sleeps after 15 minutes with no request and no websocket
message (`SLEEP_AFTER`), and the next request starts it. The service writes its history
before it listens -- about 11 s on a `basic` instance, measured, plus the container's own
start. A request waits up to 6 s (`READY_WAIT_MS`); past that, a page gets a small
"starting the demo" page that reloads itself every 4 s, and an API call a 503 with
`Retry-After`. Each request waits on its own and ends its wait before it answers: a wait
shared between requests was dropped with the request that began it and left every request
after it hanging (found under `wrangler dev`). The feed lives in the container, so each
start writes it afresh, ending at the moment it starts.

**Cost.** A container is billed while it is awake. An open page keeps it awake -- the
dashboard streams over a websocket, and every message counts as activity -- so a tab left
open overnight keeps one `basic` instance running overnight; `max_instances` stops it ever
being more than one. See [Containers pricing](https://developers.cloudflare.com/containers/pricing/).

## Operate

```bash
npx wrangler tail                    # the Worker's logs
npx wrangler containers list         # the container's state
npx wrangler deploy                  # after pulling: a new image and Worker
npx wrangler delete                  # take the demo down
npm run typecheck                    # wrangler types && tsc --noEmit
```

To change: the history and the time zone in `docker/demo/Dockerfile`; the address, the
instance size and the ceiling in `wrangler.jsonc` (`standard-1` halves a cold start, and
costs more while awake; `"workers_dev": false` leaves only the Custom Domain); the idle
time, the wait and the Statcounter code in `src/index.ts`. The code is added here, by the
Worker, and nowhere else: the image and the service load no tracker, so a dashboard on a
cluster, whose URLs carry real user names, sends nothing to a third party. A hostname in a zone of another account cannot be a
Custom Domain: deploy from that account, or delete `routes` for the `workers.dev` URL alone.

## Without Cloudflare

The same image runs anywhere Docker does:

```bash
docker build -f docker/demo/Dockerfile -t rc-dashboard-demo .     # from the repo root
docker run --rm -p 8080:8080 rc-dashboard-demo                    # http://127.0.0.1:8080
```

`npx wrangler dev` runs the Worker and the container together locally, also through Docker.
