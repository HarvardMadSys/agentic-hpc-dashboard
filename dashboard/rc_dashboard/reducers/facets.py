"""Who and what is in a window: the choices the page-wide user and agent-type filters offer.

Counted in the same pass as every other panel, from the process exits the tool
mix already reads, so offering the choices costs two small dicts rather than a
second read of the window. Exits rather than every record: a long-lived agent
writes a residency and a connection tick a minute whether or not it does
anything, so counting those would rank whoever left a session open overnight
above whoever ran a thousand tool calls.

Each user carries their class mix, because one username routinely shows up as
agent, human and IDE at once -- the filter narrows by person, and the mix says
which of the three classes that person's records will land in.
"""
from . import Reducer, add_counts
from ..export import agent_type_token

# Offered in the dropdown. The rest are counted, so the page can say how many
# it is not listing, and can still be asked for by name.
TOP_USERS = 50


def _ranked(table, cap=None):
    rows = sorted(((sum(v.values()), k, v) for k, v in table.items()),
                  key=lambda r: (-r[0], r[1]))
    return rows[:cap] if cap else rows


class FacetReducer(Reducer):
    EVENTS = ("exit",)
    KEY = "facets"

    def __init__(self, cfg, classes):
        super().__init__(cfg, classes)
        self.users = {}          # user -> {class -> exits}
        self.types = {}          # agent type (or `none`) -> {class -> exits}
        self.no_user = 0         # exits with no username: counted, not dropped

    def feed(self, rec):
        cls = rec.get("_a3") or "unlabeled"
        u = rec.get("user")
        if u:
            d = self.users.setdefault(str(u), {})
            d[cls] = d.get(cls, 0) + 1
        else:
            self.no_user += 1
        t = agent_type_token(rec.get("agent_type"))
        d = self.types.setdefault(t, {})
        d[cls] = d.get(cls, 0) + 1

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed."""
        for mine, theirs in ((self.users, o.users), (self.types, o.types)):
            for k, by_cls in theirs.items():
                add_counts(mine.setdefault(k, {}), by_cls)
        self.no_user += o.no_user

    def result(self):
        users = _ranked(self.users, TOP_USERS)
        return {
            "basis": "process exits in the window",
            "users": [{"user": u, "events": n, "by_class": dict(v)} for n, u, v in users],
            "users_total": len(self.users),
            "users_shown": len(users),
            "no_user": self.no_user,
            "agent_types": [{"agent_type": t, "events": n, "by_class": dict(v)}
                            for n, t, v in _ranked(self.types)],
        }
