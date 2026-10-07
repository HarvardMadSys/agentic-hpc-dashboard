"""Sporadicity: where a session was launched vs where its argv says it reached.

Runs anywhere: records are built in memory, pushed through the real Normalizer
so `_sid` / `_a3` are what the service would derive, and fed to the reducer.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod                 # noqa: E402
from rc_dashboard.normalize import Normalizer                 # noqa: E402
from rc_dashboard.reducers import sporadicity as sp           # noqa: E402
from test_ingest import FeedCase                              # noqa: E402

CLAUDE_WRAPPER = (
    "/bin/zsh -c source /home/glen/.claude/shell-snapshots/snapshot-zsh-1791.sh "
    "2>/dev/null || true && eval 'cd /home/glen/workspace/sglang/bench && cat README.md' "
    "< /dev/null && pwd -P >| /tmp/claude-4ad6-cwd")


def cfg():
    return configmod.Config(configmod.defaults(), {}, None)


class ExtractTest(unittest.TestCase):

    def refs(self, args, base=None, home="/home/u"):
        refs, tally = sp.extract(args, base=base, home=home)
        return sorted(refs), tally

    def test_the_agent_harness_wrapper_leaves_only_the_cd_it_carried(self):
        refs, tally = self.refs(CLAUDE_WRAPPER, home="/home/glen")
        self.assertEqual(refs, [("/home/glen/workspace/sglang/bench", False, True)])
        self.assertEqual((tally["harness"], tally["pseudo"], tally["program"]), (2, 2, 1))

    def test_system_programs_are_not_work_locations(self):
        refs, tally = self.refs("/usr/bin/python3 -Es /usr/bin/lsb_release -a")
        self.assertEqual(refs, [])
        self.assertEqual(tally["program"], 2)

    def test_the_program_itself_is_not_a_work_location(self):
        # ~/.nvm, ~/.vscode-server and .venv binaries would otherwise top the
        # directory table on every launch; the script it runs still counts
        refs, tally = self.refs("/home/u/proj/.venv/bin/python /tmp/probe.py")
        self.assertEqual(refs, [("/tmp", False, False)])
        self.assertEqual(tally["program"], 1)

    def test_a_bare_root_is_a_separator_not_a_place_unless_cd_goes_there(self):
        refs, tally = self.refs("tr / _")
        self.assertEqual((refs, tally["bare_root"]), ([], 1))
        self.assertEqual(self.refs("cd /")[0], [("/", False, True)])

    def test_quotes_join_words_as_the_shell_would(self):
        self.assertEqual(self.refs('ls "$dir"/subagents')[0], [])
        self.assertEqual(self.refs("grep -E '\"'\"'^exit'\"'\"' /tmp/x/run.log")[0],
                         [("/tmp/x", False, False)])

    def test_mutating_commands_mark_their_targets_as_writes(self):
        cases = {
            "rm -rf /scratch/u/old": [("/scratch/u/old", True, False)],
            "cp /etc/hosts /tmp/h.txt": [("/etc/hosts", False, False), ("/tmp", True, False)],
            "bash -c echo hi > /home/u/notes.txt": [("/home/u", True, False)],
            "sed -i s/a/b/ /tmp/x.py": [("/tmp", True, False)],
            "curl -sSL -o /tmp/data.bin https://x.org/a/b": [("/tmp", True, False)],
            "git clone https://github.com/x/y.git /scratch/u/y": [("/scratch/u/y", True, False)],
            "git -C /home/u/other status": [("/home/u/other", False, False)],
            "git -C /home/u/other commit -m x": [("/home/u/other", True, False)],
            "tar -xzf /tmp/a.tgz -C /scratch/u/out": [("/scratch/u/out", True, False),
                                                       ("/tmp", False, False)],
            "mkdir -p /scratch/u/new.dir": [("/scratch/u/new.dir", True, False)],
            "dd if=/dev/zero of=/tmp/blob.img": [("/tmp", True, False)],
            "mv /home/u/a.txt /scratch/u/": [("/home/u", True, False), ("/scratch/u", True, False)],
        }
        for args, want in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.refs(args)[0], sorted(want))

    def test_urls_and_remote_paths_are_not_local_directories(self):
        self.assertEqual(self.refs("curl https://example.com/a/b")[0], [])
        self.assertEqual(self.refs("scp host:/x/y /tmp/z.txt")[0], [("/tmp", True, False)])
        self.assertEqual(self.refs("echo //g")[0], [])

    def test_flag_values_globs_and_variables(self):
        self.assertEqual(self.refs("pytest --rootdir=/home/u/proj2 tests")[0],
                         [("/home/u/proj2", False, False)])
        self.assertEqual(self.refs("gcc -I/usr/include x.c")[0], [("/usr/include", False, False)])
        self.assertEqual(self.refs("ls /scratch/u/data/*.csv")[0], [("/scratch/u/data", False, False)])
        self.assertEqual(self.refs("ls /x/$VAR/y")[0], [("/x", False, False)])
        self.assertEqual(self.refs("ls $HOME/x")[0], [])

    def test_patterns_and_scripts_are_not_paths(self):
        cases = {
            "awk /VmHWM/ {print $2} /proc/1/status": [],
            "sed -i /^SALT = x/d tests/t.py": [],
            "sed -n /== 2026-10-06/,$p run.log": [],
            "sed -e s/a/b/ /tmp/x.txt": [("/tmp", False, False)],
            "grep -rn /api/v1 /home/u/other": [("/home/u/other", False, False)],
            "grep -e /api/v1 /home/u/other": [("/home/u/other", False, False)],
            "rg --files -g /Pipfile -g /.venv /home/u/x": [("/home/u/x", False, False)],
            "rsync -a --include=/input/ --exclude=/input/** src/ /scratch/u/dst/":
                [("/scratch/u/dst", True, False)],
            "echo /== /##": [],
        }
        for args, want in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.refs(args)[0], sorted(want))

    def test_a_file_at_root_is_not_a_reference_to_root(self):
        refs, tally = self.refs("ls /x.json")
        self.assertEqual((refs, tally["bare_root"]), ([], 1))

    def test_the_last_token_of_a_cut_argv_is_a_fragment(self):
        refs, tally = sp.extract("ls /proc/1/fd/0 /scratch/u/a /p", home="/home/u",
                                 truncated=True)
        self.assertEqual(refs, [("/scratch/u/a", False, False)])
        self.assertEqual(tally["pseudo"], 1)

    def test_home_shorthand(self):
        self.assertEqual(self.refs("cd ~/x")[0], [("/home/u/x", False, True)])
        self.assertEqual(self.refs("bash -c cd && ls")[0], [("/home/u", False, True)])
        self.assertEqual(self.refs("cat ~v/notes.md")[0], [("/home/v", False, False)])

    def test_relative_paths_resolve_only_against_a_trustworthy_base(self):
        refs, tally = self.refs("cat ../other/file.txt", base="/home/u/proj")
        self.assertEqual(refs, [("/home/u/other", False, False)])
        self.assertEqual(tally["relative_resolved"], 1)
        # an inherited cwd may be stale: say so rather than guess
        refs, tally = self.refs("cat ../other/file.txt", base=None)
        self.assertEqual(refs, [])
        self.assertEqual(tally["relative_unresolved"], 1)
        # ...unless the payload itself moved to an absolute directory first
        refs, _ = self.refs("bash -c cd /a/b && ls ../c", base=None)
        self.assertEqual(refs, [("/a/b", False, True), ("/a/c", False, False)])
        refs, tally = self.refs("bash -c cd sub && ls", base=None)
        self.assertEqual((refs, tally["relative_unresolved"]), ([], 1))


class ZoneTest(unittest.TestCase):

    def setUp(self):
        self.z = sp.Zones(cfg())

    def test_every_zone(self):
        want = {
            "/home/u/proj": "cwd",
            "/home/u/proj/src": "cwd",
            "/home/u/other": "home",
            "/home/u/.ssh": "home-dot",
            "/home/u/.claude/projects": "tool-state",
            "/tmp/claude-1000/scratch": "tool-state",
            "/tmp/x": "tmp",
            "/dev/shm/y": "tmp",
            "/home/v/x": "other-home",
            "/n/home01/v/x": "other-home",
            "/scratch/x": "shared",
            "/n/holylabs/LABS/lab": "shared",
            "/etc": "system",
            "/weird": "other",
        }
        for path, zone in want.items():
            with self.subTest(path=path):
                self.assertEqual(self.z.of(path, "/home/u/proj", "/home/u", "u"), zone)

    def test_no_anchor_means_no_cwd_zone(self):
        self.assertEqual(self.z.of("/home/u/proj/src", None, "/home/u", "u"), "home")

    def test_home_is_read_off_the_cwd_when_it_names_the_user(self):
        self.assertEqual(self.z.home_of("u", "/n/home01/u/proj"), "/n/home01/u")
        self.assertEqual(self.z.home_of("u", "/scratch/u/proj"), "/home/u")
        self.assertIsNone(self.z.home_of(None, "/scratch/u/proj"))

    def test_radius_counts_levels_up_from_the_anchor(self):
        self.assertEqual(sp.radius("/home/u/proj", "/home/u/proj/src"), (0, False))
        self.assertEqual(sp.radius("/home/u/proj", "/home/u/other"), (1, False))
        self.assertEqual(sp.radius("/home/u/proj", "/home/v"), (2, False))
        self.assertEqual(sp.radius("/home/u/proj", "/tmp"), (3, True))


def rec(pid, ppid, args, cwd="/home/u/proj", actor3="agent", tty=None,
        is_agent=False, agent_pid=100, user="u", event="exit"):
    cmd = args.split(" ", 1)[0].rsplit("/", 1)[-1]
    return {"schema_version": 7, "event": event, "ts": "2026-09-25 10:00:00",
            "ts_epoch": 1790000000 + pid, "host": "h1", "user": user,
            "actor3": actor3,
            "agent_type": "claude_code" if actor3 == "agent" else None,
            "command": cmd, "args": args, "pid": pid, "ppid": ppid, "cwd": cwd,
            "tty": tty, "is_agent": is_agent, "agent_pid": agent_pid,
            "session_key": ("apid:%d" % agent_pid) if agent_pid else None,
            "cpu_s": 0.1, "duration_s": 0.2}


class ReducerTest(unittest.TestCase):

    def feed(self, recs):
        c = cfg()
        norm = Normalizer(c)
        red = sp.SporadicityReducer(c, ["agent", "human-vscode", "human"])
        for r in recs:
            r = norm(r)
            if r is not None:
                red.feed(r)
        return red.result()

    def agent_session(self):
        return [
            # the child exits first, then the shell that waited on it
            rec(201, 200, "rm -rf /scratch/u/old"),
            rec(200, 100, "/bin/zsh -c eval 'cd /home/u/other && rm -rf /scratch/u/old'"),
            rec(202, 100, "cat /home/u/proj/README.md"),
            rec(100, 1, "claude --dangerously-skip-permissions", is_agent=True,
                event="truncated"),
        ]

    def test_an_agent_session_reaching_outside_its_launch_dir(self):
        out = self.feed(self.agent_session())
        a = out["by_class"]["agent"]
        self.assertEqual((a["sessions"], a["sessions_outside"], a["sessions_write_outside"]),
                         (1, 1, 1))
        # the shell's mention of the child's rm is the same operation: counted once
        self.assertEqual(a["refs"], {"cwd": 1, "home": 1, "shared": 1})
        self.assertEqual(a["writes"], {"shared": 1})
        self.assertEqual(out["coverage"]["dedup_dropped"], 1)
        self.assertEqual(a["radius"], {"root": 1})

        (row,) = out["rows"].values()
        self.assertEqual(row["anchor"], "/home/u/proj")
        self.assertEqual((row["radius"], row["root_escape"]), (3, True))
        self.assertEqual((row["write_radius"], row["outside_dirs"], row["outside_writes"]),
                         (3, 2, 1))
        self.assertEqual(row["approval"], "bypassed")
        top = {d["dir"]: d for d in row["top_outside"]}
        self.assertEqual(top["/scratch/u/old"]["writes"], 1)
        self.assertTrue(top["/home/u/other"]["cd"])
        self.assertEqual(set(out["ranks"]), {"radius", "writes", "dirs", "other_home"})

        dirs = {d["dir"]: d for d in out["dirs"]}
        self.assertEqual(dirs["/scratch/u/old"]["zone"], "shared")
        self.assertEqual(dirs["/scratch/u/old"]["sessions"], 1)

    def test_a_human_moving_between_directories_is_seen_through_its_cwd(self):
        recs = [rec(300 + i, 299, "ls", cwd="/home/u/proj", actor3="human",
                    tty="pts/1", agent_pid=None) for i in range(3)]
        recs.append(rec(310, 299, "ls", cwd="/home/u/other", actor3="human",
                        tty="pts/1", agent_pid=None))
        out = self.feed(recs)
        h = out["by_class"]["human"]
        self.assertEqual((h["sessions"], h["sessions_outside"]), (1, 1))
        (row,) = out["rows"].values()
        self.assertEqual(row["anchor"], "/home/u/proj")
        self.assertEqual([d["dir"] for d in row["top_outside"]], ["/home/u/other"])

    def test_the_three_classes_stay_three(self):
        recs = self.agent_session()
        recs.append(rec(400, 1, "cat /tmp/x.log", actor3="human-vscode", agent_pid=None,
                        tty="pts/2"))
        out = self.feed(recs)
        self.assertIn("human-vscode", out["by_class"])
        self.assertEqual(out["by_class"]["agent"]["sessions"], 1)

    def test_the_agents_own_state_is_shown_but_is_not_reach(self):
        out = self.feed([rec(201, 100, "mkdir -p /tmp/claude-1000/proj/scratchpad"),
                         rec(202, 100, "cat /home/u/.claude/settings.json")])
        a = out["by_class"]["agent"]
        self.assertEqual((a["sessions"], a["sessions_outside"]), (1, 0))
        self.assertEqual(a["refs"], {"tool-state": 2})
        self.assertEqual(a["writes"], {"tool-state": 1})
        self.assertEqual(a["radius"], {"0": 1})
        self.assertEqual(out["rows"], {})

    def test_another_users_home_is_always_findable(self):
        # twenty sessions that reach further, and one that reaches into a
        # neighbour's home: the plate counts it, so the table must list it
        recs = [rec(500 + i, 400 + i, "rm -rf /tmp/x%d/y" % i, agent_pid=400 + i)
                for i in range(20)]
        recs.append(rec(901, 900, "cat /home/v/notes/plan.md", agent_pid=900))
        out = self.feed(recs)
        self.assertEqual(out["by_class"]["agent"]["sessions_other_home"], 1)
        (sid,) = out["ranks"]["other_home"]
        self.assertEqual(out["rows"][sid]["other_home_dirs"], 1)
        self.assertEqual(out["dirs"][0]["zone"], "other-home")

    def test_an_unknown_owner_never_reads_as_someone_elses_home(self):
        r = rec(201, 100, "ls /home/u/proj2", user=None)
        r["uid"] = 4321
        out = self.feed([r])
        self.assertEqual(out["by_class"]["agent"]["refs"], {"other": 1})
        self.assertEqual(out["by_class"]["agent"]["sessions_other_home"], 0)

    def test_the_other_home_ranking_is_capped_and_says_how_many(self):
        recs = [rec(500 + i, 400 + i, "cat /home/v%d/x.md" % i, agent_pid=400 + i)
                for i in range(120)]
        out = self.feed(recs)
        self.assertEqual(len(out["ranks"]["other_home"]), 100)
        self.assertEqual(out["rank_totals"]["other_home"], 120)

    def test_a_tree_launched_at_root_has_no_radius_to_measure(self):
        out = self.feed([rec(201, 201, "ls /scratch/x", cwd="/", agent_pid=201)])
        a = out["by_class"]["agent"]
        self.assertEqual((a["sessions"], a["no_anchor"]), (0, 1))
        self.assertEqual(a["refs"], {"shared": 1})
        self.assertEqual(out["rows"], {})

    def test_a_deleted_launch_dir_still_anchors_the_session(self):
        out = self.feed([rec(201, 100, "ls /tmp/x",
                             cwd="/home/u/gone (deleted)")])
        (row,) = out["rows"].values()
        self.assertEqual(row["anchor"], "/home/u/gone")


class PanelWiringTest(FeedCase):

    def test_the_panel_is_served_beside_the_others(self):
        from rc_dashboard.aggregator import Aggregator
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.start()
        try:
            agg.backfill_now()
            agg.panels(1440)
            deadline = time.time() + 20
            while time.time() < deadline:
                held = {w["minutes"]: w for w in agg.panels_by_window.status()["held"]}
                if held[1440]["state"] in ("ready", "error"):
                    break
                time.sleep(0.05)
            panel = agg.panels(1440)["panels"]["sporadicity"]
            self.assertTrue(panel["_present"])
            self.assertEqual(panel["coverage"]["records"], {"agent": 20, "human": 10})
        finally:
            agg.stop()


if __name__ == "__main__":
    unittest.main()
