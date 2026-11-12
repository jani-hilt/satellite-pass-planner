"""Built-in stations and the private station file (kept outside the repository)."""

from __future__ import annotations

import json

import pytest

from passplan.cli import main
from passplan.passes import DEFAULT_STATION_KEY, GROUND_STATIONS, load_station_settings
from tests.conftest import DATA_DIRECTORY

# Made-up coordinates for the tests (a point in the Baltic Sea), not anyone's address.
PRIVATE_FILE_CONTENT = {
    "default": "home",
    "stations": {"home": {"name": "Test home", "latitude_degrees": 59.9, "longitude_degrees": 21.0}},
}


def test_without_private_file_only_built_in_stations(tmp_path):
    """A missing private file is not an error and leaves the built-in defaults.

    Reference value: GROUND_STATIONS and DEFAULT_STATION_KEY in passplan/passes.py.
    """
    stations, default_key = load_station_settings(tmp_path / "missing.json")
    assert stations == GROUND_STATIONS
    assert default_key == DEFAULT_STATION_KEY == "helsinki"


def test_private_file_adds_station_and_default(tmp_path):
    """Stations from the private file are added, hidden from output, and can become the default.

    Reference value: the values written to the temporary file; altitude defaults to 0 m.
    """
    path = tmp_path / "stations.json"
    path.write_text(json.dumps(PRIVATE_FILE_CONTENT))
    stations, default_key = load_station_settings(path)
    assert default_key == "home"
    home = stations["home"]
    assert (home.latitude_degrees, home.longitude_degrees, home.altitude_m) == (59.9, 21.0, 0.0)
    assert not home.show_coordinates
    assert set(GROUND_STATIONS) < set(stations)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"stations": {"home": {"name": "missing latitude", "longitude_degrees": 21.0}}}),
        json.dumps({"default": "nowhere"}),
    ],
)
def test_bad_private_file_is_reported(tmp_path, content):
    """Malformed files and an unknown default raise ValueError with the file name.

    Reference value: none needed; the expected behaviour is an exception.
    """
    path = tmp_path / "stations.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="stations.json"):
        load_station_settings(path)


def test_cli_uses_private_default_and_hides_coordinates(tmp_path, monkeypatch, capsys):
    """With a private default station, the CLI uses it and does not print its coordinates.

    Reference value: the made-up coordinates must not appear in the output.
    """
    path = tmp_path / "stations.json"
    path.write_text(json.dumps(PRIVATE_FILE_CONTENT))
    monkeypatch.setenv("PASSPLAN_STATIONS", str(path))
    main(["--tle-file", str(DATA_DIRECTORY / "iss.tle"), "--start", "2026-11-01T00:00:00", "--days", "0.5"])
    output = capsys.readouterr().out
    assert "Test home  (private station, coordinates not shown)" in output
    assert "59.9" not in output and "21.0000" not in output
