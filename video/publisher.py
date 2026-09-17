"""Supervise one camera's WAN publication pipeline (gst-launch-1.0 subprocess)
with bounded exponential backoff + jitter reconnect, clean SIGINT/SIGTERM
shutdown, and child-process reaping.

Never logs a complete SRT URI or any secret value. Failure of this process
must never affect ROS2 sensor nodes or vehicle control — this module only
ever spawns/supervises a single gst-launch-1.0 child process for one camera.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video.camera_resolver import resolve_camera, load_camera_config  # noqa: E402
from video.pipeline import VideoConfig, build_gst_launch_args, build_publish_uri  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SECRET_FILE = Path("/home/mila/.config/teleop_visualization/oci_visualization.env")

# Only these variables are ever read from the secret file. Any other
# variables present in the file (e.g. reader/subscriber credentials) are
# ignored on purpose — see docs/JETSON_WAN_HANDOFF.md section 4.
JETSON_REQUIRED_VARS = [
    "OCI_VISUALIZATION_HOST",
    "SRT_PORT",
    "MEDIA_PUBLISHER_USERNAME",
    "MEDIA_PUBLISHER_PASSWORD",
    "FRONT_SRT_PUBLISH_PASSPHRASE",
    "LEFT_SRT_PUBLISH_PASSPHRASE",
    "RIGHT_SRT_PUBLISH_PASSPHRASE",
    "CABIN_SRT_PUBLISH_PASSPHRASE",
]

PASSPHRASE_VAR_BY_ROLE = {
    "front": "FRONT_SRT_PUBLISH_PASSPHRASE",
    "left": "LEFT_SRT_PUBLISH_PASSPHRASE",
    "right": "RIGHT_SRT_PUBLISH_PASSPHRASE",
    "cabin": "CABIN_SRT_PUBLISH_PASSPHRASE",
}

MIN_STABLE_SECONDS = 5.0  # a run shorter than this counts as a failed attempt
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0

# Local, non-secret status file written by this publisher and read by
# telemetry/publisher.py to populate the optional cameras.<role> telemetry
# block honestly (real process-alive state + configured fps), rather than
# coupling the two processes directly. Gitignored (see .gitignore "runs/").
STATUS_DIR_NAME = "runs"
STATUS_WRITE_INTERVAL_SECONDS = 1.0


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
    """Read only the Jetson-required variables from the secret file. Never
    logs values. Ignores any other variable present in the file."""
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


def load_video_config(config_path: Path) -> VideoConfig:
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    capture = raw["capture"]
    encoder = raw["encoder"]
    h264parse_cfg = raw["h264parse"]
    mpegts_cfg = raw["mpegts"]
    return VideoConfig(
        width=capture["width"],
        height=capture["height"],
        framerate=capture["framerate"],
        bitrate=encoder["bitrate"],
        idr_interval_frames=encoder["idr_interval_frames"],
        iframe_interval_frames=encoder["iframe_interval_frames"],
        insert_sps_pps=encoder["insert_sps_pps"],
        poc_type=encoder["poc_type"],
        h264parse_config_interval=h264parse_cfg["config_interval"],
        h264_stream_format=h264parse_cfg["stream_format"],
        h264_alignment=h264parse_cfg["alignment"],
        mpegts_alignment=mpegts_cfg["alignment"],
        mpegts_pat_interval_ticks=mpegts_cfg["pat_interval_ticks"],
        mpegts_pmt_interval_ticks=mpegts_cfg["pmt_interval_ticks"],
        srt_pkt_size=raw["srt"]["pkt_size"],
    )


def redact_argv(argv: list[str]) -> list[str]:
    """Return a copy of argv safe to log: any arg containing 'uri=' has its
    value replaced, since the SRT URI contains credentials/passphrases."""
    redacted = []
    for arg in argv:
        if arg.startswith("uri="):
            redacted.append("uri=<redacted>")
        else:
            redacted.append(arg)
    return redacted


@dataclass
class PublisherState:
    connection_state: str = "starting"  # starting|running|reconnecting|stopped
    reconnect_count: int = 0
    last_successful_publish_ts: float | None = None
    started_at: float = field(default_factory=time.time)


class CameraPublisher:
    def __init__(self, role: str, repo_root: Path = REPO_ROOT, video_config_filename: str = "video.yaml"):
        self.role = role
        self.repo_root = repo_root
        # Level 3C1-A: minimal mode uses config/video_minimal.yaml (640x480@10fps,
        # 800kbps) instead of the locked config/video.yaml (800x600@20fps,
        # 4Mbps) used by full mode. Same pipeline builder, same resolver, same
        # reconnect logic — only which settings file is loaded differs.
        self.video_config_filename = video_config_filename
        self.state = PublisherState()
        self._shutdown_requested = False
        self._child: subprocess.Popen | None = None

    def _resolve_camera(self):
        cameras_config = self.repo_root / "config" / "cameras.yaml"
        roles = load_camera_config(cameras_config)
        usb_path = roles[self.role]
        return resolve_camera(self.role, usb_path)

    def _build_argv(self) -> tuple[list[str], str, VideoConfig]:
        image_node = self._resolve_camera().image_node
        video_config = load_video_config(self.repo_root / "config" / self.video_config_filename)
        secrets = load_jetson_secrets()
        passphrase_var = PASSPHRASE_VAR_BY_ROLE[self.role]
        srt_uri = build_publish_uri(
            host=secrets["OCI_VISUALIZATION_HOST"],
            port=secrets["SRT_PORT"],
            path=self.role,
            publisher_username=secrets["MEDIA_PUBLISHER_USERNAME"],
            publisher_password=secrets["MEDIA_PUBLISHER_PASSWORD"],
            passphrase=secrets[passphrase_var],
            pbkeylen=32,
            pkt_size=video_config.srt_pkt_size,
        )
        argv = build_gst_launch_args(image_node, video_config, srt_uri)
        return argv, image_node, video_config

    def _status_dir(self) -> Path:
        return self.repo_root / STATUS_DIR_NAME

    def _status_file(self) -> Path:
        return self._status_dir() / f"{self.role}_status.json"

    def _write_status(self, healthy: bool, fps: float | None) -> None:
        """Write a small non-secret status file for telemetry to read. Best
        effort: a failure here must never affect video publication."""
        try:
            self._status_dir().mkdir(parents=True, exist_ok=True)
            status = {
                "stream_path": self.role,
                "healthy": healthy,
                "fps": fps,
                "updated_at_monotonic": time.monotonic(),
            }
            tmp_path = self._status_file().with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(status, handle)
            tmp_path.replace(self._status_file())
        except OSError:
            logger.debug("failed to write status file for role=%s", self.role, exc_info=True)

    def _status_writer_loop(self, video_config: VideoConfig, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            healthy = self._child is not None and self._child.poll() is None
            self._write_status(healthy=healthy, fps=video_config.framerate)
            stop_event.wait(STATUS_WRITE_INTERVAL_SECONDS)

    def request_shutdown(self, *_args) -> None:
        logger.info("shutdown requested (role=%s)", self.role)
        self._shutdown_requested = True
        if self._child is not None and self._child.poll() is None:
            self._child.send_signal(signal.SIGTERM)

    def _run_once(self) -> None:
        argv, image_node, video_config = self._build_argv()
        logger.info(
            "starting pipeline role=%s image_node=%s argv=%s",
            self.role,
            image_node,
            redact_argv(argv),
        )
        self._child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.state.connection_state = "running"
        start_time = time.time()

        status_stop_event = threading.Event()
        status_thread = threading.Thread(
            target=self._status_writer_loop,
            args=(video_config, status_stop_event),
            daemon=True,
        )
        status_thread.start()

        try:
            while True:
                if self._child.poll() is not None:
                    break
                line = self._child.stdout.readline() if self._child.stdout else ""
                if line:
                    # gst-launch -e output does not contain the URI after
                    # startup; still guard against accidental leakage.
                    logger.debug("[%s] %s", self.role, line.rstrip())
                elapsed = time.time() - start_time
                if elapsed >= MIN_STABLE_SECONDS:
                    self.state.last_successful_publish_ts = time.time()
                if self._shutdown_requested:
                    break
        finally:
            status_stop_event.set()
            status_thread.join(timeout=2)
            self._write_status(healthy=False, fps=None)

        return_code = self._child.wait(timeout=10)
        elapsed = time.time() - start_time
        logger.info(
            "pipeline exited role=%s return_code=%s elapsed_s=%.1f",
            self.role,
            return_code,
            elapsed,
        )
        if elapsed >= MIN_STABLE_SECONDS:
            self.state.last_successful_publish_ts = time.time()

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.request_shutdown)
        signal.signal(signal.SIGTERM, self.request_shutdown)

        attempt = 0
        while not self._shutdown_requested:
            try:
                self._run_once()
            except Exception:  # noqa: BLE001 - must never crash the supervisor loop
                logger.exception("pipeline attempt failed role=%s", self.role)

            if self._shutdown_requested:
                break

            attempt += 1
            self.state.reconnect_count += 1
            self.state.connection_state = "reconnecting"
            delay_with_jitter = compute_backoff_delay(attempt, rand=random.random())
            logger.info(
                "reconnecting role=%s attempt=%d delay_s=%.1f",
                self.role,
                attempt,
                delay_with_jitter,
            )
            time.sleep(delay_with_jitter)

        self.state.connection_state = "stopped"
        logger.info(
            "publisher stopped role=%s reconnect_count=%d",
            self.role,
            self.state.reconnect_count,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="WAN video publisher for one camera role")
    parser.add_argument("--role", required=True, choices=["front", "left", "right", "cabin"])
    parser.add_argument(
        "--video-config",
        default="video.yaml",
        help="config/ filename to load video settings from (default: video.yaml, "
        "the locked full-mode settings). Minimal mode uses video_minimal.yaml.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    publisher = CameraPublisher(role=args.role, video_config_filename=args.video_config)
    publisher.run_forever()


if __name__ == "__main__":
    main()
