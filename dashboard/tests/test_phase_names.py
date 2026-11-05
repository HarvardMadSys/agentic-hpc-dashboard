"""What a tool call worked on, and what the agent said about it (reducers/phase_names.py).

Pure text in, names and words out. These tests pin the rules the module states: the files a program
gives a role (run, test, write, read), the folders, Slurm jobs, PRs, branches, worktrees, packages,
scratch names, `cd` targets, search terms, and recurring words a command names; the agent's own
words (a commit subject, a PR title, a log entry) and which of them a call holds; that nothing is
read from a command that has none; and that what is kept is masked, cut by characters, and bounded.

The commands are real shapes from the examples file (`2026-11-02-trajectory-levels-examples.md`)
and from the two captures the module's figures come from.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_names.py' -v
"""
import random
import shlex
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.phase_names import (KINDS, MAX_NAMES, MAX_WORDS,  # noqa: E402
                                               NAME_CHARS, SOURCES, WORDS_CHARS, _effective,
                                               names_of, own_words, tagset)
from rc_dashboard.reducers.phase_rules import _MAX_TEXT, _commands  # noqa: E402

TOKEN = "ghp_" + "a" * 36
# Options of sbatch that take a value, from its manual, apart from the job name (`-J`).
SBATCH_VALUED = (
    "-A --account --acctg-freq -a --array --batch --bb --bbf -b --begin -D --chdir "
    "--cluster-constraint -M --clusters --comment -C --constraint --container --container-id "
    "-S --core-spec --cores-per-socket --cpu-freq --cpus-per-gpu -c --cpus-per-task --deadline "
    "--delay-boot -d --dependency -m --distribution -e --error -x --exclude --export "
    "--export-file -B --extra-node-info --extra --gid --gpu-bind --gpu-freq -G --gpus "
    "--gpus-per-node --gpus-per-socket --gpus-per-task --gres --gres-flags --hint -i --input "
    "--kill-on-invalid-dep -L --licenses --mail-type --mail-user --mcs-label --mem --mem-bind "
    "--mem-per-cpu --mem-per-gpu --mincpus --network -F --nodefile -w --nodelist -N --nodes "
    "-n --ntasks --ntasks-per-core --ntasks-per-gpu --ntasks-per-node --ntasks-per-socket "
    "--open-mode -o --output -p --partition --power --priority --profile -q --qos "
    "--reservation --signal --sockets-per-node --switches --thread-spec --threads-per-core "
    "-t --time --time-min --tmp --tres-per-task --uid --wait-all-nodes --wckey").split()
# And srun's own, from its manual.
SRUN_VALUED = (
    "--accel-bind --bcast-exclude --cpu-bind --epilog --het-group --jobid --launcher-opts --mpi "
    "--msg-timeout --pack-group --prolog -r --relative --slurmd-debug --task-epilog "
    "--task-prolog -T --threads --tres-bind -W --wait").split()


def of(kind, text):
    """The names of one kind that a command has, in order."""
    return [name for k, name in names_of(text) if k == kind]


class NamesTest(unittest.TestCase):
    def test_files_by_the_role_the_program_gives_them(self):
        self.assertIn(("test", "tests/test_channel_symmetry.py"), names_of(
            "source slurm/env.sh && $PY -m pytest tests/test_channel_symmetry.py -q 2>&1 "
            "| tail -8"))
        self.assertIn(("write", "channels/symmetry.py"), names_of(
            "cat >> atomysm/channels/symmetry.py <<'PYEOF'\ndef f(): pass\nPYEOF"))
        self.assertIn(("read", "scripts/eval_e2e.py"),
                      names_of("sed -n '124,156p' scripts/eval_e2e.py"))
        self.assertIn(("run", "05_proofs.py"), names_of("python 05_proofs.py 2>&1 | tail -40"))
        # a Codex bare call's text is its whole argv
        self.assertIn(("read", "ir/test_model.py"), names_of(
            "/usr/bin/sed -n 1820,1910p src/staminabench/schema_gen/ir/test_model.py"))

    def test_folders_jobs_prs_branches_and_worktrees(self):
        self.assertIn(("dir", "novel_events/controls"),
                      names_of("mkdir -p analysis/novel_events/controls"))
        self.assertIn(("job", "atm-e2e-sym"), names_of(
            "sbatch --job-name=atm-e2e-sym slurm/gpu.sbatch scripts/eval_e2e.py"))
        self.assertIn(("pr", "#235"), names_of("gh pr view 235 --json files"))
        self.assertIn(("branch", "feat/seek-frame-decoding"),
                      names_of("git push origin feat/seek-frame-decoding"))
        self.assertIn(("worktree", "seek-frame-decoding"), names_of(
            "cd /n/home03/a/KempnerForge/.claude/worktrees/seek-frame-decoding && pytest -q"))

    def test_packages_scratch_topics_folders_and_search_terms(self):
        self.assertIn(("pkg", "gridss"),
                      names_of("mamba create -y -p ~/.conda/envs/gridss gridss=2.13.2"))
        n = names_of("cd analysis && ANALYSIS_END=2026-10-31 ./update.sh "
                     "> $SCRATCH/update_claude_exe.log")
        self.assertIn(("run", "update.sh"), n)
        self.assertIn(("scratch", "update_claude_exe"), n)
        self.assertIn(("cd", "analysis"), n)
        self.assertIn(("grep", "federat"),
                      names_of('grep -rn -i "federat" agent-guards --include=*'))
        self.assertIn(("word", "marinQC51"),
                      names_of("bash refbias_run.sh marinQC51 --throttle 15"))

    def test_at_most_six_names_and_none_from_nothing(self):
        self.assertEqual(len(names_of(" ".join("cat f%d.py;" % i for i in range(20)))), MAX_NAMES)
        self.assertEqual(tagset(None), ((), None))
        self.assertEqual(tagset(""), ((), None))


class FilesTest(unittest.TestCase):
    def test_the_script_an_interpreter_was_given(self):
        self.assertEqual(of("run", "python3 -W ignore -u src/train.py --seed 3"), ["src/train.py"])
        self.assertEqual(of("run", "bash -x scripts/build.sh arg"), ["scripts/build.sh"])
        self.assertEqual(of("run", "sh -o pipefail ./go.sh"), ["go.sh"])
        self.assertEqual(of("run", "sh +o pipefail ./go.sh"), ["go.sh"])
        self.assertEqual(of("run", "bash -m build.sh"), ["build.sh"])         # monitor mode, not -m
        self.assertEqual(of("run", "Rscript --vanilla fit.R 3"), ["fit.R"])
        self.assertEqual(of("run", "julia --project=. run.jl"), ["run.jl"])
        self.assertEqual(of("run", "node app.js"), ["app.js"])
        self.assertEqual(of("run", "node -r ts-node/register --import x.mjs app.ts"), ["app.ts"])
        self.assertEqual(of("run", "julia -p 4 -t 2 --load init.jl run.jl"), ["run.jl"])
        self.assertEqual(of("run", "python --check-hash-based-pycs never run.py"), ["run.py"])

    def test_each_interpreter_has_its_own_options_that_take_a_value(self):
        # julia's `-p` and `-t` take a value, node's `-r` does, and bash's do not: its `-p` is
        # privileged mode, `-r` restricted, and `-t` one command
        for text in ("bash -p run.sh", "bash -r run.sh", "bash -t run.sh", "sh -p run.sh",
                     "bash -pr run.sh", "bash -x -e -u run.sh", "bash -O extglob run.sh",
                     "bash --rcfile my.rc run.sh", "bash -o pipefail -p run.sh"):
            self.assertEqual(of("run", text), ["run.sh"], text)
        self.assertEqual(of("run", "python -u -X dev -W ignore -B run.py"), ["run.py"])
        self.assertEqual(of("run", "Rscript --vanilla --verbose fit.R"), ["fit.R"])
        # a value is not the script, and the word after it is
        for text in ("julia -t 4 run.jl", "julia --threads 4 run.jl", "julia -L init.jl run.jl",
                     "julia -J sys.so run.jl", "julia -C native run.jl", "julia -H /h run.jl"):
            self.assertEqual(of("run", text), ["run.jl"], text)
        for text in ("node -r mod app.js", "node --require mod app.js", "node --loader l app.js",
                     "node -C browser app.js"):
            self.assertEqual(of("run", text), ["app.js"], text)
        self.assertEqual(of("run", "/usr/bin/python3.12 tool.py"), ["tool.py"])
        self.assertEqual(of("run", "$PY scripts/x.py"), ["scripts/x.py"])   # a variable's program
        self.assertEqual(of("run", "nohup python train.py > out.txt 2>&1 &"), ["train.py"])

    def test_code_a_module_and_stdin_are_no_script(self):
        for text in ("python -c 'import x'", "python -m http.server 8000",
                     "python - <<'EOF'\nx = 1\nEOF", "bash -c 'echo hi'", 'bash -lc "ls"',
                     "node -e 'x.js'", "Rscript -e 'x.R'", "python tools/run", "python", "bash",
                     "python - arg.py", "bash -c x.sh", "sh -lc 'y.sh'", "bash -c 'build.py' z.sh",
                     "julia -e 'a.jl'", "node -p 'a.js'"):
            self.assertEqual(of("run", text), [], text)

    def test_what_follows_a_script_is_the_scripts_own(self):
        self.assertEqual(of("run", "python run.py -m foo"), ["run.py"])
        self.assertEqual(of("test", "python run.py -m pytest tests/test_a.py"), [])
        self.assertEqual(of("pkg", "python run.py -m pip install numpy"), [])
        self.assertEqual(of("write", "python run.py -c 'open(\"x.py\", \"w\")'"), [])
        self.assertEqual(of("run", "bash a.sh -c b.sh"), ["a.sh"])
        self.assertEqual(of("word", "python run.py -m mod_1"), ["mod_1"])   # `run.py`'s argument

    def test_a_program_word_that_is_a_path_to_a_file(self):
        self.assertEqual(of("run", "scripts/run.sh --fast"), ["scripts/run.sh"])
        self.assertEqual(of("run", "../bin/tool.py x"), ["bin/tool.py"])         # `..` is left out
        self.assertEqual(of("run", "../tool.py x"), ["tool.py"])
        self.assertEqual(of("run", "$SCRATCH/chk.sh"), ["chk.sh"])             # a bare variable too
        self.assertEqual(of("run", "KEEP=1 ./verify.sh"), ["verify.sh"])
        for text in ("./build/a.out", "/usr/bin/env", "make", "ls scripts/run.sh",
                     "update.sh --fast", "source env.sh"):
            self.assertEqual(of("run", text), [], text)

    def test_uv_run_reads_as_the_command_it_runs(self):
        self.assertEqual(of("run", "uv run python decks/build.py --only 5_safety"),
                         ["decks/build.py"])
        self.assertEqual(of("run", "uv run --with rich --project x ./go.sh"), ["go.sh"])
        self.assertEqual(of("test", "uv run python -m pytest tests/test_x.py::test_y -q"),
                         ["tests/test_x.py"])
        self.assertEqual(of("test", "uv run pytest tests/test_x.py"), ["tests/test_x.py"])
        self.assertEqual(of("write", "uv run python - <<'EOF'\nfrom pathlib import Path\n"
                                     "Path('out/a.py').write_text('x')\nEOF"), ["out/a.py"])
        self.assertEqual(of("write", "FOO=$SCRATCH/x uv run python -c 'print(1)' > out.json"),
                         ["out.json"])
        self.assertIn(("scratch", "x"), names_of("FOO=$SCRATCH/x uv run python -c 'print(1)'"))
        self.assertEqual(of("pkg", "uv add polars --dev"), ["polars"])             # not under `run`
        self.assertEqual(names_of("uv run"), ())

    def test_uv_run_keeps_what_the_command_was_given_and_the_command_is_not_changed(self):
        cmd = _commands("FOO=1 uv run --with x python - <<'EOF' | tee out.txt\nprint(1)\nEOF")[0]
        fields = ("name", "prog", "args", "kw", "words", "pipe_to", "heredoc", "writes", "bg",
                  "sub")
        before = [getattr(cmd, f) for f in fields]
        eff = _effective(cmd)
        self.assertIsNot(eff, cmd)
        self.assertEqual((eff.name, eff.prog, eff.args), ("python", "python", ["-"]))
        for field in ("words", "pipe_to", "heredoc", "writes", "bg", "sub"):
            self.assertEqual(getattr(eff, field), getattr(cmd, field), field)
        self.assertEqual((eff.pipe_to, eff.heredoc), ("tee", "print(1)"))
        self.assertIn("FOO=1", eff.words)
        self.assertEqual([getattr(cmd, f) for f in fields], before)      # the original is as it was
        for text in ("python x.py", "uv add polars", "uv run", "uv"):
            other = _commands(text)[0]
            self.assertIs(_effective(other), other, text)

    def test_an_sbatch_script_is_run_and_a_job_and_an_option_value_is_not_the_script(self):
        self.assertEqual(names_of("sbatch --parsable -p gpu -t 1:00:00 slurm/gpu.sbatch"),
                         (("run", "slurm/gpu.sbatch"), ("job", "gpu.sbatch")))
        self.assertEqual(of("run", "sbatch --test-only scbench/analysis/x_20261009/refresh.sbatch"),
                         ["x_20261009/refresh.sbatch"])
        self.assertEqual(of("run", "sbatch --wrap='python x.py'"), ["x.py"])    # what it wraps
        self.assertEqual(of("run", "sbatch -o logs/out.txt run.sh"), ["run.sh"])

    def test_sbatch_wrap_gives_a_command_line_that_is_read_and_no_script_and_no_job(self):
        self.assertEqual(names_of("sbatch --wrap 'python x.py'"), (("run", "x.py"),))
        self.assertEqual(names_of("sbatch --wrap='python x.py'"), (("run", "x.py"),))
        self.assertEqual(names_of("sbatch --wrap 'python train.py'"), (("run", "train.py"),))
        self.assertEqual(of("job", "sbatch --wrap 'python x.py'"), [])
        self.assertEqual(names_of("sbatch --account=delta_lab -p gpu "
                                  "--wrap 'python /n/home03/carol/x.py'"),
                         (("run", "carol/x.py"),))
        # a job name is the one `-J` or `--job-name` gives
        self.assertEqual(names_of("sbatch -J jx --wrap 'python x.py'"),
                         (("job", "jx"), ("run", "x.py")))
        self.assertEqual(names_of("sbatch --wrap 'python x.py' --job-name jy"),
                         (("job", "jy"), ("run", "x.py")))
        # the line is read as any command line is
        self.assertEqual(names_of("sbatch --wrap 'pytest tests/test_a.py > out.json'"),
                         (("test", "tests/test_a.py"), ("write", "out.json")))
        self.assertEqual(names_of("sbatch -p test --wrap 'cd /r/analysis && ./update.sh'"),
                         (("cd", "analysis"), ("run", "update.sh")))
        # what is wrapped is a program, so it is no word
        self.assertEqual(of("word", "sbatch --wrap train_2"), [])
        # after a script its options are its own, and `--wrap` is no script
        self.assertEqual(names_of("sbatch run.sh --wrap 'python x.py'"),
                         (("run", "run.sh"), ("job", "run.sh")))

    def test_an_sbatch_wrap_inside_another_is_read_to_a_depth_and_does_not_recurse_forever(self):
        def wrapped(depth, inner="python deep.py"):
            for _ in range(depth):
                inner = "sbatch --wrap " + shlex.quote(inner)
            return inner

        self.assertEqual(of("run", wrapped(1)), ["deep.py"])
        self.assertEqual(of("run", wrapped(3)), ["deep.py"])
        self.assertEqual(of("run", wrapped(4)), [])                  # too deep to be read
        self.assertEqual(of("run", wrapped(6)), [])                  # and nothing raises

    def test_a_word_with_a_space_is_no_script_and_the_options_of_sbatch_are_all_passed_over(self):
        self.assertEqual(names_of("sbatch 'my script.sh'"), ())
        self.assertEqual(names_of("sbatch --newer-option 'a b.c' run.sh"), ())
        self.assertEqual(names_of("sbatch myjob"), ())                  # no `/`, and no extension
        self.assertEqual(names_of("sbatch 'run=1.sh'"), ())             # an assignment
        self.assertEqual(of("run", "sbatch --open-mode append run.sh"), ["run.sh"])
        self.assertEqual(of("run", "sbatch --time-min 10 run.sh"), ["run.sh"])
        self.assertEqual(of("run", "sbatch --ntasks-per-gpu 1 slurm/run.sh"), ["slurm/run.sh"])
        # every option of sbatch's manual that takes a value, given apart from it
        for option in SBATCH_VALUED:
            self.assertEqual(names_of("sbatch %s v run.sh" % option),
                             (("run", "run.sh"), ("job", "run.sh")), option)
            self.assertEqual(names_of("sbatch -J j %s v slurm/run.sh" % option),
                             (("job", "j"), ("run", "slurm/run.sh"), ("job", "run.sh")), option)
        # and its flags, which take none
        for flag in ("--parsable", "--test-only", "-H", "--hold", "-W", "--wait", "-Q", "--quiet",
                     "-v", "--verbose", "--exclusive", "--requeue", "--no-requeue", "-s",
                     "--oversubscribe", "-O", "--overcommit", "--reboot", "--spread-job",
                     "--use-min-nodes", "--ignore-pbs", "--nice", "--propagate", "-k"):
            self.assertEqual(of("run", "sbatch %s run.sh" % flag), ["run.sh"], flag)

    def test_the_options_of_srun_are_passed_over_to_its_job_name(self):
        for option in SBATCH_VALUED + SRUN_VALUED:
            self.assertEqual(of("job", "srun %s v -J w1 python y.py" % option), ["w1"], option)
        self.assertEqual(
            of("job", "srun --exact -n 4 --mpi pmix --cpu-bind cores -J w2 python y.py"), ["w2"])
        # `-W` waits for sbatch and takes a time for srun
        self.assertEqual(of("run", "sbatch -W run.sh"), ["run.sh"])
        self.assertEqual(of("job", "srun -W 30 -J w3 python y.py"), ["w3"])

    def test_salloc_reads_its_options_as_srun_does(self):
        # an allocation's partition, account, and time are no words, and its job name is a job
        self.assertEqual(names_of("salloc -p kempner_h100 -A kempner_lab -t 2:00:00 --gres=gpu:1 "
                                  "--mem 64G -J dbg_1"), (("job", "dbg_1"),))
        for option in SBATCH_VALUED + SRUN_VALUED:
            self.assertEqual(of("job", "salloc %s v -J w1 bash" % option), ["w1"], option)
        # what it runs after its options is read as srun's is, and has no script to name
        self.assertEqual(names_of("salloc -p gpu_1 -n 2 srun tool run_6"), (("word", "run_6"),))
        self.assertEqual(of("run", "salloc -n 2 slurm/run.sh"), [])

    def test_test_runners_name_their_files_with_the_test_cut_off(self):
        self.assertEqual(of("test", "pytest -q tests/unit/test_video_io.py::TestA::test_b"),
                         ["unit/test_video_io.py"])
        self.assertEqual(
            of("test", "python -m pytest --ignore tests/slow.py -k fast tests/test_a.py"),
            ["tests/test_a.py"])
        self.assertEqual(of("test", "python -u -m pytest a/test_b.py c/test_d.py"),
                         ["a/test_b.py", "c/test_d.py"])
        self.assertEqual(
            of("test", "pytest --ignore tests/slow.py tests/test_a.py -c pytest.ini"),
            ["tests/test_a.py"])
        self.assertEqual(
            of("test", "python -m unittest discover -s tests -p 'test_loader.py' -v"),
            ["test_loader.py"])
        self.assertEqual(of("test", "uv run --directory dashboard python -m unittest discover "
                                    "-s tests -p 'test_phase_names.py' -v"),
                         ["test_phase_names.py"])
        for text in ("pytest tests/ -x", "pytest", "python -m pytest", "python -m json.tool a.json",
                     "ruff check src/a.py"):
            self.assertEqual(of("test", text), [], text)

    def test_files_that_are_written(self):
        self.assertEqual(of("write", "python run.py > out.json 2>&1"), ["out.json"])
        self.assertEqual(of("write", "echo hi >> docs/notes.md"), ["docs/notes.md"])
        self.assertEqual(of("write", "echo hi | tee -a docs/notes.md"), ["docs/notes.md"])
        self.assertEqual(of("write", "sed -i 's/a/b/' src/a.py src/b.py"),
                         ["src/a.py", "src/b.py"])
        self.assertEqual(of("write", "sed -i.bak -e 's/a/b/' src/a.py"), ["src/a.py"])
        self.assertEqual(of("write", "perl -pi -e 's/a/b/' x.py y.py"), ["x.py", "y.py"])
        self.assertEqual(of("write", "sed -n -i 's/a/b/p' src/a.py"), ["src/a.py"])   # quiet too
        self.assertEqual(of("write", "cp old.py dir/new.py"), ["dir/new.py"])
        self.assertEqual(of("write", "mv a.md b.md"), ["b.md"])
        for text in ("cp a.py dir/", "cp -t dir a.py", "cp a.py", "python run.py > /dev/null",
                     "python run.py > out.log", "sed 's/a/b/' src/a.py", "cat a.py"):
            self.assertEqual(of("write", text), [], text)

    def test_files_that_python_writes_by_a_literal_or_a_name(self):
        def py(body):
            return "python3 - <<'EOF'\n%s\nEOF" % body

        self.assertEqual(
            of("write", py('from pathlib import Path\nPath("pkg/mod.py").write_text("x")')),
            ["pkg/mod.py"])
        self.assertEqual(
            of("write", py('with open("data/out.csv", "w") as f:\n    f.write("x")')),
            ["data/out.csv"])
        self.assertEqual(of("write", py("open('notes.md', 'a').write('x')")), ["notes.md"])
        self.assertEqual(
            of("write", py('p = "src/a.py"\ns = open(p).read()\nopen(p, "w").write(s)')),
            ["src/a.py"])
        self.assertEqual(
            of("write", py("import pathlib\np = pathlib.Path('docs/b.md'); s = p.read_text()\n"
                           "p.write_text(s)")), ["docs/b.md"])
        self.assertEqual(
            of("write", py('from pathlib import Path\nt = Path("x/y.py")\n'
                           'Path(t).write_bytes(b"")')), ["x/y.py"])
        # the last assignment before the write is the file; one after it is not
        self.assertEqual(of("write", py('p = "a.py"\nopen(p, "w")\np = "b.py"\nopen(p, "wb")')),
                         ["a.py", "b.py"])
        self.assertEqual(of("write", py('open(p, "w")\np = "late.py"')), [])
        # a script given to python has the heredoc for stdin: it is not code
        self.assertEqual(of("write", "python run.py <<'EOF'\nopen(\"x.py\", \"w\")\nEOF"), [])
        self.assertEqual(of("write", "python -c 'open(\"x.py\", \"w\")'"), ["x.py"])
        self.assertEqual(of("write", "python -W ignore - <<'EOF'\nopen('x.py','w')\nEOF"),
                         ["x.py"])

    def test_python_that_reads_or_cannot_be_resolved_writes_nothing(self):
        def py(body):
            return "python3 - <<'EOF'\n%s\nEOF" % body

        for body in ('open("src/a.py").read()', 'open("src/a.py", "r")',
                     'x = open(f"{d}/a.py", "w")', 'open(os.path.join(d, "a.py"), "w")',
                     'for p in ["a.py", "b.py"]:\n open(p, "w")', 'self.p.write_text("x")',
                     'Path("a.py").read_text()', 'open("a.py", mode="r")'):
            self.assertEqual(of("write", py(body)), [], body)

    def test_files_that_are_read(self):
        self.assertEqual(of("read", "cat a.py b.md | head"), ["a.py", "b.md"])
        self.assertEqual(of("read", "head -n 5 notes.md"), ["notes.md"])
        self.assertEqual(of("read", "tail -n +5 logs/x.txt"), ["logs/x.txt"])
        self.assertEqual(of("read", "less docs/a.md"), ["docs/a.md"])
        self.assertEqual(of("read", "nl -ba src/a.py"), ["src/a.py"])
        self.assertEqual(of("read", "wc -l src/a.py"), ["src/a.py"])
        self.assertEqual(of("read", "diff -u old.py new.py"), ["old.py", "new.py"])
        self.assertEqual(of("read", "sed -n 1,5p a.py"), ["a.py"])
        self.assertEqual(of("read", "sed -n -e 1,5p a.py b.py"), ["a.py", "b.py"])
        self.assertEqual(of("read", "grep -n foo src/a.py"), ["src/a.py"])
        self.assertEqual(of("read", "rg foo src/a.py docs/b.md"), ["src/a.py", "docs/b.md"])
        # a pattern that looks like a file is not one
        self.assertEqual(of("read", "grep -n a.py src/b.py"), ["src/b.py"])
        self.assertEqual(of("read", "rg -e a.py src/b.py"), ["src/b.py"])
        self.assertEqual(of("read", "grep --include=*.py -rn x ."), [])
        self.assertEqual(of("read", "grep -e a.py -e b.py src/c.py"), ["src/c.py"])
        for text in ("sed 's/a/b/' a.py", "sed -i 's/a/b/' a.py", "ls a.py", "cat <<EOF\nx.py\nEOF",
                     "cat > a.py <<EOF\nx\nEOF", "sed -n -i 's/a/b/p' a.py", "ugrep foo src/a.py"):
            self.assertEqual(of("read", text), [], text)

    def test_a_glob_an_expansion_a_sentence_and_an_assignment_are_no_file(self):
        for text in ("cat src/*.py", "cat 'a b.py'", "cat a=b.py", "cat $(ls).py", "cat x{a,b}.py",
                     "cat x{1..3}.py", "cat `ls`.py", "cat -v.py", "cat py", "cat .py", "cat x.pyc",
                     "cat dir.py/", "cat a[12].py", "cat a?.py"):
            self.assertEqual(of("read", text), [], text)
        # a name that holds a variable is none, `${X}` as `$X` (until 2026-11-03 it was part of it)
        self.assertEqual(of("read", "cat run_$ID.py ${X}.py"), [])

    def test_a_file_is_shown_by_its_last_two_components(self):
        self.assertEqual(of("read", "cat /a/b/c/d.py"), ["c/d.py"])
        self.assertEqual(of("read", "cat d.py"), ["d.py"])
        self.assertEqual(of("read", "cat ./d.py ../c/d.py"), ["d.py", "c/d.py"])
        self.assertEqual(of("read", "cat ~/d.py $HOME/e.py ${X}/f/g.py"),
                         ["d.py", "e.py", "f/g.py"])


class ThingsTest(unittest.TestCase):
    def test_folders(self):
        self.assertEqual(of("dir", "mkdir -p a/b/c d"), ["b/c", "d"])
        self.assertEqual(of("dir", "mkdir -pv out/ --mode 755"), ["out"])
        self.assertEqual(of("dir", "mkdir -p x/{train,test}/logs"), ["x"])  # cut at the first brace
        self.assertEqual(of("dir", "mkdir -p {a,b}/c"), [])
        self.assertEqual(of("dir", "git worktree add ../x-wt -b feat/y"), ["x-wt"])
        self.assertEqual(of("dir", "git worktree add -b feat/y ../x-wt"), ["x-wt"])
        self.assertEqual(of("dir", "git worktree add --detach .claude/worktrees/seek abc1234"),
                         ["worktrees/seek"])
        self.assertEqual(of("dir", "rsync -a --exclude '*.pyc' src/ dest/sub/"), ["dest/sub"])
        self.assertEqual(of("dir", "cp -r a b/"), ["b"])
        self.assertEqual(of("dir", "cp -a a b/"), ["b"])
        self.assertEqual(of("dir", "cp -R a b/"), ["b"])
        self.assertEqual(of("dir", "cp --recursive a b/"), ["b"])
        self.assertEqual(of("dir", "cp --archive a b/"), ["b"])
        for text in ("mkdir plain", 'mkdir -p "$OUT"', "mkdir -p $(date)/x", "rsync -a src/ dest",
                     "rsync -a src/", "cp a b/", "git worktree list", "git worktree add"):
            self.assertEqual(of("dir", text), [], text)

    def test_jobs(self):
        self.assertEqual(of("job", "sbatch --job-name a-b run.sh"), ["a-b", "run.sh"])
        self.assertEqual(of("job", "sbatch -J x1 -p test run.sh"), ["x1", "run.sh"])
        self.assertEqual(of("job", "srun -J w1 -n 4 python y.py"), ["w1"])
        self.assertEqual(of("job", "sbatch --job-name=a-b slurm/gpu.sbatch"), ["a-b", "gpu.sbatch"])
        # after the script an option is the script's own, and another program has no job
        self.assertEqual(of("job", "sbatch run.sh --job-name=late"), ["run.sh"])
        self.assertEqual(of("job", "srun python y.py -J late"), [])
        self.assertEqual(of("job", "srun -n 2 slurm/run.sh"), [])      # srun has no script to name
        self.assertEqual(of("job", "echo -J nope"), [])
        self.assertEqual(of("job", "sbatch --job-name=$NAME run.sh"), ["run.sh"])

    def test_prs(self):
        self.assertEqual(of("pr", "gh pr checks 229 --repo org/repo --watch"), ["#229"])
        self.assertEqual(of("pr", "gh pr view --repo org/repo 8"), ["#8"])
        self.assertEqual(of("pr", "gh pr merge 233 --squash"), ["#233"])
        self.assertEqual(of("pr", "gh pr diff 7"), ["#7"])
        self.assertEqual(of("pr", "gh pr edit 235 --title t"), ["#235"])
        for text in ("gh pr create --title t", "gh pr list --author @me", "gh pr view feat/x",
                     "gh pr view", "gh issue view 5", "gh run view 12", "gh pr view main",
                     "gh pr checks my_branch", "gh pr view 12x"):
            self.assertEqual(of("pr", text), [], text)

    def test_a_pr_number_after_the_options_of_its_verb_that_take_a_value(self):
        self.assertEqual(of("pr", "gh pr edit --add-label bug 235 --title T"), ["#235"])
        # the options of each `gh pr <verb>` that take a value, from its manual, then the number
        valued = {
            "edit": "--add-assignee --add-label --add-project --add-reviewer -B --base -b --body "
                    "-F --body-file -m --milestone --remove-assignee --remove-label "
                    "--remove-project --remove-reviewer -t --title -R --repo",
            "view": "-q --jq --json -t --template -R --repo",
            "checks": "-i --interval -q --jq --json -t --template -R --repo",
            "merge": "-A --author-email -b --body -F --body-file --match-head-commit -t --subject "
                     "-R --repo",
            "diff": "--color -e --exclude -R --repo",
        }
        for verb, options in valued.items():
            for option in options.split():
                self.assertEqual(of("pr", "gh pr %s %s v 235" % (verb, option)), ["#235"],
                                 (verb, option))
        # `-r` and `-m` take a value for `edit` and `create`, and are flags for `merge`
        self.assertEqual(of("pr", "gh pr merge -r 235"), ["#235"])
        self.assertEqual(of("pr", "gh pr merge -m 235"), ["#235"])
        self.assertEqual(of("pr", "gh pr merge --squash --delete-branch 235"), ["#235"])
        self.assertEqual(of("pr", "gh pr edit -m v1 235"), ["#235"])
        self.assertEqual(of("pr", "gh pr view --web --comments 235"), ["#235"])
        self.assertEqual(of("pr", "gh --repo o/r pr view 8"), ["#8"])                # before `pr`
        self.assertEqual(of("pr", "gh pr -R o/r view 8"), ["#8"])                  # before the verb
        self.assertEqual(of("pr", "gh -R o/r pr edit --add-label bug 8 --title T"), ["#8"])
        self.assertEqual(of("pr", "gh pr create 5"), [])                             # no number yet

    def test_branches(self):
        self.assertEqual(of("branch", "git push -u origin HEAD:lep_calib"), ["lep_calib"])
        self.assertEqual(of("branch", "git push jfc lep-calib:lep_calib"), ["lep_calib"])
        self.assertEqual(of("branch", "git push -q origin main"), ["main"])
        self.assertEqual(of("branch", "git -C /r push origin feat/x"), ["feat/x"])
        self.assertEqual(of("branch", "git checkout -b feat/x origin/main"), ["feat/x"])
        self.assertEqual(of("branch", "git switch -c fix/y"), ["fix/y"])
        self.assertEqual(of("branch", "gh pr create --head owner:feat/z --title t"), ["feat/z"])
        self.assertEqual(of("branch", "gh pr create --head=feat/q"), ["feat/q"])
        for text in ("git push", "git push origin", "git checkout main", "git switch main",
                     "git checkout -b", "git pull origin main"):
            self.assertEqual(of("branch", text), [], text)

    def test_head_names_no_branch_and_a_destination_loses_its_refs_heads(self):
        for text in ("git push -u origin HEAD", "git push origin @", "git push origin HEAD:HEAD",
                     "git push origin @:@", "git push --set-upstream origin HEAD"):
            self.assertEqual(of("branch", text), [], text)
        self.assertEqual(of("branch", "git push origin HEAD:refs/heads/x"), ["x"])
        self.assertEqual(of("branch", "git push origin refs/heads/x"), ["x"])
        self.assertEqual(of("branch", "git push origin refs/heads/a:refs/heads/b"), ["b"])
        self.assertEqual(of("branch", "git push origin @:topic"), ["topic"])
        self.assertEqual(of("branch", "git push origin +HEAD:refs/heads/x"), ["x"])
        self.assertEqual(of("branch", "git push origin +feat/x"), ["feat/x"])        # a forced push
        self.assertEqual(of("branch", "git push origin HEAD:refs/for/main"), ["refs/for/main"])
        self.assertEqual(of("branch", "git push origin headline"), ["headline"])    # only `HEAD`
        self.assertEqual(of("branch", "git push origin HEAD-fix"), ["HEAD-fix"])

    def test_worktrees(self):
        self.assertEqual(of("worktree", "git -C /r/.claude/worktrees/pr9-fix status"), ["pr9-fix"])
        self.assertEqual(of("worktree", "W=/r/.claude/worktrees/pr9-with-230; cd $W"),
                         ["pr9-with-230"])
        self.assertEqual(of("worktree", "cat /r/.claude/worktrees/a.b_c/x.py "
                                        "> /r/.claude/worktrees/z/y"), ["a.b_c", "z"])
        self.assertEqual(of("worktree", "ls $R/.claude/worktrees/$NAME"), [])
        self.assertEqual(of("worktree", "ls /r/.claude/worktrees"), [])
        self.assertEqual(of("worktree", "ls /r/.claude/worktrees/agent-*"), [])  # a glob: none

    def test_packages(self):
        self.assertEqual(of("pkg", 'pip install numpy==1.26 "scipy[extra]>=1.10" '
                                   "-r requirements.txt ./local git+https://x.org/r.git pkg.whl"),
                         ["numpy", "scipy"])
        self.assertEqual(of("pkg", "pip3 install --upgrade pip setuptools"), ["pip", "setuptools"])
        self.assertEqual(of("pkg", "pip install --target libs numpy"), ["numpy"])
        self.assertEqual(of("pkg", "python -m pip install --user rich"), ["rich"])
        self.assertEqual(of("pkg", "python3 -m pip install -e . pandas"), ["pandas"])
        self.assertEqual(of("pkg", "uv pip install -e . pandas"), ["pandas"])
        self.assertEqual(of("pkg", "uv pip install -r requirements.txt torch==2.1.0+cu118"),
                         ["torch"])
        self.assertEqual(of("pkg", "conda install -n env -c conda-forge bioconda::fastp=0.23 -y"),
                         ["fastp"])
        self.assertEqual(of("pkg", "mamba create -n x python=3.11 numpy"), ["python", "numpy"])
        self.assertEqual(of("pkg", "micromamba install -p $S/renv r-base=4.4"), ["r-base"])
        self.assertEqual(of("pkg", "conda create --name y --clone x --yes"), [])
        self.assertEqual(of("pkg", "module load cuda/12.2 gcc"), ["cuda/12.2", "gcc"])
        self.assertEqual(of("pkg", "singularity exec --bind /a:/b --pwd /a /img/fastp_0.23.sif "
                                   "fastp -i x"), ["fastp_0.23.sif"])
        self.assertEqual(of("pkg", "apptainer exec docker://ubuntu:22.04 ls"), ["ubuntu:22.04"])
        for text in ("pip list", "pip install", "conda env list", "mamba env create -f e.yml",
                     "module avail cuda", "module list", "singularity run x.sif",
                     "singularity exec", "uv run pytest", "uv tool install ruff"):
            self.assertEqual(of("pkg", text), [], text)

    def test_scratch_names_an_agent_gives_its_own_files(self):
        self.assertEqual(of("scratch", "SNAP=$SCRATCH/before_reconcile; cp -r a $SNAP"),
                         ["before_reconcile"])
        self.assertEqual(of("scratch", "python x.py > /tmp/run1.log 2>&1"), ["run1"])
        self.assertEqual(of("scratch", "tail -f data/logs/rerun.log"), ["rerun"])
        self.assertEqual(of("scratch", "cat $TMPDIR/out.txt ${SCRATCH}/more.txt"), ["out", "more"])
        self.assertEqual(of("scratch", "cat ~/tmp/notes_1.txt tmp/final/merge_real.log"),
                         ["notes_1", "merge_real"])
        self.assertEqual(of("scratch", "export LOG=data/logs/x.log"), ["x"])
        self.assertEqual(of("scratch", "D=/tmp/claude-1/p/abc/scratchpad/stage_2"), ["stage_2"])
        self.assertEqual(of("scratch", "cp a.py /tmp/b.py; sed -i s/x/y/ $SCRATCH/c.py"),
                         ["b", "c"])
        for text in ("cat results/a.json", "echo hi > $SCRATCH/update_*.log", "cat $SCRATCH/",
                     "cat $SCRATCH", "X=results/a.log", "cat $SCRATCH/${NAME}.log",
                     "cat /tmp/a/", "cd $SCRATCH"):
            self.assertEqual(of("scratch", text), [], text)

    def test_a_name_that_holds_a_shell_variable_is_no_name(self):
        # what the variable holds is filled in when the command runs, and is not in its text
        self.assertEqual(names_of("for s in a b; do uv run python scripts/$s.py; done"), ())
        self.assertEqual(of("job", "sbatch -J diag-$name x.sbatch"), ["x.sbatch"])
        self.assertEqual(of("job", "sbatch --job-name=$ x.sbatch"), ["x.sbatch"])     # a bare $
        self.assertEqual(of("write", "python gen.py > out/${c}_new.json"), [])
        self.assertEqual(of("grep", r'grep -n "\b$fn\b" lib.py'), [])
        self.assertEqual(of("read", r'grep -n "\b$fn\b" lib.py'), ["lib.py"])
        self.assertEqual(of("dir", "mkdir -p run_$mode/data"), [])
        # a regex's anchor and an escaped dollar expand nothing, and a variable that is a whole
        # path component is left out of the path
        self.assertEqual(of("grep", "grep -cE '(idle|mix)$' log.txt"), ["(idle|mix)$"])
        self.assertEqual(of("grep", r"grep -n '\$PYTHON' run.sh"), [r"\$PYTHON"])
        self.assertEqual(of("read", "cat $HOME/e.py ${X}/f/g.py"), ["e.py", "f/g.py"])

    def test_a_name_that_holds_any_dollar_is_no_name_but_a_search_terms_anchor(self):
        # The shell's own parameters are filled in when the command runs too, and a `$( )` the
        # words were cut at leaves a `$` at the end of a name. Any `$` makes a name none, but in
        # a search term, where one at the end is a regex's anchor. (Until 2026-11-03 these were
        # names: `run_$$`, `j_$`, and `feat-$`.)
        for p in ("$$", "$?", "$@", "$#"):
            self.assertEqual(of("job", "sbatch -J run_%s x.sbatch" % p), ["x.sbatch"], p)
        self.assertEqual(of("branch", "git checkout -b feat-$(date +%s)"), [])
        self.assertEqual(of("branch", "git switch -c fix_$$"), [])
        self.assertEqual(of("job", "sbatch --job-name=j_$(date +%s) x.sbatch"), ["x.sbatch"])
        self.assertEqual(of("grep", "grep -c 'x$|y$' a.txt"), ["x$|y$"])

    def test_claude_codes_output_of_a_background_task_is_no_scratch_name(self):
        # Matched by its path, wherever the temporary folder is. Until 2026-11-03 such an id was a
        # scratch name: `b3tp2mtp4` below was one.
        for text in ("cat /tmp/claude-1004/-madsys-x/1a2b3c4d/tasks/bkkcpfyha.output | tail -5",
                     "cat /n/home/x/tmp/claude-61005/s/tasks/b3tp2mtp4.output",
                     "tail -20 /private/tmp/claude-501/-Users-x/0a1b/tasks/b02dzvoij.output",
                     "O=$TMPDIR/claude-7/p/s/tasks/bp2h1x336.output; sleep 30; cat $O"):
            self.assertEqual(of("scratch", text), [], text)
        # a name of the same shape that the agent gave its own file is still one, and so is the
        # output of a `tasks/` folder that is not Claude Code's
        self.assertEqual(of("scratch", "python bench.py > /tmp/burstgpt2.log"), ["burstgpt2"])
        self.assertEqual(of("scratch", "cat /tmp/run/tasks/b3tp2mtp4.output"), ["b3tp2mtp4"])

    def test_cd_targets(self):
        self.assertEqual(of("cd", "cd /a/b/c/ && ls"), ["c"])
        self.assertEqual(of("cd", "cd $SCRATCH/kempner"), ["kempner"])
        self.assertEqual(of("cd", "cd -P ../tagflow/flows"), ["flows"])
        self.assertEqual(of("cd", "cd run_$ID"), [])      # a variable's value: no name since 11-03
        for text in ("cd ..", "cd -", "cd ~", 'cd "$DIR"', "cd", "cd $SCRATCH", "cd /", "cd a/*",
                     "cd $(mktemp -d)", "cd a/{b,c}", "cd a=b"):
            self.assertEqual(of("cd", text), [], text)

    def test_search_terms(self):
        self.assertEqual(of("grep", "rg -e 'foo|bar' src"), ["foo|bar"])
        self.assertEqual(of("grep", "grep -ie Foo src"), ["Foo"])
        self.assertEqual(of("grep", "grep --regexp=foo src"), ["foo"])
        self.assertEqual(of("grep", "grep -A 3 foo f.py"), ["foo"])
        self.assertEqual(of("grep", "grep -rn --include=*.py kempner ."), ["kempner"])
        self.assertEqual(of("grep", "ugrep -i federat docs"), ["federat"])
        self.assertEqual(of("grep", "rg -g '*.py' -t py def_x src"), ["def_x"])
        self.assertEqual(of("grep", "squeue -u me | grep -c PASSED"), ["PASSED"])
        self.assertEqual(of("grep", "grep " + "x" * 100 + " f"), ["x" * 39 + "…"])
        self.assertEqual(of("grep", "grep 'a   b' f"), ["a b"])                 # whitespace folds
        self.assertEqual(of("grep", "grep 'a\nb' f"), ["a b"])
        self.assertEqual(names_of("grep -n foo src/a.py"), (("grep", "foo"), ("read", "src/a.py")))
        # a filter's pattern, no pattern, and a pattern with nothing in it name nothing
        for text in ("squeue | grep -v Dependency", "grep -rniv Warning .",
                     "rg --files -g '*.py' src", "grep -f patterns.txt f.py", "grep '^$' f",
                     "grep", "egrep foo f"):
            self.assertEqual(of("grep", text), [], text)

    def test_recurring_words(self):
        self.assertEqual(of("word", "bash refbias_run.sh marinQC51 --throttle 15"), ["marinQC51"])
        self.assertEqual(
            of("word", "python train.py --tag run_a --seed 5 --cohort lc2_cl_opus_p3"),
            ["run_a", "lc2_cl_opus_p3"])
        self.assertEqual(of("word", "tool run_1 run_2 run_3"), ["run_1", "run_2"])
        self.assertEqual(MAX_WORDS, 2)
        # 4 to 40 characters, a letter, and a digit or `_`
        self.assertEqual(
            of("word", "tool ab_ %s xyz1 abcdef 1234 1_2_3 %s" % ("x1" * 21, "x1" * 20)),
            ["xyz1", "x1" * 20])
        # a word given twice is one, and counts once toward the two
        self.assertEqual(of("word", "tool aaa_1 aaa_1 bbb_2 ccc_3"), ["aaa_1", "bbb_2"])
        self.assertEqual(of("word", "echo aaa_1; echo aaa_1; echo bbb_2; echo ccc_3"),
                         ["aaa_1", "bbb_2"])
        # not a flag, a path, a URL, an assignment, a sentence, a number, a timestamp, or a job step
        for text in ("tool --flag_1 /path/a_1 ./a_1 a/b_1 https://x.org/a_1 K=v_1 'two words_1' "
                     "12345 1.5e3 1e-4 0x1F 150G",
                     "tool -S 2026-10-09T08:20 -j 51643995.batch 51643995.extern",
                     "tool a..b_1", "tool [REDACTED]", "tool $VAR_1 ~/x_1 {a_1}",
                     "tool results_2026.csv run.py"):
            self.assertEqual(of("word", text), [], text)

    def test_a_hash_a_key_and_a_uuid_are_no_words(self):
        # only hex digits and dashes: a git hash, a key, a session id
        for text in ("git show a3844c0", "git diff a3844c0 b54b386",
                     "tool abc1234 def5678 9999aaaa",
                     "claude --session-id 08749fc1-dedd-48d3-add6-8890798cd937",
                     "claude --resume 08749fc1-dedd-48d3-add6-8890798cd937",
                     "git worktree add --detach /w/x e6c24d72493e14cb8171bb0d9f9c2c339214efba",
                     "tool 3f2a9c0e1b7d4c5a8e6f0a1b2c3d4e5f6a7b8c9d", "tool abc-123",
                     "tool A1B2-C3D4", "tool 0123456789abcdef", "tool a346"):
            self.assertEqual(of("word", text), [], text)
        # a letter past `f`, or an underscore, makes it a word
        self.assertEqual(of("word", "tool a3844c0x"), ["a3844c0x"])
        self.assertEqual(of("word", "tool a3844c0_b"), ["a3844c0_b"])
        self.assertEqual(of("word", "tool xq404 U558 boslogin07"), ["xq404", "U558"])
        self.assertEqual(of("word", "tool 08749fc1-dedd-48d3-add6-8890798cd937-x"),
                         ["08749fc1-dedd-48d3-add6-8890798cd937-x"])

    def test_a_word_is_left_to_the_rule_that_took_it(self):
        self.assertEqual(of("word", "sbatch -J atm_e2e run_1.sbatch"), [])
        self.assertEqual(of("word", "git push origin feat_x"), [])
        self.assertEqual(of("word", "gh pr view 235 --repo org_x/repo"), [])
        self.assertEqual(of("word", "python -m py_compile x.py"), [])
        self.assertEqual(of("word", "cat notes_2 a.py"), [])
        self.assertEqual(of("word", "grep -n pat_1 file_2"), [])
        self.assertEqual(of("word", "cd analysis_2026_v2 && source venv_2/bin/activate"), [])
        self.assertEqual(of("word", "module load cuda12"), [])
        self.assertEqual(of("word", "export RUN_ID=run_3; source env_2"), [])
        self.assertEqual(of("word", "ls results_v2"), ["results_v2"])

    def test_a_value_an_option_takes_is_never_a_word(self):
        # The partition of boslogin06's apid 4123227 on 2026-10-09 named its only task
        # ("Run kempner_h100 · torch") until 2026-11-03.
        self.assertEqual(of("word", "srun -p kempner_h100 --account=kempner_x_lab --gres=gpu:1 "
                                    "-c 1 --mem=1G -t 00:02:00 nvidia-smi"), [])
        self.assertEqual(names_of("sbatch -p kempner_h100 -A kempner_lab -q prio_1 x.sbatch"),
                         (("run", "x.sbatch"), ("job", "x.sbatch")))
        # every reader that knows an option takes a value takes it
        for text in ("pytest -k test_loader_io tests/test_a.py", "python -W ignore_1 x.py",
                     "rsync -a --exclude __pycache__ src/ dst/", "rg -t py_2 foo src",
                     "conda run -n scbench_r7 python x.py", "pip install -P pin_1 numpy",
                     "gh pr create --title Fix --label bug_fix_1", "git -C repo_2 status",
                     "git push -o skip_ci origin main", "mkdir -p -m mode_7 out",
                     "singularity exec -B bind_1 img.sif ls", "sed -n -e p_1 x.txt"):
            self.assertEqual(of("word", text), [], text)
        # and the same text where no option took it is a word still
        self.assertEqual(of("word", "srun -p gpu_1 python x.py kempner_h100"), ["kempner_h100"])
        self.assertEqual(of("word", "python train.py --tag run_a"), ["run_a"])

    def test_slurms_queue_and_accounting_commands_give_no_words(self):
        # what they are given is the user (`squeue -u xq404` put the user in a headline), the
        # partition, the states, the format, and the jobs they ask about
        for text in ("squeue -u xq404 -o '%.10i %.14j %.8T'", "squeue --me -h -o %T",
                     "sinfo -p kempner_h100 -h -o '%N %f %G'", "sacct -j 51551763 -X -n -o State",
                     "sacct -u jq5150 -S 2026-10-09 -s FAILED,OUT_OF_MEMORY",
                     "sstat -j 51551763.batch -o MaxRSS", "sprio -u u_558",
                     "sshare -u u_558 -A lab_1", "scontrol show job 51504080 run_7"):
            self.assertEqual(names_of(text), (), text)
        # a job a submission names is named, and so is what the poll beside it looks for
        self.assertEqual(names_of("sbatch -J train_1 x.sbatch; squeue -n train_1 -u xq404"),
                         (("job", "train_1"), ("run", "x.sbatch"), ("job", "x.sbatch")))
        self.assertEqual(names_of("until squeue -u xq404 | grep -q train_1; do sleep 60; done"),
                         (("grep", "train_1"),))

    def test_a_shell_tests_operands_are_no_words(self):
        # They are a loop's stop condition, not its work. The shape of boslogin06's apid 2525257 on
        # 2026-10-09, whose headline ended `OUT_OF_ME · NODE_FAIL` until 2026-11-03:
        loop = ("until s=$(sacct -j 1 -o State | tr -d ' '); [[ \"$s\" =~ "
                "^(COMPLETED|OUT_OF_ME|NODE_FAIL) ]]; do sleep 60; done")
        self.assertEqual(of("word", loop), [])
        for text in ('[ "$x" = done_1 ]', "test -n run_2", "[[ -f out_3.log && $n -gt 4 ]]",
                     "while [ ! -s done_4 ]; do sleep 5; done"):
            self.assertEqual(of("word", text), [], text)
        # what the loop runs is read as it was: a word of the command beside the test is a word
        self.assertEqual(of("word", "until [ -f done_5 ]; do tool run_6; sleep 9; done"), ["run_6"])

    def test_a_subagents_worktree_id_is_no_name(self):
        # Claude Code's ids for the worktrees of two subagents were in the headline of boslogin06's
        # apid 1412291 on 2026-10-09: `sim-ew · sim/assemble.py · agent-a69e2723ecfaa3a16 · …`
        self.assertEqual(names_of("cd /n/home04/x/m/.claude/worktrees/agent-a69e2723ecfaa3a16/nlo "
                                  "&& cat assemble.py"), (("cd", "nlo"), ("read", "assemble.py")))
        for text in ("cd /r/.claude/worktrees/agent-a90e1b12ed275ef3f",
                     "git worktree add .claude/worktrees/agent-a1b2",
                     "git -C /r/.claude/worktrees/agent-a1b2 status", "tool agent-a69e2723ecfaa3a16"):
            self.assertEqual(names_of(text), (), text)
        # a worktree the agent names is a name, and so is a word that is not hex after `agent-`
        self.assertEqual(of("worktree", "cd /r/.claude/worktrees/agent-review && ls"),
                         ["agent-review"])
        self.assertEqual(of("word", "tool agent-x1"), ["agent-x1"])

    def test_claude_codes_own_folders_are_no_name(self):
        # Claude Code's temporary folder, the project and the session under it, and the session's
        # scratchpad and tasks: `Read 0d1e2f30-…/scratchpad` labeled a task of boslogin06's apid
        # 3801554 on 2026-10-09, and the scratchpad was a scratch name in apid 4193830's headline
        sp = "/tmp/claude-61002/-n-home02-x/0d1e2f30-b24b-4a90-91c8-76d345cac98f/scratchpad"
        for text in ("mkdir -p %s" % sp, "cd %s && ls" % sp, "D=%s" % sp, "D=%s/" % sp,
                     "mkdir -p /tmp/claude-1/p/abc/tasks", "cd /tmp/claude-1/p/abc",
                     "cd /tmp/claude-1/p", "cd $TMPDIR/claude-7/p/s/scratchpad",
                     "X=/n/home/x/tmp/claude-61005/s/0a1b/scratchpad"):
            self.assertEqual(names_of(text), (), text)
        # what the agent makes in its scratchpad is its own
        self.assertEqual(names_of("SP=%s; mkdir -p $SP/ext && cd $SP/ext" % sp),
                         (("dir", "ext"), ("cd", "ext")))
        self.assertEqual(names_of("mkdir -p %s/kempner" % sp), (("dir", "scratchpad/kempner"),))
        self.assertEqual(names_of("python %s/q.py > %s/q_out.log" % (sp, sp)),
                         (("run", "scratchpad/q.py"), ("scratch", "q_out")))
        self.assertEqual(of("scratch", "D=%s/sweep" % sp), ["sweep"])

    def test_a_file_at_the_depth_of_claude_codes_folder_is_a_name(self):
        # The agent of boslogin06's apid 1184368 on 2026-10-09 wrote `/tmp/claude-61001/rev/
        # conncmp.py`, at the depth of a session's folder, and apid 1825381's read
        # `/tmp/claude-61003/paper.txt`, at the depth of a project's. They are the agent's files.
        self.assertEqual(names_of("cat > /tmp/claude-61001/rev/conncmp.py <<'EOF'\nimport sys\n"
                                  "EOF\npython /tmp/claude-61001/rev/conncmp.py"),
                         (("write", "rev/conncmp.py"), ("run", "rev/conncmp.py")))
        self.assertEqual(names_of("sed -i 's|a|b|' /tmp/claude-61001/rev/conncmp.py"),
                         (("write", "rev/conncmp.py"),))
        self.assertEqual(names_of("head -40 /tmp/claude-61003/paper.txt"),
                         (("read", "claude-61003/paper.txt"),))
        # and a file in `~/.claude/projects` that is no transcript and no index is a name too
        self.assertEqual(of("read", "cat ~/.claude/projects/-a/notes.md"), ["-a/notes.md"])

    def test_what_claude_code_keeps_in_its_home_is_no_name(self):
        # `~/.claude/projects`, a project's folder in it, a session's transcript and the folder
        # beside it, and the project's `memory` folder and its index `MEMORY.md`. On 2026-10-09
        # boslogin06's apid 4193830 had a task `Run on Slurm -n-netscratch-…/3b2a1c0d-….jsonl`,
        # and apid 1103933 one `Edit memory/MEMORY.md · …`; rtx6000a's apid 2559300 had one
        # `… · memory/MEMORY.md` on 2026-11-01.
        home = "/n/home01/tpoe/.claude/projects"
        slug = home + "/-n-netscratch-alpha-lab-Lab-jdoe"
        session = slug + "/3b2a1c0d-eda1-4c9a-bcbd-2a03b975c3ac"
        memory = "/home/gus/.claude/projects/-home-gus-workspace-nimbus/memory"
        self.assertEqual(names_of("grep -o 'submit_chain() {' %s.jsonl | head -1" % session),
                         (("grep", "submit_chain() {"),))
        self.assertEqual(names_of("grep -n requeue %s/tool-results/bgotb4278.txt" % session),
                         (("grep", "requeue"),))
        for text in ("cd %s && ls" % home, "cd %s && ls" % slug, "cd %s/ && ls" % session,
                     "cat %s/subagents/agent-a8fbd34d0d9d56827.jsonl" % session,
                     "cd %s && ls" % memory, "mkdir -p %s" % memory,
                     "echo '- [No colons](writing-no-colons.md)' >> "
                     "/n/home13/nbay/.claude/projects/-n-home13-nbay/memory/MEMORY.md",
                     "python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"%s/MEMORY.md\")\n"
                     "p.write_text(p.read_text())\nEOF" % memory,
                     "cat .claude/projects/-a/memory/MEMORY.md"):
            self.assertEqual(names_of(text), (), text)
        self.assertEqual(names_of('grep -n "Load scaling" %s/MEMORY.md' % memory),
                         (("grep", "Load scaling"),))
        # a relative path is read in the folder the call changed into, and a variable the call
        # assigned is read as its value, but either is shown as the command gave it
        self.assertEqual(names_of("cd %s && grep -n router-worktree MEMORY.md && cat ./MEMORY.md"
                                  % memory), (("grep", "router-worktree"),))
        self.assertEqual(names_of("M=%s; cat $M/memory/MEMORY.md; cat ${M}/memory/MEMORY.md"
                                  % slug), ())
        self.assertEqual(names_of("cd /tmp/claude-1/p/abc && mkdir -p scratchpad"), ())
        # the notes the agent writes in `memory` are its own
        self.assertEqual(names_of("cd %s/; cat >> project_mark1_A_depletion.md <<'EOF'\n"
                                  "**UPDATE:** Fig 3A\nEOF\ngrep -q Fig3A MEMORY.md" % memory),
                         (("write", "project_mark1_A_depletion.md"), ("grep", "Fig3A")))
        self.assertEqual(names_of("cat %s/project-nimbus-load-scaling.md" % memory),
                         (("read", "memory/project-nimbus-load-scaling.md"),))
        self.assertEqual(names_of("cd %s && cd .. && cat notes.md" % memory),
                         (("read", "notes.md"),))
        # a `memory` folder elsewhere, and Codex's memory, are names: the rule is what Claude Code
        # keeps in its home
        self.assertEqual(names_of("cd /r/memory && cat MEMORY.md"),
                         (("cd", "memory"), ("read", "MEMORY.md")))
        self.assertEqual(of("read", "cat /n/home08/j/.codex/memories/MEMORY.md"),
                         ["memories/MEMORY.md"])


class BoundsAndMaskingTest(unittest.TestCase):
    def test_in_order_of_first_occurrence_and_without_duplicates(self):
        self.assertEqual(names_of("cat b.py a.py; cat b.py; cat c.py"),
                         (("read", "b.py"), ("read", "a.py"), ("read", "c.py")))
        self.assertEqual(names_of("cd analysis && ./update.sh > $SCRATCH/x.log"),
                         (("cd", "analysis"), ("run", "update.sh"), ("scratch", "x")))
        # one kind may repeat a name another kind holds
        self.assertEqual(names_of("cat a.py > a.py"), (("read", "a.py"), ("write", "a.py")))

    def test_one_command_cannot_hold_more_than_six(self):
        n = names_of("cat " + " ".join("f%d.py" % i for i in range(9)))
        self.assertEqual(n, tuple(("read", "f%d.py" % i) for i in range(MAX_NAMES)))

    def test_every_kind_is_a_known_one_and_the_constants_are_as_designed(self):
        self.assertEqual(KINDS, ("run", "test", "write", "read", "dir", "job", "pr", "branch",
                                 "worktree", "pkg", "scratch", "cd", "grep", "word"))
        self.assertEqual(SOURCES, ("pr", "commit", "log"))
        self.assertEqual((MAX_NAMES, NAME_CHARS, WORDS_CHARS), (6, 80, 120))
        text = ("cd /a/.claude/worktrees/w1 && sbatch -J j1 s.sbatch && gh pr view 5 && "
                "git push o b1 && mkdir -p d/e && pip install p1 && grep -e g1 f.py > /tmp/t1.log")
        self.assertEqual({k for k, _ in names_of(text)} - set(KINDS), set())

    def test_a_name_is_cut_at_name_chars_by_characters(self):
        (kind, name), = names_of("cat " + "测" * 100 + ".py")
        self.assertEqual((kind, len(name)), ("read", NAME_CHARS))
        self.assertTrue(name.endswith("…"))
        self.assertEqual(of("read", "cat a/" + "e" * 70 + ".py"), ["a/" + "e" * 70 + ".py"])  # 75
        self.assertEqual(of("scratch", "cat $SCRATCH/" + "s" * 200 + ".log"), ["s" * 79 + "…"])
        self.assertEqual(of("pkg", "pip install " + "p" * 200), ["p" * 79 + "…"])
        self.assertEqual(of("pkg", "module load " + "m" * NAME_CHARS), ["m" * NAME_CHARS])

    def test_a_name_that_is_a_variable_or_holds_a_mask_is_no_name(self):
        for text in ("sbatch -J $NAME run.sh", "sbatch -J ${NAME} run.sh", "module load $MOD",
                     "pip install $PKG", "sbatch -J [REDACTED] x.sh", "module load [REDACTED]",
                     "sbatch --job-name=[REDACTED] x.sh"):
            self.assertEqual([n for n in names_of(text) if n[0] in ("job", "pkg") and (
                "$" in n[1] or "REDACTED" in n[1])], [], text)
        self.assertEqual(of("job", "sbatch -J $NAME run.sh"), ["run.sh"])
        self.assertEqual(of("read", "cat dir/[REDACTED].py"), [])

    def test_what_is_kept_is_masked_before_it_is_read(self):
        names, words = tagset("python run.py %s && git commit -m 'Use %s now'" % (TOKEN, TOKEN))
        self.assertNotIn("ghp_", repr((names, words)))
        self.assertIn(("run", "run.py"), names)
        self.assertEqual(words, ("commit", "Use [REDACTED] now", ""))
        # an unmasked token would be a word
        self.assertEqual(of("word", "tool " + TOKEN), ["ghp_" + "a" * 36])
        self.assertEqual(tagset("tool " + TOKEN)[0], ())

    def test_a_masked_text_given_is_the_one_read(self):
        self.assertEqual(tagset("ignored.py", "cat real.py"), ((("read", "real.py"),), None))
        self.assertEqual(tagset("cat raw.py", ""), ((), None))
        self.assertEqual(tagset("git commit -m raw", "git commit -m masked"),
                         ((), ("commit", "masked", "")))
        self.assertEqual(tagset("cat a.py\n"), (names_of("cat a.py\n"), own_words("cat a.py\n")))

    def test_no_text_no_names_and_no_words(self):
        for text in (None, "", "   ", "\n"):
            self.assertEqual(names_of(text), ())
            self.assertIsNone(own_words(text))
        self.assertEqual(tagset("  "), ((), None))

    def test_nothing_it_is_given_raises_and_what_it_gives_is_bounded(self):
        rng = random.Random(7)
        parts = ["git", "commit", "-m", "-F", "-", "--message=", "--title", "gh", "pr", "edit",
                 "create", "'", '"', "<<'EOF'", "EOF", "\n", ";", "|", "&&", ">>", ">", "LOG.md",
                 "notes.md", "$(", ")", "`", "python", "-c", "open(", "'x.py'", ", 'w')", "uv",
                 "run", "sbatch", "-J", "--job-name", "mkdir", "-p", "{a,b}", "cd", "pytest", "::",
                 "rsync", "cp", "-r", "/", "tee", "-a", "echo", "printf", "%s", "grep", "-e", "-v",
                 "sed", "-n", "-i", "perl", "-pi", "pip", "install", "module", "load",
                 "singularity", "exec", "\x00", TOKEN, "测试", ".claude/worktrees/w1",
                 "$SCRATCH/a.log", "a_1", "235", "**T**", "## T", "- x", "}", "{", "\\", "$((",
                 "))", "[[", "]]", "case", "esac", "in", "=", "a=b", "--", "-" * 3, "…", "--wrap",
                 "\"$(cat <<'EOF'\n", "\nEOF\n)\"", "-m\"x", "-am'", "HEAD", "@", "$MSG", "-F-",
                 "<<-EOF", "--head=", "--add-label", "-p", "-t", "--open-mode"]
        for _ in range(4000):
            text = " ".join(rng.choice(parts) for _ in range(rng.randint(1, 16)))
            names, words = tagset(text)
            self.assertLessEqual(len(names), MAX_NAMES, text)
            self.assertEqual(len(set(names)), len(names), text)
            self.assertLessEqual(sum(k == "word" for k, _ in names), MAX_WORDS, text)
            for kind, name in names:
                self.assertIn(kind, KINDS)
                self.assertTrue(name and len(name) <= NAME_CHARS, (text, name))
                self.assertNotIn("ghp_", name)
            if words is not None:
                source, line, ref = words
                self.assertIn(source, SOURCES)
                self.assertTrue(line and len(line) <= WORDS_CHARS, (text, line))
                self.assertTrue(any(c.isalpha() for c in line), (text, line))
                self.assertNotIn("\x00", line)
                self.assertNotIn("ghp_", line)
                self.assertTrue(ref == "" or ref[1:].isdigit(), (text, ref))

    def test_a_name_or_words_past_the_cut_of_a_long_text_are_not_read(self):
        # `phase_rules` reads a text up to its last line before `_MAX_TEXT` characters
        pad = "# " + "x" * 97 + "\n"                      # a comment of 100 characters
        before = "cat first.py\n" + pad * 990 + "cat inside.py\ngit commit -q -m 'Inside'\n"
        past = "cat past.py\ngit commit -q -m 'Past'\n"
        text = before + pad * 20 + past
        self.assertLess(len(before), _MAX_TEXT)
        self.assertGreater(len(before + pad * 20), _MAX_TEXT)
        self.assertEqual(tagset(text), ((("read", "first.py"), ("read", "inside.py")),
                                        ("commit", "Inside", "")))
        # the same lines, with no padding between them, are all read
        self.assertEqual(tagset("cat first.py\n" + past),
                         ((("read", "first.py"), ("read", "past.py")), ("commit", "Past", "")))

    def test_a_long_run_of_braces_is_read_in_linear_time(self):
        braces = "{" * 30000
        cases = (("read", "cat %sa}.py" % braces, ["{" * 79 + "…"]),     # a close, and no comma
                 ("dir", "mkdir -p %sa}" % braces, ["{" * 79 + "…"]),
                 ("cd", "cd %sa}" % braces, ["{" * 79 + "…"]),
                 ("read", "cat %sa,b}.py" % braces, []),                 # a brace expansion
                 ("dir", "mkdir -p x/%sa,b}" % braces, ["x"]))
        for kind, text, expected in cases:
            started = time.perf_counter()
            self.assertEqual(of(kind, text), expected, text[:20])
            self.assertLess(time.perf_counter() - started, 1.0, text[:20])
        # a brace expansion inside another is cut at its first brace, and leaves no brace behind
        self.assertEqual(of("dir", "mkdir -p {{a,b},c}/d"), [])
        self.assertEqual(of("dir", "mkdir -p x/{a,{b,c}}"), ["x"])
        self.assertEqual(of("dir", "mkdir -p x/{{a,b},c}/d"), ["x"])


class RealCallsTest(unittest.TestCase):
    """Whole calls from the two captures: the names a reader of the session would give them."""

    def test_a_python_edit_and_its_test_in_one_call(self):
        t = ("source slurm/env.sh && $PY - <<'PY'\nimport pathlib\n"
             'p = pathlib.Path("tests/test_channel_geometry.py"); s = p.read_text()\n'
             "s = s.replace('a', 'b')\np.write_text(s)\nPY\n"
             "$PY -m pytest tests/test_channel_geometry.py -q 2>&1 | tail -8")
        self.assertEqual(names_of(t), (("write", "tests/test_channel_geometry.py"),
                                       ("test", "tests/test_channel_geometry.py")))

    def test_a_deck_build_names_its_script_and_the_part(self):
        t = ("cd /scratch/marthen/fas-rc-ebpfm/analysis && uv run python decks/build.py "
             "--variant public --only 5_safety --no-pdf 2>&1 | head -20")
        self.assertEqual(names_of(t), (("cd", "analysis"), ("run", "decks/build.py"),
                                       ("word", "5_safety")))

    def test_a_background_rebuild_names_its_log(self):
        t = ("cd /scratch/marthen/fas-rc-ebpfm/analysis && nohup ./update.sh "
             "> data/logs/update_final3.log 2>&1 & sleep 2; echo started")
        self.assertEqual(names_of(t), (("cd", "analysis"), ("run", "update.sh"),
                                       ("scratch", "update_final3")))

    def test_a_scratch_script_written_and_run(self):
        t = ("cd /scratch/marthen/fas-rc-ebpfm/analysis && cat > /tmp/claude-1004/-madsys-x/"
             "1a2b3c4d-5adf/scratchpad/ex18.py <<'EOF'\nimport sys\nsys.path.insert(0, '.')\nEOF\n"
             "uv run python /tmp/claude-1004/-madsys-x/1a2b3c4d-5adf/scratchpad/ex18.py")
        self.assertEqual(names_of(t), (("cd", "analysis"), ("write", "scratchpad/ex18.py"),
                                       ("scratch", "ex18"), ("run", "scratchpad/ex18.py")))

    def test_a_pr_check_on_a_worktree(self):
        t = ("cd /n/home03/kmoe/kempner/KempnerForge/.claude/worktrees/seek-frame-decoding"
             " && gh pr checks 235 --repo KempnerInstitute/KempnerForge --watch")
        self.assertEqual(names_of(t), (("cd", "seek-frame-decoding"),
                                       ("worktree", "seek-frame-decoding"), ("pr", "#235")))

    def test_a_poll_of_a_logged_job_names_the_log_and_what_it_looks_for(self):
        t = ('P=/n/netscratch/kempner_dev/Lab/kmoe/phase2/pr8-qwen3-converter; '
             'grep -cE "PASSED|FAILED" $P/logs/pr8_51502014/smoke.log; squeue -j 51502014 -h')
        self.assertEqual(names_of(t), (("grep", "PASSED|FAILED"), ("scratch", "smoke")))

    def test_a_commit_in_a_burst_of_git_names_its_branch_and_words(self):
        t = ("cd /n/lab/Lab/qfox/work && git add -A sbh && git commit -q -F - <<'EOF'\n"
             "feat(report): function curves in pT\n\nBody.\nEOF\ngit push origin HEAD:lep_calib")
        self.assertEqual(tagset(t), ((("cd", "work"), ("branch", "lep_calib")),
                                     ("commit", "feat(report): function curves in pT", "")))


class OwnWordsTest(unittest.TestCase):
    def test_a_commit_subject_from_m_and_from_a_heredoc(self):
        self.assertEqual(own_words('git add -A && git commit -q -m "Fix the loader"'),
                         ("commit", "Fix the loader", ""))
        t = ("git commit -q --no-gpg-sign -F - <<'EOF'\nImpose reflection symmetry on the channel; "
             "retire the last agent-name prefixes\n\nAn achiral NMR experiment...\nEOF")
        self.assertEqual(own_words(t), ("commit", "Impose reflection symmetry on the channel; "
                                                  "retire the last agent-name prefixes", ""))

    def test_a_pr_title(self):
        self.assertEqual(own_words('gh pr create --head lep_calib --title "feat(report): function '
                                   'curves" --body-file b.md'),
                         ("pr", "feat(report): function curves", ""))
        self.assertEqual(own_words('gh pr edit 235 -t "Seek-based frame decoding"'),
                         ("pr", "Seek-based frame decoding", "#235"))

    def test_a_log_entry_title(self):
        t = ("cat >> analysis/CHAT_LOG.md <<'EOF'\n\n"
             "**Fold-back cutoff set at 2 per 1,000 (user)**\nWe measured...\nEOF")
        self.assertEqual(own_words(t), ("log", "Fold-back cutoff set at 2 per 1,000 (user)", ""))
        self.assertEqual(own_words("cat >> REQUIREMENTS.md <<'EOF'\n"
                                   "## 08:53 Yiyang awake; questions\nEOF")[1],
                         "08:53 Yiyang awake; questions")

    def test_no_words_where_none_are_written(self):
        self.assertIsNone(own_words("cat > docs/paper.md <<'EOF'\n# Title\nEOF"))
        self.assertIsNone(own_words("git commit -F msg.txt"))
        self.assertIsNone(own_words("ls -la"))

    def test_masked_first_and_cut_by_characters(self):
        ts = tagset('git commit -m "Use token ghp_' + "a" * 36 + ' for the push"')
        self.assertNotIn("ghp_" + "a" * 36, ts[1][1])
        ts = tagset('git commit -m "%s"' % ("测试" * 100))
        self.assertEqual(len(ts[1][1]), WORDS_CHARS)
        self.assertTrue(ts[1][1].endswith("…"))

    def test_a_message_flag_in_any_form(self):
        for text in ('git commit -m "Fix it"', 'git commit -am "Fix it"',
                     'git commit -qm "Fix it"', 'git commit --message "Fix it"',
                     'git commit --message="Fix it"', 'git -C /r commit -m "Fix it" --no-verify',
                     'git commit -q -a -m "Fix it" -- f.py', "git commit -m 'Fix it'",
                     'git add a && git commit -q -m "Fix it" && git push',
                     'git commit -m"Fix it"', 'git commit -am"Fix it"', "git commit -qm'Fix it'",
                     'git commit -a -m"Fix it" --no-verify', 'git commit -sm"Fix it"',
                     'git commit -m"Fix it" && git push'):
            self.assertEqual(own_words(text), ("commit", "Fix it", ""), text)
        self.assertEqual(own_words("git commit -mFix")[1], "Fix")           # a word, attached
        self.assertEqual(own_words("git commit -amFix")[1], "Fix")
        self.assertEqual(own_words("git commit -m'A' -m 'B'")[1], "A")
        self.assertIsNone(own_words("git commit -a -q"))                  # no `m` is no message
        # an option that takes a value, written with it attached, is no flag before an `m`
        self.assertIsNone(own_words("git commit -Cmaster"))
        self.assertIsNone(own_words("git commit -tmsg.txt"))
        self.assertEqual(own_words("git commit -Cmaster -m 'A'")[1], "A")

    def test_the_first_message_and_its_first_line(self):
        self.assertEqual(own_words('git commit -m "Subject\n\nBody" -m "Second"')[1], "Subject")
        self.assertEqual(own_words('git commit -m "First" -m "Second"')[1], "First")
        self.assertEqual(own_words('git commit -m "\n\nLate subject\nmore"')[1], "Late subject")
        self.assertEqual(own_words("git commit -m 'A' && git commit -m 'B'")[1], "A")
        self.assertEqual(own_words("git commit -qm 'Fix it\n\nCo-Authored-By: X <x@y>'")[1],
                         "Fix it")

    def test_a_message_from_stdin_comes_after_a_message_given(self):
        for flags in ("-F -", "--file=-", "--file -", "-q -F -", "-F-", "-qF -", "-qF-"):
            t = "git commit %s <<'EOF'\n\nSubject here\n\nbody\nEOF" % flags
            self.assertEqual(own_words(t), ("commit", "Subject here", ""), flags)
        self.assertIsNone(own_words("git commit -Fmsg.txt <<'EOF'\nignored\nEOF"))   # a file
        self.assertEqual(own_words("git commit -m 'M' -F - <<EOF\nH\nEOF")[1], "M")
        self.assertEqual(own_words("git commit -F - -m 'M' <<EOF\nH\nEOF")[1], "M")
        self.assertEqual(own_words("git commit -F - <<EOF\nH\nEOF && git commit -m 'M'")[1], "H")
        # a heredoc that is not given to git as the message is no words
        self.assertIsNone(own_words("git commit --amend --no-edit <<'EOF'\nignored\nEOF"))

    def test_a_commit_that_gives_no_words(self):
        for text in ("git commit --amend --no-edit", "git commit -q", "git commit -m",
                     'git commit -m ""', 'git commit -m "$(cat msg.txt)"',
                     "git commit -F - < msg.txt", "git status", "git log -m",
                     "git commit -F msg.txt", "git commit --file msg.txt", "git commit -m '---'"):
            self.assertIsNone(own_words(text), text)

    def test_a_heredoc_inside_a_longer_value_is_unread_and_gives_no_garbage(self):
        # `_commands` loses a heredoc inside a `$( )`: it leaves a NUL, which is never quoted. Only
        # the whole value of `-m` is read (`test_the_message_of_a_commit_given_by_a_heredoc`).
        self.assertIsNone(own_words("git commit -m \"Fix: $(cat <<'EOF'\nbody\nEOF\n)\""))
        self.assertIsNone(own_words("gh pr create --title \"Fix: $(cat <<'EOF'\nbody\nEOF\n)\""))
        self.assertIsNone(own_words("echo \"## T $(cat <<'EOF'\nbody\nEOF\n)\" >> LOG.md"))
        self.assertIsNone(own_words("git commit -m \"$(cat <<'EOF'\nbody\nEOF\n) and $(date)\""))
        self.assertIsNone(own_words("git commit -F - -m \"x $(cat <<'EOF'\nbody\nEOF\n)\""))
        # what follows the lost heredoc, up to the `)`, is the rest of the `$( )`, not words
        self.assertIsNone(own_words("echo \"## T $(cat <<'EOF'\nbody\nEOF\n) tail\" >> LOG.md"))
        self.assertIsNone(own_words("gh pr create --title \"$(cat <<'EOF'\nbody\nEOF\n) tail\""))

    def test_the_message_of_a_commit_given_by_a_heredoc(self):
        # Claude Code's own form: the heredoc's first non-empty line is the subject
        t = ("git commit -m \"$(cat <<'EOF'\nFix the loader\n\nBody.\n\n"
             "Co-Authored-By: X <x@y>\nEOF\n)\"")
        self.assertEqual(own_words(t), ("commit", "Fix the loader", ""))
        self.assertEqual(tagset(t)[1], ("commit", "Fix the loader", ""))
        self.assertEqual(names_of(t), ())                       # and the heredoc names nothing
        for text in (
                "git commit --message \"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit --message=\"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -am \"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -qm \"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -m\"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -m \"$(cat <<EOF\nFix the loader\nEOF\n)\"",       # unquoted delimiter
                "git commit -m \"$(cat <<\"EOF\"\nFix the loader\nEOF\n)\"",     # double-quoted
                "git commit -m \"$(cat <<\\EOF\nFix the loader\nEOF\n)\"",       # escaped
                "git commit -m \"$(cat <<-'EOF'\n\tFix the loader\n\tEOF\n)\"",  # `<<-`, tabs
                "git commit -m \"$(cat <<-EOF\n\t\tFix the loader\n\tEOF\n)\"",
                "git commit -m \"$(cat <<'MSG'\nFix the loader\nMSG\n)\"",        # any delimiter
                "git commit -m \"$(cat <<'EOF'\n\n\n  Fix the loader  \nEOF\n)\"",   # blank lines
                "git commit -m \"$(cat <<'EOF' \nFix the loader\nEOF\n)\"",
                "git commit -m \"$(cat   <<  'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -m \"$(\ncat <<'EOF'\nFix the loader\nEOF\n)\"",
                "cd /r && git add -A && git commit -q -m \"$(cat <<'EOF'\nFix the loader\n"
                "EOF\n)\" && git push",
                "git commit -m \"$(cat <<'EOF'\nFix the loader\nEOF\n)\" --no-verify",
                "git commit --no-verify -m \"$(cat <<'EOF'\nFix the loader\nEOF\n)\"",
                "git commit -m \"$(cat <<'EOF'\nFix the loader",                  # cut in the body
                "git commit -m \"$(cat <<'EOF'\nFix the loader\n\nBody that was cut\nEO",
                "git commit -m \"$(cat <<'EOF'\nFix the loader\nEOF"):          # cut at the end
            self.assertEqual(own_words(text), ("commit", "Fix the loader", ""), text)

    def test_a_heredoc_ends_at_its_own_delimiter_alone_on_a_line(self):
        pr = " && gh pr create --title 'Opened'"          # read only if the heredoc ended before it
        for opener, closer in (("<<'EOF'", "EOF"), ("<<'MSG'", "MSG"), ("<<-EOF", "\tEOF"),
                               ("<<-\"END_OF_TEXT\"", "\tEND_OF_TEXT"), ("<<\\X", "X")):
            indent = "\t" if closer.startswith("\t") else ""
            text = "git commit -m \"$(cat %s\n%sSubject\n%s\n)\"%s" % (opener, indent, closer, pr)
            self.assertEqual(own_words(text), ("pr", "Opened", ""), text)
        # a line that only begins with the delimiter is a line of the message
        self.assertEqual(own_words("git commit -m \"$(cat <<'EOF'\nEOFS and more\nEOF\n)\""),
                         ("commit", "EOFS and more", ""))
        self.assertEqual(own_words("git commit -m \"$(cat <<'EOF'\nEOF is the marker\nEOF\n)\""),
                         ("commit", "EOF is the marker", ""))
        # and a delimiter is not always EOF: this one never ends the heredoc below
        self.assertEqual(own_words("git commit -m \"$(cat <<'MSG'\nEOF\nSubject\nMSG\n)\"" + pr),
                         ("pr", "Opened", ""))

    def test_a_form_inside_the_heredoc_of_a_message_is_part_of_that_message(self):
        # a commit that tells of the form: its message is read once, from the outer heredoc
        outer = "git commit -m \"$(cat <<'EOF'\nSubject\n\n%s\nEOF\n)\""
        for inside in ("git commit -m \"$(cat <<'X'\ninner\nX\n)\"",      # a whole form
                       "write -m \"$(cat <<'X'"):                          # one that never closes
            self.assertEqual(own_words(outer % inside), ("commit", "Subject", ""), inside)

    def test_the_first_message_wins_whatever_form_it_takes(self):
        heredoc = "\"$(cat <<'EOF'\n%s\nEOF\n)\""
        self.assertEqual(own_words("git commit -m %s -m 'Second'" % (heredoc % "One"))[1], "One")
        self.assertEqual(own_words("git commit -m 'Title' -m %s" % (heredoc % "Body"))[1], "Title")
        self.assertEqual(own_words("git commit -m 'A' && git commit -m %s" % (heredoc % "B"))[1],
                         "A")
        self.assertEqual(own_words("git commit -m %s && git commit -m %s"
                                   % (heredoc % "One", heredoc % "Two"))[1], "One")
        self.assertEqual(own_words("git commit -m \"$MSG\" && git commit -m %s"
                                   % (heredoc % "Two"))[1], "Two")
        # an empty heredoc is no message, and the next commit may give one
        self.assertIsNone(own_words("git commit -m \"$(cat <<'EOF'\nEOF\n)\""))
        self.assertEqual(own_words("git commit -m %s && git commit -m 'Next'"
                                   % (heredoc % ""))[1], "Next")
        # what is not `git commit` is not read as a message, and a PR body is not a title
        self.assertIsNone(own_words("git tag -a v1 -m \"$(cat <<'EOF'\nTag text\nEOF\n)\""))
        self.assertIsNone(own_words("git commit -tm \"$(cat <<'EOF'\nTag text\nEOF\n)\""))  # `-t`
        self.assertEqual(own_words("gh pr create --title 'Opened' --body %s" % (heredoc % "Body")),
                         ("pr", "Opened", ""))
        self.assertIsNone(own_words("gh pr create --body %s" % (heredoc % "Body")))
        # the words are masked, and cut, as any other
        self.assertEqual(tagset("git commit -m %s" % (heredoc % ("Use %s now" % TOKEN)))[1],
                         ("commit", "Use [REDACTED] now", ""))
        self.assertEqual(len(own_words("git commit -m %s" % (heredoc % ("é" * 300)))[1]),
                         WORDS_CHARS)

    def test_a_line_with_no_letter_outside_its_variables_is_no_words(self):
        for text in ('git commit -m "$MSG"', 'git commit -m "${MSG}"', 'git commit -m "$A $B"',
                     'git commit -m "$(date +%s)"', 'git commit -m "$(basename $(pwd))"',
                     'git commit -m "${A:-$B}"', 'git commit -m "$1"', 'git commit -m "12345"',
                     'git commit -m "$(echo $(date) now)"', 'git commit -m "$(echo $((1 + 2)) x)"',
                     "git commit -m '$MSG'", 'gh pr create --title "$TITLE"',
                     'gh pr edit 3 --title "${TITLE}"', 'echo "$ENTRY" >> LOG.md',
                     'echo "${ENTRY}" >> LOG.md', 'echo "$(date) $ENTRY" >> LOG.md',
                     'echo "- $(date -Is) $ENTRY" >> status.md', 'echo "08:53" >> LOG.md',
                     "cat >> LOG.md <<EOF\n$ENTRY\nEOF", "cat >> LOG.md <<EOF\n## $ENTRY\nEOF",
                     "git commit -F - <<EOF\n$MSG\nEOF"):
            self.assertIsNone(own_words(text), text)
        # the first line with a letter outside them is the one, as written
        self.assertEqual(own_words("cat >> LOG.md <<EOF\n$ENTRY\nSecond line\nEOF")[1],
                         "Second line")
        for text, line in (('git commit -m "Fix $MODULE loader"', "Fix $MODULE loader"),
                           ('git commit -m "$PREFIX: fix"', "$PREFIX: fix"),
                           ('git commit -m "${A}x"', "${A}x"),
                           ('gh pr create --title "$A and $B"', "$A and $B"),
                           ('echo "## $ENTRY done" >> LOG.md', "$ENTRY done"),
                           ('git commit -m "$(date) fix"', "$(date) fix"),
                           ('git commit -m "测试 $X"', "测试 $X")):
            self.assertEqual(own_words(text)[1], line, text)

    def test_a_quote_that_never_closes_runs_to_the_end_of_the_line(self):
        # a record the collector cut still starts with the agent's words
        self.assertEqual(own_words('git commit -m "Fix the loader and the'),
                         ("commit", "Fix the loader and the", ""))
        self.assertEqual(own_words("git add -A && git commit -qm 'Fix the loader and the"),
                         ("commit", "Fix the loader and the", ""))
        self.assertEqual(own_words('git commit --message="Fix the loader and the')[1],
                         "Fix the loader and the")
        self.assertEqual(own_words('git commit -m"Fix the loader and the')[1],
                         "Fix the loader and the")
        self.assertEqual(own_words('git commit -am "Fix')[1], "Fix")
        self.assertEqual(own_words('git commit -m "Fix the loader\n\nBody that was cut')[1],
                         "Fix the loader")
        self.assertEqual(own_words('gh pr create --title "Seek-based frame'),
                         ("pr", "Seek-based frame", ""))
        self.assertEqual(own_words("gh pr edit 235 --title 'Seek-based"),
                         ("pr", "Seek-based", "#235"))
        self.assertEqual(own_words('gh pr create --title="Seek-based frame')[1], "Seek-based frame")
        # a quote that closes, and a value that has one inside, are as they are
        self.assertEqual(own_words('git commit -m "\\"Fix\\" the loader"')[1], '"Fix" the loader')
        self.assertEqual(own_words("git commit -m '\"Fix\" it'")[1], '"Fix" it')
        self.assertEqual(own_words('git commit -m "It\'s fixed" && git push')[1], "It's fixed")
        self.assertEqual(own_words("git commit -m 'Fix it' -m \"and this")[1], "Fix it")
        # a one-word value that has quotes of its own, and a sentence that begins with one
        self.assertEqual(own_words("git commit -m '\"WIP\"'")[1], '"WIP"')
        self.assertEqual(own_words("git commit -m \"'tis the season\" --no-verify")[1],
                         "'tis the season")
        # a cut inside a variable or a substitution is no words
        self.assertIsNone(own_words('git commit -m "$MSG'))
        self.assertIsNone(own_words('git commit -m "$(cat msg.txt'))

    def test_a_pr_title_in_any_form(self):
        self.assertEqual(own_words('gh pr create --title="A title" --body x'),
                         ("pr", "A title", ""))
        self.assertEqual(own_words('gh pr edit --repo o/r 8 --title "T"'), ("pr", "T", "#8"))
        self.assertEqual(own_words('gh pr create -R o/r -t "T1\n\nbody"'), ("pr", "T1", ""))
        self.assertEqual(own_words("gh pr edit --title T2"), ("pr", "T2", ""))
        self.assertEqual(own_words("gh pr create --title T3 7"), ("pr", "T3", ""))   # only `edit`
        self.assertEqual(own_words("gh pr edit --add-label bug 235 --title T"), ("pr", "T", "#235"))
        self.assertEqual(own_words("gh pr edit --add-reviewer a --remove-label b 9 -t T"),
                         ("pr", "T", "#9"))
        self.assertEqual(own_words("gh pr edit -m v1 -B main 12 --title T"), ("pr", "T", "#12"))
        for text in ("gh pr create --body-file b.md", "gh pr view 8", "gh pr merge 8 --title t",
                     'gh pr create --title "$(cat t.txt)"', "gh pr create --title",
                     "gh issue create -t T"):
            self.assertIsNone(own_words(text), text)

    def test_a_log_is_a_markdown_file_whose_name_says_so(self):
        for name in ("CHAT_LOG.md", "LOG.md", "docs/kf-phase2-pr-plan.md", "AGENT_COORDINATION.md",
                     "CHANGELOG.md", "session_notes.md", "NOTE.md", "status.md", "JOURNAL.md",
                     "diary.md", "PROGRESS.md", "HANDOFF.md", "REQUIREMENTS.md", "TF_TODO.md"):
            self.assertEqual(own_words("cat >> %s <<'EOF'\n## Entry\nEOF" % name),
                             ("log", "Entry", ""), name)
        for name in ("README.md", "docs/paper.md", "GUIDE.md", "LOG.txt", "notes.txt",
                     "log.md.bak", "log", "src/log.py"):
            self.assertIsNone(own_words("cat >> %s <<'EOF'\n## Entry\nEOF" % name), name)

    def test_a_log_files_word_is_a_whole_part_of_its_stem(self):
        # The stem is split on `_`, `-`, and `.`, and on camel case and digits. Until 2026-11-03 a
        # word was matched anywhere in the name, and these were logs.
        for name in ("methodology.md", "explanation.md", "technology.md", "biology.md",
                     "dialog.md", "catalog.md", "logging.md", "planning.md", "worklog.md",
                     "devnotes.md", "ToDo.md"):
            self.assertIsNone(own_words("cat >> %s <<'EOF'\n## Entry\nEOF" % name), name)
        for name in ("work_log.md", "WorkLog.md", "progressNotes.md", "notes.2026-10-09.md",
                     "HTTPLog.md", "v2-plan.md", "plan3.md", "Status.MD", "agent-coordination.md"):
            self.assertEqual(own_words("cat >> %s <<'EOF'\n## Entry\nEOF" % name),
                             ("log", "Entry", ""), name)

    def test_a_log_is_written_by_a_redirect_or_by_tee_from_a_heredoc_or_a_string(self):
        for text, line in (
                ('echo "- 08:53 Entry one" >> docs/LOG.md', "08:53 Entry one"),
                ("echo -e '\\n## Entry one\\nmore' >> PLAN.md", "Entry one"),
                ("printf '\\n## %s\\n' 'Entry one' >> PLAN.md", "Entry one"),
                ("printf '%s\\n' '**Entry one**' >> notes.md", "Entry one"),
                ("printf 'Entry one 100%%\\n' >> notes.md", "Entry one 100%"),
                ("echo text | tee -a status.md", "text"),
                ("cat <<'EOF' | tee -a HANDOFF.md\n## Entry one\nEOF", "Entry one"),
                ("tee notes.md <<'EOF'\nEntry one\nEOF", "Entry one"),
                ("cat > TODO.md <<'EOF'\n- [ ] first\nEOF", "[ ] first"),
                ("cat >| LOG.md <<EOF\nEntry one\nEOF", "Entry one"),
                ("cd /x && cat >> LOG.md <<EOF\nEntry one\nEOF\nls", "Entry one")):
            self.assertEqual(own_words(text), ("log", line, ""), text)

    def test_the_title_of_a_log_entry_is_its_heading_or_bold_line_else_its_first_line(self):
        def title(body):
            return own_words("cat >> LOG.md <<'EOF'\n%s\nEOF" % body)[1]

        self.assertEqual(title("plain line\n## Heading\nmore"), "Heading")
        self.assertEqual(title("plain line\n**Bold one** and more\nmore"), "Bold one and more")
        self.assertEqual(title("- **Control artifact results** (jobs 1, 2):\n  - detail"),
                         "Control artifact results (jobs 1, 2):")
        self.assertEqual(title("---\n\n- plain bullet\n- second"), "plain bullet")
        self.assertEqual(title("* star bullet"), "star bullet")
        self.assertEqual(title("> **Note (00:35):** the lines\n> more"), "Note (00:35): the lines")
        self.assertEqual(title("### 2026-10-09 -- the rename\ntext"), "2026-10-09 -- the rename")
        self.assertEqual(title("a **bold** word inside\nmore"), "a bold word inside")
        self.assertEqual(title("#hashtag is no heading"), "#hashtag is no heading")
        self.assertEqual(title("```\ncode line\n```"), "code line")
        self.assertEqual(title("two   spaces\tand a tab"), "two spaces and a tab")
        self.assertEqual(title("fix issue # 5 now"), "fix issue # 5 now")
        self.assertEqual(title("plain line\n$(date +%H:%M) **Stage one**"), "Stage one")
        self.assertEqual(own_words("echo -e 'plain entry' >> LOG.md")[1], "plain entry")
        self.assertEqual(own_words("echo -n 'plain entry' >> LOG.md")[1], "plain entry")
        # a date command the shell fills in is not the agent's text, but only at the start
        self.assertEqual(own_words('echo "- $(date -Is) smoke 1 failed" >> status.md')[1],
                         "smoke 1 failed")
        self.assertEqual(own_words("echo '$(date +%H:%M) stage 1 done' >> LOG.md")[1],
                         "stage 1 done")
        self.assertEqual(own_words('echo "ran $(date) fine" >> LOG.md')[1], "ran $(date) fine")
        self.assertEqual(own_words("echo '## $(date +%H:%M) Title' >> LOG.md")[1], "Title")

    def test_a_write_to_a_log_that_has_no_text_gives_no_words(self):
        for text in ("python x.py >> LOG.md", "cat a.txt >> LOG.md", "echo >> LOG.md",
                     "cat >> LOG.md <<'EOF'\nEOF", "cat >> LOG.md <<'EOF'\n---\n\nEOF",
                     "python - >> LOG.md <<'EOF'\nprint(1)\nEOF", "tail -n 5 LOG.md",
                     "cat LOG.md | tee -a notes.md", "ls | tee -a LOG.md",
                     "echo '## Not mine'; cat a.txt >> LOG.md"):
            self.assertIsNone(own_words(text), text)

    def test_a_call_holds_one_in_the_order_of_the_sources(self):
        log = "cat >> LOG.md <<'EOF'\n## Logged\nEOF"
        commit = "git commit -m 'Committed'"
        pr = "gh pr create --title 'Opened'"
        self.assertEqual(own_words("%s\n%s\n%s" % (log, commit, pr))[:2], ("pr", "Opened"))
        self.assertEqual(own_words("%s\n%s" % (log, commit))[:2], ("commit", "Committed"))
        self.assertEqual(own_words("%s\n%s" % (commit, log))[:2], ("commit", "Committed"))
        self.assertEqual(own_words(log)[:2], ("log", "Logged"))
        self.assertEqual(own_words("%s; %s" % (pr, "gh pr edit 3 -t 'Retitled'")),
                         ("pr", "Opened", ""))
        self.assertEqual(own_words("echo '## One' >> LOG.md; echo '## Two' >> LOG.md")[1], "One")

    def test_the_line_is_cut_at_words_chars_with_an_ellipsis(self):
        exactly = "a" * WORDS_CHARS
        self.assertEqual(own_words("git commit -m '%s'" % exactly)[1], exactly)
        self.assertEqual(own_words("git commit -m '%s'" % (exactly + "b"))[1],
                         "a" * (WORDS_CHARS - 1) + "…")
        self.assertEqual(len(own_words("echo '## %s' >> LOG.md" % ("é" * 300))[1]), WORDS_CHARS)

    def test_the_words_come_from_masked_text_for_each_source(self):
        for text in ("git commit -m 'k %s'" % TOKEN, "gh pr create --title 'k %s'" % TOKEN,
                     "echo '## k %s' >> LOG.md" % TOKEN,
                     "git commit -F - <<'EOF'\nk %s\nEOF" % TOKEN):
            words = tagset(text)[1]
            self.assertEqual(words[1], "k [REDACTED]", text)


if __name__ == "__main__":
    unittest.main()
