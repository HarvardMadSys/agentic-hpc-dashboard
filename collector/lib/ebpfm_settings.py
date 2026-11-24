"""Every setting the collector reads, in one table: its kind, default, tier and
meaning.

The table drives:
  * the check ebpfm.sh makes before a command that runs something (`start`,
    `once`, `install`, the node tier's loop, ...): a value it cannot use refuses
    the command, with exit status 2 and the reason, instead of a loop that spins
    or a tier that writes nothing;
  * `ebpfm config`: each setting, its value, its default and where the value came
    from (the environment, the settings file and its line, or the default), and
    any EBPFM_*/SNAPSHOT_* name that is not a setting, with the nearest one;
  * the commented defaults file (packaging/rpm/ebpfm.default, and what
    `ebpfm install` writes where there is no /etc/default/ebpfm);
  * the settings table in OPERATIONS.md.
Tests hold the table to the code that reads each setting and to those files.

The settings file is read by ebpfm.sh itself (environment > file > default),
which passes what it found here as arguments; this module never reads it.

Python 3.6, like the rest of the collector (EL8).
"""
import difflib
import json
import os
import re
import subprocess
import sys

SECTIONS = []          # (key, title, blurb), in order
SETTINGS = []          # dicts, in order
_BY_NAME = {}
_SECTION = [None]


def _section(key, title, blurb):
    SECTIONS.append((key, title, blurb))
    _SECTION[0] = key


def _s(name, kind, default, doc, unit=None, minimum=None, choices=None, tier='ebpf',
       file_default=None, dynamic=False, in_file=True, empty='error', pow2=False):
    """One setting.

    kind      int | float | bool | enum | mode | group | path | list | redact | features | str
    default   what applies when it is unset, as shown to a person
    unit      the word a message uses for a number (seconds, milliseconds, bytes, ...)
    tier      ebpf | node | both | cli: who reads it
    file_default  the value the commented line in the defaults file shows
    dynamic   the default depends on the host or the user (ebpfm.sh passes it)
    in_file   listed in the defaults file (False: read from the environment only)
    empty     what an empty value means: 'error', 'unset' (the default applies)
              or 'ok' (empty has a meaning of its own)
    """
    s = dict(name=name, kind=kind, default=default, doc=doc, unit=unit, min=minimum,
             choices=choices, tier=tier, section=_SECTION[0], dynamic=dynamic,
             in_file=in_file, empty=empty, pow2=pow2,
             file_default=default if file_default is None else file_default)
    SETTINGS.append(s)
    _BY_NAME[name] = s


# ---------------------------------------------------------------------------
_section('where', 'Where it runs and writes',
         'Read by ebpfm.sh, and by both tiers where noted.')
_s('EBPFM_TIER', 'enum', 'all as root, node otherwise',
   'Which tiers `start`, `stop`, `status`, `check` and `once` act on: node, ebpf or all.',
   choices=('node', 'ebpf', 'all'), tier='cli', file_default='all', dynamic=True,
   empty='unset')
_s('EBPFM_OUTPUT_DIR', 'path',
   'as root /scratch/ebpfm where /scratch is writable, else /var/log/ebpfm; as a user ~/.ebpfm/ebpf',
   "The eBPF tier's day-files: `<dir>/<YYYY-MM-DD>/<host>.{snapshot,exits}.jsonl`. "
   'An installed unit sets the folder it was installed with; this file overrides it.',
   tier='ebpf', file_default='/scratch/ebpfm', dynamic=True, empty='unset')
_s('EBPFM_NODE_OUTPUT_DIR', 'path',
   'as root /scratch/ebpfm-login where /scratch is writable, else /var/log/ebpfm-login; as a user ~/.ebpfm/login',
   "The node tier's day-files: `<dir>/<YYYY-MM-DD>/<host>.jsonl`.",
   tier='node', file_default='/scratch/ebpfm-login', dynamic=True, empty='unset')
_s('EBPFM_PID_DIR', 'path', '/run as root, ~/.run otherwise',
   'Where `start` keeps its pid files, and each running tier a note of its settings '
   'that `status`, `stop` and `tail` read.',
   tier='cli', file_default='/run', dynamic=True, empty='unset')
_s('EBPFM_DIR_MODE', 'mode', '0750',
   'Mode of the folders both tiers create. 0 or empty: leave the umask\'s.',
   tier='both', empty='ok')
_s('EBPFM_FILE_MODE', 'mode', '0640',
   'Mode of the day-files both tiers create. 0 or empty: leave the umask\'s.',
   tier='both', empty='ok')
_s('EBPFM_GROUP', 'group', '(none)',
   'A group (name or gid) given every folder and day-file, so its members can read them. '
   'The output names real users and what they ran.',
   tier='both', file_default='', empty='ok')
_s('EBPFM_UNIT_DIR', 'path', '/etc/systemd/system',
   'Where `install` and `install-unit` write ebpfm.service and ebpfm-node.service.',
   tier='cli', empty='unset')
_s('EBPFM_DEFAULT_FILE', 'path', '/etc/default/ebpfm',
   'The settings file ebpfm.sh reads, as the units read it. Empty: read none. '
   'From the environment only.',
   tier='cli', in_file=False, empty='ok')
_s('EBPFM_HOST', 'str', 'the short host name (hostname -s)',
   'The host name both tiers write their day-files under, and every command looks for. '
   'Set it in a container (-e EBPFM_HOST=$(hostname -s)), whose own name is not the node\'s.',
   tier='both', file_default='', dynamic=True, empty='unset')

_section('node', 'Node tier',
         'node_snapshot.py, run by ebpfm.sh every EBPFM_NODE_INTERVAL seconds.')
_s('EBPFM_NODE_INTERVAL', 'int', '300',
   'Seconds between node snapshots. A snapshot is cut short 10 s before the next is due.',
   unit='seconds', minimum=30, tier='node')
_s('EBPFM_NODE_TOP_N', 'int', '25',
   'Rows kept in each per-process and per-user table of a snapshot.',
   unit='a whole number', minimum=1, tier='node')
_s('SNAPSHOT_MODE', 'enum', 'node beside the eBPF tier, full without it',
   'full: every section. node: leave out the four per-process sections the eBPF tier '
   'collects properly (top_io_processes, top_sleeping_procs, sleeping_wchans, socket_talkers).',
   choices=('full', 'node'), tier='node', file_default='node', dynamic=True, empty='unset')
_s('SNAPSHOT_ARGS_MAXLEN', 'int', '256',
   'Characters of each command line a snapshot keeps, after masking.',
   unit='characters', minimum=1, tier='node')
_s('SNAPSHOT_CMD_TIMEOUT', 'float', '20',
   'Seconds each tool a snapshot runs (ps, w, mpstat, ...) may take before its section is left empty.',
   unit='seconds', minimum=1, tier='node')
_s('SNAPSHOT_PROC_TREE', 'bool', '1',
   'The whole-host process tree (pid, ppid, command, agent tag). 0 leaves it out.',
   tier='node')
_s('SNAPSHOT_D_STACK_MAX', 'int', '300',
   'Processes in D state whose kernel stack and open files a snapshot reads (as root).',
   unit='a whole number', minimum=0, tier='node')
_s('SNAPSHOT_PROBE_NFS_PATH', 'path', '(off)',
   'A file on NFS that each snapshot reads one block of, to time an NFS read '
   '(responsiveness.nfs_read_ms).',
   tier='node', file_default='', empty='unset')

_section('masking', 'Masking',
         'What both tiers mask in the command lines they write.')
_s('EBPFM_REDACT', 'redact', 'keys,prompts',
   'keys: credentials become [REDACTED]; prompts: the prompt handed to a coding agent '
   'becomes [REDACTED-PROMPT]. 0 (or none, off) writes command lines raw. A value that '
   'cannot be read masks both.',
   tier='both', empty='ok')

_section('run', 'eBPF tier: running',
         'ebpf_trace.py, as root.')
_s('EBPFM_DURATION', 'float', '0',
   'Seconds to run, then stop; 0 runs until stopped. `once` defaults to 30.',
   unit='seconds', minimum=0)
_s('EBPFM_RESIDENCY_S', 'float', '60',
   'Seconds between residency ticks (what is running, open sockets, totals); 0: no tick. '
   '`status` calls the tier stale after two ticks without a record.',
   unit='seconds', minimum=0)
_s('EBPFM_POLL_MS', 'int', '100',
   'Milliseconds between drains of the kernel rings.',
   unit='milliseconds', minimum=1)
_s('EBPFM_PERF_PAGES', 'int', '256',
   'Pages per CPU in each kernel ring. More absorbs bigger bursts at the cost of locked memory.',
   unit='pages', minimum=1, pow2=True)
_s('EBPFM_FLUSH_MODE', 'enum', 'poll',
   'poll: one write(2) per stream per poll round. record: one per record.',
   choices=('poll', 'record'))
_s('EBPFM_FLUSH', 'bool', '1',
   'At stop, write every live tracked process as `truncated`. Not EBPFM_FLUSH_MODE.')
_s('EBPFM_COMPRESS', 'bool', '1',
   "Gzip a day's files once the day is over (`<file>.jsonl.gz`). 0 leaves them plain.")
_s('EBPFM_REORDER_MS', 'int', '20',
   'Milliseconds behind kernel time that events are applied, so they apply in order.',
   unit='milliseconds', minimum=0)
_s('EBPFM_EXIT_HOLD_MS', 'int', '250',
   'Milliseconds an exit record waits for the connections that close after the exit. '
   '0 writes it at once, without them.',
   unit='milliseconds', minimum=0)
_s('EBPFM_GC_GRACE', 'float', '300',
   "Seconds an exited process's state is kept.",
   unit='seconds', minimum=0)

_section('argv', 'eBPF tier: command lines and Slurm', '')
_s('EBPFM_ARGS_MAXLEN', 'int', '0',
   'Characters of `args` written; 0: no cap beyond what is read.',
   unit='characters', minimum=0)
_s('EBPFM_ARGV_KMAX', 'int', '4096',
   'Bytes of argv read in the kernel at exec: the real limit when EBPFM_ARGS_MAXLEN is 0.',
   unit='bytes', minimum=1)
_s('EBPFM_WRITE_KMAX', 'int', '1024',
   'Bytes captured from each write() of a Slurm submit tool.',
   unit='bytes', minimum=1)
_s('EBPFM_SUBMIT_COMMS', 'list', 'sbatch,salloc,srun',
   'The tools whose output a `submit` record captures, comma-separated.')
_s('EBPFM_SUBMIT_BUDGET', 'int', '8192',
   'Bytes of a submit tool\'s output captured before capture stops.',
   unit='bytes', minimum=0)

_section('which', 'eBPF tier: which processes', '')
_s('EBPFM_MIN_UID', 'int', '0',
   'Also record processes of uids from this one up with no agent ancestry and no tty '
   '(attribution `uid`). 0: off. Turning it on changes what the human class means: keep 0 '
   'and filter downstream.',
   unit='a uid', minimum=0)
_s('EBPFM_INCLUDE_ROOTS', 'bool', '1',
   "Record an agent's own process. 0: only what it runs.")
_s('EBPFM_MIN_CPU', 'float', '0',
   'Leave out processes that used less CPU than this many seconds.',
   unit='seconds', minimum=0)
_s('EBPFM_MIN_DURATION', 'float', '0',
   'Leave out processes that ran for less than this many seconds.',
   unit='seconds', minimum=0)
_s('EBPFM_KEEP_TIMERS', 'bool', '0',
   'Record a timer (EBPFM_TIMER_COMMS) that is no agent and used no CPU or I/O.')
_s('EBPFM_TIMER_COMMS', 'list', 'sleep,usleep',
   'The commands treated as timers, comma-separated.')

_section('reads', 'eBPF tier: per-process reads', '')
_s('EBPFM_CWD', 'bool', '1', "Read each reported process's working directory.")
_s('EBPFM_CWD_ALWAYS', 'bool', '0',
   'Read the working directory at every exec instead of inheriting it (to validate).')
_s('EBPFM_SANDBOX', 'bool', '1', "Read each reported process's namespaces and confinement.")
_s('EBPFM_SANDBOX_ALWAYS', 'bool', '0',
   'Read the sandbox state at every exec (to validate).')
_s('EBPFM_RESIDENCY_TOPN', 'int', '5',
   'Heaviest processes listed per agent tree in a residency record.',
   unit='a whole number', minimum=0)
_s('EBPFM_DSTACK_MAX', 'int', '300',
   'Kernel stacks of processes in D state read per tick.',
   unit='a whole number', minimum=0)
_s('EBPFM_DSTACK_DEPTH', 'int', '5',
   'Frames kept of each kernel stack.',
   unit='a whole number', minimum=1)

_section('network', 'eBPF tier: network', '')
_s('EBPFM_CONN_ALL', 'bool', '0',
   'Also report loopback and private standing sockets, not only external ones.')
_s('EBPFM_CONN_UNTRACKED', 'bool', '1',
   'Report standing sockets no tracked process owns (uid, no pid).')
_s('EBPFM_TCP_ALL', 'bool', '0',
   'Also fold the connections of untracked processes.')
_s('EBPFM_CONNS_TOPN', 'int', '5',
   'Peers and providers kept in a `conns` block.',
   unit='a whole number', minimum=0)
_s('EBPFM_NETPEER_MAX', 'int', '65536',
   'Entries in the kernel endpoint table (about 3 MB at the default).',
   unit='entries', minimum=1)
_s('EBPFM_NET_ENDPOINTS_MAX', 'int', '256',
   'Endpoints listed per record; the rest are counted in `overflow`.',
   unit='entries', minimum=1)
_s('EBPFM_NETIO', 'bool', '1',
   'The per-tick `netio` series. 0: none (`net_endpoints` stays).')
_s('EBPFM_ARGV_HOSTS', 'bool', '1',
   'Name endpoints from host names on the command line, resolved with the host\'s resolver. '
   '0: no lookups.')

_section('blocks', 'eBPF tier: BPF blocks', '')
_s('EBPFM_FEATURES', 'features', 'auto',
   'auto: probe which BPF blocks this kernel loads. A comma list pins them (`ebpfm features` lists them).',
   empty='error')
_s('EBPFM_FEATURE_CACHE', 'bool', '1',
   'Keep each kernel\'s probe result, so a restart does not probe again.')
_s('EBPFM_PROBE_VERBOSE', 'bool', '0',
   'Print the compiler\'s message for each block it rejects.')

_section('commands', 'Commands', '')
_s('EBPFM_BENCH_REPS', 'int', '3',
   'Timed repetitions per condition in `overhead`.',
   unit='a whole number', minimum=1, tier='cli')

NAMES = [s['name'] for s in SETTINGS]

# Read by someone else, or set by ebpfm.sh for itself: never reported as unknown.
FOREIGN = {
    'EBPFM_LOG_ROOT': 'the dashboard reads it',
    'EBPFM_NODE_LOG_ROOT': 'the dashboard reads it',
    'EBPFM_SH': 'deploy/common/install-collector.sh reads it',
    'EBPFM_PACKAGED_UNIT': 'ebpfm install and deploy/common/install-collector.sh read it',
    'EBPFM_PROG': 'a bundle sets it for ebpfm.sh',
    'EBPFM_SELF': 'a bundle sets it for ebpfm.sh',
    'EBPFM_SYSCTL_CONF': "the tests point bootstrap's sysctl file elsewhere",
    'EBPFM_SYSTEMD_DIR': 'the tests stand in for a machine systemd runs',
}

# What EBPFM_REDACT reads (lib/redact.py's parse_kinds); a test keeps them equal.
REDACT_NAMES = {'key', 'keys', 'credential', 'credentials', 'secret', 'secrets',
                'prompt', 'prompts'}
REDACT_OFF = {'0', 'none', 'off'}

_INT = re.compile(r'^[0-9]+$')
_FLOAT = re.compile(r'^([0-9]+(\.[0-9]*)?|\.[0-9]+)$')
_MODE = re.compile(r'^0?[0-7]{3,4}$')


def _expected(s):
    k = s['kind']
    if k in ('int', 'float'):
        if s['pow2']:
            return 'expected a power of two (%s per CPU, e.g. 64, 256)' % s['unit']
        word = s['unit'] or 'a number'
        return 'expected %s (>= %s)' % (word, s['min'])
    if k == 'bool':
        return 'expected 0 or 1'
    if k == 'enum':
        return 'expected one of: %s' % ', '.join(s['choices'])
    if k == 'mode':
        return "expected an octal mode such as %s (0 or empty: leave the umask's)" % s['default']
    if k == 'redact':
        return 'expected keys, prompts, keys,prompts or 0'
    if k == 'features':
        return 'expected auto, or a comma list of BPF blocks'
    return 'expected a value'


def group_exists(group):
    """A group name or gid the host resolves, through NSS (getent) as the tracer does."""
    if _INT.match(group):
        return True
    try:
        import grp
        grp.getgrnam(group)
        return True
    except (KeyError, ImportError):
        pass
    try:
        r = subprocess.run(['getent', 'group', group], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True       # cannot tell: do not refuse on it


def check_value(name, value):
    """None if `value` is one `name` can use, else what was expected."""
    s = _BY_NAME[name]
    k = s['kind']
    if value == '':
        return None if s['empty'] in ('ok', 'unset') else _expected(s)
    if k == 'int':
        if not _INT.match(value) or int(value) < s['min']:
            return _expected(s)
        if s['pow2'] and (int(value) & (int(value) - 1)):
            return _expected(s)
        return None
    if k == 'float':
        if not _FLOAT.match(value) or float(value) < s['min']:
            return _expected(s)
        return None
    if k == 'bool':
        return None if value in ('0', '1') else _expected(s)
    if k == 'enum':
        return None if value in s['choices'] else _expected(s)
    if k == 'mode':
        return None if value == '0' or _MODE.match(value) else _expected(s)
    if k == 'group':
        return None if group_exists(value) else 'no such group (getent group %s)' % value
    if k == 'redact':
        toks = set(t.strip().lower() for t in value.split(',') if t.strip())
        if toks and (toks <= REDACT_OFF or toks <= REDACT_NAMES):
            return None
        return _expected(s)
    if k == 'features':
        toks = [t.strip() for t in value.split(',')]
        if value == 'auto' or (toks and all(re.match(r'^[a-z0-9_]+$', t) for t in toks)):
            return None
        return _expected(s)
    return None


def unknown_names(environ):
    """[(name, suggestion or None)] for each EBPFM_*/SNAPSHOT_* name that is no setting."""
    out = []
    for name in sorted(environ):
        if not re.match(r'^(EBPFM|SNAPSHOT)_', name) or name in _BY_NAME or name in FOREIGN:
            continue
        near = difflib.get_close_matches(name, NAMES, n=1, cutoff=0.6)
        out.append((name, near[0] if near else None))
    return out


def _where(name, origins):
    o = origins.get(name)
    return ' (set in %s)' % o if o and o != 'environment' else ''


def problems(environ, origins):
    """(errors, warnings): one line each, without the 'ebpfm: ' prefix."""
    errors, warnings = [], []
    for s in SETTINGS:
        v = environ.get(s['name'])
        if v is None:
            continue
        why = check_value(s['name'], v)
        if why:
            errors.append("%s=%r: %s%s" % (s['name'], v, why, _where(s['name'], origins)))
    for name, near in unknown_names(environ):
        warnings.append('%s%s is not a setting, so it is ignored%s'
                        % (name, _where(name, origins),
                           ' (did you mean %s?)' % near if near else ''))
    return errors, warnings


def tracer_complaints(environ):
    """What the eBPF tier says on stderr at start about values it reads its own way:
    a switch only `0` turns off, and EBPFM_FLUSH_MODE's poll for anything not
    `record`. Numbers say so where they are read (ebpf_trace._env_num)."""
    out = []
    for s in SETTINGS:
        if s['tier'] not in ('ebpf', 'both'):
            continue
        v = environ.get(s['name'])
        if v is None or check_value(s['name'], v) is None:
            continue
        if s['kind'] == 'bool':
            out.append("%s=%r: expected 0 or 1; read as on (only 0 turns it off)"
                       % (s['name'], v))
        elif s['name'] == 'EBPFM_FLUSH_MODE':
            out.append("%s=%r: expected poll or record; using poll" % (s['name'], v))
    return out


# --- ebpfm config ---------------------------------------------------------------
def config_rows(environ, env_names, from_file, shadowed, defaults):
    """One row per setting: name, value, default, origin, problem."""
    rows = []
    for s in SETTINGS:
        name = s['name']
        default = defaults.get(name, s['default'])
        if name in env_names:
            origin = 'environment'
            if name in shadowed:
                origin += ' (overrides %s)' % shadowed[name]
        elif name in from_file:
            origin = from_file[name]
        else:
            origin = 'default'
        value = environ.get(name) if origin != 'default' else default
        if value is None:
            value = default
        why = check_value(name, value) if origin != 'default' else None
        rows.append(dict(name=name, value=value, default=default, origin=origin,
                         problem=why, section=s['section'], tier=s['tier']))
    return rows


def render_config(rows, unknown, file_line, only=None):
    picked = [r for r in rows if not only or any(o.upper() in r['name'] for o in only)]
    out = [file_line, 'precedence: environment > settings file > default', '']
    w = max([len(r['name']) for r in picked] + [4])
    vw = min(max([len(r['value']) for r in picked] + [5]), 34)
    out.append('%-*s  %-*s  %s' % (w, 'NAME', vw, 'VALUE', 'FROM'))
    for r in picked:
        val = r['value'] if r['value'] != '' else "''"
        line = '%-*s  %-*s  %s' % (w, r['name'], vw, val, r['origin'])
        if r['origin'] != 'default' and r['value'] != r['default']:
            line += '   [default: %s]' % r['default']
        out.append(line)
        if r['problem']:
            out.append('%*s  ^ INVALID: %s' % (w, '', r['problem']))
    if unknown:
        out.append('')
        for name, near, origin in unknown:
            out.append('%s (%s) is not a setting, so it is ignored%s'
                       % (name, origin, ' (did you mean %s?)' % near if near else ''))
    return '\n'.join(out) + '\n'


# --- the defaults file ----------------------------------------------------------
_HEADER = """\
# /etc/default/ebpfm -- the collector's settings.
#
# Read by both systemd units (ebpfm.service, the eBPF tier; ebpfm-node.service,
# the node tier) through EnvironmentFile=, and by the `ebpfm` command itself, so
# `ebpfm status`, `tail` and `config` see what the units see. The environment of
# an `ebpfm` command wins over this file, and this file over the defaults.
#
# Format: KEY=VALUE, one per line: systemd's environment-file syntax, NOT a shell
# script (no export, no $VAR expansion, no command substitution). `#` starts a
# comment. Every setting is listed, commented out at its default, except
# EBPFM_MIN_UID (see its note). Each tier reads its settings when it starts:
# after an edit, `systemctl restart ebpfm ebpfm-node` (the eBPF tier writes every
# live process as `truncated` when it stops). `ebpfm config` shows what applies
# and where each value came from, and refuses a value a tier cannot use.
#
# Generated from collector/lib/ebpfm_settings.py
# (python3 collector/lib/ebpfm_settings.py defaults-file); edit the table there.
"""


def _wrap(text, width=78, prefix='# '):
    words, lines, cur = text.split(), [], ''
    for w in words:
        if cur and len(prefix) + len(cur) + 1 + len(w) > width:
            lines.append(prefix + cur)
            cur = w
        else:
            cur = w if not cur else cur + ' ' + w
    if cur:
        lines.append(prefix + cur)
    return lines


def render_defaults_file():
    out = [_HEADER.rstrip('\n'), '']
    out += ['# --- attribution (load-bearing: do not change the value) ' + '-' * 21,
            '# EBPFM_MIN_UID=0 is deliberate: it defines what actor3="human" MEANS.',
            '# Raising it (e.g. to 1000 to drop system daemons) changes the human',
            '# population and makes a capture incomparable with every earlier one.',
            '# Leave it 0 and filter downstream on `attribution` instead. Shipped',
            '# active so the value is explicit.',
            'EBPFM_MIN_UID=0', '']
    for key, title, blurb in SECTIONS:
        items = [s for s in SETTINGS if s['section'] == key and s['in_file']
                 and s['name'] != 'EBPFM_MIN_UID']
        if not items:
            continue
        head = '# --- %s ' % title
        out.append(head + '-' * max(3, 78 - len(head)))
        for s in items:
            doc = s['doc']
            if s['dynamic']:
                doc += ' Default: %s.' % s['default']
            out += _wrap(doc)
            out.append('#%s=%s' % (s['name'], s['file_default']))
        out.append('')
    return '\n'.join(out).rstrip('\n') + '\n'


# --- OPERATIONS.md's table ------------------------------------------------------
_TIER_WORD = {'ebpf': 'eBPF', 'node': 'node', 'both': 'both', 'cli': 'command'}


def render_docs_table():
    out = ['| setting | default | read by | what it does |', '|---|---|---|---|']
    for s in SETTINGS:
        d = s['default'] if s['default'] != '' else '(empty)'
        doc = s['doc'].replace('|', '\\|')
        out.append('| `%s` | %s | %s | %s |' % (s['name'], d.replace('|', '\\|'),
                                               _TIER_WORD[s['tier']], doc))
    return '\n'.join(out) + '\n'


# --- command line ---------------------------------------------------------------
def _pairs(values):
    out = {}
    for v in values:
        k, _, rest = v.partition('=')
        out[k] = rest
    return out


def _parse(argv):
    opts = {'origin': [], 'shadowed': [], 'default': [], 'env_names': '', 'file': '',
            'file_state': 'none', 'json': False, 'names': []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--origin', '--shadowed', '--default'):
            opts[a[2:]].append(argv[i + 1])
            i += 2
        elif a in ('--env-names', '--file', '--file-state'):
            opts[a[2:].replace('-', '_')] = argv[i + 1]
            i += 2
        elif a == '--json':
            opts['json'] = True
            i += 1
        else:
            opts['names'].append(a)
            i += 1
    return opts


def _file_origins(opts):
    f = opts['file'] or 'the settings file'
    return dict((k, '%s:%s' % (f, line)) for k, line in _pairs(opts['origin']).items())


def main(argv):
    if not argv:
        sys.stderr.write('usage: ebpfm_settings.py validate|config|defaults-file|docs-table|names\n')
        return 2
    cmd, opts = argv[0], _parse(argv[1:])
    from_file = _file_origins(opts)
    origins = dict((n, 'environment') for n in opts['env_names'].split())
    origins.update(from_file)
    if cmd == 'validate':
        errors, warnings = problems(os.environ, origins)
        for w in warnings:
            sys.stderr.write('ebpfm: warning: %s\n' % w)
        for e in errors:
            sys.stderr.write('ebpfm: %s\n' % e)
        return 2 if errors else 0
    if cmd == 'config':
        f = opts['file']
        shadowed = dict((k, '%s:%s' % (f, line)) for k, line in _pairs(opts['shadowed']).items())
        env_names = set(opts['env_names'].split())
        rows = config_rows(os.environ, env_names, from_file, shadowed, _pairs(opts['default']))
        unknown = [(n, near, origins.get(n, 'environment')) for n, near in unknown_names(os.environ)]
        bad = [r for r in rows if r['problem']]
        if opts['json']:
            print(json.dumps({'file': f or None, 'file_state': opts['file_state'],
                              'settings': rows,
                              'unknown': [dict(name=n, did_you_mean=near, origin=o)
                                          for n, near, o in unknown]}, indent=1))
        else:
            state = {'read': 'read', 'none': 'not present', 'unreadable': 'present but unreadable',
                     'off': 'not read (EBPFM_DEFAULT_FILE is empty)'}.get(opts['file_state'],
                                                                         opts['file_state'])
            line = 'settings file: %s (%s)' % (f or '(none)', state)
            sys.stdout.write(render_config(rows, unknown, line, opts['names']))
        return 2 if bad else 0
    if cmd == 'defaults-file':
        sys.stdout.write(render_defaults_file())
        return 0
    if cmd == 'docs-table':
        sys.stdout.write(render_docs_table())
        return 0
    if cmd == 'names':
        print('\n'.join(NAMES))
        return 0
    sys.stderr.write('ebpfm_settings.py: unknown command %r\n' % cmd)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
