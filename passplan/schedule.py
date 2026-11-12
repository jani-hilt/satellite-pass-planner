"""Greedy contact scheduling for one ground station with one antenna.

Rule, in order:
1. Higher-priority satellites first (priority 1 is the most important).
2. Within a priority, higher maximum elevation first: a higher pass is
   longer and has a shorter slant range, so it carries more data.
3. Earlier AOS breaks any remaining tie, so the result is deterministic.

A candidate is accepted if it does not overlap any already accepted contact,
where each contact is padded by the antenna slew time on both sides (the
antenna needs that long to move from one pass's LOS direction to the next
pass's AOS direction). Otherwise it is rejected, and the reason names the
contact that blocked it.

Run: ``python -m passplan.schedule --satellite tests/data/iss.tle --satellite 42063``
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from skyfield.api import load

from passplan.cli import DEFAULT_CACHE_DIRECTORY, LOCAL_TIMEZONE, describe_station, format_duration, parse_utc
from passplan.passes import Pass, load_station_settings, predict_passes, private_stations_file
from passplan.tle import ElementSet, TleError, fetch_elements, load_tle_file

DEFAULT_SLEW_MINUTES = 2.0


@dataclass(frozen=True)
class ContactRequest:
    """One pass of one satellite that could be scheduled.

    Attributes:
        satellite_name: Display name.
        priority: 1 = most important; larger numbers are less important.
        satellite_pass: The predicted pass (AOS to LOS).
    """

    satellite_name: str
    priority: int
    satellite_pass: Pass

    @property
    def start(self) -> datetime:
        return self.satellite_pass.aos.time_utc

    @property
    def end(self) -> datetime:
        return self.satellite_pass.los.time_utc

    @property
    def tag(self) -> str:
        """Satellite name and priority, e.g. 'ISS (ZARYA) (P1)'."""
        return f"{self.satellite_name} (P{self.priority})"

    def label(self, tag_width: int = 0) -> str:
        """One-line description; ``tag_width`` pads the name so that columns line up."""
        local_start = self.start.astimezone(LOCAL_TIMEZONE)
        return (
            f"{self.tag:<{tag_width}}  {self.start:%m-%d %H:%M:%S}-{self.end:%H:%M:%S} UTC "
            f"[{local_start:%H:%M} {local_start:%Z}]  "
            f"max el {self.satellite_pass.culmination.elevation_degrees:4.1f}°"
        )


@dataclass(frozen=True)
class ScheduleDecision:
    """Outcome for one request: accepted, or rejected with a reason."""

    request: ContactRequest
    accepted: bool
    reason: str


def selection_order_key(request: ContactRequest) -> tuple[int, float, datetime]:
    """Sort key implementing the rule: priority, then highest elevation, then earliest AOS."""
    return (request.priority, -request.satellite_pass.culmination.elevation_degrees, request.start)


def conflicts(first: ContactRequest, second: ContactRequest, slew_time: timedelta) -> bool:
    """True if the two contacts overlap once each is followed by the slew time.

    Contacts [a1, b1] and [a2, b2] are compatible only if a2 >= b1 + slew or
    a1 >= b2 + slew.
    """
    return first.start < second.end + slew_time and second.start < first.end + slew_time


def schedule_contacts(requests: list[ContactRequest], slew_time: timedelta) -> list[ScheduleDecision]:
    """Choose a conflict-free set of contacts with the greedy rule in the module docstring.

    Args:
        requests: Candidate contacts from any number of satellites.
        slew_time: Minimum gap between one contact's LOS and the next one's AOS.

    Returns:
        One decision per request, in chronological order of AOS.
    """
    accepted: list[ContactRequest] = []
    decisions: list[ScheduleDecision] = []
    for request in sorted(requests, key=selection_order_key):
        blocking = [other for other in accepted if conflicts(request, other, slew_time)]
        if blocking:
            blocker = blocking[0]
            gap_minutes = min(
                abs((request.start - blocker.end).total_seconds()),
                abs((blocker.start - request.end).total_seconds()),
            )
            overlapping = request.start < blocker.end and blocker.start < request.end
            detail = "overlaps" if overlapping else f"only {gap_minutes / 60.0:.1f} min apart from"
            decisions.append(ScheduleDecision(request, False, f"{detail} higher-ranked contact {blocker.label()}"))
        else:
            accepted.append(request)
            decisions.append(ScheduleDecision(request, True, "no conflict"))
    return sorted(decisions, key=lambda decision: decision.request.start)


def _load(specification: str, cache_directory: Path) -> ElementSet:
    """A satellite spec is a NORAD number (fetched from CelesTrak) or a TLE / OMM JSON file path."""
    if specification.isdigit():
        return fetch_elements(int(specification), cache_directory)
    return load_tle_file(Path(specification))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m passplan.schedule",
        description="Greedy single-antenna contact schedule. Satellites are listed in priority order.",
    )
    parser.add_argument(
        "--satellite",
        action="append",
        required=True,
        help="NORAD number or TLE / OMM JSON file; repeat the option, first = highest priority",
    )
    try:
        stations, default_station = load_station_settings()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    parser.add_argument(
        "--station",
        choices=sorted(stations),
        default=default_station,
        help=f"built-in or private station (default: %(default)s); private ones from {private_stations_file()}",
    )
    parser.add_argument("--start", type=parse_utc, help="window start, ISO 8601 (default: now)")
    parser.add_argument("--days", type=float, default=1.0, help="window length in days (default: %(default)s)")
    parser.add_argument("--min-elevation", type=float, default=10.0, help="elevation mask, degrees")
    parser.add_argument(
        "--slew-minutes", type=float, default=DEFAULT_SLEW_MINUTES, help="antenna slew time between contacts"
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIRECTORY)
    arguments = parser.parse_args(argv)

    timescale = load.timescale()
    station = stations[arguments.station]
    start_utc = arguments.start or datetime.now(UTC).replace(microsecond=0)
    end_utc = start_utc + timedelta(days=arguments.days)

    requests: list[ContactRequest] = []
    for priority, specification in enumerate(arguments.satellite, start=1):
        try:
            record = _load(specification, arguments.cache_dir)
        except TleError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        satellite = record.to_satellite(timescale)
        name = satellite.name or f"NORAD {record.norad_id}"
        for satellite_pass in predict_passes(
            satellite, station, start_utc, end_utc, arguments.min_elevation, timescale
        ):
            requests.append(ContactRequest(name, priority, satellite_pass))

    decisions = schedule_contacts(requests, timedelta(minutes=arguments.slew_minutes))
    print(
        f"Station {describe_station(station)}, {start_utc:%Y-%m-%d %H:%M} UTC + {arguments.days:g} d, "
        f"mask {arguments.min_elevation:g}°, slew {arguments.slew_minutes:g} min"
    )
    print("P1, P2, ... = priority in the order the satellites were given (P1 = most important)")
    accepted = [decision for decision in decisions if decision.accepted]
    rejected = [decision for decision in decisions if not decision.accepted]
    total_contact = sum((decision.request.satellite_pass.duration for decision in accepted), timedelta(0))
    tag_width = max((len(request.tag) for request in requests), default=0)
    print(f"\nScheduled contacts ({len(accepted)}, total {format_duration(total_contact)}):")
    for decision in accepted:
        print(f"  + {decision.request.label(tag_width)}")
    print(f"\nRejected contacts ({len(rejected)}):")
    for decision in rejected:
        print(f"  - {decision.request.label(tag_width)}\n      reason: {decision.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
