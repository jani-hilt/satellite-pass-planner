# Verification

Part of [satellite-pass-planner](../README.md).

The comparison values in the tests never come from the code being tested. All tests run
offline on frozen element sets in `tests/data/`: TLEs retrieved from CelesTrak on
2026-10-31, used over the fixed window 2026-11-01 00:00 UTC + 2 days, and OMM JSON files
retrieved on 2026-11-05. Each test's docstring says where its
comparison value comes from.

## Second method

`tests/reference_geometry.py` reimplements everything after SGP4 in plain NumPy:

1. rotation from TEME to Earth-fixed using IAU-82 GMST
2. WGS84 station position
3. projection onto east/north/up
4. a brute-force scan of elevation every 1 s, with linear interpolation of the mask
   crossings and a parabola through the samples around the maximum

The results are compared with Skyfield's `find_events` for the ISS and Sentinel-2B over
both stations. Largest difference found, and the test tolerance:

- **Elevation** (10 s grid, 2 days, above horizon)
  - Found: 0.0017°. Tolerance: 0.01°.
- **Azimuth** (same, below 80° elevation)
  - Found: 0.009°. Tolerance: 0.05°.
- **AOS / LOS time**
  - Found: 0.25 s. Tolerance: 1 s.
- **Maximum-elevation time**
  - Found: 0.11 s. Tolerance: 1 s.
- **Maximum elevation**
  - Found: 0.0017°. Tolerance: 0.01°.

Why 1 s is the right tolerance for the times:

- Skyfield refines events to 0.5 s (`half_second` in `skyfield/sgp4lib.py`).
- The reference deliberately uses UTC instead of UT1 for Earth rotation. In November
  2026 UT1 − UTC is 0.09 s.
- Interpolation on a 1 s grid adds only milliseconds.

## Physical conditions

Checked for every complete pass:

- At AOS and LOS the elevation equals the mask, within the elevation rate × 0.5 s (the
  search tolerance).
- The maximum elevation is between the mask and 90°.
- AOS < maximum < LOS.
- The duration is at most T_max = 2λ / (n − ω_E).
  - λ is the Earth-central angle of the visibility circle, from the
    station–satellite–Earth-centre triangle at apogee height; n is the mean motion and
    ω_E the Earth's rotation rate. The bound is derived step by step in
    `docs/pass_prediction_explained_fi.pdf`.
  - With a 10° mask it gives 6.95 min for the ISS and 11.3 min for Sentinel-2B. The
    longest predicted passes are 5 min 03 s and 10 min 28 s.
- Passes on consecutive revolutions repeat after the orbital period computed from the
  TLE mean motion (1440 / n min), to within T_max. Measured: 1.032 P for the ISS and
  0.97–0.99 P for Sentinel-2B.

## Edge cases

- A window opening or closing at a pass maximum gives a correctly flagged, clipped pass.
- A window edge on a whole second is printed as exactly that second (60 starts and
  60 ends tried). Times are rounded to 1 ms because one float64 Julian date resolves
  only about 40 µs.
- ISS with a 30° mask from Helsinki gives no passes; the reference scan confirms the
  peak is below 30°.
- Meteosat-11 (geostationary, 17–24° above Helsinki) is visible for the whole window.
  With a 20° mask, its 3.5° inclination splits the visibility into daily passes, which
  match the reference within 5 s.
- GOES-18 (geostationary over the Pacific, −35°) never rises.

## External comparisons

These comparisons are not part of the automated tests, except that two visibility tests
use Heavens-Above values from 2026-10-31 as reference values (see "Optical visibility").
Between 2026-11-02 and 2026-11-12 I compared the tool's output by hand with two other
programs:

- [Heavens-Above](https://www.heavens-above.com/), a satellite-tracking website
- SkySafari 7 Pro, a planetarium app for Android

Four satellites on different orbits:

- ISS, about 420 km, inclination 51.6°
- Cosmos 1833, about 850 km, inclination 70.9°
- NOAA 20, sun-synchronous, about 830 km, inclination 98.8°
- Sentinel-6A, about 1340 km, inclination 66.0°

Three places:

- Helsinki and Turku, the tool's built-in stations
- one private place in Turku, whose times are not published because they would
  reveal the place

Result:

- Pass times agreed to the second, or differed by one printed second.
- Against SkySafari, directions and peak elevations agreed to within 0.02°, about the
  precision of SkySafari's own display. Heavens-Above gives whole degrees, and these
  agreed after rounding.
- The comparisons found one bug, described below, which is now fixed.

The first comparison, on 2026-10-31, was made by the AI agent, which fetched the
Heavens-Above pages itself. It is described in the last subsection.

How to compare:

- **Same place.** The other program must use the same coordinates and height.
  `python -m passplan.heavens_above` opens Heavens-Above with the station's exact
  coordinates (see "Comparing with other programs" in the [README](../README.md)).
- **Same horizon.** Heavens-Above gives rise and set at the geometric horizon
  (`--min-elevation 0`). SkySafari gives them at a geometric elevation of −34′, the
  usual astronomical convention that allows for refraction (`--min-elevation -0.57`).
  At 0° a low Sentinel-6A pass rose 17 s later and set 17 s earlier in this tool than
  in SkySafari; at −0.57° the times matched to the second.
- **Fresh element sets in both.** SkySafari updates its orbits only from its own update
  button. Heavens-Above downloads its own copy, so the epochs can differ.

### 2026-11-12: three satellites, two public places, Heavens-Above

Each event gives this tool / Heavens-Above, UTC, with `--min-elevation 0`.
Heavens-Above gives elevation in whole degrees. Element set epochs (UTC, 2026-11-11):
ISS 08:03:57, NOAA 20 12:40:25, Sentinel-6A 15:34:33.

- **ISS (25544), Helsinki**
  - Rise: 19:58:54 / 19:58:54
  - Max: 20:03:51 / 20:03:51, elevation 19.4° / 19°
  - Set: 20:08:48 / 20:08:48
- **ISS (25544), Turku**
  - Rise: 19:58:35 / 19:58:35
  - Max: 20:03:29 / 20:03:29, elevation 18.6° / 19°
  - Set: 20:08:25 / 20:08:25
- **NOAA 20 (43013), Helsinki**
  - Rise: 00:56:27 / 00:56:27
  - Max: 01:04:14 / 01:04:14, elevation 80.4° / 80°
  - Set: 01:11:58 / 01:11:58
- **NOAA 20 (43013), Turku**
  - Rise: 00:56:29 / 00:56:29
  - Max: 01:04:17 / 01:04:17, elevation 88.0° / 88°
  - Set: 01:12:02 / 01:12:02
- **Sentinel-6A (46984), Helsinki**
  - Rise: 06:44:20 / 06:44:20
  - Max: 06:52:09 / 06:52:08, elevation 11.5° / 11°
  - Set: 06:59:57 / 06:59:57
- **Sentinel-6A (46984), Turku**
  - Rise: 08:35:23 / 08:35:23
  - Max: 08:45:39 / 08:45:39, elevation 35.4° / 35°
  - Set: 08:55:58 / 08:55:58

17 of the 18 times are identical and one differs by 1 s. All elevations, and the rise
and set directions, agree after rounding to whole degrees. The direction at the 88°
NOAA 20 peak does not (109.2° / 108°): so close to the zenith the azimuth swings about
15° within ±0.5 s of the peak, so it cannot be compared closely.

NOAA 20 entered the Earth's shadow at 01:00:52 according to Heavens-Above. The last
visible second from `--visibility` (sampled every 1 s) was 01:00:52 in Turku and
01:00:51 in Helsinki.

### 2026-11-12: SkySafari, all three places

SkySafari was compared with `--min-elevation -0.57` for nine passes: ISS, NOAA 20 and
Sentinel-6A from all three places.

Rise, peak and set agreed to the second or within 1 s. I converted SkySafari's
degrees, minutes and seconds to decimal degrees for eight of these passes and compared
them with the tool's values to three decimals. Directions and peak elevations differed
by at most 0.020° (72″). SkySafari's own readings drifted by a few tens of arcseconds
on screen, so the comparison cannot go finer than that.

### Private place in Turku

Times are not published. The results:

- **ISS, 2026-11-12:**
  - Heavens-Above: rise, peak and set identical to the second.
  - SkySafari (`--min-elevation -0.57`): peak and set identical, rise 1 s apart.
    Directions and peak elevation within 0.011°.
- **NOAA 20, 2026-11-12:**
  - Heavens-Above: rise, peak and set identical to the second.
  - SkySafari (`--min-elevation -0.57`), another pass: rise, peak and set identical to
    the second. Directions and peak elevation within 0.020°.
- **Sentinel-6A, 2026-11-12:**
  - SkySafari: rise, peak and set identical to the second with
    `--min-elevation -0.57`; peak elevation 9.67° in both. At 0° this low pass rose 17 s
    later and set 17 s earlier than in SkySafari (see "Same horizon" above).
  - Heavens-Above, another pass: rise, peak and set identical to the second. Its shadow
    entry is the same second as the tool's last visible second.
- **Cosmos 1833 (17589), 2026-11-02:**
  - Heavens-Above: rise and peak identical to the second, peak elevation 87°.
  - SkySafari: peak time identical, peak elevation within 0.1°.
  - Heavens-Above's shadow entry was 1 s after the tool's last visible second.

**Bug found in this comparison.** When the window started in the middle of a pass, the
clipped AOS was printed one second before the window start. A whole-second start such as
17:40:54 went through one float64 Julian date, which resolves about 40 µs. It came back
as 17:40:53.999994 and was truncated to 17:40:53. Times are now rounded to 1 ms first,
and a regression test tries 60 whole-second window starts and 60 ends (see Edge cases).

### 2026-10-31: ISS, all passes over two days, Heavens-Above (by the AI agent)

The AI agent fetched the Heavens-Above "all passes" list for the ISS at
60.1527 N, 24.9254 E, 10 m, for 2026-11-01 to 2026-11-02
(`heavens-above.com/PassSummary.aspx`, times in UTC), and compared it with the output.
I did not repeat this comparison by hand. This list starts and ends passes
at 10°, according to its pass-details page. This site is kept in the tests as
`HEAVENS_ABOVE_SITE`, so these comparisons stay reproducible.

Heavens-Above's orbit page shows an element set with the same epoch as ours:
2026-10-31 10:25:39 UTC, epoch field 26304.43448434 vs our 26304.43448470. Its ω and M
differ slightly from ours, but their sum is the same to 0.0004°.

Each pass below gives our time, the Heavens-Above time and the difference (UTC):

- **11-01, max el 19.6° (H-A 20°)**
  - AOS: 00:42:50.6 / 00:42:50 / +0.6 s
  - Max: 00:45:22.2 / 00:45:22 / +0.2 s
  - LOS: 00:47:54.1 / 00:47:53 / +1.1 s
- **11-01, max el 13.0° (H-A 13°)**
  - AOS: 02:19:40.1 / 02:19:40 / +0.2 s
  - Max: 02:21:19.5 / 02:21:19 / +0.5 s
  - LOS: 02:22:59.0 / 02:22:58 / +1.0 s
- **11-01, max el 13.4° (H-A 13°)**
  - AOS: 22:20:17.3 / 22:20:17 / +0.3 s
  - Max: 22:22:01.1 / 22:22:01 / +0.1 s
  - LOS: 22:23:45.5 / 22:23:45 / +0.6 s
- **11-01, max el 19.6° (H-A 20°)**
  - AOS: 23:55:27.1 / 23:55:27 / +0.2 s
  - Max: 23:57:58.8 / 23:57:59 / −0.2 s
  - LOS: 00:00:30.9 / 00:00:30 / +0.9 s
- **11-02, max el 15.3° (H-A 15°)**
  - AOS: 01:31:54.8 / 01:31:55 / −0.2 s
  - Max: 01:33:58.8 / 01:33:58 / +0.9 s
  - LOS: 01:36:03.1 / 01:36:03 / +0.2 s
- **11-02, max el 11.0° (H-A 11°)**
  - AOS: 21:33:41.3 / 21:33:41 / +0.3 s
  - Max: 21:34:41.3 / 21:34:41 / +0.3 s
  - LOS: 21:35:41.5 / 21:35:41 / +0.5 s
- **11-02, max el 18.9° (H-A 19°)**
  - AOS: 23:08:06.9 / 23:08:07 / −0.0 s
  - Max: 23:10:34.8 / 23:10:35 / −0.1 s
  - LOS: 23:13:03.3 / 23:13:03 / +0.4 s

All 7 passes match. All 21 event times agree within 1.1 s, with a mean of +0.4 s.
Heavens-Above shows whole seconds; its own details page differs from its summary by
1 s for one pass. So this agreement is at the limit of what the comparison can resolve.
The agent also tried N2YO, but its pass table did not load without JavaScript.

## Optical visibility (`--visibility`)

A pass is marked visible when the satellite is sunlit and the Sun is more than 6° below
the horizon at the same moment.

- Sunlight comes from Skyfield's `is_sunlit()`, which treats the Earth as a sphere and
  has no penumbra or atmosphere.
- The −6° threshold (end of civil twilight) is my choice, not a standard.
- Only the part of the pass above the elevation mask is checked, so use
  `--min-elevation 0` for visual observing.
- Radio contact does not depend on sunlight, so this flag does not change the pass
  list.

Checks (`tests/test_visibility_and_name.py`):

- **Independent shadow test.**
  - Skyfield's sunlit flag equals a hand-written cylindrical-shadow test (night side
    and within one Earth radius of the Earth–Sun axis).
  - This holds for the ISS and Sentinel-2B every 10 s over two days, except within
    30 s of a shadow boundary.
- **Heavens-Above details page**, fetched by the AI agent on 2026-10-31. The ISS pass of
  2026-11-01 (UTC, test tolerance 2 s):
  - Rises: Heavens-Above 02:16:42, this tool 02:16:42.
  - Exits shadow: Heavens-Above 02:24:45, this tool 02:24:45.
  - Sets: Heavens-Above 02:25:55, this tool 02:25:55.
- **Heavens-Above pass types.** Of the seven passes in the comparison above, only the
  02:19 pass is "visible" and the other six are in shadow, as Heavens-Above lists them.

These ephemeris tests (4 test cases) are skipped, and reported as skipped, if
`.tle_cache/de421.bsp` has not been downloaded. Run the tool once with `--visibility`
to download it.

## What verification cannot show

Every comparison uses the same public element sets and SGP4, so none of them measures
the error of the element set itself relative to the real satellite (see Limitations in
the [README](../README.md)).

```
$ ruff check . && ruff format --check . && pytest
All checks passed!
69 passed
```

The same checks run on GitHub Actions after every push
(`.github/workflows/tests.yml`). There the 4 test cases that need the ephemeris are
skipped, because the 17 MB DE421 file is not downloaded.
