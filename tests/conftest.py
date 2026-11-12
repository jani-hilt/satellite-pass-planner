"""Shared fixtures: frozen TLEs (tests/data) and a fixed two-day window, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from skyfield.api import load
from skyfield.timelib import Timescale

from passplan.passes import GroundStation
from passplan.tle import TleRecord, load_tle_file

DATA_DIRECTORY = Path(__file__).parent / "data"
WINDOW_START_UTC = datetime(2026, 11, 1, tzinfo=UTC)
WINDOW_END_UTC = WINDOW_START_UTC + timedelta(days=2)
MIN_ELEVATION_DEGREES = 10.0
LEO_TLE_FILES = ["iss.tle", "sentinel2b.tle"]
# The site entered in Heavens-Above on 2026-10-31 for the external comparison
# (the then default Helsinki station). Reference values from Heavens-Above
# belong to these coordinates, so they stay fixed even if the default changes.
HEAVENS_ABOVE_SITE = GroundStation(
    name="Heavens-Above comparison site", latitude_degrees=60.1527014, longitude_degrees=24.9254135, altitude_m=10.0
)


@pytest.fixture(scope="session")
def timescale() -> Timescale:
    """Skyfield timescale from the data files bundled with Skyfield (no download)."""
    return load.timescale()


def frozen_tle(file_name: str) -> TleRecord:
    """Element set saved in tests/data (retrieval time recorded in the file header)."""
    return load_tle_file(DATA_DIRECTORY / file_name)


@pytest.fixture(autouse=True)
def isolate_private_stations(tmp_path, monkeypatch):
    """Point the private station file to an empty temp path so a developer's own file never affects tests."""
    monkeypatch.setenv("PASSPLAN_STATIONS", str(tmp_path / "no_private_stations.json"))
