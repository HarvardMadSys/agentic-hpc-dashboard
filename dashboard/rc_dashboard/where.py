"""A record filter that can be applied before a record is built (`where`).

A `where` is one dict of field tests, all of which must pass, or a list of such
dicts, any of which may. A test sees the record's top-level `rec.get(field)`, so a
missing field is None:

* a set (or frozenset) passes when `str(value)` is in it;
* a `Prefix` passes when the value is a str that starts with it;
* a callable passes when `fn(value)` is truthy.

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


def test(t, v):
    """Whether value `v` passes the single test `t`."""
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
