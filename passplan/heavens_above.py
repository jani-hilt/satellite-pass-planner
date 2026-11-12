"""Open Heavens-Above's pass list for one satellite and station, for comparison by hand.

The URL uses the station's exact coordinates, so Heavens-Above computes for
the same place as ``python -m passplan --station ...``. Its parameters
(``satid``, ``lat``, ``lng``, ``loc``, ``alt``, ``tz``, ``showall``) are the
ones Heavens-Above itself uses in its PassSummary links. ``tz=UCT`` gives
times in UTC, and ``showall=t`` also lists daylight passes, which Heavens-Above
otherwise hides for satellites too faint to see.

Nothing is printed: the coordinates of a private station go only to the
browser (and so to Heavens-Above), not to the terminal.

Run: ``python -m passplan.heavens_above --station turku --norad 25544 --browser chrome``
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
import webbrowser

from passplan.passes import GroundStation, load_station_settings, private_stations_file

PASS_SUMMARY_URL = "https://www.heavens-above.com/PassSummary.aspx"
# Names registered by Python's webbrowser module on macOS (and, for chrome and
# firefox, on Linux); "default" is the system default browser.
BROWSERS = ("default", "chrome", "safari", "firefox")


def pass_summary_url(station: GroundStation, norad_id: int) -> str:
    """Heavens-Above "all passes" URL for a satellite seen from a station, times in UTC."""
    query = {
        "satid": norad_id,
        "lat": f"{station.latitude_degrees:.7f}",
        "lng": f"{station.longitude_degrees:.7f}",
        "loc": station.name if station.show_coordinates else "Private station",
        "alt": f"{station.altitude_m:.0f}",
        "tz": "UCT",
        "showall": "t",
    }
    return f"{PASS_SUMMARY_URL}?{urllib.parse.urlencode(query)}"


def open_in_browser(url: str, browser_name: str) -> None:
    """Open ``url`` in the named browser ("default" = the system default).

    Raises:
        webbrowser.Error: If that browser is not available.
    """
    browser = webbrowser.get() if browser_name == "default" else webbrowser.get(browser_name)
    browser.open(url)


def main(argv: list[str] | None = None) -> int:
    try:
        stations, default_station = load_station_settings()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(
        prog="python -m passplan.heavens_above",
        description="Open Heavens-Above's pass list for a satellite at a station's exact coordinates.",
    )
    parser.add_argument("--norad", type=int, required=True, help="NORAD catalog number")
    parser.add_argument(
        "--station",
        choices=sorted(stations),
        default=default_station,
        help=f"built-in or private station (default: %(default)s); private ones from {private_stations_file()}",
    )
    parser.add_argument("--browser", choices=BROWSERS, default="default", help="browser (default: system default)")
    arguments = parser.parse_args(argv)
    try:
        open_in_browser(pass_summary_url(stations[arguments.station], arguments.norad), arguments.browser)
    except webbrowser.Error as error:
        print(f"error: cannot open browser {arguments.browser!r}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
