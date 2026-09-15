#!/usr/bin/env bash
# Run the FRONT camera WAN video publisher.
#
# Does not print secrets. Does not use `set -x` (that would echo the
# constructed SRT URI). Ctrl-C sends SIGINT to the Python supervisor, which
# shuts down the gst-launch child cleanly.
# Note: no `set -u` — ROS2's setup.bash references unbound variables
# internally and is not nounset-safe.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source .venv/bin/activate
source /opt/ros/jazzy/setup.bash

exec python -m video.publisher --role front "$@"
