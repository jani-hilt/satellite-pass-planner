"""Sky plot of one pass: polar chart with azimuth as angle and 90 deg - elevation as radius."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from skyfield.api import EarthSatellite
from skyfield.timelib import Timescale

from passplan.passes import GroundStation, Pass, look_angles, sample_times

LOCAL_TIMEZONE = ZoneInfo("Europe/Helsinki")
TRACK_MARGIN = timedelta(minutes=3)


def plot_pass(
    satellite: EarthSatellite,
    station: GroundStation,
    selected_pass: Pass,
    min_elevation_degrees: float,
    timescale: Timescale,
    output_path: Path,
    step_seconds: float = 5.0,
) -> None:
    """Draw the pass on a sky chart and save it as PNG.

    North is up and east is to the right (as on a map seen from above; a
    sky chart held overhead would have east on the left). The zenith is the
    centre, the horizon the outer edge, and the dashed circle the mask.

    Args:
        satellite: SGP4 satellite.
        station: Observer.
        selected_pass: The pass to draw.
        min_elevation_degrees: Mask drawn as a circle, degrees.
        timescale: Skyfield timescale.
        output_path: PNG file to write.
        step_seconds: Track sampling interval, seconds.
    """
    track_start = selected_pass.aos.time_utc - TRACK_MARGIN
    track_end = selected_pass.los.time_utc + TRACK_MARGIN
    times = sample_times(track_start, track_end, step_seconds, timescale)
    elevation_degrees, azimuth_degrees = look_angles(satellite, station, times)

    above_horizon = elevation_degrees >= 0.0
    above_mask = elevation_degrees >= min_elevation_degrees
    azimuth_radians = np.radians(azimuth_degrees)
    radius_degrees = 90.0 - elevation_degrees

    figure, axes = plt.subplots(figsize=(6.5, 7.6), subplot_kw={"projection": "polar"})
    axes.set_theta_zero_location("N")
    axes.set_theta_direction(-1)
    axes.set_rlim(0.0, 90.0)
    axes.set_rticks([0, 30, 60, 90])
    axes.set_yticklabels(["90°", "60°", "30°", "0°"])
    axes.set_rlabel_position(22.5)
    axes.set_xticks(np.radians([0, 90, 180, 270]))
    axes.set_xticklabels(["N", "E", "S", "W"])

    mask_angles = np.linspace(0.0, 2.0 * np.pi, 361)
    axes.plot(
        mask_angles,
        np.full_like(mask_angles, 90.0 - min_elevation_degrees),
        linestyle="--",
        color="0.45",
        linewidth=1.0,
        label=f"Elevation mask {min_elevation_degrees:g}°",
    )
    axes.plot(
        np.where(above_horizon & ~above_mask, azimuth_radians, np.nan),
        radius_degrees,
        color="tab:blue",
        linewidth=1.0,
        alpha=0.5,
    )
    axes.plot(
        np.where(above_mask, azimuth_radians, np.nan),
        radius_degrees,
        color="tab:blue",
        linewidth=2.2,
        label="Track above mask",
    )

    def local_time_label(point_name: str, time_utc) -> str:
        local_time = time_utc.astimezone(LOCAL_TIMEZONE)
        return f"{point_name} {time_utc:%H:%M:%S} UTC ({local_time:%H:%M:%S} {local_time:%Z})"

    markers = [
        ("AOS", selected_pass.aos, "o", "tab:green"),
        ("Max", selected_pass.culmination, "^", "tab:orange"),
        ("LOS", selected_pass.los, "s", "tab:red"),
    ]
    for point_name, point, marker, color in markers:
        axes.plot(
            np.radians(point.azimuth_degrees),
            90.0 - point.elevation_degrees,
            marker=marker,
            markersize=9,
            color=color,
            linestyle="none",
            label=local_time_label(point_name, point.time_utc)
            + (f", el {point.elevation_degrees:.1f}°" if point_name == "Max" else ""),
        )

    date_text = f"{selected_pass.culmination.time_utc:%Y-%m-%d}"
    axes.set_title(
        f"{satellite.name} over {station.name}\n{date_text}  (radius = 90° − elevation)", pad=22, fontsize=11
    )
    axes.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), fontsize=8.5, frameon=False)
    figure.subplots_adjust(top=0.84, bottom=0.24)
    figure.savefig(output_path, dpi=150)
    plt.close(figure)
