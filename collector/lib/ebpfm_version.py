"""The collector's version, for `ebpfm version`, `ebpfm status` and the tracer's
`meta` record (`collector_version`).

In order, the first that answers:

  1. a VERSION file beside ebpfm.sh, stamped at build time (the RPM writes
     `<version>-<release>`, `ebpfm.sh bundle` the version of the copy it packs);
  2. `git describe --always --dirty`, in a checkout of the repository;
  3. `sha256:` and 12 hex digits of a hash over the collector's code files, so
     two copies that differ in any line never share a version.

git is asked without overriding its own safety checks: as root in a checkout
another user owns, git refuses, and the hash answers instead. Overriding them
(`safe.directory`) would let that user's repository configuration run commands
as root.

Python 3.6, like the rest of the collector (EL8).
"""
import hashlib
import os
import subprocess
import sys

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_files(code_dir):
    """The files whose bytes are the collector: the entry point, the tracer, the
    node tier and lib/ -- not the tests, the docs or the units."""
    names = ['ebpfm.sh', 'ebpf_trace.py', 'node_snapshot.py']
    lib = os.path.join(code_dir, 'lib')
    try:
        names += sorted('lib/' + n for n in os.listdir(lib) if n.endswith('.py'))
    except OSError:
        pass
    return [n for n in names if os.path.isfile(os.path.join(code_dir, n))]


def code_hash(code_dir=CODE_DIR):
    """`sha256:` and the first 12 hex digits of a hash over each code file's name
    and bytes, in a fixed order."""
    h = hashlib.sha256()
    for name in _code_files(code_dir):
        h.update(name.encode('utf-8') + b'\0')
        try:
            with open(os.path.join(code_dir, name), 'rb') as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except (IOError, OSError):
            h.update(b'unreadable')
    return 'sha256:' + h.hexdigest()[:12]


def _stamp(code_dir):
    try:
        with open(os.path.join(code_dir, 'VERSION'), encoding='utf-8') as fh:
            v = fh.read().strip()
    except (IOError, OSError, UnicodeDecodeError):
        return None
    return v.splitlines()[0].strip() if v else None


def _git(code_dir):
    try:
        r = subprocess.run(['git', '-C', code_dir, 'describe', '--always', '--dirty'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    except (OSError, subprocess.SubprocessError):
        return None
    v = r.stdout.strip()
    return v if r.returncode == 0 and v else None


def collector_version(code_dir=CODE_DIR):
    """(version, source): source is 'stamp', 'git' or 'hash'."""
    v = _stamp(code_dir)
    if v:
        return v, 'stamp'
    v = _git(code_dir)
    if v:
        return v, 'git'
    return code_hash(code_dir), 'hash'


def main(argv):
    """`ebpfm_version.py [CODE_DIR] [--source]`: the version, and with --source
    where it came from."""
    dirs = [a for a in argv[1:] if not a.startswith('--')]
    code_dir = dirs[0] if dirs else CODE_DIR
    v, source = collector_version(code_dir)
    if '--source' in argv:
        print('%s %s' % (v, source))
    else:
        print(v)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
