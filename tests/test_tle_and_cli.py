"""TLE and OMM parsing, the CelesTrak cache rules, and the command-line output (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import passplan.tle as tle_module
from passplan.cli import main
from passplan.tle import (
    NoGpDataError,
    TleError,
    fetch_elements,
    load_tle_file,
    parse_omm_json,
    parse_tle_text,
    tle_checksum_is_valid,
)
from tests.conftest import DATA_DIRECTORY

ISS_LINE1 = "1 25544U 98067A   26304.43448470  .00006246  00000+0  12087-3 0  9997"
ISS_LINE2 = "2 25544  51.6334 345.8717 0007023 331.3934  28.6669 15.49127356592878"


def test_checksums_of_real_tles_are_valid():
    """Every line of the CelesTrak TLEs in tests/data passes the modulo-10 checksum.

    Reference value: the checksum digit printed by CelesTrak in column 69.
    """
    for path in DATA_DIRECTORY.glob("*.tle"):
        record = load_tle_file(path)
        assert tle_checksum_is_valid(record.line1)
        assert tle_checksum_is_valid(record.line2)


def test_corrupted_digit_is_rejected():
    """Changing one digit of line 2 breaks the checksum and is reported.

    Reference value: by construction, a +1 change in one digit shifts the sum by 1.
    """
    corrupted = ISS_LINE2[:20] + "9" + ISS_LINE2[21:]
    assert ISS_LINE2[20] != "9"
    with pytest.raises(TleError):
        parse_tle_text(f"ISS\n{ISS_LINE1}\n{corrupted}\n", source="test")


def test_two_and_three_line_formats():
    """Both 2-line and 3-line input parse to the same element set.

    Reference value: the NORAD number 25544 in columns 3-7 of line 1.
    """
    two_line = parse_tle_text(f"{ISS_LINE1}\n{ISS_LINE2}\n", "test")[0]
    three_line = parse_tle_text(f"ISS (ZARYA)\n{ISS_LINE1}\n{ISS_LINE2}\n", "test")[0]
    assert two_line.norad_id == three_line.norad_id == 25544
    assert three_line.name == "ISS (ZARYA)" and two_line.name == ""


ISS_OMM_FILE = DATA_DIRECTORY / "iss_2026-11-05.json"
ISS_PAIRED_TLE_FILE = DATA_DIRECTORY / "iss_2026-11-05.tle"


def _write_cache(cache_directory, retrieved_utc, legacy_tle=False):
    """Cache file as fetch_elements writes it (OMM), or as written before the OMM change (TLE text)."""
    cache_directory.mkdir(parents=True, exist_ok=True)
    content = (
        {"tle_text": f"ISS (ZARYA)\n{ISS_LINE1}\n{ISS_LINE2}\n"}
        if legacy_tle
        else {"omm": json.loads(ISS_OMM_FILE.read_text())[0]}
    )
    (cache_directory / "25544.json").write_text(
        json.dumps({"source": "cached", "retrieved_utc": retrieved_utc.isoformat(), **content})
    )


def test_fresh_cache_is_used_without_download(tmp_path, monkeypatch):
    """A cache younger than 2 hours is returned and CelesTrak is not contacted.

    Reference value: CelesTrak's stated 2-hour update interval (gp-data-formats.php).
    """
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    _write_cache(tmp_path, now - timedelta(hours=1, minutes=59))

    def forbidden_download(norad_id):
        raise AssertionError("download attempted although the cache is fresh")

    monkeypatch.setattr(tle_module, "download_gp_json", forbidden_download)
    record = fetch_elements(25544, tmp_path, now)
    assert record.source == "cached" and record.format_name == "OMM"


def test_stale_cache_triggers_one_download(tmp_path, monkeypatch):
    """A cache older than 2 hours is refreshed with exactly one download and rewritten as OMM.

    Reference value: the 2-hour rule; the download is replaced by a stub
    returning the frozen CelesTrak JSON.
    """
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    _write_cache(tmp_path, now - timedelta(hours=2, minutes=1))
    calls = []

    def stub_download(norad_id):
        calls.append(norad_id)
        return ISS_OMM_FILE.read_text()

    monkeypatch.setattr(tle_module, "download_gp_json", stub_download)
    record = fetch_elements(25544, tmp_path, now)
    assert calls == [25544]
    assert record.retrieved_utc == now
    cached = json.loads((tmp_path / "25544.json").read_text())
    assert cached["retrieved_utc"] == now.isoformat()
    assert cached["omm"]["NORAD_CAT_ID"] == 25544


def test_failed_download_falls_back_to_stale_cache(tmp_path, monkeypatch):
    """If CelesTrak fails, an old cached copy is used (the CLI then prints a warning).

    The old copy here is in the pre-OMM cache format, so this also checks
    that such files are still read. Reference value: the cached retrieval
    time, which must be returned unchanged.
    """
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=1)
    _write_cache(tmp_path, old, legacy_tle=True)

    def failing_download(norad_id):
        raise TleError("HTTP 403")

    monkeypatch.setattr(tle_module, "download_gp_json", failing_download)
    record = fetch_elements(25544, tmp_path, now)
    assert record.retrieved_utc == old and record.format_name == "TLE"


def test_failed_download_without_cache_raises(tmp_path, monkeypatch):
    """With no cache and a failed download the error reaches the caller.

    Reference value: none needed; the expected behaviour is an exception.
    """

    def failing_download(norad_id):
        raise NoGpDataError("no data")

    monkeypatch.setattr(tle_module, "download_gp_json", failing_download)
    with pytest.raises(TleError, match="no GP data for NORAD 25544"):
        fetch_elements(25544, tmp_path, datetime(2026, 11, 1, tzinfo=UTC))


def test_read_timeout_becomes_a_clear_error(monkeypatch, capsys):
    """A timeout while reading CelesTrak's response is reported, not a traceback.

    Reproduces the TimeoutError seen with --name "ICEYE-X36" on 2026-11-09,
    which urllib raises bare (not wrapped in URLError). Reference value: none
    needed; the expected behaviour is exit code 1 and a one-line message.
    """

    def slow_server(request, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(tle_module.urllib.request, "urlopen", slow_server)
    with pytest.raises(TleError, match="did not answer within"):
        tle_module.download_name_search_json("ICEYE-X36")
    exit_code = main(["--name", "ICEYE-X36", "--cache-dir", "/nonexistent/passplan-test"])
    assert exit_code == 1
    assert "error: CelesTrak did not answer" in capsys.readouterr().err


def test_omm_and_tle_of_the_same_element_set_agree(timescale):
    """The same ISS element set read as TLE and as OMM JSON gives the same orbit.

    Reference values: the TLE of the pair, retrieved from CelesTrak seconds
    before the JSON (tests/data/SOURCES.md); both carry epoch 2026-11-04
    21:15:15.205 UTC. The two containers round differently: the TLE stores
    the epoch to 1e-8 day (at most 0.43 ms, i.e. 3.3 m along-track at
    7.7 km/s) and BSTAR to 5 digits. 5 m over 2 days covers that; the
    measured difference was 0.54 m.
    """
    from_tle = load_tle_file(ISS_PAIRED_TLE_FILE).to_satellite(timescale)
    from_omm = load_tle_file(ISS_OMM_FILE).to_satellite(timescale)
    assert abs((from_tle.epoch.utc_datetime() - from_omm.epoch.utc_datetime()).total_seconds()) < 1e-3
    times = timescale.tt_jd(from_tle.epoch.tt + np.arange(0.0, 2.0, 1.0 / 144.0))
    difference_m = np.linalg.norm(from_tle.at(times).xyz.km - from_omm.at(times).xyz.km, axis=0) * 1000.0
    assert difference_m.max() < 5.0


def test_omm_without_a_required_field_is_rejected():
    """An OMM object missing a field that SGP4 needs is reported, not silently propagated.

    Reference value: REQUIRED_OMM_FIELDS, the fields sgp4's omm.initialize reads.
    """
    fields = json.loads(ISS_OMM_FILE.read_text())[0]
    del fields["BSTAR"]
    with pytest.raises(TleError, match="BSTAR"):
        parse_omm_json(json.dumps([fields]), source="test")


def test_catalog_number_beyond_alpha5_is_a_clear_error(timescale):
    """A catalog number above 339999 gives a TleError instead of a crash.

    Reference value: sgp4's Alpha-5 limit 339999 (sgp4/alpha5.py, sgp4 2.26).
    """
    fields = json.loads(ISS_OMM_FILE.read_text())[0] | {"NORAD_CAT_ID": 340000}
    (record,) = parse_omm_json(json.dumps([fields]), source="test")
    with pytest.raises(TleError, match="339999"):
        record.to_satellite(timescale)


def test_cli_six_digit_catalog_number_from_omm(capsys):
    """ICEYE-X84 (NORAD 101122) is predicted from OMM although no TLE can hold its number.

    Reference values: the catalog number and name in the frozen CelesTrak
    JSON; its epoch 2026-11-04T21:44:32.246592.
    """
    exit_code = main(
        [
            "--tle-file",
            str(DATA_DIRECTORY / "iceye_x84_2026-11-05.json"),
            "--start",
            "2026-11-05T00:00:00",
            "--days",
            "1",
            "--min-elevation",
            "0",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "ICEYE-X84  NORAD 101122" in output
    assert "Elements       OMM" in output
    assert "Epoch          2026-11-04 21:44:32 UTC" in output
    assert "Pass 1" in output


def test_cli_prints_passes_and_writes_plot(tmp_path, capsys):
    """End-to-end run with a frozen TLE: header, AOS/MAX/LOS rows, local time, plot file.

    Reference values: the TLE epoch 26304.43448470 = 2026-10-31 10:25:39 UTC
    (day 304 of 2026 is 31 October; 0.43448470 d = 10 h 25 min 39 s), and
    Europe/Helsinki = UTC+2 (EET) after the 2026-10-25 change from summer time.
    """
    plot_path = tmp_path / "pass.png"
    exit_code = main(
        [
            "--tle-file",
            str(DATA_DIRECTORY / "iss.tle"),
            "--start",
            "2026-11-01T00:00:00",
            "--days",
            "1",
            "--plot",
            str(plot_path),
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Epoch          2026-10-31 10:25:39 UTC" in output
    assert "AOS  2026-11-01 00:42:5" in output
    assert "(02:42:5" in output and "EET" in output
    assert plot_path.stat().st_size > 10_000


def test_bare_plot_file_name_goes_into_the_plot_directory(tmp_path, monkeypatch, capsys):
    """`--plot pass.png` writes plots/pass.png (folder created if missing).

    Reference value: DEFAULT_PLOT_DIRECTORY in passplan/cli.py. A path with a
    directory is kept as given (checked by the end-to-end test above).
    """
    monkeypatch.chdir(tmp_path)
    arguments = ["--tle-file", str(DATA_DIRECTORY / "iss.tle"), "--start", "2026-11-01T00:00:00"]
    assert main([*arguments, "--days", "0.1", "--plot", "pass.png"]) == 0
    assert (tmp_path / "plots" / "pass.png").stat().st_size > 10_000
    assert not (tmp_path / "pass.png").exists()
    assert "saved to plots/pass.png" in capsys.readouterr().out


def test_cli_warns_about_old_tle(capsys):
    """A window 10 days after the TLE epoch prints the TLE-age warning.

    Reference value: TLE_AGE_WARNING_DAYS = 3 in passplan/cli.py.
    """
    main(["--tle-file", str(DATA_DIRECTORY / "iss.tle"), "--start", "2026-11-10T00:00:00", "--days", "0.5"])
    assert "WARNING: predictions extend more than 3 days" in capsys.readouterr().out


def test_cli_reports_continuous_and_no_visibility(capsys):
    """Geostationary edge cases are reported in words.

    Reference values: Meteosat-11 is 17-24 deg up from Helsinki and GOES-18
    is below the horizon (see test_passes.py).
    """
    main(["--tle-file", str(DATA_DIRECTORY / "meteosat11.tle"), "--start", "2026-11-01T00:00:00"])
    assert "stays above the mask for the whole window" in capsys.readouterr().out
    main(["--tle-file", str(DATA_DIRECTORY / "goes18.tle"), "--start", "2026-11-01T00:00:00"])
    assert "No passes" in capsys.readouterr().out
