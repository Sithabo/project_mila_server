import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.schema import (  # noqa: E402
    GnssSample,
    HEADING_SOURCE_GNSS,
    HEADING_SOURCE_IMU,
    HEADING_SOURCE_UNAVAILABLE,
    MAX_MESSAGE_BYTES,
    MESSAGE_TYPE,
    SCHEMA_VERSION,
    build_telemetry_message,
    compute_horizontal_speed_mps,
    convert_accuracy_to_meters,
    sanitize,
    select_heading,
    serialize,
)


def make_gnss(**overrides) -> GnssSample:
    defaults = dict(
        timestamp_utc="2030-01-01T00:00:00.000Z",
        latitude_deg=35.2,
        longitude_deg=-97.4,
        altitude_m=320.0,
        vn_mps=1.0,
        ve_mps=1.0,
        vu_mps=0.0,
        nr_sv=10,
        h_accuracy_raw=200.0,  # -> 2.0 m
        v_accuracy_raw=300.0,  # -> 3.0 m
        cog_deg=90.0,
        fix_valid=True,
    )
    defaults.update(overrides)
    return GnssSample(**defaults)


def test_speed_derivation():
    assert compute_horizontal_speed_mps(3.0, 4.0) == pytest.approx(5.0)
    assert compute_horizontal_speed_mps(0.0, 0.0) == 0.0


def test_speed_derivation_missing_inputs():
    assert compute_horizontal_speed_mps(None, 4.0) is None
    assert compute_horizontal_speed_mps(float("nan"), 4.0) is None


def test_accuracy_conversion():
    assert convert_accuracy_to_meters(200.0) == pytest.approx(2.0)
    assert convert_accuracy_to_meters(0) == 0.0
    assert convert_accuracy_to_meters(None) is None


def test_heading_prefers_valid_gnss_cog():
    heading, source = select_heading(87.0, 12.0)
    assert heading == 87.0
    assert source == HEADING_SOURCE_GNSS


def test_heading_falls_back_to_xsens_when_cog_nan():
    heading, source = select_heading(float("nan"), 12.0)
    assert heading == 12.0
    assert source == HEADING_SOURCE_IMU


def test_heading_unavailable_when_both_missing():
    heading, source = select_heading(float("nan"), None)
    assert heading is None
    assert source == HEADING_SOURCE_UNAVAILABLE


def test_sanitize_replaces_non_finite_with_none():
    data = {"a": float("nan"), "b": float("inf"), "c": [1.0, float("-inf")], "d": 2}
    result = sanitize(data)
    assert result == {"a": None, "b": None, "c": [1.0, None], "d": 2}


def test_build_message_schema_version_and_type():
    message = build_telemetry_message(
        sequence=1,
        timestamp_utc="2030-01-01T00:00:00.000Z",
        timestamp_monotonic_s=1.0,
        gnss=make_gnss(),
    )
    assert message["schema_version"] == SCHEMA_VERSION
    assert message["message_type"] == MESSAGE_TYPE
    assert message["schema_version"] == 1
    assert message["message_type"] == "vehicle_visualization"


def test_build_message_gnss_block_values():
    message = build_telemetry_message(
        sequence=1,
        timestamp_utc="2030-01-01T00:00:00.000Z",
        timestamp_monotonic_s=1.0,
        gnss=make_gnss(vn_mps=3.0, ve_mps=4.0),
    )
    gnss_block = message["gnss"]
    assert gnss_block["horizontal_speed_mps"] == pytest.approx(5.0)
    assert gnss_block["horizontal_accuracy_m"] == pytest.approx(2.0)
    assert gnss_block["vertical_accuracy_m"] == pytest.approx(3.0)
    assert gnss_block["heading_source"] == HEADING_SOURCE_GNSS


def test_build_message_nan_cog_falls_back_and_sanitizes():
    message = build_telemetry_message(
        sequence=1,
        timestamp_utc="2030-01-01T00:00:00.000Z",
        timestamp_monotonic_s=1.0,
        gnss=make_gnss(cog_deg=math.nan),
        xsens_heading_deg=45.0,
    )
    gnss_block = message["gnss"]
    assert gnss_block["heading_deg"] == 45.0
    assert gnss_block["heading_source"] == HEADING_SOURCE_IMU


def test_build_message_no_gnss_block_when_absent():
    message = build_telemetry_message(
        sequence=1,
        timestamp_utc="2030-01-01T00:00:00.000Z",
        timestamp_monotonic_s=1.0,
        gnss=None,
    )
    assert "gnss" not in message


def test_serialize_rejects_non_finite_numbers():
    # sanitize() should have already removed non-finite floats from any
    # message built via build_telemetry_message, so serialize() should never
    # see one in normal use; verify it still fails loudly if it ever does.
    with pytest.raises(ValueError):
        serialize({"schema_version": 1, "message_type": "vehicle_visualization", "x": float("nan")})


def test_serialize_rejects_oversized_message():
    huge = {"schema_version": 1, "message_type": "vehicle_visualization", "pad": "x" * (MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(ValueError, match="exceeds"):
        serialize(huge)


def test_serialize_valid_message_round_trips():
    message = build_telemetry_message(
        sequence=42,
        timestamp_utc="2030-01-01T00:00:00.000Z",
        timestamp_monotonic_s=1.0,
        gnss=make_gnss(),
    )
    payload = serialize(message)
    assert isinstance(payload, bytes)
    assert len(payload) <= MAX_MESSAGE_BYTES
