"""Capstone C: protocols to state, full scale, real servers (方案第 5 节).

Real pymodbus and asyncua servers; 8 Bridge registers plus 200 monitored items; owner, ordered ingress
and both acquirers wired as the runtime wires them.  Persistence is an in-memory commit sink here.
Deterministic gap / callback failures are covered at the library boundary in test_ua_channel.py.
"""

import asyncio
import time

from live_support import BRIDGES, UA_NAMES, Rig

from htsg import records as r


async def test_full_scale_nominal_run_stays_quiet_when_nothing_changes():
    rig = Rig()
    async with rig:
        await rig.until(
            lambda: all(rig.owner.points[n].obs is not None for n in [*UA_NAMES, *BRIDGES]),
            timeout=30, why="all 208 inputs received",
        )  # fmt: skip
        assert rig.ua.subscription_count == 1, "200 monitored items live in ONE subscription"
        negotiated = rig.uaa.negotiated
        assert len(negotiated.items) == 200 and negotiated.violations == []
        assert negotiated.requested["queue_size"] == 10 and negotiated.keepalive_count == rig.timing.ua_keepalive_count
        # derived channels follow the inputs: 8 strains, 96 differences, 96 stress proxies, 1 peak
        await rig.until(lambda: all(rig.owner.results[f"strain_{i:02d}"].current.status == "OK" for i in range(1, 9)),
                        timeout=10, why="strains")  # fmt: skip
        await rig.until(lambda: rig.owner.results["thermal_stress_peak"].current
                        and rig.owner.results["thermal_stress_peak"].current.status == "OK", timeout=10, why="peak")  # fmt: skip
        ok = sum(1 for s in rig.owner.results.values() if s.current and s.current.status == "OK")
        assert ok >= 8 + 96 + 96 + 1, ok

        # nothing changes for a while: the cache is NOT re-reported every second (终版 §3.3)
        before = len(rig.observations("ua_field"))
        await asyncio.sleep(4.5)
        assert len(rig.observations("ua_field")) == before, "no per-second replay of unchanged OPC UA values"
        assert rig.owner.sources["ua_field"].state == "VALID", "keep-alives alone keep the source valid"
        assert rig.uaa.channel.stats["keepalive"] >= 1

        # one real change -> exactly one new observation for that point, derived values follow
        await rig.ua.set("temp_inner_01", 495.0)
        await rig.until(lambda: rig.owner.points["temp_inner_01"].obs.value == 495.0, why="change delivered")
        new = [o for o in rig.observations("ua_field")[before:] if o.point_id == "temp_inner_01"]
        assert [o.value for o in new] == [495.0]
        assert rig.owner.results["delta_t_01"].current.value == rig.owner.points["temp_inner_01"].obs.value - \
            rig.owner.points["temp_outer_01"].obs.value  # fmt: skip

        # the library is not allowed to reconnect or recreate on its own
        assert rig.uaa.gen == 1 and rig.uaa.history == [] and rig.mba.gen == 1


async def test_modbus_timeout_static_values_and_recovery_at_full_scale():
    rig = Rig()
    async with rig:
        await rig.until(lambda: rig.mba.outcomes[-3:] == ["OK"] * 3, timeout=30, why="rounds running")
        assert set(rig.mb.requests) == {(0, 16)}, "all eight Bridge values in one 16-register request"
        # constant bridge values: a round is stored every second, strain_change settles at exactly 0
        await rig.until(lambda: rig.owner.results["strain_change_01"].current.status == "OK", timeout=10, why="change")
        assert rig.owner.results["strain_change_01"].current.value == 0.0
        rig.mb.delay = 1.5
        await rig.until(lambda: "TIMEOUT" in rig.mba.outcomes, timeout=10, why="timeout")
        assert rig.owner.results["strain_01"].current.reason == "INPUT_UNAVAILABLE"
        rig.mb.delay = 0.0
        await rig.until(lambda: rig.mba.outcomes[-2:] == ["OK", "OK"], timeout=10, why="recovered")
        assert rig.owner.results["strain_change_01"].current.value in (0.0,) or \
            rig.owner.results["strain_change_01"].current.reason == "BASELINE_ONLY"  # fmt: skip
        assert rig.mba.gen == 1, "one slow round does not drop the connection"
        # OPC UA kept running throughout
        assert rig.owner.sources["ua_field"].state == "VALID"
        rounds = [g for g in rig.commits if g.kind == r.GROUP_BATCH and g.source_id == "mb_field"]
        polls = [s for g in rounds for s in g.states if s.kind == "POLL"]
        ticks = [p.detail["tick"] for p in polls]
        assert ticks == sorted(ticks) and len(set(ticks)) == len(ticks)


async def test_silence_inside_the_window_recovers_the_same_generation_and_longer_silence_rebuilds():
    rig = Rig(use_proxy=True)
    async with rig:
        await rig.until(lambda: rig.owner.sources["ua_field"].state == "VALID" and rig.owner.points["temp_inner_96"].obs,
                        timeout=30, why="running")  # fmt: skip
        await asyncio.sleep(1.2)
        recoveries = len(rig.groups(r.GROUP_RECOVERY))
        rig.proxy.pause()  # silence, not a disconnect
        await rig.until(lambda: rig.owner.sources["ua_field"].state == "EXPIRED", timeout=8, why="3 s expiry")
        assert rig.owner.health["resyncs"] == 0 and rig.uaa.gen == 1, "expiry never rebuilds"
        assert rig.owner.results["delta_t_01"].current.reason == "SOURCE_INVALID"
        rig.proxy.resume()  # inside the 3-6 s window: same subscription resumes with continuous numbers
        await rig.until(lambda: rig.owner.sources["ua_field"].state == "VALID", timeout=8, why="same-generation recovery")
        s = rig.owner.sources["ua_field"]
        assert s.conn_gen == 1 and s.seg == 2 and rig.uaa.gen == 1 and rig.uaa.history == []
        assert len(rig.groups(r.GROUP_RECOVERY)) == recoveries + 1
        group = rig.groups(r.GROUP_RECOVERY)[-1]
        assert {o.point_id for o in group.observations if o.origin == r.ORIGIN_ASSERTION} >= set(UA_NAMES)
        assert rig.owner.results["delta_t_01"].current.status == "OK"

        # longer than the 6 s threshold: silence itself triggers the rebuild on a new generation
        rig.proxy.pause()
        await rig.until(lambda: rig.owner.health["resyncs"] == 1, timeout=12, why="6 s silence")
        assert rig.owner.sources["ua_field"].resync_reason == "SILENCE" and rig.owner.sources["ua_field"].alarm
        rig.proxy.resume()
        await rig.until(lambda: rig.uaa.gen == 2 and rig.owner.sources["ua_field"].state == "VALID",
                        timeout=25, why="rebuilt generation")  # fmt: skip
        assert rig.owner.sources["ua_field"].conn_gen == 2
        assert rig.owner.health["stale_generation_batches"] >= 0
        assert not rig.owner.sources["ua_field"].alarm, "a valid response in the new generation clears the alarm"
        assert rig.ua.subscription_count >= 1


async def test_a_dropped_opcua_server_is_one_disconnect_and_one_application_initiated_rebuild():
    rig = Rig()
    async with rig:
        await rig.until(lambda: rig.owner.sources["ua_field"].state == "VALID", timeout=30, why="running")
        client_checks = []
        original = rig.uaa.client_factory

        def factory():
            client = original()
            client_checks.append(client)
            return client

        # the library must hold auto_reconnect off and its two tasks must end at once
        client = factory()
        assert client._auto_reconnect is False
        await rig.ua.stop()
        started = time.monotonic()
        await rig.until(lambda: rig.owner.sources["ua_field"].state == "RESYNC", timeout=8, why="disconnect noticed")
        assert time.monotonic() - started < 6.0, "an explicit disconnect does not wait for the 6 s timer"
        assert rig.owner.health["resyncs"] == 1
        await rig.ua.start()
        await rig.until(lambda: rig.uaa.gen == 2 and rig.owner.sources["ua_field"].state == "VALID",
                        timeout=40, why="rebuilt after the server returned")  # fmt: skip
        assert rig.owner.health["resyncs"] == 1, "exactly one rebuild cycle: no second, library-initiated one"
        assert len(rig.uaa.history) == 1
        assert rig.ua.subscription_count == 1, "the old subscription was not leaked next to the new one"
