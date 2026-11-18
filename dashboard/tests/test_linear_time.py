"""A command line costs time in proportion to its length, whatever is in it.

Command lines are written by arbitrary users on a shared machine, and every one an agent's session
runs is read here, under the aggregator's lock, in every window build and hour reduction. Each
test below is one shape that took super-linear time, at about the length the collector keeps (its
in-kernel argv read is 4,096 bytes) or, where the old cost only passed a second further out, at
that length. The bounds are many times what the code now takes and well under what it took, so a
slow, busy machine does not fail them. The rewrites are held to what they replace on random
input, so that no classification changes.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_linear_time.py' -v
"""
import random
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard.reducers import phase_names as N  # noqa: E402
from rc_dashboard.reducers import phase_rules as R  # noqa: E402

BOUND = 0.25        # seconds; what each shape below took is in its test


def timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return time.perf_counter() - t0, out


def backslash_lines(n):
    """`n` lines that each end in three backslashes: no line lexes alone, and no quote closes."""
    return "\n".join(["a\\\\\\"] * n)


def lines_as_they_were(text):
    """`phase_rules._lines` before it found where a quote closes in one pass: the reference."""
    if len(text) > R._MAX_TEXT:
        k = text.rfind("\n", 0, R._MAX_TEXT)
        text = text[:k if k > 0 else R._MAX_TEXT]
    lines = text.split("\n")
    n, i = len(lines), 0
    while i < n:
        line = lines[i]
        i += 1
        while line.endswith("\\") and not line.endswith("\\\\") and i < n:
            line = line[:-1] + " " + lines[i]
            i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        bodies = []

        def cut(m):
            nonlocal i
            end = next(g for g in m.groups()[1:] if g is not None)
            body = []
            while i < n:
                l = lines[i]  # noqa: E741
                i += 1
                if (l.lstrip("\t") if m.group(1) else l) == end:
                    break
                body.append(l)
            bodies.append("\n".join(body))
            return " \x00%d\x00 " % (len(bodies) - 1)

        if "<<" in line:
            line = R._HEREDOC_OP.sub(cut, line)
        first, resume, lexed, k = line, i, R._lex(line), 0
        while lexed is None and i < n and k < R._LOOKAHEAD:
            line += "\n" + lines[i]
            i += 1
            k += 1
            lexed = R._lex(line)
        if lexed is None:
            i, toks = resume, first.split()
            lexed = toks, [False] * len(toks)
        yield lexed[0], lexed[1], bodies


def shell_soup(rnd, pieces, n):
    return "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, n)))


SOUP = ["'", '"', "\\", "\n", " ", "a", "b c", "x\\\n", "<<EOF\n", "EOF\n", "<<-'Q'\n", "\tQ\n",
        "# c\n", ";", "|", "$(", ")", "{ ", "} ", "[[ ", "]] ", "if ", "then ", "fi\n", "echo ",
        "git commit -m ", "cat <<'E' > n.md\n", "E\n", "&& ", "`", "=", "x=1 "]


class LookAheadTest(unittest.TestCase):
    """`_lines` lexed a growing join up to 40 times for every line that failed to lex: 2 s for
    1,000 lines that each end in three backslashes (5 KB), and 2.2 s in `TrajectoryReducer.feed`
    for one 4 KB Codex call of 817 of them."""

    def test_the_quote_scanner_fails_where_the_lexer_fails(self):
        rnd = random.Random(1)
        for _ in range(20000):
            s = shell_soup(rnd, SOUP, 12)
            self.assertEqual(R._lex(s) is None, bool(R._left_open(s)), repr(s))

    def test_the_lines_are_those_of_the_look_ahead_as_it_was(self):
        rnd = random.Random(2)
        saved = R._LOOKAHEAD_MISSES
        try:
            R._LOOKAHEAD_MISSES = 10 ** 9           # no budget: the look-ahead alone
            for _ in range(3000):
                s = shell_soup(rnd, SOUP, 60)
                self.assertEqual(list(R._lines(s)), list(lines_as_they_were(s)), repr(s))
        finally:
            R._LOOKAHEAD_MISSES = saved
        for _ in range(3000):                        # with it: texts leave a few quotes open
            s = shell_soup(rnd, SOUP, 60)
            self.assertEqual(list(R._lines(s)), list(lines_as_they_were(s)), repr(s))

    def test_past_its_budget_a_text_is_not_looked_past(self):
        """Eight quotes open for good, then one that closes on the next line: it is split on
        whitespace, where the look-ahead read it as one word."""
        filler = "\n".join(["x"] * 41)
        text = "\n".join(["echo \"open %d\n%s" % (k, filler) for k in range(8)]) + "\necho \"a\nb\""
        last = list(R._lines(text))[-2:]
        self.assertEqual(last[0][0], ["echo", '"a'])
        self.assertEqual(list(lines_as_they_were(text))[-1][0], ["echo", "a\nb"])

    def test_four_kb_of_lines_that_never_close(self):
        text = backslash_lines(817)                  # 4,084 characters
        for fn in (R.category_of.__wrapped__, N.tagset.__wrapped__):
            dt, _ = timed(fn, text)
            self.assertLess(dt, BOUND, fn.__name__)  # each took 1.5 s

    def test_a_codex_call_of_four_kb_through_the_reducer(self):
        from test_traj_phases import T0, fed, root8, s8
        kw = dict(root=300, agent_type="codex")
        recs = [root8(T0, root=300, command="codex", args="codex", agent_type="codex"),
                s8(T0 + 10, 301, 300, "bash", "/usr/bin/bash -lc " + backslash_lines(800), **kw)]
        R.category_of.cache_clear()
        N.tagset.cache_clear()
        dt, _ = timed(fed, recs)
        self.assertLess(dt, BOUND)                   # 2.2 s


class PatternTest(unittest.TestCase):
    """Patterns that matched in super-linear time, rewritten to match the same texts."""

    def test_an_open_mode_is_read_one_way(self):
        old = re.compile(r"['\"][rwxabt+]*[wax+][rwxabt+]*['\"]")
        new = re.compile(R._FILE_MODE)
        alphabet = "rwxabt+z"
        words = [""]
        for _ in range(6):
            words = [w + c for w in words for c in alphabet] + [""]
            for w in set(words):
                for q in ("''", '""', "'\""):
                    s = q[0] + w + q[1]
                    self.assertEqual(bool(old.fullmatch(s)), bool(new.fullmatch(s)), s)
        dt, m = timed(R._PY_WRITES.search, "open(p,'" + "w" * 16000 + "z')")
        self.assertIsNone(m)
        self.assertLess(dt, BOUND)                   # 1.4 s
        dt, cat = timed(R.category_of.__wrapped__, "python3 -c \"open(p,'" + "w" * 4000 + "z')\"")
        self.assertLess(dt, BOUND)

    def test_codex_sourcing_its_snapshot_is_found_as_it_was(self):
        old = re.compile(r"""(?:^|[;&|\n({])\s*(?:source|\.)\s+["']?\S*/\.codex/""")
        rnd = random.Random(4)
        pieces = [";", "&", "|", "\n", "(", "{", " ", "\t", "\r", "\x0b", ".", "source", "x",
                  "/h/.codex/s.sh", '"', "'", "cp -r . ~/.codex/b", "if "]
        for _ in range(20000):
            s = shell_soup(rnd, pieces, 14)
            self.assertEqual(bool(old.search(s)), bool(R._CODEX_SOURCED.search(s)), repr(s))
        dt, m = timed(R._CODEX_SOURCED.search, "x" + "\n" * 16000 + "y")
        self.assertIsNone(m)
        self.assertLess(dt, BOUND)                   # 1.3 s
        rec = {"_command": "bash", "args": "bash -c x" + "\n" * 16000 + "y"}
        dt, kind = timed(R.classify_direct, rec, "codex")
        self.assertEqual(kind, ("call", "shell"))
        self.assertLess(dt, BOUND)

    def test_the_apps_asset_reads_are_found_as_they_were(self):
        old = re.compile(
            r"^(?:(?:\S*/)?cat|/proc/self/exe --codex-run-as-process-setup (?:\S+ )+?/bin/cat /bin/cat) "
            r"/(?:\S*/)?\.codex/(?:\.tmp/(?:plugins|bundled-marketplaces)|plugins/cache)/"
            r"(?:\S*/)?assets/\S+$")
        rnd = random.Random(5)
        heads = ["cat ", "/bin/cat ", "/usr/bin/cat ", "rg ",
                 "/proc/self/exe --codex-run-as-process-setup x /bin/cat /bin/cat ",
                 "/proc/self/exe --codex-run-as-process-setup /bin/cat /bin/cat "]
        parts = ["/h", "/.codex", "/.tmp", "/plugins", "/bundled-marketplaces", "/cache", "/assets",
                 "/assets/", "/a.png", "/x", " ", "/", ".codex/", "assets"]
        for _ in range(20000):
            s = rnd.choice(heads) + shell_soup(rnd, parts, 10)
            self.assertEqual(bool(old.match(s)), bool(R._APP_ASSETS.match(s)), s)
        k = 160
        args = "cat /x" + "/.codex/plugins/cache" * k + "/assets" * (3 * k) + "/a b"   # 6.7 KB
        dt, kind = timed(R.classify_direct, {"_command": "cat", "args": args}, "codex")
        self.assertEqual(kind, ("call", "bare"))
        self.assertLess(dt, BOUND)                   # 0.97 s


class SplitterTest(unittest.TestCase):
    """Where a command starts was read again over every word of the command for each `{`, `[[`
    and `}`: 0.6 s for 16,000 characters of `{ `."""

    def test_the_commands_are_those_of_the_full_reading(self):
        rnd = random.Random(6)
        full = lambda self: all(w in R._KEYWORDS for w in self.cur)    # noqa: E731
        texts = [shell_soup(rnd, SOUP, 50) for _ in range(4000)]
        saved = R._Splitter._at_start
        try:
            R._Splitter._at_start = full
            want = [R._split(t) for t in texts]
        finally:
            R._Splitter._at_start = saved
        for t, w in zip(texts, want):
            self.assertEqual(R._split(t), w, repr(t))

    def test_a_run_of_braces(self):
        sp = R._Splitter()
        t0 = time.perf_counter()
        for _ in range(20000):
            sp.word("{")
        self.assertLess(time.perf_counter() - t0, BOUND)   # about 2 s
        dt, _ = timed(R.category_of.__wrapped__, "{ " * 2000)
        self.assertLess(dt, BOUND)


class HeredocMessageTest(unittest.TestCase):
    """`_heredoc_messages` read a heredoc it then did not take again from every opening inside it:
    1 s for 1,600 nested openings (39 KB)."""

    def nested(self, m):
        opens = "".join('-m "$(cat <<D%d\n' % k for k in range(m))
        closes = "".join("D%d\nx\n" % k for k in reversed(range(m)))
        return "git commit " + opens + closes

    def test_nested_openings(self):
        text = self.nested(1600)
        dt, out = timed(N._heredoc_messages, text)
        self.assertIs(out, text)                     # none is the whole value: nothing taken
        self.assertLess(dt, BOUND)

    def test_openings_on_a_last_line_with_no_newline(self):
        """None can match, as every opening ends in a newline, but each was tried to the end of
        the line: 0.46 s for 8,000 of them (128 KB)."""
        text = "git commit\n" + '-m "$(cat <<\'x\' ' * 8000
        dt, out = timed(N._heredoc_messages, text)
        self.assertIs(out, text)
        self.assertLess(dt, BOUND)

    def test_a_heredoc_not_taken_is_passed_over_body_and_all(self):
        """An opening inside the body of one that was read and not taken is data, as one inside
        a body that was taken is."""
        inner = '-m "$(cat <<\'I\'\nInner subject\nI\n)"'
        text = 'git commit -m "$(cat <<\'O\'\nOuter\n%s\nO\n) and more"' % inner
        self.assertIs(N._heredoc_messages(text), text)
        taken = 'git commit -m "$(cat <<\'EOF\'\nFix the thing\n\nBody.\nEOF\n)"'
        self.assertEqual(N._heredoc_messages(taken), "git commit -m 'Fix the thing'")
        self.assertEqual(N._heredoc_messages(taken + " && git push"),
                         "git commit -m 'Fix the thing' && git push")
        cut = 'git commit -m "$(cat <<\'EOF\'\nFix the thing\nEOF\n)'     # its quote cut off
        self.assertEqual(N._heredoc_messages(cut), "git commit -m 'Fix the thing'")
        more = cut + "\n)"
        self.assertIs(N._heredoc_messages(more), more)


if __name__ == "__main__":
    unittest.main()
