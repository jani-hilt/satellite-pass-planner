"""Loading orbital element sets: OMM (JSON) from CelesTrak, TLE or OMM from a local file.

A TLE and an OMM carry the same SGP4 mean elements; only the container
differs. The TLE's fixed columns hold catalog numbers up to 99999, and
CelesTrak reports that objects catalogued from 2026-07-11 onwards have
6-digit numbers and are distributed only in OMM-capable formats (CelesTrak
front page, read 2026-11-05). Downloads therefore use ``FORMAT=JSON``.

CelesTrak usage rules followed here (read from celestrak.org on 2026-10-31,
pages "A New Way to Obtain GP Data" / gp-data-formats.php and usage-policy.php):

* The GP query format is ``https://celestrak.org/NORAD/elements/gp.php?CATNR=<n>&FORMAT=<format>``.
* GP data is updated once every 2 hours, so a cached copy younger than
  2 hours is never re-downloaded.
* Any response other than HTTP 200 must stop the process and be reported
  to a human; repeating a failed request is not allowed.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sgp4.api import Satrec
from sgp4.conveniences import sat_epoch_datetime
from skyfield.api import EarthSatellite
from skyfield.timelib import Timescale

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=JSON"
# "NAME: Satellite Name. Allows searching for satellites by parts of their name."
# (CelesTrak gp-data-formats.php, read 2026-10-31). Case handling is not
# documented; catalogue names are upper case, so the query is upper-cased.
CELESTRAK_NAME_URL = "https://celestrak.org/NORAD/elements/gp.php?NAME={name}&FORMAT=JSON"
# A query without a match returns HTTP 404 with this body (observed 2026-11-05
# for both CATNR and NAME queries, TLE and JSON formats).
NO_GP_DATA_TEXT = "No GP data found"
CACHE_MAX_AGE = timedelta(hours=2)
HTTP_TIMEOUT_SECONDS = 60
USER_AGENT = "passplan/0.1 (small pass-prediction learning project)"
# Fields read by sgp4's omm.initialize (sgp4 2.26, sgp4/omm.py), which
# Skyfield's EarthSatellite.from_omm calls.
REQUIRED_OMM_FIELDS = (
    "OBJECT_ID",
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "EPHEMERIS_TYPE",
    "CLASSIFICATION_TYPE",
    "NORAD_CAT_ID",
    "ELEMENT_SET_NO",
    "REV_AT_EPOCH",
    "BSTAR",
    "MEAN_MOTION_DOT",
    "MEAN_MOTION_DDOT",
)


class TleError(RuntimeError):
    """Raised when an element set (TLE or OMM) cannot be obtained or parsed."""


class NoGpDataError(TleError):
    """CelesTrak answered that it has no GP data for the query."""


@dataclass(frozen=True)
class TleRecord:
    """One two-line element set as text, plus where and when it was obtained.

    Attributes:
        name: Satellite name from line 0 (empty string if the source had none).
        line1: TLE line 1 (69 characters).
        line2: TLE line 2 (69 characters).
        source: Human-readable origin, e.g. a URL or file path.
        retrieved_utc: When the TLE was downloaded, or None for a local file.
    """

    name: str
    line1: str
    line2: str
    source: str
    retrieved_utc: datetime | None = None
    format_name: str = "TLE"

    @property
    def norad_id(self) -> int:
        """NORAD catalog number from columns 3-7 of line 1."""
        return int(self.line1[2:7])

    @property
    def epoch_utc(self) -> datetime:
        """Epoch decoded by sgp4 (handles the two-digit year)."""
        return sat_epoch_datetime(Satrec.twoline2rv(self.line1, self.line2))

    def to_satellite(self, timescale: Timescale) -> EarthSatellite:
        """Build a Skyfield satellite object (SGP4 propagator) from this TLE."""
        return EarthSatellite(self.line1, self.line2, self.name or None, timescale)


@dataclass(frozen=True)
class OmmRecord:
    """One element set in CCSDS OMM key-value form (CelesTrak JSON), plus its origin.

    Attributes:
        fields: OMM keys and values as CelesTrak's JSON gives them.
        source: Human-readable origin, e.g. a URL or file path.
        retrieved_utc: When it was downloaded, or None for a local file.
    """

    fields: dict[str, Any] = field(hash=False)
    source: str
    retrieved_utc: datetime | None = None
    format_name: str = "OMM"

    @property
    def name(self) -> str:
        return str(self.fields.get("OBJECT_NAME", "")).strip()

    @property
    def norad_id(self) -> int:
        return int(self.fields["NORAD_CAT_ID"])

    @property
    def epoch_utc(self) -> datetime:
        """EPOCH field (ISO 8601, UTC without an offset)."""
        return datetime.fromisoformat(str(self.fields["EPOCH"])).replace(tzinfo=UTC)

    def to_satellite(self, timescale: Timescale) -> EarthSatellite:
        """Build a Skyfield satellite object (SGP4 propagator) from the OMM fields.

        Raises:
            TleError: If sgp4 rejects a field. sgp4 stores the catalog number in
                Alpha-5 form, which ends at 339999 (sgp4 2.26, sgp4/alpha5.py), and
                parses EPOCH as ``%Y-%m-%dT%H:%M:%S.%f`` (sgp4/omm.py).
        """
        try:
            return EarthSatellite.from_omm(timescale, self.fields)
        except ValueError as error:
            raise TleError(f"Cannot use the OMM element set from {self.source}: {error}") from error


ElementSet = TleRecord | OmmRecord


def tle_checksum_is_valid(line: str) -> bool:
    """Check the modulo-10 checksum in column 69 of a TLE line.

    The checksum is the sum of all digits in columns 1-68, with each minus
    sign counting as 1, modulo 10 (CelesTrak "NORAD Two-Line Element Set
    Format" documentation [unverified: rule recalled, not re-read]).
    """
    if len(line) < 69 or not line[68].isdigit():
        return False
    total = sum(int(character) for character in line[:68] if character.isdigit())
    total += line[:68].count("-")
    return total % 10 == int(line[68])


def parse_tle_text(text: str, source: str, retrieved_utc: datetime | None = None) -> list[TleRecord]:
    """Parse text containing one or more 2-line or 3-line element sets.

    Args:
        text: File or HTTP response contents.
        source: Description of where the text came from (stored in each record).
        retrieved_utc: Download time, if the text came from the network.

    Returns:
        Element sets in the order they appear.

    Raises:
        TleError: If no valid element set is found or a checksum fails.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    records: list[TleRecord] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("1 ") and index + 1 < len(lines) and lines[index + 1].startswith("2 "):
            name, line1, line2 = "", line, lines[index + 1]
            index += 2
        elif index + 2 < len(lines) and lines[index + 1].startswith("1 ") and lines[index + 2].startswith("2 "):
            name, line1, line2 = line.strip(), lines[index + 1], lines[index + 2]
            index += 3
        else:
            index += 1
            continue
        for tle_line in (line1, line2):
            if not tle_checksum_is_valid(tle_line):
                raise TleError(f"TLE checksum failed in {source}: {tle_line!r}")
        records.append(TleRecord(name, line1, line2, source, retrieved_utc))
    if not records:
        raise TleError(f"No two-line element set found in {source}")
    return records


def parse_omm_json(text: str, source: str, retrieved_utc: datetime | None = None) -> list[OmmRecord]:
    """Parse CelesTrak GP JSON: a list of OMM objects (a single object is also accepted).

    Raises:
        TleError: If the text is not JSON, is empty, or an object lacks a field SGP4 needs.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise TleError(f"{source} is not valid JSON: {error}") from error
    objects = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(objects, list) or not objects:
        raise TleError(f"No OMM element set found in {source}")
    records = []
    for fields in objects:
        missing = [key for key in REQUIRED_OMM_FIELDS if key not in fields]
        if missing:
            raise TleError(f"OMM element set in {source} lacks {', '.join(missing)}")
        records.append(OmmRecord(dict(fields), source, retrieved_utc))
    return records


def parse_element_text(text: str, source: str, retrieved_utc: datetime | None = None) -> list[ElementSet]:
    """Parse OMM JSON (text starting with '[' or '{') or TLE text."""
    if text.lstrip().startswith(("[", "{")):
        return list(parse_omm_json(text, source, retrieved_utc))
    return list(parse_tle_text(text, source, retrieved_utc))


def load_tle_file(path: Path, norad_id: int | None = None) -> ElementSet:
    """Read an element set from a local TLE file or CelesTrak OMM JSON file.

    Args:
        path: File with one or more 2-/3-line TLEs, or a JSON list of OMM objects.
        norad_id: Pick the set with this catalog number; if None, the first set.

    Raises:
        TleError: If the file has no matching element set.
    """
    records = parse_element_text(path.read_text(), source=str(path))
    if norad_id is None:
        return records[0]
    for record in records:
        if record.norad_id == norad_id:
            return record
    raise TleError(f"NORAD {norad_id} not found in {path}")


def _cache_path(cache_directory: Path, norad_id: int) -> Path:
    return cache_directory / f"{norad_id}.json"


def _read_cache(cache_file: Path) -> ElementSet | None:
    """Cached element set; files written before the OMM change hold ``tle_text`` and are still read."""
    if not cache_file.exists():
        return None
    cached = json.loads(cache_file.read_text())
    retrieved_utc = datetime.fromisoformat(cached["retrieved_utc"])
    if "omm" in cached:
        return OmmRecord(cached["omm"], cached["source"], retrieved_utc)
    return parse_tle_text(cached["tle_text"], cached["source"], retrieved_utc)[0]


def download_gp_json(norad_id: int) -> str:
    """Download one element set from CelesTrak as OMM JSON.

    Raises:
        NoGpDataError: If CelesTrak has no data for this number.
        TleError: On any other non-200 response or network failure. The
            request is not retried, following CelesTrak's usage policy.
    """
    return _download(CELESTRAK_GP_URL.format(norad_id=norad_id))


def download_name_search_json(name: str) -> str:
    """Download all element sets whose name contains ``name`` (CelesTrak NAME query, OMM JSON).

    Raises:
        NoGpDataError: If no name matches.
        TleError: On any other non-200 response or network failure (not retried).
    """
    return _download(_name_url(name))


def _name_url(name: str) -> str:
    return CELESTRAK_NAME_URL.format(name=urllib.parse.quote(name.strip().upper()))


def _download(url: str) -> str:
    """GET ``url`` once; any failure raises TleError and is never retried (CelesTrak policy)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()[:300]
        if error.code == 404 and NO_GP_DATA_TEXT in detail:
            raise NoGpDataError(f"CelesTrak has no GP data for {url}") from error
        raise TleError(
            f"CelesTrak returned HTTP {error.code} for {url}: {detail!r}. "
            "Not retrying (CelesTrak usage policy); wait 2 hours before trying again."
        ) from error
    except urllib.error.URLError as error:
        raise TleError(f"Could not reach CelesTrak ({url}): {error.reason}") from error
    except TimeoutError as error:
        # A timeout while the response is being read is not wrapped in URLError.
        raise TleError(
            f"CelesTrak did not answer within {HTTP_TIMEOUT_SECONDS} s ({url}). "
            "Not retrying (CelesTrak usage policy); try again later."
        ) from error
    except (OSError, http.client.HTTPException) as error:
        raise TleError(f"Connection to CelesTrak failed ({url}): {error!r}") from error
    if status != 200:
        raise TleError(f"CelesTrak returned HTTP {status} for {url}; not retrying.")
    return body


def _write_cache(cache_file: Path, url: str, retrieved_utc: datetime, content: dict[str, Any]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"source": url, "retrieved_utc": retrieved_utc.isoformat(), **content}, indent=2))


def fetch_elements(norad_id: int, cache_directory: Path, now_utc: datetime | None = None) -> ElementSet:
    """Return CelesTrak's current element set for a satellite, using a local cache.

    The cache is used when it is younger than ``CACHE_MAX_AGE`` (2 hours,
    CelesTrak's update interval). If a download fails and an older cached
    copy exists, the old copy is returned and the caller can warn about it.

    Args:
        norad_id: NORAD catalog number (5 or 6 digits).
        cache_directory: Directory for ``<norad_id>.json`` cache files.
        now_utc: Current time (injectable for tests); defaults to the clock.

    Raises:
        TleError: If nothing can be downloaded and nothing is cached.
    """
    if norad_id < 1:
        raise TleError("NORAD catalog numbers start at 1.")
    now_utc = now_utc or datetime.now(UTC)
    cache_file = _cache_path(cache_directory, norad_id)
    cached = _read_cache(cache_file)
    if cached is not None and now_utc - cached.retrieved_utc < CACHE_MAX_AGE:
        return cached
    url = CELESTRAK_GP_URL.format(norad_id=norad_id)
    try:
        text = download_gp_json(norad_id)
    except NoGpDataError as error:
        if cached is not None:
            return cached
        raise NoGpDataError(f"CelesTrak has no GP data for NORAD {norad_id}.") from error
    except TleError:
        if cached is not None:
            return cached
        raise
    record = parse_omm_json(text, source=url, retrieved_utc=now_utc)[0]
    _write_cache(cache_file, url, now_utc, {"omm": record.fields})
    return record


def search_by_name(name: str, cache_directory: Path, now_utc: datetime | None = None) -> list[OmmRecord]:
    """Find satellites whose name contains ``name`` via CelesTrak, with the same 2-hour cache.

    Every match is also stored in its per-NORAD cache file, so a following
    ``--norad`` run uses it without another download.

    Args:
        name: Part of the satellite name, e.g. "COSMOS 1833" or "ICEYE-X84".
        cache_directory: Directory for cache files.
        now_utc: Current time (injectable for tests); defaults to the clock.

    Returns:
        Matching element sets (empty if none).

    If the download fails and an older cached search exists, the old result
    is returned (as ``fetch_elements`` does); its ``retrieved_utc`` tells the
    caller how old it is.

    Raises:
        TleError: If the search cannot be downloaded and is not cached.
    """
    now_utc = now_utc or datetime.now(UTC)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name.strip().upper()).strip("_") or "empty"
    cache_file = cache_directory / f"name_{slug}.json"
    url = _name_url(name)
    cached_records: list[OmmRecord] | None = None
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if "omm_list" in cached:
            retrieved_utc = datetime.fromisoformat(cached["retrieved_utc"])
            cached_records = [OmmRecord(fields, url, retrieved_utc) for fields in cached["omm_list"]]
            if now_utc - retrieved_utc < CACHE_MAX_AGE:
                return cached_records
    try:
        records = parse_omm_json(download_name_search_json(name), source=url, retrieved_utc=now_utc)
    except NoGpDataError:
        records = []
    except TleError:
        if cached_records is not None:
            return cached_records
        raise
    _write_cache(cache_file, url, now_utc, {"omm_list": [record.fields for record in records]})
    for record in records:
        _write_cache(_cache_path(cache_directory, record.norad_id), url, now_utc, {"omm": record.fields})
    return records
