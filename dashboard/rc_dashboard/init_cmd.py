"""`rc-dashboard init`: a first config, a password, and what to run next.

It looks where the collector writes -- the default candidates, the folders
/etc/default/ebpfm names (feeds.candidates) -- and whether this host runs Slurm
(`sbatch` on PATH, or /etc/slurm), asks for each value with what it found as the
answer given (`--yes` asks nothing: each value from its flag, else what was
found), makes a password as `new-password --install` does, with its hash in a
hash file beside it, writes a short TOML config, checks the feeds as
`check-feeds` does, and prints the `serve` command and the `ssh -L` tunnel
command to run next.

It never replaces a config without `--force`, and never writes a default config
file beside the other one (dashboard.toml beside dashboard.config.json), which
the service would refuse to choose between. A password already there is kept,
its hash file written from it when missing, unless `--force`.
"""
import datetime
import getpass
import os
import shutil
import socket
import sys

from . import auth
from . import config as configmod
from . import feeds as feedmod


class Refused(Exception):
    pass


def default_config_path():
    """dashboard/dashboard.toml in a checkout; installed without one,
    ~/.config/rc-dashboard/dashboard.toml (which `serve` is then told with
    --config)."""
    if configmod.CHECKOUT:
        return os.path.join(configmod.HERE, "dashboard.toml")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "rc-dashboard", "dashboard.toml")


def slurm_here():
    """Whether this host runs Slurm: `sbatch` on PATH, or /etc/slurm."""
    return bool(shutil.which("sbatch")) or os.path.isdir("/etc/slurm")


def found_roots(cands, days=8):
    """The candidates that hold day-files, else those that are readable folders."""
    probed = feedmod.candidates(cands, days)
    found = [e["path"] for e in probed if e["_read"] and e["state"] == "found"]
    return found or [e["path"] for e in probed if e["_read"]], probed


class Asker:
    """Each question with its answer given; `--yes` asks none."""

    def __init__(self, yes, out=None, read=input):
        self.yes = yes
        self.out = out or sys.stdout
        self.read = read

    def ask(self, question, given):
        if self.yes:
            return given
        try:
            got = self.read("%s [%s]: " % (question, given if given not in (None, "") else "none"))
        except EOFError:
            raise Refused("init asks its questions on a terminal; with no one to answer, give "
                          "--yes and the values as flags (rc-dashboard init --help)")
        got = got.strip()
        return given if not got else got

    def yes_no(self, question, given):
        if self.yes:
            return given
        while True:
            got = self.ask(question + " (y/n)", "y" if given else "n").lower()
            if got in ("y", "yes"):
                return True
            if got in ("n", "no"):
                return False
            print("  y or n, please", file=self.out)


def _paths(text):
    """`a, b` as ["a", "b"]; "none" or "-" as []."""
    if isinstance(text, list):
        return text
    if text is None or str(text).strip().lower() in ("", "none", "-"):
        return []
    return [p.strip() for p in str(text).split(",") if p.strip()]


def toml_config(site, ebpf, node, slurm, port, hash_file, when, host):
    q = configmod._toml_value
    lines = ["# Written by `rc-dashboard init` on %s, on %s." % (when, host),
             "# The settings a site chooses, each with its default and a line of help:",
             "# `rc-dashboard print-config --reference`. A flag or an RC_DASH_* variable",
             "# beats what is set here.",
             "",
             "[site]",
             "name = %s" % q(site)]
    if ebpf:
        lines += ["", "[feeds.ebpf]",
                  "roots = %s   # the collector's EBPFM_OUTPUT_DIR; a glob may name many"
                  % q(ebpf)]
    if node:
        lines += ["", "[feeds.ebpf_node]",
                  "roots = %s   # its EBPFM_NODE_OUTPUT_DIR" % q(node)]
    lines += ["", "[slurm]", "enabled = %s" % q(bool(slurm)),
              "", "[server]", "port = %d" % port,
              "", "[server.auth]",
              "password_hash_file = %s   # `rc-dashboard new-password --install` rewrites it"
              % q(hash_file)]
    return "\n".join(lines) + "\n"


def serve_command(path):
    """The exact command that serves with the config at `path`."""
    default = path == os.path.join(configmod.HERE, "dashboard.toml")
    flag = "" if default and configmod.CHECKOUT else " --config %s" % _quote(path)
    if configmod.CHECKOUT:
        return "cd %s && uv run --frozen rc-dashboard serve%s" % (_quote(configmod.HERE), flag)
    exe = shutil.which("rc-dashboard")
    if exe:
        return "%s serve%s" % (_quote(exe), flag)
    return "%s -m rc_dashboard serve%s" % (_quote(sys.executable), flag)


def _quote(s):
    import shlex
    return shlex.quote(s)


def main(args, read=input, out=None):
    out = out or sys.stdout
    try:
        return _main(args, Asker(args.yes, out, read), out)
    except Refused as e:
        print("init: %s" % e, file=sys.stderr)
        return 2


def _main(args, asker, out):
    def say(*a):
        print(*a, file=out)

    # -- where the config goes, refused before a question more is asked
    path = os.path.abspath(os.path.expanduser(
        args.config or asker.ask("Write the config to", default_config_path())))
    _refuse_config(path, args.force)

    # -- where the collector writes
    ebpf_found, ebpf_probed = found_roots(configmod._ebpf_candidates())
    node_found, node_probed = found_roots(configmod._node_candidates())
    say("Looked for the collector's eBPF tier in:")
    for e in ebpf_probed:
        say("  %-40s %s" % (e["path"], feedmod.candidate_line(e)))
    say("and for its node tier in:")
    for e in node_probed:
        say("  %-40s %s" % (e["path"], feedmod.candidate_line(e)))
    say("")

    ebpf = _paths(asker.ask("The collector's eBPF tier folder(s), comma-separated "
                            "(none: look in the places above)",
                            ", ".join(args.ebpf_root or ebpf_found[:1])))
    node = _paths(asker.ask("Its node tier folder(s), comma-separated (none: look in the "
                            "places above)", ", ".join(args.node_root or node_found[:1])))
    host = socket.gethostname().split(".")[0]
    site = asker.ask("What every page calls this deployment", args.site_name or host)
    slurm = asker.yes_no("Does this cluster run Slurm (the HPC tab)?",
                         slurm_here() if args.slurm is None else args.slurm)
    port = args.port or 8080
    if not args.yes and not args.port:
        port = _port(asker.ask("The port to serve on, on 127.0.0.1", str(port)))
    pwdir = os.path.abspath(os.path.expanduser(args.password_dir or auth.DEFAULT_DIR))
    hash_file = os.path.join(pwdir, "password.hash")

    # -- the password
    pw_file = os.path.join(pwdir, "password")
    if os.path.exists(pw_file) and not args.force:
        if not os.path.exists(hash_file):
            with open(pw_file) as fh:
                auth.write_hash_file(hash_file, auth.hash_password(fh.read().strip()))
            say("Kept the password in %s, and wrote its hash to %s." % (pw_file, hash_file))
        else:
            auth.read_hash_file(hash_file)          # one that parses, or stop here
            say("Kept the password in %s, its hash in %s (--force makes a new one)."
                % (pw_file, hash_file))
    else:
        try:
            auth.new_password(pwdir, (), args.force, hash_file=hash_file)
        except FileExistsError as e:
            raise Refused(str(e))
        say("Made a password: %s (mode 600), a netrc for curl beside it, and its hash in %s."
            % (pw_file, hash_file))

    # -- the config
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    text = toml_config(site, ebpf, node, slurm, port, hash_file, when, host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    say("Wrote %s." % path)
    say("")

    # -- what the service will find
    try:
        cfg = configmod.load(None, path)
    except SystemExit as e:
        raise Refused("the config just written does not load: %s" % e)
    reports = feedmod.resolve(cfg)
    code = _table(reports, out)
    say("")
    if code:
        say("! The eBPF feed is not readable yet (see above): the service starts, and reads "
            "the folder once it is there and readable.")
    static = cfg.get("server.static_dir")
    say("Next:")
    if not os.path.isfile(os.path.join(static or "", "index.html")):
        say("  build the page once:   cd %s && npm ci && npm run build"
            % _quote(os.path.join(configmod.HERE, "web")))
    say("  start the service:     %s" % serve_command(path))
    user = getpass.getuser()
    fqdn = socket.getfqdn() or host
    say("  then, on your own machine, a tunnel to it:")
    say("                         ssh -N -L %d:127.0.0.1:%d %s@%s" % (port, port, user, fqdn))
    say("  and open http://127.0.0.1:%d, signing in with the password in %s." % (port, pw_file))
    return 0


def _refuse_config(path, force):
    """Refuse a config already there (without --force), and a default config file
    beside the other default one, which the service would refuse to choose
    between (config.find_config_file)."""
    if os.path.exists(path) and not force:
        raise Refused("%s already exists; pass --force to replace it" % path)
    here = os.path.abspath(configmod.HERE)
    if os.path.dirname(path) == here and os.path.basename(path) in configmod.DEFAULT_FILES:
        others = [os.path.join(here, n) for n in configmod.DEFAULT_FILES
                  if os.path.join(here, n) != path and os.path.exists(os.path.join(here, n))]
        if others:
            raise Refused("%s exists, and the service would refuse to choose between it and "
                          "%s: move it away first, or write this one elsewhere with --config"
                          % (others[0], path))


def _port(text):
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise Refused("%r is not a port" % text)
    return port


def _table(reports, out):
    """print_table's lines, to `out`."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = feedmod.print_table(reports)
    out.write(buf.getvalue())
    return code
