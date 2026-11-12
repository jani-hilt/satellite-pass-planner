"""The Heavens-Above comparison link (no network, no real browser)."""

from __future__ import annotations

import json
import urllib.parse

import passplan.heavens_above as heavens_above_module
from passplan.heavens_above import main, pass_summary_url
from passplan.passes import GROUND_STATIONS


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_url_uses_the_exact_station_coordinates():
    """The link computes for the same place as the predictor.

    Reference values: the Turku station in passplan/passes.py and the parameter
    names of Heavens-Above's own PassSummary links.
    """
    url = pass_summary_url(GROUND_STATIONS["turku"], 25544)
    assert url.startswith("https://www.heavens-above.com/PassSummary.aspx?")
    assert _query(url) == {
        "satid": "25544",
        "lat": "60.4517221",
        "lng": "22.2668467",
        "loc": "Turku (Kauppatori market square)",
        "alt": "10",
        "tz": "UCT",
        "showall": "t",
    }


def test_private_station_opens_in_chosen_browser_without_printing(tmp_path, monkeypatch, capsys):
    """A private station's coordinates go only to the browser, under a neutral label.

    Reference values: the made-up test coordinates written to the temporary file.
    """
    path = tmp_path / "stations.json"
    stations = {"home": {"name": "Test home", "latitude_degrees": 59.9, "longitude_degrees": 21.0}}
    path.write_text(json.dumps({"stations": stations}))
    monkeypatch.setenv("PASSPLAN_STATIONS", str(path))
    opened = []

    class FakeBrowser:
        def open(self, url):
            opened.append(url)

    monkeypatch.setattr(heavens_above_module.webbrowser, "get", lambda name=None: opened.append(name) or FakeBrowser())
    assert main(["--station", "home", "--norad", "46984", "--browser", "chrome"]) == 0
    browser_name, url = opened
    assert browser_name == "chrome"
    query = _query(url)
    assert (query["lat"], query["lng"], query["loc"], query["satid"]) == (
        "59.9000000",
        "21.0000000",
        "Private station",
        "46984",
    )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
