#!/usr/bin/env bash
# Run the WAN video publisher for one camera role: left, right, or cabin.
# (FRONT has its own script, scripts/run_front_wan.sh, left unchanged.)
#
# Usage: ./scripts/run_camera_wan.sh <left|right|cabin> [extra args]
#
# Does not print secrets. Does not use `set -x` (that would echo the
# constructed SRT URI). Ctrl-C sends SIGINT to the Python supervisor, which
# shuts down the gst-launch child cleanly.
# Note: no `set -u` — ROS2's setup.bash references unbound variables
# internally and is not nounset-safe.
set -eo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <left|right|cabin> [extra args]" >&2
    exit 1
fi
ROLE="$1"
shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source .venv/bin/activate
source /opt/ros/jazzy/setup.bash

exec python -m video.publisher --role "$ROLE" "$@"
