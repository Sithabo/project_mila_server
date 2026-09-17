import asyncio
import json
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.publisher import (  # noqa: E402
    CAMERA_ROLES,
    MINIMAL_PUBLISH_INTERVAL_SECONDS,
    PUBLISH_INTERVAL_SECONDS,
    TCP_KEEPALIVE_IDLE_SECONDS,
    TCP_KEEPALIVE_INTERVAL_SECONDS,
    TCP_KEEPALIVE_PROBES,
    TelemetryPublisher,
    compute_backoff_delay,
    enable_aggressive_tcp_keepalive,
    load_jetson_secrets,
    read_all_camera_statuses,
    read_camera_status,
    read_front_camera_status,
)
from telemetry.schema import HEADING_SOURCE_IMU, GnssSample  # noqa: E402


class _FakeNode:
    def get_latest_gnss_sample(self):
        return None

    def get_latest_xsens_heading_deg(self):
        return None


class _FakeNodeWithXsensAndGnss:
    """A node that DOES have valid Xsens heading and a valid GNSS sample
    available, used to prove minimal mode never forwards them regardless."""

    def get_latest_gnss_sample(self):
        return GnssSample(
            timestamp_utc="2030-01-01T00:00:00.000Z",
            latitude_deg=35.2,
            longitude_deg=-97.4,
            altitude_m=320.0,
            vn_mps=1.0,
            ve_mps=1.0,
            vu_mps=0.0,
            nr_sv=10,
            h_accuracy_raw=200.0,
            v_accuracy_raw=300.0,
            cog_deg=float("nan"),  # invalid, so full mode WOULD fall back to IMU
            fix_valid=True,
        )

    def get_latest_xsens_heading_deg(self):
        return 42.0  # a real, available Xsens heading


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


class _FakeTransport:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, name):
        return self._sock if name == "socket" else None


class _FakeWebSocketWithTransport:
    def __init__(self, sock):
        self.transport = _FakeTransport(sock)


def test_enable_aggressive_tcp_keepalive_sets_real_socket_options():
    # Level 3B1-B4 regression test: reproduces the actual defect (a silently
    # dead TCP connection surviving for 15+ minutes after a network interface
    # change went undetected by the default ping/pong keepalive) by verifying
    # the fix's socket options are genuinely applied and readable back — not
    # merely that setsockopt was "called" on a mock.
    raw_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        fake_ws = _FakeWebSocketWithTransport(raw_socket)

        result = enable_aggressive_tcp_keepalive(fake_ws)

        assert result is True
        assert raw_socket.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert (
            raw_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
            == TCP_KEEPALIVE_IDLE_SECONDS
        )
        assert (
            raw_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
            == TCP_KEEPALIVE_INTERVAL_SECONDS
        )
        assert (
            raw_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)
            == TCP_KEEPALIVE_PROBES
        )
    finally:
        raw_socket.close()


def test_enable_aggressive_tcp_keepalive_missing_socket_returns_false():
    fake_ws = _FakeWebSocketWithTransport(None)
    assert enable_aggressive_tcp_keepalive(fake_ws) is False


def test_enable_aggressive_tcp_keepalive_never_raises_on_broken_socket():
    class _ExplodingSocket:
        def setsockopt(self, *args, **kwargs):
            raise OSError("simulated platform without TCP_KEEPIDLE")

    fake_ws = _FakeWebSocketWithTransport(_ExplodingSocket())
    # Must degrade gracefully (fall back to slower default detection) rather
    # than crash the publisher.
    assert enable_aggressive_tcp_keepalive(fake_ws) is False


# --- Level 3B2-A: multi-camera telemetry status ---------------------------


def _write_status(path, role, healthy=True, fps=20, age_s=0.0):
    path.write_text(
        json.dumps(
            {
                "stream_path": role,
                "healthy": healthy,
                "fps": fps,
                "updated_at_monotonic": time.monotonic() - age_s,
            }
        )
    )


def test_camera_roles_covers_all_four():
    assert set(CAMERA_ROLES) == {"front", "left", "right", "cabin"}


@pytest.mark.parametrize("role", ["front", "left", "right", "cabin"])
def test_read_camera_status_works_for_every_role(tmp_path, role):
    status_file = tmp_path / f"{role}_status.json"
    _write_status(status_file, role)

    result = read_camera_status(role, status_file)

    assert result is not None
    assert result["stream_path"] == role
    assert result["healthy"] is True
    assert result["fps"] == 20


def test_read_all_camera_statuses_returns_only_fresh_roles(monkeypatch, tmp_path):
    import telemetry.publisher as telemetry_publisher_module

    monkeypatch.setattr(telemetry_publisher_module, "STATUS_DIR", tmp_path)

    _write_status(tmp_path / "front_status.json", "front")
    _write_status(tmp_path / "left_status.json", "left")
    # right/cabin: no file at all — must be omitted, not fabricated.

    statuses = read_all_camera_statuses()

    assert set(statuses.keys()) == {"front", "left"}
    assert statuses["front"]["stream_path"] == "front"
    assert statuses["left"]["stream_path"] == "left"


def test_read_all_camera_statuses_omits_stale_roles(monkeypatch, tmp_path):
    import telemetry.publisher as telemetry_publisher_module

    monkeypatch.setattr(telemetry_publisher_module, "STATUS_DIR", tmp_path)

    _write_status(tmp_path / "front_status.json", "front", age_s=0.0)
    _write_status(tmp_path / "cabin_status.json", "cabin", age_s=100.0)  # stale

    statuses = read_all_camera_statuses()

    assert set(statuses.keys()) == {"front"}


def test_read_all_camera_statuses_empty_when_no_files(monkeypatch, tmp_path):
    import telemetry.publisher as telemetry_publisher_module

    monkeypatch.setattr(telemetry_publisher_module, "STATUS_DIR", tmp_path)

    assert read_all_camera_statuses() == {}


def test_build_cameras_block_supports_multiple_simultaneous_roles(monkeypatch, tmp_path):
    import telemetry.publisher as telemetry_publisher_module

    monkeypatch.setattr(telemetry_publisher_module, "STATUS_DIR", tmp_path)
    for role in ["front", "left", "right", "cabin"]:
        _write_status(tmp_path / f"{role}_status.json", role)

    publisher = TelemetryPublisher(_FakeNode())
    message = publisher._build_message()

    assert set(message["cameras"].keys()) == {"front", "left", "right", "cabin"}
    for role in ["front", "left", "right", "cabin"]:
        assert message["cameras"][role]["stream_path"] == role
        assert message["cameras"][role]["healthy"] is True


# --- Level 3C1-A: minimal mode telemetry -----------------------------------


def _minimal_publisher(node) -> TelemetryPublisher:
    return TelemetryPublisher(
        node,
        publish_interval_s=MINIMAL_PUBLISH_INTERVAL_SECONDS,
        include_cameras=False,
        include_system=False,
        include_imu_heading=False,
    )


def test_minimal_rate_is_5hz_full_rate_is_10hz():
    assert MINIMAL_PUBLISH_INTERVAL_SECONDS == pytest.approx(0.2)  # 5 Hz
    assert PUBLISH_INTERVAL_SECONDS == pytest.approx(0.1)  # 10 Hz, unchanged
    assert MINIMAL_PUBLISH_INTERVAL_SECONDS > PUBLISH_INTERVAL_SECONDS


def test_minimal_publisher_uses_5hz_interval():
    publisher = _minimal_publisher(_FakeNode())
    assert publisher.publish_interval_s == pytest.approx(0.2)


def test_minimal_message_includes_gnss():
    publisher = _minimal_publisher(_FakeNodeWithXsensAndGnss())
    message = publisher._build_message()
    assert "gnss" in message
    assert message["gnss"]["fix_valid"] is True
    assert message["gnss"]["latitude_deg"] == 35.2
    # Existing field name/unit preserved exactly, per Level 3C1-A section 6 —
    # no duplicate speed_mph field, no renaming.
    assert "horizontal_speed_mps" in message["gnss"]
    assert message["gnss"]["satellite_count"] == 10
    assert message["gnss"]["horizontal_accuracy_m"] == pytest.approx(2.0)


def test_minimal_message_excludes_cameras():
    publisher = _minimal_publisher(_FakeNode())
    message = publisher._build_message()
    assert "cameras" not in message


def test_minimal_message_excludes_system():
    publisher = _minimal_publisher(_FakeNode())
    message = publisher._build_message()
    assert "system" not in message


def test_minimal_message_never_uses_imu_heading_even_when_available():
    # The fake node has a real, valid Xsens heading (42.0) AND an invalid
    # GNSS cog (NaN) — full mode would fall back to IMU here. Minimal mode
    # must never do so, per Level 3C1-A: no IMU dependency.
    publisher = _minimal_publisher(_FakeNodeWithXsensAndGnss())
    message = publisher._build_message()
    assert message["gnss"]["heading_source"] != HEADING_SOURCE_IMU
    assert message["gnss"]["heading_deg"] is None


def test_full_mode_still_uses_imu_heading_fallback_unchanged():
    """Regression guard: full mode's existing IMU-fallback behavior must be
    completely unaffected by the addition of minimal mode."""
    publisher = TelemetryPublisher(_FakeNodeWithXsensAndGnss())  # default = full mode
    message = publisher._build_message()
    assert message["gnss"]["heading_source"] == HEADING_SOURCE_IMU
    assert message["gnss"]["heading_deg"] == 42.0


def test_minimal_message_still_has_schema_version_and_type():
    publisher = _minimal_publisher(_FakeNode())
    message = publisher._build_message()
    assert message["schema_version"] == 1
    assert message["message_type"] == "vehicle_visualization"
