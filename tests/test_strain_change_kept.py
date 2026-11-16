"""修复轮19 D2 (R1, 终版 §7): the page no longer shows `strain_change`, but the backend still makes and archives it.

Real archive on the development clusters, through the history interface the history page uses.  Why it matters: the
5-minute change is worked out in the browser and kept nowhere; the 1-second difference of two planned rounds is what the
archive has of the change, and the history page is where it is read.  Taking it off the live page must not take it out
of the archive.
"""

import time

import httpx
import pytest


def test_the_history_still_has_the_strain_change_of_one_round_to_the_next(archive_rig):
    """D2: strain_change_01 is there, and each value is the strain of its round minus that of the round 1 s before.  The
    archive keeps a value when it changes (修复轮14), so not every round has a row of the change."""
    rig = archive_rig
    time.sleep(8)  # the rig's own rounds, one a second, each written to the archive
    now = time.time()
    answer = httpx.get(
        f"{rig.url}/api/replay",
        params={"center": now - 6, "seconds": 8, "points": "strain_01,strain_change_01"},
        headers={"Authorization": f"Bearer {rig.credentials['viewer']}"},
        timeout=10,
    )
    assert answer.status_code == 200, answer.text[:300]
    body = answer.json()

    def rows(cid):
        return [r for s in body["channels"][cid]["segments"] for r in s["rows"] if not r["held"] and r["valid"]]

    strain = sorted((r["time"], r["value"]) for r in rows("strain_01"))
    changes = rows("strain_change_01")
    assert len(changes) >= 3, f"D2: the change is still archived: {len(changes)} rows"
    checked = 0
    for r in changes:
        # the round before: rounds are a second apart; a row of the same round written again a moment later is not one
        before = [s for s in strain if s[0] <= r["time"] - 0.5]
        same = [s for s in strain if s[0] == r["time"]]
        if not before or not same:
            continue
        assert r["value"] == pytest.approx(same[0][1] - before[-1][1], abs=1e-9), "D2: this round minus the one before"
        assert 0.7 <= r["time"] - before[-1][0] <= 1.3, "D2: a difference of 1 s, not of 5 minutes"
        checked += 1
    assert checked >= 2, checked
