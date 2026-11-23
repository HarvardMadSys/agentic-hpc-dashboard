"""The operator's side of the collector: the settings table and its checks,
`ebpfm config`, `version`, `status`, the command line (per-command --help, unknown
options, refused settings), the node tier's start/status/stop, `install --dry-run`,
the settings file ebpfm.sh reads and writes, and the bundle.

No root, no BPF, no systemd: the node tier runs with a stand-in for
node_snapshot.py, and the eBPF tier's start-up wait is driven by a stand-in that
writes a meta record.

    python3 -m unittest discover -s collector/tests -q
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REPO = _ROOT.parent
sys.path.insert(0, str(_ROOT / 'lib'))

import ebpfm_settings as S     # noqa: E402
import ebpfm_status as ST      # noqa: E402
import ebpfm_version as V      # noqa: E402

SCRIPT = str(_ROOT / 'ebpfm.sh')
TODAY = datetime.date.today().isoformat()


def _clean_env(**extra):
    """The environment without any EBPFM_*/SNAPSHOT_* setting of the caller's."""
    e = dict((k, v) for k, v in os.environ.items()
             if not re.match(r'^(EBPFM|SNAPSHOT)_', k))
    e.update(extra)
    return e


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ebpfm-cli-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def env(self, **extra):
        e = _clean_env(HOME=self.path('home'), EBPFM_DEFAULT_FILE=self.path('default-ebpfm'),
                       EBPFM_PID_DIR=self.path('run'), EBPFM_OUTPUT_DIR=self.path('out', 'ebpf'),
                       EBPFM_NODE_OUTPUT_DIR=self.path('out', 'node'),
                       EBPFM_UNIT_DIR=self.path('units'), EBPFM_PACKAGED_UNIT=self.path('no-package'),
                       EBPFM_SYSCTL_CONF=self.path('99-ebpfm.conf'))
        e.update(extra)
        return e

    def sh(self, *args, script=SCRIPT, timeout=120, **env):
        r = subprocess.run(['bash', script] + list(args), env=self.env(**env),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr


def _fn(name):
    """One function of ebpfm.sh, as text, to run on its own."""
    src = Path(SCRIPT).read_text(encoding='utf-8')
    m = re.search(r'^%s\(\) \{[^\n]*\n.*?^\}\n' % re.escape(name), src, re.M | re.S)
    return m.group(0)


def _bash(script, env=None):
    r = subprocess.run(['bash', '-c', 'set -uo pipefail\n' + script], env=env or _clean_env(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=120)
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------------------
# the settings table
# ---------------------------------------------------------------------------
class SettingsTableTest(unittest.TestCase):
    """The table is held to the code that reads each setting."""

    def read_names(self):
        tracer = (_ROOT / 'ebpf_trace.py').read_text(encoding='utf-8')
        node = (_ROOT / 'node_snapshot.py').read_text(encoding='utf-8')
        sh = Path(SCRIPT).read_text(encoding='utf-8')
        entry = (_REPO / 'docker' / 'collector' / 'entrypoint.sh').read_text(encoding='utf-8')
        names = set(re.findall(r"os\.environ\.get\('((?:EBPFM|SNAPSHOT)_[A-Z0-9_]+)'", tracer + node))
        names |= set(re.findall(r"_env_(?:num|mode)\('(EBPFM_[A-Z0-9_]+)'", tracer))
        names |= set(re.findall(r'\$\{((?:EBPFM|SNAPSHOT)_[A-Z0-9_]+)', sh + entry))
        return names

    def test_every_setting_the_code_reads_is_in_the_table(self):
        names = self.read_names()
        self.assertGreater(len(names), 50)
        missing = sorted(n for n in names if n not in S.NAMES and n not in S.FOREIGN)
        self.assertEqual(missing, [])

    def test_the_table_has_nothing_the_code_does_not_read(self):
        extra = sorted(set(S.NAMES) - self.read_names() - {'EBPFM_DEFAULT_FILE'})
        self.assertEqual(extra, [])

    def test_the_defaults_are_the_codes(self):
        tracer = (_ROOT / 'ebpf_trace.py').read_text(encoding='utf-8')
        node = (_ROOT / 'node_snapshot.py').read_text(encoding='utf-8')
        by = dict((s['name'], s) for s in S.SETTINGS)
        checked = 0
        for name, default in re.findall(r"_env_num\('(EBPFM_[A-Z0-9_]+)', ([0-9.]+), ", tracer):
            self.assertEqual(float(by[name]['default']), float(default), name)
            checked += 1
        for name, default in re.findall(r"os\.environ\.get\('((?:EBPFM|SNAPSHOT)_[A-Z0-9_]+)', '([^']*)'\)",
                                        tracer + node):
            if by[name]['kind'] in ('int', 'float'):
                self.assertEqual(float(by[name]['default']), float(default), name)
            elif not by[name]['dynamic']:
                self.assertEqual(by[name]['default'], default, name)
            checked += 1
        sh = Path(SCRIPT).read_text(encoding='utf-8')
        for name, default in re.findall(r'\$\{(EBPFM_(?:NODE_INTERVAL|NODE_TOP_N|BENCH_REPS|RESIDENCY_S)):-([0-9]+)\}', sh):
            self.assertEqual(float(by[name]['default']), float(default), name)
            checked += 1
        self.assertGreater(checked, 40)

    def test_redact_reads_the_names_the_table_accepts(self):
        import redact
        self.assertEqual(S.REDACT_NAMES, set(redact._KIND_NAMES))
        for off in S.REDACT_OFF:
            self.assertEqual(redact.parse_kinds(off)[0], frozenset(), off)

    def test_what_a_refused_value_says(self):
        cases = {
            ('EBPFM_NODE_INTERVAL', 'five'): 'expected seconds (>= 30)',
            ('EBPFM_NODE_INTERVAL', '5m'): 'expected seconds (>= 30)',
            ('EBPFM_NODE_INTERVAL', '29'): 'expected seconds (>= 30)',
            ('EBPFM_PERF_PAGES', '100'): 'expected a power of two (pages per CPU, e.g. 64, 256)',
            ('EBPFM_CWD', 'false'): 'expected 0 or 1',
            ('EBPFM_FLUSH_MODE', 'Poll'): 'expected one of: poll, record',
            ('EBPFM_TIER', 'both'): 'expected one of: node, ebpf, all',
            ('SNAPSHOT_MODE', 'fast'): 'expected one of: full, node',
            ('EBPFM_POLL_MS', ''): 'expected milliseconds (>= 1)',
            ('EBPFM_DIR_MODE', '0999'): "expected an octal mode such as 0750 (0 or empty: leave the umask's)",
            ('EBPFM_REDACT', 'everything'): 'expected keys, prompts, keys,prompts or 0',
            ('EBPFM_GROUP', 'no-such-group-zz9'): 'no such group (getent group no-such-group-zz9)',
        }
        for (name, value), why in cases.items():
            self.assertEqual(S.check_value(name, value), why, (name, value))
        for name, value in (('EBPFM_NODE_INTERVAL', '30'), ('EBPFM_RESIDENCY_S', '0.5'),
                            ('EBPFM_REDACT', 'prompt'), ('EBPFM_REDACT', 'Keys,Prompts'),
                            ('EBPFM_REDACT', 'off'), ('EBPFM_DIR_MODE', ''), ('EBPFM_DIR_MODE', '0'),
                            ('EBPFM_FILE_MODE', '640'), ('EBPFM_FEATURES', 'core,argv_user'),
                            ('EBPFM_GROUP', '0'), ('EBPFM_OUTPUT_DIR', ''), ('EBPFM_TIER', '')):
            self.assertIsNone(S.check_value(name, value), (name, value))

    def test_problems_say_where_a_value_was_set(self):
        errors, warnings = S.problems({'EBPFM_NODE_INTERVAL': 'five', 'EBPFM_RESIDENCY': '30'},
                                      {'EBPFM_RESIDENCY': '/etc/default/ebpfm:4'})
        self.assertEqual(errors, ["EBPFM_NODE_INTERVAL='five': expected seconds (>= 30)"])
        self.assertEqual(warnings, ['EBPFM_RESIDENCY (set in /etc/default/ebpfm:4) is not a setting, '
                                    'so it is ignored (did you mean EBPFM_RESIDENCY_S?)'])
        errors, _ = S.problems({'EBPFM_NODE_INTERVAL': '5m'}, {'EBPFM_NODE_INTERVAL': 'f:2'})
        self.assertEqual(errors, ["EBPFM_NODE_INTERVAL='5m': expected seconds (>= 30) (set in f:2)"])

    def test_names_that_are_someone_elses_are_not_reported(self):
        env = dict((n, 'x') for n in S.FOREIGN)
        env['EBPFM_FLUSHMODE'] = 'poll'
        self.assertEqual(S.unknown_names(env), [('EBPFM_FLUSHMODE', 'EBPFM_FLUSH_MODE')])

    def test_the_tracer_says_what_it_makes_of_a_value_it_reads_its_own_way(self):
        out = S.tracer_complaints({'EBPFM_CWD': 'false', 'EBPFM_FLUSH_MODE': 'Poll',
                                   'EBPFM_NODE_INTERVAL': 'five', 'EBPFM_NETIO': '0'})
        self.assertEqual(out, ["EBPFM_FLUSH_MODE='Poll': expected poll or record; using poll",
                               "EBPFM_CWD='false': expected 0 or 1; read as on (only 0 turns it off)"])

    def test_the_rpm_defaults_file_is_the_tables(self):
        text = (_REPO / 'packaging' / 'rpm' / 'ebpfm.default').read_text(encoding='utf-8')
        self.assertEqual(text, S.render_defaults_file(),
                         'regenerate: python3 collector/lib/ebpfm_settings.py defaults-file '
                         '> packaging/rpm/ebpfm.default')
        for s in S.SETTINGS:
            if s['in_file'] and s['name'] != 'EBPFM_MIN_UID':
                self.assertRegex(text, r'(?m)^#%s=' % s['name'])
        self.assertRegex(text, r'(?m)^EBPFM_MIN_UID=0$')
        # deploy/common/install-collector.sh turns this line into the group it sets
        self.assertRegex(text, r'(?m)^#EBPFM_GROUP=$')
        self.assertNotIn('nothing reads the node tier', text)

    def test_operations_md_carries_the_table(self):
        text = (_ROOT / 'OPERATIONS.md').read_text(encoding='utf-8')
        m = re.search(r'<!-- settings-table:begin -->\n(.*?)<!-- settings-table:end -->', text, re.S)
        self.assertIsNotNone(m, 'OPERATIONS.md lost its settings-table markers')
        self.assertEqual(m.group(1), S.render_docs_table(),
                         'regenerate: python3 collector/lib/ebpfm_settings.py docs-table')


# ---------------------------------------------------------------------------
# the version
# ---------------------------------------------------------------------------
class VersionTest(Tmp):
    def copy(self):
        d = self.path('collector')
        shutil.copytree(str(_ROOT), d, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        return d

    def test_a_stamp_wins(self):
        d = self.copy()
        with open(os.path.join(d, 'VERSION'), 'w') as fh:
            fh.write('1.2.3-1.el8\n')
        self.assertEqual(V.collector_version(d), ('1.2.3-1.el8', 'stamp'))
        rc, out, _ = self.sh('version', script=os.path.join(d, 'ebpfm.sh'))
        self.assertEqual((rc, out), (0, 'ebpfm 1.2.3-1.el8\n'))

    def test_outside_a_checkout_the_code_names_itself(self):
        d = self.copy()
        v, source = V.collector_version(d)
        self.assertEqual(source, 'hash')
        self.assertRegex(v, r'^sha256:[0-9a-f]{12}$')
        self.assertEqual(V.collector_version(d)[0], v)
        with open(os.path.join(d, 'lib', 'ebpfm_status.py'), 'a') as fh:
            fh.write('\n# changed\n')
        self.assertNotEqual(V.collector_version(d)[0], v)

    def test_a_checkout_says_git_describe_where_git_answers(self):
        r = subprocess.run(['git', '-C', str(_ROOT), 'describe', '--always', '--dirty'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if r.returncode != 0 or (_ROOT / 'VERSION').exists():
            self.skipTest('git does not describe this checkout for this user')
        self.assertEqual(V.collector_version(str(_ROOT)), (r.stdout.strip(), 'git'))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
class StatusTest(Tmp):
    def write(self, path, recs):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a') as fh:
            for r in recs:
                fh.write(json.dumps(r) + '\n')

    def facts(self, **kw):
        f = dict(tier='node', running='1', how='pid', pid=str(os.getpid()), dir=self.path('node'),
                 host='node7', interval='300', elapsed='5000')
        f.update(kw)
        return f

    def test_fresh_stale_starting_and_not_running(self):
        now = time.time()
        p = self.path('node', TODAY, 'node7.jsonl')
        self.write(p, [{'ts_epoch': now - 3000}, {'ts_epoch': now - 10}])
        r = ST.evaluate(self.facts(), now, 'v1')
        self.assertEqual((r['state'], round(r['newest_record_age_s'])), ('ok', 10))
        self.assertEqual(r['today']['node']['records'], 2)
        r = ST.evaluate(self.facts(), now + 1200, 'v1')
        self.assertEqual(r['state'], 'stale')
        r = ST.evaluate(self.facts(elapsed='30'), now + 1200, 'v1')      # just restarted
        self.assertEqual(r['state'], 'starting')
        r = ST.evaluate(self.facts(running='0'), now, 'v1')
        self.assertEqual(r['state'], 'not running')
        self.assertEqual(ST.verdict([r]), 'not running')

    def test_the_verdict_and_its_exit_code(self):
        def rep(state):
            return {'state': state}
        self.assertEqual(ST.verdict([rep('ok'), rep('starting')]), 'healthy')
        self.assertEqual(ST.verdict([rep('ok'), rep('not running')]), 'degraded')
        self.assertEqual(ST.verdict([rep('ok'), rep('stale')]), 'degraded')
        self.assertEqual(ST.verdict([rep('ok'), rep('unknown')]), 'unknown')
        self.assertEqual(ST.verdict([rep('not running')] * 2), 'not running')
        self.assertEqual([ST.VERDICT_CODE[v] for v in ('healthy', 'degraded', 'not running', 'unknown')],
                         [0, 1, 3, 4])

    def test_the_ebpf_tiers_own_totals_meta_and_version(self):
        now = time.time()
        pid = os.getpid()
        snap = self.path('ebpf', TODAY, 'node7.snapshot.jsonl')
        self.write(snap, [{'event': 'stop', 'perf_lost': {'events': 9}, 'collector_pid': 1,
                           'ts_epoch': now - 100},
                          {'event': 'meta', 'collector_pid': pid, 'collector_version': 'v0',
                           'ts_epoch': now - 50}])
        f = self.facts(tier='ebpf', dir=self.path('ebpf'), interval='60')
        r = ST.evaluate(f, now, 'v1')
        self.assertIsNone(r['perf_lost'], "the run before's stop is not this run's")
        self.assertEqual(r['version'], 'v0')
        self.assertIn('runs v0, but v1 is installed', r['warnings'][0])
        self.write(snap, [{'event': 'residency_totals', 'perf_lost': {'events': 3, 'argv_events': 1},
                           'handler_errors': None, 'collector_pid': pid, 'ts_epoch': now - 5}])
        r = ST.evaluate(f, now, 'v0')
        self.assertEqual((r['perf_lost'], r['handler_errors'], r['state'], r['warnings'][0][:12]),
                         (4, 0, 'degraded', 'perf_lost 4:'))
        self.assertEqual(ST.main(['has-meta', self.path('ebpf'), 'node7', str(pid)]), 0)
        self.assertEqual(ST.main(['has-meta', self.path('ebpf'), 'node7', '1']), 1)

    def test_a_hash_and_a_describe_do_not_compare(self):
        self.assertFalse(ST.comparable('sha256:0123456789ab', 'ca94ad3'))
        self.assertTrue(ST.comparable('ca94ad3', 'v1.0-2-gabc'))

    def test_the_unit_wrote_under_the_full_host_name(self):
        now = time.time()
        p = self.path('ebpf', TODAY, 'node7.rc.example.edu.snapshot.jsonl')
        self.write(p, [{'event': 'residency_totals', 'ts_epoch': now - 3}])
        self.assertEqual(ST.day_path(self.path('ebpf'), TODAY, 'node7', 'snapshot'), p)

    def test_json(self):
        now = time.time()
        self.write(self.path('node', TODAY, 'node7.jsonl'), [{'ts_epoch': now - 1}])
        facts = '\n'.join('%s=%s' % kv for kv in self.facts().items()) + '\nend\n'
        r = subprocess.run([sys.executable, str(_ROOT / 'lib' / 'ebpfm_status.py'), 'report', '--json',
                            '--code-dir', str(_ROOT), '--host', 'node7'], input=facts,
                           stdout=subprocess.PIPE, universal_newlines=True)
        d = json.loads(r.stdout)
        self.assertEqual((r.returncode, d['verdict'], d['exit'], d['tiers'][0]['state']),
                         (0, 'healthy', 0, 'ok'))
        self.assertIsNone(d['tiers'][0]['perf_lost'])


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
class CommandLineTest(Tmp):
    def test_help_on_a_command_runs_nothing(self):
        for cmd in ('start', 'stop', 'once', 'bootstrap', 'install', 'install-unit',
                    'uninstall-unit', 'overhead', 'status', 'tail'):
            rc, out, err = self.sh(cmd, '--help')
            self.assertEqual((rc, err), (0, ''), cmd)
            self.assertTrue(out.startswith('Usage: %s %s' % (SCRIPT, cmd)), out)
            rc, out, _ = self.sh(cmd, 'x', '-h') if cmd in ('once', 'tail') else self.sh(cmd, '-h')
            self.assertEqual(rc, 0, cmd)
        self.assertFalse(os.path.exists(self.path('run')), 'nothing started')
        self.assertFalse(os.path.exists(self.path('out')), 'nothing written')
        self.assertFalse(os.path.exists(self.path('units')), 'no unit written')

    def test_an_unknown_option_or_argument_is_refused(self):
        rc, out, err = self.sh('start', '--json')
        self.assertEqual((rc, out), (2, ''))
        self.assertEqual(err, "ebpfm: start: unknown option '--json' (see: %s start --help)\n" % SCRIPT)
        self.assertEqual(self.sh('stop', 'now')[0], 2)
        self.assertEqual(self.sh('tail', 'ten')[0], 2)
        self.assertEqual(self.sh('once', '0')[0], 2)
        rc, _, err = self.sh('statsu')
        self.assertEqual(rc, 2)
        self.assertIn("unknown command 'statsu'; did you mean 'status'?", err)
        self.assertFalse(os.path.exists(self.path('run')))

    def test_a_setting_a_tier_cannot_use_refuses_the_command(self):
        rc, out, err = self.sh('start', EBPFM_TIER='node', EBPFM_NODE_INTERVAL='five')
        self.assertEqual(rc, 2)
        self.assertIn("ebpfm: EBPFM_NODE_INTERVAL='five': expected seconds (>= 30)", err)
        self.assertFalse(os.path.exists(self.path('run')), 'no loop was started')
        # looking still works
        rc, out, err = self.sh('status', EBPFM_TIER='node', EBPFM_NODE_INTERVAL='five')
        self.assertEqual(rc, 3)
        rc, out, err = self.sh('help', EBPFM_TIER='both')
        self.assertEqual(rc, 0)

    def test_the_settings_file_is_read_as_systemd_reads_it(self):
        with open(self.path('default-ebpfm'), 'w') as fh:
            fh.write('# comment\n; also a comment\nEBPFM_NODE_INTERVAL = "45"\n  SNAPSHOT_MODE=full\n'
                     "EBPFM_NODE_TOP_N='10'\nEBPFM_NODE_TOP_N=12\nEBPFM_RESIDENCY=30\n#EBPFM_POLL_MS=1\n")
        rc, out, err = self.sh('config', '--json', EBPFM_NODE_TOP_N='7')
        self.assertEqual(rc, 0, err)
        rows = dict((r['name'], r) for r in json.loads(out)['settings'])
        f = self.path('default-ebpfm')
        self.assertEqual((rows['EBPFM_NODE_INTERVAL']['value'], rows['EBPFM_NODE_INTERVAL']['origin']),
                         ('45', f + ':3'))
        self.assertEqual((rows['SNAPSHOT_MODE']['value'], rows['SNAPSHOT_MODE']['origin']), ('full', f + ':4'))
        self.assertEqual((rows['EBPFM_NODE_TOP_N']['value'], rows['EBPFM_NODE_TOP_N']['origin']),
                         ('7', 'environment (overrides %s:6)' % f))
        self.assertEqual((rows['EBPFM_POLL_MS']['value'], rows['EBPFM_POLL_MS']['origin']), ('100', 'default'))
        unknown = json.loads(out)['unknown']
        self.assertEqual([(u['name'], u['did_you_mean']) for u in unknown],
                         [('EBPFM_RESIDENCY', 'EBPFM_RESIDENCY_S')])
        rc, out, _ = self.sh('config', 'NODE_INTERVAL')
        self.assertIn('EBPFM_NODE_INTERVAL', out)
        self.assertNotIn('EBPFM_POLL_MS', out)
        with open(self.path('default-ebpfm'), 'a') as fh:
            fh.write('EBPFM_POLL_MS=0\n')
        rc, out, _ = self.sh('config')
        self.assertEqual(rc, 2)
        self.assertIn('INVALID: expected milliseconds (>= 1)', out)

    def test_install_dry_run_changes_nothing(self):
        rc, out, err = self.sh('install', '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertIn('nothing was changed (--dry-run)', out)
        for step in ('1. dependencies', '2. settings', '3. units', '4. start', '5. verdict'):
            self.assertIn(step, out)
        self.assertIn('EBPFM_OUTPUT_DIR=%s' % self.path('out', 'ebpf'), out)
        for p in ('default-ebpfm', 'units', 'run', '99-ebpfm.conf'):
            self.assertFalse(os.path.exists(self.path(p)), p)
        for cmd in ('install-unit', 'uninstall-unit', 'bootstrap'):
            rc, out, err = self.sh(cmd, '--dry-run')
            self.assertEqual(rc, 0, (cmd, err))
            self.assertIn('nothing was changed (--dry-run)', out, cmd)
        self.assertFalse(os.path.exists(self.path('units')))

    def test_install_refuses_beside_the_package(self):
        open(self.path('no-package'), 'w').close()
        rc, out, err = self.sh('install', '--dry-run')
        self.assertEqual(rc, 1)
        self.assertIn('the ebpfm-collector package is installed', err)


class TemporaryCopyTest(Tmp):
    def test_units_are_not_installed_from_a_bundles_temporary_copy(self):
        d = self.path('ebpfm.XyZ', 'collector')
        shutil.copytree(str(_ROOT), d, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        with open(os.path.join(d, '.ebpfm-bundle'), 'w') as fh:
            fh.write('/home/me/ebpfm-bundle.sh\n')
        for cmd in ('install-unit', 'install'):
            rc, out, err = self.sh(cmd, '--dry-run', script=os.path.join(d, 'ebpfm.sh'))
            self.assertEqual(rc, 1, cmd)
            self.assertIn('temporary folder', err)
            self.assertIn('sudo /home/me/ebpfm-bundle.sh --extract /opt/ebpfm && sudo /opt/ebpfm/ebpfm.sh %s'
                          % cmd, err)

    def test_the_bundle_cleans_up_and_extracts_for_good(self):
        out = self.path('b.sh')
        rc, so, err = self.sh('bundle', out)
        self.assertEqual(rc, 0, err)
        tmpdir = self.path('tmp')
        os.makedirs(tmpdir)
        r = subprocess.run(['bash', out, 'version'], env=self.env(TMPDIR=tmpdir), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'ebpfm %s\n' % V.collector_version(str(_ROOT))[0])
        self.assertEqual(os.listdir(tmpdir), [], 'the temporary copy is gone')
        dest = self.path('opt')
        r = subprocess.run(['bash', out, '--extract', dest], env=self.env(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ('ebpfm.sh', 'VERSION', 'OPERATIONS.md', 'lib', 'ebpfm-node.service'):
            self.assertTrue(os.path.exists(os.path.join(dest, name)), name)
        self.assertFalse(os.path.exists(os.path.join(dest, '.ebpfm-bundle')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'lib', '__pycache__')))


# ---------------------------------------------------------------------------
# the node tier, run for real with a stand-in snapshot
# ---------------------------------------------------------------------------
SNAP_OK = ('import json, sys, time\n'
           'print(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),\n'
           '                  "hostname": sys.argv[1], "ts_epoch": time.time()}))\n')
SNAP_FAIL = 'import sys\nsys.stderr.write("snapshot: cannot read /proc\\n")\nsys.exit(1)\n'


class NodeTierTest(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        self.col = self.path('collector')
        shutil.copytree(str(_ROOT), self.col, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        self.script = os.path.join(self.col, 'ebpfm.sh')
        self.addCleanup(self.stop)

    def snapshot(self, body):
        with open(os.path.join(self.col, 'node_snapshot.py'), 'w') as fh:
            fh.write(body)

    def run_(self, *args, **env):
        env.setdefault('EBPFM_TIER', 'node')
        env.setdefault('EBPFM_NODE_INTERVAL', '30')
        return self.sh(*args, script=self.script, **env)

    def stop(self):
        self.run_('stop')

    def test_start_waits_for_a_snapshot_and_status_follows_the_loop(self):
        self.snapshot(SNAP_OK)
        custom = self.path('custom')
        rc, out, err = self.run_('start', EBPFM_NODE_OUTPUT_DIR=custom)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('waiting for its first snapshot (up to 60s) ... ok', out)
        rc, out, _ = self.run_('start')
        self.assertEqual(rc, 0, 'already running is not a failure')
        self.assertIn('already running', out)
        # a fresh shell, which would look in the default folder: the loop's note leads to custom
        rc, out, err = self.run_('status')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('node tier: RUNNING', out)
        self.assertIn(os.path.join(custom, TODAY, socket_host() + '.jsonl'), out)
        self.assertIn('it writes to %s' % custom, out)
        rc, out, _ = self.run_('status', '--json')
        d = json.loads(out)
        self.assertEqual((rc, d['verdict'], d['tiers'][0]['dir']), (0, 'healthy', custom))
        rc, out, _ = self.run_('tail', '1', 'node')
        self.assertEqual(rc, 0)
        self.assertIn('"ts_epoch"', out)
        state = os.path.join(self.path('run'), 'ebpfm-node_%s.env' % socket_host())
        self.assertTrue(os.path.exists(state))
        rc, out, _ = self.run_('stop')
        self.assertEqual(rc, 0)
        self.assertIn('node tier stopped', out)
        self.assertFalse(os.path.exists(state), 'the loop takes its note with it')
        rc, out, _ = self.run_('status')
        self.assertEqual(rc, 3)
        rc, out, _ = self.run_('stop')
        self.assertEqual((rc, out.strip()), (0, 'node tier not running on %s' % socket_host()))

    def test_a_failing_first_snapshot_fails_start(self):
        self.snapshot(SNAP_FAIL)
        rc, out, err = self.run_('start')
        self.assertEqual(rc, 1, out + err)
        self.assertIn('FAILED: its first snapshot failed', out)
        self.assertIn('snapshot: cannot read /proc', out)

    def test_no_wait(self):
        self.snapshot(SNAP_OK)
        rc, out, err = self.run_('start', '--no-wait')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('not waiting for its first snapshot (--no-wait)', out)


def socket_host():
    return subprocess.run(['hostname', '-s'], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


# ---------------------------------------------------------------------------
# the eBPF tier's start through systemd-run, with stand-ins for root and systemd
# ---------------------------------------------------------------------------
FAKE_TRACER = ('import json, os, signal, sys, time\n'
               'host = sys.argv[1]\n'
               'root = os.environ["EBPFM_OUTPUT_DIR"]\n'
               'signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n'
               'time.sleep(1)\n'
               'd = os.path.join(root, time.strftime("%Y-%m-%d"))\n'
               'os.makedirs(d, exist_ok=True)\n'
               'with open(os.path.join(d, host + ".snapshot.jsonl"), "a") as fh:\n'
               '    fh.write(json.dumps({"event": "meta", "collector_pid": os.getpid(),\n'
               '                         "ts_epoch": time.time()}) + "\\n")\n'
               'sys.stderr.write("ebpfm v running on %s (features: core)\\n" % host)\n'
               'time.sleep(60)\n')
# systemd-run: starts the command in the background as the "unit", unless STUB_DIES;
# systemctl: answers for that one process.
SYSTEMD_RUN = ('#!/bin/sh\necho "systemd-run $*" >> "$STUB_LOG"\n'
               '[ -n "$STUB_DIES" ] && exit 0\n'
               'while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done\n'
               '"$@" >/dev/null 2>&1 &\necho $! > "$STUB_PID"\nexit 0\n')
SYSTEMCTL = ('#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n'
             'pid=$(cat "$STUB_PID" 2>/dev/null)\n'
             'alive() { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; }\n'
             'case "$1" in\n'
             '  is-active) case "$*" in *ebpfm-*) alive && exit 0 ;; esac; exit 3 ;;\n'
             '  show) case "$*" in *MainPID*) alive && echo "$pid" || echo 0 ;; *) echo /system.slice/x ;; esac ;;\n'
             '  stop) alive && kill "$pid" ;;\n'
             '  --version) echo "systemd 255 (255.4)" ;;\n'
             'esac\nexit 0\n')


class EbpfStartTest(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        self.col = self.path('collector')
        shutil.copytree(str(_ROOT), self.col, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        with open(os.path.join(self.col, 'ebpf_trace.py'), 'w') as fh:
            fh.write(FAKE_TRACER)
        self.bin = self.path('bin')
        os.makedirs(self.bin)
        os.makedirs(self.path('systemd'))
        for name, body in (('id', '#!/bin/sh\n[ "$1" = -u ] && echo 0 || exec /usr/bin/id "$@"\n'),
                           ('systemd-run', SYSTEMD_RUN), ('systemctl', SYSTEMCTL)):
            p = os.path.join(self.bin, name)
            with open(p, 'w') as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.addCleanup(self.kill)

    def kill(self):
        try:
            with open(self.path('mainpid')) as fh:
                os.kill(int(fh.read()), 9)
        except (OSError, ValueError):
            pass

    def run_(self, *args, **env):
        e = dict(PATH=self.bin + ':' + os.environ['PATH'], STUB_LOG=self.path('calls'),
                 STUB_PID=self.path('mainpid'), EBPFM_SYSTEMD_DIR=self.path('systemd'),
                 EBPFM_TIER='ebpf')
        e.update(env)
        return self.sh(*args, script=os.path.join(self.col, 'ebpfm.sh'), **e)

    def test_a_unit_that_dies_at_once_is_a_failure_not_pid_0(self):
        rc, out, err = self.run_('start', STUB_DIES='1')
        self.assertEqual(rc, 1, out + err)
        self.assertIn('FAILED to start', err)
        self.assertNotIn('pid 0', out + err)
        self.assertFalse(os.path.exists(os.path.join(self.path('run'), 'ebpfm_%s.pid' % socket_host())))

    def test_a_unit_is_waited_for_until_its_meta_record(self):
        rc, out, err = self.run_('start')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('as unit ebpfm-%s.service (pid ' % socket_host(), out)
        self.assertIn('write its meta record', out)
        self.assertIn(' ok', out)
        with open(self.path('calls')) as fh:
            calls = fh.read()
        self.assertIn('--slice=system.slice', calls)
        self.assertIn('--setenv=EBPFM_OUTPUT_DIR=%s' % self.path('out', 'ebpf'), calls)
        rc, out, err = self.run_('status')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('ebpf tier: RUNNING', out)
        rc, out, err = self.run_('stop')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('stopped', out)


# ---------------------------------------------------------------------------
# the pieces of ebpfm.sh that need root or systemd, run on their own
# ---------------------------------------------------------------------------
class ShellPiecesTest(Tmp):
    def test_await_ebpf(self):
        """start's wait for the tracer: its meta record, its death, or the timeout."""
        out_dir = self.path('out')
        log = self.path('t.log')
        open(log, 'w').close()
        fns = _fn('_log_since') + _fn('_log_tail') + _fn('_log_name') + _fn('_await_ebpf')

        def rig(wait):
            return ('DIR=%s; OUTPUT_DIR=%s; HOST=node7; OPT_WAIT=%d; PROG=ebpfm\n'
                    'PID_FILE=%s; STATE_FILE=%s\n'
                    % (_ROOT, out_dir, wait, self.path('p'), self.path('s')) + fns)
        writer = ('import json, os, sys, time\ntime.sleep(1.5)\n'
                  'd = os.path.join(sys.argv[1], time.strftime("%Y-%m-%d"))\n'
                  'os.makedirs(d, exist_ok=True)\n'
                  'open(os.path.join(d, "node7.snapshot.jsonl"), "a").write(json.dumps('
                  '{"event": "meta", "collector_pid": os.getpid()}) + "\\n")\n'
                  'sys.stderr.write("ebpfm v running on node7 (features: core)\\n")\ntime.sleep(30)\n')
        w = self.path('w.py')
        with open(w, 'w') as fh:
            fh.write(writer)
        logf = open(log, 'a')
        self.addCleanup(logf.close)
        p = subprocess.Popen([sys.executable, w, out_dir], stderr=logf)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        rc, out, _ = _bash(rig(20) + '_await_ebpf %d %s 0\n' % (p.pid, log))
        self.assertEqual(rc, 0, out)
        self.assertIn(' ok', out)
        self.assertIn('attached: core', out)
        dead = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stderr.write("error: python3-bcc not found\\n")'],
                                stderr=logf)
        dead.wait()
        rc, out, _ = _bash(rig(20) + '_await_ebpf %d %s 0\n' % (dead.pid, log))
        self.assertEqual(rc, 1, out)
        self.assertIn('FAILED: it exited', out)
        self.assertIn('python3-bcc not found', out)
        quiet = subprocess.Popen(['sleep', '30'])
        self.addCleanup(quiet.wait)
        self.addCleanup(quiet.kill)
        rc, out, _ = _bash(rig(2) + '_await_ebpf %d %s 0\n' % (quiet.pid, log))
        self.assertEqual(rc, 4, out)
        self.assertIn('not yet after 2s', out)

    def test_alive_counts_a_zombie_as_exited(self):
        """stop waits for the tier to exit. A tier `start` left behind is reaped by PID 1,
        and in a container whose PID 1 reaps nothing it stays a zombie, which kill -0
        still finds: stop said "stopping" for good."""
        fn = _fn('_alive')
        live = subprocess.Popen(['sleep', '30'])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        zombie = subprocess.Popen(['true'])
        for _ in range(100):            # exited, and left unreaped: a zombie
            with open('/proc/%d/stat' % zombie.pid) as fh:
                if fh.read().rsplit(') ', 1)[1].startswith('Z'):
                    break
            time.sleep(0.05)
        self.addCleanup(zombie.wait)
        odd = self.path('a) b')         # a name with ") " in it: /proc/PID/stat's (comm)
        shutil.copy(shutil.which('sleep'), odd)
        named = subprocess.Popen([odd, '30'])
        self.addCleanup(named.wait)
        self.addCleanup(named.kill)
        gone = subprocess.Popen(['true'])
        gone.wait()
        for pid, want in ((live.pid, 0), (zombie.pid, 1), (named.pid, 0), (gone.pid, 1)):
            rc, out, err = _bash(fn + '_alive %d\n' % pid)
            self.assertEqual(rc, want, (pid, out, err))

    def test_the_settings_file_writer(self):
        f = self.path('default-ebpfm')
        with open(f, 'w') as fh:
            fh.write('# site\nEBPFM_MIN_UID=0\n#EBPFM_OUTPUT_DIR=/scratch/ebpfm\nEBPFM_GROUP=lab\n'
                     ' EBPFM_NODE_OUTPUT_DIR = /old\nEBPFM_NODE_OUTPUT_DIR=/older\n')
        rig = ('declare -A _FILE_VALS=()\n_settings_py() { python3 %s/lib/ebpfm_settings.py "$@"; }\n'
               % _ROOT + _fn('_settings_file_write'))
        rc, out, err = _bash(rig + '_settings_file_write %s EBPFM_OUTPUT_DIR "/data/e b/ebpfm" '
                                   'EBPFM_NODE_OUTPUT_DIR /data/node\n' % f)
        self.assertEqual((rc, err), (0, ''))
        with open(f) as fh:
            self.assertEqual(fh.read(), '# site\nEBPFM_MIN_UID=0\nEBPFM_OUTPUT_DIR=/data/e b/ebpfm\n'
                                        'EBPFM_GROUP=lab\nEBPFM_NODE_OUTPUT_DIR=/data/node\n')
        g = self.path('new', 'default-ebpfm')
        rc, out, err = _bash(rig + '_settings_file_write %s EBPFM_OUTPUT_DIR /x\n' % g)
        self.assertEqual((rc, err), (0, ''))
        with open(g) as fh:
            text = fh.read()
        self.assertIn('\nEBPFM_OUTPUT_DIR=/x\n', text)
        self.assertNotIn('#EBPFM_OUTPUT_DIR=', text)
        self.assertEqual(text.count('EBPFM_OUTPUT_DIR='), 1)
        self.assertIn('EBPFM_MIN_UID=0', text)

    def test_install_marks_its_own_snapshot_mode_and_takes_only_that_out(self):
        f = self.path('default-ebpfm')
        with open(f, 'w') as fh:
            fh.write('EBPFM_MIN_UID=0\n#SNAPSHOT_MODE=node\n')
        src = Path(SCRIPT).read_text(encoding='utf-8')
        mark = re.search(r"^_INSTALL_MARK='([^']*)'", src, re.M).group(0)
        rig = ('declare -A _FILE_VALS=()\n_settings_py() { :; }\n' + mark + '\n'
               + _fn('_settings_file_write') + _fn('_mark_full') + _fn('_unmark_full'))
        rc, out, err = _bash(rig + '_mark_full %s >/dev/null; cat %s\n' % (f, f))
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r'^EBPFM_MIN_UID=0\n# `ebpfm install` set the next line[^\n]*\nSNAPSHOT_MODE=full\n')
        rc, out, err = _bash(rig + '_unmark_full %s && cat %s\n' % (f, f))
        self.assertEqual((rc, out), (0, 'EBPFM_MIN_UID=0\n'))
        with open(f, 'w') as fh:
            fh.write('SNAPSHOT_MODE=full\n')          # the admin's own: kept
        rc, out, err = _bash(rig + '_unmark_full %s; echo rc=$?; cat %s\n' % (f, f))
        self.assertEqual(out, 'rc=1\nSNAPSHOT_MODE=full\n')

    def test_the_verdict(self):
        rig = ('_US=$\'\\x1f\'\nwant_node() { [ "$T" != ebpf ]; }\nwant_ebpf() { [ "$T" != node ]; }\n'
               + _fn('_verdict'))

        def verdict(t, node, ebpf):
            arrays = 'CK_FAILS_NODE=(%s); CK_FAILS_EBPF=(%s)\n' % (
                ' '.join('"%s$_US""m$_US""f"' % n for n in node),
                ' '.join('"%s$_US""m$_US""f"' % n for n in ebpf))
            return _bash(rig + 'T=%s\n' % t + arrays + '_verdict; echo "rc=$?"\n')[1]
        self.assertEqual(verdict('all', [], []), 'READY: both tiers\nrc=0\n')
        self.assertEqual(verdict('node', [], []), 'READY: node tier only\nrc=0\n')
        self.assertEqual(verdict('all', [], ['python3-bcc']),
                         'READY: node tier only (the eBPF tier is NOT READY: 1 FAIL)\n'
                         '  - python3-bcc: m\n      fix: f\nrc=1\n')
        self.assertTrue(verdict('all', ['ps'], ['bcc']).startswith('NOT READY: 2 FAIL\n  - ps: m\n'))


if __name__ == '__main__':
    unittest.main()
