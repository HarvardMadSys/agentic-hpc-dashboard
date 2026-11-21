"""One JSON parser for every line read off the collector's files.

orjson parses a collector line in about a third of the time the standard
library takes, and the read is a third of a window build. It is optional: a
deployment without it falls back to `json` and only runs slower. It also
refuses something `json` accepts -- a lone surrogate -- so a line it rejects is
retried with `json` before it counts as malformed: switching parsers must never
drop a record the old one kept. An integer wider than 64 bits it does not
refuse: orjson 3.13 reads it as a float, where `json` keeps it exact. The
collector writes none (docs/feed-parquet, Caveats).
"""
import json

try:
    import orjson
except ImportError:          # pragma: no cover - exercised only without orjson
    orjson = None


if orjson is not None:
    _fast = orjson.loads

    def loads(line):
        try:
            return _fast(line)
        except ValueError:
            return json.loads(line)
else:
    loads = json.loads

DecodeError = ValueError     # what both parsers raise on a malformed line


def record(line):
    """The record a collector line holds: `loads`, and a ValueError, as for a
    line that does not parse, when the line is JSON but not an object (`0`,
    `null`, `[]`, a string). Every reader of the day-files counts such a line
    as one that does not parse; handed on, the first `.get` on it raised, and
    one such line stopped the tail for every host."""
    rec = loads(line)
    if type(rec) is not dict:
        raise ValueError("a JSON %s, not an object" % type(rec).__name__)
    return rec
