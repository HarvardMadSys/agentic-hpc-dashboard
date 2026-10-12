"""Workspace and blast radius: where an agent ran, and what its commands named.

Two kinds of evidence are tested apart, because the page keeps them apart: the
directory a process RAN in (measured, its cwd) and the paths its command line
NAMED (inferred from argv, classified read / write / delete / named). The
command-line shapes are the ones rtx6000a and madsys-gpu1 actually show: Claude
Code's `zsh -c source <snapshot> && eval '<cmd>'` tool shells, Codex's
`/bin/zsh -c <cmd>`, a `codex-linux-sandbox ... -- <payload>`, and the
`multiprocessing` flood that is most of a busy day's exits.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_sessions import HOME, SNAP, cfg, rec, reduce          # noqa: E402

from rc_dashboard.reducers import workspace as ws                 # noqa: E402
from rc_dashboard.reducers.workspace import D, N, R, W, plan, resolve, split  # noqa: E402

PROJ = HOME + "/proj"
TAIL = " < /dev/null && pwd -P >| /tmp/claude-a1b2-cwd"


def bash(cmd):
    """A Claude Code Bash tool call's argv, boilerplate and all."""
    return SNAP + "'" + cmd.replace("'", "'\\''") + "'" + TAIL


def ops(role, args):
    """{(op, token)} a command line yields, ignoring dir-ness and base."""
    return {(op, tok) for op, tok, _isdir, _base in plan(role, args)[0]}


class ParseTest(unittest.TestCase):
    """What one command line names, before any record's cwd is applied."""

    def test_only_the_evald_command_of_a_claude_tool_call_counts(self):
        # the snapshot it sources and the cwd file it writes are the harness's,
        # on every call: counted, every Claude session would write to /tmp
        got = plan("tool-shell", bash("cat README.md"))[0]
        self.assertEqual([(op, tok) for op, tok, _, _ in got], [(R, "README.md")])

    def test_an_escaped_quote_inside_eval_is_unescaped(self):
        got = ops("tool-shell", bash("grep -n 'it''s' notes/a.txt"))
        self.assertEqual(got, {(R, "notes/a.txt")})

    def test_redirections_write_and_read(self):
        got = ops(None, "/bin/zsh -c sort < in.txt > out.txt 2> err.log && cat a >> log/b")
        self.assertEqual(got, {(R, "in.txt"), (W, "out.txt"), (W, "err.log"), (R, "a"),
                               (W, "log/b")})

    def test_a_descriptor_and_dev_null_are_not_files(self):
        got = plan(None, "/bin/zsh -c make 2>&1 >/dev/null")[0]
        self.assertEqual([t for _, t, _, _ in got], ["/dev/null"])
        self.assertIsNone(resolve("/dev/null", None, PROJ, HOME))   # dropped when placed

    def test_command_semantics(self):
        cases = {
            "cat a.txt": {(R, "a.txt")},
            "head -n 20 logs/run.log": {(R, "logs/run.log")},
            "cp -r src/ /scratch/bak/": {(R, "src/"), (W, "/scratch/bak/")},
            "mv a.txt b.txt": {(W, "a.txt"), (W, "b.txt")},
            "cp -t dest/ a b": {(W, "dest/"), (R, "a"), (R, "b")},
            "rm -rf build": {(D, "build")},
            "rmdir old": {(D, "old")},
            "mkdir -p out/logs": {(W, "out/logs")},
            "touch x.lock": {(W, "x.lock")},
            "tee -a /tmp/t.log": {(W, "/tmp/t.log")},
            "ln -s /scratch/data data": {(N, "/scratch/data"), (W, "data")},
            "sed -i s/a/b/ src/x.py": {(W, "src/x.py")},
            "sed -n 1,20p src/x.py": {(R, "src/x.py")},
            "perl -pi -e s/a/b/ x.txt": {(W, "x.txt")},
            "rsync -av data/ /scratch/copy/": {(R, "data/"), (W, "/scratch/copy/")},
            "tar -xzf d.tgz -C /scratch/u/d": {(R, "d.tgz"), (W, "/scratch/u/d")},
            "tar czf out.tgz src": {(W, "out.tgz"), (R, "src")},
            "git clone https://github.com/x/y /scratch/y": {(W, "/scratch/y")},
            "git -C /home/alice/other status": {(R, "/home/alice/other")},
            "git rm old.py": {(D, "old.py")},
            "wget -O ~/dl/f.bin https://example.com/f": {(W, "~/dl/f.bin")},
            "curl -sSL -o /tmp/x.json https://example.com/x": {(W, "/tmp/x.json")},
            "find build -name *.o -delete": {(D, "build")},
            "find . -type f -exec rm -f {} +": {(D, ".")},
            "find /home/bob/shared -name x": {(R, "/home/bob/shared")},
            "rg -n needle src tests/t.py": {(R, "src"), (R, "tests/t.py")},
            "grep -rn TODO": {(R, ".")},
            "python train.py --out ckpt/": {(R, "train.py"), (N, "ckpt/")},
            "bash scripts/run.sh": {(R, "scripts/run.sh")},
            "unlink stale.sock": {(D, "stale.sock")},
            "perl script.pl data/x.txt": {(R, "script.pl"), (N, "data/x.txt")},
            "perl -lane print in.tsv": {(R, "in.tsv")},
            "gzip -d logs/a.gz": {(W, "logs/a.gz")},
            "gzip -c logs/a": {(R, "logs/a")},
            "sort -o sorted.txt in.txt": {(W, "sorted.txt"), (R, "in.txt")},
            "cd && cat .bashrc": {(R, ".bashrc")},
            "ls": {(R, ".")},
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(ops(None, "/bin/zsh -c " + cmd), want)

    def test_anything_else_path_like_is_named(self):
        self.assertEqual(ops(None, "mytool --data=/scratch/d run cfg/a.yaml plain"),
                         {(N, "/scratch/d"), (N, "cfg/a.yaml")})

    def test_urls_flags_remotes_and_patterns_are_not_paths(self):
        self.assertEqual(ops(None, "scp -P 22 a.txt bob@host:/x/y"), {(R, "a.txt")})
        self.assertEqual(ops(None, "git show HEAD:src/x.py"), set())
        self.assertEqual(ops(None, "echo a/b > /dev/null"), set())
        self.assertEqual(ops(None, "pgrep -f scripts/run.sh"), set())
        self.assertEqual(ops(None, "curl -H Content-Type: application/json https://x/y"), set())

    def test_a_heredoc_body_is_data_not_commands(self):
        cmd = "cat > gen.py << 'EOF'\nimport os\nos.remove('/etc/passwd')\nrm -rf /\nEOF\npython gen.py"
        self.assertEqual(ops("tool-shell", bash(cmd)), {(W, "gen.py"), (R, "gen.py")})

    def test_a_sed_script_split_on_its_spaces_is_not_a_write_to_root(self):
        # seen on rtx6000a: `sed -i 's|    return cov / (vx ...|...|' scores.py`
        # exec'd, so its argv arrives with the script cut at every space
        args = "sed -i s|    return cov / (vx \\* vy)|    return 1| scores.py"
        self.assertEqual(ops(None, args), {(W, "scores.py")})
        self.assertEqual(ops(None, "ls /"), {(R, "/")})

    def test_an_execd_argv_has_no_shell_syntax(self):
        # the collector joins argv with spaces: `;` and `>` inside an argument
        # are characters, and a program split on its spaces is not files
        flood = ("/home/glen/workspace/nimbus/.venv/bin/python -c from multiprocessing.spawn "
                 "import spawn_main; spawn_main(tracker_fd=6, pipe_handle=14) "
                 "--multiprocessing-fork")
        self.assertEqual(plan(None, flood)[0], ())
        self.assertEqual(ops(None, "awk {print $1} > data/a.csv"), {(R, "data/a.csv")})

    def test_a_sandboxed_claude_tool_call_is_read_through_its_eval(self):
        # bwrap around the snapshot shell: neither the binds nor the boilerplate
        args = "/usr/bin/bwrap --ro-bind / / --bind /tmp /tmp -- " + bash("rm -rf build")
        self.assertEqual(ops("sandbox", args), {(D, "build")})

    def test_a_sandbox_wrappers_setup_is_not_its_work(self):
        self.assertEqual(ops(None, "bwrap --ro-bind /usr /usr --bind /tmp /tmp -- cat in.txt"),
                         {(R, "in.txt")})
        sb = ("/home/a/.codex/tmp/arg0/codex-linux-sandbox --sandbox-policy-cwd /x "
              "--command-cwd /x -- /bin/bash -lc rm -rf dist")
        self.assertEqual(ops("sandbox", sb), {(D, "dist")})

    def test_a_cd_moves_where_relative_paths_land(self):
        got = plan("tool-shell", bash("cd /scratch/run && python train.py > log.txt"))[0]
        bases = {tok: base for _op, tok, _d, base in got}
        self.assertEqual(bases["train.py"], "/scratch/run")
        self.assertEqual(bases["log.txt"], "/scratch/run")

    def test_the_agents_own_launch_line_names_only_its_granted_dirs(self):
        self.assertEqual(ops("agent", "claude --add-dir /scratch/data --resume x/y"),
                         {(N, "/scratch/data")})
        self.assertEqual(ops("probe", "/usr/bin/node /home/a/.nvm/bin/codex --version"), set())

    def test_system_scripts_run_by_an_interpreter_are_the_program(self):
        self.assertEqual(ops(None, "/bin/sh /usr/bin/lsb_release -a"), set())

    def test_path_tokens_per_command_line_are_capped_and_counted(self):
        items, capped = plan(None, "cat " + " ".join("f%d.txt" % i for i in range(40)))
        self.assertEqual((len(items), capped), (ws.MAX_ITEMS, 40 - ws.MAX_ITEMS))


class ResolveTest(unittest.TestCase):
    def test_relative_paths_resolve_against_the_process_cwd(self):
        self.assertEqual(resolve("../data/x.csv", None, PROJ + "/src", HOME),
                         HOME + "/proj/data/x.csv")
        self.assertEqual(resolve("out.txt", "/scratch/run", PROJ, HOME), "/scratch/run/out.txt")
        self.assertEqual(resolve("out.txt", "sub", PROJ, HOME), PROJ + "/sub/out.txt")

    def test_an_inherited_cwd_is_not_climbed_out_of(self):
        # rtx6000a: `./bin/cachesim ../data/x.bin`, its cwd copied from the agent
        # in /home/marthen, read as /home/data -- another user's home
        self.assertIsNone(resolve("../data/x.bin", None, "/home/marthen", HOME, True))
        self.assertEqual(resolve("src/../a.py", None, PROJ, HOME, True), PROJ + "/a.py")
        self.assertEqual(resolve("../data/x.bin", None, PROJ, HOME), HOME + "/data/x.bin")
        self.assertEqual(resolve("../x", "/scratch/run", PROJ, HOME, True), "/scratch/x")
        # `git -C .. diff -- web/x` from a copied cwd climbs out of it too
        self.assertIsNone(resolve("web/x.ts", "..", "/home/marthen", HOME, True))
        self.assertEqual(resolve("x.ts", "web", PROJ, HOME, True), PROJ + "/web/x.ts")

    def test_home_shorthands(self):
        self.assertEqual(resolve("~/.cache/x", None, PROJ, HOME), HOME + "/.cache/x")
        self.assertEqual(resolve("$HOME/x", None, PROJ, HOME), HOME + "/x")
        self.assertEqual(resolve("${HOME}/x", None, PROJ, HOME), HOME + "/x")
        self.assertEqual(resolve("~bob/notes", None, PROJ, HOME), "/home/bob/notes")

    def test_what_cannot_be_placed_is_not_guessed(self):
        self.assertIsNone(resolve("out.txt", None, None, HOME))      # no cwd recorded
        self.assertIsNone(resolve("out.txt", "\0", PROJ, HOME))      # after `cd $VAR`
        self.assertIsNone(resolve("/proc/self/fd/3", None, PROJ, HOME))
        # `/` is an inherited directory, not a workplace: `ls src` there is not /src
        self.assertIsNone(resolve("src", None, "/", HOME))
        self.assertEqual(resolve("/etc/hosts", None, "/", HOME), "/etc/hosts")

    def test_a_file_belongs_to_its_directory_and_a_glob_cuts_it(self):
        self.assertEqual(split("/p/src/a.py", False), ("/p/src", "a.py"))
        self.assertEqual(split("/p/out/logs", True), ("/p/out/logs", "."))
        self.assertEqual(split("/p/data/*/x.csv", False), ("/p/data", "*/x.csv"))


def agent(t0, pid=100, cwd=PROJ, user="alice", agent_type="claude_code", **kw):
    return rec("residency", t0, pid, 50, "claude", "claude", agent_pid=pid, tree_root=pid,
               is_agent=True, cwd=cwd, user=user, agent_type=agent_type, **kw)


def call(t, pid, cmd, cwd=PROJ, root=100, io=None, user="alice", **kw):
    """A Bash tool call: the tagged snapshot shell, run by agent `root`."""
    return rec("exit", t, pid, root, "zsh", bash(cmd), agent_pid=pid, tree_root=root,
               is_agent=True, cwd=cwd, user=user, io=io, **kw)


def child(t, pid, ppid, command, args, cwd=PROJ, root=100, io=None, **kw):
    """What a tool call ran: untagged, keyed on the tool call's shell."""
    return rec("exit", t, pid, ppid, command, args, agent_pid=ppid, tree_root=root,
               cwd=cwd, io=io, **kw)


def workspace(recs):
    return reduce(recs)["cascades"]["workspace"]


def agent_row(w, pid):
    return next(a for a in w["agents"] if a["pid"] == pid)


def row(view, shown):
    return next(d for d in view["dirs"] if d["shown"] == shown)


class BlastRadiusTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 600

    def test_ran_in_bytes_and_named_are_kept_apart(self):
        t0 = self.t0
        io = {"rchar_mb": 2.0, "wchar_mb": 1.0, "rd_mb": 0.5, "wr_mb": 0.25,
              "scope": "process"}
        w = workspace([
            agent(t0),
            child(t0 + 1, 201, 200, "python", "python train.py", io=io),
            call(t0 + 2, 200, "python train.py > out/log.txt", io=io),
        ])
        a = agent_row(w, 100)
        self.assertEqual((a["home"], a["home_src"], a["home_shown"]), (PROJ, "agent", "~/proj"))
        home = row(a["self"], "./")
        self.assertEqual(home["procs"], 2)                          # measured
        self.assertEqual(home["io"]["wchar_mb"], 2.0)
        self.assertEqual(home["read"], 1)                           # train.py, named twice
        out = row(a["self"], "./out")
        self.assertEqual((out["procs"], out["write"], out["io"]), (0, 1, None))
        self.assertIsNone(out["flag"])                              # inside the home
        s = a["self"]["summary"]
        self.assertEqual((s["procs"], s["inside_pct"], s["outside_writes"]), (2, 100.0, 0))
        self.assertEqual(s["io"]["rchar_mb"], 4.0)

    def test_writes_and_deletes_outside_the_home_are_flagged_by_kind(self):
        t0 = self.t0
        w = workspace([
            agent(t0),
            call(t0 + 1, 200, "cp results.csv /scratch/shared/results.csv"),
            call(t0 + 2, 201, "rm -rf ~/other-project/build"),
            call(t0 + 3, 202, "echo x > /tmp/scratch.txt"),
            call(t0 + 4, 203, "touch /home/bob/notes.md"),
            call(t0 + 5, 204, "cat /etc/hosts"),
        ])
        a = agent_row(w, 100)
        flags = {(f["shown"], f["op"], f["kind"]) for f in a["self"]["flags"]}
        self.assertEqual(flags, {
            ("/scratch/shared/results.csv", "write", "shared storage"),
            ("~/other-project/build", "delete", "own home"),
            ("/tmp/scratch.txt", "write", "temp"),
            ("~bob/notes.md", "write", "other user's home"),
        })
        # the most alarming place first, a delete before a write within it
        self.assertEqual([f["kind"] for f in a["self"]["flags"]],
                         ["other user's home", "shared storage", "own home", "temp"])
        self.assertEqual(row(a["self"], "/etc")["flag"], None)       # a read is not flagged
        s = a["self"]["summary"]
        self.assertEqual(s["outside_writes"], 4)
        self.assertEqual({g["root"] for g in s["roots"]},
                         {"/scratch", "~/other-project", "/tmp", "~bob", "/etc"})
        self.assertEqual(w["summary"]["with_outside_writes"], 1)
        self.assertEqual((w["summary"]["other_homes"], w["summary"]["shared_roots"]), (1, 1))

    def test_an_agent_started_at_the_root_is_anchored_on_its_first_call(self):
        t0 = self.t0
        w = workspace([
            agent(t0, cwd="/"),
            call(t0 + 1, 200, "ls", cwd="/"),
            call(t0 + 2, 201, "cat a.txt", cwd=PROJ),
        ])
        a = agent_row(w, 100)
        self.assertEqual((a["home"], a["home_src"]), (PROJ, "first_call"))

    def test_how_much_of_ran_in_was_read_rather_than_inherited(self):
        t0 = self.t0
        w = workspace([agent(t0),
                       call(t0 + 1, 200, "ls", cwd_source="proc"),
                       child(t0 + 2, 211, 210, "cat", "cat ../x.txt", cwd_source="inherit"),
                       call(t0 + 3, 210, "cat ../x.txt", cwd_source="inherit"),
                       call(t0 + 4, 220, "ls")])           # schema 7: no cwd_source
        s = agent_row(w, 100)["self"]["summary"]
        self.assertEqual(s["cwd_read_pct"], 33.3)
        self.assertEqual(s["unresolved"], 2)                   # ../x.txt, twice

    def test_no_home_means_outside_is_not_measured(self):
        # an agent known only from its tool calls, none of which recorded a cwd
        t0 = self.t0
        w = workspace([rec("exit", t0 + 1, 200, 100, "zsh", bash("rm /tmp/x"),
                           agent_pid=200, tree_root=100, is_agent=True, cwd=None)])
        a = agent_row(w, 100)
        self.assertIsNone(a["home"])
        s = a["self"]["summary"]
        self.assertIsNone(s["outside"])
        self.assertIsNone(s["outside_writes"])
        self.assertIsNone(s["inside_pct"])
        self.assertEqual(s["unresolved"], 0)
        self.assertEqual(s["procs_no_cwd"], 1)

    def test_tool_call_keys_fold_into_their_agent(self):
        t0 = self.t0
        w = workspace([agent(t0)] + [call(t0 + i, 200 + i, "ls") for i in range(1, 6)])
        self.assertEqual(len(w["agents"]), 1)
        self.assertEqual(agent_row(w, 100)["self"]["summary"]["keys"], 5)

    def test_an_inherited_argv_is_not_read_twice(self):
        t0 = self.t0
        w = workspace([
            agent(t0),
            child(t0 + 1, 201, 200, "zsh", bash("rm -rf a b c"), argv_source="inherit"),
            call(t0 + 2, 200, "rm -rf a b c"),
        ])
        r = row(agent_row(w, 100)["self"], "./")
        self.assertEqual((r["procs"], r["delete"]), (2, 3))
        self.assertEqual(w["buffer"]["inherited_argv"], 1)

    def test_deep_directories_fold_into_an_ancestor_and_say_how_many(self):
        t0 = self.t0
        deep = PROJ + "/a/b/c/d/e/f"
        w = workspace([agent(t0), call(t0 + 1, 200, "ls", cwd=deep),
                       call(t0 + 2, 201, "ls", cwd=PROJ + "/a/b/c/d/x")])
        r = row(agent_row(w, 100)["self"], "./a/b/c/d")
        self.assertEqual((r["procs"], r["deeper"]), (2, 2))

    def test_many_quiet_subdirectories_fold_into_their_parent(self):
        # one directory per run, as an eval harness writes them
        t0 = self.t0
        runs = [call(t0 + i, 200 + i, "cat out/run%02d/result.json" % i) for i in range(1, 10)]
        runs.append(call(t0 + 20, 220, "cat out/run01/a.json out/run01/b.json"))
        v = agent_row(workspace([agent(t0)] + runs), 100)["self"]
        shown = {d["shown"] for d in v["dirs"]}
        self.assertIn("./out/run01", shown)                 # the busiest stay rows
        self.assertEqual(len([s for s in shown if s.startswith("./out/run")]), 3)
        out = row(v, "./out")
        self.assertEqual((out["deeper"], out["read"]), (6, 6))

    def test_served_paths_are_masked(self):
        t0 = self.t0
        w = workspace([agent(t0), call(t0 + 1, 200,
                                       "wget -O /tmp/dl?access_token=sekrit123 https://x/y")])
        blob = repr(w)
        self.assertNotIn("sekrit123", blob)
        self.assertIn("[REDACTED]", blob)
        self.assertNotIn("args", {k for a in w["agents"] for k in a})


class SubagentTest(unittest.TestCase):
    """An agent started beneath an agent is its subagent: `with subagents`
    adds its directories, judged against the PARENT's home."""

    def records(self):
        t0 = time.time() - 600
        return [
            agent(t0),
            call(t0 + 1, 200, "cat README.md"),
            # the parent runs `codex exec` from a tool call, in another tree
            rec("exit", t0 + 50, 250, 210, "codex", "codex exec fix the tests",
                agent_pid=250, tree_root=100, is_agent=True, agent_type="codex",
                cwd="/scratch/sub"),
            rec("exit", t0 + 10, 260, 250, "zsh", "/bin/zsh -c rm -rf /scratch/sub/tmp",
                agent_pid=250, tree_root=100, agent_type="codex", cwd="/scratch/sub"),
            rec("exit", t0 + 51, 210, 100, "zsh", bash("codex exec fix the tests"),
                agent_pid=210, tree_root=100, is_agent=True),
        ]

    def test_this_agent_only_leaves_its_subagent_out(self):
        w = workspace(self.records())
        a = agent_row(w, 100)
        self.assertEqual(a["subagents"], [250])
        self.assertEqual({d["shown"] for d in a["self"]["dirs"]}, {"./"})
        self.assertEqual(a["self"]["summary"]["outside_writes"], 0)

    def test_with_subagents_adds_theirs_against_the_parents_home(self):
        w = workspace(self.records())
        tree = agent_row(w, 100)["tree"]
        self.assertIn("/scratch/sub", {d["shown"] for d in tree["dirs"]})
        self.assertEqual(tree["summary"]["outside_writes"], 1)
        self.assertEqual(tree["flags"][0]["shown"], "/scratch/sub/tmp")

    def test_the_subagent_is_judged_against_its_own_home_alone(self):
        w = workspace(self.records())
        sub = agent_row(w, 250)
        self.assertEqual((sub["home"], sub["parent_agent"], sub["tree"]),
                         ("/scratch/sub", 100, None))
        self.assertEqual(sub["self"]["summary"]["outside_writes"], 0)
        self.assertEqual(w["summary"]["with_subagents"], 1)

    def test_agent_tree_nodes_carry_home_and_a_workspace_summary(self):
        c = reduce(self.records())["cascades"]
        t = next(t for t in c["trees"] if t["root"]["pid"] == 100)
        n = {x["pid"]: x for x in t["nodes"]}
        self.assertEqual(n[250]["cwd_vs_parent_agent"], "outside")
        self.assertEqual(n[100]["workspace"]["dirs"], 1)
        self.assertNotIn("drift", n[100])


class CapTest(unittest.TestCase):
    def reducer(self, **conf):
        r = ws.WorkspaceReducer(cfg())
        for k, v in conf.items():
            setattr(r, k, v)
        return r

    def feed(self, r, n, cwd=lambda i: PROJ, key=lambda i: "100"):
        t = time.time()
        for i in range(n):
            r.feed({"_a3": "agent", "_kt": "apid", "_kv": key(i), "host": "h1",
                    "user": "alice", "_ts_epoch": t + i, "cwd": cwd(i), "args": None})

    def test_directories_per_key_are_capped_and_counted(self):
        r = self.reducer(max_dirs=5)
        self.feed(r, 8, cwd=lambda i: "%s/d%d" % (PROJ, i))
        st = r.keys[("h1", "100")]
        self.assertEqual((len(st.dirs), st.over, st.procs), (5, 3, 8))

    def test_keys_past_the_cap_are_dropped_and_counted(self):
        r = self.reducer(max_keys=3)
        self.feed(r, 5, key=lambda i: str(100 + i))
        self.assertEqual((len(r.keys), r.dropped), (3, 2))

    def test_the_directory_entry_budget_is_global(self):
        r = self.reducer(max_entries=4)
        self.feed(r, 6, cwd=lambda i: "%s/d%d" % (PROJ, i), key=lambda i: str(100 + i % 2))
        self.assertEqual((r.entries, sum(s.over for s in r.keys.values())), (4, 2))

    def test_names_per_directory_are_capped_and_counted(self):
        d = ws._dir_entry()
        for i in range(ws.MAX_NAMES + 7):
            ws._add_name(d, "f%d" % i, R)
        self.assertEqual((len(d[8]), d[9]), (ws.MAX_NAMES, 7))

    def test_the_agent_list_is_capped_but_never_cuts_a_flagged_agent(self):
        t0 = time.time() - 600
        recs = []
        for i in range(4):
            pid = 100 + i * 10
            recs += [agent(t0, pid=pid)]
            recs += [call(t0 + 1 + k, pid + 1 + k, "ls", root=pid) for k in range(5 - i)]
        # the quietest agent is the one that wrote outside its home
        recs += [call(t0 + 9, 139, "touch /scratch/x", root=130)]
        w = reduce_with(recs, max_agents=2)["cascades"]["workspace"]
        self.assertEqual([a["pid"] for a in w["agents"]], [100, 110, 130])
        self.assertEqual((w["summary"]["agents"], w["summary"]["shown"]), (4, 3))


def reduce_with(recs, **attrs):
    """`reduce`, with the workspace reducer's bounds changed first."""
    import test_sessions
    from rc_dashboard.normalize import Normalizer
    from rc_dashboard.windows import PanelView
    c = test_sessions.cfg()
    classes = list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]
    view = PanelView(c, classes, 1440)
    reducers = view.fresh_reducers()
    for k, v in attrs.items():
        setattr(reducers["workspace"], k, v)
    dispatch = PanelView.build_dispatch(reducers)
    norm = Normalizer(c)
    for raw in recs:
        r = norm.normalize(raw)
        if r is not None:
            for red in dispatch.get(r.get("event"), ()):
                red.feed(r)
    view.reducers = reducers
    return view.materialize()


if __name__ == "__main__":
    unittest.main()
