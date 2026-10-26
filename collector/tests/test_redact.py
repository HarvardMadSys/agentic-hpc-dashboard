"""lib/redact.py and its use by the tracer: credentials and coding-agent prompts
masked in every command line the collector writes, raw argv kept in memory.

    python3 -m unittest discover collector/tests
"""
import contextlib
import ctypes
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('EBPFM_OUTPUT_DIR', '/tmp/ebpfm_test_out')
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'lib'))

import redact as R               # noqa: E402
import ebpf_trace as T           # noqa: E402

P = R.PROMPT_MASK
K = R.MASK


def red(argv, **kw):
    return R.redact_argv(argv, **kw)[0]


class KeysTest(unittest.TestCase):
    def test_a_key_in_an_env_prefix(self):
        out = red(['bash', '-c', 'OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx python run.py'])
        self.assertNotIn('sk-proj-abc', out)
        self.assertIn('OPENAI_API_KEY=%s' % K, out)

    def test_an_authorization_header(self):
        out = red(['curl', '-H', 'Authorization: Bearer abc.def.ghi', 'https://api.x.com'])
        self.assertEqual(out, 'curl -H Authorization: Bearer %s https://api.x.com' % K)

    def test_a_key_flag_and_a_bare_token(self):
        self.assertEqual(red(['tool', '--api-key', 'abc123']), 'tool --api-key %s' % K)
        self.assertEqual(red(['git', 'push', 'https://ghp_%s@github.com/o/r' % ('a' * 36)]),
                         'git push https://%s@github.com/o/r' % K)

    def test_a_reference_to_a_secret_is_not_one(self):
        self.assertEqual(red(['tool', '--token', '$HF_TOKEN']), 'tool --token $HF_TOKEN')

    def test_ordinary_lines_pass_untouched(self):
        for argv in (['python', 'train.py', '--max_tokens=100'], ['ls', '-la'],
                     ['git', 'log', '--oneline']):
            self.assertEqual(R.redact_argv(argv), (' '.join(argv), None))

    def test_masking_twice_changes_nothing(self):
        once = red(['tool', '--password', 'hunter2'])
        self.assertEqual(R.redact_line(once)[0], once)

    def test_the_rules_that_fired_are_named(self):
        self.assertEqual(R.redact_keys('tool --token abc')[1], ['secret_flag'])


class PromptTest(unittest.TestCase):
    def check(self, argv, want):
        self.assertEqual(red(argv), want)

    # --- the agents themselves ------------------------------------------------
    def test_claude_print_mode(self):
        self.check(['claude', '-p', 'fix the failing test', '--output-format', 'json'],
                   'claude -p %s --output-format json' % P)

    def test_claude_with_an_initial_prompt(self):
        self.check(['claude', 'explain this repository'], 'claude %s' % P)

    def test_names_ids_and_flags_stay(self):
        self.check(['claude', '--model', 'sonnet', '--resume', '1f2e3d4c', 'carry on',
                    '--dangerously-skip-permissions'],
                   'claude --model sonnet --resume 1f2e3d4c %s --dangerously-skip-permissions' % P)

    def test_a_value_that_reads_as_prose_is_masked_whatever_its_flag(self):
        self.check(['claude', '-r', 'please carry on with the plan'], 'claude -r %s' % P)

    def test_system_prompts_are_prompts(self):
        self.check(['claude', '--append-system-prompt', 'be terse', '-p', 'go'],
                   'claude --append-system-prompt %s -p %s' % (P, P))

    def test_one_mask_per_run_of_prompt_text(self):
        self.check(['claude', 'fix', 'the', 'bug'], 'claude %s' % P)

    def test_admin_subcommands_have_no_prompt(self):
        argv = ['claude', 'mcp', 'add', 'srv', '--', 'npx', 'some-server']
        self.check(argv, ' '.join(argv))

    def test_a_bare_agent_is_left_alone(self):
        self.assertEqual(R.redact_argv(['claude']), ('claude', None))
        self.assertEqual(R.redact_argv(['codex', 'app-server']), ('codex app-server', None))

    def test_codex(self):
        self.check(['codex', 'exec', '--json', 'do the thing'], 'codex exec --json %s' % P)
        self.check(['codex', '-m', 'o3', '-c', 'model_reasoning_effort="high"', 'do it'],
                   'codex -m o3 -c model_reasoning_effort="high" %s' % P)

    def test_gemini_and_its_forms(self):
        self.check(['gemini', '-p', 'summarize', '-m', 'gemini-2.5-pro'],
                   'gemini -p %s -m gemini-2.5-pro' % P)
        self.check(['gemini', '--prompt=two words'], 'gemini --prompt=%s' % P)

    def test_aiders_positionals_are_files(self):
        self.check(['aider', '--model', 'sonnet', '-m', 'add tests', 'src/a.py'],
                   'aider --model sonnet -m %s src/a.py' % P)

    def test_ollama_keeps_the_model(self):
        self.check(['ollama', 'run', 'llama3', 'why is the sky blue'], 'ollama run llama3 %s' % P)
        self.check(['ollama', 'pull', 'llama3'], 'ollama pull llama3')

    def test_opencode_names_a_directory_unless_it_runs(self):
        self.check(['opencode', '/home/u/proj'], 'opencode /home/u/proj')
        self.check(['opencode', 'run', 'add a test'], 'opencode run %s' % P)

    def test_a_generic_name_is_an_agent_only_with_its_subcommand(self):
        self.check(['q', 'chat', 'hello there'], 'q chat %s' % P)
        self.check(['q', '-d', ',', 'select c1 from data.csv'], 'q -d , select c1 from data.csv')

    def test_after_double_dash_everything_is_text(self):
        self.check(['claude', '-p', '--', '--looks like a flag'], 'claude -p -- %s' % P)

    # --- however it is launched -----------------------------------------------
    def test_through_an_interpreter(self):
        self.check(['node', '/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js', '-p', 'x y'],
                   'node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js -p %s' % P)
        self.check(['/usr/bin/env', 'node', '--no-warnings', '/home/u/.npm/bin/claude', 'hi there'],
                   '/usr/bin/env node --no-warnings /home/u/.npm/bin/claude %s' % P)
        self.check(['python3', '-m', 'aider', '-m', 'refactor this'],
                   'python3 -m aider -m %s' % P)

    def test_through_a_package_runner(self):
        self.check(['npx', '-y', '@anthropic-ai/claude-code@1.0.3', '-p', 'go now'],
                   'npx -y @anthropic-ai/claude-code@1.0.3 -p %s' % P)
        self.check(['uvx', '--from', 'aider-chat', 'aider', '-m', 'go now'],
                   'uvx --from aider-chat aider -m %s' % P)

    def test_through_wrappers(self):
        for pre in (['timeout', '600'], ['nohup'], ['env', 'FOO=1'], ['nice', '-n', '10'],
                    ['srun', '--pty', '-p', 'gpu'], ['ssh', '-p', '22', 'login1'],
                    ['sudo', '-u', 'alice']):
            self.check(pre + ['claude', '-p', 'do it now'],
                       ' '.join(pre) + ' claude -p %s' % P)

    def test_inside_a_shell_command_string(self):
        self.check(['bash', '-c', "cd /repo && claude -p 'fix it please'"],
                   'bash -c cd /repo && claude -p %s' % P)
        self.check(['sbatch', '--wrap=codex exec "run the suite"'],
                   'sbatch --wrap=codex exec %s' % P)
        self.check(['tmux', 'new-session', '-d', "claude -p 'hello there'"],
                   'tmux new-session -d claude -p %s' % P)
        self.check(['ssh', 'login1', "claude -p 'hello there'"], 'ssh login1 claude -p %s' % P)
        self.check(['su', '-', 'alice', '-c', "codex exec 'go on'"],
                   'su - alice -c codex exec %s' % P)
        self.check(['bash', '-c', '-l', "claude -p 'x y'"], 'bash -c -l claude -p %s' % P)
        self.check(['nohup', 'bash', '-lc', "claude -p 'x y'"], 'nohup bash -lc claude -p %s' % P)

    def test_a_command_string_cut_short_is_masked_from_the_agent_on(self):
        self.check(['bash', '-lc', 'claude -p "unterminated and cut'],
                   'bash -lc claude %s' % P)

    # --- what is not an agent run ----------------------------------------------
    def test_mentions_of_an_agent_are_not_runs_of_one(self):
        for argv in (['grep', 'claude', 'notes.txt'], ['git', 'commit', '-m', 'claude fixed it'],
                     ['ls', '/home/u/.claude'], ['vim', 'claude.md'], ['echo', 'codex'],
                     ['echo', 'claude -p is how you run it'],
                     ['timeout', '60', 'git', 'commit', '-m', 'claude -p fix']):
            self.assertEqual(R.redact_argv(argv), (' '.join(argv), None), argv)

    def test_claude_codes_own_tool_commands_pass_untouched(self):
        cmd = ("source /home/u/.claude/shell-snapshots/snapshot-bash-1.sh && eval 'git status' "
               "\\< /dev/null && pwd -P >| /tmp/claude-a1b2-cwd")
        self.assertEqual(R.redact_argv(['bash', '-c', '-l', cmd]),
                         (' '.join(['bash', '-c', '-l', cmd]), None))

    def test_codexs_sandbox_helper_runs_tool_commands_not_prompts(self):
        argv = ['/x/@openai/codex/vendor/x86_64-unknown-linux-musl/codex/codex-linux-sandbox',
                '--sandbox-policy', 'workspace-write', '--', 'bash', '-lc', 'rg TODO src']
        self.assertEqual(red(argv), ' '.join(argv))

    # --- the two entry points ----------------------------------------------------
    def test_what_was_masked_is_named(self):
        self.assertEqual(R.redact_argv(['claude', '-p', 'x'])[1], ['prompt'])
        self.assertEqual(R.redact_argv(['claude', '-p', 'x', '--api-key', 'abc'])[1],
                         ['prompt', 'secret_flag'])

    def test_each_kind_can_be_left_out(self):
        self.assertEqual(red(['claude', '-p', 'x'], prompts=False), 'claude -p x')
        self.assertEqual(red(['tool', '--api-key', 'abc'], keys=False), 'tool --api-key abc')
        self.assertEqual(red(['claude', '-p', 'x', '--model', 'k-abc'], keys=False),
                         'claude -p %s --model k-abc' % P)

    def test_a_flag_the_table_does_not_know(self):
        """One short word after an unknown long flag is its value (`--effort
        max`, seen on the fleet); prose after one, or anything after an unknown
        short flag, is still taken for a prompt."""
        self.assertEqual(red(['claude', '--brand-new-flag', 'max']), 'claude --brand-new-flag max')
        self.assertEqual(red(['claude', '--brand-new-flag', 'fix the bug']),
                         'claude --brand-new-flag %s' % P)
        self.assertEqual(red(['claude', '-z', 'word']), 'claude -z %s' % P)
        self.assertEqual(red(['claude', '--verbose', 'continue']), 'claude --verbose %s' % P)

    def test_seen_on_the_fleet(self):
        argv = ['/n/home08/u/.local/bin/claude', '--resume', '1bc97dd3-507d-42d7-9b4c-6ff41b4698f6',
                '--model', 'opus', '--effort', 'max', '--permission-mode', 'auto']
        self.assertEqual(R.redact_argv(argv), (' '.join(argv), None))
        argv = ['/x/anthropic.claude-code-2.1.310-linux-x64/resources/native-binary/claude',
                'auth', 'status', '--json']
        self.assertEqual(R.redact_argv(argv), (' '.join(argv), None))
        script = 'D=/n/lab/data/droid\necho "=== $D"\ndu -sh $D 2>/dev/null\n'
        self.assertEqual(R.redact_argv(['bash', '-c', script]), ('bash -c ' + script.strip(), None))

    def test_a_script_the_lexer_cannot_split_is_masked_only_at_an_agent_run(self):
        heredoc = ("cd /repo && python3 - <<'EOF'\nq = Path('x')\ns = s.replace('''a\nEOF")
        self.assertEqual(R.redact_argv(['bash', '-c', heredoc])[1], None)
        script = "cat > run.sh <<'SH'\npython -m runner --planner qwen --x 'y\nSH"
        self.assertEqual(R.redact_argv(['bash', '-c', script])[1], None)
        self.assertEqual(red(['bash', '-c', "cd /r && claude -p 'unterminated"]),
                         'bash -c cd /r && claude %s' % P)

    def test_usage_screens_and_ids_and_json_are_not_prompts(self):
        for argv in (['codex', 'exec', 'review', '--help'], ['claude', '--version'],
                     ['codex', 'resume', '0199a5c2-5b1e-7f20-9c3a-1b2c3d4e5f60'],
                     ['claude', '--mcp-config', '{"mcpServers": {"x": {"command": "npx"}}}',
                      '--allowedTools=Bash(git:*) Edit Read']):
            self.assertEqual(R.redact_argv(argv), (' '.join(argv), None), argv)

    def test_a_string_in_a_python_heredoc_is_not_a_command(self):
        script = "python3 - <<'EOF'\nprint('codex exec is the runner', codex)\nEOF"
        self.assertEqual(R.redact_argv(['bash', '-c', script])[1], None)

    def test_a_lone_path_is_a_script_not_a_prompt(self):
        argv = ['/x/.local/bin/cursor-agent', '--use-system-ca',
                '/x/.local/share/cursor-agent/versions/2026.10.22/index.js', '--api-key', 'crsr_abc']
        self.assertEqual(red(argv), ' '.join(argv[:3]) + ' --api-key ' + K)

    # --- the review's leaks ----------------------------------------------------
    def test_operators_split_commands_glued_or_not(self):
        """Python 3.6's shlex left `;` glued to a word under whitespace_split, so
        on EL8 the agent run after it was never found."""
        for script, want in (
                ('cd /proj; claude -p "secret plan"', 'cd /proj ; claude -p %s' % P),
                ('cd /proj;claude -p "secret plan"', 'cd /proj ; claude -p %s' % P),
                ('cat x|claude -p "sum it up"', 'cat x | claude -p %s' % P),
                ('echo $(claude -p "sum it up")', "echo '$' ( claude -p %s )" % P),
                ('echo `claude -p "sum it up"`', 'echo ` claude -p %s `' % P)):
            self.assertEqual(R.redact_argv(['bash', '-c', script])[0], 'bash -c ' + want, script)

    def test_the_tokenizer_reads_quotes_as_sh_does(self):
        self.assertEqual(R.shell_split('a "b c" \'d e\' f\\ g "h\\"i"'),
                         [('a', False), ('b c', False), ('d e', False), ('f g', False),
                          ('h"i', False)])
        self.assertEqual(R.shell_split('a #b c\nd'), [('a', False), ('\n', True), ('d', False)])
        with self.assertRaises(ValueError):
            R.shell_split('claude -p "unterminated')

    def test_a_prompt_written_as_a_list_is_masked(self):
        self.check(['claude', '-p', '- rotate the prod DB password\n- then restart'],
                   'claude -p %s' % P)
        self.check(['codex', 'exec', '- fix the failing test'], 'codex exec %s' % P)
        self.check(['claude', '-p', '- set FOO=bar in the config'], 'claude -p %s' % P)

    def test_containers_and_other_wrappers(self):
        for pre in (['apptainer', 'exec', '--nv', '-B', '/n:/n', 'img.sif'],
                    ['singularity', 'exec', 'img.sif'], ['screen', '-dmS', 'work'],
                    ['flock', '-w', '5', '/tmp/l'], ['numactl', '--cpunodebind=0', '-m', '0'],
                    ['systemd-run', '--user', '--scope', '-p', 'MemoryMax=1G'],
                    ['conda', 'run', '-n', 'agents'], ['poetry', 'run'], ['unbuffer']):
            self.check(pre + ['claude', '-p', 'do it now'], ' '.join(pre) + ' claude -p %s' % P)
        self.check(['flock', '/tmp/l', '-c', "claude -p 'do it now'"],
                   'flock /tmp/l -c claude -p %s' % P)

    def test_a_joined_line_takes_no_subcommand_or_usage_word_on_trust(self):
        """The node tier only has `ps`'s joined line, where a prompt's first word
        can be a subcommand's name and a `-v` inside it a flag."""
        self.assertEqual(R.redact_line('claude -p update the README with the new keys')[0],
                         'claude -p update %s' % P)
        self.assertEqual(R.redact_line('claude -p fix the -v flag handling')[0],
                         'claude -p %s -v %s' % (P, P))

    def test_a_newline_ends_a_command_in_a_script(self):
        self.check(['bash', '-c', "cd /repo\nclaude -p 'fix it now'\necho done"],
                   'bash -c cd /repo \n claude -p %s \n echo done' % P)

    def test_a_joined_line_collapses_the_prompts_words(self):
        self.assertEqual(R.redact_line('claude -p fix the failing test --output-format json')[0],
                         'claude -p %s --output-format json' % P)

    def test_a_joined_line_keeps_nothing_that_might_be_a_prompts_word(self):
        self.assertEqual(R.redact_line('codex exec read reports/c750.md and summarize')[0],
                         'codex exec %s' % P)
        self.assertEqual(R.redact_line('claude --remote-control Usage of the API')[0],
                         'claude --remote-control %s' % P)

    def test_a_joined_shell_line_takes_the_rest_as_its_command(self):
        self.assertEqual(R.redact_line('bash -c claude -p hi there')[0],
                         'bash -c claude -p %s' % P)

    def test_boundaries_keep_a_flag_inside_a_prompt_masked(self):
        """Joined, `--force` inside the prompt reads as a flag and survives."""
        self.assertEqual(red(['claude', '-p', 'use --force carefully']), 'claude -p %s' % P)


class DashboardSyncTest(unittest.TestCase):
    def test_the_credential_rules_are_the_dashboards(self):
        dash = _ROOT.parent / 'dashboard' / 'rc_dashboard' / 'redact.py'
        if not dash.exists():
            self.skipTest('no dashboard alongside this collector')
        spec = importlib.util.spec_from_file_location('dashboard_redact', str(dash))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.MASK, R.MASK)
        self.assertEqual([(n, g, rx.pattern, rx.flags) for n, g, rx, _s in mod.RULES],
                         [(n, g, rx.pattern, rx.flags) for n, g, rx, _s in R.RULES])


class _ArgvT(ctypes.Structure):
    _fields_ = [('pid', ctypes.c_uint32), ('len', ctypes.c_uint32),
                ('raw_len', ctypes.c_uint32), ('_pad', ctypes.c_uint32),
                ('ktime_ns', ctypes.c_uint64), ('buf', ctypes.c_char * 4096)]


def _argv_ev(pid, data):
    a = _ArgvT()
    a.pid, a.len, a.raw_len, a.ktime_ns = pid, len(data), len(data), 1
    ctypes.memmove(ctypes.addressof(a) + _ArgvT.buf.offset, data, len(data))
    return a


class Ev(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class CollectorTest(unittest.TestCase):
    """The tracer masks what it writes and nothing it reads."""

    def setUp(self):
        T.proc.clear(); T._children.clear()
        self._saved = (T.REDACT, T.REDACT_KEYS, T.REDACT_PROMPTS, T.ARGS_MAXLEN, T._tgid_of,
                       T.read_cwd)
        T._tgid_of = lambda pid: pid
        T.read_cwd = lambda pid: None

    def tearDown(self):
        (T.REDACT, T.REDACT_KEYS, T.REDACT_PROMPTS, T.ARGS_MAXLEN, T._tgid_of,
         T.read_cwd) = self._saved

    def _ident(self, pid):
        return T._base_identity(T.proc[pid], 'agent', 'ancestry')

    def test_the_record_is_masked_and_the_table_is_not(self):
        T.proc_add(100, 1, comm='claude')
        T.on_argv(_argv_ev(100, b'claude\x00-p\x00refactor auth\x00--api-key\x00abc123\x00'))
        i = self._ident(100)
        self.assertEqual(i['args'], 'claude -p %s --api-key %s' % (P, K))
        self.assertEqual(i['args_redacted'], ['prompt', 'secret_flag'])
        self.assertEqual(T.proc[100]['args'], 'claude -p refactor auth --api-key abc123')
        self.assertEqual(T.proc[100]['agent_type'] or T.actor_of(T.proc[100])[0], 'agent')

    def test_the_kernels_boundaries_are_used(self):
        T.proc_add(101, 1, comm='claude')
        T.on_argv(_argv_ev(101, b'claude\x00-p\x00use --force carefully\x00'))
        self.assertEqual(self._ident(101)['args'], 'claude -p %s' % P)

    def test_a_fork_keeps_its_parents_masking(self):
        T.proc_add(102, 1, comm='claude')
        T.on_argv(_argv_ev(102, b'claude\x00-p\x00use --force carefully\x00'))
        T.on_fork(Ev(pid=103, ppid=102, uid=1000, ktime_ns=5))
        self.assertEqual(self._ident(103)['args'], 'claude -p %s' % P)

    def test_nothing_is_masked_when_switched_off(self):
        T.REDACT, T.REDACT_KEYS, T.REDACT_PROMPTS = frozenset(), False, False
        T.proc_add(104, 1, comm='claude', args='claude -p secret plan')
        i = self._ident(104)
        self.assertEqual((i['args'], i['args_redacted']), ('claude -p secret plan', None))

    def test_the_cap_cuts_the_masked_line(self):
        T.ARGS_MAXLEN = 20
        T.proc_add(105, 1, comm='tool', args='tool --token ' + 'x' * 40)
        self.assertEqual(self._ident(105)['args'], ('tool --token ' + K)[:20])

    def test_a_new_argv_is_never_written_under_an_old_masking(self):
        T.proc_add(106, 1, comm='claude')
        T.on_argv(_argv_ev(106, b'claude\x00-p\x00one\x00'))
        self._ident(106)
        T.proc[106]['args'] = 'git status'
        self.assertEqual(self._ident(106)['args'], 'git status')

    def test_a_line_the_masking_cannot_read_is_not_written(self):
        saved = T.redact_line

        def broken(*a, **kw):
            raise RuntimeError('boom')
        T.redact_line = broken
        try:
            T.proc_add(107, 1, comm='tool', args='tool --x y')
            i = self._ident(107)
        finally:
            T.redact_line = saved
        self.assertEqual((i['args'], i['args_redacted']), ('[REDACTED]', ['error']))

    def test_the_setting(self):
        self.assertEqual(T._redact_kinds(None), {'keys', 'prompts'})
        self.assertEqual(T._redact_kinds(''), {'keys', 'prompts'})
        self.assertEqual(T._redact_kinds('0'), frozenset())
        self.assertEqual(T._redact_kinds('keys'), {'keys'})
        self.assertEqual(T._redact_kinds('prompt'), {'prompts'})
        self.assertEqual(T._redact_kinds('Credentials, Prompt'), {'keys', 'prompts'})

    def test_a_setting_it_cannot_read_masks_everything(self):
        """Failing open would write out the keys the setting meant to keep in."""
        for v in ('keys,bogus', 'prompts-only', '0,keys'):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(T._redact_kinds(v), {'keys', 'prompts'}, v)
            self.assertIn('cannot read', err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
