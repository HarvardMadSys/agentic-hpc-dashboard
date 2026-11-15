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
 *
 * The demo takes a password (the DEMO_PASSWORD secret: `npx wrangler secret
 * put DEMO_PASSWORD`). Without the cookie the sign-in sets, a page gets the
 * sign-in page, which says whom to ask (CONTACT), and anything else a 401;
 * neither wakes the container. With no secret set the demo is closed, never
 * open. Changing the secret signs everyone out.
 */
import { Container } from "@cloudflare/containers";

declare global {
  interface Env {
    DEMO_PASSWORD?: string;
  }
}

/** Whom a visitor asks for the password. */
const CONTACT = "admin@agentic-system.org";
const COOKIE = "demo_auth";
const SIGNED_IN_FOR_S = 30 * 86400;
const SIGN_IN_PATH = "/__demo/sign-in";

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

// ------------------------------------------------------------------ password

/** The cookie's value: an HMAC of a fixed label under the password, so it holds
 * no password, and a new password makes every old cookie worthless. */
async function token(password: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(password), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode("rc-dashboard-demo/v1"));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Equal strings, compared in time that does not depend on where they differ. */
function same(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}

function cookieOf(request: Request, name: string): string | null {
  for (const part of (request.headers.get("cookie") ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

/** Only a path on this site: never a redirect elsewhere. `next` is resolved as a
 * browser would (which reads `/\evil.example` as `//evil.example`), and anything
 * that lands on another origin, or contains a backslash at all, is "/". */
function backTo(next: string | null, site: URL): string {
  if (!next || !next.startsWith("/") || next.includes("\\")) return "/";
  let to: URL;
  try {
    to = new URL(next, site);
  } catch {
    return "/";
  }
  return to.origin === site.origin ? to.pathname + to.search + to.hash : "/";
}

/** Plain http on the open internet: the password must never cross it. Loopback
 * is left alone, for `wrangler dev`. */
function insecure(url: URL): boolean {
  return url.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
}

const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function signInPage(next: string, wrong: boolean, status: number): Response {
  const page = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>demo · synthetic data · Agent Behaviour</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f4f1;
         color: #1e1e1e; font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width: min(26rem, 100% - 2.5rem); padding: 2rem 0; }
  h1 { margin: 0; font-size: 1.35rem; }
  .sub { margin: .1rem 0 1.2rem; font: 12px ui-monospace, Menlo, monospace; color: #8a6410;
         border: 1px dashed #c99a2e; border-radius: 99px; padding: .15rem .6rem; display: inline-block; }
  form { display: flex; gap: .5rem; margin: 1rem 0 .4rem; }
  input { flex: 1; min-width: 0; font: inherit; padding: .5rem .7rem; border: 1px solid #c9c4bc;
          border-radius: 8px; background: #fff; }
  button { font: inherit; padding: .5rem 1rem; border: 0; border-radius: 8px; background: #a51c30;
           color: #fff; cursor: pointer; }
  .wrong { color: #a51c30; font-size: 13.5px; margin: 0; }
  .muted { color: #6b6b6b; font-size: 13.5px; }
  a { color: #a51c30; }
</style>
</head>
<body>
<main>
  <h1>Agent Behaviour</h1>
  <div class="sub">demo · synthetic data</div>
  <p>This demo of the dashboard runs on made-up records for fictional login nodes and their
  users. It takes a password.</p>
  <form method="post" action="${SIGN_IN_PATH}">
    <input type="hidden" name="next" value="${esc(next)}">
    <input type="password" name="password" placeholder="Password" aria-label="Password"
           autocomplete="current-password" required autofocus>
    <button type="submit">Enter</button>
  </form>
  ${wrong ? '<p class="wrong">That password is not right.</p>' : ""}
  <p class="muted">For a password, write to
  <a href="mailto:${CONTACT}?subject=Agent%20dashboard%20demo">${CONTACT}</a>.</p>
</main>
</body>
</html>
`;
  return new Response(page, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-robots-tag": "noindex",
    },
  });
}

/** The gate: null to let the request through, or the answer to give instead. */
async function gate(request: Request, env: Env): Promise<Response | null> {
  const url = new URL(request.url);
  if (insecure(url)) {
    // Before any form is shown or read, whatever the zone's Always Use HTTPS says.
    url.protocol = "https:";
    return Response.redirect(url.toString(), 308);
  }
  const password = env.DEMO_PASSWORD ?? "";
  const want = password ? await token(password) : null;

  if (url.pathname === SIGN_IN_PATH && request.method === "POST") {
    const form = await request.formData().catch(() => null);
    const next = backTo(form?.get("next")?.toString() ?? null, url);
    const given = form?.get("password")?.toString() ?? "";
    if (!want || !same(await token(given), want)) {
      await new Promise((r) => setTimeout(r, 1000)); // a slower guess
      return signInPage(next, true, 401);
    }
    return new Response(null, {
      status: 303,
      headers: {
        location: next,
        "cache-control": "no-store",
        "set-cookie": `${COOKIE}=${want}; Path=/; Max-Age=${SIGNED_IN_FOR_S}; HttpOnly; Secure; SameSite=Lax`,
      },
    });
  }

  const have = cookieOf(request, COOKIE);
  if (want && have && same(have, want)) return null;

  const page =
    request.method === "GET" &&
    !request.headers.has("upgrade") &&
    (request.headers.get("accept") ?? "").includes("text/html");
  if (page) return signInPage(backTo(url.pathname + url.search, url), false, 401);
  return Response.json(
    { error: "password", detail: `the demo takes a password; ask ${CONTACT}` },
    { status: 401, headers: { "cache-control": "no-store" } },
  );
}

export default {
  async fetch(request, env): Promise<Response> {
    const stop = await gate(request, env);
    if (stop) return stop;
    return counted(await env.DEMO.getByName("demo").fetch(request));
  },
} satisfies ExportedHandler<Env>;
