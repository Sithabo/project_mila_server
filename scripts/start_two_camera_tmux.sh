#!/usr/bin/env bash
# Start two-camera visualization mode inside a detached tmux session, so the
# GNSS driver and the publishers keep running after the SSH connection that
# started them closes (laptop asleep, Wi-Fi drop, terminal closed).
#
#   ./scripts/start_two_camera_tmux.sh     # start
#   tmux attach -t mila-two-camera         # watch (Ctrl+b then 0/1; Ctrl+b d to detach)
#   ./scripts/stop_two_camera_tmux.sh      # stop everything cleanly
#
# Window 0 "gnss": the Septentrio GNSS driver (skipped if one is already
# running elsewhere). Window 1 "viz": scripts/run_two_camera_visualization.sh,
# unchanged, including its own refusal to start next to conflicting
# publishers. Each window drops to a shell if its program exits, so the last
# error stays readable instead of the window disappearing.
#
# Survives disconnects, not reboots. Visualization only: starts nothing on
# the vehicle control path. Does not print secrets. Does not use `set -x`.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="mila-two-camera"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
SENSORS_SETUP="/home/mila/sensors_ws/install/setup.bash"

if ! command -v tmux > /dev/null; then
    echo "[two-camera-tmux] ERROR: tmux is not installed (sudo apt install -y tmux)." >&2
    exit 1
fi
if tmux has-session -t "$SESSION" 2> /dev/null; then
    echo "[two-camera-tmux] already running. Watch: tmux attach -t $SESSION" >&2
    echo "[two-camera-tmux] Stop first with scripts/stop_two_camera_tmux.sh to restart." >&2
    exit 1
fi

GNSS_COMMAND="source $ROS_SETUP && source $SENSORS_SETUP && ros2 launch mila_septentrio_launch septentrio.launch.py; echo '[two-camera-tmux] GNSS driver exited.'; exec bash"
VIZ_COMMAND="cd $REPO_ROOT && ./scripts/run_two_camera_visualization.sh; echo '[two-camera-tmux] visualization exited.'; exec bash"

if pgrep -f "septentrio_gnss_driver_node" > /dev/null; then
    echo "[two-camera-tmux] a GNSS driver is already running; not starting another."
    tmux new-session -d -s "$SESSION" -n viz -c "$REPO_ROOT" bash -lc "$VIZ_COMMAND"
else
    echo "[two-camera-tmux] starting GNSS driver (window 0: gnss)..."
    tmux new-session -d -s "$SESSION" -n gnss -c "$REPO_ROOT" bash -lc "$GNSS_COMMAND"
    # Give the driver a head start; the telemetry publisher copes without
    # GNSS anyway, it just sends no position until a fix arrives.
    sleep 3
    echo "[two-camera-tmux] starting visualization (window 1: viz)..."
    tmux new-window -t "$SESSION" -n viz -c "$REPO_ROOT" bash -lc "$VIZ_COMMAND"
fi

echo "[two-camera-tmux] running in tmux session '$SESSION'."
echo "[two-camera-tmux]   watch: tmux attach -t $SESSION   (Ctrl+b then 0/1 to switch, Ctrl+b d to detach)"
echo "[two-camera-tmux]   stop:  ./scripts/stop_two_camera_tmux.sh"
