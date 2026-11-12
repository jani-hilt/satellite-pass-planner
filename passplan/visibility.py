"""Optical visibility of a pass: is the satellite sunlit while the observer's sky is dark?

A satellite reflects sunlight, so it can be seen by eye only when it is in
sunlight and the sky at the observer is dark enough (Skyfield documentation,
"Earth Satellites": "A satellite is generally only visible to a ground
observer when there is still sunlight up at its altitude.").

* Sunlight: Skyfield's ``is_sunlit()``. It treats the Earth as a sphere of
  the equatorial radius and calls the satellite shadowed when the straight
  line to the Sun hits that sphere (skyfield/positionlib.py, Skyfield 1.54):
  no penumbra and no atmosphere, so shadow entry and exit can be off by a few
  seconds.
* Dark sky: Sun more than 6 degrees below the horizon (end of civil
  twilight). This threshold is our choice; brighter satellites can be seen
  earlier and faint ones need a darker sky.

Radio contact does not depend on any of this; it is reported for observers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from skyfield.api import EarthSatellite, Loader
from skyfield.jpllib import SpiceKernel
from skyfield.timelib import Timescale

from passplan.passes import GroundStation, Pass, sample_times, utc_datetimes

# JPL DE421 covers 1900-2050 and is 17 MB (Skyfield documentation, planets.html,
# table under "Choosing an Ephemeris"); downloaded once, never updated.
EPHEMERIS_FILE_NAME = "de421.bsp"
DARK_SKY_SUN_ALTITUDE_DEGREES = -6.0

VISIBLE = "visible"
IN_SHADOW = "not visible: satellite in Earth's shadow"
DAYLIGHT = "not visible: daylight or twilight"
SUNLIT_ONLY_IN_BRIGHT_SKY = "not visible: sunlit only while the sky is still bright"


@dataclass(frozen=True)
class PassVisibility:
    """Visibility summary of one pass.

    Attributes:
        category: One of VISIBLE, IN_SHADOW, DAYLIGHT, SUNLIT_ONLY_IN_BRIGHT_SKY.
        visible_from_utc: First sampled instant that is visible (None if never).
        visible_until_utc: Last sampled instant that is visible (None if never).
    """

    category: str
    visible_from_utc: datetime | None = None
    visible_until_utc: datetime | None = None


def ephemeris_is_downloaded(directory: Path) -> bool:
    """True if the DE421 file is already in ``directory`` (no download needed)."""
    return (directory / EPHEMERIS_FILE_NAME).exists()


def load_ephemeris(directory: Path) -> SpiceKernel:
    """Open DE421 from ``directory``, downloading it there on first use (~17 MB)."""
    directory.mkdir(parents=True, exist_ok=True)
    return Loader(str(directory), verbose=False)(EPHEMERIS_FILE_NAME)


def classify_visibility(
    times_utc: list[datetime], sunlit: np.ndarray, sun_altitude_degrees: np.ndarray
) -> PassVisibility:
    """Classify a pass from sampled sunlight flags and Sun altitudes.

    Args:
        times_utc: Sample times.
        sunlit: True where the satellite is in sunlight.
        sun_altitude_degrees: Sun altitude at the observer, degrees.

    Returns:
        VISIBLE with the first and last visible sample if any sample is both
        sunlit and under a dark sky; otherwise the reason it is not visible.
    """
    dark_sky = sun_altitude_degrees < DARK_SKY_SUN_ALTITUDE_DEGREES
    visible = sunlit & dark_sky
    if visible.any():
        indices = np.flatnonzero(visible)
        return PassVisibility(VISIBLE, times_utc[indices[0]], times_utc[indices[-1]])
    if not dark_sky.any():
        return PassVisibility(DAYLIGHT)
    if not sunlit.any():
        return PassVisibility(IN_SHADOW)
    return PassVisibility(SUNLIT_ONLY_IN_BRIGHT_SKY)


def pass_visibility(
    satellite: EarthSatellite,
    station: GroundStation,
    satellite_pass: Pass,
    ephemeris: SpiceKernel,
    timescale: Timescale,
    step_seconds: float = 1.0,
) -> PassVisibility:
    """Visibility of one pass, sampled every ``step_seconds`` from AOS to LOS.

    Only the part of the pass above the elevation mask is examined; use a low
    mask (e.g. ``--min-elevation 0``) to see the whole pass above the horizon.
    """
    times = sample_times(satellite_pass.aos.time_utc, satellite_pass.los.time_utc, step_seconds, timescale)
    sunlit = np.asarray(satellite.at(times).is_sunlit(ephemeris), dtype=bool)
    observer = ephemeris["earth"] + station.position()
    sun_altitude, _, _ = observer.at(times).observe(ephemeris["sun"]).apparent().altaz()
    return classify_visibility(utc_datetimes(times), sunlit, sun_altitude.degrees)
