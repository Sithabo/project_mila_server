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


def is_finite(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


def compute_horizontal_speed_mps(vn, ve):
    if not (is_finite(vn) and is_finite(ve)):
        return None
    return math.sqrt(vn ** 2 + ve ** 2)


def convert_accuracy_to_meters(raw_hundredths_of_a_meter):
    """PVTGeodetic h_accuracy/v_accuracy are in units of 0.01 m."""
    if not is_finite(raw_hundredths_of_a_meter):
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
    speed = compute_horizontal_speed_mps(gnss.vn_mps, gnss.ve_mps)
    heading_deg, heading_source = select_heading(gnss.cog_deg, xsens_heading_deg)
    return {
        "timestamp_utc": gnss.timestamp_utc,
        "latitude_deg": gnss.latitude_deg,
        "longitude_deg": gnss.longitude_deg,
        "altitude_m": gnss.altitude_m,
        "horizontal_speed_mps": speed,
        "heading_deg": heading_deg,
        "heading_source": heading_source,
        "satellite_count": gnss.nr_sv,
        "horizontal_accuracy_m": convert_accuracy_to_meters(gnss.h_accuracy_raw),
        "vertical_accuracy_m": convert_accuracy_to_meters(gnss.v_accuracy_raw),
        "fix_valid": gnss.fix_valid,
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
