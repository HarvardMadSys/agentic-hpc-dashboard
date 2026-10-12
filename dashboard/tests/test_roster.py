"""User roster storage: tiered bins, compact slots, a cached window.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import roster as rmod                              # noqa: E402
from rc_dashboard.roster import UserRoster                          # noqa: E402

DAY = 86400
NOW = (1_790_000_000 // 3600) * 3600 + 1234      # mid-bin, fixed


def flat(r, e):
    """A flat entry as {(class, type): [n, cpu]}, for comparing."""
    out = {}
    for key, n, cpu in r.decode(e):
        out[key] = [n, round(cpu, 6)]
    return out


def brute(recs, now, window_s, fine_s=600, coarse_s=3600, span=DAY):
    """What the roster should hold: each record in the bin it lands in, a bin
    counted when it overlaps the window."""
    cutoff = now - window_s
    out = {}
    for t, u, c, at, cpu in recs:
        if t < now - span:
            end = (t // coarse_s + 1) * coarse_s
        else:
            end = (t // fine_s + 1) * fine_s
        if end <= cutoff:
            continue
        d = out.setdefault(u, {}).setdefault((c, at), [0, 0.0])
        d[0] += 1
        d[1] = round(d[1] + cpu, 6)
    return out


class TierTest(unittest.TestCase):
    def setUp(self):
        rnd = random.Random(7)
        self.recs = [(NOW - rnd.uniform(0, 3 * DAY), f"u{rnd.randrange(30)}",
                      rnd.choice(["agent", "human", "ide"]),
                      rnd.choice(["claude_code", "codex", ""]), round(rnd.uniform(0, 2), 3))
                     for _ in range(5000)]
        self.r = UserRoster(7 * DAY)
        for t, u, c, at, cpu in self.recs:
            self.r.add(t, u, c, at, cpu, now=NOW)

    def check(self, window_s):
        got = {u: flat(self.r, e) for u, e in self.r.totals(NOW, window_s).items()}
        self.assertEqual(got, brute(self.recs, NOW, window_s))

    def test_matches_brute_force_at_every_window(self):
        for w in (3600, 6 * 3600, DAY, 3 * DAY, 7 * DAY):
            self.check(w)

    def test_old_records_land_hourly_recent_ones_in_ten_minutes(self):
        self.assertTrue(all((i + 1) * 600 > NOW - DAY for i in self.r.fine))
        self.assertTrue(self.r.coarse)

    def test_rolling_folds_day_old_bins_without_changing_a_week(self):
        before = {u: flat(self.r, e) for u, e in self.r.totals(NOW, 7 * DAY).items()}
        later = NOW + 2 * 3600
        n_fine = len(self.r.fine)
        self.r.roll(later)
        self.assertLess(len(self.r.fine), n_fine)
        rmod.REBUILD_S, keep = 0, rmod.REBUILD_S
        try:
            after = {u: flat(self.r, e) for u, e in self.r.totals(later, 8 * DAY).items()}
        finally:
            rmod.REBUILD_S = keep
        self.assertEqual(after, before)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.r = UserRoster(7 * DAY)
        self.r.add(NOW - 3000, "ann", "agent", "codex", 1.0, now=NOW)

    def n(self, now, user="ann"):
        e = self.r.totals(now, 3600).get(user)
        return sum(n for _, n, _ in self.r.decode(e)) if e else 0

    def test_open_bin_is_read_fresh_every_time(self):
        self.assertEqual(self.n(NOW), 1)
        self.r.add(NOW - 1, "ann", "agent", "codex", 1.0, now=NOW)
        self.assertEqual(self.n(NOW), 2)

    def test_a_late_record_shows_within_the_rebuild_interval(self):
        self.assertEqual(self.n(NOW), 1)
        self.r.add(NOW - 2000, "ann", "agent", "codex", 1.0, now=NOW)  # a closed bin
        self.assertEqual(self.n(NOW + 1), 1)                       # cached, briefly
        self.assertEqual(self.n(NOW + rmod.REBUILD_S + 1), 2)

    def test_reads_never_change_the_stored_lists(self):
        self.r.add(NOW - 1, "ann", "agent", "codex", 1.0, now=NOW)
        for _ in range(3):
            self.assertEqual(self.n(NOW), 2)


class RetentionTest(unittest.TestCase):
    def test_users_past_retention_are_forgotten(self):
        r = UserRoster(DAY, fine_span_s=3600)
        r.add(NOW - 3600, "old", "human", "", 0.0, now=NOW)
        r.add(NOW - 10, "new", "human", "", 0.0, now=NOW)
        r.roll(NOW + DAY)
        self.assertNotIn("old", r.last)
        self.assertEqual(r.totals(NOW + DAY, DAY).get("old"), None)

    def test_records_older_than_retention_are_dropped(self):
        r = UserRoster(DAY)
        self.assertFalse(r.add(NOW - 2 * DAY, "x", "agent", "codex", 1.0, now=NOW))
        self.assertEqual(r.totals(NOW, DAY), {})


if __name__ == "__main__":
    unittest.main()
