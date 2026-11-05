"""What a tool call was, named from its command text (reducers/phase_rules.py).

The classifier is pure text in, one of CATS out. These tests pin the rules the
module states: the first-of order when a call holds several commands, what a
wrapper or a setup command does and does not decide, what looks like a command and
is not, a program no table names (it is run), a program held in a variable (read from
its assignment, its name, or its arguments), the shapes of Claude's and Codex's shell
argv, and that an unreadable command is `unknown` and never raises.

Then which of an agent's processes are tool calls and which are its own housekeeping: whether a
process is the agent's direct child (`parent_is_agent`, through the schema-7 and schema-8 shapes of
a record), what a direct child is (`classify_direct`), and the command a call ran (`call_text`).

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_rules.py' -v
"""
import random
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.normalize import Normalizer  # noqa: E402
from rc_dashboard.procindex import ProcIndex  # noqa: E402
from rc_dashboard.reducers.phase_rules import (CATS, PHASE_AGENTS, call_text, category_of,  # noqa: E402
                                               category_of_argv, classify_direct, codex_chore_of,
                                               is_fs_helper, is_sandbox_probe, login_helper,
                                               norm_hash, parent_is_agent, program_name, shell_text,
                                               split_simple, waits_on)
from rc_dashboard.roles import ROLES  # noqa: E402
from test_sessions import cfg, claude_session, rec  # noqa: E402

CASES = [  # text -> category
    ("ls -la", "explore"),
    ("sed -n '1,80p' src/a.py", "explore"),
    ("sed -i 's/a/b/' src/a.py", "edit"),
    ("cat > notes.md <<'EOF'\nhello\nEOF", "edit"),
    ("cd /proj && python -m pytest tests -x 2>&1 | tail -20", "test"),
    ("uv run pytest -q", "test"),
    ("ruff check .", "test"),
    ("bash scripts/run_tests.sh", "test"),
    ("make && ./bench", "run"),
    ("pip install -e .", "build"),
    ("bash build.sh", "build"),
    ("curl -sSLo data.tgz https://x.org/y.tgz", "build"),
    ("curl -s http://localhost:8080/api/live | jq .", "explore"),
    ("sbatch run.sh", "slurm"),
    ("squeue -u $USER", "slurm"),
    ("git add -A && git commit -m wip", "git"),
    ("git diff HEAD~1", "explore"),
    ("until [ -f out.json ]; do sleep 15; done", "wait"),
    ("sleep 600; tail -5 log.txt", "wait"),
    ("tail -f log.txt", "wait"),
    ("python train.py --epochs 3", "run"),
    ("python -c \"import torch; print(torch.__version__)\"", "run"),
    ("python - <<'EOF'\nopen('a.txt','w').write('x')\nEOF", "edit"),
    ("nvidia-smi", "explore"),
    ("cd /proj", "explore"),          # setup only: my call, it never names a call with anything else in it
    ("module load cuda && make -j8", "build"),
    ("", "unknown"),
    ("echo \"unterminated", "explore"),  # unbalanced quotes must not raise
    ("lmp -in in.melt", "run"),         # a program no table names is run
]

# More shapes, grouped by the rule each one pins. text -> category.
WRAPPERS_AND_SETUP = [
    ("FOO=1 BAR=2 pytest -q", "test"),
    ("timeout 600 python train.py", "run"),
    ("timeout -s KILL 10m python train.py", "run"),
    ("nohup python train.py > log.txt 2>&1 &", "run"),
    ("env -i HOME=/x python a.py", "run"),
    ("nice -n 10 make -j4", "build"),
    ("stdbuf -oL python x.py | tee run.log", "run"),
    ("time make", "build"),
    ("exec python x.py", "run"),
    ("setsid python x.py &", "run"),
    ("find . -name '*.pyc' | xargs rm -f", "edit"),
    ("xargs -n 1 -P 4 python x.py < jobs.txt", "run"),
    ("command -v python", "explore"),
    ("env", "explore"),
    ("if [ -f x ]; then echo yes; fi", "explore"),
    ("for f in *.sh; do bash $f; done", "run"),
    ("! grep -q x f", "explore"),
    ("source venv/bin/activate && python x.py", "run"),
    ("conda activate env && pytest", "test"),
    ("export A=1", "explore"),
    ("module load cuda", "explore"),
    ("ml purge", "explore"),
    ("set -e; cd /x", "explore"),
    (". env.sh", "explore"),
    ("A=1", "explore"),
    ("setopt NO_EXTENDED_GLOB", "explore"),
    ("eval \"$(conda shell.bash hook)\"", "explore"),
    ("eval \"$(conda shell.bash hook)\" && conda activate e && python x.py", "run"),
    ("cd /x && module load gcc && ./configure && make", "build"),
    ("for i in 1 2; do continue; done", "explore"),
    ("while true; do [ $i -ge 3 ] && break; done", "explore"),
    ("[ $(( 2 + 1 )) -ge 3 ] && sbatch a.sh", "slurm"),
    ("compgen -e", "explore"), ("shift", "explore"), ("let i++", "explore"), ("history", "explore"),
    ("\\builtin unalias -- x", "explore"),
    ("time (ls | wc -l)", "explore"), ("exec > log 2>&1", "explore"), ("nohup", "explore"),
    ("bash -c 'cd /x && pytest -q'", "test"),
    ("srun --pty bash -lc 'python x.py'", "slurm"),
    ("nohup bash -c 'until [ -f x ]; do sleep 5; done' &", "wait"),
    # sudo, ionice, taskset, and numactl run the command after them, past their valued options
    # (final review Minor: rtx6000a runs `sudo -n bash -c`)
    ("sudo -n bash -c \"pytest -q\"", "test"),
    ("sudo -u alice python x.py", "run"),
    ("sudo -u alice -g grp -- make -j8", "build"),
    ("sudo VAR=1 pytest -q", "test"),
    ("taskset -c 0-3 python train.py", "run"),
    ("taskset 0x3 pytest -x", "test"),
    ("ionice -c3 -n7 tar czf a.tgz d", "explore"),                     # as `tar czf a.tgz d` reads
    ("ionice -c 2 -n 0 make", "build"),
    ("numactl --cpunodebind=0 -m 0 ./a.out", "run"),
    ("numactl -N 0 --membind 0 pytest", "test"),
    # and their near misses: with these nothing after them runs
    ("sudo -l", "explore"),                     # lists what may be run
    ("sudo -e /etc/hosts", "edit"),             # sudoedit: edits the file, not a program run
    ("ionice -p 1234", "explore"),              # a running process's class
    ("taskset -p 1234", "explore"), ("taskset -cp 0-3 1234", "explore"),
    ("numactl --hardware", "explore"),          # the nodes, listed
]

LOOPS = [
    ("while true; do sbatch x.sh; sleep 60; done", "wait"),
    ("until [ -z \"$(squeue -h -u $USER)\" ]; do sleep 30; done", "wait"),
    ("while squeue -u $USER | grep -q x; do sleep 60; done", "wait"),
    ("until pytest -x; do sleep 5; done", "wait"),
    ("for i in 1 2 3; do sbatch j.sh; sleep 1; done", "slurm"),   # a pause between submits is not a wait
    ("while read l; do echo $l; done < f", "explore"),
    ("while :; do date; done", "explore"),
    ("watch -n 30 squeue -u $USER", "wait"),
    ("watch nvidia-smi", "wait"),
]

SUBSTITUTIONS = [
    ("JOB=$(sbatch --parsable run.sh); echo $JOB", "slurm"),
    ("echo \"queue: $(squeue -u $USER | wc -l)\"", "slurm"),
    ("[ \"$(squeue -h -u $USER | wc -l)\" -gt 0 ] && echo busy", "slurm"),
    ("x=$(python -c \"print(1)\")", "run"),
    ("echo $(date)", "explore"),
    ("echo $(date) > out.txt", "edit"),                         # the redirect after it is echo's own
    ("tail $(ls -t logs/*.log | head -1) -f", "wait"),         # and so is a flag
]

PRIORITY = [   # a call takes the first of: test, slurm, run, build, edit, git, wait, explore
    ("cd x && make && pytest -x | tail", "test"),
    ("sbatch a.sh && python b.py", "slurm"),
    ("python train.py && git commit -am x", "run"),
    ("make && cp a b", "build"),
    ("cp a b && git add b", "edit"),
    ("git add . ; sleep 5", "git"),
    ("sleep 5; ls", "wait"),
    ("ls; pwd", "explore"),
    ("pytest; sbatch a.sh", "test"),
]

SLEEPS_FIRST = [   # R58: a call that begins by sleeping a minute or more waits, ahead of the order
    ("sleep 590; W=$(cat f); echo $W > out", "wait"),
    ("cd d && sleep 600 && tail log", "wait"),
    ("W=/n/x; sleep 120; cat >> $W/notes.md <<'EOF'\nok\nEOF", "wait"),
    ("sleep 2m && sbatch x.sh", "wait"),
    ("sleep 1m", "wait"), ("sleep 1m 30s; ls", "wait"), ("sleep 0.5h; git push", "wait"),
    ("sleep infinity", "wait"),
    # the cost of the rule, pinned: a training run after a long sleep reads as the wait
    ("sleep 600; python train.py", "wait"),
    ("sleep 30; pytest", "test"),                       # under a minute: the order decides
    ("sleep 59.9; sbatch x.sh", "slurm"),
    ("sleep $T; python train.py", "run"),               # no length to read
    ("python train.py; sleep 600", "run"),              # it does not begin with the sleep
    ("source venv/bin/activate && sleep 600 && pytest", "test"),   # only cd and assignments go
    ("for i in 1 2; do sleep 600; sbatch x.sh; done", "slurm"),    # a `for` loop is no wait
    # R61: a sleep sent to the background is no first step; an assignment's substitution is passed
    ("sleep 600 & python train.py", "run"),
    ("( sleep 3600 && kill -9 $$ ) & ./run_tests.sh", "test"),
    ("{ sleep 3600; kill $$; } & pytest -q", "test"),
    ("nohup sleep 600 &\npython train.py", "run"),
    ("( sleep 600 ) > /tmp/log 2>&1 & make", "build"),
    ("X=$(date +%s); sleep 600; python x.py", "wait"),
    ("N=$(squeue -h -u $USER | wc -l); sleep 300; python x.py", "wait"),
    ("echo $(sleep 600) && pytest", "test"),                       # it begins with `echo`
    # a job sent to the background before a pause is still the call's first step (boslogin06's
    # 2250182 at 16:10:31 on 2026-10-28)
    ("nohup srun --jobid=5 -n1 python x.py > log 2>&1 & sleep 90; tail -5 log", "slurm"),
    ("python serve.py & sleep 600; curl -s localhost:8000", "run"),
]

PYTHON = [
    ("python -m unittest discover", "test"),
    ("python3 -m pytest -q", "test"),
    ("python3.11 -m pytest", "test"),
    ("python -m pip install x", "build"),
    ("python -m venv .venv", "build"),
    ("python -m json.tool x.json", "explore"),
    ("python -m http.server 8000", "run"),
    ("python script.py", "run"),
    ("python test_foo.py", "test"),
    ("python scripts/check_env.py", "test"),
    ("python build_dataset.py", "run"),            # a Python script named build_* is data, not a build
    ("python setup.py install", "build"),
    ("python --version", "explore"),
    ("/usr/bin/python3 -c \"import os\"", "run"),
    ("~/.conda/envs/x/bin/python train.py", "run"),
    ("python -c \"open('a','w').write('x')\"", "edit"),
    ("python -c \"with open('a.txt', 'a') as f: f.write('x')\"", "edit"),
    ("python -c \"with open('a.bin', mode='wb') as f: pass\"", "edit"),
    ("python -c \"from pathlib import Path; Path('a').write_text('x')\"", "edit"),
    ("python -c \"import json; json.dump(d, open('o.json','w'))\"", "edit"),
    ("python -c \"import pandas as pd; pd.read_csv('a').to_csv('b')\"", "edit"),
    ("python -c \"import shutil; shutil.copy('a','b')\"", "edit"),
    ("python -c \"import shutil; shutil.move('a','b')\"", "edit"),
    ("python -c \"import os; os.remove('a')\"", "edit"),
    ("python -c \"import os; os.makedirs('d')\"", "edit"),
    ("python -c \"import numpy as np; np.save('a.npy', x)\"", "edit"),
    ("python -c \"import pickle; pickle.dump(x, f)\"", "edit"),
    ("python -c \"import matplotlib.pyplot as plt; plt.savefig('a.png')\"", "edit"),
    ("python -c \"from pathlib import Path; Path('d').mkdir()\"", "edit"),
    ("python -c \"open(os.path.join(d, 'x'), 'w').write('y')\"", "edit"),     # a call inside the call
    ("python -c \"print(s.replace('a', 'b'))\"", "run"),                      # not os.replace
    ("python -c \"import json; print(json.dumps(d))\"", "run"),    # dumps returns a string
    ("python -c \"print(open('a').read())\"", "run"),
    ("python -c \"open('a','r')\"", "run"),
    ("python -c \"import unittest; unittest.main()\"", "test"),
    ("python -c \"import pytest; pytest.main(['-q'])\"", "test"),
    ("python3 <<'EOF'\nimport pytest\nEOF", "test"),
    ("python3 - <<EOF\nprint(1)\nEOF", "run"),
    ("python - <<'EOF'\nfrom pathlib import Path\nPath('x').write_text('y')\nEOF", "edit"),
    ("python train.py <<EOF\nopen('a','w')\nEOF", "run"),    # a script was named: the heredoc is its stdin
]

EDITS = [
    ("sed -i.bak 's/a/b/' f", "edit"),
    ("sed --in-place s/a/b/ f", "edit"),
    ("sed -Ei 's/a+/b/' f", "edit"),
    ("sed -n 's/a/b/p' f", "explore"),
    ("sed -e 's/i/j/' f", "explore"),                       # the `i` is the script, not a flag
    ("perl -pi -e 's/a/b/' f", "edit"),
    ("perl -Mstrict -ne 'print' f", "run"),                 # `strict` has an `i`: it is a module, not -i
    ("echo hi > f", "edit"),
    ("echo hi >> f", "edit"),
    ("printf 'x' > f", "edit"),
    ("cat a b > c", "edit"),
    ("echo hi > /dev/null", "explore"),
    ("echo hi 2>&1", "explore"),
    ("echo hi >&2", "explore"),
    ("cat f 2>/dev/null", "explore"),
    ("printf x | tee f", "edit"),
    ("ls | tee -a log", "edit"),
    ("ls | tee", "explore"),
    ("ls > files.txt", "explore"),                  # only cat, echo, printf, and tee write by redirect
    ("rm -rf build", "edit"),
    ("mkdir -p out", "edit"),
    ("touch x", "edit"),
    ("chmod +x run.sh", "edit"),
    ("ln -s a b", "edit"),
    ("cp -r a b", "edit"),
    ("mv a b", "edit"),
    ("patch -p1 < x.diff", "edit"),
    ("git apply x.patch", "edit"),
    ("find . -name '*.pyc' -delete", "edit"),
    ("find . -name '*.py'", "explore"),
    ("cat <<'EOF' > script.sh\necho hi\nEOF", "edit"),
    ("cat <<'EOF'\nhello\nEOF", "explore"),                 # to the terminal: no file
]

GIT = [
    ("git status", "explore"),
    ("git log --oneline -5", "explore"),
    ("git show HEAD", "explore"),
    ("git blame f", "explore"),
    ("git diff --stat", "explore"),
    ("git -C /repo status", "explore"),
    ("git -c core.pager=cat log", "explore"),
    ("git branch", "explore"),
    ("git branch -a", "explore"),
    ("git remote -v", "explore"),
    ("git tag", "explore"),
    ("git stash list", "explore"),
    ("git config --get user.name", "explore"),
    ("git ls-files", "explore"),
    ("git rev-parse HEAD", "explore"),
    ("git --version", "explore"),
    ("git", "explore"),
    ("git commit -m x", "git"),
    ("git push origin main", "git"),
    ("git pull", "git"),
    ("git fetch --all", "git"),
    ("git clone https://x.org/r.git", "git"),
    ("git checkout -b x", "git"),
    ("git switch x", "git"),
    ("git merge x", "git"),
    ("git rebase main", "git"),
    ("git stash", "git"),
    ("git reset --hard", "git"),
    ("git tag v1", "git"),
    ("git branch feature", "git"),
    ("git add -A", "git"),
    ("gh pr create --fill", "git"),
    ("gh", "git"),
]

TOOLS = [   # package managers, build systems, test runners, runners
    ("pip install x", "build"), ("pip3 install -r r.txt", "build"), ("pip uninstall -y x", "build"),
    ("pip list", "explore"), ("pip show x", "explore"), ("pip freeze", "explore"),
    ("uv sync", "build"), ("uv pip install x", "build"), ("uv add x", "build"),
    ("uv pip list", "explore"), ("uv run python a.py", "run"), ("uv run --with rich python a.py", "run"),
    ("conda install x", "build"), ("conda create -n e python=3.11", "build"),
    ("conda env create -f e.yml", "build"), ("mamba install x", "build"),
    ("conda list", "explore"), ("conda env list", "explore"), ("conda run -n e pytest", "test"),
    ("spack install x", "build"), ("spack find", "explore"),
    ("npm install", "build"), ("npm ci", "build"), ("npm run build", "build"), ("npm ls", "explore"),
    ("npm test", "test"), ("npm run test", "test"), ("npm run lint", "test"), ("npm start", "run"),
    ("npx tsc --noEmit", "test"),
    ("cargo build", "build"), ("cargo test", "test"), ("cargo clippy", "test"), ("cargo run", "run"),
    ("go build ./...", "build"), ("go test ./...", "test"), ("go run .", "run"),
    ("make", "build"), ("make -j8 all", "build"), ("make clean", "build"), ("make -C build", "build"),
    ("make test", "test"), ("make check", "test"), ("make run", "run"),
    ("cmake -B build", "build"), ("cmake --build build", "build"), ("ninja -C build", "build"),
    ("gcc -o a a.c", "build"), ("nvcc x.cu", "build"), ("pdflatex main.tex", "build"),
    ("latex main.tex", "build"), ("dvipng a.dvi", "build"),
    ("hf download x", "build"), ("huggingface-cli download x", "build"),
    ("pytest", "test"), ("pytest -x tests/", "test"), ("tox", "test"), ("ctest", "test"),
    ("mypy x", "test"), ("pyright", "test"), ("tsc --noEmit", "test"), ("eslint .", "test"),
    ("shellcheck x.sh", "test"), ("flake8", "test"), ("ruff format .", "test"),
    ("python -m ruff check", "test"),
    ("./bench", "run"), ("./a.out --x", "run"), ("Rscript x.R", "run"), ("julia x.jl", "run"),
    ("matlab -batch x", "run"), ("torchrun --nproc_per_node 4 train.py", "run"),
    ("accelerate launch x.py", "run"), ("mpirun -np 4 ./a.out", "run"),
    ("apptainer exec img.sif python x.py", "run"), ("node x.js", "run"),
    ("bash run.sh", "run"), ("sh run.sh", "run"), ("./run.sh", "run"),
    ("bash scripts/check_env.sh", "test"), ("./verify.sh", "test"),
    ("bash install.sh", "build"), ("bash setup_env.sh", "build"), ("bash download_data.sh", "build"),
    ("apptainer pull img.sif docker://x", "build"), ("apptainer inspect img.sif", "explore"),
]

SLURM_AND_WAIT = [
    ("sbatch --wrap=\"sleep 1\"", "slurm"), ("srun --pty bash", "slurm"), ("salloc -N1", "slurm"),
    ("scancel 123", "slurm"), ("sacct -j 1", "slurm"), ("scontrol show job 1", "slurm"),
    ("sinfo", "slurm"), ("sstat -j 1", "slurm"), ("seff 1", "slurm"), ("sbatch --version", "explore"),
    ("sleep 5", "wait"), ("wait", "wait"), ("wait $pid", "wait"),
    ("tail -F x", "wait"), ("tail -n 20 -f x", "wait"), ("tail --follow=name x", "wait"),
    ("pgrep -f x", "wait"), ("ps aux | grep python", "wait"), ("kill -0 123", "wait"),
    ("timeout 600 tail -f log", "wait"),
    ("tail -n 20 x", "explore"), ("tail -5 log.txt", "explore"),
]

FETCH = [
    ("curl -s https://x.org/a", "explore"),
    ("curl -o /dev/null -s -w '%{http_code}' https://x.org/a", "explore"),    # the body is thrown away
    ("curl -sSf https://x.org/i.sh | sh", "build"),
    ("curl -sSf https://x.org/i.sh | bash -s -- --yes", "build"),
    ("curl -L https://x.org/a.tgz | tar xz", "build"),
    ("curl -O https://x.org/a.tgz", "build"),
    ("curl --output f https://x.org/a", "build"),
    ("curl -s https://x.org/a.json > a.json", "build"),
    ("wget -qO- https://x.org/a | jq .", "explore"),                         # to stdout
    ("wget -O f https://x.org/a", "build"),
    ("wget -qO - https://x.org/a", "explore"),
]

EXPLORE = [
    ("rg foo", "explore"), ("grep -rn x .", "explore"), ("head -5 f", "explore"), ("nl -ba f", "explore"),
    ("wc -l f", "explore"), ("jq . f", "explore"), ("diff a b", "explore"), ("tree", "explore"),
    ("stat f", "explore"), ("du -sh .", "explore"), ("df -h", "explore"), ("date", "explore"),
    ("whoami", "explore"), ("echo hi", "explore"), ("which python", "explore"),
    ("awk '{print $1}' f", "explore"), ("sort f | uniq -c", "explore"), ("module avail", "explore"),
    ("ugrep -G --ignore-files -qE passed /tmp/t.output", "explore"),
    # utilities that are not run, so no table can leave them to "a program nobody named"
    ("stty -a", "explore"), ("tput cols", "explore"), ("seq 1 10", "explore"),
    ("pdftotext a.pdf -", "explore"), ("zcat a.gz | head", "explore"), ("tar tf a.tgz", "explore"),
    ("pkill -f x", "explore"), ("sha1sum a", "explore"), ("dig x.org", "explore"),
    ("jobstats 123", "explore"),
    ("who | wc -l", "explore"), ("last -n 5", "explore"), ("logger hi", "explore"),
]

# A program no table names: on a login node it is most often a code or a lab tool being run.
UNKNOWN_PROGRAMS = [
    ("lmp -in in.melt", "run"),
    ("gmx mdrun -deffnm x", "run"),
    ("mpirun -np 4 lmp_mpi -in x", "run"),
    ("mytool", "run"),                                          # bare, as a census gives it
    ("module load gromacs && gmx mdrun -s a.tpr", "run"),
    ("pw.x -in a.in > a.out", "run"),
    ("nequip-train config.yaml", "run"),
    ("lmp -in x && git add .", "run"),                          # run outranks git,
    ("lmp -in x | tee log.txt", "run"),                         # edit,
    ("lmp -in x; sleep 5", "run"),                              # and wait,
    ("make && lmp -in x", "run"),                               # and build,
    ("lmp -in x; sbatch a.sh", "slurm"),                        # but not slurm,
    ("pytest && lmp -in x", "test"),                            # or test
    ("frobnicate --x", "run"),
    # a function that a sourced file defines is a name no table has
    ("source env.sh; load_dataset DIV1", "run"),
]

# Words that look like a command and are not: none of them is run.
NOT_COMMANDS = [
    # the patterns of a case
    ("case $s in RUNNING|PENDING) sleep 5;; COMPLETED) echo done;; esac", "wait"),
    ("case $b in exp10|exp8|gt) ;; *) continue;; esac", "explore"),
    ("case $x in\n  a) ls ;;\n  b|c) pwd ;;\nesac", "explore"),
    ("case $x in (a|b) echo hi;; esac", "explore"),
    # a function's own name, and a call to it
    ("g() { git status; }; g", "explore"),
    ("snap () { :; }\nsnap", "explore"),
    ("function g { git log; }; g", "explore"),
    ("run_it() { sbatch a.sh; }; run_it", "slurm"),
    ("c() { ls; }; echo \"n: $(c x)\"", "explore"),            # a call inside a substitution too
    ("f(){ ls; }\nf | wc -l", "explore"),
    # arithmetic
    ("echo $(( n + 1 ))", "explore"),
    ("(( i++ ))", "explore"),
    ("for ((i = 0; i < 3; i++)); do echo $i; done", "explore"),
    ("x=$(( a * b ))", "explore"),
    # the words a command goes on with after a $( )
    ("echo $(date) prompt $(ls) done", "explore"),
    ("ls $(pwd)/logs", "explore"),
    # after an assignment alone they are the command
    ("OMP_NUM_THREADS=$(nproc) python train.py", "run"),
    ("A=$(date) B=$(hostname) pytest -q", "test"),
    ("A=$(date) make", "build"),
    # a word glued to the `)` is part of the word before it: here an assignment, not a command
    ("D=$(pwd)/data; ls", "explore"),
    ("T=$(readlink -f x)/pipeline_test; ls", "explore"),
    ("D=$(pwd)/data; ls $D", "explore"),
    ("OUT=$(date +%F)_results; ls", "explore"),
    ("T=$(readlink -f ../data)/pipeline_forces_test; ls", "explore"),
    ("A=$(date)\"_x\"; ls", "explore"),                         # quoted, still glued
    ("A=$(pwd)$(date)/z; ls", "explore"),                       # two substitutions in one word
    ("OUT=$(pwd)/$(date +%F)_results; ls", "explore"),
    ("A=$(date)/x make", "build"),                              # a glued tail, then the command
    ("TREE_TEST_DIR=$(readlink -f ../x)/pipeline_test sbatch a.sh", "slurm"),
    # and when it is the program's own path it is the program
    ("$(dirname $0)/run_tests.sh", "test"),
    ("$(pwd)/bin/python train.py", "run"),
    # [[ ]]
    ("if [[ $name =~ ^(PWD|OLDPWD)$ ]]; then continue; fi", "explore"),
    ("[[ -f a && -f b ]] && echo ok", "explore"),
    # an awk field
    ("awk -F/ '{print $(NF-1)}' f | sort | uniq -c", "explore"),
]

UNKNOWN = [
    ("", "unknown"), ("   ", "unknown"), ("\n\n", "unknown"), (None, "unknown"),
    ("$CODEX_REMOTE_PAYLOAD", "unknown"),
    ("sh -c 'if [ -z \"$SHELL\" ]; then exit 127; fi; CODEX_REMOTE_PAYLOAD=\"$1\"; "
     "exec \"$SHELL\" -lc \"$CODEX_REMOTE_PAYLOAD\"'", "unknown"),
    ("$CMD train", "unknown"),           # held in a variable no rule reads (VariableProgramTest)
    ("bash", "unknown"), ("&&", "unknown"), (";", "unknown"), ("()", "unknown"), ("> out.txt", "unknown"),
    ("2>&1", "unknown"),                 # a descriptor, not a program
    # fragments of shell syntax are no program
    ("-ge 3 ]", "unknown"), ("2 + 1", "unknown"),
]

SNAP = "/bin/zsh -c source /home/a/.claude/shell-snapshots/snapshot-zsh-1.sh && eval "
SHOPT = ("/bin/bash -c -l shopt -u extglob 2>/dev/null || true && { \\builtin unalias -- 'unsetenv'; "
         "\\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 && eval ")


class CategoryTest(unittest.TestCase):
    def test_each_command_shape_gets_its_category(self):
        for text, want in CASES:
            with self.subTest(text=text):
                self.assertEqual(category_of(text), want)

    def test_a_childs_own_argv_is_named_the_same_way(self):
        self.assertEqual(category_of_argv("python3", "python3 train.py"), "run")
        self.assertEqual(category_of_argv("sleep", "sleep 15"), "wait")
        self.assertEqual(category_of_argv("2.1.308-3f6ee6c",
                         "ugrep -G --ignore-files -qE passed|failed /tmp/t.output"), "explore")
        self.assertEqual(category_of_argv("srun", None), "slurm")

    def test_what_a_wait_waits_on(self):
        self.assertEqual(waits_on("squeue -h -u $USER -o %j", "slurm"), "slurm")
        self.assertEqual(waits_on("grep -qE 'passed|failed' /tmp/x/tasks/a.output", "explore"), "output")
        self.assertEqual(waits_on("pgrep -f train.py", "wait"), "process")
        self.assertEqual(waits_on("sleep 600", "wait"), "sleep")
        self.assertEqual(waits_on("date", "explore"), "command")

    def test_hashes_are_stable_and_ignore_spacing(self):
        self.assertEqual(norm_hash("squeue  -u x"), norm_hash("squeue -u x"))
        self.assertEqual(norm_hash("squeue -u x"), zlib.crc32(b"squeue -u x"))
        self.assertEqual(norm_hash(""), 0)


class ShellTextTest(unittest.TestCase):
    def test_claude_eval_body_new_style(self):
        a = ("/bin/zsh -c source /home/a/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true"
             " && setopt NO_EXTENDED_GLOB 2>/dev/null || true && eval 'bash w26.sh 580 | tail -1'"
             " < /dev/null && pwd -P >| /tmp/claude-f1f6-cwd")
        self.assertEqual(shell_text("zsh", a), "bash w26.sh 580 | tail -1")

    def test_claude_eval_body_old_style_and_escaped_quotes(self):
        a = ("/bin/bash -c source /h/.claude/shell-snapshots/s.sh && eval 'grep -F '\"'\"'x y'\"'\"' f'"
             " \\< /dev/null && pwd -P >| /tmp/claude-1-cwd")
        self.assertEqual(shell_text("bash", a), "grep -F 'x y' f")

    def test_codex_login_shell_string(self):
        self.assertEqual(shell_text("bash", "/usr/bin/bash -lc sed -n '1,35p' docs/a.md"),
                         "sed -n '1,35p' docs/a.md")

    def test_not_a_shell_with_a_command(self):
        self.assertIsNone(shell_text("cat", "cat /x"))
        self.assertIsNone(shell_text("bash", "/bin/bash -l -i"))

    def test_claude_eval_body_with_no_suffix(self):
        self.assertEqual(shell_text("zsh", "/bin/zsh -c source /home/a/.claude/shell-snapshots/s.sh && eval 'git status'"), "git status")

    # -- the quoting a body can come in -------------------------------------------------

    def test_a_body_quoted_the_other_way_round_is_unquoted_the_same(self):
        a = SNAP + "'grep -F '\\''x y'\\'' f' < /dev/null && pwd -P >| /tmp/claude-1-cwd"
        self.assertEqual(shell_text("zsh", a), "grep -F 'x y' f")

    def test_a_double_quoted_body(self):
        self.assertEqual(shell_text("zsh", SNAP + '"echo \\"hi\\" && ls" < /dev/null'), 'echo "hi" && ls')

    def test_a_body_with_no_metacharacter_is_not_quoted_at_all(self):
        a = SNAP + "/proj/wait_vision.sh < /dev/null && pwd -P >| /tmp/claude-3fc9-cwd"
        self.assertEqual(shell_text("zsh", a), "/proj/wait_vision.sh")

    def test_an_eval_inside_the_body_is_the_bodys(self):
        a = SNAP + ("'eval \"$(conda shell.bash hook)\" && python x.py' < /dev/null"
                    " && pwd -P >| /tmp/claude-1-cwd")
        self.assertEqual(shell_text("zsh", a), 'eval "$(conda shell.bash hook)" && python x.py')

    def test_a_body_keeps_its_newlines(self):
        a = SNAP + "'python - <<'\"'\"'EOF'\"'\"'\nprint(1)\nEOF' < /dev/null"
        self.assertEqual(shell_text("zsh", a), "python - <<'EOF'\nprint(1)\nEOF")

    def test_an_empty_body_is_empty_not_none(self):
        self.assertEqual(shell_text("zsh", SNAP + "'' < /dev/null"), "")

    def test_a_body_cut_short_keeps_what_was_recorded(self):
        a = SNAP + "'git commit -m '\"'\"'a long mess"
        self.assertEqual(shell_text("zsh", a), "git commit -m 'a long mess")

    def test_a_snapshot_script_that_runs_nothing_has_no_command(self):
        a = ("/bin/zsh -c source /h/.claude/shell-snapshots/s.sh; "
             "if [ ! -f \"$SNAPSHOT_FILE\" ]; then exit 1; fi")
        self.assertIsNone(shell_text("zsh", a))

    # -- a Claude shell that did not source a snapshot ------------------------------------

    def test_claude_shell_with_no_snapshot_path(self):
        a = SHOPT + "'tail -5 \"$f\"' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd"
        self.assertEqual(shell_text("bash", a), 'tail -5 "$f"')

    def test_claude_shell_with_no_snapshot_and_no_redirect_from_null(self):
        a = SHOPT + "'sleep 9' && pwd -P >| /tmp/claude-ab12-cwd"
        self.assertEqual(shell_text("bash", a), "sleep 9")

    # -- Codex and other `-c` strings -----------------------------------------------------

    def test_options_on_either_side_of_dash_c(self):
        self.assertEqual(shell_text("bash", "/bin/bash -c -l ls -la"), "ls -la")
        self.assertEqual(shell_text("bash", "bash -l -c ls -la"), "ls -la")
        self.assertEqual(shell_text("bash", "bash --norc -c ls"), "ls")
        self.assertEqual(shell_text("bash", "bash -o pipefail -c make test"), "make test")
        self.assertEqual(shell_text("sh", "/bin/sh -c printf \"%s\" \"$HOME\""), "printf \"%s\" \"$HOME\"")

    def test_a_dash_c_string_keeps_its_whitespace_and_newlines(self):
        a = "/usr/bin/bash -lc cat <<'EOF'\n  a  b\nEOF"
        self.assertEqual(shell_text("bash", a), "cat <<'EOF'\n  a  b\nEOF")

    def test_an_eval_in_a_codex_string_is_not_claudes_wrapper(self):
        self.assertEqual(shell_text("bash", "/bin/bash -lc eval \"$(conda shell.bash hook)\" && pytest"),
                         "eval \"$(conda shell.bash hook)\" && pytest")

    def test_a_shell_with_nothing_to_run(self):
        self.assertIsNone(shell_text("bash", "bash script.sh"))        # a script file, not a command string
        self.assertIsNone(shell_text("sh", "/bin/sh -c"))
        self.assertIsNone(shell_text("zsh", ""))
        self.assertIsNone(shell_text("zsh", None))
        self.assertIsNone(shell_text(None, None))

    def test_only_a_shell_has_a_command_string(self):
        self.assertIsNone(shell_text("python3", "python3 -c print(1)"))
        self.assertIsNone(shell_text("ssh", "ssh host -c aes128 ls"))


class SplitTest(unittest.TestCase):
    def test_separators_split_simple_commands(self):
        self.assertEqual(split_simple("a b && c || d; e | f & g"),
                         [["a", "b"], ["c"], ["d"], ["e"], ["f"], ["g"]])

    def test_newlines_separate_commands(self):
        self.assertEqual(split_simple("ls\npwd\n\n  \ndate"), [["ls"], ["pwd"], ["date"]])

    def test_quotes_protect_what_looks_like_an_operator(self):
        self.assertEqual(split_simple("echo 'a && b' \"c; d\" e\\;f"), [["echo", "a && b", "c; d", "e;f"]])

    def test_redirections_stay_with_their_command(self):
        self.assertEqual(split_simple("make 2>&1 | tail"), [["make", "2", ">&", "1"], ["tail"]])
        self.assertEqual(split_simple("echo hi >> a.log"), [["echo", "hi", ">>", "a.log"]])

    def test_parentheses_and_substitutions_open_commands_of_their_own(self):
        self.assertEqual(split_simple("(cd x; ls)"), [["cd", "x"], ["ls"]])
        self.assertEqual(split_simple("echo $(date)"), [["echo", "$"], ["date"]])

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        self.assertEqual(split_simple("echo foo#bar"), [["echo", "foo#bar"]])

    def test_a_comment_line_is_dropped(self):
        self.assertEqual(split_simple("# run the tests\nls"), [["ls"]])

    def test_a_backslash_newline_continues_the_line(self):
        self.assertEqual(split_simple("make \\\n  -j8"), [["make", "-j8"]])

    def test_unbalanced_quotes_fall_back_to_whitespace(self):
        self.assertEqual(split_simple("echo \"unterminated"), [["echo", "\"unterminated"]])
        self.assertEqual(split_simple("echo it's"), [["echo", "it's"]])

    def test_empty_text_has_no_commands(self):
        self.assertEqual(split_simple(""), [])
        self.assertEqual(split_simple("  ;  ; "), [])

    def test_a_heredoc_body_is_one_token_after_the_marker(self):
        self.assertEqual(split_simple("cat > notes.md <<'EOF'\nhello\nEOF"),
                         [["cat", ">", "notes.md", "__HEREDOC__", "hello"]])

    def test_commands_go_on_after_a_heredoc(self):
        self.assertEqual(split_simple("python - <<EOF | tail -1\nprint(1)\nEOF\nls"),
                         [["python", "-", "__HEREDOC__", "print(1)"], ["tail", "-1"], ["ls"]])

    def test_a_heredoc_body_is_data_whatever_it_holds(self):
        body = "don't; rm -rf x && y\n\"unbalanced"
        self.assertEqual(split_simple("cat <<'EOF'\n%s\nEOF" % body), [["cat", "__HEREDOC__", body]])

    def test_a_dash_heredoc_ends_at_a_tab_indented_terminator(self):
        self.assertEqual(split_simple("cat <<-EOF\n\thi\n\tEOF\nls"),
                         [["cat", "__HEREDOC__", "\thi"], ["ls"]])

    def test_a_heredoc_with_no_terminator_runs_to_the_end(self):
        self.assertEqual(split_simple("cat <<EOF\na\nb"), [["cat", "__HEREDOC__", "a\nb"]])

    def test_a_here_string_is_not_a_heredoc(self):
        self.assertEqual(split_simple("grep x <<< hello"), [["grep", "x", "<<<", "hello"]])

    def test_a_heredoc_inside_a_quoted_substitution_is_not_commands(self):
        # the shape of every Claude Code commit
        got = split_simple("git commit -m \"$(cat <<'EOF'\nFix the thing\nEOF\n)\"")
        self.assertEqual(got[0][:3], ["git", "commit", "-m"])
        self.assertEqual({c[0] for c in got} & {"Fix", "EOF"}, set())

    def test_two_heredocs_on_one_line_take_their_bodies_in_order(self):
        self.assertEqual(split_simple("cat <<A <<B\none\nA\ntwo\nB"),
                         [["cat", "__HEREDOC__", "one", "__HEREDOC__", "two"]])

    def test_text_that_looks_like_the_readers_own_placeholder_is_a_word(self):
        self.assertEqual(split_simple("echo \x000\x00"), [["echo", "\x000\x00"]])

    def test_the_patterns_of_a_case_are_not_commands(self):
        self.assertEqual(split_simple("case $x in a|b) ls;; c) pwd;; esac"), [["ls"], ["pwd"]])
        self.assertEqual(split_simple("case $x in (a|b) ls;; esac"), [["ls"]])

    def test_the_inside_of_arithmetic_is_not_commands(self):
        self.assertEqual(split_simple("echo $(( n + 1 ))"), [["echo", "$"]])
        self.assertEqual(split_simple("echo $(( (a + b) * 2 ))"), [["echo", "$"]])

    def test_the_inside_of_a_double_bracket_is_one_command(self):
        self.assertEqual(split_simple("[[ $a =~ ^(x|y)$ ]] && ls"),
                         [["[[", "$a", "=~", "^", "x", "y", "$", "]]"], ["ls"]])

    def test_a_function_definition_keeps_its_name_as_a_word(self):
        self.assertEqual(split_simple("g() { ls; }"), [["g"], ["{", "ls"], ["}"]])

    def test_the_words_after_a_substitution_go_on_with_its_command(self):
        self.assertEqual(split_simple("echo $(date) done"), [["echo", "$", "done"], ["date"]])
        self.assertEqual(split_simple("A=$(date) make"), [["A=$", "make"], ["date"]])

    def test_a_word_glued_to_the_closing_parenthesis_joins_the_word_before_it(self):
        self.assertEqual(split_simple("D=$(pwd)/data; ls"), [["D=$/data"], ["pwd"], ["ls"]])
        self.assertEqual(split_simple("OUT=$(pwd)/$(date)_x"), [["OUT=$/$_x"], ["pwd"], ["date"]])
        self.assertEqual(split_simple("echo $(( 1 + 2 ))x"), [["echo", "$x"]])

    def test_a_space_after_the_closing_parenthesis_keeps_the_words_apart(self):
        self.assertEqual(split_simple("A=$(date) /data"), [["A=$", "/data"], ["date"]])
        self.assertEqual(split_simple("A=$(date)    /data"), [["A=$", "/data"], ["date"]])

    def test_a_quoted_string_that_spans_lines_stays_one_word(self):
        self.assertEqual(split_simple("git commit -m \"Fix X\n\nmake sure it works\"\nls"),
                         [["git", "commit", "-m", "Fix X\n\nmake sure it works"], ["ls"]])


class CategoryShapeTest(unittest.TestCase):
    def run_table(self, table):
        for text, want in table:
            with self.subTest(text=text):
                self.assertEqual(category_of(text), want)

    def test_wrappers_keywords_and_setup(self):
        self.run_table(WRAPPERS_AND_SETUP)

    def test_a_wrapped_command_reads_as_it_does_alone(self):        # final review Minor
        for wrap in ("sudo -n", "sudo -u alice -H", "sudo -E env A=1", "ionice -c3 -n7",
                     "ionice -c idle", "taskset -c 0-3", "taskset -a ff", "numactl -l",
                     "numactl --cpunodebind=0 -m 0", "numactl -C 0-7 --interleave all"):
            for text in ("tar czf a.tgz d", "pytest -q", "sbatch a.sh", "rm -rf build",
                         "git push", "python x.py", "bash -c 'until [ -f x ]; do sleep 5; done'"):
                with self.subTest(wrap=wrap, text=text):
                    self.assertEqual(category_of(wrap + " " + text), category_of(text))

    def test_a_loop_that_sleeps_is_a_wait(self):
        self.run_table(LOOPS)

    def test_a_substitution_is_read_too(self):
        self.run_table(SUBSTITUTIONS)

    def test_a_call_with_several_commands_takes_the_first_of_the_order(self):
        self.run_table(PRIORITY)

    def test_a_call_that_begins_with_a_long_sleep_waits_on_it(self):
        self.run_table(SLEEPS_FIRST)
        # R60: on what the rest of it names, Slurm, a process, or a file or its output, and else
        # on the fixed sleep
        for text, on in (("sleep 120 && squeue -u $USER", "slurm"),
                         ("sleep 2m && sbatch x.sh", "slurm"),
                         ("sleep 600; pgrep -f train.py", "process"),
                         ("cd d && sleep 600 && tail log", "output"),
                         ("sleep 590; W=$(cat f); echo $W > out", "output"),
                         ("sleep 600; python train.py", "sleep"),
                         ("X=$(date +%s); sleep 600; python x.py", "sleep"),
                         ("sleep infinity", "sleep")):
            with self.subTest(text=text):
                self.assertEqual(waits_on(text, "wait"), on)
        self.assertEqual(waits_on("sleep 5; tail log", "wait"), "output")   # under a minute

    def test_python(self):
        self.run_table(PYTHON)

    def test_edits(self):
        self.run_table(EDITS)

    def test_git(self):
        self.run_table(GIT)

    def test_package_managers_build_systems_and_runners(self):
        self.run_table(TOOLS)

    def test_slurm_and_waiting(self):
        self.run_table(SLURM_AND_WAIT)

    def test_fetching(self):
        self.run_table(FETCH)

    def test_exploring(self):
        self.run_table(EXPLORE)

    def test_a_program_no_table_names_is_run(self):
        self.run_table(UNKNOWN_PROGRAMS)

    def test_what_looks_like_a_command_and_is_not_is_not_run(self):
        self.run_table(NOT_COMMANDS)

    def test_what_cannot_be_read_is_unknown(self):
        self.run_table(UNKNOWN)

    def test_a_heredoc_body_is_never_read_as_commands(self):
        self.assertEqual(category_of("cat > notes.md <<'EOF'\npytest -x\nsbatch run.sh\nEOF"), "edit")
        self.assertEqual(category_of("cat <<'EOF'\npytest -x\nEOF"), "explore")

    def test_a_commit_message_that_names_a_command_is_a_commit(self):
        self.assertEqual(category_of("git commit -m \"Fix X\n\nmake sure it works\nrun pytest\""), "git")
        self.assertEqual(category_of("git commit -m \"$(cat <<'EOF'\nmake the build faster\nEOF\n)\""), "git")

    def test_every_answer_is_one_of_the_categories(self):
        for table in (CASES, WRAPPERS_AND_SETUP, LOOPS, SUBSTITUTIONS, PRIORITY, SLEEPS_FIRST, PYTHON, EDITS,
                      GIT, TOOLS, SLURM_AND_WAIT, FETCH, EXPLORE, UNKNOWN_PROGRAMS, NOT_COMMANDS, UNKNOWN):
            for text, _ in table:
                self.assertIn(category_of(text), CATS)

    def test_the_categories_are_in_priority_order(self):
        self.assertEqual(CATS, ("test", "slurm", "run", "build", "edit", "git", "wait", "explore", "unknown"))

    def test_nothing_unreadable_raises(self):
        rng = random.Random(20261029)
        bits = ["'", '"', "$(", ")", "`", "<<", "<<-", "EOF", "\n", "\\", "&&", "||", ";", "|", "&", ">",
                ">>", "<", "(", "{", "}", "if", "then", "do", "done", "while", "until", "for", "sleep 1",
                "python -c", "-c", "eval", "bash -c", "$", "${", "#", " ", "  ", "\t", "git", "make", "x=1",
                "case", "in", "esac", ";;", "((", "))", "$((", "[[", "]]", "function", "()", "g() {", "lmp",
                "/d", "_x", "A=$(", "$(pwd)",
                "\x00", "é", "\ud800"]
        for _ in range(4000):
            text = "".join(rng.choice(bits) for _ in range(rng.randint(1, 14)))
            with self.subTest(text=text):
                self.assertIn(category_of(text), CATS)
                self.assertIn(waits_on(text, "wait"), ("slurm", "process", "output", "sleep", "command"))
                split_simple(text)
                norm_hash(text)
                shell_text("bash", "bash -c " + text)
                shell_text("zsh", SNAP + text)

    def test_a_deeply_nested_command_is_read_to_a_bounded_depth(self):
        text = "echo " + "\"$(" * 60 + "sbatch x.sh" + ")\"" * 60
        self.assertIn(category_of(text), CATS)

    def test_a_very_long_command_is_read_to_a_bounded_length(self):
        self.assertEqual(category_of("echo " + "x" * 400000), "explore")
        self.assertEqual(category_of("ls\n" * 80000 + "pytest"), "explore")      # past the cap, not read


class VariableProgramTest(unittest.TestCase):
    """A program held in a variable (`$PY -m pytest`) is read by the first rule that applies: the
    text assigns it earlier, its name is Python's, or its first argument is Python's."""

    def test_a_variable_assigned_in_the_same_command_reads_as_its_value(self):
        self.assertEqual(category_of("PY=../venv/bin/python; $PY -m pytest tests/x.py -q"), "test")
        self.assertEqual(category_of('PY=/opt/py/bin/python3 && "$PY" train.py --epochs 3'), "run")

    def test_a_python_like_name_reads_as_python(self):
        self.assertEqual(category_of(
            "source slurm/env.sh && $PY -m pytest tests/test_x.py -q 2>&1 | tail -8"), "test")
        self.assertEqual(category_of(
            "source slurm/env.sh && ${PYTHON} scripts/eval_e2e.py --beam 400"), "run")
        self.assertEqual(category_of('"$PY" -m pytest -q'), "test")

    def test_python_arguments_read_as_python(self):
        self.assertEqual(category_of("$INTERP -c 'import json; print(1)'"), "run")
        self.assertEqual(category_of("$BIN -m pytest tests"), "test")

    def test_a_variable_python_heredoc_follows_the_heredoc_rules(self):
        edit = ("source slurm/env.sh && $PY - <<'PY'\nimport pathlib\np = pathlib.Path('a.py')\n"
                "p.write_text(p.read_text().replace('a', 'b'))\nPY")
        self.assertEqual(category_of(edit), "edit")
        run = "PY=../venv/bin/python; $PY - <<'EOF'\nimport duckdb\nprint(duckdb.connect())\nEOF"
        self.assertEqual(category_of(run), "run")

    def test_other_variables_read_as_today(self):
        self.assertEqual(category_of("$CMD --flag x"), "unknown")
        self.assertEqual(category_of('echo "$HOME"'), "explore")
        self.assertEqual(category_of("ls $DIR"), "explore")

    # -- rule 1: the text assigns it earlier ---------------------------------------------------
    # Each uses a name and an argument no other rule reads (`SB`, `job.sh`), so only the assignment
    # can say what runs.

    def test_an_assignment_reads_whatever_its_form(self):
        for text in ("SB=sbatch; $SB job.sh",
                     "export SB=sbatch; $SB job.sh",
                     "export OTHER=1 SB=sbatch && ${SB} job.sh",
                     "SB='sbatch --parsable'; $SB job.sh",
                     'SB="sbatch --parsable" && "$SB" job.sh',
                     'SB="sbatch --parsable" && "${SB}" job.sh'):
            with self.subTest(text=text):
                self.assertEqual(category_of(text), "slurm")

    def test_an_assignment_wins_over_the_name_and_the_arguments(self):
        # without it, the name `PY` and the `.py` file say Python, and `-c` does
        self.assertEqual(category_of("PY=sbatch; $PY train.py"), "slurm")
        self.assertEqual(category_of("P=pytest; $P -c x"), "test")

    def test_the_last_assignment_before_the_command_is_the_one(self):
        self.assertEqual(category_of("SB=ls; SB=sbatch; $SB job.sh"), "slurm")
        self.assertEqual(category_of("SB=sbatch; SB=ls; $SB job.sh"), "explore")

    def test_the_first_word_of_a_value_is_the_program(self):
        self.assertEqual(category_of("S='sbatch --parsable -p gpu'; $S job.sh"), "slurm")
        self.assertEqual(category_of("G='git -c safe.directory=*'; $G commit -m x"), "git")
        self.assertEqual(category_of("B=/opt/bin/make; $B -j8"), "build")
        self.assertEqual(category_of("SH=bash; $SH -c 'pytest -q'"), "test")    # read as a shell is

    def test_a_value_that_names_no_program_leaves_the_next_rules_to_read_it(self):
        # a substitution leaves `$` alone as the value
        self.assertEqual(category_of("PY=$(which python3); $PY -m pytest -q"), "test")
        self.assertEqual(category_of("P=${PY:-python3}; $P -m pytest -q"), "test")
        self.assertEqual(category_of("P=$OTHER; $P -c 'print(1)'"), "run")
        self.assertEqual(category_of("PY=; $PY x.py"), "run")
        # a value with a path in it names a program, though its head is a substitution
        self.assertEqual(category_of("X=$(cat .env_path)/bin/pytest; $X -q"), "test")

    def test_only_an_assignment_before_the_command_counts(self):
        self.assertEqual(category_of("$SB job.sh; SB=sbatch"), "explore")  # setup is all it reads
        # `SB=sbatch cmd` gives `cmd` an environment: it sets nothing for the commands after it,
        # and `$SB` there is expanded before it applies
        self.assertEqual(category_of("SB=sbatch $SB job.sh"), "unknown")
        self.assertEqual(category_of("SB=sbatch make; $SB job.sh"), "build")

    # -- rule 2: the name; rule 3: the first argument ------------------------------------------

    def test_which_names_read_as_python(self):
        for name in ("PY", "py", "Py", "PYTHON", "python3", "PYTHON3", "MTB_PY", "NATIVE_PYTHON",
                     "venvpython"):
            with self.subTest(name=name):
                self.assertEqual(category_of("$%s train" % name), "run")
                self.assertEqual(category_of("${%s} train" % name), "run")
        for name in ("P", "ENVPY", "PYTHONPATH", "PYENV", "PY_BIN", "RUNNER"):
            with self.subTest(name=name):
                self.assertEqual(category_of("$%s train" % name), "unknown")

    def test_which_first_arguments_read_as_python(self):
        for args, want in (("-m pytest", "test"), ("-c 'print(1)'", "run"), ("-", "run"),
                           ("x.py", "run"), ("tools/x.py --flag", "run"), ("'a b/x.py'", "run"),
                           ("tests/test_x.py", "test"),
                           # only the first argument is read
                           ("--flag x", "unknown"), ("job.sh", "unknown"), ("-u x.py", "unknown"),
                           ("x.py.bak", "unknown"), ("-mpytest", "unknown")):
            with self.subTest(args=args):
                self.assertEqual(category_of("$RUNNER " + args), want)

    def test_a_python_variable_goes_through_the_python_rules(self):
        self.assertEqual(category_of("$PY -m pip install -e ."), "build")
        self.assertEqual(category_of("$PY -m venv .venv"), "build")
        self.assertEqual(category_of("$PY -c \"open('a', 'w').write('x')\""), "edit")
        self.assertEqual(category_of("$PY -c 'import pytest'"), "test")
        self.assertEqual(category_of("$PY scripts/run_tests.py"), "test")
        self.assertEqual(category_of("$PY --version"), "explore")
        self.assertEqual(category_of("$PY"), "run")

    def test_a_wrapper_before_the_variable_is_dropped_as_it_is_before_a_program(self):
        self.assertEqual(category_of("nohup $PY train.py > log 2>&1 &"), "run")
        self.assertEqual(category_of('timeout 600 "$PY" -m pytest -q'), "test")
        self.assertEqual(category_of("env A=1 ${PYTHON} -m pytest"), "test")
        self.assertEqual(category_of("if $PY -c 'import x'; then echo ok; fi"), "run")
        self.assertEqual(category_of("while $PY -c 'import x'; do sleep 5; done"), "wait")

    def test_a_variable_that_is_not_the_program_is_not_read(self):
        self.assertEqual(category_of("sbatch $PY"), "slurm")
        self.assertEqual(category_of("ls -l $PY -m"), "explore")
        self.assertEqual(category_of("echo $PY -m pytest"), "explore")

    def test_only_a_whole_parameter_expansion_is_a_program_word(self):
        for word in ("$1", "$@", "${PY:-python3}", "${PY}x", "$PY$PY", "$(which python)"):
            with self.subTest(word=word):
                self.assertNotEqual(category_of(word + " -m pytest"), "test")

    def test_an_argv_has_no_expansions(self):
        self.assertEqual(category_of_argv("$PY", "$PY -m pytest -q"), "unknown")
        self.assertEqual(category_of_argv("bash", "/bin/bash -lc $PY -m pytest -q"), "test")

    def test_nothing_it_reads_raises(self):
        rng = random.Random(20261102)
        bits = ["$PY", "${PY}", '"$PY"', "$P", "${", "$", "PY=python", "PY=", "P=$(which python)",
                "PY=${PY:-python}", "PY='env A=1 python'", "export", "export PY=x", "-m", "-c", "-",
                "x.py", "pytest", ";", "&&", "|", "&", "$(", ")", "(", "\n", "<<EOF", "EOF", "'",
                '"', "env", "nohup", "if", "then", "fi", "while", "do", "done", "sleep 1", "\\",
                "é"]
        for _ in range(3000):
            text = " ".join(rng.choice(bits) for _ in range(rng.randint(1, 12)))
            with self.subTest(text=text):
                self.assertIn(category_of(text), CATS)
                self.assertIn(waits_on(text, "wait"),
                              ("slurm", "process", "output", "sleep", "command"))


class ArgvTest(unittest.TestCase):
    def test_a_shell_is_read_through_to_its_command(self):
        self.assertEqual(category_of_argv("zsh", SNAP + "'sbatch x.sh' < /dev/null"), "slurm")
        self.assertEqual(category_of_argv("bash", "/usr/bin/bash -lc pytest -q"), "test")
        self.assertEqual(category_of_argv("bash", "/bin/bash -c -l ls -la"), "explore")

    def test_a_script_run_by_a_shell_is_named_by_its_name(self):
        self.assertEqual(category_of_argv("bash", "bash scripts/run_tests.sh"), "test")
        self.assertEqual(category_of_argv("bash", "bash build.sh"), "build")

    def test_an_argv_is_one_command_with_no_shell_syntax(self):
        # the collector joins argv with spaces, so `|` and `;` here are characters of one argument
        self.assertEqual(category_of_argv("ugrep", "ugrep -E a|b f"), "explore")
        self.assertEqual(category_of_argv("python3", "python3 -c import os;os.remove('x')"), "edit")
        self.assertEqual(category_of_argv("cat", "cat a > b"), "explore")

    def test_a_childs_wrapper_is_dropped(self):
        self.assertEqual(category_of_argv("watch", "watch -n 5 squeue -u x"), "wait")
        self.assertEqual(category_of_argv("timeout", "timeout 5 sleep 10"), "wait")
        self.assertEqual(category_of_argv("env", "env FOO=1 pytest -q"), "test")
        self.assertEqual(category_of_argv("taskset", "taskset -c 0-3 python train.py"), "run")
        self.assertEqual(category_of_argv("numactl", "numactl --cpunodebind=0 -m 0 ./a.out"), "run")

    def test_a_wrappers_shell_is_read_through_to_its_command(self):    # final review Minor
        # As a shell's own argv is: `sudo -n bash -c "pytest -q"` is test however it is recorded
        self.assertEqual(category_of_argv("sudo", "sudo -n bash -c pytest -q"), "test")
        self.assertEqual(category_of_argv("sudo", "/usr/bin/sudo -u a /bin/bash -lc cd /x && make"), "build")
        self.assertEqual(category_of_argv("nohup", "nohup bash -c until [ -f x ]; do sleep 5; done"), "wait")
        self.assertEqual(category_of_argv("timeout", "timeout 60 sh -c sbatch a.sh"), "slurm")
        # its near miss: a shell given no `-c` string reads as that shell does
        self.assertEqual(category_of_argv("sudo", "sudo -n bash scripts/run_tests.sh"),
                         category_of_argv("bash", "bash scripts/run_tests.sh"))
        self.assertEqual(category_of_argv("sudo", "sudo -n bash -l -i"), "unknown")
        # `watch` waits whatever it runs, read through a shell or not, as in shell text (leftovers L4)
        self.assertEqual(category_of_argv("watch", "watch -n 5 bash -c squeue -u me"), "wait")
        self.assertEqual(category_of_argv("watch", "watch -n 5 bash -c squeue -u me"),
                         category_of('watch -n 5 bash -c "squeue -u me"'))
        self.assertEqual(category_of_argv("sudo", "sudo -n watch -n 5 sh -c pytest -q"), "wait")

    def test_a_command_name_alone_names_the_program(self):
        self.assertEqual(category_of_argv("python", "python"), "run")
        self.assertEqual(category_of_argv("sleep", None), "wait")
        self.assertEqual(category_of_argv("python3", ""), "run")
        self.assertEqual(category_of_argv("git", "git"), "explore")
        self.assertEqual(category_of_argv("npm exec @model", "npm exec @model"), "run")

    def test_a_program_no_table_names_is_run_in_an_argv_too(self):
        self.assertEqual(category_of_argv("gmx", "gmx mdrun -deffnm x"), "run")
        self.assertEqual(category_of_argv("lmp", "lmp -in in.melt"), "run")
        self.assertEqual(category_of_argv("lmp", "lmp"), "run")                # a census gives a name alone
        self.assertEqual(category_of_argv("lmp", None), "run")
        self.assertEqual(category_of_argv("stty", "stty -a"), "explore")           # a utility is not run

    def test_a_version_string_is_no_program(self):
        self.assertEqual(category_of_argv("2.1.308-3f6ee6c", None), "unknown")    # the agent's own binary

    def test_nothing_recorded_is_unknown(self):
        self.assertEqual(category_of_argv(None, None), "unknown")
        self.assertEqual(category_of_argv("", ""), "unknown")
        self.assertEqual(category_of_argv("zsh", None), "unknown")
        self.assertEqual(category_of_argv("bash", "/bin/bash -l -i"), "unknown")
        self.assertEqual(category_of_argv("sh", "/bin/sh -c $CODEX_REMOTE_PAYLOAD"), "unknown")


class WaitsOnTest(unittest.TestCase):
    def test_a_loop_waits_on_what_its_condition_asks(self):
        self.assertEqual(waits_on("until [ -z \"$(pgrep -f train.py)\" ]; do sleep 10; done", "wait"),
                         "process")
        self.assertEqual(waits_on("until [ -f out.json ]; do sleep 15; done", "wait"), "output")
        self.assertEqual(waits_on("while squeue -u $USER | grep -q x; do sleep 60; done", "wait"), "slurm")
        self.assertEqual(waits_on("until [ \"$(sacct -j 1 -n -o State)\" = COMPLETED ]; "
                                  "do sleep 30; done", "wait"), "slurm")
        self.assertEqual(waits_on("until grep -q done log.txt; do sleep 30; done", "wait"), "output")
        self.assertEqual(waits_on("while kill -0 123 2>/dev/null; do sleep 5; done", "wait"), "process")

    def test_a_loop_that_asks_something_else_waits_on_that_command(self):
        self.assertEqual(waits_on("until curl -s localhost:8000/health; do sleep 2; done", "wait"), "command")
        self.assertEqual(waits_on("while true; do date; sleep 5; done", "wait"), "command")

    def test_watch_waits_on_what_it_runs(self):
        self.assertEqual(waits_on("watch -n 30 sacct -j 1", "wait"), "slurm")

    def test_a_poll_waits_on_what_it_reads(self):
        self.assertEqual(waits_on("ls -la /tmp/x", "explore"), "output")
        self.assertEqual(waits_on("tail -f log.txt", "wait"), "output")
        self.assertEqual(waits_on("test -s out.json", "explore"), "output")
        self.assertEqual(waits_on("[ -f out.json ]", "explore"), "output")
        self.assertEqual(waits_on("[ -z \"$x\" ]", "explore"), "command")      # a string test names no file
        self.assertEqual(waits_on("ps aux | grep x", "explore"), "process")
        self.assertEqual(waits_on("kill -0 123", "wait"), "process")
        self.assertEqual(waits_on("wait", "wait"), "process")

    def test_a_fixed_sleep_waits_on_the_time(self):
        self.assertEqual(waits_on("sleep 5", "wait"), "sleep")
        self.assertEqual(waits_on("sleep 600; make", "wait"), "sleep")      # R58: on that sleep
        # R60: unless the rest names what it waits on
        self.assertEqual(waits_on("sleep 600; tail -5 log.txt", "wait"), "output")
        self.assertEqual(waits_on("sleep 5; tail -5 log.txt", "wait"), "output")

    def test_the_category_alone_can_say_slurm(self):
        self.assertEqual(waits_on("anything", "slurm"), "slurm")

    def test_nothing_to_read_waits_on_the_command(self):
        self.assertEqual(waits_on(None, "wait"), "command")
        self.assertEqual(waits_on("", "unknown"), "command")
        self.assertEqual(waits_on("nvidia-smi", "explore"), "command")


class ProgramListTest(unittest.TestCase):
    """What `seen` names (leftovers L3, R72): never a login shell's startup helper, and Claude's
    binary under its version name as `claude`."""

    def test_a_login_shells_helpers_are_no_program_of_a_call(self):
        for command, args in (
                # the rules' own shapes (R51)
                ("grepconf.sh", "/bin/sh /usr/libexec/grepconf.sh -c"), ("grepconf.sh", ""),
                ("grep", "grep -q /opt/puppetlabs/bin"), ("dircolors", "dircolors -b"),
                ("manpath", ""),
                # RHEL's /etc/profile and /etc/bashrc, profile.d, and Lmod's shell check
                ("id", "/usr/bin/id -un"), ("id", "/usr/bin/id -u"), ("id", "/usr/bin/id -gn"),
                ("hostname", "/usr/bin/hostname"), ("flatpak", "flatpak --installations"),
                ("grep", "grep -qsi ^COLOR.*none /etc/GREP_COLORS"),
                ("expr", "/usr/bin/expr /usr/bin/bash : -*\\(.*\\)"),
                ("basename", "/usr/bin/basename /usr/bin/bash"),
                # the same by name where schema 7 kept no argv (3,557 such `id` that day), or kept
                # its parent's in its place (R72; 17 `grepconf.sh` under 2522045's open call)
                ("id", ""), ("hostname", ""), ("flatpak", ""),
                ("grepconf.sh", "bash -c squeue -h -u $USER -o %j 2>&1"), ("grepconf.sh", "-bash"),
                ("id", "bash -c squeue -h -u $USER -o %j 2>&1"), ("hostname", "/bin/bash -c -l ls")):
            with self.subTest(args=args or command):
                self.assertTrue(login_helper(command, args))

    def test_the_same_programs_run_by_hand_are_the_calls(self):
        for command, args in (("id", "id -un"), ("hostname", "hostname"), ("id", "id"),
                              ("hostname", "hostname -s"), ("id", "/usr/bin/id -G"),
                              ("flatpak", "flatpak list"), ("grep", "grep -q x f"),
                              ("basename", "basename /usr/bin/python3"), ("sleep", "sleep 20"),
                              ("bash", "/bin/bash -c -l ls")):
            with self.subTest(args=args or command):
                self.assertFalse(login_helper(command, args))

    def test_claudes_binary_under_its_version_name_is_claude(self):
        for command, want in (("2.1.311", "claude"), ("2.1.308-3f6ee6c", "claude"),
                              ("/usr/bin/python3", "python3"), ("claude", "claude"),
                              ("claude.exe", "claude.exe"), ("python3.12", "python3.12"),
                              ("", None), (None, None)):
            with self.subTest(command=command):
                self.assertEqual(program_name(command), want)


class CacheTest(unittest.TestCase):
    def setUp(self):
        category_of.cache_clear()
        category_of_argv.cache_clear()

    def test_a_command_line_that_recurs_is_read_once(self):
        for _ in range(3):
            self.assertEqual(category_of("squeue -u x | wc -l"), "slurm")
        info = category_of.cache_info()
        self.assertEqual((info.misses, info.hits), (1, 2))

    def test_an_argv_is_cached_on_the_command_and_the_args(self):
        for _ in range(3):
            self.assertEqual(category_of_argv("sleep", "sleep 15"), "wait")
        category_of_argv("sleep", "sleep 16")
        category_of_argv("sleepy", "sleep 15")                  # another command, the same args
        info = category_of_argv.cache_info()
        self.assertEqual((info.misses, info.hits), (3, 2))

    def test_each_cache_is_bounded(self):
        self.assertEqual(category_of.cache_info().maxsize, 65536)
        self.assertEqual(category_of_argv.cache_info().maxsize, 65536)

    def test_a_text_past_4_kb_is_read_around_the_cache(self):          # final review Minor
        # So each cache holds 65,536 texts of 4 KB at most, not of 100,000 characters
        long = "ls " + "x" * 5000
        for _ in range(2):
            self.assertEqual(category_of(long), "explore")
        self.assertEqual(category_of.cache_info().currsize, 0)
        self.assertEqual(category_of_argv("bash", "bash -c " + long), "explore")
        self.assertEqual(category_of_argv.cache_info().currsize, 0)
        self.assertEqual(category_of("ls " + "x" * 4000), "explore")      # under it: cached
        self.assertEqual(category_of.cache_info().currsize, 1)

    def test_the_cache_does_not_change_an_answer(self):
        for text, want in CASES + UNKNOWN_PROGRAMS + NOT_COMMANDS:
            self.assertEqual(category_of(text), want)                    # a miss
            self.assertEqual(category_of(text), want)                    # a hit
            self.assertEqual(category_of.__wrapped__(text), want)        # the function itself


class HashTest(unittest.TestCase):
    def test_any_whitespace_collapses(self):
        self.assertEqual(norm_hash("a\n b\t\tc"), norm_hash("a b c"))
        self.assertEqual(norm_hash("  a b  "), norm_hash("a b"))

    def test_different_commands_differ(self):
        self.assertNotEqual(norm_hash("squeue -u a"), norm_hash("squeue -u b"))

    def test_empty_is_zero(self):
        self.assertEqual(norm_hash(None), 0)
        self.assertEqual(norm_hash("   \n"), 0)

    def test_a_hash_is_an_unsigned_32_bit_int(self):
        h = norm_hash("café \ud800 x")
        self.assertIsInstance(h, int)
        self.assertTrue(0 <= h < 2 ** 32)


# ------------------------------------------------------------------ which processes are tool calls
T = 1_793_000_000
SNAP8 = SNAP.replace("/bin/zsh -c", "/bin/bash -c")      # schema 8 records the shell as `bash`
_NORMALIZER = Normalizer(cfg())


def norm(r):
    """As production does it, so `_sv`, `_command`, and `_role` are set."""
    return _NORMALIZER.normalize(r)


class FakeProcs:
    def __init__(self, m):
        self.m = m

    def get(self, h, p):
        return self.m.get(p)


class _NeverAsked:
    def get(self, h, p):
        raise AssertionError("the index was asked about %r" % (p,))


class ParentTest(unittest.TestCase):
    def test_schema8_tool_shell_is_a_direct_child(self):        # ppid == agent_pid
        self.assertTrue(parent_is_agent(norm(rec("exit", T, 201, 100, "bash", SNAP8 + "'ls'",
            agent_pid=100, tree_root=100, schema_version=8, depth=1)), None))

    def test_schema8_grandchild_is_not(self):
        self.assertFalse(parent_is_agent(norm(rec("exit", T, 202, 201, "ls", "ls",
            agent_pid=100, tree_root=100, schema_version=8, depth=2)), None))

    def test_schema7_tagged_shell_falls_back_to_the_tree_root(self):
        self.assertTrue(parent_is_agent(norm(rec("exit", T, 200, 100, "zsh", SNAP + "'ls'",
            agent_pid=200, tree_root=100, is_agent=True)), None))

    def test_schema7_untagged_child_of_a_tool_shell_is_not(self):   # agent_pid is the shell there
        self.assertFalse(parent_is_agent(norm(rec("exit", T, 201, 200, "git", "git status",
            agent_pid=200, tree_root=100)), None))

    def test_schema7_asks_the_index_when_it_has_the_parent(self):
        r = norm(rec("exit", T, 200, 150, "zsh", SNAP + "'ls'", agent_pid=200, tree_root=90, is_agent=True))
        self.assertTrue(parent_is_agent(r, FakeProcs({150: [90, "agent"]})))
        self.assertFalse(parent_is_agent(r, FakeProcs({150: [90, "tool-shell"]})))

    def test_the_index_is_asked_for_this_host_and_the_parent(self):
        asked = []

        class Spy:
            def get(self, host, pid):
                asked.append((host, pid))

        parent_is_agent(norm(rec("exit", T, 200, 150, "zsh", SNAP + "'ls'", agent_pid=200,
                                 tree_root=90, is_agent=True, host="node7")), Spy())
        self.assertEqual(asked, [("node7", 150)])

    def test_schema7_an_index_that_lacks_the_parent_falls_back_to_the_tree_root(self):
        shell = norm(rec("exit", T, 200, 100, "zsh", SNAP + "'ls'", agent_pid=200, tree_root=100,
                         is_agent=True))
        child = norm(rec("exit", T, 201, 200, "git", "git status", agent_pid=200, tree_root=100))
        self.assertTrue(parent_is_agent(shell, FakeProcs({})))
        self.assertFalse(parent_is_agent(child, FakeProcs({})))

    def test_schema7_a_tool_shell_that_is_its_own_tree_root_is_not_the_agent(self):
        # an orphaned loop shell is the root of its own tree: its `sleep`s have ppid == tree_root
        sleep = norm(rec("exit", T, 301, 300, "sleep", "sleep 15", agent_pid=300, tree_root=300))
        self.assertTrue(parent_is_agent(sleep, None))                                  # no index: guess
        self.assertFalse(parent_is_agent(sleep, FakeProcs({300: [1, "tool-shell"]})))  # the index knows

    def test_schema7_a_held_parent_with_no_role_is_not_the_agent(self):
        r = norm(rec("exit", T, 201, 150, "git", "git status", agent_pid=200, tree_root=150))
        self.assertFalse(parent_is_agent(r, FakeProcs({150: [100, None]})))

    def test_schema8_a_tool_shell_root_has_no_direct_children(self):
        # a tool shell that outlived its agent is its own agent_pid, and what its loop runs is its
        # call's, so it must reach the pending join and not be read as the agent's own
        pgrep = norm(rec("exit", T, 600, 500, "pgrep", "pgrep -f train.py", agent_pid=500, tree_root=500,
                         schema_version=8, depth=1))
        self.assertFalse(parent_is_agent(pgrep, FakeProcs({500: [1, "tool-shell"]})))
        self.assertTrue(parent_is_agent(pgrep, FakeProcs({500: [1, "agent"]})))
        self.assertTrue(parent_is_agent(pgrep, FakeProcs({500: [1, "infra"]})))
        self.assertTrue(parent_is_agent(pgrep, None))

    def test_schema8_only_a_tool_calls_root_has_no_direct_children(self):
        # the roles of a process that is one tool call's. Any other root runs the agent's own chores:
        # an agent, the server that hosts sessions (infra), a client, a helper, a probe, an IDE
        tool_call = {"tool-shell", "sandbox", "embedded-tool"}
        self.assertLessEqual(tool_call, set(ROLES))                          # the names are roles.py's
        pgrep = norm(rec("exit", T, 600, 500, "pgrep", "pgrep -f train.py", agent_pid=500, tree_root=500,
                         schema_version=8, depth=1))
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(parent_is_agent(pgrep, FakeProcs({500: [1, role]})), role not in tool_call)
        self.assertTrue(parent_is_agent(pgrep, FakeProcs({500: [1, None]})))      # held, never tagged

    def test_schema8_a_root_the_index_lacks_is_taken_for_the_agent(self):
        pgrep = norm(rec("exit", T, 600, 500, "pgrep", "pgrep -f train.py", agent_pid=500, tree_root=500,
                         schema_version=8, depth=1))
        self.assertTrue(parent_is_agent(pgrep, FakeProcs({})))
        self.assertTrue(parent_is_agent(pgrep, FakeProcs({600: [500, "helper"]})))   # it knows only the child

    def test_schema8_the_index_is_asked_for_this_host_and_the_agent(self):
        asked = []

        class Spy:
            def get(self, host, pid):
                asked.append((host, pid))

        parent_is_agent(norm(rec("exit", T, 600, 500, "pgrep", "pgrep -f x", agent_pid=500, tree_root=500,
                                 schema_version=8, depth=1, host="node7")), Spy())
        self.assertEqual(asked, [("node7", 500)])

    def test_schema8_what_is_not_a_direct_child_is_decided_without_the_index(self):
        grandchild = norm(rec("exit", T, 602, 601, "ls", "ls", agent_pid=500, tree_root=500,
                              schema_version=8, depth=2))
        runtime = norm(rec("exit", T, 500, 500, "claude", "claude", agent_pid=500, tree_root=500,
                           is_agent=True, schema_version=8, depth=0))
        self.assertFalse(parent_is_agent(grandchild, _NeverAsked()))
        self.assertFalse(parent_is_agent(runtime, _NeverAsked()))

    def test_schema8_the_real_index_says_what_the_root_is(self):
        def root(pid, command, args):
            return norm(rec("residency", T, pid, 1, command, args, agent_pid=pid, tree_root=pid,
                            is_agent=True, schema_version=8, depth=0))

        procs = ProcIndex(cfg())
        procs.feed(root(500, "bash", SNAP8 + "'until pgrep -f x; do sleep 15; done'"))   # an orphaned tool shell
        procs.feed(root(100, "claude", "claude"))
        loop = norm(rec("exit", T, 600, 500, "sleep", "sleep 15", agent_pid=500, tree_root=500,
                        schema_version=8, depth=1))
        call = norm(rec("exit", T, 201, 100, "bash", SNAP8 + "'ls'", agent_pid=100, tree_root=100,
                        schema_version=8, depth=1))
        self.assertEqual((procs.get("h1", 500)[1], procs.get("h1", 100)[1]), ("tool-shell", "agent"))
        self.assertFalse(parent_is_agent(loop, procs))
        self.assertTrue(parent_is_agent(call, procs))

    def test_a_parent_that_is_no_process_is_never_the_agent(self):
        for sv in (7, 8):
            for ppid in (None, 0, 1):
                with self.subTest(schema=sv, ppid=ppid):
                    r = norm(rec("exit", T, 201, ppid, "ls", "ls", agent_pid=ppid, tree_root=ppid,
                                 schema_version=sv))
                    self.assertFalse(parent_is_agent(r, None))
                    self.assertFalse(parent_is_agent(r, FakeProcs({ppid: [1, "agent"]})))

    def test_schema8_the_agent_runtime_is_not_a_child_of_itself(self):
        r = norm(rec("exit", T, 100, 100, "claude", "claude", agent_pid=100, tree_root=100,
                     is_agent=True, schema_version=8, depth=0))
        self.assertFalse(parent_is_agent(r, None))

    def test_schema8_a_record_with_no_agent_is_no_childs(self):
        r = norm(rec("exit", T, 201, 100, "ls", "ls", agent_pid=None, tree_root=100, schema_version=8))
        self.assertFalse(parent_is_agent(r, None))


DIRECT = [  # (agent_type, command, args) -> classify_direct result
    ("claude_code", "zsh", SNAP + "'git status'", ("call", "bash")),
    ("claude_code", "bash", "/bin/bash -c -l shopt -u extglob 2>/dev/null || true && eval 'ls -la' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd", ("call", "bash")),
    ("claude_code", "2.1.291", "rg --no-config --files /home/a", ("call", "search")),
    ("claude_code", "claude.exe", "ugrep -G --ignore-files --hidden -I --exclude-dir=.git --exclude-dir=.svn --exclude-dir=.hg --exclude-dir=.bzr --exclude-dir=.jj --exclude-dir=.sl -qE passed|failed /tmp/t.output", ("call", "search")),
    ("claude_code", "git", "git -c core.fsmonitor= -c core.hooksPath=/dev/null status", ("housekeeping", "git")),
    ("claude_code", "realpath", "realpath /madsys/x", ("housekeeping", "realpath")),
    ("codex", "bash", "/usr/bin/bash -lc pytest -q", ("call", "shell")),
    ("codex", "cat", "cat /n/home/x/AGENTS.md", ("call", "bare")),
    ("codex", "git", "git -c safe.bareRepository=explicit rev-parse HEAD", ("housekeeping", "git (codex)")),
    ("codex", "lsb_release", "/bin/sh /usr/bin/lsb_release -a", ("housekeeping", "lsb_release")),
    ("codex", "getconf", "getconf LONG_BIT", ("housekeeping", "getconf")),
    ("codex", "sh", "/bin/sh -c printf \"%s\" \"$HOME\"", ("housekeeping", "printf $HOME")),
    ("codex", "codex", "/h/.codex/packages/app-server-daemon/releases/0.165.0/bin/codex --version", ("housekeeping", "probe")),
    ("codex", "codex-linux-sandbox", "codex-linux-sandbox --sandbox-policy-cwd /h --command-cwd /", ("call", "wrapper")),
    ("cursor", "bash", "bash -lc ls", ("none", "")),
]

# The shapes the captures have, each beside its nearest miss, so a rule that is widened or narrowed
# shows here. Each row is a direct child of its agent.
DIRECT_SHAPES = [
    # Claude Code. A Bash tool call is a shell with a shell snapshot in its argv, or, with no
    # snapshot, Claude's own cwd capture (`eval '<cmd>' ... pwd -P >| /tmp/claude-<id>-cwd`)
    ("claude_code", "bash", SHOPT + "'ls -la' < /dev/null && pwd -P >| /tmp/claude-61004-cwd", ("call", "bash")),
    ("claude_code", "bash", "/bin/bash -c source /home/a/.claude/shell-snapshots/snapshot-bash-1.sh 2>/dev/null"
     " || true && shopt -u extglob 2>/dev/null || true && eval 'ls' < /dev/null && pwd -P >| /tmp/claude-1a2b-cwd",
     ("call", "bash")),
    ("claude_code", "bash", SNAP8 + "'ls'", ("call", "bash")),
    ("claude_code", "zsh", "/bin/zsh -c ls", ("housekeeping", "zsh")),       # no snapshot, no cwd capture
    ("claude_code", "sh", "sh -c ls -1 /home/a/.claude/projects/p", ("housekeeping", "sh")),
    ("claude_code", "cat", "cat /home/a/.claude/shell-snapshots/snapshot-zsh-1.sh", ("housekeeping", "cat")),
    # A shell whose argv was not kept, or was kept only as far as its own options, is a call with no
    # command text (R64, spec finding 7): on boslogin06 on 2026-10-28 that adds 2,038 calls, the
    # Claude runtimes' direct children that are such shells. A shell that kept a word past them is not.
    ("claude_code", "bash", "", ("call", "bash")),
    ("claude_code", "bash", "/bin/bash -c", ("call", "bash")),
    ("claude_code", "bash", "/bin/bash -c -l", ("call", "bash")),
    ("claude_code", "zsh", "/bin/zsh -o pipefail -c", ("call", "bash")),
    ("claude_code", "bash", "bash /home/a/.claude/hooks/pre.sh", ("housekeeping", "bash")),   # a hook
    ("claude_code", "bash", "/bin/bash -c -l ls", ("housekeeping", "bash")),
    ("claude_code", "python3", "", ("housekeeping", "python3")),      # no argv, and no shell
    # Grep and Glob: the agent binary run under another argv[0]
    ("claude_code", "claude", "ugrep -G --ignore-files --hidden -I -qE passed /tmp/t.output", ("call", "search")),
    ("claude_code", "2.1.308-3f6ee6c", "bfs /home/a -name *.py", ("call", "search")),
    ("claude_code", "2.1.308-3f6ee6c", "/opt/claude/rg --no-config -n x", ("call", "search")),
    ("claude_code", "claude", "claude --version", ("housekeeping", "claude")),
    # `claude.exe`, Claude's binary under that file name (its own argv names
    # `nanoclaw/.tools/bin/claude.exe` on boslogin06), does the same: about 4,000 direct children
    # there, as `ugrep`, `bfs`, and `rg --version`
    ("claude_code", "claude.exe", "bfs -S dfs -regextype findutils-default /data -name *.mp4 -newer /stamp",
     ("call", "search")),
    ("claude_code", "claude.exe", "rg --version", ("call", "search")),
    ("claude_code", "claude.exe", "claude.exe --version", ("housekeeping", "claude.exe")),
    ("claude_code", "bwrap", "bwrap --ro-bind / / /bin/bash -c ls", ("call", "wrapper")),
    # the desktop app's and the user's own commands
    ("claude_code", "pgrep", "pgrep -f train.py", ("housekeeping", "pgrep")),
    ("claude_code", "command-guard.sh", "/bin/bash /home/a/.claude/hooks/command-guard.sh",
     ("housekeeping", "command-guard.sh")),
    ("claude_code", "/usr/bin/git", "/usr/bin/git status", ("housekeeping", "git")),     # a basename
    ("claude_code", None, "", ("housekeeping", "unnamed")),
    # Codex runs most of the model's commands with no shell, and runs its own too
    ("codex", "bash", "/bin/bash -lc sed -n 1,80p src/a.py", ("call", "shell")),
    ("codex", "zsh", "/bin/zsh -lc ls", ("call", "shell")),
    ("codex", "sh", "/bin/sh -c ls -la", ("call", "shell")),
    ("codex", "rg", "rg -n TODO src", ("call", "bare")),
    ("codex", "python3", "python3 --version", ("call", "bare")),               # the model's, not a probe
    ("codex", "git", "git status --short", ("call", "bare")),
    ("codex", "git", "git -c core.pager=cat log -3", ("call", "bare")),
    ("codex", "git", "", ("call", "bare")),
    ("codex", "bash", "bash scripts/run_tests.sh", ("call", "bare")),           # a shell with no -c string
    ("codex", "bash", "/bin/bash -lc cat /home/a/.codex/AGENTS.md", ("call", "shell")),
    ("codex", "bash", "/bin/bash -lc source /home/a/venv/bin/activate && pytest", ("call", "shell")),
    # The remote launcher's login shell, a subshell of which repeats its argv: the payload it execs
    # is the VS Code extension's own (`command -v codex`, `codex --version`, the app-server proxy).
    ("codex", "bash", "/bin/bash -l -i -c CODEX_HOME=\"${CODEX_HOME:-$HOME/.codex}\"; export CODEX_HOME; "
     "exec /bin/sh -c \"$CODEX_REMOTE_PAYLOAD\"", ("housekeeping", "remote launch")),
    ("codex", "bash", "/bin/bash -lc echo $CODEX_REMOTE_PAYLOAD_DIR", ("call", "shell")),   # another name
    ("codex", "bash", "/bin/bash -lc cat payload.txt", ("call", "shell")),
    ("codex", "codex", "/h/.codex/bin/codex --codex-run-as-fs-helper", ("call", "bare")),
    ("codex", "codex-linux-sandbox", "/h/.codex/tmp/arg0/codex-linux-sandbox --sandbox-policy-cwd /h"
     " --command-cwd /h", ("call", "wrapper")),
    ("codex", "codex", "codex-linux-sandbox --sandbox-policy-cwd /h --command-cwd /h", ("call", "wrapper")),
    ("codex", "codex-linux-san", "/proc/self/exe --sandbox-policy-cwd /h --command-cwd /h", ("call", "wrapper")),
    ("codex", "codex", "bwrap --as-pid-1 --new-session --ro-bind / / /bin/bash -lc ls", ("call", "wrapper")),
    ("codex", "bwrap", "bwrap --as-pid-1 --new-session --ro-bind / / /bin/bash -lc ls", ("call", "wrapper")),
    # Codex's own commands
    ("codex", "sh", "/bin/sh -c if command -v flock >/dev/null 2>&1; then exit 1; else exit 2; fi",
     ("housekeeping", "flock check")),
    ("codex", "sh", "/bin/sh -s", ("housekeeping", "sh -s")),
    ("codex", "bash", "/bin/bash -s", ("call", "bare")),                      # the chore is Codex's own `sh`
    ("codex", "bash", "/usr/bin/bash -c set -e; . \"/n/home10/u/.codex/shell_snapshots/01a1.tmp-1793198831\"",
     ("housekeeping", "snapshot check")),
    ("codex", "bash", "/bin/bash -c source /home/a/.codex/shell_snapshots/s.sh", ("housekeeping", "snapshot check")),
    ("codex", "bash", "/bin/bash -c ls &&\n  . ~/.codex/shell_snapshots/s.sh", ("housekeeping", "snapshot check")),
    # Near misses (final review Minor): `source` and `.` only where a command starts
    ("codex", "bash", "/bin/bash -c cp -r . ~/.codex/backup", ("call", "shell")),
    ("codex", "bash", "/bin/bash -lc rsync -a . /h/.codex/backup/", ("call", "shell")),
    ("codex", "bash", "/bin/bash -lc echo source /h/.codex/x.sh", ("call", "shell")),
    ("codex", "bash", "/bin/bash -lc echo do . /h/.codex/x.sh", ("call", "shell")),
    # A newer Codex's wrapper around the model's own command (boslogin06, 2026-10-28, cut short)
    ("codex", "bash", "/usr/bin/bash -c __CODEX_SNAPSHOT_OVERRIDE_SET_0=\"${CODEX_THREAD_ID+x}\"\n\n"
     "if . '/n/home12/u/.codex/shell_snapshots/01a1.sh' >/dev/null 2>&1; then :; fi\n\n"
     "exec '/usr/bin/bash' -c 'cat artifacts/summary.json'", ("call", "shell")),
    ("codex", "git", "git -c safe.bareRepository=explicit -c core.hooksPath=/dev/null -c core.fsmonitor= rev-parse"
     " --show-toplevel", ("housekeeping", "git (codex)")),
    ("codex", "sh", "/bin/sh -c printf '%s' \"$HOME\"", ("housekeeping", "printf $HOME")),
    ("codex", "codex", "codex --version", ("housekeeping", "probe")),
    ("codex", "codex", "codex login status", ("housekeeping", "probe")),
    ("codex", "codex", "codex auth status", ("housekeeping", "probe")),
    ("codex", "node", "node /home/a/.nvm/versions/node/v22.22.0/bin/codex --version", ("housekeeping", "probe")),
    ("codex", "codex", "codex exec fix the tests", ("call", "bare")),           # a nested run is the model's
    # what that login shell's profile scripts ran while it started, each by its whole argv
    ("codex", "manpath", "manpath", ("housekeeping", "login shell startup")),
    ("codex", "dircolors", "dircolors -b", ("housekeeping", "login shell startup")),
    ("codex", "lesspipe", "/bin/sh /usr/bin/lesspipe", ("housekeeping", "login shell startup")),
    ("codex", "locale", "locale", ("housekeeping", "login shell startup")),
    ("codex", "locale-check", "/usr/bin/locale-check C.UTF-8", ("housekeeping", "login shell startup")),
    ("codex", "groups", "groups", ("housekeeping", "login shell startup")),
    ("codex", "grepconf.sh", "/bin/sh /usr/libexec/grepconf.sh -c", ("housekeeping", "login shell startup")),
    ("codex", "grep", "grep -q /opt/puppetlabs/bin", ("housekeeping", "login shell startup")),
    ("codex", "grep", "grep --color=auto -q /opt/puppetlabs/puppet/share/man",
     ("housekeeping", "login shell startup")),
    ("codex", "awk", "awk BEGIN { for (name in ENVIRON) if (toupper(name) == \"NPM_CONFIG_PREFIX\") "
     "{ print name; break } }", ("housekeeping", "login shell startup")),
    ("codex", "awk", "awk BEGIN {\n    if (ARGV[1] == \"\" || ARGV[2] == \"\") exit(1)\n    split(ARGV[1], a, "
     "/\\./);\n  } 22.22.0 0", ("housekeeping", "login shell startup")),
    ("codex", "awk", "awk -v NVM_DIR=/home/a/.nvm -v RS=: \n  index($0, NVM_DIR) == 1 {\n path = 1 }",
     ("housekeeping", "login shell startup")),
    ("codex", "locale", "locale -a", ("call", "bare")),                        # asked, not the profile's
    ("codex", "groups", "groups alice", ("call", "bare")),
    ("codex", "dircolors", "dircolors -p", ("call", "bare")),
    ("codex", "grep", "grep -q /opt/puppetlabs/bin /etc/profile", ("call", "bare")),
    ("codex", "awk", "awk BEGIN { print 1 }", ("call", "bare")),
    ("codex", "lesspipe", "/bin/sh /usr/bin/lesspipe notes.gz", ("call", "bare")),
    # what the app-server daemon runs for itself
    ("codex", "git", "/usr/bin/git --version", ("housekeeping", "version check")),
    ("codex", "git", "git version", ("housekeeping", "version check")),
    ("codex", "pwd", "pwd -P", ("housekeeping", "path check")),
    ("codex", "ps", "ps -p 1601264 -o stat= -o lstart=", ("housekeeping", "process check")),
    ("codex", "rm", "rm -f -- /home/a/.codex/app-server-control/forwarded-ssh-agent.sock",
     ("housekeeping", "control socket")),
    # the launcher's payload, the sandbox's self-test, the snapshot writer, a helper with no argv
    ("codex", "sh", "/bin/sh -c printf '%b' '\\043\\354'; PATH=\"${CODEX_INSTALL_DIR:-$HOME/.local/bin}:$PATH\";"
     " export PATH; codex --version", ("housekeeping", "remote launch")),
    ("codex", "bwrap", "/usr/bin/bwrap --unshare-user --unshare-net --ro-bind / / /bin/true",
     ("housekeeping", "sandbox probe")),
    ("codex", "bwrap", "bwrap --ro-bind / / /bin/bash -lc pytest || true", ("call", "wrapper")),
    ("codex", "bwrap", "bwrap --ro-bind / / /bin/true --version", ("call", "wrapper")),
    ("codex", "bash", "/usr/bin/bash -lc if [ -z \"$BASH_ENV\" ] && [ -r \"$HOME/.bashrc\" ]; then\n"
     "  . \"$HOME/.bashrc\"\nfi\necho '# Snapshot file'\necho '# Unset all aliases'", ("housekeeping", "snapshot write")),
    ("codex", "bash", "/usr/bin/bash -lc echo '# Results'", ("call", "shell")),
    ("codex", "grepconf.sh", "", ("housekeeping", "login shell startup")),
    ("codex", "locale", "", ("call", "bare")),                                # a name alone says too little
    ("codex", "git", "git version --build-options", ("call", "bare")),
    ("codex", "pwd", "pwd", ("call", "bare")),
    ("codex", "ps", "ps -p 1601264 -o pid,etime", ("call", "bare")),
    ("codex", "rm", "rm -f -- /home/a/proj/out.sock", ("call", "bare")),
    ("codex", "sh", "/bin/sh -c printf \"%s\\n\" \"$HOME\"", ("call", "shell")),   # not the exact chore
    ("codex", "sh", "/bin/sh -c ls \"$HOME\"", ("call", "shell")),
    # an agent type with no recognized calls gets none, whatever the process is
    ("cursor", "zsh", SNAP + "'git status'", ("none", "")),
    ("copilot", "git", "git status", ("none", "")),
    ("vscode", "bash", "bash -lc ls", ("none", "")),
    (None, "bash", "bash -lc ls", ("none", "")),
    ("", "bash", "bash -lc ls", ("none", "")),
]


class DirectTest(unittest.TestCase):
    def test_each_direct_child_shape(self):
        for at, c, a, want in DIRECT:
            with self.subTest(args=a):
                self.assertEqual(classify_direct({"command": c, "_command": c, "args": a}, at), want)

    def test_each_shape_and_its_nearest_miss(self):
        for at, c, a, want in DIRECT_SHAPES:
            with self.subTest(agent=at, command=c, args=a):
                self.assertEqual(classify_direct({"command": c, "_command": c, "args": a}, at), want)

    def test_a_sandbox_that_runs_true_alone_is_its_self_test(self):
        for payload, want in (("/usr/bin/true", True), ("true", True), (" /bin/true ", True),
                              ("true --help", False), ("/bin/bash -lc true", False), ("", False),
                              (None, False), ("pytest || true", False)):
            with self.subTest(payload=payload):
                self.assertEqual(is_sandbox_probe(payload), want)

    def test_a_codex_chore_is_known_by_its_argv_alone(self):
        # What ran under a process that never exited keeps only its argv (PendingJoin's P_TEXT).
        for argv, want in (("/bin/sh -s", "sh -s"), ("/bin/sh /usr/bin/lsb_release -a", "lsb_release"),
                           ("lsb_release -a", "lsb_release"), ("/usr/bin/git --version", "version check"),
                           ("git -c safe.bareRepository=explicit rev-parse HEAD", "git (codex)"),
                           ("/bin/bash -l -i -c exec /bin/sh -c \"$CODEX_REMOTE_PAYLOAD\"", "remote launch"),
                           ("manpath", "login shell startup"), ("codex --version", "probe"),
                           ("/bin/bash -lc pytest -q", None), ("sleep 15", None), ("", None), (None, None)):
            with self.subTest(argv=argv):
                self.assertEqual(codex_chore_of(argv), want)

    def test_a_record_the_normalizer_made_is_read_the_same_way(self):
        r = norm(rec("exit", T, 201, 100, "bash", SHOPT + "'ls -la' < /dev/null && pwd -P >| /tmp/claude-1-cwd",
                     agent_pid=100, tree_root=100, schema_version=8, depth=1))
        self.assertEqual(classify_direct(r, "claude_code"), ("call", "bash"))
        r = norm(rec("exit", T, 301, 300, "lsb_release", "/bin/sh /usr/bin/lsb_release -a", agent_pid=300,
                     tree_root=300, agent_type="codex", schema_version=8))
        self.assertEqual(classify_direct(r, "codex"), ("housekeeping", "lsb_release"))

    def test_the_executable_is_the_command_not_argv0(self):
        # `lsb_release` is a script: its comm is the script, its argv[0] the interpreter
        self.assertEqual(classify_direct({"_command": "lsb_release", "args": "/bin/sh /usr/bin/lsb_release -a"},
                                         "codex"), ("housekeeping", "lsb_release"))
        # a version-named comm re-running the binary as ripgrep, and the pre-v7 key
        self.assertEqual(classify_direct({"comm": "2.1.291", "args": "rg --files"}, "claude_code"),
                         ("call", "search"))

    def test_with_no_command_recorded_argv0_names_the_program(self):
        self.assertEqual(classify_direct({"args": SNAP + "'ls'"}, "claude_code"), ("call", "bash"))
        self.assertEqual(classify_direct({"args": "git status"}, "claude_code"), ("housekeeping", "git"))
        self.assertEqual(classify_direct({"args": "/usr/bin/bash -lc ls"}, "codex"), ("call", "shell"))

    def test_only_two_agent_types_have_calls(self):
        self.assertEqual(PHASE_AGENTS, frozenset({"claude_code", "codex"}))

    def test_fs_helper(self):
        self.assertTrue(is_fs_helper({"args": "/h/bin/codex --codex-run-as-fs-helper"}))
        self.assertFalse(is_fs_helper({"args": "codex exec"}))

    def test_the_fs_helper_as_the_captures_have_it(self):
        helper = {"_command": "codex", "args": "/n/home01/u/.codex/packages/app-server-daemon/releases/"
                  "0.165.0-x86_64-unknown-linux-musl/bin/codex --codex-run-as-fs-helper"}
        self.assertTrue(is_fs_helper(helper))
        self.assertTrue(is_fs_helper(dict(helper, command="codex")))

    def test_the_sandbox_wrappers_carry_the_helpers_flag_and_are_not_the_helper(self):
        # the helper is the command they wrap, so it is in their own argv
        flag = "--codex-run-as-fs-helper"
        for command, args in (
                ("bwrap", "bwrap --as-pid-1 --new-session --ro-bind / / /h/.codex/bin/codex " + flag),
                ("codex-linux-sandbox", "/h/.codex/tmp/arg0/codex-linux-sandbox --sandbox-policy-cwd /h "
                 "--command-cwd / -- /h/.codex/bin/codex " + flag),
                ("codex", "codex-linux-sandbox --sandbox-policy-cwd /h --command-cwd / -- codex " + flag),
                ("codex", "bwrap --as-pid-1 --ro-bind / / codex " + flag)):
            with self.subTest(command=command, args=args[:40]):
                self.assertFalse(is_fs_helper({"_command": command, "args": args}))

    def test_the_other_helper_flags_are_not_the_fs_helper(self):
        setup = "/proc/self/exe --codex-run-as-process-setup 60"
        self.assertFalse(is_fs_helper({"_command": "exe", "args": setup}))
        self.assertFalse(is_fs_helper({"_command": "codex", "args": "codex"}))
        self.assertFalse(is_fs_helper({}))
        self.assertFalse(is_fs_helper({"args": None}))

    def test_call_text(self):
        self.assertEqual(call_text({"_command": "zsh", "args": SNAP + "'git status'"}, "bash"), "git status")
        self.assertEqual(call_text({"_command": "bash", "args": "/bin/bash -c -l shopt -u extglob 2>/dev/null"
                                    " || true && eval 'ls -la' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd"},
                                   "bash"), "ls -la")
        self.assertEqual(call_text({"_command": "cat", "args": "cat /x"}, "bare"), "cat /x")
        self.assertIsNone(call_text({"_command": "codex-linux-sandbox", "args": "codex-linux-sandbox"}, "wrapper"))

    def test_call_text_of_claudes_cwd_capture_shell(self):
        self.assertEqual(call_text({"_command": "bash", "args": SHOPT + "'cd /p && make' < /dev/null"
                                    " && pwd -P >| /tmp/claude-61004-cwd"}, "bash"), "cd /p && make")
        self.assertEqual(call_text({"_command": "bash", "args": SHOPT + "'echo '\"'\"'hi'\"'\"'' < /dev/null"
                                    " && pwd -P >| /tmp/claude-61004-cwd"}, "bash"), "echo 'hi'")

    def test_call_text_of_each_kind_a_call_can_be(self):
        shell = {"_command": "bash", "args": "/usr/bin/bash -lc pytest -q"}
        self.assertEqual(call_text(shell, "shell"), "pytest -q")
        self.assertEqual(call_text({"_command": "sh", "args": "/bin/sh -c ls -la"}, "shell"), "ls -la")
        search = {"_command": "claude", "args": "ugrep -G -qE passed|failed /tmp/t.output"}
        self.assertEqual(call_text(search, "search"), "ugrep -G -qE passed|failed /tmp/t.output")
        self.assertEqual(call_text({"_command": "bash", "args": "bash scripts/run_tests.sh"}, "bare"),
                         "bash scripts/run_tests.sh")
        self.assertEqual(call_text({"_command": "cat", "args": "  cat /x  "}, "bare"), "cat /x")

    def test_call_text_is_taken_from_the_records_own_argv_only(self):
        self.assertIsNone(call_text({"_command": "cat", "args": "cat /x"}, "wrapper"))
        self.assertIsNone(call_text({"_command": "cat", "args": "cat /x"}, "housekeeping"))
        self.assertIsNone(call_text({"_command": "cat", "args": "cat /x"}, "no such kind"))

    def test_call_text_says_none_when_nothing_was_recorded(self):
        for kind in ("bash", "shell", "search", "bare", "wrapper"):
            with self.subTest(kind=kind):
                self.assertIsNone(call_text({}, kind))
                self.assertIsNone(call_text({"_command": "cat", "args": None}, kind))
                self.assertIsNone(call_text({"_command": "cat", "args": "   "}, kind))
        # a snapshot script that runs nothing, a shell that was given no command string, a command
        # cut off inside its quote, and an empty eval: no text, and never an empty string
        snapshot_only = "/bin/zsh -c source /h/.claude/shell-snapshots/s.sh"
        self.assertIsNone(call_text({"_command": "zsh", "args": snapshot_only}, "bash"))
        self.assertIsNone(call_text({"_command": "bash", "args": "bash script.sh"}, "shell"))
        self.assertIsNone(call_text({"_command": "bash", "args": SNAP8 + "'"}, "bash"))
        empty_eval = SHOPT + "'' < /dev/null && pwd -P >| /tmp/claude-1-cwd"
        self.assertIsNone(call_text({"_command": "bash", "args": empty_eval}, "bash"))

    def test_odd_records_never_raise_and_answer_in_kind(self):
        odd = [{}, {"args": None}, {"args": ""}, {"args": "   "}, {"command": None, "args": "\n"},
               {"_command": "bash", "args": "bash -c"}, {"_command": "zsh", "args": "-c"},
               {"_command": "sh", "args": "x" * 60000}, {"_command": "bash", "args": SNAP},
               {"_command": "bash", "args": SHOPT + "'"}, {"_command": "", "args": "\t\n"}]
        for r in odd:
            for at in ("claude_code", "codex", "cursor", None):
                with self.subTest(record=str(r)[:40], agent=at):
                    kind, shape = classify_direct(r, at)
                    if kind == "none":
                        self.assertEqual(shape, "")
                        self.assertNotIn(at, PHASE_AGENTS)
                    elif kind == "call":
                        self.assertIn(shape, ("bash", "search", "wrapper", "shell", "bare"))
                    else:
                        self.assertEqual(kind, "housekeeping")
                        self.assertTrue(shape)
                    is_fs_helper(r)
                    for k in ("bash", "shell", "search", "bare", "wrapper"):
                        text = call_text(r, k)
                        self.assertTrue(text is None or (isinstance(text, str) and text.strip()))


class CodexAppReadsTest(unittest.TestCase):
    """The desktop app reads its plugins' icons with `cat`, hundreds in a burst: its reads, not the
    model's calls. Only the two whole-argv forms it runs are taken, and never a sandboxed call."""

    ICON = "/n/home08/u/.codex/.tmp/plugins/plugins/slack/assets/app-icon.png"
    SKILL = "/n/home08/u/.codex/.tmp/plugins/plugins/slack/skills/slack/SKILL.md"
    SETUP = "/proc/self/exe --codex-run-as-process-setup 62 4088756 pipe  / "      # as the capture has it
    SANDBOX = ("/n/home08/u/.codex/tmp/arg0/x/codex-linux-sandbox --sandbox-policy-cwd /n/home08/u/proj "
               "--command-cwd /n/home08/u/proj")

    @staticmethod
    def rec(args, command="cat"):
        return {"command": command, "args": args, "agent_type": "codex"}

    def read(self, args, command="cat"):
        return classify_direct(self.rec(args, command), "codex")

    def test_plugin_and_marketplace_assets_are_the_apps(self):
        for a in ("/bin/cat /n/home08/u/.codex/.tmp/plugins/plugins/slack/assets/app-icon.png",
                  "/bin/cat /n/home08/u/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/"
                  "visualize/assets/visualize.svg"):
            self.assertEqual(classify_direct(self.rec(a), "codex"), ("housekeeping", "app assets"))

    def test_the_users_own_files_are_still_calls(self):
        self.assertEqual(classify_direct(self.rec("/bin/cat /n/home08/u/proj/overview.png"), "codex"),
                         ("call", "bare"))
        self.assertEqual(classify_direct(self.rec("/bin/cat /n/home08/u/.codex/config.toml"), "codex"),
                         ("call", "bare"))

    def test_the_cache_of_installed_plugins_is_the_apps_too(self):
        # the same icons, cached under the plugins a user installed (103 reads for one user on
        # boslogin06 on 2026-10-09)
        a = ("/bin/cat /n/home07/u/.codex/plugins/cache/claude-plugins-official/superpowers/6.4.1/"
             "assets/superpowers-small.svg")
        self.assertEqual(self.read(a), ("housekeeping", "app assets"))

    def test_what_else_the_cache_holds_is_still_a_call(self):
        # a skill of an installed plugin is what the model reads
        a = ("/bin/cat /n/home07/u/.codex/plugins/cache/claude-plugins-official/superpowers/6.4.1/"
             "skills/brainstorming/SKILL.md")
        self.assertEqual(self.read(a), ("call", "bare"))

    def test_the_read_caught_in_its_process_setup_stage_is_the_apps_too(self):
        # The same `cat`, recorded before its exec: the program is `exe` (16 on boslogin06 on
        # 2026-10-09) or `cat` (1), and `/bin/cat /bin/cat <file>` ends the argv.
        a = ("/proc/self/exe --codex-run-as-process-setup 62 4088756 pipe  / /bin/cat /bin/cat "
             "/n/home08/u/.codex/.tmp/plugins/plugins/github/assets/github-small.svg")
        for command in ("exe", "cat"):
            with self.subTest(command=command):
                self.assertEqual(classify_direct(self.rec(a, command), "codex"),
                                 ("housekeeping", "app assets"))

    def test_the_process_setup_stage_of_any_other_command_is_a_call(self):
        # `/bin/cat /bin/cat <file in an assets folder>` has to end it, and nothing else may follow
        for tail in ("/bin/cat /bin/cat " + self.SKILL,
                     "/bin/cat /bin/cat " + self.ICON + " /n/home08/u/proj/notes.txt",
                     "/bin/rg /bin/rg -n slack " + self.ICON,
                     "/bin/cat " + self.ICON):
            with self.subTest(tail=tail):
                self.assertEqual(self.read(self.SETUP + tail, "exe"), ("call", "bare"))

    def test_a_read_whose_argv_was_not_recorded_is_still_a_call(self):
        # Schema 7 kept no argv for 228 `cat` in one session's bursts. With nothing to name its
        # file, such a `cat` cannot be told from the model's own.
        for args in (None, "", "   "):
            with self.subTest(args=args):
                self.assertEqual(classify_direct(self.rec(args), "codex"), ("call", "bare"))

    def test_it_is_known_by_its_argv_alone(self):
        # what ran under a process that never exited keeps only its argv (PendingJoin's P_TEXT)
        self.assertEqual(codex_chore_of("/bin/cat " + self.ICON), "app assets")
        self.assertEqual(codex_chore_of(self.SETUP + "/bin/cat /bin/cat " + self.ICON), "app assets")
        self.assertEqual(codex_chore_of("/bin/cat /n/home07/u/.codex/plugins/cache/m/p/1.0/assets/a.png"),
                         "app assets")
        self.assertIsNone(codex_chore_of("/bin/cat /n/home08/u/proj/overview.png"))
        self.assertIsNone(codex_chore_of("/bin/cat " + self.SKILL))

    def test_only_those_three_folders_are_the_apps(self):
        for a in ("/bin/cat /n/home08/u/.codex/skills/request-slurm-compute/SKILL.md",   # a skill
                  "/bin/cat /n/home08/u/.codex/skills/request-slurm-compute/assets/diagram.png",
                  "/bin/cat /n/home08/u/.codex/.tmp/plugins-old/slack/assets/app-icon.png",
                  "/bin/cat /n/home08/u/.codex/.tmp/assets/app-icon.png",
                  "/bin/cat /n/home08/u/.codex/plugins/slack/assets/app-icon.png",         # not the cache
                  "/bin/cat /n/home08/u/.codex/plugins/cachex/slack/assets/app-icon.png",
                  "/bin/cat /n/home08/u/proj/my.codex/.tmp/plugins/slack/assets/app-icon.png"):
            with self.subTest(args=a):
                self.assertEqual(self.read(a), ("call", "bare"))

    def test_only_a_file_in_an_assets_folder_is_the_apps(self):
        # what else a plugin holds is what the model reads: its skills, docs, manifests, and scripts
        for a in ("/bin/cat " + self.SKILL,
                  "/bin/cat /n/home08/u/.codex/.tmp/plugins/plugins/slack/README.md",
                  "/bin/cat /n/home08/u/.codex/.tmp/bundled-marketplaces/openai-bundled/plugins/"
                  "visualize/.codex-plugin/plugin.json",
                  "/bin/cat /n/home08/u/.codex/.tmp/plugins/plugins/slack/my-assets/app-icon.png",
                  "/bin/cat /n/home08/u/.codex/.tmp/plugins/plugins/slack/assetsx/app-icon.png",
                  "/bin/cat /n/home08/u/.codex/.tmp/plugins/plugins/slack/assets"):
            with self.subTest(args=a):
                self.assertEqual(self.read(a), ("call", "bare"))

    def test_only_the_whole_argv_the_app_runs_is_taken(self):
        # `cat <one file>` and nothing else. The model's `rg`, `sed`, or `cat` of a plugin's skill, its
        # `python3` of a plugin's script, a `cat` with an option or a second file, and a relative path
        # are calls, even when the file is in an assets folder.
        for command, a in (("rg", "rg -n description " + self.SKILL),
                           ("sed", "/usr/bin/sed -n 1,80p " + self.SKILL),
                           ("cat", "/bin/cat " + self.SKILL),
                           ("python3", "python3 /n/home08/u/.codex/.tmp/plugins/plugins/slack/"
                            "scripts/sync.py"),
                           ("rg", "rg -n slack " + self.ICON),
                           ("file", "/usr/bin/file " + self.ICON),
                           ("head", "head -c 64 " + self.ICON),
                           ("cat", "/bin/cat -n " + self.ICON),
                           ("cat", "/bin/cat " + self.ICON + " /n/home08/u/proj/notes.txt"),
                           ("cat", "/bin/cat .codex/.tmp/plugins/plugins/slack/assets/app-icon.png")):
            with self.subTest(args=a):
                self.assertEqual(self.read(a, command), ("call", "bare"))

    def test_a_shell_string_or_a_sandboxed_call_is_not_the_apps(self):
        # The app runs its `cat` itself, with no shell and no sandbox. A shell given a command string
        # is the model's, and so is whatever names the sandbox: its wrapper however it was recorded,
        # and the process-setup stage that starts it, whose command and argv[0] are both `exe`.
        stage = "/proc/self/exe --codex-run-as-process-setup 67 4088756 pipe /n/home08/u/proj "
        for command, args, want in (
                ("bash", "/usr/bin/bash -lc cat " + self.ICON, ("call", "shell")),
                ("codex-linux-sandbox", self.SANDBOX + " -- /bin/cat " + self.ICON, ("call", "wrapper")),
                ("codex", "codex-linux-sandbox --sandbox-policy-cwd /n/home08/u/proj --command-cwd "
                 "/n/home08/u/proj -- /bin/cat " + self.ICON, ("call", "wrapper")),
                ("bwrap", "bwrap --as-pid-1 --new-session --ro-bind / / /bin/cat " + self.ICON,
                 ("call", "wrapper")),
                ("exe", stage + self.SANDBOX + " -- /bin/cat " + self.ICON, ("call", "bare")),
                # even when what follows the sandbox ends as the app's own argv does
                ("exe", stage + self.SANDBOX + " -- /bin/cat /bin/cat " + self.ICON, ("call", "bare")),
                ("exe", stage + "bwrap --ro-bind / / /bin/cat /bin/cat " + self.ICON, ("call", "bare")),
                # the process is named for the wrapper, and the argv it kept is its payload's
                ("bwrap", "/bin/cat " + self.ICON, ("call", "wrapper")),
                ("codex-linux-san", "/bin/cat " + self.ICON, ("call", "wrapper"))):
            with self.subTest(command=command, args=args[-50:]):
                self.assertEqual(self.read(args, command), want)


class SessionShapesTest(unittest.TestCase):
    """What Task 5 asks of each exit record: is its parent the agent, and if it is, what is it."""

    def verdicts(self, recs, agent_type, procs=None):
        out = {}
        for r in map(norm, recs):
            out[r["pid"]] = parent_is_agent(r, procs) and classify_direct(r, agent_type)
        return out

    def test_a_claude_session_in_schema_7(self):
        recs = [r for r in claude_session(T) if r["event"] == "exit"]
        self.assertEqual(self.verdicts(recs, "claude_code"), {
            201: False,                         # git, under the first tool shell
            200: ("call", "bash"),              # the first tool shell, tagged, so its own agent_pid
            210: ("call", "search"),            # Grep: the binary re-run as rg, under the agent
            301: False,
            300: ("call", "bash")})

    def test_a_claude_session_in_schema_8(self):
        def at(pid, ppid, command, args, depth):
            return rec("exit", T + pid, pid, ppid, command, args, agent_pid=100, tree_root=100,
                       schema_version=8, depth=depth)
        recs = [
            at(202, 201, "git", "git status", 2),                                  # under the tool shell
            at(203, 201, "2.1.308-3f6ee6c", "ugrep -G -qE passed /tmp/t.output", 2),   # so is a grep there
            at(201, 100, "bash", SNAP8 + "'git status; ugrep passed f'", 1),
            at(204, 100, "2.1.308-3f6ee6c", "rg --no-config --files /home/alice", 1),
            at(205, 100, "git", "git -c core.fsmonitor= -c core.hooksPath=/dev/null status", 1),
            at(206, 100, "realpath", "realpath /home/alice/proj", 1),
            at(207, 100, "command-guard.sh", "/bin/bash /home/alice/.claude/hooks/command-guard.sh", 1),
            at(208, 100, "claude.exe", "ugrep -G --ignore-files -qE passed|failed /tmp/t.output", 1),
            at(210, 209, "claude.exe", "ugrep -G --ignore-files -qE passed|failed /tmp/t.output", 2),
            at(209, 100, "bash", SNAP8 + "'ugrep -qE passed f'", 1),
        ]
        self.assertEqual(self.verdicts(recs, "claude_code"), {
            202: False, 203: False,
            201: ("call", "bash"),
            204: ("call", "search"),
            205: ("housekeeping", "git"),
            206: ("housekeeping", "realpath"),
            207: ("housekeeping", "command-guard.sh"),
            208: ("call", "search"),            # claude.exe: a Grep under the agent
            210: False,                         # and under a tool shell it is part of that shell's call
            209: ("call", "bash")})

    def test_a_claude_session_whose_tool_shell_outlived_its_agent_in_schema_8(self):
        # the loop's shell is its own agent_pid (pid 500, ppid 1). With the index knowing it is a tool
        # shell its `pgrep` and `sleep` are not the agent's children, and with no entry they are
        procs = ProcIndex(cfg())
        procs.feed(norm(rec("residency", T, 500, 1, "bash", SNAP8 + "'until pgrep -f x; do sleep 15; done'",
                            agent_pid=500, tree_root=500, is_agent=True, schema_version=8, depth=0)))
        recs = [rec("exit", T + i, pid, 500, command, args, agent_pid=500, tree_root=500,
                    schema_version=8, depth=1)
                for i, (pid, command, args) in enumerate([(600, "pgrep", "pgrep -f x"),
                                                          (601, "sleep", "sleep 15")])]
        self.assertEqual(self.verdicts(recs, "claude_code", procs), {600: False, 601: False})
        self.assertEqual(self.verdicts(recs, "claude_code"),
                         {600: ("housekeeping", "pgrep"), 601: ("housekeeping", "sleep")})

    def test_a_server_that_hosts_sessions_keeps_its_chores_as_housekeeping_in_schema_8(self):
        # the desktop app's server and Codex's app-server daemon are `infra`: not an agent runtime, but
        # what they run is the agent's own, so it is housekeeping by shape and not the join's
        # (rtx6000a apids 1284087 and 2591891). An ssh bootstrap shell (`helper`) keeps its calls too
        procs = ProcIndex(cfg())
        for pid, command, args, agent_type in [
                (700, "server", "/home/alice/.claude/remote/srv/be1c1ca5/server --serve --socket "
                                "/home/alice/.claude/remote/run/1c59/rpc.sock", "claude_code"),
                (800, "codex", "/home/alice/.codex/packages/app-server-daemon/releases/0.165.0-x86_64-unknown-linux-musl"
                               "/bin/codex app-server daemon pid-update-loop", "codex"),
                (900, "sh", "/bin/sh -c printf '%b' '\\050\\320\\117'; PATH=\"${CODEX_INSTALL_DIR:-$HOME/.local/bin}:$PATH\";"
                            " export PATH; if command -v codex >/dev/null 2>&1; then exit 0; fi; exit 86", "codex")]:
            procs.feed(norm(rec("residency", T, pid, 1, command, args, agent_pid=pid, tree_root=pid,
                                is_agent=True, agent_type=agent_type, schema_version=8, depth=0)))
        self.assertEqual([procs.get("h1", p)[1] for p in (700, 800, 900)], ["infra", "infra", "helper"])

        def under(root, agent_type, *kids):
            return [rec("exit", T + i, root + 1 + i, root, command, args, agent_pid=root, tree_root=root,
                        agent_type=agent_type, schema_version=8, depth=1)
                    for i, (command, args) in enumerate(kids)]

        desktop = under(700, "claude_code",
                        ("git", "git -c core.fsmonitor= -c core.hooksPath=/dev/null status"),
                        ("realpath", "realpath /home/alice/proj"),
                        ("sh", "sh -c ls -1 /home/alice/.claude/projects/p"))
        daemon = under(800, "codex",
                       ("sh", "/bin/sh -s"),
                       ("codex", "/home/alice/.codex/packages/app-server-daemon/releases/0.165.0/bin/codex --version"),
                       ("sh", "/bin/sh -c if command -v flock >/dev/null 2>&1; then exit 1; else exit 2; fi"))
        remote = under(900, "codex",
                       ("bash", "/bin/bash -l -i -c CODEX_HOME=\"${CODEX_HOME:-$HOME/.codex}\"; export CODEX_HOME;"
                                " exec /bin/sh -c \"$CODEX_REMOTE_PAYLOAD\""))
        self.assertEqual(self.verdicts(desktop, "claude_code", procs),
                         {701: ("housekeeping", "git"), 702: ("housekeeping", "realpath"),
                          703: ("housekeeping", "sh")})
        self.assertEqual(self.verdicts(daemon, "codex", procs),
                         {801: ("housekeeping", "sh -s"), 802: ("housekeeping", "probe"),
                          803: ("housekeeping", "flock check")})
        # read as a direct child of the helper root, and named as Codex's own: the remote launcher
        self.assertEqual(self.verdicts(remote, "codex", procs), {901: ("housekeeping", "remote launch")})

    def test_a_claude_session_through_the_index_in_schema_7(self):
        # the shell is held as a tool shell, so the sleep under it is not the agent's child
        procs = FakeProcs({200: [100, "tool-shell"], 100: [50, "agent"]})
        recs = [rec("exit", T, 201, 200, "sleep", "sleep 15", agent_pid=200, tree_root=100),
                rec("exit", T, 200, 100, "zsh", SNAP + "'sleep 15'", agent_pid=200, tree_root=100, is_agent=True),
                rec("exit", T, 210, 100, "git", "git status", agent_pid=100, tree_root=100)]
        self.assertEqual(self.verdicts(recs, "claude_code", procs),
                         {201: False, 200: ("call", "bash"), 210: ("housekeeping", "git")})

    def test_a_codex_session_in_schema_8(self):
        def at(pid, ppid, command, args, depth):
            return rec("exit", T + pid, pid, ppid, command, args, agent_pid=300, tree_root=300,
                       agent_type="codex", schema_version=8, depth=depth)
        recs = [
            at(301, 300, "cat", "cat /n/home/x/AGENTS.md", 1),
            at(302, 300, "git", "git -c safe.bareRepository=explicit rev-parse HEAD", 1),
            at(303, 300, "lsb_release", "/bin/sh /usr/bin/lsb_release -a", 1),
            at(305, 304, "pytest", "pytest -q", 2),                                # under the shell
            at(304, 300, "bash", "/usr/bin/bash -lc pytest -q", 1),
            at(312, 311, "codex", "/h/.codex/bin/codex --codex-run-as-fs-helper", 3),    # under the wrapper
            at(311, 310, "bwrap", "bwrap --as-pid-1 --ro-bind / / /h/.codex/bin/codex --codex-run-as-fs-helper", 2),
            at(310, 300, "codex-linux-sandbox", "codex-linux-sandbox --sandbox-policy-cwd /h --command-cwd /h", 1),
        ]
        self.assertEqual(self.verdicts(recs, "codex"), {
            301: ("call", "bare"),
            302: ("housekeeping", "git (codex)"),
            303: ("housekeeping", "lsb_release"),
            305: False,
            304: ("call", "shell"),
            312: False, 311: False,
            310: ("call", "wrapper")})


if __name__ == "__main__":
    unittest.main()
