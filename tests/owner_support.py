"""Shared builders for owner-level tests: a small config, batch builders and a sink recorder."""

from htsg import records as r
from htsg.config import Settings
from htsg.owner import Owner

MODEL = {"id": "thermal", "version": "t1", "basis": "test"}


def small_config(extra_points=(), extra_derived=(), **over):
    points = [
        {"id": "b1", "source": "mb", "unit": "mV/V", "address": 0, "data_type": "float32",
         "extreme_low": -30, "extreme_high": 30},
        {"id": "b2", "source": "mb", "unit": "mV/V", "address": 2, "data_type": "float32",
         "extreme_low": -30, "extreme_high": 30},
        {"id": "ti", "source": "ua", "unit": "degC", "node_id": "ns=2;s=ti", "extreme_low": -50, "extreme_high": 800},
        {"id": "to", "source": "ua", "unit": "degC", "node_id": "ns=2;s=to", "extreme_low": -50, "extreme_high": 800},
        *extra_points,
    ]  # fmt: skip
    derived = [
        {"id": "s1", "inputs": ["b1"], "operation": "bridge_strain", "unit": "με", "extreme_low": -5000, "extreme_high": 5000},
        {"id": "s2", "inputs": ["b2"], "operation": "bridge_strain", "unit": "με", "extreme_low": -5000, "extreme_high": 5000},
        {"id": "c1", "inputs": ["s1"], "operation": "change", "unit": "με", "extreme_low": -10000, "extreme_high": 10000},
        {"id": "c2", "inputs": ["s2"], "operation": "change", "unit": "με", "extreme_low": -10000, "extreme_high": 10000},
        {"id": "dt", "inputs": ["ti", "to"], "operation": "difference", "unit": "degC"},
        {"id": "sp", "inputs": ["ti", "to"], "operation": "thermal_stress", "unit": "MPa", "model_id": "thermal"},
        *extra_derived,
    ]  # fmt: skip
    data = {
        "sources": [
            {"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 15020},
            {"id": "ua", "kind": "opcua", "enabled": True, "endpoint": "opc.tcp://127.0.0.1:14840/"},
        ],
        "points": points,
        "derived": derived,
        "models": [MODEL],
        **over,
    }
    return Settings.model_validate(data)


class Recorder:
    def __init__(self):
        self.commits = []

    def commit(self, group):
        self.commits.append(group)

    def observations(self, point=None):
        return [o for g in self.commits for o in g.observations if point in (None, o.point_id)]

    def results(self, point=None):
        return [x for g in self.commits for x in g.results if point in (None, x.point_id)]

    def states(self, kind=None):
        return [x for g in self.commits for x in g.states if kind in (None, x.kind)]


class FixedBucket:
    """For a test that builds the state owner by hand and stores its groups: every group goes into bucket 1, which
    `archive_db_factory` prepares (修复轮09).  The runtime gives the owner the real BucketClock."""

    def label(self):
        return 1

    def tick(self, wall, mono):
        pass


def make_owner(config=None, wall=1000.0, mono=100.0, generations=("mb", "ua")):
    config = config or small_config()
    recorder = Recorder()
    owner = Owner(config, wall, mono, commit=recorder.commit, buckets=FixedBucket(), run_id="run")
    for source in generations:
        owner.begin_generation(source, 1, wall, mono)
    return owner, recorder


def poll(tick, values, *, source="mb", gen=1, outcome="OK", quality=r.GOOD, wall=None, mono=None, status=None):
    """One Modbus round; `values` maps point id -> value (a missing id was not answered)."""
    wall = 1000.0 + tick if wall is None else wall
    mono = 100.0 + tick if mono is None else mono
    items = tuple(
        r.Item(pid, v, quality if v is not None else r.BAD, status, response_id="resp0") for pid, v in values.items()
    )
    return r.InputBatch(source, gen, r.KIND_MODBUS_POLL, wall, mono, items, tick=tick, poll_id=f"poll{tick}", outcome=outcome)


def notify(items, *, source="ua", gen=1, wall=1010.0, mono=110.0, seq=1):
    """One OPC UA data notification; items are (point, value[, quality[, status]])."""
    built = tuple(
        r.Item(i[0], i[1], i[2] if len(i) > 2 else r.GOOD, i[3] if len(i) > 3 else 0) for i in items
    )
    return r.InputBatch(source, gen, r.KIND_UA_NOTIFY, wall, mono, built, publish_seq=seq)
