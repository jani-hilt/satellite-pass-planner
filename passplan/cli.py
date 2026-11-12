"""Command-line interface: ``python -m passplan --norad 25544``."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from skyfield.api import load

from passplan.geometry import orbital_period_minutes, tle_age_days
from passplan.passes import (
    GroundStation,
    Pass,
    SkyPoint,
    load_station_settings,
    max_elevation_in_window,
    predict_passes,
    private_stations_file,
)
from passplan.tle import ElementSet, TleError, fetch_elements, load_tle_file, search_by_name

LOCAL_TIMEZONE = ZoneInfo("Europe/Helsinki")
# Skyfield's documentation: "Once a file is a few days old, you will probably
# want to download the file again." Vallado et al. (2006): TLE data is accurate
# to about a kilometre at epoch "and it quickly degrades". 3 days is our
# reading of "a few days", not a published threshold.
TLE_AGE_WARNING_DAYS = 3.0
DEFAULT_CACHE_DIRECTORY = Path(".tle_cache")
# A plot file name without a directory goes here, so plots do not pile up in
# the repository root.
DEFAULT_PLOT_DIRECTORY = Path("plots")


def parse_utc(text: str) -> datetime:
    """Parse an ISO 8601 time; a time without an offset is taken as UTC."""
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def build_argument_parser(stations: dict[str, GroundStation], default_station: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m passplan",
        description="Predict satellite passes over a ground station (SGP4 via Skyfield).",
    )
    source = parser.add_argument_group("orbit data")
    source.add_argument("--norad", type=int, help="NORAD catalog number (downloaded from CelesTrak, cached 2 h)")
    source.add_argument("--tle-file", type=Path, help="read a TLE or CelesTrak OMM JSON file instead of downloading")
    source.add_argument(
        "--name",
        help="search CelesTrak by (part of) the satellite name; one match is used, several are listed",
    )
    source.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIRECTORY,
        help="element-set cache directory (default: %(default)s)",
    )

    station = parser.add_argument_group("ground station")
    station.add_argument(
        "--station",
        choices=sorted(stations),
        default=default_station,
        help="built-in or private station (default: %(default)s); overridden by --lat/--lon. "
        f"Private stations are read from {private_stations_file()}",
    )
    station.add_argument("--lat", type=float, help="geodetic latitude, degrees north")
    station.add_argument("--lon", type=float, help="longitude, degrees east")
    station.add_argument(
        "--altitude-m", type=float, default=0.0, help="height above WGS84 ellipsoid, m (with --lat/--lon)"
    )

    window = parser.add_argument_group("search window")
    window.add_argument("--start", type=parse_utc, help="window start, ISO 8601, UTC if no offset (default: now)")
    window.add_argument("--days", type=float, default=2.0, help="window length in days (default: %(default)s)")
    window.add_argument(
        "--min-elevation", type=float, default=10.0, help="elevation mask in degrees (default: %(default)s)"
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--plot",
        type=Path,
        help=f"save a sky plot of one pass to this PNG file; a bare file name goes into {DEFAULT_PLOT_DIRECTORY}/",
    )
    output.add_argument("--plot-pass", type=int, help="pass number to plot (default: highest complete pass)")
    output.add_argument(
        "--visibility",
        action="store_true",
        help="also tell whether each pass can be seen by eye (satellite sunlit, sky dark); "
        "downloads the 17 MB JPL DE421 ephemeris into --cache-dir on first use",
    )
    return parser


def resolve_station(arguments: argparse.Namespace, stations: dict[str, GroundStation]) -> GroundStation:
    if (arguments.lat is None) != (arguments.lon is None):
        raise SystemExit("error: give both --lat and --lon, or neither")
    if arguments.lat is not None:
        return GroundStation(
            name=f"{arguments.lat:.4f}°N {arguments.lon:.4f}°E",
            latitude_degrees=arguments.lat,
            longitude_degrees=arguments.lon,
            altitude_m=arguments.altitude_m,
        )
    return stations[arguments.station]


def describe_station(station: GroundStation) -> str:
    """Station name, plus coordinates unless the station is private."""
    if not station.show_coordinates:
        return f"{station.name}  (private station, coordinates not shown)"
    return (
        f"{station.name}  {station.latitude_degrees:.4f}°N {station.longitude_degrees:.4f}°E {station.altitude_m:.0f} m"
    )


def load_elements(arguments: argparse.Namespace, now_utc: datetime) -> ElementSet | None:
    """The requested element set, or None if a name search found several (they are listed)."""
    if arguments.tle_file is not None:
        return load_tle_file(arguments.tle_file, arguments.norad)
    if arguments.name is not None:
        matches = search_by_name(arguments.name, arguments.cache_dir, now_utc)
        if not matches:
            raise TleError(f"No satellite on CelesTrak has {arguments.name!r} in its name.")
        if len(matches) == 1:
            return matches[0]
        print(f"{len(matches)} satellites match {arguments.name!r}; choose one with --norad:")
        print(f"  {'NORAD':>6}  {'Name':<26}Epoch (UTC)")
        for record in matches:
            print(f"  {record.norad_id:>6}  {record.name:<26}{record.epoch_utc:%Y-%m-%d %H:%M}")
        return None
    if arguments.norad is None:
        raise SystemExit("error: give --norad, --name or --tle-file")
    return fetch_elements(arguments.norad, arguments.cache_dir, now_utc)


def format_time(time_utc: datetime) -> str:
    """'2026-11-01 03:12:05 UTC  (05:12:05 EET)'; the local date is shown if it differs."""
    local_time = time_utc.astimezone(LOCAL_TIMEZONE)
    local_format = "%H:%M:%S %Z" if local_time.date() == time_utc.date() else "%Y-%m-%d %H:%M:%S %Z"
    return f"{time_utc:%Y-%m-%d %H:%M:%S} UTC  ({local_time.strftime(local_format)})"


def format_duration(duration: timedelta) -> str:
    total_seconds = round(duration.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} h {minutes:02d} min {seconds:02d} s" if hours else f"{minutes} min {seconds:02d} s"


def format_pass(number: int, satellite_pass: Pass) -> str:
    def row(label: str, point: SkyPoint, note: str = "", show_elevation: bool = False) -> str:
        elevation = f"  el {point.elevation_degrees:5.1f}°" if show_elevation else ""
        return f"  {label:<4} {format_time(point.time_utc)}  az {point.azimuth_degrees:5.1f}°{elevation}{note}"

    start_note = "  [window start: satellite already above mask]" if satellite_pass.starts_before_window else ""
    end_note = "  [window end: satellite still above mask]" if satellite_pass.ends_after_window else ""
    max_note = "  [highest point inside window]" if not satellite_pass.is_complete else ""
    duration_prefix = "at least " if not satellite_pass.is_complete else ""
    return "\n".join(
        [
            f"Pass {number}",
            row("AOS", satellite_pass.aos, start_note),
            row("MAX", satellite_pass.culmination, max_note, show_elevation=True),
            row("LOS", satellite_pass.los, end_note),
            f"  Duration {duration_prefix}{format_duration(satellite_pass.duration)}",
        ]
    )


def format_visibility(visibility) -> str:
    """One indented line describing whether the pass can be seen by eye."""
    if visibility.visible_from_utc is None:
        return f"  Visibility {visibility.category}"
    start, end = visibility.visible_from_utc, visibility.visible_until_utc
    local_start, local_end = start.astimezone(LOCAL_TIMEZONE), end.astimezone(LOCAL_TIMEZONE)
    return (
        f"  Visibility VISIBLE {start:%H:%M:%S}-{end:%H:%M:%S} UTC "
        f"({local_start:%H:%M:%S}-{local_end:%H:%M:%S} {local_end:%Z}): sunlit, sky dark"
    )


def _visibility_function(arguments, satellite, station, timescale):
    """Return pass -> PassVisibility if --visibility was given, else None."""
    if not arguments.visibility:
        return None
    from passplan.visibility import ephemeris_is_downloaded, load_ephemeris, pass_visibility

    if not ephemeris_is_downloaded(arguments.cache_dir):
        print(f"(Downloading the JPL DE421 ephemeris, 17 MB, into {arguments.cache_dir}/ once.)")
    ephemeris = load_ephemeris(arguments.cache_dir)
    print(
        "Visibility: sunlit satellite and Sun below -6° counts as visible; "
        "only the part above the elevation mask is checked.\n"
    )
    return lambda satellite_pass: pass_visibility(satellite, station, satellite_pass, ephemeris, timescale)


def resolve_plot_path(path: Path) -> Path:
    """Put a bare file name into DEFAULT_PLOT_DIRECTORY; keep any path with a directory as given."""
    if path.is_absolute() or path.parent != Path("."):
        return path
    return DEFAULT_PLOT_DIRECTORY / path


def choose_pass_to_plot(passes: list[Pass], requested_number: int | None) -> tuple[int, Pass]:
    if requested_number is not None:
        if not 1 <= requested_number <= len(passes):
            raise SystemExit(f"error: --plot-pass must be between 1 and {len(passes)}")
        return requested_number, passes[requested_number - 1]
    candidates = [(number, item) for number, item in enumerate(passes, start=1) if item.is_complete] or list(
        enumerate(passes, start=1)
    )
    return max(candidates, key=lambda numbered: numbered[1].culmination.elevation_degrees)


def main(argv: list[str] | None = None) -> int:
    try:
        stations, default_station = load_station_settings()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    arguments = build_argument_parser(stations, default_station).parse_args(argv)
    now_utc = datetime.now(UTC)
    station = resolve_station(arguments, stations)
    try:
        elements = load_elements(arguments, now_utc)
    except TleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if elements is None:
        return 0

    timescale = load.timescale()
    try:
        satellite = elements.to_satellite(timescale)
    except TleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    start_utc = arguments.start or now_utc.replace(microsecond=0)
    end_utc = start_utc + timedelta(days=arguments.days)

    print(f"Satellite      {satellite.name or '(no name)'}  NORAD {elements.norad_id}")
    print(f"Elements       {elements.format_name}  {elements.source}")
    if elements.retrieved_utc is not None:
        cache_age_hours = (now_utc - elements.retrieved_utc).total_seconds() / 3600.0
        print(f"Retrieved      {elements.retrieved_utc:%Y-%m-%d %H:%M:%S} UTC ({cache_age_hours:.1f} h ago)")
        if cache_age_hours > 2.0:
            print("WARNING: could not refresh the elements from CelesTrak; using an older cached copy.")
    age_at_start = tle_age_days(satellite, start_utc)
    age_at_end = tle_age_days(satellite, end_utc)
    print(
        f"Epoch          {satellite.epoch.utc_datetime():%Y-%m-%d %H:%M:%S} UTC  "
        f"(age {age_at_start:+.2f} d at window start, {age_at_end:+.2f} d at window end)"
    )
    if max(abs(age_at_start), abs(age_at_end)) > TLE_AGE_WARNING_DAYS:
        print(
            f"WARNING: predictions extend more than {TLE_AGE_WARNING_DAYS:g} days from the element epoch; "
            "SGP4 errors grow with time from epoch, so times may be off by seconds or more."
        )
    print(f"Orbital period {orbital_period_minutes(satellite):.2f} min (from the mean motion)")
    print(f"Ground station {describe_station(station)}")
    print(
        f"Window         {start_utc:%Y-%m-%d %H:%M:%S} UTC + {arguments.days:g} d, "
        f"elevation mask {arguments.min_elevation:g}°"
    )
    print()

    passes = predict_passes(satellite, station, start_utc, end_utc, arguments.min_elevation, timescale)
    if not passes:
        highest = max_elevation_in_window(satellite, station, start_utc, end_utc, timescale)
        print(
            f"No passes: the satellite never rises above {arguments.min_elevation:g}° in this window "
            f"(highest elevation about {highest:.1f}°)."
        )
        return 0
    if len(passes) == 1 and passes[0].starts_before_window and passes[0].ends_after_window:
        print("The satellite stays above the mask for the whole window (e.g. geostationary).")
    visibility_of = _visibility_function(arguments, satellite, station, timescale)
    for number, satellite_pass in enumerate(passes, start=1):
        print(format_pass(number, satellite_pass))
        if visibility_of is not None:
            print(format_visibility(visibility_of(satellite_pass)))
    print(f"\n{len(passes)} pass(es).")

    if arguments.plot is not None:
        from passplan.plot import plot_pass

        number, selected_pass = choose_pass_to_plot(passes, arguments.plot_pass)
        plot_path = resolve_plot_path(arguments.plot)
        plot_path.parent.mkdir(parents=True, exist_ok=True)
        plot_pass(satellite, station, selected_pass, arguments.min_elevation, timescale, plot_path)
        print(f"Sky plot of pass {number} saved to {plot_path}")
    return 0
