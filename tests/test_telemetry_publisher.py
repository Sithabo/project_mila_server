import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.publisher import (  # noqa: E402
    TelemetryPublisher,
    compute_backoff_delay,
    load_jetson_secrets,
    read_front_camera_status,
)


class _FakeNode:
    def get_latest_gnss_sample(self):
        return None

    def get_latest_xsens_heading_deg(self):
        return None


class _FakeWebSocket:
    def __init__(self, recv_payloads):
        self._recv_payloads = list(recv_payloads)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return self._recv_payloads.pop(0)


def test_load_jetson_secrets_reads_only_telemetry_required(tmp_path):
    secret_file = tmp_path / "oci_visualization.env"
    secret_file.write_text(
        "\n".join(
            [
                "OCI_VISUALIZATION_HOST=203.0.113.10",
                "TELEMETRY_PORT=8767",
                "TELEMETRY_PUBLISHER_TOKEN=pubtoken",
                "TELEMETRY_SUBSCRIBER_TOKEN=subtoken",
                "TELEMETRY_HOST=0.0.0.0",
            ]
        )
    )
    secret_file.chmod(0o600)

    secrets = load_jetson_secrets(secret_file)

    assert secrets["TELEMETRY_PUBLISHER_TOKEN"] == "pubtoken"
    assert "TELEMETRY_SUBSCRIBER_TOKEN" not in secrets
    assert "TELEMETRY_HOST" not in secrets


def test_load_jetson_secrets_missing_required_raises(tmp_path):
    secret_file = tmp_path / "incomplete.env"
    secret_file.write_text("OCI_VISUALIZATION_HOST=203.0.113.10\n")
    secret_file.chmod(0o600)

    with pytest.raises(RuntimeError, match="missing required variable"):
        load_jetson_secrets(secret_file)


def test_backoff_delay_matches_video_publisher_contract():
    assert compute_backoff_delay(1, rand=0.0) == pytest.approx(0.5)
    assert compute_backoff_delay(2, rand=0.0) == pytest.approx(1.0)
    assert compute_backoff_delay(10, rand=0.0) <= 15.0


def test_authenticate_accepts_valid_ack():
    publisher = TelemetryPublisher(_FakeNode())
    websocket = _FakeWebSocket([json.dumps({"type": "auth_ok", "role": "publisher"})])

    asyncio.run(publisher._authenticate(websocket, "token123"))

    sent_message = json.loads(websocket.sent[0])
    assert sent_message == {"type": "auth", "role": "publisher", "token": "token123"}


def test_authenticate_rejects_invalid_ack():
    publisher = TelemetryPublisher(_FakeNode())
    websocket = _FakeWebSocket([json.dumps({"type": "error", "reason": "bad_token"})])

    with pytest.raises(RuntimeError, match="unexpected auth response"):
        asyncio.run(publisher._authenticate(websocket, "token123"))


def test_build_message_has_no_gnss_when_ros2_data_unavailable():
    publisher = TelemetryPublisher(_FakeNode())
    message = publisher._build_message()
    assert message["schema_version"] == 1
    assert message["message_type"] == "vehicle_visualization"
    assert "gnss" not in message


def test_build_message_includes_real_system_block():
    publisher = TelemetryPublisher(_FakeNode())
    message = publisher._build_message()
    # Real, locally-measured values — not fabricated. uptime_s and
    # temperature_c may be None on a machine without /proc/uptime or a
    # thermal zone, but the key must be present and cpu/memory percent must
    # be real numbers from psutil.
    assert "system" in message
    assert isinstance(message["system"]["cpu_percent"], (int, float))
    assert isinstance(message["system"]["memory_percent"], (int, float))


def test_read_front_camera_status_missing_file_returns_none(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert read_front_camera_status(missing) is None


def test_read_front_camera_status_fresh_file_returns_block(tmp_path):
    status_file = tmp_path / "front_status.json"
    status_file.write_text(
        json.dumps(
            {
                "stream_path": "front",
                "healthy": True,
                "fps": 20,
                "updated_at_monotonic": time.monotonic(),
            }
        )
    )
    result = read_front_camera_status(status_file)
    assert result is not None
    assert result["stream_path"] == "front"
    assert result["healthy"] is True
    assert result["fps"] == 20
    assert result["last_frame_age_s"] < 1.0


def test_read_front_camera_status_stale_file_returns_none(tmp_path):
    status_file = tmp_path / "front_status.json"
    status_file.write_text(
        json.dumps(
            {
                "stream_path": "front",
                "healthy": True,
                "fps": 20,
                # 100 seconds old — far beyond the staleness threshold, as if
                # the video publisher process died without cleanup.
                "updated_at_monotonic": time.monotonic() - 100.0,
            }
        )
    )
    assert read_front_camera_status(status_file) is None


def test_read_front_camera_status_malformed_json_returns_none(tmp_path):
    status_file = tmp_path / "front_status.json"
    status_file.write_text("not valid json{{{")
    assert read_front_camera_status(status_file) is None


def test_send_rate_measured_from_real_send_timestamps():
    publisher = TelemetryPublisher(_FakeNode())
    assert publisher._measured_send_rate_hz() is None  # no samples yet
    for _ in range(5):
        publisher._build_message()
        time.sleep(0.01)
    rate = publisher._measured_send_rate_hz()
    assert rate is not None
    assert rate > 0
