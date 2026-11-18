# Risk profiles: what counts as risky on this cluster

The **Risk** tab counts commands that should not run on a login node and credentials written
into command lines. What counts as either differs by cluster: `nvidia-smi` is a pointless
question on a GPU-less login node and a sensible one on a GPU box, compute nodes are named
differently everywhere, and a site has its own tools and its own token formats. So the rules
are built in, and each cluster changes them in one place.

| file | for |
|---|---|
| [`example.json`](example.json) | every key, annotated. Copy it, keep what you need, delete the rest |
| [`fasrc.json`](fasrc.json) | FASRC login nodes (`boslogin*`, `holylogin*`): no GPU, compute nodes named `holy*` / `bos*` / `c<NNN>`. `run-fasrc.sh` uses it |
| [`gpu-dev.json`](gpu-dev.json) | a GPU development box that is also the login node: the two "no GPU here" rules switched off |

## Using one

```bash
RC_DASH_RISK_PROFILE=risk-profiles/gpu-dev.json ./run.sh build     # env var
uv run python -m rc_dashboard serve --config my.config.json        # or "risk": {"profile": ...} in the config
```

A relative path resolves against `dashboard/`. The profile is merged over the `risk` section of
the config (the profile wins; objects merge key by key, lists are replaced whole), and the
result is part of the resolved configuration: `--print-config` shows each key with
`"_origin": "profile:<path>"`, and the Risk tab's **Rules in effect** panel lists every rule
with where it came from. A profile that is missing or is not valid JSON **stops the service at
start-up** -- running with rules other than the ones asked for would be worse. A rule inside a
valid profile that is wrong is left out and named on the page, never fatal.

Changing a profile changes what every count means, so saved window builds made with the old
rules are not resumed: the first load after a change re-reads the window.

## The keys

```jsonc
{
  "vars":          { "<name>": "<regex text>" },                  // {{name}} in any pattern
  "dangerous":     { "disable": [...], "override": {...}, "add": [...] },
  "secrets":       { "disable": [...], "override": {...}, "add": [...] },
  "login_compute": { "compute": [...], "transfer": [...] },
  "sections":      [ {"id": ..., "kind": ..., "title": ..., "rules": ...}, ... ]   // the Risk tab, in order
}
```

Any key that starts with `//` is a comment and is ignored, at any depth -- including inside a
rule.

### `vars`

Text substituted into patterns wherever `{{name}}` appears, before the pattern is compiled.
It is regex text, inserted as is: `"holy|bos|c\\d{3,}"` is an alternation, not a literal.

| variable | default | used by |
|---|---|---|
| `compute_hosts` | `holy\|bos\|node\|gpu\|c\d{3,}` | `ssh_compute`: the host an `ssh` goes to (its destination, not the remote command) |

A pattern that names a variable nobody set is left out and reported.

### `dangerous` and `secrets`

| key | value | effect |
|---|---|---|
| `disable` | `["id", ...]` | those built-in rules are not counted |
| `override` | `{"id": {field: value}}` | any of `label`, `severity`, `axis`, `pattern`, `gate` replaced |
| `add` | `[{rule}, ...]` | new rules, counted after the built-ins |

An added rule:

| field | required | default | meaning |
|---|---|---|---|
| `id` | yes | | short, unique, not a built-in id: it names the rule in the payload and the page |
| `pattern` | yes | | a Python regular expression (`re`); `{{vars}}` allowed |
| `label` | | the id | what the page calls it |
| `severity` | | `med` | `HIGH`, `med` or `low`; anything else reads as `med`. Orders the table |
| `axis` | | `policy-unaware` | dangerous only: `policy-unaware` (it works, and the site asks you not to) or `environment-unaware` (it runs, and tells you nothing useful here) |
| `gate` | | none | literals of which at least one appears in **every** line the pattern matches |

**What a rule reads.** A built-in dangerous rule reads the words of the command line, case-
insensitively: the command is the process's own program (or the script Python runs), never a
folder of a path, so `ls /usr/local/go/bin` is no build; a flag is a word that begins with `-`,
and short ones may stand together or apart (`-rf`, `-r -f`), up to `--`; and what a rule asks
of an argument it asks of that argument alone -- the host an `ssh` goes to, not its remote
command, and the mode a `chmod` sets, not a file name with `777` in it. The table below says
what each one asks; the Risk tab shows the same under **Rules in effect**.

A pattern a profile gives -- an `override` with a `pattern`, or an `add` -- is a regex over the
command name and its arguments, `"<command> <args>"`, case-insensitively: `git push --force`
matches `^git\b` and `PIP install` matches `pip`. A secret pattern sees the arguments only,
case-sensitively, because token formats are (`AKIA…` is a key, `akia…` is not). A pattern reads
the first 4,096 characters of a line at most, cut at a word, and the line is anyone's to write:
write it so its time grows with the line, not faster. Anchor it at the command
(`^(?:\S*/)?rm\b`) and put each further condition in a lookahead (`(?=.*\s-…)`), as
`example.json` does; a chain such as `rm\b[^\n]*-rf[^\n]*/scratch/` is tried from every
place it could start, and took 17 s on one crafted line of 4 KB.

**Gates.** Each built-in rule carries a gate: a cheap substring test that skips the rule on
the command lines that cannot match, which took pattern time from 2.1 s to 0.4 s on a day of
one login node's command lines. A gate is only correct for the pattern it was written for, so it is
dropped -- the rule runs on every line -- when a profile replaces the pattern (unless the
override gives a new `gate`), or when a `vars` value changes the pattern. An added rule runs
ungated unless you give a `gate`. **A wrong gate silently hides matches; when in doubt, leave
it out.** A dangerous rule's gate is compared case-insensitively, a secret rule's as written.

**Secrets are masked, not shown.** A credential pattern you add or change is also applied by the
service's redaction (`rc_dashboard/redact.py`) to every command line the page shows -- the Live
stream, Search & export, the process tables -- and the whole match is replaced by
`[REDACTED]`. The credentials panel itself never shows the value or the line around it, only
which pattern fired, how often, and in which tools.

### `login_compute`

Which tool buckets (`rc_dashboard/tool_buckets.py`) count as compute on a login node, and which
as bulk transfer, listed separately and kept out of the compute total.

| key | default |
|---|---|
| `compute` | `["build", "interpreter"]` |
| `transfer` | `["net/data"]` |

Buckets: `poll/probe`, `slurm-submit`, `slurm-query`, `git`, `runtime`, `interpreter`,
`shell/env-init`, `search`, `build`, `net/data`, `files`, `other`. A name that is not one of
these is reported on the page (it would otherwise count nothing).

### `sections`

The Risk tab is built of sections, and which ones a cluster has is its call: a GPU box that is
also the login node has no use for "compute on a login node", and a site may want its
data-movement rules apart from the rest. `sections` is the tab, top to bottom -- one headline
plate and one panel per section, in this order.

| field | required | meaning |
|---|---|---|
| `id` | yes | unique; names the section in the payload |
| `kind` | yes | `login_compute`, `commands` (dangerous-command rules) or `credentials` (credential rules) |
| `title` | | the panel's and the plate's heading; a default per kind |
| `rules` | | `commands` and `credentials` only: a list of rule ids, or `"rest"` (the default) for every enabled rule of that kind no other section lists |

The default is the three the tab always had:

```json
"sections": [
  {"id": "login_compute", "kind": "login_compute", "title": "Compute on a login node"},
  {"id": "commands", "kind": "commands", "rules": "rest", "title": "Commands that should not run here"},
  {"id": "credentials", "kind": "credentials", "rules": "rest", "title": "Credentials on a command line"}
]
```

- **A section left out is not drawn and not computed.** With no `login_compute` section the
  reducer skips its CPU accounting; with no `credentials` section no credential pattern runs.
- **A rule no section shows is not counted** -- a count nobody can see is a cost with no reader.
  Rules in effect lists it as "in no section". Put a `"rest"` section last to catch everything
  you did not place.
- **A rule is in exactly one section**: listing it twice is reported, and the first wins. At
  most one `"rest"` section per kind.
- **Masking credentials is never switched off** by this: a credential pattern is applied to
  every command line the page shows whether or not a section counts it.
- The Overview's "risky agent actions" plate sums only the kinds this cluster counts, and says
  which.

A profile's `sections` replaces the whole list, so a profile that sets it states the full tab.
`gpu-dev.json` drops `login_compute`; `example.json` puts the download rules in a "Data
movement" section of their own ahead of the rest.

## The built-in rules

Dangerous commands -- read by word (above), case-insensitively:

| id | label | matches | axis | severity |
|---|---|---|---|---|
| `nvidia_smi_login` | nvidia-smi (pointless without a local GPU) | the command `nvidia-smi` | environment-unaware | low |
| `nvcc_login` | nvcc compile on a login node | the command `nvcc` | environment-unaware | low |
| `pip_install` | pip install on a login node | the command `pip` (`pip3`, `pip3.11`) with the word `install` | policy-unaware | med |
| `conda_install` | conda/mamba install or create on login | the command `conda`, `mamba` or `micromamba` with the word `install` or `create` | policy-unaware | med |
| `build_login` | compiler or make on a login node | the command `make`, `cmake`, `ninja`, `gcc`, `g++`, `clang`, `clang++`, `nvcc`, `cargo` or `go`, or one of them with a version (`gcc-12`) | policy-unaware | med |
| `ssh_compute` | ssh straight into a compute node | the command `ssh`, the host it goes to matching `{{compute_hosts}}` | policy-unaware | med |
| `server_on_login` | http.server / tensorboard / streamlit on login | `-m http.server`; the command `tensorboard` or `streamlit`; `jupyter lab`, `jupyter notebook` | policy-unaware | med |
| `bulk_download` | wget/curl bulk download on login | the command `wget` or `curl` with `-o` or `-O` (alone, or among short flags: `-sSLO`) or `--output…` | policy-unaware | med |
| `hf_download` | huggingface download on login | the command `huggingface-cli` or `hf` with the word `download` | policy-unaware | med |
| `headless_browser` | headless browser swarm | `chromium`, `chrome` or `msedge` before `--headless` on a line; `playwright` or `puppeteer` as the command or a folder of a path | policy-unaware | low |
| `git_force_push` | git push --force | the command `git`, subcommand `push`, with `--force` or `-f` (alone or among short flags); not `--force-with-lease` | policy-unaware | low |
| `rm_rf_shared` | rm -rf against a shared path | the command `rm` with both `-r` (`-R`, `--recursive`) and `-f` (`--force`), together or apart: any `rm -rf`, whatever the path | policy-unaware | HIGH |
| `chmod_777` | chmod 777 | the command `chmod` with the mode `777` | policy-unaware | med |

Credentials -- matched against the arguments (their first 4,096 characters), case-sensitively:

| id | label | severity |
|---|---|---|
| `openai_key` | OpenAI/Anthropic-style key literal | HIGH |
| `github_pat` | GitHub personal access token | HIGH |
| `hf_token` | HuggingFace token | HIGH |
| `aws_key_id` | AWS access key id | HIGH |
| `slack_token` | Slack token | HIGH |
| `bearer_header` | Authorization: Bearer header | HIGH |
| `api_key_env` | API-key environment assignment | HIGH |
| `token_flag` | --token / --api-key / --secret with an inline value | med |
| `password_flag` | --password with an inline value | med |
| `db_password_short` | database client -p&lt;password&gt; | med |
| `redis_password` | redis-cli -a &lt;password&gt; | med |
| `curl_userpass` | curl -u user:password | med |
| `private_key_inline` | inline private key material | HIGH |

The rules themselves are in `rc_dashboard/reducers/risk.py` (`DANGEROUS` and `_WORD_RULES`,
`SECRETS`), with why each is shaped the way it is, and on the Risk tab under **Rules in effect** as resolved for
this cluster. `tests/test_risk_rules.py` checks that every profile in this directory loads with
no rejected rule and that this table names every built-in.

## Checking a profile before you deploy it

```bash
RC_DASH_RISK_PROFILE=risk-profiles/mysite.json uv run python -m rc_dashboard --print-config | grep -A3 '"risk"'
RC_DASH_RISK_PROFILE=risk-profiles/mysite.json uv run python -c \
  'from rc_dashboard import config; from rc_dashboard.reducers.risk import ruleset
r = ruleset(config.load()).summary()
print({k: r[k] for k in ("disabled", "changed", "added", "invalid", "unknown")})'
```

`invalid` and `unknown` should both be empty: an `invalid` rule did not compile or names an
unset variable (or a section has a bad kind, a duplicate id, a second `"rest"`, or a rule
already placed), an `unknown` id is a `disable`, `override` or section `rules` entry naming no
rule (or a bucket name that does not exist). Add `"unsectioned"` to the keys printed to see the
rules no section shows.
