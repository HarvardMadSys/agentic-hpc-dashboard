"""`ebpfm status`: is each tier running, and is data still arriving?

ebpfm.sh knows how each tier runs (its unit or pid file, where it writes, how
often) and writes those facts to this module's stdin, one `key=value` per line,
a line `end` after each tier. This module reads the day-files and says, per tier:

  * how old the newest record is, against twice the tier's interval: older is
    STALE (EBPFM_RESIDENCY_S for the eBPF tier, whose snapshot stream ticks even
    on an idle node; EBPFM_NODE_INTERVAL for the node tier);
  * today's records and bytes per stream, the log, the unit's state;
  * the eBPF tier's perf_lost and handler_errors from its latest totals: unknown
    (null), not 0, until it has written one;
  * the version the tier runs, against the version installed.

Exit status: 0 healthy, 1 degraded or stale (or one tier of two not running),
3 not running, 4 unknown (a tier runs, but its files cannot be read or it has no
heartbeat). `has-meta` answers whether a tracer has written its `meta` record,
which `start` waits for.

Python 3.6, like the rest of the collector (EL8).
"""
import datetime
import glob
import gzip
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ebpfm_version import collector_version  # noqa: E402

TAIL_STEPS = (256 << 10, 4 << 20, 32 << 20)
VERDICT_CODE = {'healthy': 0, 'degraded': 1, 'not running': 3, 'unknown': 4}
LOW_DISK_PCT = 10.0


# --- reading day-files ----------------------------------------------------------
def day_path(root, day, host, stream=None):
    """`<root>/<day>/<host>[.<stream>].jsonl`, else its .gz, else a file the
    unit wrote under the fully qualified name (an install from before ebpfm.sh
    and the unit agreed on the short one), else None."""
    d = os.path.join(root, day)
    stem = host if stream is None else '%s.%s' % (host, stream)
    for p in (os.path.join(d, stem + '.jsonl'), os.path.join(d, stem + '.jsonl.gz')):
        if os.path.exists(p):
            return p
    if stream is not None:
        for p in sorted(glob.glob(os.path.join(glob.escape(d), glob.escape(host) + '.*.' + stream
                                               + '.jsonl'))):
            return p
    return None


def last_record(path, pred=None):
    """The last complete line of a plain day-file that parses as a JSON object and
    satisfies `pred`, read from the end; None if there is none."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            for step in TAIL_STEPS:
                start = max(0, size - step)
                fh.seek(start)
                lines = fh.read(size - start).split(b'\n')
                if start > 0:
                    lines = lines[1:]          # the first may be cut
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw.decode('utf-8', 'replace'))
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and (pred is None or pred(rec)):
                        return rec
                if start == 0:
                    break
    except (IOError, OSError):
        return None
    return None


def count_lines(path):
    n = 0
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rb') as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b'\n')
    return n


def newest_epoch(path):
    """When the newest record in a day-file was written: its ts_epoch for a plain
    file, the file's mtime for a sealed .gz (gzip keeps the source's)."""
    if path.endswith('.gz'):
        return os.path.getmtime(path)
    rec = last_record(path, lambda r: isinstance(r.get('ts_epoch'), (int, float)))
    if rec is not None:
        return float(rec['ts_epoch'])
    return os.path.getmtime(path) if os.path.getsize(path) else None


def find_meta(root, host, pid, days):
    """The `meta` record collector `pid` wrote, from the snapshot stream of `days`."""
    want = b'"collector_pid": %d' % int(pid)
    for day in days:
        p = day_path(root, day, host, 'snapshot')
        if p is None:
            continue
        opener = gzip.open if p.endswith('.gz') else open
        try:
            with opener(p, 'rb') as fh:
                found = None
                for raw in fh:
                    if b'"meta"' in raw and want in raw:
                        try:
                            rec = json.loads(raw.decode('utf-8', 'replace'))
                        except ValueError:
                            continue
                        if rec.get('event') == 'meta' and rec.get('collector_pid') == int(pid):
                            found = rec
                if found is not None:
                    return found
        except (IOError, OSError, EOFError):
            continue
    return None


def read_state(path):
    """ebpfm.sh's note of a running tier's settings: KEY=VALUE lines."""
    out = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                k, sep, v = line.rstrip('\n').partition('=')
                if sep:
                    out[k] = v
    except (IOError, OSError):
        return {}
    return out


# --- formatting -------------------------------------------------------------------
def human_bytes(n):
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if n < 1024 or unit == 'T':
            return ('%d%s' % (n, unit)) if unit == 'B' else ('%.1f%s' % (n, unit))
        n /= 1024.0


def human_age(s):
    s = int(round(s))
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm %02ds' % (s // 60, s % 60)
    if s < 86400:
        return '%dh %02dm' % (s // 3600, (s % 3600) // 60)
    return '%dd %02dh' % (s // 86400, (s % 86400) // 3600)


def comparable(a, b):
    """Two versions say whether the code differs only when both are the same kind:
    git answers `describe` only to its checkout's owner, so another user sees the
    code's hash for the same files (ebpfm_version.py)."""
    return a.startswith('sha256:') == b.startswith('sha256:')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- one tier ---------------------------------------------------------------------
def evaluate(f, now, installed):
    """The report for one tier from its facts."""
    tier = f.get('tier')
    root, host = f.get('dir', ''), f.get('host', '')
    running = f.get('running') == '1'
    pid = _int(f.get('pid'))
    elapsed = _num(f.get('elapsed'))
    interval = _num(f.get('interval'))
    today = datetime.date.fromtimestamp(now)
    days = [today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()]
    streams = ['snapshot', 'exits'] if tier == 'ebpf' else [None]
    r = dict(tier=tier, running=running, how=f.get('how') or None, unit=f.get('unit') or None,
             pid=pid, enabled=f.get('enabled') or None, restarts=_int(f.get('restarts')),
             dir=root, host=host, interval_s=interval, mode=f.get('mode') or None,
             log=f.get('log') or None, uptime_s=elapsed, today={}, newest_record_age_s=None,
             stale_after_s=(2 * interval) if interval else None, perf_lost=None,
             handler_errors=None, version=None, version_installed=installed, disk=None,
             warnings=[], notes=[n for n in f.get('note', []) if n], state=None)
    unreadable = False
    newest = None
    for s in streams:
        p = day_path(root, days[0], host, s)
        key = s or 'node'
        if p is None:
            r['today'][key] = None
            continue
        try:
            r['today'][key] = dict(path=p, records=count_lines(p), bytes=os.path.getsize(p))
            t = newest_epoch(p)
        except (IOError, OSError):
            unreadable = True
            r['today'][key] = dict(path=p, records=None, bytes=None)
            continue
        if t is not None:
            newest = t if newest is None else max(newest, t)
    if newest is None:
        for s in streams:
            p = day_path(root, days[1], host, s)
            if p is None:
                continue
            try:
                t = newest_epoch(p)
            except (IOError, OSError):
                unreadable = True
                continue
            if t is not None:
                newest = t if newest is None else max(newest, t)
    # A folder this user cannot read hides its files rather than failing on them
    # (stat answers "no such file"), so it is tried: access(2) would answer for
    # the real uid without the capabilities the reads below have.
    if root and os.path.isdir(root):
        try:
            os.listdir(root)
        except OSError:
            unreadable = True
    if newest is not None:
        r['newest_record_age_s'] = round(max(0.0, now - newest), 1)

    if tier == 'ebpf':
        snap = (r['today'].get('snapshot') or {}).get('path')
        if snap and not snap.endswith('.gz'):
            # this run's own totals: a restarted tier's last `stop` is the run before's
            tot = last_record(snap, lambda x: x.get('event') in ('residency_totals', 'stop')
                              and 'perf_lost' in x
                              and (not running or not pid or x.get('collector_pid') == pid))
            if tot is not None:
                lost = tot.get('perf_lost')
                r['perf_lost'] = (sum(v for v in lost.values() if isinstance(v, int))
                                  if isinstance(lost, dict) else None)
                # the tracer writes null for a run that counted none; absent: not counted
                errs = tot.get('handler_errors')
                if isinstance(errs, dict):
                    r['handler_errors'] = sum(v for v in errs.values() if isinstance(v, int))
                elif 'handler_errors' in tot and errs is None:
                    r['handler_errors'] = 0
        if running and pid:
            start_day = datetime.date.fromtimestamp(now - (elapsed or 0))
            look = [start_day.isoformat(), (start_day + datetime.timedelta(days=1)).isoformat()]
            look = [d for d in [days[0]] + look if d <= days[0]]
            meta = find_meta(root, host, pid, list(dict.fromkeys(look)))
            if meta is not None:
                r['version'] = meta.get('collector_version')
                if r['version'] is None:
                    r['notes'].append('this tracer records no version: it predates collector_version')
    else:
        st = read_state(f.get('state', '')) if f.get('state') else {}
        if st and pid and st.get('EBPFM_STATE_PID') == str(pid):
            r['version'] = st.get('EBPFM_STATE_VERSION') or None
    if running and r['version'] and installed and r['version'] != installed:
        if comparable(r['version'], installed):
            r['warnings'].append('runs %s, but %s is installed: restart it to run the installed code%s'
                                 % (r['version'], installed,
                                    ' (the eBPF tier writes its live processes as `truncated`)'
                                    if tier == 'ebpf' else ''))
        else:
            r['notes'].append('runs %s; installed %s, as this user sees it (git answers only '
                              "its checkout's owner), so the two do not compare" % (r['version'], installed))
    try:
        du = shutil.disk_usage(root) if root and os.path.isdir(root) else None
    except OSError:
        du = None
    if du is not None and du.total:
        r['disk'] = dict(free_bytes=du.free, free_pct=round(100.0 * du.free / du.total, 1))
        if r['disk']['free_pct'] < LOW_DISK_PCT:
            r['warnings'].append('only %.1f%% of %s is free' % (r['disk']['free_pct'], root))

    if not running:
        r['state'] = 'not running'
    elif unreadable:
        r['state'] = 'unknown'
        r['notes'].append('cannot read %s as this user (try with sudo)' % root)
    elif not interval:
        r['state'] = 'unknown'
        r['notes'].append('no heartbeat to judge freshness by (EBPFM_RESIDENCY_S=0)' if tier == 'ebpf'
                          else 'no interval to judge freshness by (EBPFM_NODE_INTERVAL=%r)'
                          % f.get('interval'))
    else:
        young = elapsed is not None and elapsed < 2 * interval
        age = r['newest_record_age_s']
        if age is None or age > 2 * interval:
            r['state'] = 'starting' if young else 'stale'
        else:
            r['state'] = 'ok'
    if r['state'] in ('ok', 'starting'):
        if r['perf_lost']:
            r['state'] = 'degraded'
            r['warnings'].append('perf_lost %d: the drain fell behind and events were dropped '
                                 'in this run (raise EBPFM_PERF_PAGES)' % r['perf_lost'])
        elif r['disk'] and r['disk']['free_pct'] < LOW_DISK_PCT:
            r['state'] = 'degraded'
    return r


def verdict(reports):
    if not reports or all(r['state'] == 'not running' for r in reports):
        return 'not running'
    if any(r['state'] in ('stale', 'degraded', 'not running') for r in reports):
        return 'degraded'
    if any(r['state'] == 'unknown' for r in reports):
        return 'unknown'
    return 'healthy'


# --- output -----------------------------------------------------------------------
def _hint(r, prog, root):
    """How to start a tier that is not running: the eBPF tier needs root."""
    sudo = '' if root or r['tier'] == 'node' else 'sudo '
    return '%s%s start   (%s check says what is missing)' % (sudo, prog, prog)


def render_human(reports, v, code, host, installed, source, prog, root):
    out = ['ebpfm on %s: %s (exit %d)' % (host, v.upper(), code),
           '  installed collector: %s (%s)' % (installed, source)]
    for r in reports:
        name = 'node tier' if r['tier'] == 'node' else 'ebpf tier'
        if not r['running']:
            out.append('%s: NOT RUNNING on %s' % (name, r['host']))
        else:
            bits = []
            if r['unit']:
                bits.append('unit %s' % r['unit'])
            if r['pid']:
                bits.append('pid %d' % r['pid'])
            if r['enabled']:
                bits.append(r['enabled'])
            if r['restarts'] is not None and r['unit']:
                bits.append('%d restart%s' % (r['restarts'], '' if r['restarts'] == 1 else 's'))
            if r['mode']:
                bits.append('mode %s' % r['mode'])
            if r['interval_s']:
                bits.append('every %ss' % ('%g' % r['interval_s']))
            label = {'ok': 'RUNNING', 'starting': 'RUNNING (starting)', 'stale': 'RUNNING but STALE',
                     'degraded': 'RUNNING, DEGRADED', 'unknown': 'RUNNING'}[r['state']]
            out.append('%s: %s on %s (%s)' % (name, label, r['host'], ', '.join(bits)))
        age = r['newest_record_age_s']
        if age is not None:
            line = '  newest record   %s ago' % human_age(age)
            if r['stale_after_s']:
                line += ' (stale after %s)' % human_age(r['stale_after_s'])
            out.append(line)
        elif r['running']:
            out.append('  newest record   none yet')
        files = [(k, v2) for k, v2 in sorted(r['today'].items()) if v2]
        if files:
            for k, v2 in files:
                n = v2['records']
                out.append('  today %-9s %s, %s  %s' % (
                    (k if r['tier'] == 'ebpf' else ''),
                    ('%d record%s' % (n, '' if n == 1 else 's')) if n is not None else 'unreadable',
                    human_bytes(v2['bytes']) if v2['bytes'] is not None else '?', v2['path']))
        else:
            want = ('%s.{snapshot,exits}.jsonl' if r['tier'] == 'ebpf' else '%s.jsonl') % r['host']
            out.append('  today           no records (%s)'
                       % os.path.join(r['dir'], datetime.date.today().isoformat(), want))
        if r['tier'] == 'ebpf' and r['running']:
            if r['perf_lost'] is None:
                out.append('  perf_lost       not known yet (no residency_totals today)')
            else:
                out.append('  perf_lost       %d   handler_errors %s'
                           % (r['perf_lost'], r['handler_errors']
                              if r['handler_errors'] is not None else 'unknown'))
        if r['running']:
            out.append('  version         %s' % (r['version'] or 'unknown'))
        if r['log']:
            log = r['log']
            if log.startswith('journal:'):
                log = 'journalctl -u %s' % log[len('journal:'):]
            out.append('  log             %s' % log)
        if r['disk']:
            out.append('  disk            %s: %s free (%s%%)' % (r['dir'], human_bytes(r['disk']['free_bytes']),
                                                            '%g' % r['disk']['free_pct']))
        for w in r['warnings']:
            out.append('  WARN            %s' % w)
        for n in r['notes']:
            out.append('  note            %s' % n)
        if not r['running']:
            out.append('  to start it     %s' % _hint(r, prog, root))
    out.append('exit codes: 0 healthy, 1 degraded or stale, 3 not running, 4 unknown')
    return '\n'.join(out) + '\n'


def read_facts(stream):
    tiers, cur = [], {}
    for line in stream:
        line = line.rstrip('\n')
        if line == 'end':
            tiers.append(cur)
            cur = {}
            continue
        k, sep, v = line.partition('=')
        if not sep:
            continue
        if k == 'note':
            cur.setdefault('note', []).append(v)
        else:
            cur[k] = v
    if cur:
        tiers.append(cur)
    return tiers


def main(argv):
    if argv and argv[0] == 'has-meta':
        root, host, pid = argv[1], argv[2], argv[3]
        today = datetime.date.today()
        days = [today.isoformat(), (today - datetime.timedelta(days=1)).isoformat()]
        return 0 if find_meta(root, host, pid, days) is not None else 1
    as_json = '--json' in argv
    code_dir, prog, host, root = None, 'ebpfm', os.uname()[1].split('.')[0], False
    for i, a in enumerate(argv):
        if a == '--code-dir':
            code_dir = argv[i + 1]
        elif a == '--prog':
            prog = argv[i + 1]
        elif a == '--host':
            host = argv[i + 1]
        elif a == '--root':
            root = argv[i + 1] == '1'
    installed, source = collector_version(code_dir) if code_dir else collector_version()
    now = time.time()
    reports = [evaluate(f, now, installed) for f in read_facts(sys.stdin)]
    v = verdict(reports)
    code = VERDICT_CODE[v]
    if as_json:
        print(json.dumps({'host': host, 'verdict': v, 'exit': code,
                          'collector_version_installed': installed,
                          'collector_version_source': source, 'checked_at': now,
                          'tiers': reports}, indent=1, sort_keys=True))
    else:
        sys.stdout.write(render_human(reports, v, code, host, installed, source, prog, root))
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
