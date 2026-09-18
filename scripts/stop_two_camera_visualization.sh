#!/usr/bin/env bash
# Stop the two-camera visualization mode's logical-FRONT + CABIN + telemetry
# publishers only. Does NOT touch full-mode (FRONT/LEFT/RIGHT/CABIN) or
# one-camera minimal-mode publishers, ROS2 sensor nodes, or the control path.
#
# Distinguishes this mode's publishers from other modes' by matching the
# --camera-config cameras_two_camera.yaml argument that only this mode's
# video publishers are launched with.
set -eo pipefail

STOPPED=0

FRONT_PID="$(pgrep -f 'video\.publisher --role front.*cameras_two_camera' || true)"
if [ -n "$FRONT_PID" ]; then
    echo "[two-camera] stopping logical FRONT publisher (pid=$FRONT_PID)"
    kill -TERM $FRONT_PID
    STOPPED=1
fi

CABIN_PID="$(pgrep -f 'video\.publisher --role cabin.*cameras_two_camera' || true)"
if [ -n "$CABIN_PID" ]; then
    echo "[two-camera] stopping CABIN publisher (pid=$CABIN_PID)"
    kill -TERM $CABIN_PID
    STOPPED=1
fi

TELEMETRY_PID="$(pgrep -f 'telemetry\.publisher --mode minimal' || true)"
if [ -n "$TELEMETRY_PID" ]; then
    echo "[two-camera] stopping minimal telemetry publisher (pid=$TELEMETRY_PID)"
    kill -TERM $TELEMETRY_PID
    STOPPED=1
fi

if [ "$STOPPED" -eq 0 ]; then
    echo "[two-camera] nothing to stop (no two-camera-mode publishers found)"
fi
