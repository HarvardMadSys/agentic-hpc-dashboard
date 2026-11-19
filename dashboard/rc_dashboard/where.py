"""A record filter that can be applied before a record is built (`where`).

A `where` is one dict of field tests, all of which must pass, or a list of such
dicts, any of which may. A test sees the record's top-level `rec.get(field)`, so a
missing field is None:

* a set (or frozenset) passes when `str(value)` is in it;
* a `Prefix` passes when the value is a str that starts with it;
* an `AfterColon` set passes when the value is a str whose part after its first
  colon (all of it, when it has none) is in it: a session key's pid;
* a callable passes when `fn(value)` is truthy.

Tests that are not callables are what a replica can apply to a whole column at
once; a callable it has to call on each value.

The readers build these from the same inputs as their own checks, never
stricter, and still check each record they are handed: a `where` only lets a
reader skip what cannot match. A sealed day-file's replica (`replica.py`) tests
them on the few columns they name before it builds any record.
"""


class Prefix(str):
    """A `where` test: the field is a str starting with this."""
    __slots__ = ()

    def __repr__(self):
        return "Prefix(%s)" % str.__repr__(self)


class AfterColon(frozenset):
    """A `where` test: the field is a str whose part after its first colon -- the
    whole str when it has none -- is one of these. A session key names its pid so
    (`normalize.session_key_of`: `apid:<pid>`, `claude:<pid>`, or the bare pid)."""
    __slots__ = ()

    def __repr__(self):
        return "AfterColon(%s)" % sorted(self)


def test(t, v):
    """Whether value `v` passes the single test `t`."""
    if isinstance(t, AfterColon):
        return isinstance(v, str) and v.split(":", 1)[-1] in t
    if isinstance(t, (set, frozenset)):
        return str(v) in t
    if isinstance(t, Prefix):
        return isinstance(v, str) and v.startswith(t)
    if callable(t):
        return bool(t(v))
    raise TypeError("a where test is a set, a Prefix or a callable, not %s"
                    % type(t).__name__)


def branches(where):
    """`where` as a list of dicts, checked; None for no filter."""
    if where is None:
        return None
    out = [where] if isinstance(where, dict) else list(where)
    for br in out:
        if not isinstance(br, dict):
            raise TypeError("where is a dict or a list of dicts")
        for t in br.values():
            if not (isinstance(t, (set, frozenset, Prefix)) or callable(t)):
                raise TypeError("a where test is a set, a Prefix or a callable, not %s"
                                % type(t).__name__)
    return out


def fields(where):
    """Every field a `where` tests."""
    return sorted({f for br in (branches(where) or ()) for f in br})


def match(where, rec):
    """The reference semantics: one dict (every test passes) or a list of dicts
    (any dict passes), each test on `rec.get(field)`."""
    if where is None:
        return True
    for br in ([where] if isinstance(where, dict) else where):
        for f, t in br.items():
            if not test(t, rec.get(f)):
                break
        else:
            return True
    return False
