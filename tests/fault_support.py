"""Builders for fault tests: a small config with ordinary thresholds, fake-time helpers and a sink."""

from owner_support import FixedBucket, Recorder, notify, small_config

from htsg import records as r
from htsg.owner import Owner

WALL0, MONO0 = 1000.0, 100.0


def at(mono):
    return WALL0 + (mono - MONO0), mono


def fault_config(point_rules=True, derived_rules=True, ua_only=True, buffer_limit=None):
    """ti/to may exceed 490 (hysteresis 3); dt = ti - to within +-55 (hysteresis 4 each side)."""
    payload = small_config().model_dump(mode="json")
    if ua_only:
        payload["sources"][0]["enabled"] = False
    for p in payload["points"]:
        if point_rules and p["id"] in ("ti", "to"):
            p.update(high=490.0, hysteresis_high=3.0)
    for d in payload["derived"]:
        if derived_rules and d["id"] == "dt":
            d.update(low=-55.0, high=55.0, hysteresis_low=4.0, hysteresis_high=4.0)
        if derived_rules and d["id"] in ("s1", "s2"):
            d.update(low=-690.0, high=690.0, hysteresis_low=55.0, hysteresis_high=55.0)
    if buffer_limit is not None:
        payload["timing"]["fault_buffer_limit"] = buffer_limit
    return type(small_config()).model_validate(payload)


class Accepting:
    """Commit sink that can be told to refuse (an archive outage).  An accepted group is confirmed at once, as a writer that
    keeps up would (R03b: the owner builds a recovery unit only behind nothing older that is still on its way); a recovery
    unit is left to the test."""

    def __init__(self):
        self.accept = True
        self.groups = []
        self.owner = None

    def __call__(self, group):
        if not self.accept:
            return False
        self.groups.append(group)
        if self.owner is not None and group.kind != r.GROUP_RECOVERY:
            self.owner.committed(group)
        return True


def fault_owner(config=None, sink=None):
    sink = sink if sink is not None else Accepting()
    rec = Recorder()
    owner = Owner(config or fault_config(), WALL0, MONO0, commit=sink, buckets=FixedBucket(), run_id="run")
    sink.owner = owner
    owner.begin_generation("ua", 1, WALL0, MONO0)
    return owner, sink, rec


def feed(owner, mono, items, seq=1):
    wall, mono = at(mono)
    return owner.accept(notify(items, wall=wall, mono=mono, seq=seq))


def keepalive(owner, mono):
    wall, mono = at(mono)
    return owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall, mono))


def tick(owner, mono):
    owner.tick(*at(mono))


def faults_of(sink):
    return [f for g in sink.groups for f in g.faults]
