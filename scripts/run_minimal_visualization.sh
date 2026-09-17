#!/usr/bin/env bash
# Level 3C1-A: Minimal robust WAN visualization mode — CABIN video only
# (640x480@10fps, 800kbps) + GNSS-only telemetry (~5Hz), for use over
# constrained/unreliable mobile Internet (ATT/Starlink field testing showed
# the full four-camera mode is not reliable enough on the available uplink).
#
# Starts exactly one CABIN video publisher and one minimal telemetry
# publisher. Does NOT start FRONT/LEFT/RIGHT. Refuses to start if a
# conflicting publisher is already running, rather than creating duplicates.
#
# Does not print secrets. Does not use `set -x`.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[minimal] checking for conflicting publishers..."
CONFLICT=0
for role in front left right cabin; do
    if pgrep -f "video\.publisher --role $role" > /dev/null; then
        echo "[minimal] ERROR: a $role publisher is already running." >&2
        CONFLICT=1
    fi
done
if pgrep -f "telemetry\.publisher" > /dev/null; then
    echo "[minimal] ERROR: a telemetry publisher is already running (the OCI relay only allows one)." >&2
    CONFLICT=1
fi
if [ "$CONFLICT" -eq 1 ]; then
    echo "[minimal] Refusing to start — stop the conflicting process(es) first" >&2
    echo "[minimal] (see scripts/stop_minimal_visualization.sh, or for full mode: stop its publishers manually)." >&2
    exit 1
fi

# Note: no `set -u` — ROS2's setup.bash references unbound variables
# internally and is not nounset-safe.
source .venv/bin/activate
source /opt/ros/jazzy/setup.bash

echo "[minimal] starting CABIN video publisher (640x480@10fps, 800kbps)..."
./scripts/run_camera_wan.sh cabin --video-config video_minimal.yaml &
CABIN_PID=$!
echo "[minimal] CABIN publisher pid=$CABIN_PID"

echo "[minimal] starting minimal GNSS telemetry publisher (~5Hz, GNSS only)..."
./scripts/run_telemetry.sh --mode minimal &
TELEMETRY_PID=$!
echo "[minimal] telemetry publisher pid=$TELEMETRY_PID"

cleanup() {
    echo "[minimal] stopping..."
    kill "$CABIN_PID" "$TELEMETRY_PID" 2>/dev/null || true
    wait "$CABIN_PID" "$TELEMETRY_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "[minimal] both started. Press Ctrl-C to stop both cleanly."
echo "[minimal] (or run scripts/stop_minimal_visualization.sh from another shell if this one is backgrounded)."
wait
