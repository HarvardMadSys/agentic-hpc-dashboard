"""One JSON parser for every line read off the collector's files.

orjson parses a collector line in about a third of the time the standard
library takes, and the read is a third of a window build. It is optional: a
deployment without it falls back to `json` and only runs slower. It also
refuses a few things `json` accepts -- an integer wider than 64 bits, a lone
surrogate -- so a line it rejects is retried with `json` before it counts as
malformed: switching parsers must never drop a record the old one kept.
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
