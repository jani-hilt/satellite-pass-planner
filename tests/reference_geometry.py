"""Independent reference implementation used only by the tests.

Shares nothing with the code under test except the SGP4 propagator itself
(the ``sgp4`` package, which is the model being used, not the thing being
checked). Everything after SGP4 is written out here with NumPy:

1. SGP4 position in the TEME frame for UTC times (the TLE epoch is UTC,
   Vallado et al. 2006).
2. Rotation TEME -> pseudo Earth-fixed (PEF) about the z axis by the
   Greenwich mean sidereal time of the IAU-1982 model, which is the angle
   that defines TEME (Vallado et al. 2006). The GMST polynomial is copied
   from ``gstime()`` in python-sgp4 (Vallado's code) and vectorised here;
   it is fed UTC instead of UT1: the difference |UT1 - UTC| < 0.9 s shifts
   the Earth's rotation by < 0.9 s, which is quantified in the tests. Polar
   motion (PEF -> ITRF, a few metres to ~15 m) is ignored.
3. Station position from geodetic coordinates on the WGS84 ellipsoid.
4. Line-of-sight vector projected onto local east/north/up unit vectors.
5. AOS/LOS/maximum found by brute force on a dense grid: linear
   interpolation for mask crossings, a parabola through the three samples
   around each local maximum.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sgp4.api import Satrec

# WGS84 defining constants (semi-major axis and inverse flattening).
# [unverified from the standard itself; cross-checked in test_reference_constants
# against Skyfield's wgs84 model.]
WGS84_EQUATORIAL_RADIUS_KM = 6378.137
WGS84_INVERSE_FLATTENING = 298.257223563
UNIX_EPOCH_JULIAN_DATE = 2440587.5


@dataclass(frozen=True)
class ReferencePass:
    """Brute-force pass: times as POSIX seconds (UTC), elevation in degrees."""

    aos_seconds: float
    culmination_seconds: float
    culmination_elevation_degrees: float
    los_seconds: float


def greenwich_mean_sidereal_time_radians(julian_date_ut1: np.ndarray) -> np.ndarray:
    """IAU-1982 GMST in radians, same expression as python-sgp4's ``gstime()``."""
    centuries = (julian_date_ut1 - 2451545.0) / 36525.0
    seconds = (
        -6.2e-6 * centuries**3
        + 0.093104 * centuries**2
        + (876600.0 * 3600.0 + 8640184.812866) * centuries
        + 67310.54841
    )
    return np.radians(seconds / 240.0) % (2.0 * np.pi)  # 360 deg / 86400 s = 1/240 deg per s


def station_ecef_km(latitude_degrees: float, longitude_degrees: float, altitude_m: float) -> np.ndarray:
    """Earth-fixed Cartesian position (km) of a point given in WGS84 geodetic coordinates."""
    latitude = np.radians(latitude_degrees)
    longitude = np.radians(longitude_degrees)
    flattening = 1.0 / WGS84_INVERSE_FLATTENING
    eccentricity_squared = flattening * (2.0 - flattening)
    prime_vertical_radius = WGS84_EQUATORIAL_RADIUS_KM / np.sqrt(1.0 - eccentricity_squared * np.sin(latitude) ** 2)
    altitude_km = altitude_m / 1000.0
    return np.array(
        [
            (prime_vertical_radius + altitude_km) * np.cos(latitude) * np.cos(longitude),
            (prime_vertical_radius + altitude_km) * np.cos(latitude) * np.sin(longitude),
            (prime_vertical_radius * (1.0 - eccentricity_squared) + altitude_km) * np.sin(latitude),
        ]
    )


def elevation_azimuth_degrees(
    line1: str,
    line2: str,
    latitude_degrees: float,
    longitude_degrees: float,
    altitude_m: float,
    posix_seconds: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Elevation and azimuth (degrees) of the satellite at UTC POSIX times.

    POSIX time has no leap seconds, which is fine inside a window that does
    not contain one.
    """
    satellite = Satrec.twoline2rv(line1, line2)
    julian_date = UNIX_EPOCH_JULIAN_DATE + posix_seconds / 86400.0
    whole_days = np.floor(julian_date)
    error_codes, position_teme_km, _velocity = satellite.sgp4_array(whole_days, julian_date - whole_days)
    if np.any(error_codes):
        raise RuntimeError(f"SGP4 error codes {set(error_codes.tolist())}")

    sidereal_angle = greenwich_mean_sidereal_time_radians(julian_date)
    cos_angle, sin_angle = np.cos(sidereal_angle), np.sin(sidereal_angle)
    position_earth_fixed_km = np.column_stack(
        [
            cos_angle * position_teme_km[:, 0] + sin_angle * position_teme_km[:, 1],
            -sin_angle * position_teme_km[:, 0] + cos_angle * position_teme_km[:, 1],
            position_teme_km[:, 2],
        ]
    )
    line_of_sight_km = position_earth_fixed_km - station_ecef_km(latitude_degrees, longitude_degrees, altitude_m)

    latitude = np.radians(latitude_degrees)
    longitude = np.radians(longitude_degrees)
    east = np.array([-np.sin(longitude), np.cos(longitude), 0.0])
    north = np.array([-np.sin(latitude) * np.cos(longitude), -np.sin(latitude) * np.sin(longitude), np.cos(latitude)])
    up = np.array([np.cos(latitude) * np.cos(longitude), np.cos(latitude) * np.sin(longitude), np.sin(latitude)])

    range_km = np.linalg.norm(line_of_sight_km, axis=1)
    elevation = np.degrees(np.arcsin(line_of_sight_km @ up / range_km))
    azimuth = np.degrees(np.arctan2(line_of_sight_km @ east, line_of_sight_km @ north)) % 360.0
    return elevation, azimuth


def brute_force_passes(
    line1: str,
    line2: str,
    latitude_degrees: float,
    longitude_degrees: float,
    altitude_m: float,
    start_utc: datetime,
    end_utc: datetime,
    min_elevation_degrees: float,
    step_seconds: float = 1.0,
) -> list[ReferencePass]:
    """Complete passes found by sampling elevation every ``step_seconds``.

    Passes cut by the window edges are dropped (they have no AOS or LOS).
    If a pass has several local maxima the highest is reported.
    """
    start_seconds = start_utc.timestamp()
    sample_seconds = start_seconds + np.arange(0.0, (end_utc - start_utc).total_seconds() + 1e-9, step_seconds)
    elevation, _ = elevation_azimuth_degrees(
        line1, line2, latitude_degrees, longitude_degrees, altitude_m, sample_seconds
    )
    above = elevation >= min_elevation_degrees
    change_indices = np.flatnonzero(np.diff(above.astype(np.int8)))

    def crossing_time(index: int) -> float:
        # Linear interpolation of elevation between samples index and index + 1.
        fraction = (min_elevation_degrees - elevation[index]) / (elevation[index + 1] - elevation[index])
        return sample_seconds[index] + fraction * step_seconds

    passes: list[ReferencePass] = []
    for rise_index in change_indices:
        if above[rise_index]:  # this change is a set; handled from its rise
            continue
        later_sets = change_indices[(change_indices > rise_index) & above[change_indices]]
        if later_sets.size == 0:
            break
        set_index = later_sets[0]
        segment = slice(rise_index + 1, set_index + 1)
        peak_index = rise_index + 1 + int(np.argmax(elevation[segment]))
        # Parabola through (peak-1, peak, peak+1): vertex offset in steps.
        left, middle, right = elevation[peak_index - 1 : peak_index + 2]
        curvature = left - 2.0 * middle + right
        offset_steps = 0.5 * (left - right) / curvature if curvature != 0.0 else 0.0
        peak_elevation = middle - 0.25 * (left - right) * offset_steps
        passes.append(
            ReferencePass(
                aos_seconds=crossing_time(rise_index),
                culmination_seconds=sample_seconds[peak_index] + offset_steps * step_seconds,
                culmination_elevation_degrees=float(peak_elevation),
                los_seconds=crossing_time(set_index),
            )
        )
    return passes


def posix_seconds(time_utc: datetime) -> float:
    """Seconds since 1970-01-01 UTC for a timezone-aware datetime."""
    return time_utc.astimezone(UTC).timestamp()
