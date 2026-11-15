"""Each window's panels as last served, saved so a new view can show them at once.

A window's panels are what `PanelView.materialize()` makes of its reducers:
every reducer's `result()`, credentials masked (`redact.payload`). They live in
memory, so a view the registry creates -- after a restart, or for a window its
LRU dropped -- has none until it is built again, from saved hours (`hours`), a
saved build (`snapshots`) or a scan. Until then every panel is blank and says
it is reading, which on a wide window or a filtered one (always a scan) is
seconds to minutes.

So each materialisation is also written down here, with the span it covers,
and a view the registry creates shows the saved panels at once, exactly as a
view being rebuilt shows its previous reduction: with that reduction's own
`built_at`, `covers_*` and drift, and the page's "rebuilding" pill, until its
own build swaps fresh panels in. What is saved is a picture of what the page
showed, never an input: no build reads it, and every panel a build installs is
computed afresh.

Saved panels are shown only when all of these hold, and the view's status says
why when they are not (`cache.panels_note`):

* **Same code and config.** The fingerprint holds what a saved build is keyed
  on (`snapshots.code_digest`: the reducers, `windows.py`, the masking, every
  config key outside `server`) and what an hour is (`hours.code_digest`: the
  merge). New code may compute a panel differently, and the page that renders
  it expects what the new code makes.
* **The same window and filter.** A filtered view never shows the unfiltered
  panels, nor the reverse.
* **Inside the window's drift budget** (`panels.max_drift_pct`), measured from
  when the reduction was built: a held view that old would already have been
  read again.

They are saved after each build and each refresh (`rebuild.interval_s`), not on
the trajectories' few-second re-ranking (`PanelView.materialize_key`). A copy is
never written over a newer one: a refresh and a build can finish together on
two threads.

`panels.cache_windows=false` turns this off with the saved builds. The files are
written as `snapshots` writes its own: a pickle header, then the
zlib-compressed panels, written atomically, 0600 in a 0700 directory, and never
loaded unless private to this uid.

This module is not in either code fingerprint (`snapshots.CODE_FILES`,
`hours.CODE_FILES`): it stores what a build served and decides nothing a
reducer's state means. Changing it changes no saved build or hour.
"""
import glob
import hashlib
import os
import pickle
import threading
import time
import zlib

from . import hours, snapshots

FORMAT = 1
SUBDIR = "payloads"
# zlib level: the panels are a few hundred KB of repetitive dicts, and the save
# runs on the build worker and the refresh tick; 1 takes most of what 9 would.
LEVEL = 1
# What a view's status shows of the panels it was given, and what is saved
# with them.
SPAN = ("covers_from", "covers_to", "built_at", "consumed_through", "live_through",
        "live_records")


def code_digest():
    """The code a window's panels are computed with: everything a saved build
    is keyed on and everything a saved hour is, so a panel computed before a
    change to either is never shown after it."""
    both = snapshots.code_digest() + hours.code_digest()
    return hashlib.sha256(("payloads %d\n" % FORMAT + both).encode()).hexdigest()


class PayloadStore:
    """One file per window and filter, named by the fingerprint.

    The fingerprint is taken per call, as `snapshots.SnapshotStore` takes its
    own: the feed's roots are resolved again while the service runs.
    """

    def __init__(self, directory, cfg, keep=8):
        self.dir = directory
        self.cfg = cfg
        self.keep = max(1, int(keep))
        self.code = code_digest()
        self._mu = threading.Lock()
        self._newest = {}               # path -> `materialized_at` written there last

    @classmethod
    def from_config(cls, cfg):
        """None when disabled, or when there is nowhere to put it."""
        if not cfg.get("panels.cache_windows", True):
            return None
        base = cfg.get("cache.dir")
        if not base:
            return None
        return cls(os.path.join(base, SUBDIR), cfg, cfg.get("panels.cache_keep", 8))

    @staticmethod
    def _stem(minutes, flt=None):
        return "p%d" % int(minutes) if not flt else "p%d-%s" % (int(minutes), flt.slug())

    def path(self, minutes, roots, flt=None):
        fp = snapshots.fingerprint(self.code, self.cfg, roots)
        return os.path.join(self.dir, "%s.%s.pkl" % (self._stem(minutes, flt), fp)), fp

    def save(self, minutes, roots, meta, payload, flt=None):
        """Write `payload` and `meta` (its span, `materialized_at`) atomically.

        Returns the header written, or None when a newer copy is already there.
        """
        dest, fp = self.path(minutes, roots, flt)
        meta = dict(meta, format=FORMAT, fingerprint=fp, minutes=int(minutes),
                    filter=flt.as_dict() if flt else None, saved_at=time.time())
        # Serialised before the lock: the panels are never changed in place
        # (`materialize` and `materialize_key` each make a new dict, and
        # `redact.payload` copies every container), so no one else's lock is
        # needed to read them.
        body = zlib.compress(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL), LEVEL)
        at = meta.get("materialized_at") or 0.0
        with self._mu:
            if self._newest.get(dest, float("-inf")) > at:
                return None
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            tmp = "%s.%d.%d.tmp" % (dest, os.getpid(), threading.get_ident())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    pickle.dump(meta, fh, protocol=pickle.HIGHEST_PROTOCOL)
                    fh.write(body)
                os.replace(tmp, dest)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._newest[dest] = at
        self.prune(fp)
        return meta

    def load(self, minutes, roots, flt=None, check=None):
        """`(meta, payload, None)`, or `(None, None, reason)`.

        `check(meta)` returns a reason to refuse, or None; the panels are only
        decompressed once the header has passed.
        """
        path, fp = self.path(minutes, roots, flt)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            other = glob.glob(os.path.join(self.dir, "%s.*.pkl" % self._stem(minutes, flt)))
            return None, None, ("the saved panels were made by different code, config "
                                "or feed roots" if other
                                else "no saved panels for this window")
        except OSError as e:            # a symlink (O_NOFOLLOW), a permission
            return None, None, "saved panels not opened (%s)" % e.strerror
        try:
            with os.fdopen(fd, "rb") as fh:
                st = os.fstat(fh.fileno())
                if st.st_uid != os.getuid() or st.st_mode & 0o022:
                    return None, None, "saved panels file is not private to this user; ignored"
                meta = pickle.load(fh)
                if not isinstance(meta, dict) or meta.get("format") != FORMAT \
                        or meta.get("fingerprint") != fp or meta.get("minutes") != int(minutes):
                    return None, None, "saved by a different code or config"
                if meta.get("filter") != (flt.as_dict() if flt else None):
                    return None, None, "saved for a different user or agent-type filter"
                why = check(meta) if check is not None else None
                if why:
                    return None, None, why
                payload = pickle.loads(zlib.decompress(fh.read()))
        except Exception as e:          # a cache that cannot be read is no cache
            self.discard(minutes, roots, flt)
            return None, None, "saved panels unreadable (%s); discarded" % e
        if not isinstance(payload, dict):
            self.discard(minutes, roots, flt)
            return None, None, "saved panels unreadable (not a payload); discarded"
        return meta, payload, None

    def discard(self, minutes, roots, flt=None):
        try:
            os.unlink(self.path(minutes, roots, flt)[0])
        except OSError:
            pass

    def prune(self, current=None):
        """Keep the newest `keep` files, any window: the current fingerprint's
        first, and among those the unfiltered before the filtered, as
        `snapshots.SnapshotStore.prune` ranks its own."""
        def rank(p):
            n = os.path.basename(p)
            return (current is not None and n.endswith(".%s.pkl" % current),
                    "-" not in n.split(".", 1)[0], os.path.getmtime(p))
        try:
            names = [os.path.join(self.dir, n) for n in os.listdir(self.dir)
                     if n.startswith("p") and n.endswith(".pkl")]
            names.sort(key=rank, reverse=True)
        except OSError:
            return
        for p in names[self.keep:]:
            try:
                os.unlink(p)
            except OSError:
                pass
