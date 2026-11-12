"""Optical visibility (sunlight + dark sky) and CelesTrak name search."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import passplan.tle as tle_module
from passplan.cli import main
from passplan.passes import predict_passes
from passplan.tle import NoGpDataError, TleError, search_by_name
from passplan.visibility import (
    DAYLIGHT,
    IN_SHADOW,
    SUNLIT_ONLY_IN_BRIGHT_SKY,
    VISIBLE,
    classify_visibility,
    ephemeris_is_downloaded,
    load_ephemeris,
    pass_visibility,
)
from tests.conftest import HEAVENS_ABOVE_SITE, WINDOW_END_UTC, WINDOW_START_UTC, frozen_tle

EPHEMERIS_DIRECTORY = Path(__file__).resolve().parents[1] / ".tle_cache"
needs_ephemeris = pytest.mark.skipif(
    not ephemeris_is_downloaded(EPHEMERIS_DIRECTORY),
    reason="DE421 not downloaded; run once with --visibility to fetch it into .tle_cache/",
)

ISS_LINES = frozen_tle("iss.tle")
SENTINEL_LINES = frozen_tle("sentinel2b.tle")


# --- classification logic (no ephemeris needed) ---------------------------------


def _times(count: int) -> list[datetime]:
    return [datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=index) for index in range(count)]


@pytest.mark.parametrize(
    ("sunlit", "sun_altitude", "expected"),
    [
        ([False, True, True, False], [-20, -20, -20, -20], VISIBLE),
        ([True, True, True, True], [10, 5, 1, 0], DAYLIGHT),
        ([True, True, True, True], [-3, -4, -5, -5.9], DAYLIGHT),
        ([False, False, False, False], [-20, -20, -20, -20], IN_SHADOW),
        ([True, True, False, False], [-2, -4, -7, -8], SUNLIT_ONLY_IN_BRIGHT_SKY),
    ],
)
def test_classification_rules(sunlit, sun_altitude, expected):
    """Each category follows from its definition.

    Reference values: hand-made inputs; visible = sunlit and Sun below -6 deg
    at the same instant. The third case checks that -5.9 deg is still too bright.
    """
    result = classify_visibility(_times(4), np.array(sunlit), np.array(sun_altitude, dtype=float))
    assert result.category == expected


def test_visible_interval_is_first_and_last_visible_sample():
    """The reported interval spans the first and last visible samples.

    Reference value: samples 1 and 2 are the only sunlit ones in a dark sky.
    """
    times = _times(4)
    result = classify_visibility(times, np.array([False, True, True, False]), np.full(4, -20.0))
    assert (result.visible_from_utc, result.visible_until_utc) == (times[1], times[2])


# --- with the real ephemeris -----------------------------------------------------


@needs_ephemeris
@pytest.mark.parametrize("tle_record", [ISS_LINES, SENTINEL_LINES], ids=["iss", "sentinel2b"])
def test_sunlit_matches_independent_cylinder_shadow(tle_record, timescale):
    """Skyfield's is_sunlit agrees with a hand-written cylindrical shadow test.

    Reference value: a satellite at geocentric position r is in shadow when it
    is on the night side (r . s < 0, s = unit vector to the Sun) and closer
    than one Earth radius to the Earth-Sun axis (|r x s| < R). The Sun is so
    far away that its rays are almost parallel (R / 1 au ~ 4e-5 rad), so this
    cylinder and Skyfield's line-to-Sun test differ only within seconds of a
    shadow boundary; samples within 30 s of a transition are excluded.
    """
    ephemeris = load_ephemeris(EPHEMERIS_DIRECTORY)
    satellite = tle_record.to_satellite(timescale)
    seconds = np.arange(0.0, (WINDOW_END_UTC - WINDOW_START_UTC).total_seconds(), 10.0)
    times = timescale.tt_jd(timescale.from_datetime(WINDOW_START_UTC).tt + seconds / 86400.0)

    skyfield_sunlit = np.asarray(satellite.at(times).is_sunlit(ephemeris), dtype=bool)
    position_km = satellite.at(times).xyz.km.T
    sun_km = (ephemeris["sun"] - ephemeris["earth"]).at(times).xyz.km.T
    sun_unit = sun_km / np.linalg.norm(sun_km, axis=1, keepdims=True)
    along_sun = np.sum(position_km * sun_unit, axis=1)
    distance_from_axis = np.linalg.norm(np.cross(position_km, sun_unit), axis=1)
    reference_sunlit = ~((along_sun < 0.0) & (distance_from_axis < 6378.137))

    transitions = np.flatnonzero(np.diff(reference_sunlit.astype(np.int8)))
    near_transition = np.zeros(seconds.size, dtype=bool)
    for index in transitions:
        near_transition[max(index - 3, 0) : index + 4] = True
    assert transitions.size > 20  # both orbits cross the shadow many times in two days
    assert np.array_equal(skyfield_sunlit[~near_transition], reference_sunlit[~near_transition])


@needs_ephemeris
def test_matches_heavens_above_iss_pass(timescale):
    """Rise, shadow exit and set of one ISS pass agree with Heavens-Above.

    Reference values: Heavens-Above pass details for the ISS at 60.1527 N,
    24.9254 E, 10 m (retrieved 2026-10-31, element set with the same epoch as
    tests/data/iss.tle): "Rises 02:16:42", "Exits shadow 02:24:45" and
    "Sets 02:25:55" UTC on 2026-11-01. Heavens-Above shows whole seconds and
    our samples are 1 s apart, so the tolerance is 2 s.
    """
    ephemeris = load_ephemeris(EPHEMERIS_DIRECTORY)
    satellite = ISS_LINES.to_satellite(timescale)
    station = HEAVENS_ABOVE_SITE
    start = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
    (iss_pass,) = predict_passes(satellite, station, start, start + timedelta(minutes=40), 0.0, timescale)
    visibility = pass_visibility(satellite, station, iss_pass, ephemeris, timescale)

    def reference(clock: str) -> datetime:
        return datetime.fromisoformat(f"2026-11-01T{clock}+00:00")

    assert abs((iss_pass.aos.time_utc - reference("02:16:42")).total_seconds()) <= 2.0
    assert abs((iss_pass.los.time_utc - reference("02:25:55")).total_seconds()) <= 2.0
    assert visibility.category == VISIBLE
    assert abs((visibility.visible_from_utc - reference("02:24:45")).total_seconds()) <= 2.0


@needs_ephemeris
def test_heavens_above_pass_types_for_two_days(timescale):
    """Of the seven ISS passes above 10 deg, only the 02:19 pass on 1 Nov is visible.

    Reference values: Heavens-Above "all passes" list for the same site and
    days (README table): pass type "visible" for 1 Nov 02:19, "night (unlit)"
    for the other six. Heavens-Above lists passes by their 10 deg crossing but
    judges visibility over the whole pass, so each pass is evaluated here
    from 0 deg to 0 deg.
    """
    ephemeris = load_ephemeris(EPHEMERIS_DIRECTORY)
    satellite = ISS_LINES.to_satellite(timescale)
    station = HEAVENS_ABOVE_SITE
    high_passes = predict_passes(satellite, station, WINDOW_START_UTC, WINDOW_END_UTC, 10.0, timescale)
    whole_passes = predict_passes(satellite, station, WINDOW_START_UTC, WINDOW_END_UTC, 0.0, timescale)
    categories = []
    for high in high_passes:
        (whole,) = [p for p in whole_passes if p.aos.time_utc <= high.culmination.time_utc <= p.los.time_utc]
        categories.append(pass_visibility(satellite, station, whole, ephemeris, timescale).category)
    assert categories == [IN_SHADOW, VISIBLE, IN_SHADOW, IN_SHADOW, IN_SHADOW, IN_SHADOW, IN_SHADOW]


# --- CelesTrak name search (download replaced by a stub) -------------------------


def _omm(norad_id: int, name: str, epoch: str) -> dict:
    """A minimal CelesTrak-style OMM object (orbit values are placeholders)."""
    return {
        "OBJECT_NAME": name,
        "OBJECT_ID": "1990-005C",
        "EPOCH": epoch,
        "MEAN_MOTION": 14.3,
        "ECCENTRICITY": 0.001,
        "INCLINATION": 98.8,
        "RA_OF_ASC_NODE": 100.0,
        "ARG_OF_PERICENTER": 10.0,
        "MEAN_ANOMALY": 350.0,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": norad_id,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 1000,
        "BSTAR": 5e-5,
        "MEAN_MOTION_DOT": 1e-6,
        "MEAN_MOTION_DDOT": 0,
    }


def test_name_search_parses_caches_and_fills_norad_cache(tmp_path, monkeypatch):
    """A search downloads once, is cached for 2 h, and seeds the per-NORAD cache.

    Reference values: the stub response (catalog numbers 20443 and 101122)
    and CelesTrak's 2-hour update interval.
    """
    response = json.dumps(
        [
            _omm(20443, "ARIANE 40 R/B", "2026-10-31T09:04:19.000000"),
            _omm(101122, "ICEYE-X84", "2026-11-04T21:44:32.000000"),
        ]
    )
    calls = []
    monkeypatch.setattr(tle_module, "download_name_search_json", lambda name: calls.append(name) or response)
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    records = search_by_name("ariane", tmp_path, now)
    assert [record.norad_id for record in records] == [20443, 101122]
    assert [record.norad_id for record in search_by_name("ariane", tmp_path, now + timedelta(hours=1))] == [
        20443,
        101122,
    ]
    assert calls == ["ariane"]
    cached = json.loads((tmp_path / "101122.json").read_text())
    assert cached["omm"]["OBJECT_NAME"] == "ICEYE-X84"


def test_name_search_without_match_is_empty(tmp_path, monkeypatch):
    """CelesTrak's 'No GP data found' (HTTP 404) for a name means no match, not an error.

    Reference value: the 404 response observed for an unknown name on 2026-11-05.
    """

    def no_match(name):
        raise NoGpDataError("no data")

    monkeypatch.setattr(tle_module, "download_name_search_json", no_match)
    assert search_by_name("no such satellite", tmp_path, datetime(2026, 11, 1, tzinfo=UTC)) == []


def test_failed_name_search_falls_back_to_stale_cache(tmp_path, monkeypatch):
    """If CelesTrak fails, an older cached search result is used, as with --norad.

    Reference value: the cached retrieval time, which must be returned unchanged.
    """
    response = json.dumps([_omm(101122, "ICEYE-X84", "2026-11-04T21:44:32.000000")])
    monkeypatch.setattr(tle_module, "download_name_search_json", lambda name: response)
    old = datetime(2026, 11, 5, 12, tzinfo=UTC)
    search_by_name("ICEYE-X84", tmp_path, old)

    def timed_out(name):
        raise TleError("CelesTrak did not answer")

    monkeypatch.setattr(tle_module, "download_name_search_json", timed_out)
    records = search_by_name("ICEYE-X84", tmp_path, old + timedelta(days=1))
    assert [(record.norad_id, record.retrieved_utc) for record in records] == [(101122, old)]
    with pytest.raises(TleError, match="did not answer"):
        search_by_name("ICEYE-X36", tmp_path, old + timedelta(days=1))


def test_cli_lists_several_name_matches(tmp_path, monkeypatch, capsys):
    """With several matches the CLI lists NORAD numbers and stops without predicting.

    Reference values: the stub catalog numbers and epochs.
    """
    response = json.dumps(
        [
            _omm(20443, "ARIANE 40 R/B", "2026-10-31T09:04:19.000000"),
            _omm(21610, "ARIANE 40 R/B", "2026-10-31T08:57:07.000000"),
        ]
    )
    monkeypatch.setattr(tle_module, "download_name_search_json", lambda name: response)
    exit_code = main(["--name", "ariane 40", "--cache-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "2 satellites match 'ariane 40'" in output
    assert "20443  ARIANE 40 R/B             2026-10-31 09:04" in output
    assert "Pass 1" not in output
