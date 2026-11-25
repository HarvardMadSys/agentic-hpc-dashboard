#!/usr/bin/env python3
"""rsync_guard.py: the one command the copy's key may run on a node.

The dashboard's host copies each node's collector files with deploy/fleet/pull.py, which
runs rsync over ssh with a key of its own. On the node, that key's line in
~/.ssh/authorized_keys names this script as its forced command:

    restrict,from="<the host>",command="/usr/bin/python3 <SENDER_DIR>/current/rsync_guard.py /scratch/ebpfm" ssh-ed25519 AAAA...

so sshd runs it in place of whatever the key asked for, which sshd puts in
SSH_ORIGINAL_COMMAND. One shape is let through, rsync sending (reading) files from under
the folders named after the script, the ROOTs:

    rsync --server --sender <options> . <path> [<path> ...]

It is checked, then run as it was asked, with no shell between (os.execv). Anything else is
refused with one line on stderr, which pull.py records as the pass's error, and exit 1:

  * not `rsync --server --sender`: a shell or any other command, rsync receiving (which
    would write here), or rsync's daemon;
  * an option that deletes or removes files (--delete*, --remove-source-files), writes or
    runs anything on the node (--log-file, --write-batch, --temp-dir, --partial-dir,
    --backup-dir, --rsh, --rsync-path, ...), follows links out of the ROOTs (-L, -k, -K,
    --copy-unsafe-links), or carries the paths past this check (-s, --protect-args: rsync
    sends them once it runs); and any other option not in SHORT, FLAGS or WITH_VALUE below,
    which are what pull.py sends and the read-only ones a stock rsync client may add (as
    rsync's own support/rrsync allows them with -ro);
  * a path that is not a full path, holds `..`, a wildcard or a backslash, or does not
    lead, its links followed, into a ROOT. A ROOT not there yet is no refusal: rsync says
    so, as pull.py expects of a node whose collector has written nothing.

What it cannot see: the names that --files-from=- sends over rsync's own connection once
rsync runs. rsync's sender takes each as a name under the source folder, `..` and a
leading `/` dropped, so the one way out of the ROOTs would be a link inside them, which
only what writes them (the collector, as root) can make.

    rsync_guard.py [--rsync PATH] ROOT [ROOT ...]

--rsync names the rsync to run; by default the first of /usr/bin/rsync,
/usr/local/bin/rsync and /bin/rsync. Exit 2 when it is installed wrong: no ROOT, a ROOT
that is not a full path below /, or no rsync there. Python 3.6 or later, standard library
only, as a node's /usr/bin/python3 has it.
"""
import os
import re
import sys

RSYNCS = ("/usr/bin/rsync", "/usr/local/bin/rsync", "/bin/rsync")

# A short-option word as rsync's client packs it (-tRe.LsfxCIvu): letters, each a choice of
# what the sender reads or how it says so, then rsync's capability string after `e`.
SHORT = frozenset("rtRdlpogDHAXEUNIOJWcnquvxzmC")
SHORT_WORD = re.compile(r"^-(?P<letters>[A-Za-z]*?)(?P<caps>e[0-9]*\.[A-Za-z0-9_]*)?\Z")
SHORT_WHY = {
    "L": "follows links out of the folders this key reads",
    "k": "follows links out of the folders this key reads",
    "K": "follows links on the receiving side, which is not this one",
    "s": "carries the paths past this check (rsync sends them once it runs)",
    "b": "makes backups, which writes on the node",
    "e": "names a command to run",
}

# Long options taken as one word: alone, or with =VALUE, each value checked.
FLAGS = frozenset((
    "append", "list-only", "from0", "recursive", "times", "links", "perms", "owner", "group",
    "specials", "no-specials", "hard-links", "numeric-ids", "one-file-system",
    "no-implied-dirs", "no-r", "no-relative", "no-W", "size-only", "ignore-times",
    "existing", "ignore-existing", "ignore-missing-args", "safe-links", "munge-links",
    "new-compress", "old-compress", "open-noatime", "use-qsort", "stats", "msgs2stderr",
    "no-msgs2stderr",
))
_DIGITS = re.compile(r"^\d+\Z")
_WORD = re.compile(r"^[A-Za-z0-9.,_+-]+\Z")          # no `/`: never a path on the node
WITH_VALUE = {
    "timeout": _DIGITS,
    "files-from": re.compile(r"^-\Z"),                # the list over the connection only
    "bwlimit": _WORD, "block-size": _DIGITS, "checksum-choice": _WORD,
    "checksum-seed": _DIGITS, "compress-choice": _WORD, "compress-level": _WORD,
    "max-alloc": _WORD, "max-size": _WORD, "min-size": _WORD, "modify-window": _WORD,
    "info": _WORD, "debug": _WORD, "stderr": _WORD,
}
# Why some are refused, by name; any other one not taken "is not an option this key takes".
LONG_WHY = (
    (("del", "force", "ignore-errors", "max-delete"), "deletes files"),
    (("log-file", "log-file-format", "write-batch", "only-write-batch", "read-batch",
      "temp-dir", "partial-dir", "partial", "backup", "backup-dir", "suffix", "compare-dest",
      "copy-dest", "link-dest", "mkpath", "inplace", "sparse", "preallocate", "fsync",
      "delay-updates", "super", "fake-super", "chmod", "chown", "usermap", "groupmap",
      "write-devices", "fuzzy"), "writes on the node"),
    (("rsh", "rsync-path", "daemon", "config", "server", "sender"),
     "runs something other than rsync sending"),
    (("copy-links", "copy-dirlinks", "copy-unsafe-links", "keep-dirlinks", "no-munge-links"),
     "follows links out of the folders this key reads"),
    (("copy-devices",), "reads devices"),
    (("protect-args", "secluded-args"),
     "carries the paths past this check (rsync sends them once it runs)"),
    (("exclude-from", "include-from"), "reads a file on the node"),
)
DOT_DOT = re.compile(r"(^|/)\.\.(/|\Z)")


class Refused(Exception):
    pass


class Misinstalled(Exception):
    pass


def _long_why(name, has_value):
    if name.startswith(("delete", "remove")):
        return "deletes or removes files on the node"
    if name in FLAGS:
        return "takes no value"
    if name == "files-from":
        return "reads its list of files from a file on the node: only - (the list over " \
               "the connection) is taken"
    if name in WITH_VALUE:
        return "has a value this key does not take" if has_value else \
            "takes its value in the same word (=VALUE)"
    for names, why in LONG_WHY:
        if name in names:
            return why
    return "is not an option this key takes"


def check_option(word):
    """Refused unless `word`, one option before the `.`, is one this key takes."""
    shown = word if len(word) <= 60 else word[:57] + "..."
    if word.startswith("--"):
        name, eq, value = word[2:].partition("=")
        if (name in FLAGS and not eq) or (eq and name in WITH_VALUE
                                          and WITH_VALUE[name].match(value)):
            return
        raise Refused("%s %s" % (shown, _long_why(name, bool(eq))))
    m = SHORT_WORD.match(word)
    if not m or not (m.group("letters") or m.group("caps")):
        raise Refused("%s is not an option this key takes" % shown)
    for c in m.group("letters"):
        if c not in SHORT:
            raise Refused("-%s (in %s) %s" % (c, shown, SHORT_WHY.get(
                c, "is not an option this key takes")))


def check_path(path, roots):
    """Refused unless `path` is a full path that leads, its links followed, into a root."""
    if not path.startswith("/"):
        raise Refused("%s is not a full path: name the folder from /" % path)
    if any(c in path for c in "*?["):
        raise Refused("%s holds a wildcard: name the folder itself" % path)
    if DOT_DOT.search(path):
        raise Refused("%s holds `..`: name the folder from /" % path)
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return
    raise Refused("%s is outside the folders this key reads (%s)" % (path, " ".join(roots)))


def check(command, roots):
    """The arguments after `rsync` that `command` (SSH_ORIGINAL_COMMAND) asks for, if it
    is rsync sending files from under `roots` (each a real path); raises Refused."""
    if not command or not command.strip():
        raise Refused("this key runs rsync, sending, and nothing else: no shell")
    if any(c in command for c in "\\'\"") or any(ord(c) < 32 and c not in "\t" for c in command):
        raise Refused("a command that needs quoting is none the copy sends")
    words = command.split()
    if words[0] != "rsync":
        raise Refused("this key runs `rsync --server --sender` only, not %s" % words[0][:60])
    if words[1:3] != ["--server", "--sender"]:
        raise Refused("this key reads, and does not write or serve: only "
                      "`rsync --server --sender` runs")
    # The options first: with -s, rsync sends no `.` and no path here at all.
    dot = 3
    while dot < len(words) and words[dot] != ".":
        check_option(words[dot])
        dot += 1
    if dot == len(words):
        raise Refused("no `.` before the paths: not a command rsync's client sends")
    paths = words[dot + 1:]
    if not paths:
        raise Refused("no folder named after the `.`")
    for p in paths:
        check_path(p, roots)
    return words[1:]


def setup(argv):
    """(rsync, roots) from the forced command's own arguments; raises Misinstalled."""
    rsync, roots, args = None, [], list(argv)
    while args:
        a = args.pop(0)
        if a == "--rsync" and args:
            rsync = args.pop(0)
        elif a.startswith("--rsync="):
            rsync = a[len("--rsync="):]
        elif a.startswith("-"):
            raise Misinstalled("%s is not an option of rsync_guard.py" % a)
        else:
            roots.append(a)
    if not roots:
        raise Misinstalled("no folder to read: name it after rsync_guard.py in the key's command")
    for r in roots:
        if not r.startswith("/") or os.path.realpath(r) == "/":
            raise Misinstalled("%s is not a full path below /: name the collector's folder" % r)
    if rsync is None:
        rsync = next((p for p in RSYNCS if os.access(p, os.X_OK)), None)
        if rsync is None:
            raise Misinstalled("no rsync in %s: install it, or name it with --rsync"
                               % ", ".join(RSYNCS))
    elif not (rsync.startswith("/") and os.access(rsync, os.X_OK)):
        raise Misinstalled("--rsync %s is not a full path to a program" % rsync)
    return rsync, [os.path.realpath(r) for r in roots]


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    try:
        rsync, roots = setup(sys.argv[1:] if argv is None else argv)
    except Misinstalled as e:
        sys.stderr.write("rsync_guard.py: installed wrong: %s\n" % e)
        return 2
    try:
        args = check(environ.get("SSH_ORIGINAL_COMMAND"), roots)
    except Refused as e:
        sys.stderr.write("rsync_guard.py: refused: %s\n" % e)
        return 1
    try:
        os.execv(rsync, ["rsync"] + args)
    except OSError as e:
        sys.stderr.write("rsync_guard.py: cannot run %s: %s\n" % (rsync, e.strerror or e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
