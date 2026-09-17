#!/usr/bin/env bash
# Stop the minimal visualization mode's CABIN + telemetry publishers only.
# Does NOT touch any full-mode (FRONT/LEFT/RIGHT) publishers, ROS2 sensor
# nodes, or the control path.
set -eo pipefail

STOPPED=0

CABIN_PID="$(pgrep -f 'video\.publisher --role cabin' || true)"
if [ -n "$CABIN_PID" ]; then
    echo "[minimal] stopping CABIN publisher (pid=$CABIN_PID)"
    kill -TERM $CABIN_PID
    STOPPED=1
fi

TELEMETRY_PID="$(pgrep -f 'telemetry\.publisher --mode minimal' || true)"
if [ -n "$TELEMETRY_PID" ]; then
    echo "[minimal] stopping minimal telemetry publisher (pid=$TELEMETRY_PID)"
    kill -TERM $TELEMETRY_PID
    STOPPED=1
fi

if [ "$STOPPED" -eq 0 ]; then
    echo "[minimal] nothing to stop (no minimal-mode publishers found)"
fi
