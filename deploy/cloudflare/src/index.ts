/* The dashboard's demo mode on Cloudflare: one container running
 * `rc_dashboard demo` (docker/demo/Dockerfile), and this Worker in front of it.
 *
 * Every request goes to the same instance, "demo": every visitor watches the
 * same live demo, and there is never more than one to pay for (max_instances in
 * wrangler.jsonc). The page's websocket is proxied through, by the containers
 * package.
 *
 * The container sleeps after SLEEP_AFTER with no request and no websocket
 * message, and the next request starts it again. The service then writes its
 * synthetic history (RC_DASH_DEMO_DAYS) before it listens: ~11 s on a `basic`
 * instance, measured, plus the container's own start. The containers package
 * waits 20 s for a port and then fails the request, so a request that finds the
 * demo cold waits at most READY_WAIT_MS for it and otherwise gets a page that
 * says it is starting and reloads itself (an API call, a 503). The container
 * goes on starting regardless; the next request finds it further along.
 *
 * Visits are counted by Statcounter, whose code (STATCOUNTER) this Worker adds
 * to each dashboard page it serves. Only here, on the public demo: the image,
 * `rc_dashboard demo` and every deployment on a cluster load no tracker, and a
 * cluster's URLs, whose filters carry real user names, reach no third party.
 */
import { Container } from "@cloudflare/containers";

const SLEEP_AFTER = "15m";
/** How long one request waits for a starting demo before it answers "starting". */
const READY_WAIT_MS = 6_000;
/** The port-check budget of one wait: never the limit; the request's own abort is. */
const PORT_BUDGET_MS = 180_000;

export class DashboardDemo extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = SLEEP_AFTER;
  // The demo reads nothing from the internet, and a public one should reach nothing.
  enableInternet = false;

  private ready = false;

  override onStop(): void {
    this.ready = false;
  }

  override async fetch(request: Request): Promise<Response> {
    if (!this.ready) {
      try {
        // Each request waits on its own, and its wait ends -- aborted -- before it
        // answers. A wait shared across requests was dropped with the request that
        // began it, and hung every request after it (seen under `wrangler dev`).
        await this.startAndWaitForPorts(this.defaultPort, {
          abort: AbortSignal.timeout(READY_WAIT_MS),
          portReadyTimeoutMS: PORT_BUDGET_MS,
        });
        this.ready = true;
      } catch {
        return startingResponse(request);
      }
    }
    return super.fetch(request);
  }
}

function startingResponse(request: Request): Response {
  const headers = { "retry-after": "5", "cache-control": "no-store" };
  const page =
    request.method === "GET" &&
    !request.headers.has("upgrade") &&
    (request.headers.get("accept") ?? "").includes("text/html");
  if (!page) {
    return Response.json(
      { error: "starting", detail: "the demo is starting; try again in a few seconds" },
      { status: 503, headers },
    );
  }
  return new Response(STARTING_PAGE, {
    status: 503,
    headers: { ...headers, "content-type": "text/html; charset=utf-8" },
  });
}

const STARTING_PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="4">
<title>demo · synthetic data · Agent Behaviour</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f4f1;
         color: #1e1e1e; font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 34rem; padding: 2rem 1.25rem; }
  h1 { margin: 0; font-size: 1.35rem; }
  .sub { margin: .1rem 0 1.4rem; font: 12px ui-monospace, Menlo, monospace; color: #8a6410;
         border: 1px dashed #c99a2e; border-radius: 99px; padding: .15rem .6rem; display: inline-block; }
  .muted { color: #6b6b6b; font-size: 13.5px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #a51c30;
         margin-right: .5rem; animation: beat 1.2s ease-in-out infinite; }
  @keyframes beat { 50% { opacity: .3; } }
</style>
</head>
<body>
<main>
  <h1>Agent Behaviour</h1>
  <div class="sub">demo · synthetic data</div>
  <p><span class="dot"></span>Starting the demo. It writes two days of made-up records for
  fictional login nodes and their users, then serves the dashboard over them. Nothing in it
  was measured.</p>
  <p class="muted">After the demo has been idle this takes about half a minute; the page
  reloads itself.</p>
</main>
</body>
</html>
`;

/** Statcounter's default code for the openData-AgenticSystem project, as it gives it. */
const STATCOUNTER = `<!-- Default Statcounter code for openData-AgenticSystem
https://data.agentic-system.org -->
<script type="text/javascript">
var sc_project=13354152;
var sc_invisible=1;
var sc_security="3a3407c0";
</script>
<script type="text/javascript"
src="https://www.statcounter.com/counter/counter.js"
async></script>
<noscript><div class="statcounter"><a title="Web Analytics
Made Easy - Statcounter" href="https://statcounter.com/"
target="_blank"><img class="statcounter"
src="https://c.statcounter.com/13354152/0/3a3407c0/1/"
alt="Web Analytics Made Easy - Statcounter"
referrerPolicy="no-referrer-when-downgrade"></a></div></noscript>
<!-- End of Statcounter Code -->
`;

/** A dashboard page with STATCOUNTER at the end of its body, where Statcounter
 * asks for it; anything else as it came. Only a 200 page is counted: never the
 * "starting" page (a 503), which reloads itself every few seconds and would
 * count each reload as a visit, and never an API reply or the websocket. */
function counted(response: Response): Response {
  const type = response.headers.get("content-type") ?? "";
  if (response.status !== 200 || !type.startsWith("text/html")) {
    return response;
  }
  return new HTMLRewriter()
    .on("body", {
      element(body) {
        body.append(STATCOUNTER, { html: true });
      },
    })
    .transform(response);
}

export default {
  async fetch(request, env): Promise<Response> {
    return counted(await env.DEMO.getByName("demo").fetch(request));
  },
} satisfies ExportedHandler<Env>;
