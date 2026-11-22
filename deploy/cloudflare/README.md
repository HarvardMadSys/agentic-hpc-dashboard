# The demo on Cloudflare

This folder puts the dashboard's [demo mode](../../dashboard/README.md#demo-mode) on a public
URL, **<https://dashboard-demo.agentic-system.org>**. A Worker fronts one
[Cloudflare Container](https://developers.cloudflare.com/containers/) running
`rc_dashboard demo`, built from [`docker/demo/Dockerfile`](../../docker/demo/Dockerfile).
Every visitor watches the same live demo, over made-up records for fictional login nodes and
users, and every page says *fake data*.

## Deploy

Needs the Cloudflare account that holds the `agentic-system.org` zone, on the Workers Paid
plan (Containers run there); Docker running locally (`wrangler deploy` builds the image,
`linux/amd64`); and Node 22.12 or later (what the pinned wrangler needs).

```bash
cd deploy/cloudflare
npm ci
npx wrangler login
npx wrangler deploy      # builds docker/demo/Dockerfile, pushes it, deploys the Worker
npx wrangler secret put DEMO_PASSWORD   # the demo's password; until it is set, nobody gets in
```

**The password.** Visitors sign in once and stay signed in on that browser for 30 days (a
cookie). Until then, a page request gets a sign-in page that says to write to
admin@agentic-system.org (`CONTACT` in `src/index.ts`), and an API call a 401; neither wakes
the container. A wrong password is answered a second late. Running
`npx wrangler secret put DEMO_PASSWORD` again changes the password at once, with no deploy,
and signs everyone out. With no secret set, the demo stays closed.

The demo is also at the `https://agent-dashboard-demo.<your-subdomain>.workers.dev` URL the
deploy prints. Its hostname is the Worker's [Custom Domain](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
(`routes` in `wrangler.jsonc`): the first deploy creates its DNS record and certificate,
which can take a few minutes to answer, as can the container's first start. Until then, and
after every idle spell, the page says the demo is starting and reloads itself. The Worker
redirects `http://` to `https://`, so the password never crosses the network in the clear.

### Deploying from GitHub

[`.github/workflows/demo-deploy.yml`](../../.github/workflows/demo-deploy.yml) runs the same
`npx wrangler deploy`, with the wrangler version `package.json` pins, on every push to
`develop` that changes what the demo runs: the service (`dashboard/rc_dashboard`,
`pyproject.toml`, `uv.lock`), the page (`dashboard/web`, its checks included),
`docker/demo`, this folder, or the workflow itself, and only once every test suite
(`ci.yml`) has passed; if one fails, the demo stays as it is. A change to `*.md` files alone does not deploy: nothing in the image's
build, the service or the Worker reads one. Run it by hand from the Actions tab
(*demo-deploy*, *Run workflow*, on `develop`) after rotating the token, or to pick up a
security fix in a base image, which `docker/demo/Dockerfile` names by tag. Only `develop`
deploys; a run started from another branch is skipped.

`npm run typecheck` runs first, since wrangler's bundler strips types without checking them.
Deploys queue rather than cancel one another, because a deploy is not transactional: the new
Worker can be live before its image has rolled out. GitHub keeps one waiting run, so of
several pushes during a deploy the newest deploys next, and the ones it replaced show as
cancelled.

It needs two secrets from the Cloudflare account that holds the `agentic-system.org` zone,
on the **`demo` environment**, not the repository, so that no other workflow or branch can
read a token that edits that account's Workers and Containers:

1. *Settings*, *Environments*, *New environment*, `demo`, before the workflow's first run: a
   run that finds no `demo` environment creates one with no branch rule, which protects
   nothing until step 2 is done.
2. Under *Deployment branches and tags*, choose *Selected branches and tags* and add
   `develop`. This rule, not the job's `if`, keeps the token from other branches: a run
   started by hand uses the workflow file of its branch, which could drop the `if`.
3. Only then, add the two environment secrets:

| secret | value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | an API token (*My Profile*, *API Tokens*, *Create Token*) from the **Edit Cloudflare Workers** template, with **Account · Containers · Edit** added, scoped to that account and its `agentic-system.org` zone |
| `CLOUDFLARE_ACCOUNT_ID` | that account's ID (the Workers overview shows it) |

Never add them as repository secrets as well (*Settings*, *Secrets and variables*,
*Actions*, *Repository secrets* should hold no `CLOUDFLARE_*`): any workflow on any branch can
read a repository secret, whatever the environment's rule says.

Until both are set, the deploy job fails at its first step, after the tests, and names the
missing secret. If a deploy fails at attaching the Custom Domain, add **Zone · DNS · Edit**
for `agentic-system.org` to the token; Cloudflare's docs do not say whether the template
covers it.

## What runs

| | |
|---|---|
| the Worker (`src/index.ts`) | sends every request, the page's websocket included, to one container instance named `demo` |
| the container | `rc_dashboard demo` on port 8080: writes 2 days of history (`RC_DASH_DEMO_DAYS`), then serves and keeps appending in real time |
| instance | `basic` (1/4 vCPU, 1 GiB), at most one (`max_instances: 1`); the demo holds ~150 MB with every window built |
| outbound internet | none (`enableInternet = false`); Ask LLM is off in the image |
| visits | counted by [Statcounter](https://statcounter.com/): the Worker adds its code (`STATCOUNTER` in `src/index.ts`) to the end of each dashboard page, never to the "starting" page, which reloads itself, nor to an API reply. Nothing else does: the image and the service load no tracker, so a dashboard on a cluster, whose URLs carry real user names, sends nothing to a third party |
| time zone | `America/New_York` (`TZ` in the Dockerfile): the working day the fictional people keep |

**Cold start.** The container sleeps after 15 minutes with no request and no websocket
message (`SLEEP_AFTER`), and the next request starts it again. Before it listens, the
service writes its history (about 11 s on a `basic` instance) and builds every window from
it (`panels.preprocess`; ~6 s on a laptop), so the first page has every window ready; the
container's own start comes on top. A request waits up to 6 s (`READY_WAIT_MS`); after that
a page request gets a small "starting the demo" page that reloads itself every 4 s, and an
API call a 503 with `Retry-After`. The feed lives in the container, so each start writes it
afresh, ending at the moment the container starts.

**Cost.** A container is billed while it is awake, and an open page keeps it awake: the
dashboard streams over a websocket, and every message counts as activity, so a tab left open
overnight keeps the instance running overnight. See
[Containers pricing](https://developers.cloudflare.com/containers/pricing/).

## Operate

```bash
npx wrangler tail                    # the Worker's logs
npx wrangler containers list         # the container's state
npx wrangler deploy                  # after pulling: a new image and Worker
npx wrangler delete                  # take the demo down
npm run typecheck                    # wrangler types && tsc --noEmit
```

To change the history or the time zone, edit `docker/demo/Dockerfile`. The address, the
instance size and the instance ceiling are in `wrangler.jsonc` (`standard-1` halves a cold
start but costs more while awake, and `"workers_dev": false` leaves only the Custom Domain).
The idle time, the wait and the Statcounter code are in `src/index.ts`. A hostname in a zone
that belongs to another account can't be a Custom Domain: deploy from that account, or
delete `routes` to use the `workers.dev` URL alone.

## Without Cloudflare

The same image runs anywhere Docker does ([`docker/README.md`](../../docker/README.md#the-demo)),
and `npx wrangler dev` runs the Worker and the container together locally, also through
Docker.
