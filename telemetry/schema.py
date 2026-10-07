"""Telemetry message schema (version 1) per docs/JETSON_WAN_HANDOFF.md section 7.

Consumes already-measured values only. Does not implement any localization
or sensor-fusion algorithm — heading selection is a straight preference
(GNSS course-over-ground, else Xsens fused heading, else unavailable), not a
fusion filter.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

SCHEMA_VERSION = 1
MESSAGE_TYPE = "vehicle_visualization"
MAX_MESSAGE_BYTES = 65536

HEADING_SOURCE_GNSS = "GNSS_COURSE"
HEADING_SOURCE_IMU = "IMU"
HEADING_SOURCE_UNAVAILABLE = "UNAVAILABLE"

# Septentrio SBF "Do-Not-Use" values (SBF Reference Guide). Until it has a
# solution, the receiver fills PVTGeodetic fields with these instead of
# measurements, and the ROS driver passes them through unchanged. They must
# become null, never a reading: e.g. NrSV 255 would show as "255 satellites",
# HAccuracy 65535 as "±655.35 m", and a COG of -2e10 is finite, so it would
# otherwise win heading selection as a valid GNSS course.
SBF_DNU_U1 = 255  # u1 fields, e.g. NrSV
SBF_DNU_U2 = 65535  # u2 fields, e.g. HAccuracy / VAccuracy (0.01 m units)
SBF_DNU_FLOAT = -2e10  # f4/f8 fields, e.g. Vn, Ve, Vu, COG, Height
# f4 can't hold -2e10 exactly (it arrives as about -19999999488), so treat
# anything at or below this as the float Do-Not-Use value.
_SBF_DNU_FLOAT_THRESHOLD = -1e10


def is_finite(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


def measured_float(value):
    """A float measurement, or None if it is missing, non-finite, or the SBF
    float Do-Not-Use value."""
    if not is_finite(value) or value <= _SBF_DNU_FLOAT_THRESHOLD:
        return None
    return value


def satellite_count(nr_sv):
    """NrSV, or None if missing or the SBF u1 Do-Not-Use value (255)."""
    if nr_sv is None or nr_sv == SBF_DNU_U1:
        return None
    return nr_sv


def compute_horizontal_speed_mps(vn, ve):
    if not (is_finite(vn) and is_finite(ve)):
        return None
    return math.sqrt(vn ** 2 + ve ** 2)


def convert_accuracy_to_meters(raw_hundredths_of_a_meter):
    """PVTGeodetic h_accuracy/v_accuracy are in units of 0.01 m. 65535 is the
    SBF Do-Not-Use value, not 655.35 m."""
    if not is_finite(raw_hundredths_of_a_meter) or raw_hundredths_of_a_meter == SBF_DNU_U2:
        return None
    return raw_hundredths_of_a_meter / 100.0


def select_heading(cog_deg, xsens_heading_deg):
    """Prefer GNSS course-over-ground when valid; otherwise fall back to a
    valid Xsens fused heading; otherwise report unavailable. Never fabricates
    a value."""
    if is_finite(cog_deg):
        return cog_deg, HEADING_SOURCE_GNSS
    if is_finite(xsens_heading_deg):
        return xsens_heading_deg, HEADING_SOURCE_IMU
    return None, HEADING_SOURCE_UNAVAILABLE


def sanitize(value):
    """Recursively replace non-finite floats (NaN/inf) with None so the
    resulting structure is always valid, finite JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


@dataclass
class GnssSample:
    timestamp_utc: str | None
    latitude_deg: float | None
    longitude_deg: float | None
    altitude_m: float | None
    vn_mps: float | None
    ve_mps: float | None
    vu_mps: float | None
    nr_sv: int | None
    h_accuracy_raw: float | None
    v_accuracy_raw: float | None
    cog_deg: float | None
    fix_valid: bool


def build_gnss_block(gnss: GnssSample, xsens_heading_deg: float | None) -> dict:
    speed = compute_horizontal_speed_mps(measured_float(gnss.vn_mps), measured_float(gnss.ve_mps))
    heading_deg, heading_source = select_heading(measured_float(gnss.cog_deg), xsens_heading_deg)
    return {
        "timestamp_utc": gnss.timestamp_utc,
        "latitude_deg": gnss.latitude_deg,
        "longitude_deg": gnss.longitude_deg,
        "altitude_m": measured_float(gnss.altitude_m),
        "horizontal_speed_mps": speed,
        "heading_deg": heading_deg,
        "heading_source": heading_source,
        "satellite_count": satellite_count(gnss.nr_sv),
        "horizontal_accuracy_m": convert_accuracy_to_meters(gnss.h_accuracy_raw),
        "vertical_accuracy_m": convert_accuracy_to_meters(gnss.v_accuracy_raw),
        "fix_valid": gnss.fix_valid,
    }


def build_camera_health_block(
    stream_path: str,
    healthy: bool,
    fps: float | None = None,
    last_frame_age_s: float | None = None,
) -> dict:
    return {
        "stream_path": stream_path,
        "healthy": healthy,
        "fps": fps,
        "last_frame_age_s": last_frame_age_s,
    }


def build_system_block(
    uptime_s: float | None,
    cpu_percent: float | None,
    memory_percent: float | None,
    temperature_c: float | None,
    telemetry_rate_hz: float | None,
) -> dict:
    return {
        "uptime_s": uptime_s,
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "temperature_c": temperature_c,
        "telemetry_rate_hz": telemetry_rate_hz,
    }


def build_telemetry_message(
    sequence: int,
    timestamp_utc: str,
    timestamp_monotonic_s: float,
    gnss: GnssSample | None = None,
    xsens_heading_deg: float | None = None,
    cameras: dict | None = None,
    system_info: dict | None = None,
    source_id: str = "jetson_onboard",
) -> dict:
    message: dict = {
        "schema_version": SCHEMA_VERSION,
        "message_type": MESSAGE_TYPE,
        "source_id": source_id,
        "sequence": sequence,
        "timestamp_utc": timestamp_utc,
        "timestamp_monotonic_s": timestamp_monotonic_s,
    }
    if gnss is not None:
        message["gnss"] = build_gnss_block(gnss, xsens_heading_deg)
    if cameras:
        message["cameras"] = cameras
    if system_info:
        message["system"] = system_info
    return sanitize(message)


def serialize(message: dict) -> bytes:
    """Serialize to UTF-8 JSON, rejecting non-finite numbers explicitly
    (allow_nan=False) rather than silently emitting invalid JSON."""
    payload = json.dumps(message, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"telemetry message is {len(payload)} bytes, exceeds "
            f"{MAX_MESSAGE_BYTES}-byte relay limit"
        )
    return payload
