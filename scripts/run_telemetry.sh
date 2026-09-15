#!/usr/bin/env bash
# Run the ROS2 -> WebSocket telemetry publisher.
#
# Does not print secrets. Does not use `set -x`.
# Note: no `set -u` — ROS2's setup.bash references unbound variables
# internally and is not nounset-safe.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source .venv/bin/activate
source /opt/ros/jazzy/setup.bash

exec python -m telemetry.publisher "$@"
