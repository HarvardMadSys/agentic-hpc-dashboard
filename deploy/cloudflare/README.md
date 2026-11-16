# The demo on Cloudflare

The dashboard's [demo mode](../../dashboard/README.md#demo-mode) on a public URL,
**<https://dashboard-demo.agentic-system.org>**: this Worker fronts one
[Cloudflare Container](https://developers.cloudflare.com/containers/) running
`rc_dashboard demo`, built from [`docker/demo/Dockerfile`](../../docker/demo/Dockerfile).
Every visitor watches the same live demo, and every page says *fake data*: the
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

The demo is then at <https://dashboard-demo.agentic-system.org>, and at the
`https://agent-dashboard-demo.<your-subdomain>.workers.dev` URL the deploy prints. The
hostname is the Worker's [Custom Domain](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
(`routes` in `wrangler.jsonc`): the first deploy creates its DNS record and certificate,
which can take a few minutes to answer, and the container takes a few minutes to provision.
Until then, and after every idle spell, the page says the demo is starting and reloads
itself. Plain `http://` is redirected to `https://` by the Worker, so the password never
crosses the network in the clear.

### Deploying from GitHub

[`.github/workflows/demo-deploy.yml`](../../.github/workflows/demo-deploy.yml) runs the same
`npx wrangler deploy` on every push to `develop` that changes what the demo runs: the
service (`dashboard/rc_dashboard`, `pyproject.toml`, `uv.lock`), the page (`dashboard/web`),
`docker/demo`, or this folder. A change to the collector or a test does not redeploy. It can
also be run by hand from the repository's Actions tab (*demo-deploy*, *Run workflow*, on
`develop`): after rotating the token, or after a security fix to a base image, since
`docker/demo/Dockerfile` names them by tag and a rebuilt one reaches the demo only at the
next deploy. Only `develop` deploys; a run started from another branch is skipped.

The runner's Docker builds the image, and wrangler is the version pinned in `package.json`,
as for a deploy by hand. `npm run typecheck` runs first, since wrangler's bundler strips
types without checking them. Deploys queue rather than cancel one another, because a deploy
is not transactional: the new Worker can be live before its image has rolled out. GitHub
keeps one waiting run, so of several pushes during a deploy the newest is the one that
deploys next, and the ones it replaced show as cancelled.

It needs two secrets from the Cloudflare account that holds the `agentic-system.org` zone.
Put them on the **`demo` environment**, not on the repository, so that no other workflow or
branch can read a token that edits that account's Workers and Containers:

1. *Settings*, *Environments*, *New environment*, `demo`. Do this before the workflow's first
   run: a run that finds no `demo` environment creates one with no branch rule, which
   protects nothing until step 2 is done.
2. Under *Deployment branches and tags*, choose *Selected branches and tags* and add
   `develop`. This rule, not the job's `if`, is what keeps the token from other branches: a
   run started by hand uses the workflow file of the branch it is started on, which can
   have the `if` taken out.
3. Only then, add the two environment secrets:

| secret | value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | an API token (*My Profile*, *API Tokens*, *Create Token*) from the **Edit Cloudflare Workers** template, with **Account · Containers · Edit** added, scoped to that account and its `agentic-system.org` zone |
| `CLOUDFLARE_ACCOUNT_ID` | that account's ID (the Workers overview shows it) |

Never add them as repository secrets as well (*Settings*, *Secrets and variables*,
*Actions*, *Repository secrets* should hold no `CLOUDFLARE_*`): a repository secret is
readable by any workflow on any branch, whatever the environment's rule says.

Until both are set, the workflow fails at its first step and names the missing secret, so a
push to `develop` never looks deployed when it was not. Cloudflare's docs name the template
for Workers and the Containers permission. They do not say what attaching the Custom Domain
needs beyond the template: if a deploy fails at the domain, add **Zone · DNS · Edit** for
`agentic-system.org` to the token.

## What runs

| | |
|---|---|
| the address | `dashboard-demo.agentic-system.org` (a Custom Domain), and the `workers.dev` URL |
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
before it listens -- about 11 s on a `basic` instance, measured when the demo still wrote
JSONL, plus the container's own start. EBR, which it writes now, takes longer to write than
JSONL (timings in [Demo mode](../../dashboard/README.md#demo-mode)). A request waits up to
6 s (`READY_WAIT_MS`); past that, a page gets a small "starting the demo" page that reloads
itself every 4 s, and an API call a 503 with `Retry-After`. Each request waits on its own and
ends its wait before it answers: a wait shared between requests was dropped with the request
that began it and left every request after it hanging (found under `wrangler dev`). The feed
lives in the container, so each start writes it afresh, ending at the moment it starts.

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
