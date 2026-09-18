#!/usr/bin/env bash
# Level 3C2-A: Two-camera low-bandwidth WAN visualization mode — logical
# FRONT + CABIN video (640x480@10fps, 800kbps each) + GNSS-only telemetry
# (~5Hz), for use over constrained uplinks (~5 Mbps or lower) where the full
# four-camera mode is not reliable, but a single camera (existing one-camera
# minimal mode) leaves too little situational awareness.
#
# ROLE CORRECTION: logical FRONT in this mode is served by the physical
# camera at USB path usb-4.1.2.2 — the camera config/cameras.yaml's full
# four-camera mapping calls "left" — because the user has visually verified
# that camera is actually front-facing. See config/cameras_two_camera.yaml
# for the full explanation. config/cameras.yaml (full four-camera mode) is
# NOT modified; this mode loads a separate mapping file.
#
# Starts exactly two video publishers (logical front, cabin) and one minimal
# telemetry publisher. Does NOT start RIGHT, and does NOT start a "left" WAN
# stream. Refuses to start if a conflicting publisher is already running
# (including "left", since it is the same physical camera as this mode's
# logical front and cannot be opened twice), rather than creating duplicates
# or blindly killing anything.
#
# Does not print secrets. Does not use `set -x`.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[two-camera] checking for conflicting publishers..."
CONFLICT=0
for role in front left right cabin; do
    if pgrep -f "video\.publisher --role $role" > /dev/null; then
        echo "[two-camera] ERROR: a $role publisher is already running." >&2
        CONFLICT=1
    fi
done
if pgrep -f "telemetry\.publisher" > /dev/null; then
    echo "[two-camera] ERROR: a telemetry publisher is already running (the OCI relay only allows one)." >&2
    CONFLICT=1
fi
if [ "$CONFLICT" -eq 1 ]; then
    echo "[two-camera] Refusing to start — stop the conflicting process(es) first" >&2
    echo "[two-camera] (see scripts/stop_two_camera_visualization.sh, scripts/stop_minimal_visualization.sh," >&2
    echo "[two-camera] or for full mode: stop its publishers manually)." >&2
    exit 1
fi

# Note: no `set -u` — ROS2's setup.bash references unbound variables
# internally and is not nounset-safe.
source .venv/bin/activate
source /opt/ros/jazzy/setup.bash

echo "[two-camera] starting logical FRONT video publisher (physical usb-4.1.2.2, 640x480@10fps, 800kbps)..."
./scripts/run_front_wan.sh --video-config video_minimal.yaml --camera-config cameras_two_camera.yaml &
FRONT_PID=$!
echo "[two-camera] FRONT publisher pid=$FRONT_PID"

echo "[two-camera] starting CABIN video publisher (640x480@10fps, 800kbps)..."
./scripts/run_camera_wan.sh cabin --video-config video_minimal.yaml --camera-config cameras_two_camera.yaml &
CABIN_PID=$!
echo "[two-camera] CABIN publisher pid=$CABIN_PID"

echo "[two-camera] starting minimal GNSS telemetry publisher (~5Hz, GNSS only)..."
./scripts/run_telemetry.sh --mode minimal &
TELEMETRY_PID=$!
echo "[two-camera] telemetry publisher pid=$TELEMETRY_PID"

cleanup() {
    echo "[two-camera] stopping..."
    kill "$FRONT_PID" "$CABIN_PID" "$TELEMETRY_PID" 2>/dev/null || true
    wait "$FRONT_PID" "$CABIN_PID" "$TELEMETRY_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "[two-camera] all three started. Press Ctrl-C to stop them cleanly."
echo "[two-camera] (or run scripts/stop_two_camera_visualization.sh from another shell if this one is backgrounded)."
wait
