"""WebSocket telemetry publisher per docs/JETSON_WAN_HANDOFF.md sections 6-8.

Publishes the latest ROS2-derived vehicle_visualization state at ~10 Hz using
latest-state semantics (no outgoing queue — each tick sends the current
snapshot). On disconnect, reconnects independently with bounded exponential
backoff + jitter, re-authenticates, and resumes from the newest state; it
never replays telemetry history. Never logs the auth token or a subscriber
credential — only Jetson-required variables are read from the secret file.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import rclpy
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telemetry.ros2_adapter import TelemetrySourceNode  # noqa: E402
from telemetry.schema import build_telemetry_message, serialize  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_SECRET_FILE = Path("/home/mila/.config/teleop_visualization/oci_visualization.env")

# Only these variables are ever read — no reader/subscriber credentials, no
# server-bind variable. See docs/JETSON_WAN_HANDOFF.md section 4.
JETSON_REQUIRED_VARS = ["OCI_VISUALIZATION_HOST", "TELEMETRY_PORT", "TELEMETRY_PUBLISHER_TOKEN"]

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
PUBLISH_INTERVAL_SECONDS = 0.1  # ~10 Hz target
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
    def __init__(self, node: TelemetrySourceNode):
        self.node = node
        self.state = TelemetryPublisherState()
        self._sequence = 0
        self._shutdown_requested = False

    def request_shutdown(self, *_args) -> None:
        logger.info("telemetry shutdown requested")
        self._shutdown_requested = True

    def _build_message(self) -> dict:
        self._sequence += 1
        gnss = self.node.get_latest_gnss_sample()
        xsens_heading_deg = self.node.get_latest_xsens_heading_deg()
        return build_telemetry_message(
            sequence=self._sequence,
            timestamp_utc=_now_iso_utc(),
            timestamp_monotonic_s=time.monotonic(),
            gnss=gnss,
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
                    await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
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

    publisher = TelemetryPublisher(node)
    signal.signal(signal.SIGINT, publisher.request_shutdown)
    signal.signal(signal.SIGTERM, publisher.request_shutdown)

    secrets = load_jetson_secrets()
    try:
        asyncio.run(publisher.run_forever(secrets))
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    main()
