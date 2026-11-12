"""Greedy single-antenna scheduling: hand-checked example and invariants on real passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from passplan.passes import GROUND_STATIONS, Pass, SkyPoint, predict_passes
from passplan.schedule import ContactRequest, conflicts, schedule_contacts
from tests.conftest import MIN_ELEVATION_DEGREES, WINDOW_END_UTC, WINDOW_START_UTC, frozen_tle

SLEW = timedelta(minutes=2)
SCHEDULE_TLE_FILES = ["iss.tle", "sentinel2b.tle", "landsat9.tle", "sentinel2a.tle", "noaa20.tle"]


def _request(name: str, priority: int, start: str, end: str, max_elevation: float) -> ContactRequest:
    def at(clock: str) -> datetime:
        hour, minute = map(int, clock.split(":"))
        return datetime(2026, 11, 1, hour, minute, tzinfo=UTC)

    middle = at(start) + (at(end) - at(start)) / 2
    satellite_pass = Pass(
        aos=SkyPoint(at(start), 200.0, 10.0),
        culmination=SkyPoint(middle, 180.0, max_elevation),
        los=SkyPoint(at(end), 160.0, 10.0),
    )
    return ContactRequest(name, priority, satellite_pass)


HAND_EXAMPLE = [
    _request("A", 1, "10:00", "10:08", 30.0),
    _request("B", 2, "10:09", "10:15", 60.0),
    _request("C", 2, "10:11", "10:20", 40.0),
    _request("D", 3, "10:21", "10:30", 80.0),
    _request("E", 3, "10:23", "10:30", 20.0),
]


def _outcome(decisions):
    return {decision.request.satellite_name: decision.accepted for decision in decisions}


def test_hand_example_with_two_minute_slew():
    """Five synthetic passes, slew 2 min.

    Reference value, worked by hand with the rule (priority, then elevation):
    A (P1) accepted. P2 by elevation: B (60 deg) starts 1 min after A's LOS
    < 2 min -> rejected; C starts 3 min after A -> accepted. P3: D (80 deg)
    starts 1 min after C's LOS -> rejected; E starts 3 min after C -> accepted.
    """
    decisions = schedule_contacts(HAND_EXAMPLE, SLEW)
    assert _outcome(decisions) == {"A": True, "B": False, "C": True, "D": False, "E": True}
    reasons = {decision.request.satellite_name: decision.reason for decision in decisions}
    assert "A (P1)" in reasons["B"] and "only 1.0 min apart" in reasons["B"]
    assert "C (P2)" in reasons["D"]


def test_hand_example_without_slew():
    """Same passes with zero slew time give a different, hand-checked result.

    Reference value, by hand: A accepted; B (10:09) no longer conflicts with
    A (LOS 10:08) -> accepted; C overlaps B -> rejected; D clears B -> accepted;
    E overlaps D -> rejected.
    """
    decisions = schedule_contacts(HAND_EXAMPLE, timedelta(0))
    assert _outcome(decisions) == {"A": True, "B": True, "C": False, "D": True, "E": False}


def test_conflict_rule_boundaries():
    """A gap exactly equal to the slew time is allowed; one second less is not.

    Reference value: the definition a2 >= b1 + slew for compatible contacts.
    """
    first = _request("X", 1, "10:00", "10:08", 30.0)
    exactly = _request("Y", 1, "10:10", "10:15", 30.0)
    assert not conflicts(first, exactly, SLEW)
    assert conflicts(first, exactly, SLEW + timedelta(seconds=1))


@pytest.fixture(scope="module")
def real_requests(timescale):
    """All passes of five LEO satellites over Helsinki in the fixed 2-day window, priority = list order."""
    station = GROUND_STATIONS["helsinki"]
    requests = []
    for priority, file_name in enumerate(SCHEDULE_TLE_FILES, start=1):
        satellite = frozen_tle(file_name).to_satellite(timescale)
        for satellite_pass in predict_passes(
            satellite, station, WINDOW_START_UTC, WINDOW_END_UTC, MIN_ELEVATION_DEGREES, timescale
        ):
            requests.append(ContactRequest(satellite.name, priority, satellite_pass))
    return requests


def test_real_schedule_has_no_overlaps_and_respects_slew(real_requests):
    """Accepted contacts, sorted by time, are separated by at least the slew time.

    Reference value: the slew parameter (2 min); checked pairwise on the output.
    """
    decisions = schedule_contacts(real_requests, SLEW)
    accepted = sorted((d.request for d in decisions if d.accepted), key=lambda request: request.start)
    assert len(accepted) > 10
    for earlier, later in zip(accepted, accepted[1:], strict=False):
        assert later.start - earlier.end >= SLEW


def test_real_schedule_rejections_are_justified(real_requests):
    """Every rejected contact conflicts with an accepted one of equal or higher rank, and there are some.

    Reference value: the selection rule. A rejection without such a blocker
    would mean a contact was dropped for no reason (the result is maximal).
    """
    decisions = schedule_contacts(real_requests, SLEW)
    accepted = [d.request for d in decisions if d.accepted]
    rejected = [d.request for d in decisions if not d.accepted]
    assert rejected, "the five-satellite example should contain conflicts"
    assert len(accepted) + len(rejected) == len(real_requests)
    for request in rejected:
        assert any(conflicts(request, other, SLEW) and other.priority <= request.priority for other in accepted)


def test_top_priority_satellite_is_never_rejected_without_own_conflict(real_requests):
    """Priority-1 passes (ISS) are only rejected by other ISS passes, which cannot overlap.

    Reference value: consecutive ISS passes are ~93 min apart (orbital period),
    far more than the 2 min slew, so all ISS passes must be scheduled.
    """
    decisions = schedule_contacts(real_requests, SLEW)
    assert all(d.accepted for d in decisions if d.request.priority == 1)
