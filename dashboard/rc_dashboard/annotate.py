"""What each tool call was for, in a model's words: the session inspector's
"annotate purposes".

The dashboard's own `purpose` is a rule over the command's name (`git` is git,
`python` is compute), which says what kind of program ran but not why: two
`python -c` calls, one checking a CUDA install and one plotting results, are
the same purpose by rule. This asks a language model instead. It is the only
place in the dashboard where a model writes a label onto a row, so the label
is kept apart from everything measured: its own field, its own colour on the
page, the model that wrote it named beside it, and never folded into any count.

What the model is given is what the page already shows -- each tool call's
command line as the inspector serves it, masked by `redact` and, when names are
masked, by the anonymizer -- and the folder it ran in. Nothing else of the
session leaves the host. It goes to whichever provider `annotate` names:

  anthropic   the Claude Messages API (`base_url` for a proxy or gateway)
  openai      any OpenAI-compatible chat completions endpoint: OpenAI itself,
              or `base_url` pointing at vLLM, Ollama, LiteLLM, OpenRouter, ...
  codex       the Codex CLI on this host, with that user's own login
  claude      the Claude Code CLI on this host (`claude -p`), with its login
  ask         whatever agent the chat (`ask`) is set up to run
  command     any command: the prompt on stdin, the answer on stdout (or in
              {answer_file}); {workdir} {answer_file} {model} are filled in

The two HTTP providers are called with the standard library alone (the service
depends on nothing but fastapi and uvicorn), so a proxy set in HTTPS_PROXY is
honoured as urllib honours it. An API key is read from the environment variable
`api_key_env` names (by default ANTHROPIC_API_KEY or OPENAI_API_KEY), or from
`api_key_file`, or -- last, and never shown by /api/config -- `api_key`.

The prompt is other users' command lines, text written by whoever ran them. A
CLI provider hands it to an agent on this host, which can read whatever the
service's account can read; so, as Ask's agent (ask.py), it runs only while the
service has a password, with only part of the service's environment
(`ask.agent_env`, `ask.env_passthrough`), in a directory of its own in the
system's temporary one. An HTTP provider runs nothing here: the model gets the
masked text the page shows, no tools, and its answer is read as a label and a
kind, so it is not refused for want of a password.

Every answer is kept (`<cache.dir>/annotate/purposes.jsonl`), keyed by the
provider and model, the prompt it was asked with (the built-in one's version,
or a digest of the site's own) and the command line, so a command
an agent runs in every session is asked about once, and every reader who opens
a session sees what any reader asked for before.
"""
import collections
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import ask, redact

# Bumped whenever the prompt or the label set changes what an answer means:
# answers kept under another version are not shown.
PROMPT_VERSION = 1

# The kinds a purpose is filed under, for its colour on the page. A model's
# kind outside these is `other`, never a new one.
KINDS = ("explore", "edit", "build", "test", "run", "debug", "hpc", "git", "data",
         "env", "monitor", "other")

KIND_GUIDE = """- explore: look around -- list, read or search files, inspect code or data
- edit: write or change files (sed -i, patch, a heredoc into a file, mv, rm)
- build: compile, install packages, set up a build
- test: run tests, linters or type checkers
- run: run the project's program, a script, a training or analysis job, on this host
- debug: check why something failed: tracebacks, logs, versions, a failing case
- hpc: the scheduler and the cluster: sbatch, srun, squeue, sacct, modules, partitions
- git: version control
- data: move or fetch data: download, copy, archive, sync
- env: the environment itself: paths, variables, which tool is installed, shell setup
- monitor: wait for or watch something: poll a job, sleep, tail a log
- other: none of these"""

DEFAULT_SYSTEM = """You label the tool calls an AI coding agent made on a computer the dashboard
watches, so a person reading the agent's session can see at a glance what each
call was for. You are given the calls in the order the agent made them, with the
folder each ran in when it is known. Each call is a shell command line; some
values in it are masked (a credential, a name) -- keep the masks as they are.

For each call, write:
- "purpose": why the agent most likely ran it, in at most 12 words, starting
  with a verb ("check which CUDA version the node has", "run the unit tests
  for the parser"). Read it in the light of the calls around it. Say what it
  does, not how it is spelled; do not repeat the command. When it cannot be
  told, say what it does plainly.
- "kind": one of explore, edit, build, test, run, debug, hpc, git, data, env,
  monitor, other:
%s

Answer with one JSON object and nothing else -- no prose, no code fence -- whose
keys are the calls' ids: {"1": {"purpose": "...", "kind": "..."}, ...}. Label
every call you are given.""" % KIND_GUIDE

PURPOSE_MAX = 120      # characters of a purpose kept
CMD_MAX = 600          # characters of a command line sent (the page shows 220)

# Argv for each CLI preset, with how it is handed a model. The prompt is piped
# on stdin; read-only and ephemeral where the CLI has the switch.
CLI_PRESETS = {
    "codex": (ask.PRESETS["codex"], ["-m"]),
    "claude": (["claude", "-p", "--output-format", "text", "{model_flag}"], ["--model"]),
}

DEFAULT_MODEL = {"anthropic": "claude-opus-5-5"}
DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
PROVIDERS = ("anthropic", "openai", "codex", "claude", "ask", "command")
# What `describe` says for a CLI provider while the service has no password.
NO_PASSWORD = ("this dashboard has no password, and a CLI provider runs only behind one: it runs "
               "an agent on this host, which can read whatever the service's account can read, "
               "over other users' command lines. Set server.auth.password_hash (`python -m "
               "rc_dashboard new-password` makes one), or use the anthropic or openai provider, "
               "which runs nothing here")
ANTHROPIC_VERSION = "2023-06-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


# ------------------------------------------------------------------ config
def provider_of(cfg):
    """`annotate.provider`, or `command` when only a command is set."""
    p = (cfg.get("annotate.provider") or "").strip().lower()
    if not p and cfg.get("annotate.command"):
        return "command"
    return p or "anthropic"


def model_of(cfg):
    p = provider_of(cfg)
    if p == "ask":
        return cfg.get("ask.model") or ""
    return cfg.get("annotate.model") or DEFAULT_MODEL.get(p, "")


def api_key(cfg):
    """The key for an HTTP provider: the variable `api_key_env` names, else
    `api_key_file`, else `api_key`. None when none is set."""
    p = provider_of(cfg)
    env = cfg.get("annotate.api_key_env") or DEFAULT_KEY_ENV.get(p)
    if env and os.environ.get(env):
        return os.environ[env].strip()
    path = cfg.get("annotate.api_key_file") or ""
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                return key
        except OSError:
            pass
    return (cfg.get("annotate.api_key") or "").strip() or None


def argv_for(cfg, workdir, answer_file):
    """The command a CLI provider runs. ValueError when there is none."""
    p = provider_of(cfg)
    if p == "ask":
        return ask.argv_for(cfg, workdir, answer_file)
    model = model_of(cfg)
    if p == "command":
        cmd, flag = cfg.get("annotate.command"), ["-m"]
        if not cmd:
            raise ValueError("annotate.provider is `command` but annotate.command is not set")
    elif p in CLI_PRESETS:
        cmd, flag = CLI_PRESETS[p]
    else:
        raise ValueError("annotate.provider %r is not a CLI" % p)
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    out = []
    for a in cmd:
        if a == "{model_flag}":
            out += flag + [model] if model else []
            continue
        out.append(str(a).replace("{workdir}", workdir).replace("{answer_file}", answer_file)
                   .replace("{model}", model))
    return out


def _uses_file(cfg):
    p = provider_of(cfg)
    if p == "ask":
        raw = cfg.get("ask.command") or ask.PRESETS.get(cfg.get("ask.agent", "codex")) or []
    elif p == "command":
        raw = cfg.get("annotate.command") or []
    else:
        raw = CLI_PRESETS.get(p, ([], []))[0]
    if isinstance(raw, str):
        raw = [raw]
    return any("{answer_file}" in str(a) for a in raw)


def describe(cfg):
    """What the page shows about the annotator, and whether it can run here."""
    p = provider_of(cfg)
    model = model_of(cfg)
    err = None
    if p not in PROVIDERS:
        err = "annotate.provider %r is not one of %s" % (p, ", ".join(PROVIDERS))
    elif p in ("anthropic", "openai"):
        if not model:
            err = "set annotate.model: the %s provider has no default model" % p
        elif p == "anthropic" and not api_key(cfg):
            err = ("no API key: set %s in the service's environment, or annotate.api_key_file"
                   % (cfg.get("annotate.api_key_env") or DEFAULT_KEY_ENV[p]))
        elif p == "openai" and not api_key(cfg) and not cfg.get("annotate.base_url"):
            err = ("no API key: set %s in the service's environment, or annotate.api_key_file "
                   "(a local endpoint set by annotate.base_url needs none)"
                   % (cfg.get("annotate.api_key_env") or DEFAULT_KEY_ENV[p]))
    else:
        try:
            argv = argv_for(cfg, "<workdir>", "<answer>")
            if not argv or not shutil.which(argv[0]):
                err = "%s was not found on the service's PATH" % (argv[0] if argv else "?")
        except ValueError as e:
            err = str(e)
        if not str(cfg.get("server.auth.password_hash") or "").strip():
            err = NO_PASSWORD
    base = cfg.get("annotate.base_url") or DEFAULT_BASE.get(p)
    return {"enabled": bool(cfg.get("annotate.enabled", False)),
            "provider": p, "model": model or None,
            # where it goes, for a reader deciding whether to press the button
            "endpoint": base if p in ("anthropic", "openai") else None,
            "available": err is None, "error": err,
            "auto": bool(cfg.get("annotate.auto", False)),
            "max_calls": int(cfg.get("annotate.max_calls", 400)),
            "kinds": list(KINDS), "prompt_version": PROMPT_VERSION}


def label_of(cfg):
    """Who wrote an answer: `provider:model`, as the page names it."""
    return "%s:%s" % (provider_of(cfg), model_of(cfg) or "default")


def ident_of(cfg):
    """What an answer is kept under: who wrote it, and a digest of the prompt
    it was written to -- the site's own when it has one -- so a changed prompt
    is asked afresh. ValueError when the prompt file cannot be read."""
    digest = hashlib.sha256(system_text(cfg).encode()).hexdigest()[:12]
    return "%s|%s" % (label_of(cfg), digest)


# ------------------------------------------------------------- the calls
def calls_in(body, cap=400):
    """The tool calls in one inspector body (proctree's, as served): every
    distinct command line a tool-call shell or sandbox ran, in the order they
    first started, with the folder each first ran in. Groups add their sample
    lines and their drawn members. `(calls, total)`: the first `cap`, and how
    many distinct lines there were."""
    seen = {}

    def add(cmd, cwd, t):
        if not cmd:
            return
        cur = seen.get(cmd)
        if cur is None or (t is not None and (cur[1] is None or t < cur[1])):
            seen[cmd] = (cwd if cur is None else cur[0] or cwd, t)

    def walk(e, cwd):
        if not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "p":
            cwd = e.get("cwd") or cwd
            add(e.get("cmdline"), cwd, e.get("start"))
            for c in e.get("kids") or ():
                walk(c, cwd)
        elif k == "g":
            for s in e.get("samples") or ():
                add(s.get("cmdline"), cwd, e.get("start"))
            for c in e.get("items") or ():
                walk(c, cwd)

    walk(body.get("tree"), None)
    order = sorted(seen.items(), key=lambda kv: (kv[1][1] is None, kv[1][1] or 0))
    return [{"cmd": c, "cwd": v[0]} for c, v in order[:cap]], len(order)


def prompt_for(calls, agent_type=None):
    lines = []
    for i, c in enumerate(calls, 1):
        item = {"id": str(i), "cmd": c["cmd"][:CMD_MAX]}
        if c.get("cwd"):
            item["cwd"] = c["cwd"]
        lines.append(json.dumps(item, ensure_ascii=False))
    who = ("The agent is %s." % agent_type) if agent_type else ""
    return ("%s The calls, one JSON object a line:\n%s\n" % (who, "\n".join(lines))).strip()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.M)


def parse(text, n):
    """`{index: (purpose, kind)}` from a model's answer, for ids 1..n. An id
    the answer leaves out stays unlabelled; nothing is filled in for it."""
    if not text:
        return {}
    t = _FENCE.sub("", text.strip())
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        obj = json.loads(t[a:b + 1])
    except ValueError:
        return {}
    if isinstance(obj, dict) and isinstance(obj.get("calls"), (dict, list)):
        obj = obj["calls"]
    if isinstance(obj, list):
        obj = {str(x.get("id")): x for x in obj if isinstance(x, dict)}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        try:
            i = int(str(k).strip())
        except ValueError:
            continue
        if not 1 <= i <= n:
            continue
        if isinstance(v, str):
            purpose, kind = v, "other"
        elif isinstance(v, dict):
            purpose, kind = v.get("purpose"), v.get("kind")
        else:
            continue
        if not isinstance(purpose, str) or not purpose.strip():
            continue
        purpose = " ".join(redact.redact(purpose).split())
        if len(purpose) > PURPOSE_MAX:
            purpose = purpose[:PURPOSE_MAX - 1] + "…"
        kind = kind.strip().lower() if isinstance(kind, str) else "other"
        out[i - 1] = (purpose, kind if kind in KINDS else "other")
    return out


# --------------------------------------------------------------- providers
class ProviderError(RuntimeError):
    pass


def _post(url, headers, payload, timeout, tries=3):
    """One JSON POST, retried on 429 and 5xx (after `retry-after` when given)."""
    data = json.dumps(payload).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers=dict(headers, **{"content-type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if (e.code == 429 or e.code >= 500) and attempt + 1 < tries:
                try:
                    wait = float(e.headers.get("retry-after") or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(min(30.0, wait or 2.0 * (attempt + 1)))
                continue
            raise ProviderError("%s answered %d: %s" % (url, e.code, body.strip() or e.reason))
        except urllib.error.URLError as e:
            if attempt + 1 < tries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ProviderError("could not reach %s: %s" % (url, e.reason))
        except (TimeoutError, OSError) as e:
            if attempt + 1 < tries:
                continue
            raise ProviderError("%s: %s" % (url, e))
    raise ProviderError("%s: no answer" % url)


def _anthropic(cfg, system, user, timeout):
    base = (cfg.get("annotate.base_url") or DEFAULT_BASE["anthropic"]).rstrip("/")
    url = base if base.endswith("/messages") else base + "/v1/messages"
    payload = {"model": model_of(cfg), "max_tokens": int(cfg.get("annotate.max_tokens", 8000)),
               "system": system, "messages": [{"role": "user", "content": user}]}
    effort = (cfg.get("annotate.effort", "low") or "").strip().lower()
    if effort in EFFORTS:
        payload["output_config"] = {"effort": effort}
    headers = {"x-api-key": api_key(cfg) or "", "anthropic-version": ANTHROPIC_VERSION}
    r = _post(url, headers, payload, timeout)
    if r.get("stop_reason") == "refusal":
        raise ProviderError("the model declined to answer (%s)"
                            % ((r.get("stop_details") or {}).get("category") or "refusal"))
    text = "".join(b.get("text") or "" for b in r.get("content") or ()
                   if isinstance(b, dict) and b.get("type") == "text")
    if not text and r.get("stop_reason") == "max_tokens":
        raise ProviderError("the answer was cut at annotate.max_tokens; lower annotate.batch_size")
    return text


def _openai(cfg, system, user, timeout):
    base = (cfg.get("annotate.base_url") or DEFAULT_BASE["openai"]).rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {"model": model_of(cfg),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if cfg.get("annotate.max_tokens"):
        payload["max_tokens"] = int(cfg.get("annotate.max_tokens"))
    key = api_key(cfg)
    headers = {"authorization": "Bearer " + key} if key else {}
    r = _post(url, headers, payload, timeout)
    try:
        return r["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ProviderError("unexpected answer from %s: %s" % (url, json.dumps(r)[:300]))


def _cli(cfg, system, user, timeout):
    # As Ask's agent: a directory of its own in the system's temporary one, not
    # beside the salt and the session key in cache.dir (0700, removed after),
    # and only part of the service's environment.
    work = tempfile.mkdtemp(prefix="rc-dash-annotate-")
    answer_file = os.path.join(work, "answer.txt")
    try:
        argv = argv_for(cfg, work, answer_file)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=work, env=ask.agent_env(cfg),
                                text=True, start_new_session=True)
        killer = threading.Timer(timeout, lambda: ask._kill(proc))
        killer.start()
        try:
            out, err = proc.communicate(system + "\n\n" + user)
        finally:
            killer.cancel()
        text = None
        if _uses_file(cfg) and os.path.exists(answer_file):
            with open(answer_file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        if not text:
            text = out
        if proc.returncode != 0 and not (text or "").strip():
            if proc.returncode in (-signal.SIGKILL, -signal.SIGTERM):
                raise ProviderError("%s timed out after %d s" % (argv[0], timeout))
            raise ProviderError("%s exited %d: %s%s" % (argv[0], proc.returncode,
                                                        (err or "").strip()[-400:], ask.ENV_HINT))
        return text or ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def complete(cfg, system, user, timeout):
    """One prompt to the configured provider; the answer's text."""
    p = provider_of(cfg)
    if p == "anthropic":
        return _anthropic(cfg, system, user, timeout)
    if p == "openai":
        return _openai(cfg, system, user, timeout)
    return _cli(cfg, system, user, timeout)


def system_text(cfg):
    path = cfg.get("annotate.system_prompt_file") or ""
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as e:
            raise ValueError("annotate.system_prompt_file %s: %s" % (path, e.strerror or e))
    return (cfg.get("annotate.system_prompt") or DEFAULT_SYSTEM).strip()


# ----------------------------------------------------------- the annotator
class Annotator:
    """Answers kept on disk, and the jobs that ask for more."""

    KEEP_JOBS = 50

    def __init__(self, cfg, complete_fn=None):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.jobs = collections.OrderedDict()
        self.slots = threading.BoundedSemaphore(max(1, int(cfg.get("annotate.max_concurrent", 1))))
        self._known = None
        self._complete = complete_fn or complete
        # a command asked about by one job is not asked about again by another:
        # (label, command) -> the id of the job asking, which a later request
        # for the same command follows instead
        self._inflight = {}

    # ------------------------------------------------------------- cache
    def _path(self):
        return os.path.join(self.cfg.get("cache.dir") or tempfile.gettempdir(),
                            "annotate", "purposes.jsonl")

    @staticmethod
    def key(ident, cmd):
        h = hashlib.sha256(("%s\0%d\0%s" % (ident, PROMPT_VERSION, cmd)).encode()).hexdigest()
        return h[:32]

    def _table(self):
        """key -> [purpose, kind, label, at]; read from disk once. Lock held."""
        if self._known is None:
            self._known = {}
            cap = int(self.cfg.get("annotate.cache_max", 200000))
            try:
                with open(self._path(), encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                            self._known[r["k"]] = [r["p"], r.get("c") or "other", r.get("m"),
                                                   r.get("t")]
                        except (ValueError, KeyError, TypeError):
                            continue
            except OSError:
                pass
            self._bound()
        return self._known

    # Past the cap, the oldest answers go down to this share of it, and the
    # file is written anew: once per tenth of the cap, not on every batch.
    CACHE_SLACK = 0.9

    def _bound(self):
        """Hold the table (and the file) within `annotate.cache_max`, the
        oldest answers going first. Lock held."""
        cap = max(1, int(self.cfg.get("annotate.cache_max", 200000)))
        if len(self._known) <= cap:
            return
        keep = max(1, int(cap * self.CACHE_SLACK))
        for k in list(self._known)[:len(self._known) - keep]:
            del self._known[k]
        if self.cfg.get("annotate.cache", True):
            self._rewrite()

    def _rewrite(self):
        path = self._path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for k, (p, c, m, t) in self._known.items():
                fh.write(json.dumps({"k": k, "p": p, "c": c, "m": m, "t": t}) + "\n")
        os.replace(tmp, path)

    def _keep(self, rows):
        """Store new answers. Lock held."""
        if not rows or not self.cfg.get("annotate.cache", True):
            return
        path = self._path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            for k, (p, c, m, t) in rows:
                fh.write(json.dumps({"k": k, "p": p, "c": c, "m": m, "t": t}) + "\n")

    def known(self, cmds):
        """`{cmd: [purpose, kind]}` for each command already answered by the
        configured model under this prompt."""
        try:
            ident = ident_of(self.cfg)
        except ValueError:
            return {}
        with self.lock:
            table = self._table()
            out = {}
            for c in cmds:
                hit = table.get(self.key(ident, c))
                if hit:
                    out[c] = [hit[0], hit[1]]
            return out

    # -------------------------------------------------------------- jobs
    def submit(self, calls, agent_type=None, about=None):
        """Ask about every call not answered yet. Returns the job's view, or
        None when every call is answered or being answered already: the jobs
        answering them are `following`'s."""
        info = describe(self.cfg)
        if not info["enabled"]:
            raise ValueError("annotation is turned off (annotate.enabled)")
        if not info["available"]:
            raise ValueError(info["error"] or "the annotator cannot run here")
        ident = ident_of(self.cfg)       # an unreadable prompt file fails here
        label = label_of(self.cfg)
        with self.lock:
            table = self._table()
            todo = [c for c in calls if self.key(ident, c["cmd"]) not in table
                    and (ident, c["cmd"]) not in self._inflight]
            if not todo:
                return None
            jid = uuid.uuid4().hex[:12]
            for c in todo:
                self._inflight[(ident, c["cmd"])] = jid
            job = {"id": jid, "state": "queued", "about": about,
                   "provider": info["provider"], "model": info["model"], "label": label,
                   "ident": ident,
                   "total": len(todo), "done": 0, "unlabelled": 0,
                   "submitted": time.time(), "started": None, "finished": None,
                   "error": None, "results": {}}
            self.jobs[job["id"]] = job
            # Only an ended job is let go: a queued or running one is what its
            # readers poll and what `following` names, however many are waiting.
            over = len(self.jobs) - self.KEEP_JOBS
            for jid in [j for j, v in self.jobs.items() if v["state"] in ("done", "error")]:
                if over <= 0:
                    break
                del self.jobs[jid]
                over -= 1
        threading.Thread(target=self._run, args=(job, todo, agent_type), daemon=True,
                         name="annotate-%s" % job["id"]).start()
        return self.view(job["id"])

    def following(self, calls, exclude=None):
        """The ids of the jobs still asking about any of `calls`, oldest first,
        but `exclude`: a reader whose calls another request is already asking
        about follows that request's job rather than seeing nothing."""
        try:
            ident = ident_of(self.cfg)
        except ValueError:
            return []
        with self.lock:
            ids = {self._inflight.get((ident, c["cmd"])) for c in calls}
            return [j for j in self.jobs if j in ids and j != exclude]

    def view(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            if j is None:
                return None
            out = dict(j, results=dict(j["results"]))
        end = out["finished"] or time.time()
        out["elapsed_s"] = round(end - (out["started"] or out["submitted"]), 1)
        return out

    def _run(self, job, todo, agent_type):
        label, ident = job["label"], job["ident"]
        size = max(1, int(self.cfg.get("annotate.batch_size", 40)))
        timeout = int(self.cfg.get("annotate.timeout_s", 120))
        try:
            with self.slots:
                with self.lock:
                    job.update(state="running", started=time.time())
                system = system_text(self.cfg)
                for i in range(0, len(todo), size):
                    batch = todo[i:i + size]
                    text = self._complete(self.cfg, system, prompt_for(batch, agent_type), timeout)
                    got = parse(text, len(batch))
                    now = time.time()
                    rows = []
                    with self.lock:
                        table = self._table()
                        for j, c in enumerate(batch):
                            self._inflight.pop((ident, c["cmd"]), None)
                            if j not in got:
                                job["unlabelled"] += 1
                                continue
                            p, k = got[j]
                            key = self.key(ident, c["cmd"])
                            table[key] = [p, k, label, now]
                            rows.append((key, (p, k, label, now)))
                            job["results"][c["cmd"]] = [p, k]
                        job["done"] += len(batch)
                        self._keep(rows)
                        self._bound()
            with self.lock:
                job.update(state="done", finished=time.time())
        except Exception as e:
            with self.lock:
                job.update(state="error", error=str(e), finished=time.time())
        finally:
            with self.lock:
                for c in todo:
                    self._inflight.pop((ident, c["cmd"]), None)
