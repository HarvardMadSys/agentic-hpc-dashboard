"""The synthetic feed as a pure function of (seed, hosts, start, clock).

Each local day of each host has a PLAN: which of its people work that day, and
the sessions they start -- a few numbers per session, drawn from a generator
seeded on (seed, host, day, user), so one person's plan never moves another's.
A session is BUILT into its records only when a window being written overlaps
it: every record it will ever write, whole, from its own seeded generator. A
window keeps the records that fall in it. So `records(a, b) + records(b, c)` is
`records(a, c)`, byte for byte, which is what lets a stopped demo resume. On disk
(feed.py) that is, for JSONL: same bytes; for EBR: same decoded records, since an
EBR file's frames and segments follow the rounds that wrote it.

Pids come from a block per (day, session), so two sessions on one host never
share a pid on a day -- a row is keyed on its root's pid and its day, so two would
read as one -- and they recycle weekly, as pids do.
"""
import bisect
import random
import time
from datetime import date, timedelta

from . import agents, cast, host as hostmod, sessions

# Bump when what a seed writes changes: a feed from another generator is
# written again rather than resumed into a mixture of the two.
GENERATOR = 2

# The longest any session runs: how far back a window looks for plans.
MAX_SESSION_S = 20 * 3600
# 96 sessions a host a day (one host holds every person: ~75 at most), 5000 pids
# each, a week of them below the kernel's pid_max (4194304).
PID_BASE, PID_BLOCK, PID_SLOTS = 200000, 5000, 96
TAIL_S = 2.0


def midnight(day):
    y, m, d = (int(x) for x in day.split("-"))
    return time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))


def midnight_after(day):
    return midnight((date(*(int(x) for x in day.split("-"))) + timedelta(1)).isoformat())


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def days_between(t0, t1):
    """Local days that overlap [t0, t1)."""
    a = date.fromtimestamp(t0)
    b = date.fromtimestamp(max(t0, t1 - 1e-3))
    return [(a + timedelta(n)).isoformat() for n in range((b - a).days + 1)]


class Spec:
    """One planned session: who, what kind, when. The build fills in the rest."""
    __slots__ = ("key", "host", "person", "kind", "start", "end", "seed", "idx",
                 "pid_base", "day")

    def __init__(self, host, person, kind, start, end, seed, day):
        self.host, self.person, self.kind = host, person, kind
        self.start, self.end, self.seed, self.day = start, end, seed, day
        self.key = self.idx = self.pid_base = None


class Built:
    """A session's records, serialised, in file order."""
    __slots__ = ("spec", "items", "keys", "census")

    def __init__(self, spec, recs, census):
        self.spec = spec
        recs.sort(key=lambda r: (r[0], r[1]))
        self.items = [(t, "%s|%06d" % (spec.key, seq), tier, spec.host, stream, line)
                      for t, seq, tier, stream, line in recs]
        self.keys = [x[0] for x in self.items]
        # minute epoch -> [(class, agent_type, user, n_procs, threads, rss_mb, d_state)]
        self.census = census

    def between(self, t0, t1):
        return self.items[bisect.bisect_left(self.keys, t0):bisect.bisect_left(self.keys, t1)]


class World:
    def __init__(self, seed, n_hosts, start):
        self.seed = str(seed)
        self.start = float(start)
        self.hosts = ["demo-login%02d" % (i + 1) for i in range(n_hosts)]
        self._plans = {}
        self._built = {}
        self.hostgen = {h: hostmod.HostFeed(self, h, i) for i, h in enumerate(self.hosts)}

    # --------------------------------------------------------------- plans
    def plan(self, host, day):
        key = (host, day)
        if key in self._plans:
            return self._plans[key]
        d0 = midnight(day)
        weekend = time.localtime(d0 + 43200).tm_wday >= 5
        specs = []
        for p in cast.PEOPLE:
            if cast.hosts_of(p, self.hosts) != host:
                continue
            rng = random.Random("%s|plan|%s|%s|%s" % (self.seed, host, day, p["user"]))
            if rng.random() >= p["days"][1 if weekend else 0]:
                continue
            n = rng.randint(*p["sessions"])
            h0, span = p["hours"]
            begin = d0 + (h0 + rng.uniform(-0.75, 0.75)) * 3600
            stop = begin + span * 3600 * rng.uniform(0.85, 1.1)
            kinds = sorted(p["kinds"].items())
            t = begin
            for k in range(n):
                kind = rng.choices([x for x, _ in kinds], [w for _, w in kinds])[0]
                dur = sessions.duration(kind, rng)
                seed = "%s|%s|%s|%s|%d" % (self.seed, host, day, p["user"], k)
                if kind == "vscode_parked":
                    # the whole local day, every day: one server per day, back to back
                    specs.append(Spec(host, p, kind, d0 + 30, midnight_after(day) - 30, seed, day))
                    break
                if kind in sessions.START_AT:
                    # at its own time of night: one a day, not in the day's sequence
                    if not any(x.kind == kind and x.person is p for x in specs):
                        s0 = d0 + rng.uniform(*sessions.START_AT[kind]) * 3600
                        specs.append(Spec(host, p, kind, s0, s0 + dur, seed, day))
                    continue
                room = max(60.0, (stop - t) / (n - k) - dur)
                s0 = t + rng.uniform(0, room)
                specs.append(Spec(host, p, kind, s0, s0 + dur, seed, day))
                t = s0 + dur + rng.uniform(300, 2400)
                if t >= stop:
                    break
        specs.sort(key=lambda s: (s.start, s.person["user"]))
        week = date(*(int(x) for x in day.split("-"))).toordinal() % 7
        for i, s in enumerate(specs):
            s.idx = i
            s.key = "%s|%s|%03d" % (host, day, i)
            s.pid_base = PID_BASE + (week * PID_SLOTS + i % PID_SLOTS) * PID_BLOCK
        self._plans[key] = specs
        if len(self._plans) > 64:               # plans are cheap; keep the recent ones
            for k in sorted(self._plans, key=lambda k: k[1])[:16]:
                self._plans.pop(k, None)
        return specs

    def specs(self, host, t0, t1):
        """Every planned session of `host` that is alive somewhere in [t0, t1)."""
        out = []
        for day in days_between(t0 - MAX_SESSION_S, t1):
            for s in self.plan(host, day):
                # a session's last records may land a moment past its planned end
                # (agents.build keeps them to TAIL_S), so a window looks that far
                if s.start < t1 and s.end + TAIL_S > t0 and s.end + TAIL_S > self.start:
                    out.append(s)
        return out

    def built(self, spec):
        b = self._built.get(spec.key)
        if b is None:
            recs, census = agents.build(spec, self)
            # nothing before the collector started: a session it found running
            # is seen from then on, as a real one would be
            recs = [r for r in recs if r[0] >= self.start]
            b = self._built[spec.key] = Built(spec, recs, census)
        return b

    def census(self, host, minute):
        """What was running on `host` at `minute`, from every session alive then."""
        out = []
        for s in self.specs(host, minute, minute + 1):
            out += self.built(s).census.get(minute, ())
        return out

    # ------------------------------------------------------------- records
    def records(self, t0, t1):
        """Every record with t0 <= ts_epoch < t1, in the order a collector writes them,
        as `(epoch, tier, host, stream, line)`."""
        t0 = max(t0, self.start)
        if t1 <= t0:
            return []
        items = []
        for host in self.hosts:
            for s in self.specs(host, t0, t1):
                items += self.built(s).between(t0, t1)
            items += self.hostgen[host].between(t0, t1)
        items.sort(key=lambda x: (x[0], x[1]))
        # a session long over will not be asked for again
        for k in [k for k, b in self._built.items() if b.spec.end < t0 - 600]:
            del self._built[k]
        return [(t, tier, host, stream, ln) for t, _o, tier, host, stream, ln in items]
