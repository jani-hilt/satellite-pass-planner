"""Pass prediction checked against an independent method and physical constraints."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from skyfield.api import wgs84

from passplan.geometry import max_pass_duration_minutes, orbital_period_minutes
from passplan.passes import GROUND_STATIONS, look_angles, predict_passes, sample_times
from tests.conftest import (
    LEO_TLE_FILES,
    MIN_ELEVATION_DEGREES,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    frozen_tle,
)
from tests.reference_geometry import (
    WGS84_EQUATORIAL_RADIUS_KM,
    WGS84_INVERSE_FLATTENING,
    brute_force_passes,
    elevation_azimuth_degrees,
    posix_seconds,
)

# Skyfield's find_events refines rise/set and maxima to an epsilon of half a
# second (``half_second`` in skyfield/sgp4lib.py, Skyfield 1.54). The reference
# feeds UTC instead of UT1 to the sidereal-time formula; |UT1 - UTC| is about
# 0.09 s in November 2026 (Skyfield's IERS table). Linear interpolation on a
# 1 s grid adds at most a few milliseconds here. 0.5 + 0.1 rounded up to 1 s.
EVENT_TIME_TOLERANCE_SECONDS = 1.0
# Peak elevation: elevation changes by < 1e-4 deg within 0.5 s of the
# maximum for these orbits, and the 0.09 s rotation offset moves the station
# by ~20 m, i.e. < 0.01 deg at > 300 km range.
PEAK_ELEVATION_TOLERANCE_DEGREES = 0.01
SKYFIELD_SEARCH_EPSILON_SECONDS = 0.5

STATION_KEYS = sorted(GROUND_STATIONS)


def _passes(tle_file: str, station_key: str, timescale, min_elevation=MIN_ELEVATION_DEGREES, start=None, end=None):
    tle = frozen_tle(tle_file)
    satellite = tle.to_satellite(timescale)
    station = GROUND_STATIONS[station_key]
    passes = predict_passes(
        satellite, station, start or WINDOW_START_UTC, end or WINDOW_END_UTC, min_elevation, timescale
    )
    return tle, satellite, station, passes


def test_reference_constants_match_skyfield_wgs84():
    """The hand-typed WGS84 constants in the reference agree with Skyfield's wgs84 model.

    Reference value: Skyfield's ``wgs84`` object (an independent code base).
    """
    assert wgs84.radius.km == pytest.approx(WGS84_EQUATORIAL_RADIUS_KM, abs=1e-9)
    assert wgs84.inverse_flattening == pytest.approx(WGS84_INVERSE_FLATTENING, abs=1e-9)


@pytest.mark.parametrize("station_key", STATION_KEYS)
@pytest.mark.parametrize("tle_file", LEO_TLE_FILES)
def test_look_angles_match_independent_frame_chain(tle_file, station_key, timescale):
    """Skyfield's elevation/azimuth equal those from a hand-written TEME -> ENU chain.

    Reference value: tests/reference_geometry.py (raw sgp4 + IAU-82 GMST
    rotation + WGS84 station + ENU projection), sampled every 10 s over two
    days. Only times with the satellite above the horizon are compared.
    Tolerance 0.01 deg: the reference ignores polar motion (~10 m) and uses
    UTC for UT1 (0.09 s of Earth rotation, ~20 m at Helsinki).
    """
    tle = frozen_tle(tle_file)
    satellite = tle.to_satellite(timescale)
    station = GROUND_STATIONS[station_key]
    times = sample_times(WINDOW_START_UTC, WINDOW_END_UTC, 10.0, timescale)
    elevation, azimuth = look_angles(satellite, station, times)
    reference_seconds = np.array([posix_seconds(time_utc) for time_utc in times.utc_datetime()])
    reference_elevation, reference_azimuth = elevation_azimuth_degrees(
        tle.line1,
        tle.line2,
        station.latitude_degrees,
        station.longitude_degrees,
        station.altitude_m,
        reference_seconds,
    )
    visible = reference_elevation > 0.0
    assert visible.sum() > 50
    assert np.max(np.abs(elevation[visible] - reference_elevation[visible])) < 0.01
    azimuth_difference = (azimuth[visible] - reference_azimuth[visible] + 180.0) % 360.0 - 180.0
    # Azimuth is ill-defined near the zenith, so it gets a looser bound there.
    low = reference_elevation[visible] < 80.0
    assert np.max(np.abs(azimuth_difference[low])) < 0.05


@pytest.mark.parametrize("station_key", STATION_KEYS)
@pytest.mark.parametrize("tle_file", LEO_TLE_FILES)
def test_events_match_brute_force_search(tle_file, station_key, timescale):
    """AOS, TCA and LOS agree with a brute-force 1 s scan within 1 s.

    Reference value: ``brute_force_passes`` in tests/reference_geometry.py,
    which samples elevation every second with its own coordinate chain and
    interpolates the crossings (linear) and maxima (parabola). See
    EVENT_TIME_TOLERANCE_SECONDS for the error budget.
    """
    tle, _, station, passes = _passes(tle_file, station_key, timescale)
    complete = [item for item in passes if item.is_complete]
    reference = brute_force_passes(
        tle.line1,
        tle.line2,
        station.latitude_degrees,
        station.longitude_degrees,
        station.altitude_m,
        WINDOW_START_UTC,
        WINDOW_END_UTC,
        MIN_ELEVATION_DEGREES,
    )
    assert len(complete) == len(reference) > 0
    for predicted, expected in zip(complete, reference, strict=True):
        assert posix_seconds(predicted.aos.time_utc) == pytest.approx(
            expected.aos_seconds, abs=EVENT_TIME_TOLERANCE_SECONDS
        )
        assert posix_seconds(predicted.culmination.time_utc) == pytest.approx(
            expected.culmination_seconds, abs=EVENT_TIME_TOLERANCE_SECONDS
        )
        assert posix_seconds(predicted.los.time_utc) == pytest.approx(
            expected.los_seconds, abs=EVENT_TIME_TOLERANCE_SECONDS
        )
        assert predicted.culmination.elevation_degrees == pytest.approx(
            expected.culmination_elevation_degrees, abs=PEAK_ELEVATION_TOLERANCE_DEGREES
        )


@pytest.mark.parametrize("station_key", STATION_KEYS)
@pytest.mark.parametrize("tle_file", LEO_TLE_FILES)
def test_physical_constraints(tle_file, station_key, timescale):
    """Mask crossings, peak range, event order and pass length are physically consistent.

    Reference values:
    * elevation at AOS/LOS = mask, within |d(elevation)/dt| x 0.5 s, because
      Skyfield locates the crossing to 0.5 s (rate measured by finite difference);
    * peak elevation in [mask, 90 deg] by definition of elevation;
    * AOS < TCA < LOS by definition;
    * duration <= T_max = 2 lambda / (n - omega_E), the zenith-pass bound
      derived in passplan/geometry.py and docs/pass_prediction_explained_fi.tex from plane geometry.
    """
    _, satellite, station, passes = _passes(tle_file, station_key, timescale)
    duration_bound = timedelta(minutes=max_pass_duration_minutes(satellite, MIN_ELEVATION_DEGREES))
    complete = [item for item in passes if item.is_complete]
    assert complete
    for satellite_pass in complete:
        for point in (satellite_pass.aos, satellite_pass.los):
            centre = timescale.from_datetime(point.time_utc)
            neighbours = timescale.tt_jd(centre.tt + np.array([-0.5, 0.5]) / 86400.0)
            elevation_pair, _ = look_angles(satellite, station, neighbours)
            rate_degrees_per_second = abs(elevation_pair[1] - elevation_pair[0])
            allowed = rate_degrees_per_second * SKYFIELD_SEARCH_EPSILON_SECONDS + 1e-6
            assert abs(point.elevation_degrees - MIN_ELEVATION_DEGREES) <= allowed
        assert MIN_ELEVATION_DEGREES <= satellite_pass.culmination.elevation_degrees <= 90.0
        assert satellite_pass.aos.time_utc < satellite_pass.culmination.time_utc < satellite_pass.los.time_utc
        assert timedelta(0) < satellite_pass.duration <= duration_bound


def test_duration_bound_value_for_iss(timescale):
    """The bound is a few minutes for the ISS, as expected for LEO at ~420 km.

    Reference value: hand calculation in docs/pass_prediction_explained_fi.tex (about 6.9 min for
    h = 420 km, mask 10 deg, period 93 min).
    """
    satellite = frozen_tle("iss.tle").to_satellite(timescale)
    assert max_pass_duration_minutes(satellite, 10.0) == pytest.approx(6.9, abs=0.15)


@pytest.mark.parametrize("station_key", STATION_KEYS)
@pytest.mark.parametrize("tle_file", LEO_TLE_FILES)
def test_pass_repetition_matches_orbital_period(tle_file, station_key, timescale):
    """Passes on consecutive revolutions repeat after about one orbital period.

    Reference value: period P = 1440 min / n with n read directly from TLE
    line 2 (revolutions per day), independent of the pass search. For two
    passes one revolution apart, the satellite's position along its orbit at
    the two maxima differs by less than the visible arc 2 lambda (both lie on
    it), so |gap - P| < 2 lambda / n <= T_max. Gaps of several revolutions are
    not bounded this way because the Earth turns a further ~25 deg per orbit,
    so for those only "at least one revolution" is checked.
    """
    _, satellite, _, passes = _passes(tle_file, station_key, timescale)
    period_minutes = orbital_period_minutes(satellite)
    expected_period_minutes = 1440.0 / float(frozen_tle(tle_file).line2[52:63])
    assert period_minutes == pytest.approx(expected_period_minutes, rel=1e-9)

    maxima_minutes = np.array([posix_seconds(item.culmination.time_utc) / 60.0 for item in passes])
    gaps_in_periods = np.diff(maxima_minutes) / period_minutes
    whole_orbits = np.round(gaps_in_periods)
    assert np.all(whole_orbits >= 1)
    assert np.any(whole_orbits == 1)
    bound_in_periods = max_pass_duration_minutes(satellite, MIN_ELEVATION_DEGREES) / period_minutes
    one_orbit = whole_orbits == 1
    assert np.all(np.abs(gaps_in_periods[one_orbit] - 1.0) < bound_in_periods)


def test_window_starting_mid_pass(timescale):
    """A window that opens at a pass's maximum gives a pass flagged as already in progress.

    Reference value: the same pass computed over the full window. The LOS
    must agree within the 0.5 s search epsilon; the AOS is the window start
    and the in-window maximum is the window start (the satellite is setting).
    """
    _, _, _, full_passes = _passes("iss.tle", "helsinki", timescale)
    target = full_passes[0]
    _, _, _, clipped = _passes("iss.tle", "helsinki", timescale, start=target.culmination.time_utc)
    first = clipped[0]
    assert first.starts_before_window and not first.ends_after_window
    assert abs((first.aos.time_utc - target.culmination.time_utc).total_seconds()) < 1e-3
    assert abs((first.los.time_utc - target.los.time_utc).total_seconds()) <= SKYFIELD_SEARCH_EPSILON_SECONDS
    assert first.culmination.elevation_degrees == pytest.approx(target.culmination.elevation_degrees, abs=0.01)
    assert first.duration < target.duration


def test_window_ending_mid_pass(timescale):
    """A window that closes at a pass's maximum gives a pass flagged as still in progress.

    Reference value: the same pass computed over the full window (AOS within
    the 0.5 s search epsilon; LOS equals the window end).
    """
    _, _, _, full_passes = _passes("iss.tle", "helsinki", timescale)
    target = full_passes[0]
    _, _, _, clipped = _passes("iss.tle", "helsinki", timescale, end=target.culmination.time_utc)
    last = clipped[-1]
    assert last.ends_after_window and not last.starts_before_window
    assert abs((last.aos.time_utc - target.aos.time_utc).total_seconds()) <= SKYFIELD_SEARCH_EPSILON_SECONDS
    assert abs((last.los.time_utc - target.culmination.time_utc).total_seconds()) < 1e-3


def test_clipped_edges_print_as_the_window_edges(timescale):
    """A window edge on a whole second is reported as exactly that second.

    Reference values: the window edges themselves. One float64 TT Julian
    date resolves only about 40 us (2.46e6 d x 2**-52), so without rounding
    a start such as 17:40:54 came back as 17:40:53.999994 and was printed,
    truncated, one second early. 60 whole-second starts inside an ISS pass
    are tried, and the same for the window end.
    """
    _, _, _, full_passes = _passes("iss.tle", "helsinki", timescale)
    target = full_passes[0]
    first_second = target.aos.time_utc.replace(microsecond=0) + timedelta(seconds=30)
    for offset_seconds in range(60):
        edge = first_second + timedelta(seconds=offset_seconds)
        _, _, _, starting = _passes("iss.tle", "helsinki", timescale, start=edge)
        assert starting[0].starts_before_window
        assert starting[0].aos.time_utc == edge
        _, _, _, ending = _passes("iss.tle", "helsinki", timescale, end=edge)
        assert ending[-1].ends_after_window
        assert ending[-1].los.time_utc == edge


def test_leo_never_above_mask(timescale):
    """The ISS never reaches 30 deg from Helsinki in this window, so no passes are returned.

    Reference value: the brute-force reference scan finds a maximum elevation
    below 30 deg over the same window.
    """
    tle, _, station, passes = _passes("iss.tle", "helsinki", timescale, min_elevation=30.0)
    seconds = np.arange(posix_seconds(WINDOW_START_UTC), posix_seconds(WINDOW_END_UTC), 1.0)
    reference_elevation, _ = elevation_azimuth_degrees(
        tle.line1, tle.line2, station.latitude_degrees, station.longitude_degrees, station.altitude_m, seconds
    )
    assert reference_elevation.max() < 30.0
    assert passes == []


def test_geostationary_always_visible(timescale):
    """Meteosat-11 (geostationary, ~17-24 deg from Helsinki) stays above a 10 deg mask.

    Reference value: the reference chain's elevation sampled every minute
    has its minimum above 10 deg, so there must be exactly one pass that
    covers the whole window and is clipped at both ends.
    """
    tle, _, station, passes = _passes("meteosat11.tle", "helsinki", timescale)
    seconds = np.arange(posix_seconds(WINDOW_START_UTC), posix_seconds(WINDOW_END_UTC), 60.0)
    reference_elevation, _ = elevation_azimuth_degrees(
        tle.line1, tle.line2, station.latitude_degrees, station.longitude_degrees, station.altitude_m, seconds
    )
    assert reference_elevation.min() > MIN_ELEVATION_DEGREES
    assert len(passes) == 1
    only = passes[0]
    assert only.starts_before_window and only.ends_after_window
    assert abs((only.duration - (WINDOW_END_UTC - WINDOW_START_UTC)).total_seconds()) < 1e-3
    assert only.culmination.elevation_degrees == pytest.approx(reference_elevation.max(), abs=0.05)


def test_geostationary_never_visible(timescale):
    """GOES-18 (geostationary over the Pacific) is below the horizon from Helsinki.

    Reference value: the reference chain's elevation sampled every minute is
    negative throughout.
    """
    tle, _, station, passes = _passes("goes18.tle", "helsinki", timescale)
    seconds = np.arange(posix_seconds(WINDOW_START_UTC), posix_seconds(WINDOW_END_UTC), 60.0)
    reference_elevation, _ = elevation_azimuth_degrees(
        tle.line1, tle.line2, station.latitude_degrees, station.longitude_degrees, station.altitude_m, seconds
    )
    assert reference_elevation.max() < 0.0
    assert passes == []


def test_inclined_geostationary_daily_passes(timescale):
    """Meteosat-11's 3.5 deg inclination makes its elevation swing daily; a 20 deg mask cuts it into passes.

    Reference value: brute-force reference scan with the same 20 deg mask on
    a 10 s grid. Tolerance 5 s: GEO elevation changes only ~1e-4 deg/s, so the
    reference's small geometric simplifications (UTC for UT1, no polar
    motion; ~4e-4 deg here) can move a crossing by a few seconds.
    """
    tle, _, station, passes = _passes("meteosat11.tle", "helsinki", timescale, min_elevation=20.0)
    reference = brute_force_passes(
        tle.line1,
        tle.line2,
        station.latitude_degrees,
        station.longitude_degrees,
        station.altitude_m,
        WINDOW_START_UTC,
        WINDOW_END_UTC,
        20.0,
        step_seconds=10.0,
    )
    complete = [item for item in passes if item.is_complete]
    assert len(complete) == len(reference) >= 1
    for predicted, expected in zip(complete, reference, strict=True):
        assert posix_seconds(predicted.aos.time_utc) == pytest.approx(expected.aos_seconds, abs=5.0)
        assert posix_seconds(predicted.los.time_utc) == pytest.approx(expected.los_seconds, abs=5.0)
