#!/usr/bin/env bash
# Stop what scripts/start_two_camera_tmux.sh started, in a safe order:
#   1. the two-camera publishers, gracefully, via the existing
#      scripts/stop_two_camera_visualization.sh (SIGTERM, then wait);
#   2. the GNSS driver in this session's "gnss" window, with Ctrl+C, the
#      same way you'd stop it by hand (only that window, nothing else);
#   3. the tmux session itself.
# Touches no other mode's publishers, no other ROS2 nodes, and nothing on the
# vehicle control path.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="mila-two-camera"

"$REPO_ROOT/scripts/stop_two_camera_visualization.sh"

# Wait up to ~10 s for the publishers to finish shutting down.
for _ in $(seq 1 20); do
    if ! pgrep -f 'video\.publisher --role (front|cabin).*cameras_two_camera|telemetry\.publisher --mode minimal' > /dev/null; then
        break
    fi
    sleep 0.5
done

if tmux has-session -t "$SESSION" 2> /dev/null; then
    if tmux list-windows -t "$SESSION" -F '#W' | grep -qx gnss; then
        echo "[two-camera-tmux] stopping the GNSS driver started by this session..."
        tmux send-keys -t "$SESSION:gnss" C-c
        sleep 3
    fi
    tmux kill-session -t "$SESSION"
    echo "[two-camera-tmux] session '$SESSION' closed."
else
    echo "[two-camera-tmux] no tmux session '$SESSION' (nothing else to stop)."
fi
