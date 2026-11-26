"""Basis notes -- `notes`, `basis` and every `*_basis` key, which say in words how a
panel counts -- stay off the page's payloads, which show none of them.

    cd dashboard && .venv/bin/python -m unittest tests.test_basis -q
"""
import json
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import jsonout                    # noqa: E402
from test_anonymize_leak import Served              # noqa: E402


def basis_keys(obj, path=""):
    """Every basis key in `obj`, at any depth, lists included, by its path."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if jsonout.is_basis(k):
                out.append("%s/%s" % (path, k))
            out += basis_keys(v, "%s/%s" % (path, k))
    elif isinstance(obj, list):
        for v in obj:
            out += basis_keys(v, path + "[]")
    return out


class StripTest(unittest.TestCase):

    def test_basis_keys_go_and_the_rest_is_shared(self):
        rows = [{"basis": "kept: a list is not walked"}]
        held = {"a": {"cpu_basis": "x", "n": 1}, "notes": {"y": "z"}, "rows": rows,
                "b": {"n": 2}}
        got = jsonout.strip_basis(held)
        self.assertEqual(got, {"a": {"n": 1}, "rows": rows, "b": {"n": 2}})
        self.assertIs(got["b"], held["b"])           # unchanged: shared, not copied
        self.assertIn("notes", held)                  # what the service holds is untouched
        self.assertIn("cpu_basis", held["a"])
        same = {"a": {"n": 1}}
        self.assertIs(jsonout.strip_basis(same), same)


class PagePayloadTest(Served):
    """The real app over a small feed (test_anonymize_leak)."""

    def test_the_pages_payloads_carry_no_basis_notes(self):
        ranks = json.loads(self.get("/api/trajectories")[1]).get("ranks") or {}
        sid = (ranks.get("events") or [None])[0]
        self.assertTrue(sid)
        alias = self.anon.user_alias("alice")
        for path in ("/api/panels", "/api/trajectories", "/api/users",
                     "/api/user?name=" + urllib.parse.quote(alias),
                     "/api/trajectory?sid=" + urllib.parse.quote(sid)):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200, body[:200])
                self.assertEqual(basis_keys(json.loads(body)), [])


if __name__ == "__main__":
    unittest.main()
