import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.publisher import (  # noqa: E402
    TelemetryPublisher,
    compute_backoff_delay,
    load_jetson_secrets,
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
