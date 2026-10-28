"""lib/redact.py and its use by the tracer: credentials and coding-agent prompts
masked in every command line the collector writes, raw argv kept in memory.

    python3 -m unittest discover collector/tests
"""
import contextlib
import ctypes
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import time
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
        for line in ('curl -H "Authorization: Bearer $TOK" https://api.x',
                     'curl -H "Authorization: Bearer $(cat tok.txt)" https://api.x',
                     'test -n "${OPENROUTER_API_KEY:-}"',
                     "python3 -c 'HfApi(token=os.environ[\"HF_TOKEN\"])'",
                     "python3 -c 'HfApi(token=False)'"):
            self.assertEqual(R.redact_keys(line), (line, []), line)

    def test_what_only_looks_like_a_user_and_password_is_left(self):
        """Seen on the fleet: date's format and a container's uid:gid."""
        for line in ("date -u +%Y-%m-%dT%H:%M:%SZ", "date -u +%H:%M",
                     "podman run --user 1000:1000 img", "docker exec -u 0:0 c sh"):
            self.assertEqual(R.redact_keys(line), (line, []), line)
        self.assertEqual(R.redact_keys('curl -u api:wandb_v1_abcdef https://x')[0],
                         'curl -u api:%s https://x' % K)

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

    def test_an_options_value_is_kept_whatever_it_reads_like(self):
        """A session's name or a resume search term is not a prompt."""
        argv = ['claude', '-r', 'carry on with the plan']
        self.assertEqual(R.redact_argv(argv), (' '.join(argv), None))
        self.check(['claude', '-r', 'auth bug', 'fix it now'], 'claude -r auth bug %s' % P)

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
                ('cd /proj; claude -p "secret plan"', 'cd /proj; claude -p %s' % P),
                ('cd /proj;claude -p "secret plan"', 'cd /proj;claude -p %s' % P),
                ('cat x|claude -p "sum it up"', 'cat x|claude -p %s' % P),
                ('echo $(claude -p "sum it up")', 'echo $(claude -p %s)' % P),
                ('echo `claude -p "sum it up"`', 'echo `claude -p %s`' % P)):
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
                         'claude -p %s' % P)
        self.assertEqual(R.redact_line('claude -p fix the -v flag handling')[0],
                         'claude -p %s -v %s' % (P, P))

    def test_a_newline_ends_a_command_in_a_script(self):
        self.check(['bash', '-c', "cd /repo\nclaude -p 'fix it now'\necho done"],
                   'bash -c cd /repo\nclaude -p %s\necho done' % P)

    def test_a_joined_line_collapses_the_prompts_words(self):
        self.assertEqual(R.redact_line('claude -p fix the failing test --output-format json')[0],
                         'claude -p %s --output-format json' % P)

    def test_a_joined_line_keeps_nothing_that_might_be_a_prompts_word(self):
        self.assertEqual(R.redact_line('codex exec read reports/c750.md and summarize')[0],
                         'codex exec %s' % P)
        self.assertEqual(R.redact_line('claude --remote-control Usage of the API')[0],
                         'claude --remote-control Usage %s' % P)

    def test_a_joined_shell_line_takes_the_rest_as_its_command(self):
        self.assertEqual(R.redact_line('bash -c claude -p hi there')[0],
                         'bash -c claude -p %s' % P)

    def test_boundaries_keep_a_flag_inside_a_prompt_masked(self):
        """Joined, `--force` inside the prompt reads as a flag and survives."""
        self.assertEqual(red(['claude', '-p', 'use --force carefully']), 'claude -p %s' % P)


class NotAPromptTest(unittest.TestCase):
    """Arguments that were masked although no prompt was in them, each shape
    taken from a day of FASRC login-node command lines (2026-10-26)."""

    def same(self, argv):
        self.assertEqual(R.redact_argv(argv), (' '.join(argv), None), argv)

    def same_joined(self, line):
        self.assertEqual(R.redact_line(line), (line, None), line)

    def test_session_names(self):
        self.same(['claude', '--resume', '0c00efea-1827-48af-aef6-065adb167f58',
                   '--name', 'Mantis shrimp', '--remote-control', 'Mantis shrimp',
                   '--permission-mode', 'auto'])
        self.same(['claude', '--name', 'RC - Career', '--remote-control', 'RC - Career'])
        self.same(['claude', '--resume', '0c00efea-1827-48af-aef6-065adb167f58',
                   '--remote-control', 'fasrc-NLO'])
        self.same(['bash', '-lc', "claude --name 'paper co-reader' --remote-control "
                                  "'paper co-reader' --permission-mode auto; exec bash"])
        self.assertEqual(red(['claude', '--remote-control', 'fasrc-gammaZ', 'You are gammaZ.']),
                         'claude --remote-control fasrc-gammaZ %s' % P)

    def test_claude_codes_own_daemons(self):
        self.same(['claude', 'bg-spare', '--bg-spare', '/tmp/cc-daemon-1/8fb1/spare/35a.claim.sock'])
        self.same(['claude', 'bg-pty-host', '--bg-pty-host', '/tmp/cc-daemon-1/8fb1/pty/f5d.sock',
                   '162', '64', '--', '/u/.local/share/claude/versions/2.1.301', '--session-id',
                   'f5d34d0a-0a19-4db6-9dbe-8d7ec662d701', '--fork-session', '--resume',
                   '/u/.claude/projects/-p/db2eab0c.jsonl', '--model', 'opus'])
        self.same(['/u/.local/bin/claude', 'daemon', 'run', '--json-path', '/u/.claude/daemon.json',
                   '--origin', 'transient', '--spawned-by', '{"label":"claude","pid":4040227}'])
        self.same(['claude', 'remote-control', '--name', 'Remote Dev (FASRC)', '--capacity', '32'])
        for argv in (['claude', 'rc'], ['claude', 'attach', 'e3b05be0'],
                     ['/u/.local/share/claude/versions/2.1.310', '--session-id',
                      '84647189-d9bd-4429-8f44-59fef0f97ca9', '--agent', 'claude',
                      '--inherit-permission-mode', 'auto'],
                     ['/u/.local/share/claude/versions/2.1.282', '--print', '--sdk-url',
                      'https://api.anthropic.com/v1/code/sessions/cse_01Xpy', '--resume=https://'
                      'api.anthropic.com/v1/code/sessions/cse_01Xpy', '--input-format',
                      'stream-json', '--max-thinking-tokens', '31999']):
            self.same(argv)

    def test_codex_servers_and_sessions(self):
        self.same(['codex', '-c', 'features.code_mode_host=true', 'app-server', '--listen',
                   'unix://'])
        self.same(['codex', 'app-server', 'daemon', 'pid-update-loop'])
        self.same(['codex', '--no-daemon', 'resume', '01a032f5-fa59-73b3-ad52-dbc2c6199cea',
                   '--yolo', '-C', '/u'])
        self.same(['codex', 'fork'])
        self.same(['codex', 'cloud', 'list'])
        self.check_masked(['codex', 'exec', '--json', 'resume',
                           '01a17649-ec8c-7c90-a4e6-0b1bbfd6e447', 'Continue the task in TASK.md'],
                          'codex exec --json resume 01a17649-ec8c-7c90-a4e6-0b1bbfd6e447 %s' % P)
        self.check_masked(['codex', 'cloud', 'exec', '--env', 'e1', 'fix the flaky test'],
                          'codex cloud exec --env e1 %s' % P)
        self.check_masked(['codex', 'queue', '--thread', 't1', '--message', 'and add tests'],
                          'codex queue --thread t1 --message %s' % P)
        self.check_masked(['codex', '-c', 'developer_instructions="be terse and careful"',
                           '-m', 'o3'], 'codex -c %s -m o3' % P)

    def test_cursor_agents_worker(self):
        argv = ['/u/.local/bin/cursor-agent', '--use-system-ca',
                '/u/.local/share/cursor-agent/versions/2026.10.19/index.js', '--api-key',
                'crsr_abc', '--endpoint', 'https://api2.cursor.sh', 'worker', 'start',
                '--worker-dir', '/lab/u', '--name', '/lab/u @ login1', '--label',
                'cursor.private_worker.workspace_key=cursor-agent-worker-bf2178222d']
        self.assertEqual(red(argv), ' '.join(argv).replace('crsr_abc', K))

    def test_paths_and_ids_and_slash_commands(self):
        self.same(['claude', '/resume'])
        self.same(['claude', '--agents', '/lab/run/agents.json', '--resume',
                   '41c5898e-06c4-498b-9193-6a8aef2b1f21'])
        self.check_masked(['claude', '--agents', '{"r": {"prompt": "review it"}}'],
                          'claude --agents %s' % P)

    def test_a_one_word_prompt_is_still_one(self):
        self.check_masked(['claude', 'continue'], 'claude %s' % P)
        self.check_masked(['codex', 'exec', 'hello'], 'codex exec %s' % P)

    def test_a_prompt_given_as_several_arguments_keeps_no_path_of_its_own(self):
        self.check_masked(['claude', 'fix', 'the', 'bug', 'in', 'src/auth.py'], 'claude %s' % P)
        self.check_masked(['codex', 'resume', '0199a5c2-5b1e-7f20-9c3a-1b2c3d4e5f60', 'go', 'on'],
                          'codex resume 0199a5c2-5b1e-7f20-9c3a-1b2c3d4e5f60 %s' % P)

    def test_a_command_string_keeps_its_own_text(self):
        """Only the prompt changes: quotes, spacing and the rest stay as written,
        but for a comment beside the prompt, which goes with it."""
        script = 'cd /p && claude -p "fix it" | tee "log 1.txt"  # note'
        self.assertEqual(red(['bash', '-c', script]),
                         'bash -c cd /p && claude -p %s | tee "log 1.txt"  # %s' % (P, P))
        self.assertEqual(red(['bash', '-c', "tmux new -d 'claude -p \"fix it\"'"]),
                         "bash -c tmux new -d 'claude -p %s'" % P)

    def test_joined_lines(self):
        """The node tier's `w` has only a joined line; so has any line whose
        argument boundaries could not be read."""
        for line in ('codex app-server --listen unix:// --analytics-default-enabled',
                     'codex app-server proxy', 'claude /resume', 'claude rc',
                     'claude auth status --json', 'claude mcp add srv -- npx some-server',
                     'claude --output-format stream-json --verbose --max-thinking-tokens 31999',
                     'claude --brand-new-number 31999 --brand-new-path /tmp/x.sock',
                     'claude daemon run --origin transient --spawned-by {"pid":1}',
                     'claude --resume 0c00efea-1827-48af-aef6-065adb167f58 --remote-control '
                     'fasrc-NLO'):
            self.same_joined(line)
        self.assertEqual(R.redact_line('claude --resume 0c00efea-1827-48af-aef6-065adb167f58 '
                                       'ORCH auto-resume: continue TASK.md now')[0],
                         'claude --resume 0c00efea-1827-48af-aef6-065adb167f58 %s' % P)
        self.assertEqual(R.redact_line('codex exec --json resume 01a17649-ec8c-7c90-a4e6-'
                                       '0b1bbfd6e447 Continue the task in TASK.md')[0],
                         'codex exec --json resume 01a17649-ec8c-7c90-a4e6-0b1bbfd6e447 %s' % P)

    def check_masked(self, argv, want):
        self.assertEqual(red(argv), want)


class LeakReviewTest(unittest.TestCase):
    """What an independent review of the narrowing above found getting out: each
    a prompt or a credential it let through. Made-up values; the shapes are the
    review's, or a day of FASRC login-node command lines'."""

    KEY = 'abcd1234efgh5678wxyz'

    def same(self, argv):
        self.assertEqual(R.redact_argv(argv), (' '.join(argv), None), argv)

    def same_joined(self, line):
        self.assertEqual(R.redact_line(line), (line, None), line)

    def hidden(self, out, *secrets):
        for s in secrets:
            self.assertNotIn(s, out)

    # --- prompts ---------------------------------------------------------------
    def test_a_prompt_with_no_spaces_in_it_is_still_one(self):
        """Chinese and Japanese are written without spaces, and a digit or a dot
        in them made them read as an id or a file's name."""
        for argv in (['claude', '-p', '修复第3个测试'], ['codex', 'exec', '把train.py改成用PyTorch2.0'],
                     ['claude', '-p', '请阅读README.md并总结'],
                     ['ollama', 'run', 'qwen2.5', '解释Python3.12的新特性'],
                     ['codex', 'review', '--base', 'main', '重点检查v2接口'],
                     ['claude', '--allowedTools', 'Edit', 'Bash', '修复登录错误'],
                     ['codex', 'exec', '-i', 'a.png', '修复登录错误'],
                     ['claude', '--brand-new-flag', '修复登录错误']):
            out = red(argv)
            self.assertIn(P, out, argv)
            self.assertNotRegex(out, '[^\x00-\x7f]', argv)
        self.assertEqual(red(['claude', '-p', 'fix_the_auth_bug']), 'claude -p %s' % P)
        self.assertEqual(red(['claude', '-p', 'v2']), 'claude -p %s' % P)
        for argv in (['claude', '/resume'], ['claude', '-p', '-'], ['claude', '--add-dir', '/数据/x'],
                     ['codex', 'resume', '0199a5c2-5b1e-7f20-9c3a-1b2c3d4e5f60']):
            self.same(argv)

    def test_a_sentence_given_to_an_option_that_takes_a_name_is_a_prompt(self):
        """codex's -p is --profile and its -m --model: a sentence there is a
        prompt typed from habit. Only a session's name is kept whatever it reads
        like; a JSON config is not a sentence."""
        for argv in (['codex', '-p', 'fix the failing test'],
                     ['codex', 'exec', '-m', 'fix the failing test'],
                     ['codex', '--profile=fix the failing test'], ['gemini', '-m', 'fix the failing test'],
                     ['aider', '--model', 'fix the failing test'],
                     ['claude', '--agent', 'You are a strict code reviewer'],
                     ['claude', '--debug', 'fix the failing test'], ['claude', '-w', 'fix the failing test'],
                     ['claude', '--from-pr', 'fix the failing test']):
            self.hidden(red(argv), 'failing test', 'code reviewer')
        self.same(['claude', '--name', 'Fix the failing test', '-r', 'carry on with the plan'])
        self.same(['claude', '--settings', '{"model": "opus", "env": {"A": "b c"}}'])

    def test_claude_codes_hidden_prompt_options(self):
        for argv in (['claude', '--deep-link-origin', '--prefill-b64', 'UmV3cml0ZSB0aGUgYmlsbGluZw=='],
                     ['claude', '--prefill', 'refactor_billing'],
                     ['claude', '--plan-mode-instructions', 'plan the migration'],
                     ['claude', '--append-subagent-system-prompt', 'be terse'],
                     ['claude', '--remote', 'fix the flaky test']):
            self.assertEqual(red(argv), ' '.join(argv[:-1] + [P]), argv)
            self.hidden(R.redact_line(' '.join(argv))[0], argv[-1])

    def test_a_subcommands_name_can_begin_a_prompt(self):
        """A sentence after a subcommand that takes none is a prompt. Split on
        spaces, so is a word the subcommand does not take, or any word once
        print mode has asked for a prompt."""
        self.assertEqual(red(['claude', 'setup', 'the', 'conda', 'env', 'for', 'this', 'repo']),
                         'claude %s' % P)
        self.assertEqual(red(['claude', 'ultrareview', 'focus on the auth changes']),
                         'claude ultrareview %s' % P)
        for line in ('claude -p update deps', 'claude -p rm stale checkpoints',
                     'claude -p install pytorch nightly', 'claude -p setup CI pipeline',
                     'claude -p install torch==2.1 and cuda-12.1 on gpu-node-3',
                     'claude install pytorch nightly', 'codex a quick fix'):
            self.hidden(R.redact_line(line)[0], 'deps', 'stale', 'pytorch', 'pipeline', 'torch',
                        'quick')
        for line in ('claude install stable', 'claude install 2.1.309', 'claude attach Mantis',
                     'claude daemon run --json-path /u/.claude/daemon.json', 'codex cloud list',
                     'codex completion zsh', 'codex apply task_e_0123', 'codex login status'):
            self.same_joined(line)

    def test_claude_codes_pty_host_runs_a_command_of_its_own(self):
        """It starts as one argument, `claude bg-pty-host`, and the claude after
        `--` carries whatever prompt options its session was given."""
        argv = ['claude bg-pty-host', '--bg-pty-host', '/tmp/cc/pty/f5d.sock', '162', '64', '--',
                '/u/.local/share/claude/versions/2.1.309', '--session-id',
                'f5d34d0a-0a19-4db6-9dbe-8d7ec662d701', '--append-system-prompt',
                'You review grant drafts for the PI', '--name', 'Grant review']
        self.assertEqual(R.redact_argv(argv),
                         (' '.join(argv[:10]) + ' %s --name Grant review' % P, ['prompt']))
        self.hidden(R.redact_line(' '.join(argv))[0], 'grant drafts')
        self.assertEqual(red(['claude', '--bg-spare', '/tmp/x.sock', 'fix the bug in auth']),
                         'claude --bg-spare /tmp/x.sock %s' % P)
        # and a program named with spaces by `exec -a`
        self.assertEqual(red(['claude -p fix the bug in the auth module', '30']), 'claude -p %s' % P)

    def test_a_joined_prompt_keeps_no_word_after_an_option_inside_it(self):
        self.assertEqual(R.redact_line('claude -p run pytest -k test_payroll_secret and fix')[0],
                         'claude -p %s -k %s' % (P, P))
        self.assertEqual(R.redact_line('claude --add-dir /repo src/payroll.py:42 leaks salaries')[0],
                         'claude --add-dir /repo %s' % P)
        self.same_joined('claude --add-dir /repo /data --model opus')

    def test_quoting_the_lexer_now_reads(self):
        """`$'...'` (how printf %q writes a prompt with a newline in it) and quotes
        inside a quoted $(...) put the lexer out of step: a `#` read as a comment
        after that, and the rest of the line went out as it was written."""
        for script in ("cd /x && claude -p $'it\\'s #1: fix the plan' && claude -p \"rotate the keys\"",
                       'claude -p "count $(echo "a #b") then fix the plan"',
                       "claude -p $'it\\'s #1: fix the plan\\nthen ship it'"):
            self.hidden(red(['bash', '-c', script]), 'fix the plan', 'rotate', 'ship it')
        # and a name after an escape there, `\nclaude`, or quoted in pieces, is
        # still a name
        self.hidden(red(['bash', '-c', "bash -c $'cd /x\\nclaude -p \\'fix the plan\\''"]),
                    'fix the plan')
        self.hidden(red(['bash', '-c', "cd /x && clau'd'e -p 'fix the plan'"]), 'fix the plan')
        self.assertEqual(R.shell_split("a $'b\\'c d' \"e $(f \"g h\") i\" $\"j k\""),
                         [('a', False), ("b'c d", False), ('e $(f "g h") i', False), ('j k', False)])

    def test_a_string_read_right_has_no_way_around_the_grammar(self):
        """A string the lexer used to give up on was masked from its first agent
        to the end; read right, it reaches the shell's own grammar: the reserved
        words a command follows, a command inside a quoted $(...), and strings
        nested deeper than are read."""
        for script in ("for f in *.py; do claude -p $'it\\'s the secret plan'; done",
                       'if true; then codex exec "the secret plan"; fi',
                       '{ gemini -p "the secret plan"; }', 'out="$(claude -p "the secret plan")"'):
            self.hidden(red(['bash', '-c', script]), 'secret plan')
        deep = 'claude -p "the secret plan"'
        for wrap in ('tmux new -d %s', 'bash -lc %s', 'ssh host %s', 'bash -c %s') * 2:
            deep = wrap % shlex.quote(deep)
        self.hidden(red(['bash', '-c', deep]), 'secret plan')

    def test_what_reading_more_must_not_mask(self):
        """Seen on the fleet: Claude Code's tool calls nest four strings deep
        (`bash -c`, `eval`, `tmux new`, `bash -lc`); `2>&1`'s 2 is a file
        descriptor; a thread's name follows `codex exec resume`; `%q` is no
        agent, nor `ollama-0.34.4-pull`; and split on spaces, a session's name
        keeps its words."""
        tool = ("source /u/.claude/shell-snapshots/snapshot-bash-1.sh && eval 'N='\"'\"'RC - DNA'\"'\"'; "
                "tmux new-session -d -s x \"bash -lc \\\"claude --name '\"'\"'$N'\"'\"' "
                "--permission-mode auto; exec bash\\\"\"' < /dev/null")
        self.same(['bash', '-c', tool])
        self.assertEqual(red(['bash', '-c', 'claude -p "hi there" --model haiku 2>&1 | tail -3']),
                         'bash -c claude -p %s --model haiku 2>&1 | tail -3' % P)
        self.assertEqual(red(['codex', 'exec', 'resume', 'thread-1', 'go on with it']),
                         'codex exec resume thread-1 %s' % P)
        self.same(['bash', '-c', "echo x  # The %q literal needs no fork"])
        self.same(['bash', '-c', "ls -d ollama-0.34.4-pull 2>&1; claude --version"])
        self.same_joined('claude --name paper co-reader --remote-control paper co-reader')

    def test_a_heredocs_body_is_data(self):
        """Its quotes are no shell's (`Don't forget` put the lexer out of step
        for what followed), and fed to an agent it is the prompt."""
        script = ("cat > n.md <<'EOF'\nDon't forget\nEOF\n"
                  "for f in *.md; do codex exec \"review $f for the secret plan\"; done")
        self.assertEqual(red(['bash', '-c', script]),
                         'bash -c ' + script.replace('"review $f for the secret plan"', P))
        script = "git commit -m \"$(cat <<'EOF'\nDon't break it\nEOF\n)\" && claude -p 'the secret plan'"
        self.assertEqual(red(['bash', '-c', script]),
                         'bash -c ' + script.replace("'the secret plan'", P))
        self.assertEqual(red(['bash', '-c', "claude -p <<'EOF'\nthe secret plan\nEOF\necho done"]),
                         "bash -c claude -p <<'EOF'\n%s\nEOF\necho done" % P)
        self.same(['bash', '-c', "cat <<-EOF > x\n\tit's codex exec's\n\tEOF\necho done"])

    def test_a_comment_beside_a_prompt_goes_with_it(self):
        self.assertEqual(red(['bash', '-c', 'claude -p "fix it"  # then the secret plan']),
                         'bash -c claude -p %s  # %s' % (P, P))
        self.assertEqual(red(['bash', '-c', 'make test  # then: cd x && claude -p "the secret plan"']),
                         'bash -c make test  # %s' % P)
        # a comment that only names one, as `echo` would, is not an agent run
        self.same(['bash', '-c', 'make test  # then deploy with claude -p'])

    def test_agent_launchers_seen_on_the_fleet(self):
        """Claude Code's native binary as the npm package names it and under a
        version; codex's native shim; the Codex plugin for Claude Code; and a
        wrapper script, which reaches the kernel as `bash SCRIPT ...`."""
        for argv in (['/u/node_modules/@anthropic-ai/claude-code/bin/claude.exe', '-p',
                      'review the auth module'],
                     ['/opt/bin/claude.exe.2.1.281', '-p', '--model', 'opus', '--system-prompt',
                      'You grade essays'],
                     ['/opt/bin/claude-2.1.295', '-p', '--system-prompt', 'You grade essays'],
                     ['node', '/u/.npm-global/bin/codex.node-native', 'exec', 'review the auth module'],
                     ['node', '/u/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/'
                      'codex-companion.mjs', 'task', '--model', 'gpt-5', '--effort', 'high',
                      'review the auth module'],
                     ['bash', '/home/u/.local/bin/codex', 'exec', '--sandbox', 'read-only',
                      'review the auth module'],
                     ['/usr/bin/env', 'bash', '/home/u/.local/bin/claude', '-p', 'review the auth module']):
            self.hidden(red(argv), 'auth module', 'grade essays')
            self.hidden(R.redact_line(' '.join(argv))[0], 'auth module', 'grade essays')
        for argv in (['/u/node_modules/@anthropic-ai/claude-code/bin/claude.exe', 'daemon', 'run'],
                     ['claude.exe'], ['bash', '/home/u/bin/claude_session.sh', 'fix'],
                     ['bash', '-c', 'pwd -P >| /tmp/claude-7cb5-cwd']):
            self.same(argv)

    # --- credentials -------------------------------------------------------------
    def test_a_user_and_password_however_written(self):
        k = self.KEY
        for line in ('curl -u $USER:%s https://x' % k, 'curl -u ${USER}:%s https://x' % k,
                     'curl --user=alice:%s https://x' % k, 'curl --proxy-user=alice:%s https://x' % k,
                     "curl -u 'alice:%s' https://x" % k, 'curl -u 12345:67890 https://x'):
            self.hidden(R.redact_keys(line)[0], k, '67890')
        for line in ("date -u '+%H:%M'", "python -c 'y = x -u[:, 0]'", 'docker run -u 1000:1000 img',
                     'nvidia-docker run --user=0:0 img', 'podman-hpc run --user 0:0 img',
                     'podman run \\\n  --cap-drop ALL --user 1000:1000 img',
                     'python /x/tests/fake_podman.py run --user 1000:1000 img',
                     'curl -s -u "api:$K" https://x'):
            self.assertEqual(R.redact_keys(line), (line, []), line)

    def test_a_key_written_as_a_shell_default(self):
        k = self.KEY
        for line in ('export OPENAI_API_KEY=${OPENAI_API_KEY:-%s}' % k,
                     'curl -H x-api-key:-%s https://x' % k, ': ${ANTHROPIC_API_KEY:=%s}' % k):
            self.hidden(R.redact_keys(line)[0], k)
        for line in ('echo ${OPENAI_API_KEY:-$FALLBACK_KEY}',
                     ': ${OPENAI_API_KEY:?set OPENAI_API_KEY first}'):
            self.assertEqual(R.redact_keys(line), (line, []), line)

    def test_a_credential_quoted_inside_a_command_string(self):
        """Edited in place, a string keeps quotes the rules cannot read through:
        where they find a credential in the string rebuilt from its words, that
        is written instead."""
        k = self.KEY
        for script in ("claude -p 'fix it'; curl -u 'alice:%s' https://x" % k,
                       "claude -p 'fix it'; curl -H 'X-Api-Key: '%s https://x" % k,
                       'claude -p "fix it"; export HF_TOKEN=\\"%s\\"' % k,
                       "claude -p 'fix it' && eval 'export HF_TOKEN='\"'\"'%s'\"'\"''" % k,
                       'claude -p "fix it"; curl -H Authorization:\\ Bearer\\ %s https://x' % k):
            out = red(['bash', '-c', script])
            self.assertNotIn(k, out, script)
            self.assertIn(P, out, script)
        out = red(['bash', '-c', 'out="$(claude -p "the secret plan")"; curl -H \'X-Api-Key: \'' + k])
        self.hidden(out, k, 'secret plan')

    def test_credential_headers_as_json_keys(self):
        k = self.KEY
        cfg = ('{"type":"http","url":"https://h/mcp","headers":{"Authorization":"Bearer %s",'
               '"X-API-Key":"%s"}}' % (k, k))
        self.hidden(red(['claude', 'mcp', 'add-json', 'srv', cfg]), k)
        self.hidden(R.redact_line('claude mcp add-json srv ' + cfg)[0], k)
        self.hidden(red(['codex', '-c', 'model_providers.x.http_headers={"X-Api-Key"="%s"}' % k,
                         'exec', 'go']), k)
        self.hidden(red(['claude', '--mcp-config', '{"s":{"args":["-y","pkg","--api-key","%s"]}}' % k]),
                    k)
        self.hidden(red(['bash', '-c', 'claude mcp add-json srv "{\\"headers\\":{\\"Authorization\\":'
                                       '\\"Bearer %s\\"}}"' % k]), k)

    def test_a_value_that_only_starts_like_code_is_a_secret(self):
        for line in ('env DB_PASSWORD=None-of-your-business claude', 'PASSWORD=true!x run',
                     'PASSWORD=%s3cr3t run', 'TOKEN=OS.abc123 run', 'SECRET={}abc run'):
            self.assertIn('=' + K, R.redact_keys(line)[0], line)
        for line in ("python3 -c 'HfApi(token=None)'", "python3 -c 'f(password=%s, x=1)'",
                     "python3 -c 'f(token=os.getenv(\"T\"))'"):
            self.assertEqual(R.redact_keys(line), (line, []), line)

    def test_an_eos_token(self):
        tok = 'zteos64:' + 'QUJD' * 20
        for line in ("ssh host xrdcp 'root://eos.example.ch//eos/user/x/f.root?authz=%s' ." % tok,
                     'export EOSAUTHZ=%s' % tok):
            self.hidden(R.redact_keys(line)[0], 'QUJDQUJD')


class NodeTierTest(unittest.TestCase):
    """The node tier has ps's joined lines; it masks each from the process's own
    arguments while /proc still has them."""

    def setUp(self):
        self.argv = ['claude', '-c', 'x = 1; import time; time.sleep(30)', '--name',
                     'Mantis shrimp']
        self.p = subprocess.Popen(self.argv, executable=sys.executable)
        self.line = ' '.join(self.argv)
        for _ in range(200):                    # until the child has exec'd
            with open('/proc/%d/cmdline' % self.p.pid, 'rb') as f:
                if f.read():
                    break
            time.sleep(0.01)

    def tearDown(self):
        self.p.kill()
        self.p.wait()

    def test_a_ps_line_is_masked_from_the_processs_own_arguments(self):
        self.assertEqual(R.redact_ps_line(self.line, self.p.pid),
                         ('claude -c %s --name Mantis shrimp' % P, ['prompt']))
        # joined, the session's name loses its second word
        self.assertEqual(R.redact_line(self.line)[0], 'claude -c %s --name Mantis %s' % (P, P))

    def test_a_line_the_pid_no_longer_runs_is_masked_as_joined(self):
        line = 'claude --name Mantis shrimp'
        self.assertEqual(R.redact_ps_line(line, self.p.pid), R.redact_line(line))
        self.assertEqual(R.redact_ps_line(self.line, 2 ** 22 + 7), R.redact_line(self.line))


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
