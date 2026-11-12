"""Pass prediction: when a satellite is above a ground station's elevation mask.

The orbit is propagated with SGP4 (Skyfield's ``EarthSatellite``) and the
rise/culmination/set times come from Skyfield's ``find_events`` search
(Skyfield documentation, "Earth Satellites"). This module turns those raw
events into complete pass records and handles the edge cases: a pass that is
already in progress when the window opens, one that is still in progress when
it closes, and a satellite that never rises or never sets (geostationary).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from skyfield.api import EarthSatellite, wgs84
from skyfield.timelib import Time, Timescale
from skyfield.toposlib import GeographicPosition

RISE_EVENT, CULMINATION_EVENT, SET_EVENT = 0, 1, 2


@dataclass(frozen=True)
class GroundStation:
    """A ground station on the WGS84 ellipsoid.

    Attributes:
        name: Display name.
        latitude_degrees: Geodetic latitude, degrees north.
        longitude_degrees: Longitude, degrees east.
        altitude_m: Height above the WGS84 ellipsoid, metres.
        source: Where the coordinates come from.
        show_coordinates: False for private stations, whose coordinates are
            then left out of printed output (safe for screen sharing).
    """

    name: str
    latitude_degrees: float
    longitude_degrees: float
    altitude_m: float
    source: str = "user input"
    show_coordinates: bool = True

    def position(self) -> GeographicPosition:
        """Skyfield geographic position for this station."""
        return wgs84.latlon(self.latitude_degrees, self.longitude_degrees, self.altitude_m)


# A few tens of metres of altitude changes elevation angles negligibly, so an
# assumed 10 m is used where the source gives no height.
GROUND_STATIONS: dict[str, GroundStation] = {
    "helsinki": GroundStation(
        name="Helsinki (Tähtitorninvuori observatory hill)",
        latitude_degrees=60.1618132,
        longitude_degrees=24.9525605,
        altitude_m=31.0,
        source=(
            "OpenStreetMap node 11360115982, natural=peak 'Tähtitorninvuori', "
            "tag ele=31 (retrieved 2026-11-05 via Nominatim and the OSM API)"
        ),
    ),
    "turku": GroundStation(
        name="Turku (Kauppatori market square)",
        latitude_degrees=60.4517221,
        longitude_degrees=22.2668467,
        altitude_m=10.0,
        source=(
            "OpenStreetMap Nominatim, 'Kauppatori, Turku' (marketplace, way 621404185, "
            "retrieved 2026-10-31); altitude assumed"
        ),
    ),
}
DEFAULT_STATION_KEY = "helsinki"

# Private stations (e.g. home) live outside the repository so they can never be
# committed. The file location can be changed with an environment variable.
PRIVATE_STATIONS_ENVIRONMENT_VARIABLE = "PASSPLAN_STATIONS"
DEFAULT_PRIVATE_STATIONS_FILE = Path("~/.config/passplan/stations.json")


def private_stations_file() -> Path:
    """Path of the private station file ($PASSPLAN_STATIONS or ~/.config/passplan/stations.json)."""
    return Path(os.environ.get(PRIVATE_STATIONS_ENVIRONMENT_VARIABLE, DEFAULT_PRIVATE_STATIONS_FILE)).expanduser()


def load_station_settings(path: Path | None = None) -> tuple[dict[str, GroundStation], str]:
    """Built-in stations merged with the user's private stations, and the default key.

    The private file is JSON::

        {
          "default": "home",
          "stations": {
            "home": {"name": "Home", "latitude_degrees": 60.0,
                     "longitude_degrees": 22.0, "altitude_m": 10}
          }
        }

    ``default`` is optional. A missing file is not an error.

    Args:
        path: File to read; defaults to ``private_stations_file()``.

    Raises:
        ValueError: If the file is malformed or ``default`` names an unknown station.
    """
    path = path or private_stations_file()
    stations = dict(GROUND_STATIONS)
    if not path.exists():
        return stations, DEFAULT_STATION_KEY
    try:
        settings = json.loads(path.read_text())
        for key, fields in settings.get("stations", {}).items():
            stations[key] = GroundStation(
                name=str(fields.get("name", key)),
                latitude_degrees=float(fields["latitude_degrees"]),
                longitude_degrees=float(fields["longitude_degrees"]),
                altitude_m=float(fields.get("altitude_m", 0.0)),
                source=f"private station file {path}",
                show_coordinates=False,
            )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"Cannot read station file {path}: {error}") from error
    default_key = settings.get("default", DEFAULT_STATION_KEY)
    if default_key not in stations:
        raise ValueError(f"Station file {path}: default {default_key!r} is not a known station")
    return stations, default_key


@dataclass(frozen=True)
class SkyPoint:
    """Satellite direction seen from the station at one instant.

    Attributes:
        time_utc: Timezone-aware UTC datetime.
        azimuth_degrees: Clockwise from true north, 0-360.
        elevation_degrees: Above the horizon, -90 to 90.
    """

    time_utc: datetime
    azimuth_degrees: float
    elevation_degrees: float


@dataclass(frozen=True)
class Pass:
    """One contact window above the elevation mask.

    Attributes:
        aos: Acquisition of signal (rise above the mask), or the window start
            if ``starts_before_window``.
        culmination: Highest point inside the window (time of closest
            approach, TCA, for a complete pass).
        los: Loss of signal (set below the mask), or the window end if
            ``ends_after_window``.
        starts_before_window: The satellite was already above the mask at the
            window start, so the true AOS is earlier than ``aos``.
        ends_after_window: The satellite is still above the mask at the window
            end, so the true LOS is later than ``los``.
    """

    aos: SkyPoint
    culmination: SkyPoint
    los: SkyPoint
    starts_before_window: bool = False
    ends_after_window: bool = False

    @property
    def duration(self) -> timedelta:
        """LOS minus AOS (a lower bound if the pass is clipped by the window)."""
        return self.los.time_utc - self.aos.time_utc

    @property
    def is_complete(self) -> bool:
        """True if both AOS and LOS fall inside the search window."""
        return not (self.starts_before_window or self.ends_after_window)


def look_angles(satellite: EarthSatellite, station: GroundStation, times: Time) -> tuple[np.ndarray, np.ndarray]:
    """Elevation and azimuth of the satellite from the station.

    The chain is SGP4 (TEME frame) -> Skyfield's frame rotations -> local
    horizon frame of the station. Vectorised over ``times``.

    Returns:
        (elevation_degrees, azimuth_degrees) arrays with the shape of ``times``.
    """
    elevation, azimuth, _distance = (satellite - station.position()).at(times).altaz()
    return elevation.degrees, azimuth.degrees


def utc_datetimes(times: Time) -> list[datetime]:
    """UTC datetimes of a Skyfield Time array, rounded to the nearest millisecond.

    One float64 TT Julian date (about 2.46e6 d) resolves only 2.46e6 d x 2**-52,
    about 40 us, so a whole-second instant can come back as hh:mm:ss.999994 and
    print one second early when truncated. 1 ms is far below the 0.5 s event
    search tolerance, so rounding to it loses nothing.
    """
    rounded = []
    for moment in np.atleast_1d(times.utc_datetime()):
        shifted = moment + timedelta(microseconds=500)
        rounded.append(shifted.replace(microsecond=shifted.microsecond - shifted.microsecond % 1000))
    return rounded


def _sky_points(satellite: EarthSatellite, station: GroundStation, times: Time) -> list[SkyPoint]:
    elevation_degrees, azimuth_degrees = look_angles(satellite, station, times)
    return [
        SkyPoint(time_utc, float(azimuth), float(elevation))
        for time_utc, azimuth, elevation in zip(
            utc_datetimes(times), np.atleast_1d(azimuth_degrees), np.atleast_1d(elevation_degrees), strict=True
        )
    ]


def _build_pass(
    satellite: EarthSatellite,
    station: GroundStation,
    timescale: Timescale,
    start_tt: float,
    end_tt: float,
    culmination_tt: list[float],
    starts_before_window: bool,
    ends_after_window: bool,
) -> Pass:
    """Assemble a Pass from Terrestrial Time Julian dates of its events."""
    candidate_tt = list(culmination_tt)
    # If the pass is clipped, its highest point inside the window may be the
    # window edge itself (e.g. the satellite is setting when the window opens).
    if starts_before_window:
        candidate_tt.append(start_tt)
    if ends_after_window:
        candidate_tt.append(end_tt)
    points = _sky_points(satellite, station, timescale.tt_jd(np.array([start_tt, end_tt, *candidate_tt])))
    aos, los, candidates = points[0], points[1], points[2:]
    culmination = max(candidates, key=lambda point: point.elevation_degrees)
    return Pass(aos, culmination, los, starts_before_window, ends_after_window)


def predict_passes(
    satellite: EarthSatellite,
    station: GroundStation,
    start_utc: datetime,
    end_utc: datetime,
    min_elevation_degrees: float,
    timescale: Timescale,
) -> list[Pass]:
    """Find all passes above the elevation mask within a time window.

    Args:
        satellite: SGP4 satellite built from a TLE.
        station: Observer location.
        start_utc: Window start, timezone-aware.
        end_utc: Window end, timezone-aware.
        min_elevation_degrees: Elevation mask (horizon cut-off), degrees.
        timescale: Skyfield timescale (UTC/UT1/TT conversions).

    Returns:
        Passes in time order. A satellite that stays above the mask for the
        whole window gives one pass with both clipping flags set; one that
        never rises above the mask gives an empty list.
    """
    window_start = timescale.from_datetime(start_utc)
    window_end = timescale.from_datetime(end_utc)
    event_times, event_kinds = satellite.find_events(
        station.position(), window_start, window_end, altitude_degrees=min_elevation_degrees
    )

    start_elevation, _ = look_angles(satellite, station, window_start)
    passes: list[Pass] = []
    # Open pass being assembled: [start_tt, culmination_tts, starts_before_window]
    open_pass: list | None = None
    if start_elevation >= min_elevation_degrees:
        open_pass = [window_start.tt, [], True]

    for event_tt, event_kind in zip(event_times.tt, event_kinds, strict=True):
        if event_kind == RISE_EVENT:
            open_pass = [event_tt, [], False]
            continue
        if open_pass is None:
            # A culmination or set with no rise before it: the satellite was
            # above the mask at the window start within the search tolerance.
            open_pass = [window_start.tt, [], True]
        if event_kind == CULMINATION_EVENT:
            open_pass[1].append(event_tt)
        elif event_kind == SET_EVENT:
            start_tt, culminations, clipped_start = open_pass
            passes.append(
                _build_pass(satellite, station, timescale, start_tt, event_tt, culminations, clipped_start, False)
            )
            open_pass = None

    if open_pass is not None:
        start_tt, culminations, clipped_start = open_pass
        passes.append(
            _build_pass(satellite, station, timescale, start_tt, window_end.tt, culminations, clipped_start, True)
        )
    return passes


def max_elevation_in_window(
    satellite: EarthSatellite,
    station: GroundStation,
    start_utc: datetime,
    end_utc: datetime,
    timescale: Timescale,
    step_seconds: float = 30.0,
) -> float:
    """Highest elevation (degrees) on a regular time grid; used to explain 'no passes'.

    A 30 s grid can miss the true peak by a fraction of a degree for LEO,
    which is fine for a diagnostic message.
    """
    times = sample_times(start_utc, end_utc, step_seconds, timescale)
    elevation_degrees, _ = look_angles(satellite, station, times)
    return float(np.max(elevation_degrees))


def sample_times(start_utc: datetime, end_utc: datetime, step_seconds: float, timescale: Timescale) -> Time:
    """Regular time grid from start to end (inclusive of start) as a Skyfield Time array."""
    total_seconds = (end_utc - start_utc).total_seconds()
    offsets_seconds = np.arange(0.0, total_seconds + 1e-9, step_seconds)
    start = timescale.from_datetime(start_utc)
    return timescale.tt_jd(start.tt + offsets_seconds / 86400.0)
