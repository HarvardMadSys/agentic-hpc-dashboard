"""The tool mix by any one resource: disk, network, and memory beside processes
and CPU (roadmap notes 24 and 25).

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.tools import ToolMixReducer          # noqa: E402


class Cfg:
    def get(self, dotted, default=None):
        return {"reducers.tool_mix": {"head_n": 1}}.get(dotted, default)


def ex(tool, pid, cpu=0.0, rd=None, wr=None, net=None, rss=None, atype="claude_code",
       cls="agent"):
    r = {"event": "exit", "_a3": cls, "_eff": tool, "_command": tool, "_sid": "s",
         "pid": pid, "ppid": 1, "cpu_s": cpu, "agent_type": atype}
    if rd is not None or wr is not None:
        r["io"] = {"rd_mb": rd, "wr_mb": wr}
    if net is not None:
        r["net_tx_bytes"] = net
    if rss is not None:
        r["peak_rss_mb"] = rss
    return r


def reduce(recs):
    r = ToolMixReducer(Cfg(), ["agent", "human", "ide"])
    for rec in recs:
        r.feed(rec)
    return r


class ToolCostTest(unittest.TestCase):

    def recs(self):
        return [ex("git", 1, cpu=0.1), ex("git", 2, cpu=0.1),           # most processes
                ex("python", 3, cpu=9.0, rss=100.0),                     # most CPU
                ex("rsync", 4, rd=500.0, wr=100.0, net=4e6),             # most disk and network
                ex("node", 5, rss=2000.0), ex("node", 6, rss=1000.0)]    # most memory

    def test_each_resources_top_tool_is_listed_with_every_resource(self):
        tools = {t["tool"]: t for t in reduce(self.recs()).result()["tools"]["agent"]}
        self.assertEqual(set(tools), {"git", "python", "rsync", "node"})   # head_n 1, per resource
        self.assertEqual((tools["rsync"]["io_mb"], tools["rsync"]["net_mb"]), (600.0, 4.0))
        self.assertEqual((tools["node"]["mem_mean_mb"], tools["node"]["mem_max_mb"]), (1500.0, 2000.0))
        self.assertIsNone(tools["git"]["mem_mean_mb"])                   # none reported: none, not 0

    def test_a_kind_of_tool_carries_its_disk_and_network_share(self):
        rows = {r["b"]: r for r in reduce(self.recs()).result()["by_class"]["agent"]["rows"]}
        self.assertEqual(rows["net/data"]["io_pct"], 100.0)
        self.assertEqual(rows["net/data"]["net_mb"], 4.0)

    def test_merged_hours_add_the_sums_and_keep_the_larger_max(self):
        a, b = reduce(self.recs()[:5]), reduce(self.recs()[5:])
        a.merge(b)
        node = next(t for t in a.result()["tools"]["agent"] if t["tool"] == "node")
        self.assertEqual((node["mem_mean_mb"], node["mem_max_mb"], node["calls"]), (1500.0, 2000.0, 2))

    def test_a_kind_of_tool_carries_its_memory_share(self):
        rows = {r["b"]: r for r in reduce(self.recs()).result()["by_class"]["agent"]["rows"]}
        mem = {b: r["mem_pct"] for b, r in rows.items() if r["mem_mb"]}
        self.assertAlmostEqual(sum(mem.values()), 100.0, places=1)
        node = next(b for b in rows if rows[b]["mem_mb"] == 3000.0)
        self.assertAlmostEqual(rows[node]["mem_pct"], 100 * 3000 / 3100, places=1)


class PerAgentTypeTest(unittest.TestCase):
    """The Agents tab's "nothing mixes types": each agent type's tools alone."""

    def test_each_agent_type_has_its_own_mix_tools_and_head(self):
        out = reduce([ex("git", 1), ex("git", 2), ex("python", 3, cpu=5.0, atype="codex"),
                      ex("vim", 4, atype=None, cls="human")]).result()
        self.assertEqual(set(out["by_agent_type"]), {"claude_code", "codex"})
        cc, cx = out["by_agent_type"]["claude_code"], out["by_agent_type"]["codex"]
        self.assertEqual((cc["by"]["calls"], cx["by"]["calls"]), (2, 1))
        self.assertEqual([t["tool"] for t in cx["tools"]], ["python"])
        self.assertEqual([h["tool"] for h in cc["head"]], ["git"])
        self.assertEqual(cx["n_tools"], 1)
        # the class still holds both, and no type key leaks in as a class
        self.assertEqual(out["by_class"]["agent"]["calls"], 3)
        self.assertEqual(set(out["by_class"]), {"agent", "human"})

    def test_merged_hours_keep_the_types_apart(self):
        a = reduce([ex("git", 1)])
        a.merge(reduce([ex("python", 2, atype="codex")]))
        self.assertEqual(set(a.result()["by_agent_type"]), {"claude_code", "codex"})


if __name__ == "__main__":
    unittest.main()
