"""Per-user roster for the live tier: who ran what, cheap at any number of users.

The first version kept one RollingBuckets slot per (user, class, agent type)
per 10-minute bin and re-added a whole window on every live frame. At 2,000
users that was 640 MB and up to 600 ms a frame, under the aggregator's lock.
Three changes make it hold:

- Two tiers of bins: 10-minute bins for the last day, hourly beyond it, out to
  retention. A roster needs no 10-minute resolution a week back, and the old
  bins become a sixth as many.
- A compact slot: a bin maps a user to ONE flat list,
  [kind, n, cpu, rd, wr, net, kind, n, ...], where `kind` is a small integer
  standing for a (class, agent type) pair. A dict per pair per bin was several
  times the memory for the same numbers. Disk read, disk written, and network
  MB ride along so the Active users table can show each account's I/O over the
  window without a second roster.
- A cached window: between two frames only the open bin changes, so the sum
  over a window's closed bins is kept and rebuilt only when a bin closes, the
  window's edge crosses a bin, or -- at most every REBUILD_S -- a late record
  lands in a closed bin (the backfill writes old records for minutes on
  start). A frame then costs the open bin plus one pass over the users.

A window's edge lands on a bin boundary: 10 minutes inside the last day, an
hour beyond it. A roster can afford that; the rate chart, which cannot, keeps
its own 1-minute bins.
"""
import time

FINE_S = 600
COARSE_S = 3600
FINE_SPAN_S = 86400
REBUILD_S = 15
CACHE_MAX = 8
# One entry: kind, processes, CPU s, disk read MB, disk written MB, network MB.
W = 6


def _bump(slot, user, kind, vals):
    """Count one process of `kind` for `user`; `vals` are its CPU and I/O. A
    record with no CPU or no I/O block still ran: it counts, and adds 0."""
    e = slot.get(user)
    if e is None:
        slot[user] = [kind, 1, *vals]
        return
    for j in range(0, len(e), W):
        if e[j] == kind:
            e[j + 1] += 1
            e[j + 2] += vals[0]
            e[j + 3] += vals[1]
            e[j + 4] += vals[2]
            e[j + 5] += vals[3]
            return
    e.extend((kind, 1, *vals))


def _add_into(dst, e):
    """Add flat entry `e` into flat entry `dst`, in place."""
    for j in range(0, len(e), W):
        k = e[j]
        for i in range(0, len(dst), W):
            if dst[i] == k:
                for f in range(1, W):
                    dst[i + f] += e[j + f]
                break
        else:
            dst.extend(e[j:j + W])


def _merge(dst, src):
    """Add every user's entry in slot `src` into slot `dst`; `src` is not shared."""
    for u, e in src.items():
        d = dst.get(u)
        if d is None:
            dst[u] = list(e)
        else:
            _add_into(d, e)


class UserRoster:
    def __init__(self, retention_s, fine_s=FINE_S, coarse_s=COARSE_S,
                 fine_span_s=FINE_SPAN_S, skew_s=120):
        if coarse_s % fine_s:
            raise ValueError("coarse bins must be whole multiples of fine bins")
        self.retention_s = retention_s
        self.fine_s = fine_s
        self.coarse_s = coarse_s
        self.fine_span_s = min(fine_span_s, retention_s)
        self.skew_s = skew_s
        self.fine = {}        # fine bin index -> {user: [kind, n, cpu, ...]}
        self.coarse = {}      # coarse bin index -> the same
        self.kinds = []       # kind -> (class, agent type)
        self._kind = {}       # (class, agent type) -> kind
        self.last = {}        # user -> newest epoch seen
        self.version = 0      # bumped whenever a CLOSED bin changes
        self._cache = {}      # window_s -> (sig, version, built_at, {user: entry})
        self._rolled = None   # fine index the last roll ran in

    # ---------------------------------------------------------------- ingest
    def kind_of(self, cls, atype):
        key = (cls, atype or "")
        k = self._kind.get(key)
        if k is None:
            k = self._kind[key] = len(self.kinds)
            self.kinds.append(key)
        return k

    def add(self, epoch, user, cls, atype, cpu_s, now=None, rd_mb=None, wr_mb=None, net_mb=None):
        if epoch is None or not user:
            return False
        now = time.time() if now is None else now
        if epoch > now + self.skew_s:
            return False
        epoch = min(epoch, now)          # small skew: the newest bin
        if epoch < now - self.retention_s:
            return False
        self.roll(now)
        k = self.kind_of(cls, atype)
        if epoch >= now - self.fine_span_s:
            idx, store = int(epoch // self.fine_s), self.fine
            if idx != int(now // self.fine_s):
                self.version += 1
        else:
            idx, store = int(epoch // self.coarse_s), self.coarse
            self.version += 1
        slot = store.get(idx)
        if slot is None:
            slot = store[idx] = {}
        _bump(slot, user, k, (cpu_s or 0.0, rd_mb or 0.0, wr_mb or 0.0, net_mb or 0.0))
        if epoch > self.last.get(user, 0):
            self.last[user] = epoch
        return True

    def roll(self, now):
        """Once per fine bin: fold day-old fine bins into the hourly tier, drop
        what fell out of retention, and forget users last seen before it."""
        cur = int(now // self.fine_s)
        if self._rolled == cur:
            return
        self._rolled = cur
        edge = now - self.fine_span_s
        for idx in [i for i in self.fine if (i + 1) * self.fine_s <= edge]:
            slot = self.fine.pop(idx)
            dst = self.coarse.setdefault(idx * self.fine_s // self.coarse_s, {})
            _merge(dst, slot)
            self.version += 1
        old = now - self.retention_s
        for store, size in ((self.fine, self.fine_s), (self.coarse, self.coarse_s)):
            for idx in [i for i in store if (i + 1) * size <= old]:
                del store[idx]
                self.version += 1
        for u in [u for u, e in self.last.items() if e < old]:
            del self.last[u]

    # ------------------------------------------------------------------ read
    def _closed(self, now, window_s):
        """{user: flat entry} over the window's CLOSED bins, cached per window."""
        cutoff = now - window_s
        open_idx = int(now // self.fine_s)
        sig = (int(cutoff // self.fine_s), int(cutoff // self.coarse_s), open_idx)
        hit = self._cache.pop(window_s, None)
        if hit and hit[0] == sig and (hit[1] == self.version or now - hit[2] < REBUILD_S):
            self._cache[window_s] = hit          # most recently used last
            return hit[3]
        agg = {}
        for store, size in ((self.coarse, self.coarse_s), (self.fine, self.fine_s)):
            for idx, slot in store.items():
                # a bin counts when it overlaps the window; the open bin is
                # added fresh on every read instead
                if (idx + 1) * size <= cutoff or (store is self.fine and idx == open_idx):
                    continue
                _merge(agg, slot)
        self._cache[window_s] = (sig, self.version, now, agg)
        while len(self._cache) > CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        return agg

    def totals(self, now=None, window_s=None):
        """{user: [kind, n, cpu, ...]} over the window. Do not mutate the lists."""
        now = time.time() if now is None else now
        window_s = min(int(window_s or self.retention_s), self.retention_s)
        self.roll(now)
        agg = self._closed(now, window_s)
        open_slot = self.fine.get(int(now // self.fine_s))
        if not open_slot:
            return agg
        out = dict(agg)                          # shallow: shared where unchanged
        for u, e in open_slot.items():
            base = out.get(u)
            if base is None:
                out[u] = list(e)
            else:
                m = list(base)
                _add_into(m, e)
                out[u] = m
        return out

    def decode(self, e):
        """A flat entry as ((class, agent type), n, cpu) triples."""
        return [(self.kinds[e[j]], e[j + 1], e[j + 2]) for j in range(0, len(e or ()), W)]

    def decode_full(self, e):
        """A flat entry as ((class, agent type), n, cpu, rd MB, wr MB, net MB)."""
        return [(self.kinds[e[j]], *e[j + 1:j + W]) for j in range(0, len(e or ()), W)]
