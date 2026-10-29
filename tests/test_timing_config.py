"""D03: 终版 §11 time-limit contract as configuration.

Why it matters: the safety of failure detection depends on the relations between these
numbers (keepalive vs validity vs alarm), not on any single value.
"""

import pytest
from pydantic import ValidationError

from htsg.config import Settings, Timing

BASE = {"sources": [{"id": "s", "kind": "modbus"}], "points": [], "derived": []}


def test_defaults_match_the_section_11_table():
    t = Timing()
    assert t.acquisition_period == 1
    assert (t.modbus_connect_timeout, t.modbus_request_timeout, t.modbus_retries) == (0.8, 0.8, 0)
    assert (t.modbus_reconnect_timeouts, t.modbus_alarm_seconds) == (3, 10)
    assert (t.ua_publish_interval, t.ua_sampling_interval, t.ua_queue_size) == (1, 1, 10)
    assert (t.ua_keepalive_count, t.ua_lifetime_count, t.ua_session_timeout) == (2, 60, 60)
    assert (t.ua_data_validity, t.ua_alarm_seconds, t.ua_request_timeout) == (3, 6, 5)
    assert (t.backoff_initial, t.backoff_max, t.backoff_stable_seconds) == (1, 30, 60)
    assert (t.resync_persistent_after, t.clock_jump) == (5, 2)
    assert (t.db_connect_timeout, t.db_statement_timeout, t.db_lock_timeout) == (3, 3, 1)
    assert (t.watermark_interval, t.stats_interval, t.status_interval) == (10, 60, 1)
    assert (t.ui_poll_interval, t.ui_poll_timeout, t.ui_stall_notice) == (1, 5, 3)  # 修复轮05: the page reads, nothing pushes
    assert (t.fault_confirm_seconds, t.fault_buffer_limit) == (5, 1000)


def test_settings_carry_the_defaults_without_extra_configuration():
    assert Settings.model_validate(BASE).timing == Timing()


@pytest.mark.parametrize(
    "change",
    [
        {"ua_keepalive_count": 3},  # silence 3 s + margin is no longer below the 3 s validity
        {"ua_alarm_seconds": 3},  # alarm must exceed validity
        {"ua_data_validity": 5, "ua_alarm_seconds": 5},  # alarm not above validity
        {"ua_lifetime_count": 5},  # below 3x keepalive
        {"ui_stall_notice": 2},  # not above two poll intervals
        {"backoff_initial": 40},
    ],
)
def test_inconsistent_relations_are_rejected(change):
    with pytest.raises(ValidationError):
        Timing(**change)


def test_a_reading_may_wait_longer_than_the_interruption_notice_but_the_notice_needs_two_poll_intervals():
    """修复轮05 (终版 §11): a reading still waiting when the page already says "interrupted" is intended, so the 5 s limit
    may exceed the 3 s notice; one failed reading alone must not raise the notice, so it must exceed two intervals."""
    assert Timing(ui_poll_timeout=10).ui_poll_timeout == 10
    with pytest.raises(ValidationError, match="two poll intervals"):
        Timing(ui_poll_interval=1.5)  # the 3 s notice is not above two 1.5 s intervals
    for bad in (0, -1, float("inf"), float("nan")):  # finite and positive, both of them
        with pytest.raises(ValidationError):
            Timing(ui_poll_interval=bad)
        with pytest.raises(ValidationError):
            Timing(ui_poll_timeout=bad)


@pytest.mark.parametrize(
    "path",
    ["timing.push_coalesce", "timing.push_heartbeat", "timing.client_send_timeout", "limits.ws_queue", "limits.max_clients", "allowed_origins"],
)
def test_a_field_of_the_removed_push_is_refused_by_name_and_not_ignored(path):
    """修复轮05 (终版 §18.1): a configuration still carrying a push-only field is refused and names it; nothing converts it."""
    section, _, name = path.rpartition(".")
    payload = {**BASE, **({section: {name: 1}} if section else {name: []})}
    with pytest.raises(ValidationError) as refused:
        Settings.model_validate(payload)
    assert path in str(refused.value) and "Extra inputs are not permitted" in str(refused.value)


def test_alarm_must_be_at_least_three_times_the_keepalive_silence():
    # silence = 2 s -> 3x = 6 s; an alarm of 5.5 s would fire inside the normal quiet period
    with pytest.raises(ValidationError, match="3x the keepalive silence"):
        Timing(ua_alarm_seconds=5.5)


def test_server_revision_is_rechecked_rather_than_trusting_the_request():
    t = Timing()
    assert t.check_negotiated(1, 2, 60) == []
    # the server stretched the publish interval to 2 s: normal silence 4 s exceeds validity
    assert t.check_negotiated(2, 2, 60)
    # a short revised lifetime would let the server reclaim the subscription too early
    assert t.check_negotiated(1, 2, 4)


def test_queue_revised_to_one_runs_but_is_marked_undetectable():
    assert Timing.queue_capability(1) == "OVERWRITE_UNDETECTABLE"
    assert Timing.queue_capability(10) == "OVERFLOW_DETECTABLE"


def test_production_refuses_the_simplified_login():
    with pytest.raises(ValidationError, match="dev_admin_login"):
        Settings.model_validate({**BASE, "mode": "production", "dev_admin_login": True})


@pytest.mark.parametrize("field", ["primary_path", "emergency_path"])
def test_development_databases_may_be_on_any_local_drive(field):
    """修复轮06 (user 2026-10-27): the rehearsal on the real disks puts the emergency database on C:, in development too -
    "development databases only on D:" is lifted."""
    assert getattr(Settings.model_validate({**BASE, "storage": {field: "C:/HTSG/x"}}).storage, field) == "C:/HTSG/x"


def test_production_is_not_forced_onto_d_drive():
    Settings.model_validate(
        {**BASE, "mode": "production", "storage": {"primary_path": "E:/pg", "emergency_path": "F:/pg"}}
    )
