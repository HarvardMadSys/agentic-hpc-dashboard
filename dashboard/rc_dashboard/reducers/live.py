"""Requirement 2: the live tier, all three actor classes, measured.

Everything here replaces a fabricated equivalent.  The retired builder's
`rc_synth.live()` hardcoded `running_agent_jobs: 1184`, `pending: 417`,
`agent_gpu_jobs: 263`, `agent_gpu_idle_now: 61`, and the viewer's `tick()`
manufactured new exit events and rate samples every three seconds.  None of that
survives: a bin with no records is `null`, and a quantity with no feed is absent
with a notice rather than plausible.

Three of those four plates came from the scheduler census and dcgm, which are not
configured feeds.  Rather than carry the numbers over or leave a placeholder, the
jobs plate is replaced by one built from eBPF `submit` records -- agent job
submissions in the window, which is per-submission AND actor-attributed, and so
is better evidence than a census count.
"""
import collections
import time

from ..buckets import RollingBuckets, _epoch_of
from ..roster import UserRoster
from ..redact import redact
from .slurm import outcome as slurm_outcome, reason as slurm_reason


def _sum_or_none(vals):
    """Sum what was reported; None when nothing was, so an absent counter never
    renders as a measured 0."""
    seen = [v for v in vals if v is not None]
    return sum(seen) if seen else None


def _conn_count(v, kind="ext"):
    """One class's standing-socket count from `tcp_open_ext_by_actor3`.

    Schema 5 reports a triple per class -- {open, ext, estab} -- separating all
    standing sockets from the externally-addressed ones from the established
    ones. A bare int is accepted too, so an older or narrower emitter still
    reads rather than raising. `None` stays None: zero standing sockets and
    "the collector did not report" are different facts.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get(kind)
    if isinstance(v, (int, float)):
        return v
    return None


class LiveReducer:
    # conn / netio are read for one thing: the identity block of a running agent
    # root, which carries its launch flags. Residency alone cannot give that -- its
    # identity is the TREE root, which for a VS Code-launched Claude is the VS Code
    # server, and every bypassed session on rtx6000a was launched that way.
    EVENTS = ("exit", "residency", "residency_totals", "submit", "truncated", "conn", "netio")
    KEY = "live"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        # ONE instance, sized at RETENTION.  Every selected window is a slice of
        # these bins rather than its own accumulator: two accumulators over the
        # same stream are two chances to disagree about the same minute.
        self.retention_s = int(cfg.get("live.retention_min", 10080)) * 60
        self.bin_s = int(cfg.get("live.bin_s", 60))
        self.buckets = RollingBuckets(self.retention_s, self.bin_s,
                                      int(cfg.get("live.clock_skew_s", 120)))
        self.event_tail = collections.deque(maxlen=int(cfg.get("live.event_tail", 500)))
        self.submit_tail = collections.deque(maxlen=int(cfg.get("live.submit_tail", 50)))
        self.hosts = {}          # host -> newest residency_totals-derived rollup
        self.trees = {}          # (host, tree_root_pid) -> newest residency tick
        self.window_s = self.retention_s
        self.submits_in_window = collections.deque()
        self.conn_unmatched_births = None
        # (host, pid) -> [newest epoch, approval] for every agent-session root
        # seen in an identity block; an exit removes it.
        self.roots = {}
        # Who ran what, per account (roster.py: tiered bins, compact slots, a
        # cached window -- it has to hold at thousands of users).
        self.roster = UserRoster(
            self.retention_s,
            fine_s=int(cfg.get("live.user_bin_s", 600)),
            coarse_s=int(cfg.get("live.user_coarse_bin_s", 3600)),
            fine_span_s=int(cfg.get("live.user_fine_span_s", 86400)),
            skew_s=int(cfg.get("live.clock_skew_s", 120)))
        # The live frame carries the busiest N; a search reaches the rest
        # through /api/live/users, so a node with thousands of accounts does
        # not ship them all on every frame.
        self.user_rows = int(cfg.get("live.user_rows", 100))

    # ------------------------------------------------------------------ feed
    def feed(self, rec, older=False):
        """One record.  `older` says it predates everything already held.

        That is the backfill, which reads the history newest first while the
        tail goes on feeding new records.  The bins are sums and take records in
        any order; the "latest" state is not order-free, so an older record only
        fills what nothing newer has claimed -- a host's plate keeps its newest
        tick, and the tails grow at their OLD end.
        """
        ev = rec.get("event")
        self._root(rec, ev, older)
        if ev in ("exit", "truncated"):
            self._exit(rec, older)
        elif ev == "residency":
            self._residency(rec, older)
        elif ev == "residency_totals":
            self._totals(rec, older)
        elif ev == "submit":
            self._submit(rec, older)

    def _root(self, rec, ev, older=False):
        """Track the live agent-session roots and their launch approval.

        Only a process whose role is `agent`, on its own record: a tool shell or
        a child never speaks for its session (normalize.approval_of)."""
        pid = rec.get("pid")
        if rec.get("_role") != "agent" or rec.get("_a3") != "agent" \
                or pid is None or pid != rec.get("agent_pid"):
            return
        key = (rec.get("host"), pid)
        if ev == "exit":
            if not older:
                self.roots.pop(key, None)
            return
        e = rec.get("_ts_epoch")
        cur = self.roots.get(key)
        if cur is None or (e is not None and (cur[0] is None or e > cur[0])):
            self.roots[key] = [e, rec.get("_approval") or "unknown"]

    @staticmethod
    def _place(tail, row, older):
        """Newest at the left; an older row joins the right end while there is room."""
        if not older:
            tail.appendleft(row)
        elif len(tail) < tail.maxlen:
            tail.append(row)

    def _exit(self, rec, older=False):
        self.buckets.add(rec, rec["_a3"], cpu_s=rec.get("cpu_s"))
        self.roster.add(_epoch_of(rec), rec.get("user"), rec["_a3"],
                        rec.get("agent_type"), rec.get("cpu_s"))
        io = rec.get("io") or {}
        self._place(self.event_tail, {
            "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            "host": rec.get("host"), "user": rec.get("user"),
            "actor3": rec["_a3"], "agent_type": rec.get("agent_type"),
            "comm": rec.get("_command"), "tool": rec.get("_eff"),
            # Raw, and private to this process: masked once, the first time
            # the row is served (`result`), so the backfill's millions of rows
            # that age out unseen never pay for it. Cut after masking, never
            # before -- a cut can leave a token too short to recognise.
            "_argv": rec.get("args") or "",
            "purpose": rec.get("_purpose"), "depth": rec.get("depth"),
            "pid": rec.get("pid"),
            "duration_s": rec.get("duration_s"), "cpu_s": rec.get("cpu_s"),
            "peak_rss_mb": rec.get("peak_rss_mb"),
            "rd_mb": io.get("rd_mb"), "wr_mb": io.get("wr_mb"),
            "exit_code": rec.get("exit_code"), "signal": rec.get("signal"),
            "sandbox": rec.get("_sandbox"), "approval": rec.get("_approval"),
            "session_key": rec.get("session_key"),
        }, older)

    def _residency(self, rec, older=False):
        key = (rec.get("host"), rec.get("tree_root_pid") or rec.get("agent_pid"))
        if older and key in self.trees:
            return                       # a newer tick already holds this tree
        self.trees[key] = {
            "host": rec.get("host"), "agent_type": rec.get("agent_type"),
            "age_s": rec.get("age_s"), "n_procs": rec.get("n_procs"),
            "threads": rec.get("threads"), "rss_mb": rec.get("rss_mb"),
            "cpu_s": rec.get("cpu_s"), "d_state": rec.get("d_state"),
            "headless": rec.get("headless"), "max_depth": rec.get("max_depth"),
            "tcp_open_ext": rec.get("tcp_open_ext"),
            "state_counts": rec.get("state_counts"),
            "top_procs": rec.get("top_procs"),
            "sandbox": rec.get("_sandbox"), "approval": rec.get("_approval"),
            "epoch": rec.get("_ts_epoch"), "user": rec.get("user"),
            "actor3": rec.get("_a3"),
        }

    def _totals(self, rec, older=False):
        h = rec.get("host") or "?"
        if older and h in self.hosts:
            return                       # a newer tick already holds this host
        self.hosts[h] = {
            "host": h, "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            # under this dashboard's classes, IDEs included (normalize.class_totals)
            "by_actor3": rec.get("_by_class") or rec.get("by_actor3") or {},
            "by_agent_type": rec.get("by_agent_type") or {},
            "trees": rec.get("trees"), "tracked_live": rec.get("tracked_live"),
            "conn_records": rec.get("conn_records"),
            "tcp_open_ext": (rec.get("_tcp_by_class") or rec.get("tcp_open_ext_by_actor3")
                             or {}),
            "fork_rate": rec.get("fork_rate"),
            "dropped": (rec.get("dropped") or {}).get("n"),
            "d_stack_top": rec.get("d_stack_top"),
            "conn_unmatched_births": rec.get("conn_unmatched_births"),
        }
        if rec.get("conn_unmatched_births") is not None and not (
                older and self.conn_unmatched_births is not None):
            self.conn_unmatched_births = rec["conn_unmatched_births"]

    @staticmethod
    def _served(row):
        """The row as the API may show it: argv masked, in place, once."""
        if "_argv" in row:
            row["args"] = redact(row.pop("_argv"))[:400]
        return row

    def _submit(self, rec, older=False):
        row = {
            "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            "host": rec.get("host"), "user": rec.get("user"),
            "actor3": rec["_a3"], "agent_type": rec.get("agent_type"),
            "tool": rec.get("tool"), "job_id": rec.get("job_id"),
            "job_ids": rec.get("job_ids"),
            "work_dir": rec.get("work_dir"), "exit_code": rec.get("exit_code"),
            # schema 5 parses these from the tool's argv; null when the request
            # came from #SBATCH directives in the script instead
            "partition": rec.get("partition"), "gpus": rec.get("gpus"),
            "array": rec.get("array"), "time_limit_s": rec.get("time_limit_s"),
            "mem": rec.get("mem"), "cpus_per_task": rec.get("cpus_per_task"),
            "req_src": rec.get("req_src"),
            # what came back -- a job, a dry run, a step, a refusal -- and the
            # command itself, masked like every other argv this serves
            "outcome": slurm_outcome(rec),
            "args": redact(rec.get("args") or "")[:300],
        }
        if row["outcome"] == "rejected":
            row["why"] = slurm_reason(rec)
        self._place(self.submit_tail, row, older)
        # Kept oldest-first so `result` can prune from the left.
        if older:
            self.submits_in_window.appendleft(row)
        else:
            self.submits_in_window.append(row)

    # ----------------------------------------------------------------- users
    LIVE_S = 600     # a session root seen this recently is running now

    def _live_trees(self, now):
        return [t for t in self.trees.values()
                if (t.get("epoch") or 0) >= now - self.LIVE_S]

    def _running(self, now):
        """user -> {class: {agent type: sessions running now}}, and the newest
        tick each user's sessions sent."""
        live, seen = {}, {}
        for t in self._live_trees(now):
            u = t.get("user")
            if not u:
                continue
            lv = live.setdefault(u, {}).setdefault(t.get("actor3") or "agent", {})
            at = t.get("agent_type") or ""
            lv[at] = lv.get(at, 0) + 1
            seen[u] = max(seen.get(u, 0), t.get("epoch") or 0)
        return live, seen

    def _row(self, u, e, live, seen):
        """One account's row: what it ran in the window, by class and agent
        type, and the sessions it has running now.

        A person is ONE row however many classes they show up in -- the point of
        this over the collector's per-(host, class) `users` count, which adds
        the same person once per class and once per node."""
        r = {"user": u, "n": 0, "cpu_s": 0.0, "by_class": {}, "types": {},
             "live": live.get(u) or {}, "last_epoch": None}
        for (c, at), n, cpu in self.roster.decode(e):
            r["n"] += n
            r["cpu_s"] += cpu
            b = r["by_class"].setdefault(c, {"n": 0, "cpu_s": 0.0})
            b["n"] += n
            b["cpu_s"] += cpu
            if at:
                ty = r["types"].setdefault(c, {})
                ty[at] = ty.get(at, 0) + n
        r["cpu_s"] = round(r["cpu_s"], 1)
        for b in r["by_class"].values():
            b["cpu_s"] = round(b["cpu_s"], 1)
        last = max(self.roster.last.get(u) or 0, seen.get(u) or 0)
        r["last_epoch"] = last or None
        return r

    def users(self, now=None, window_s=None, q=None, limit=None, running=None):
        """The roster: distinct accounts, per-class counts, who runs what now,
        and the busiest rows.

        `q` filters by a substring of the account name and `running`
        ("class:agent type") to the accounts with such a session up -- both over
        EVERY account, since the frame carries only the busiest `user_rows`.

        Built lean, because it runs on every live frame: one pass over the
        accounts for the counts and the sort key, and full rows only for the
        ones returned."""
        now = now or time.time()
        window_s = min(int(window_s or self.retention_s), self.retention_s)
        tot = self.roster.totals(now, window_s)
        live, seen = self._running(now)
        kinds = self.roster.kinds
        by_class = dict.fromkeys(self.classes, 0)
        running_by = {}                  # class -> agent type -> users running it
        for u, m in live.items():
            for c, ty in m.items():
                d = running_by.setdefault(c, {})
                for at in ty:
                    d[at] = d.get(at, 0) + 1
        want = None
        if running:
            c, _, at = running.partition(":")
            want = (c, at)
        ql = q.lower() if q else None
        keyed = []
        for u in set(tot) | set(live):
            e = tot.get(u) or ()
            m = live.get(u) or {}
            cs = {kinds[e[j]][0] for j in range(0, len(e), 3)} | set(m)
            for c in cs:
                if c in by_class:
                    by_class[c] += 1
            if ql and ql not in u.lower():
                continue
            if want and not (m.get(want[0]) or {}).get(want[1]):
                continue
            # running agents first, then the most active, then by name: the
            # order a reader scanning for "who has agents going" wants
            n = sum(e[j + 1] for j in range(0, len(e), 3))
            keyed.append((-sum((m.get("agent") or {}).values()), -n, u))
        keyed.sort()
        limit = self.user_rows if limit is None else max(0, int(limit))
        return {
            "total": len(set(tot) | set(live)),
            "by_class": by_class,
            "with_live_agent": sum(1 for m in live.values() if m.get("agent")),
            "running_by_type": running_by,
            "rows": [self._row(u, tot.get(u), live, seen) for _, _, u in keyed[:limit]],
            "rows_total": len(keyed),
            "bin_s": self.roster.fine_s,
        }

    def user_detail(self, name, now=None, window_s=None, recent=15):
        """One account opened up: its row, each session it has running, and
        its newest commands in the event tail."""
        now = now or time.time()
        window_s = min(int(window_s or self.retention_s), self.retention_s)
        cutoff, _ = self.buckets.covers(now, window_s)
        tot = self.roster.totals(now, window_s)
        live, seen = self._running(now)
        row = self._row(name, tot.get(name), live, seen) \
            if name in tot or name in live else None
        sessions = sorted(
            ({k: t.get(k) for k in ("host", "agent_type", "actor3", "age_s", "n_procs",
                                    "threads", "rss_mb", "cpu_s", "d_state", "headless",
                                    "approval", "sandbox", "epoch")}
             for t in self._live_trees(now) if t.get("user") == name),
            key=lambda t: (t["actor3"] != "agent", -(t["n_procs"] or 0)))
        cmds = [self._served(dict(e)) for e in self.event_tail
                if e.get("user") == name and (e.get("epoch") or 0) >= cutoff][:recent]
        return {"user": name, "row": row, "sessions": sessions, "recent": cmds,
                "recent_depth": self.event_tail.maxlen}

    # ---------------------------------------------------------------- result
    def result(self, now=None, window_s=None):
        """The live tier as seen through `window_s` (default: all of retention).

        Everything time-bounded here reads the SAME cutoff -- the rate series,
        its headline totals, the submissions and the event tail -- so a reader
        never sees a chart and a plate that disagree about what "in window"
        means.  The host and residency plates are deliberately NOT cut: they are
        a snapshot of what is running now, not an aggregate over the window, and
        narrowing the window does not make a running process stop running.
        """
        now = now or time.time()
        self.buckets.evict(now)
        # Prune to retention, then read through the view's own cutoff: the deque
        # is shared by every window, so it must hold the widest one.
        while (self.submits_in_window
               and (self.submits_in_window[0]["epoch"] or 0) < now - self.retention_s):
            self.submits_in_window.popleft()
        window_s = min(int(window_s or self.retention_s), self.retention_s)
        cutoff, _end = self.buckets.covers(now, window_s)

        rate = self.buckets.series(self.classes, "n", now, window_s)
        last = self.buckets.last_complete_bin(self.classes, now)
        totals = self.buckets.totals(now=now, window_s=window_s)

        # plates: all three classes, from residency_totals
        agg = collections.defaultdict(lambda: collections.Counter())
        for h in self.hosts.values():
            for cls, d in (h["by_actor3"] or {}).items():
                for k, v in (d or {}).items():
                    if isinstance(v, (int, float)):
                        agg[cls][k] += v
        # `residency` is emitted once per live AGENT tree, so a root count only
        # exists for the agent class; the other classes are absent rather than 0,
        # because the collector never claims to enumerate their roots.
        live_trees = sum(1 for t in self.trees.values()
                         if (t.get("epoch") or 0) >= now - 600)
        roots_by_cls = {}
        for t in self.trees.values():
            if (t.get("epoch") or 0) >= now - 600:
                c = t.get("actor3") or "agent"
                roots_by_cls[c] = roots_by_cls.get(c, 0) + 1
        # Live agent sessions by their launch approval. The collector's own
        # residency_totals `autonomous` cannot be used: its is_autonomous misses
        # `--permission-mode bypassPermissions`, so it read 0 on every day of
        # rtx6000a's capture while 30k records carried approval_mode=bypassed.
        for k in [k for k, v in self.roots.items()
                  if v[0] is not None and v[0] < now - self.retention_s]:
            del self.roots[k]
        live_roots = [v[1] for v in self.roots.values()
                      if v[0] is not None and v[0] >= now - 600]
        live_ap = collections.Counter(live_roots)

        subs = [s for s in self.submits_in_window if (s["epoch"] or 0) >= cutoff]
        by_cls_subs = collections.Counter(s["actor3"] for s in subs)

        hosts = []
        for h, v in sorted(self.hosts.items()):
            by = v["by_actor3"] or {}
            ext = v.get("tcp_open_ext") or {}
            # Per-class breakdown is a plain {class: n_procs} map, and the flat
            # columns are the host-level totals across classes -- the host table
            # shows one row per host with a class split, not one row per pair.
            row = {
                "host": h, "ts": v["ts"],
                "by_class": {c: ((by.get(c) or {}).get("n_procs") or 0)
                             for c in self.classes if by.get(c)},
                "procs": sum((by.get(c) or {}).get("n_procs") or 0 for c in by),
                "threads": sum((by.get(c) or {}).get("threads") or 0 for c in by),
                "rss_gb": round(sum((by.get(c) or {}).get("rss_mb") or 0
                                    for c in by) / 1024.0, 2) or None,
                "d_state": sum((by.get(c) or {}).get("d_state") or 0 for c in by),
                "users": sum((by.get(c) or {}).get("users") or 0 for c in by),
                "tcp_open_ext": _sum_or_none(_conn_count(x) for x in ext.values()),
                "tcp_open": _sum_or_none(_conn_count(x, "open") for x in ext.values()),
                "tcp_estab": _sum_or_none(_conn_count(x, "estab") for x in ext.values()),
                "roots": v.get("trees"),
                "trees": v.get("trees"), "tracked_live": v.get("tracked_live"),
                "conn_records": v.get("conn_records"), "fork_rate": v.get("fork_rate"),
                "dropped": v.get("dropped"),
                # load and cpu_pct belong to the NODE tier; absent here rather
                # than zero, so the column reads as unreported not as idle
                "load_1": None, "cpu_pct": None,
                # by_actor3 carries procs/threads/rss/d_state/users/states; the
                # standing-socket triple arrives in a sibling field, so merge it
                # here rather than making the viewer join two maps.
                "by_class_detail": {
                    c: dict(
                        (by.get(c) or {}),
                        tcp_open_ext=_conn_count((ext or {}).get(c)),
                        tcp_open=_conn_count((ext or {}).get(c), "open"),
                        tcp_estab=_conn_count((ext or {}).get(c), "estab"),
                    ) for c in by
                },
            }
            hosts.append(row)
        # a host that reported residency ticks but no totals still gets a row
        seen = {r["host"] for r in hosts}
        for (host, _r), t in self.trees.items():
            if host and host not in seen:
                seen.add(host)
                hosts.append({"host": host, "ts": t.get("epoch"), "by_class": {},
                              "note": "residency only, no residency_totals tick"})

        retained = self.buckets.oldest_epoch()
        return {
            "window": {
                "minutes": window_s // 60, "bin_s": self.bin_s,
                "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
                "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "retention_minutes": self.retention_s // 60,
                # How far the bins ACTUALLY reach back. After a cold start this
                # is younger than the window, and the page says so rather than
                # drawing empty bins that read as a quiet fleet.
                "retained_from": (time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(retained))
                                  if retained else None),
                "retained_minutes": (int((now - retained) // 60)
                                     if retained else None),
            },
            "rate": rate,
            # Every per-class quantity is a plain {class: value} map under its own
            # bare name; a headline number is the map's sum, never a separate
            # figure that could drift from it.
            "now": {
                "exits_per_min": sum(last.values()),
                "exits_per_min_by_class": last,
                "events": {c: totals.get(c, {}).get("n", 0) for c in self.classes},
                "cpu_s": {c: round(totals.get(c, {}).get("cpu_s", 0.0), 1)
                          for c in self.classes},
                "procs": {c: agg[c].get("n_procs") for c in self.classes},
                "threads": {c: agg[c].get("threads") for c in self.classes},
                "rss_gb": {c: (round(agg[c]["rss_mb"] / 1024.0, 2)
                               if agg[c].get("rss_mb") else None)
                           for c in self.classes},
                "users": {c: agg[c].get("users") for c in self.classes},
                "d_state": {c: agg[c].get("d_state") for c in self.classes},
                "roots": {c: roots_by_cls.get(c) for c in self.classes},
                "live_agent_trees": live_trees,
                # null when no agent root was seen, so "none seen" never reads as
                # "none bypassed"; a real 0 once there are roots to count.
                "autonomous_roots": live_ap.get("bypassed", 0) if live_roots else None,
                "live_agent_sessions": len(live_roots) if live_roots else None,
                "live_sessions_by_approval": dict(live_ap),
                # replaces the census plate: real, in-scope, actor-attributed
                "submissions": dict(by_cls_subs),
                "submissions_window": len(subs),
                "submission_users": len({s["user"] for s in subs}) if subs else 0,
            },
            "hosts": hosts,
            "users": self.users(now=now, window_s=window_s),
            # A bounded tail, cut to the window. The tail is `event_tail` deep
            # REGARDLESS of window -- it is a tail, not an aggregate -- so a wide
            # window does not deepen it; narrowing one only removes rows that
            # fell outside. `events_tail_depth` says which limit bit.
            "events": [self._served(e) for e in self.event_tail
                       if (e.get("epoch") or 0) >= cutoff],
            "events_tail_depth": self.event_tail.maxlen,
            "submits": [s for s in self.submit_tail if (s.get("epoch") or 0) >= cutoff],
            "skew_dropped": self.buckets.skew_dropped,
            "no_timestamp": self.buckets.no_ts,
            "conn_unmatched_births": self.conn_unmatched_births,
            "unavailable": {
                "running_jobs": "needs the slurm_jobs census feed (not configured)",
                "gpu_held": "needs dcgm / gpu_binding (not configured)",
            },
        }
