"""WebSocket telemetry publisher per docs/JETSON_WAN_HANDOFF.md sections 6-8.

Publishes the latest ROS2-derived vehicle_visualization state at ~10 Hz using
latest-state semantics (no outgoing queue — each tick sends the current
snapshot). On disconnect, reconnects independently with bounded exponential
backoff + jitter, re-authenticates, and resumes from the newest state; it
never replays telemetry history. Never logs the auth token or a subscriber
credential — only Jetson-required variables are read from the secret file.

Level 3B1-B4 fix: after switching the Jetson's Internet WiFi network, the
telemetry publisher's existing WebSocket connection silently died — the
kernel TCP socket stayed in ESTABLISHED state (still bound to the old
network's now-invalid source IP) with data backed up unsent in its send
buffer, and neither the application (send() kept "succeeding" — the OS
buffered the bytes without error) nor the websockets library's default
ping/pong keepalive (ping_interval=20s, ping_timeout=20s) detected it for
over 15 minutes. Root cause: the library's ping/pong frames suffer the exact
same fate as application data on a connection whose underlying route is
gone — they queue into the same doomed kernel send buffer instead of
actually reaching the peer, so timeout only occurs once Linux's own TCP
retransmission-timeout logic gives up, which defaults to many minutes.
Fix: enable aggressive kernel-level TCP keepalive (SO_KEEPALIVE +
TCP_KEEPIDLE/INTVL/CNT) on the raw socket immediately after connecting, so
the kernel itself detects an unreachable peer in ~15 seconds and surfaces it
as a connection error — triggering the existing (already correct) reconnect
logic much sooner.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import logging
import random
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil
import rclpy
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.ros2_adapter import TelemetrySourceNode  # noqa: E402
from telemetry.schema import (  # noqa: E402
    build_camera_health_block,
    build_system_block,
    build_telemetry_message,
    serialize,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECRET_FILE = Path("/home/mila/.config/teleop_visualization/oci_visualization.env")

# Only these variables are ever read — no reader/subscriber credentials, no
# server-bind variable. See docs/JETSON_WAN_HANDOFF.md section 4.
JETSON_REQUIRED_VARS = ["OCI_VISUALIZATION_HOST", "TELEMETRY_PORT", "TELEMETRY_PUBLISHER_TOKEN"]

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
PUBLISH_INTERVAL_SECONDS = 0.1  # ~10 Hz target (full mode)
MINIMAL_PUBLISH_INTERVAL_SECONDS = 0.2  # ~5 Hz target (Level 3C1-A minimal mode)

# Kernel-level TCP keepalive tuning (see module docstring: Level 3B1-B4 fix).
# Detects an unreachable peer (e.g. the local network interface/IP changed
# under a live connection) in ~TCP_KEEPALIVE_IDLE_SECONDS +
# TCP_KEEPALIVE_PROBES * TCP_KEEPALIVE_INTERVAL_SECONDS seconds, instead of
# relying on the application-level ping/pong keepalive or the OS's default
# multi-minute TCP retransmission timeout, both of which were confirmed too
# slow for this failure mode.
TCP_KEEPALIVE_IDLE_SECONDS = 5
TCP_KEEPALIVE_INTERVAL_SECONDS = 3
TCP_KEEPALIVE_PROBES = 3

# Written by video/publisher.py (see STATUS_DIR_NAME there) — one file per
# camera role. A missing or stale file means "unknown" for that role — its
# cameras.<role> block is omitted rather than fabricated. time.monotonic() is
# CLOCK_MONOTONIC on Linux, a system-wide (not per-process) clock, so
# comparing a timestamp written by a video publisher process against
# time.monotonic() read here is valid.
CAMERA_ROLES = ["front", "left", "right", "cabin"]
STATUS_DIR = REPO_ROOT / "runs"
CAMERA_STATUS_STALE_AFTER_SECONDS = 3.0
# Backward-compatible alias (Level 3B1-B4 and earlier only ever had FRONT).
FRONT_STATUS_FILE = STATUS_DIR / "front_status.json"
FRONT_STATUS_STALE_AFTER_SECONDS = CAMERA_STATUS_STALE_AFTER_SECONDS

THERMAL_ZONE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
SEND_RATE_WINDOW_SIZE = 20  # samples used to compute the measured send rate


def read_camera_status(role: str, status_file: Path | None = None) -> dict | None:
    """Read one camera role's status file. Returns None (never fabricated
    data) if the file is missing, malformed, or stale."""
    if status_file is None:
        status_file = STATUS_DIR / f"{role}_status.json"
    try:
        with open(status_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    updated_at = data.get("updated_at_monotonic")
    if not isinstance(updated_at, (int, float)):
        return None
    age_s = time.monotonic() - updated_at
    if age_s > CAMERA_STATUS_STALE_AFTER_SECONDS or age_s < 0:
        return None
    return build_camera_health_block(
        stream_path=data.get("stream_path", role),
        healthy=bool(data.get("healthy", False)),
        fps=data.get("fps"),
        last_frame_age_s=round(age_s, 3),
    )


def read_front_camera_status(status_file: Path = FRONT_STATUS_FILE) -> dict | None:
    """Retained for backward compatibility; equivalent to
    read_camera_status("front", status_file)."""
    return read_camera_status("front", status_file)


def read_all_camera_statuses() -> dict[str, dict]:
    """Read every camera role's status file. Only includes roles with a
    fresh, valid status — never fabricates an entry for a role with no data."""
    statuses = {}
    for role in CAMERA_ROLES:
        status = read_camera_status(role)
        if status is not None:
            statuses[role] = status
    return statuses


def read_system_uptime_s() -> float | None:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except (FileNotFoundError, ValueError, OSError):
        return None


def read_temperature_c() -> float | None:
    try:
        raw_millidegrees = THERMAL_ZONE_PATH.read_text(encoding="utf-8").strip()
        return float(raw_millidegrees) / 1000.0
    except (FileNotFoundError, ValueError, OSError):
        return None


def enable_aggressive_tcp_keepalive(websocket) -> bool:
    """Configure kernel-level TCP keepalive on the connection's raw socket so
    an unreachable peer (e.g. a local network interface/IP change orphaning
    this connection) is detected in seconds, not minutes. Best effort: if the
    socket can't be reached or the platform lacks these options, this must
    never crash the publisher — just fall back to the slower default
    detection path. Returns True if keepalive was successfully configured."""
    try:
        raw_socket = websocket.transport.get_extra_info("socket")
        if raw_socket is None:
            return False
        raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE_SECONDS)
        raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL_SECONDS)
        raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_PROBES)
        return True
    except (AttributeError, OSError):
        logger.debug("could not enable TCP keepalive on telemetry socket", exc_info=True)
        return False


AUTH_TIMEOUT_SECONDS = 5.0


def compute_backoff_delay(
    attempt: int,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
    rand: float = 0.5,
) -> float:
    """Bounded exponential backoff with jitter. `rand` is injectable (expects
    a value in [0, 1), e.g. random.random()) so this is deterministically
    testable; jitter multiplier is in [0.5, 1.5)."""
    delay = min(cap, base * (2 ** (attempt - 1)))
    return delay * (0.5 + rand)


def load_jetson_secrets(secret_file: Path = DEFAULT_SECRET_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(secret_file, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in JETSON_REQUIRED_VARS:
                values[key] = value.strip().strip('"').strip("'")
    missing = [var for var in JETSON_REQUIRED_VARS if var not in values]
    if missing:
        raise RuntimeError(
            f"secret file is missing required variable(s): {missing} "
            "(names only — no values logged)"
        )
    return values


def _now_iso_utc() -> str:
    return (
        datetime.fromtimestamp(time.time(), tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class TelemetryPublisherState:
    connection_state: str = "starting"  # starting|running|reconnecting|stopped
    reconnect_count: int = 0
    last_successful_publish_ts: float | None = None
    last_error_reason: str | None = None


class TelemetryPublisher:
    def __init__(
        self,
        node: TelemetrySourceNode,
        publish_interval_s: float = PUBLISH_INTERVAL_SECONDS,
        include_cameras: bool = True,
        include_system: bool = True,
        include_imu_heading: bool = True,
    ):
        self.node = node
        # Level 3C1-A: minimal mode (GNSS-only, ~5 Hz, no cameras/system/IMU
        # in the outgoing message) reuses this exact class — same connection,
        # auth, TCP-keepalive, and reconnect/backoff logic as full mode. Only
        # these four knobs differ; the ROS2 subscriptions themselves
        # (including Xsens) are unchanged and still run locally regardless —
        # only what gets forwarded over WAN is restricted.
        self.publish_interval_s = publish_interval_s
        self.include_cameras = include_cameras
        self.include_system = include_system
        self.include_imu_heading = include_imu_heading
        self.state = TelemetryPublisherState()
        self._sequence = 0
        self._shutdown_requested = False
        self._send_timestamps: collections.deque[float] = collections.deque(
            maxlen=SEND_RATE_WINDOW_SIZE
        )
        # First call after process start compares against no prior sample and
        # returns a meaningless value; discard it so later calls in
        # _build_system_block give real interval-based percentages.
        psutil.cpu_percent(interval=None)

    def request_shutdown(self, *_args) -> None:
        logger.info("telemetry shutdown requested")
        self._shutdown_requested = True

    def _measured_send_rate_hz(self) -> float | None:
        if len(self._send_timestamps) < 2:
            return None
        span = self._send_timestamps[-1] - self._send_timestamps[0]
        if span <= 0:
            return None
        return (len(self._send_timestamps) - 1) / span

    def _build_system_block(self) -> dict:
        return build_system_block(
            uptime_s=read_system_uptime_s(),
            cpu_percent=psutil.cpu_percent(interval=None),
            memory_percent=psutil.virtual_memory().percent,
            temperature_c=read_temperature_c(),
            telemetry_rate_hz=self._measured_send_rate_hz(),
        )

    def _build_cameras_block(self) -> dict | None:
        statuses = read_all_camera_statuses()
        return statuses if statuses else None

    def _build_message(self) -> dict:
        self._sequence += 1
        self._send_timestamps.append(time.monotonic())
        gnss = self.node.get_latest_gnss_sample()
        xsens_heading_deg = (
            self.node.get_latest_xsens_heading_deg() if self.include_imu_heading else None
        )
        return build_telemetry_message(
            sequence=self._sequence,
            timestamp_utc=_now_iso_utc(),
            timestamp_monotonic_s=time.monotonic(),
            gnss=gnss,
            cameras=self._build_cameras_block() if self.include_cameras else None,
            system_info=self._build_system_block() if self.include_system else None,
            xsens_heading_deg=xsens_heading_deg,
        )

    async def _authenticate(self, websocket, token: str) -> None:
        await websocket.send(
            json.dumps({"type": "auth", "role": "publisher", "token": token})
        )
        raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_SECONDS)
        acknowledgement = json.loads(raw)
        if acknowledgement != {"type": "auth_ok", "role": "publisher"}:
            raise RuntimeError(
                f"unexpected auth response type={acknowledgement.get('type')!r}"
            )

    async def _watch_for_relay_errors(self, websocket) -> None:
        """Listen concurrently for the relay's {"type":"error",...}
        acknowledgements (see docs/JETSON_WAN_HANDOFF.md section 6) so a
        rejected telemetry message is observable rather than silent."""
        async for raw in websocket:
            try:
                incoming = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(incoming, dict) and incoming.get("type") == "error":
                reason = incoming.get("reason")
                logger.warning("telemetry relay reported error: %s", reason)
                self.state.last_error_reason = f"relay_error:{reason}"

    async def _run_connection(self, uri: str, token: str) -> None:
        async with connect(uri, compression=None, open_timeout=5) as websocket:
            keepalive_enabled = enable_aggressive_tcp_keepalive(websocket)
            logger.info("tcp keepalive configured: %s", keepalive_enabled)
            await self._authenticate(websocket, token)
            logger.info("telemetry publisher authenticated")
            self.state.connection_state = "running"
            error_watcher = asyncio.create_task(self._watch_for_relay_errors(websocket))
            try:
                while not self._shutdown_requested:
                    message = self._build_message()
                    payload = serialize(message)
                    # Send as TEXT (decoded str), matching the proven smoke
                    # test contract in docs/JETSON_WAN_HANDOFF.md exactly
                    # (json.dumps(...) there produces a str -> TEXT frame).
                    await websocket.send(payload.decode("utf-8"))
                    self.state.last_successful_publish_ts = time.time()
                    await asyncio.sleep(self.publish_interval_s)
            finally:
                error_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await error_watcher

    async def run_forever(self, secrets: dict[str, str]) -> None:
        uri = f"ws://{secrets['OCI_VISUALIZATION_HOST']}:{secrets['TELEMETRY_PORT']}/"
        token = secrets["TELEMETRY_PUBLISHER_TOKEN"]
        attempt = 0
        while not self._shutdown_requested:
            try:
                await self._run_connection(uri, token)
            except (ConnectionClosed, OSError, asyncio.TimeoutError, RuntimeError) as exc:
                logger.warning("telemetry connection lost: %s: %s", type(exc).__name__, exc)
                self.state.last_error_reason = type(exc).__name__
            if self._shutdown_requested:
                break
            attempt += 1
            self.state.reconnect_count += 1
            self.state.connection_state = "reconnecting"
            delay_with_jitter = compute_backoff_delay(attempt, rand=random.random())
            logger.info(
                "telemetry reconnecting attempt=%d delay_s=%.1f",
                attempt,
                delay_with_jitter,
            )
            await asyncio.sleep(delay_with_jitter)
        self.state.connection_state = "stopped"
        logger.info(
            "telemetry publisher stopped reconnect_count=%d", self.state.reconnect_count
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="WAN telemetry publisher")
    parser.add_argument(
        "--mode",
        choices=["full", "minimal"],
        default="full",
        help="full (default): ~10Hz, gnss+cameras+system+IMU heading. "
        "minimal (Level 3C1-A): ~5Hz, GNSS only, no cameras/system/IMU.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rclpy.init()
    node = TelemetrySourceNode()
    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    if args.mode == "minimal":
        publisher = TelemetryPublisher(
            node,
            publish_interval_s=MINIMAL_PUBLISH_INTERVAL_SECONDS,
            include_cameras=False,
            include_system=False,
            include_imu_heading=False,
        )
    else:
        publisher = TelemetryPublisher(node)
    logger.info("telemetry publisher starting mode=%s", args.mode)
    signal.signal(signal.SIGINT, publisher.request_shutdown)
    signal.signal(signal.SIGTERM, publisher.request_shutdown)

    secrets = load_jetson_secrets()
    try:
        asyncio.run(publisher.run_forever(secrets))
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    main()
