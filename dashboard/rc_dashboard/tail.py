"""Follow the collector's append-only JSONL without re-reading it.

`IngestSource` is the seam: today the only implementation follows files, but a
`PushSource` fed by an HTTP/UDP sink in the collector implements the same
`poll()` and the reducers never learn which one fed them.

Two correctness rules, both from how the collector writes:

* `DailyWriter.write()` (ebpf_trace.py:1010) does `write(json+'\n')` then
  `flush()`, which is NOT atomic against a concurrent reader.  So we read to EOF,
  find the last newline, yield only complete lines, and advance the stored offset
  only that far.  Trailing bytes are re-read next cycle: idempotent, never a
  truncated JSON line, never a lost record.

* Offsets are keyed on `(st_dev, st_ino)`, not path.  A new date directory
  therefore cold-starts instead of inheriting yesterday's offset, and a
  recreated file is detected as a new inode rather than silently seeking past
  its EOF.  Hosts roll over independently, so both date dirs stay in the window.
"""
import glob
import json
import os
import time

STATE_VERSION = 1


class IngestSource:
    """Anything that can hand the aggregator a batch of raw records."""

    def poll(self):
        raise NotImplementedError

    def close(self):
        pass


class FileTailSource(IngestSource):
    def __init__(self, roots, days=2, state_path=None, cold_start_mb=256):
        self.roots = list(roots)
        self.days = days
        self.state_path = state_path
        self.cold_start = cold_start_mb * 1024 * 1024
        self.state = {}
        self.stats = {"files": 0, "bytes_read": 0, "partial_holds": 0,
                      "parse_errors": 0, "cold_starts": 0, "resets": 0}
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as fh:
                blob = json.load(fh)
            if blob.get("version") == STATE_VERSION:
                self.state = blob.get("files", {})
        except Exception:
            self.state = {}

    def save_state(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": STATE_VERSION,
                       "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "files": self.state}, fh)
        os.replace(tmp, self.state_path)

    def prune_state(self):
        """Keep `days + 2` date dirs of entries so the file stays small."""
        keep = set(self.files())
        self.state = {k: v for k, v in self.state.items() if k in keep}

    # ------------------------------------------------------------------ files
    def files(self):
        out = []
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            dates = sorted(d for d in os.listdir(root)
                           if d.startswith("20") and os.path.isdir(os.path.join(root, d)))
            for d in dates[-self.days:]:
                out += sorted(glob.glob(os.path.join(root, d, "*.jsonl")))
            out += sorted(glob.glob(os.path.join(root, "*.jsonl")))[-self.days:]
        return sorted(set(out))

    # ------------------------------------------------------------------- read
    def _read_one(self, path, cold_start_allowed=True):
        try:
            fh = open(path, "rb")
        except OSError:
            return
        with fh:
            try:
                st = os.fstat(fh.fileno())
            except OSError:
                return
            rec = self.state.get(path)
            offset = 0
            if rec and rec.get("dev") == st.st_dev and rec.get("inode") == st.st_ino:
                offset = rec.get("offset", 0)
                if st.st_size < offset:
                    offset = 0                     # truncated or rewritten
                    self.stats["resets"] += 1
            elif rec:
                self.stats["resets"] += 1          # replaced: different inode
            elif cold_start_allowed and st.st_size > self.cold_start:
                offset = st.st_size - self.cold_start
                self.stats["cold_starts"] += 1
            if offset >= st.st_size:
                self._remember(path, st, offset, rec)
                return
            fh.seek(offset)
            data = fh.read(st.st_size - offset)

        if offset > 0 and self.state.get(path, {}).get("offset") != offset:
            # we jumped into the middle of a line on a cold start
            nl = data.find(b"\n")
            if nl == -1:
                return
            offset += nl + 1
            data = data[nl + 1:]

        cut = data.rfind(b"\n")
        if cut == -1:
            self.stats["partial_holds"] += 1       # no complete line yet; re-read later
            return
        complete, consumed = data[:cut + 1], cut + 1
        self.stats["bytes_read"] += consumed

        for line in complete.split(b"\n"):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                self.stats["parse_errors"] += 1

        st2 = os.stat(path)
        self._remember(path, st2, offset + consumed, self.state.get(path))

    def _remember(self, path, st, offset, prev):
        self.state[path] = {"dev": st.st_dev, "inode": st.st_ino,
                            "size": st.st_size, "offset": offset,
                            "mtime": st.st_mtime}

    def poll(self, cold_start_allowed=True):
        """Yield every complete record appended since the last call."""
        paths = self.files()
        self.stats["files"] = len(paths)
        for p in paths:
            yield from self._read_one(p, cold_start_allowed)

    def seek_all_to_end(self):
        """Mark every current file as fully consumed, without reading it."""
        for p in self.files():
            try:
                st = os.stat(p)
            except OSError:
                continue
            self._remember(p, st, st.st_size, None)
