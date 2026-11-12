"""Simple orbit geometry used for sanity checks and the TLE-age report.

The pass-duration bound is derived in docs/pass_prediction_explained_fi.tex (section
"Ylilennon enimmäiskesto"). It uses only plane geometry on a spherical Earth.
"""

from __future__ import annotations

import math
from datetime import datetime

from sgp4.earth_gravity import wgs72
from skyfield.api import EarthSatellite
from skyfield.constants import ANGVEL

# WGS-72 equatorial radius (6378.135 km), the constant SGP4 itself uses
# (Vallado et al. 2006, "Revisiting Spacetrack Report #3").
EARTH_RADIUS_KM = wgs72.radiusearthkm
# Earth rotation rate relative to the stars; Skyfield's ANGVEL is in rad/s.
EARTH_ROTATION_RAD_PER_MIN = ANGVEL * 60.0


def orbital_period_minutes(satellite: EarthSatellite) -> float:
    """Orbital period in minutes from the TLE mean motion (stored by sgp4 as ``no_kozai``, rad/min)."""
    return 2.0 * math.pi / satellite.model.no_kozai


def apogee_altitude_km(satellite: EarthSatellite) -> float:
    """Apogee height above the equatorial radius (sgp4's ``alta`` is in Earth radii)."""
    return satellite.model.alta * EARTH_RADIUS_KM


def visibility_half_angle_radians(altitude_km: float, min_elevation_degrees: float) -> float:
    """Earth-central half-angle of the circle where the satellite is above the mask.

    Triangle Earth centre - station - satellite, with the angle at the station
    equal to 90 deg + elevation. The sine rule gives the nadir angle
    eta = asin(R cos(eps) / (R + h)), and the central angle is
    lambda = 90 deg - eps - eta.
    """
    elevation_radians = math.radians(min_elevation_degrees)
    nadir_angle = math.asin(EARTH_RADIUS_KM * math.cos(elevation_radians) / (EARTH_RADIUS_KM + altitude_km))
    return math.pi / 2.0 - elevation_radians - nadir_angle


def max_pass_duration_minutes(satellite: EarthSatellite, min_elevation_degrees: float) -> float:
    """Conservative upper bound for one pass of a near-circular LEO satellite.

    The longest pass goes through the zenith and spans a central angle of
    2 * lambda. The satellite moves at angular rate n; the station moves with
    the Earth at most at rate omega_E in the same direction, so the relative
    rate is at least n - omega_E. Using the apogee altitude makes lambda as
    large as possible. Hence T_max = 2 * lambda / (n - omega_E).
    """
    half_angle = visibility_half_angle_radians(apogee_altitude_km(satellite), min_elevation_degrees)
    relative_rate = satellite.model.no_kozai - EARTH_ROTATION_RAD_PER_MIN
    return 2.0 * half_angle / relative_rate


def tle_age_days(satellite: EarthSatellite, at_utc: datetime) -> float:
    """Days from the TLE epoch to ``at_utc`` (negative if before the epoch)."""
    epoch_utc = satellite.epoch.utc_datetime()
    return (at_utc - epoch_utc).total_seconds() / 86400.0
