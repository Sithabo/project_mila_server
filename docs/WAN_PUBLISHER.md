# WAN Publisher (FRONT video + telemetry)

This document covers the Level 3B1-B implementation: the FRONT camera's WAN video
publisher and the ROS2-to-WebSocket telemetry publisher. LEFT/RIGHT/CABIN WAN
publication is not implemented yet (FRONT-only, by design, for this phase).

## Architecture

```
Jetson cameras + GNSS/Xsens
        |
        v
     Jetson
        |
        |  SRT video (FRONT only, this phase) + WebSocket telemetry
        v
       OCI
        |
        v
remote operator PC
```

The Jetson is the SRT/WebSocket **publisher**; the remote operator PC is the
**reader/subscriber**. See `docs/JETSON_WAN_HANDOFF.md` for the full non-secret
protocol contract this implementation follows exactly (endpoints, URI syntax,
media payload requirements, telemetry schema, reconnect contract).

This is strictly one-directional visualization data leaving the Jetson. It never
uses UDP port 4210, never talks to the ESP32, and never carries `CMD`/`MANEUVER`/
`BUTTON` messages — see "Control separation" below.

## Stable camera role resolution

`/dev/videoN` numbers are not stable across reboots/USB re-enumeration (confirmed
directly during discovery — see `docs/CAMERA_TOPOLOGY.md`). `video/camera_resolver.py`
resolves each role (`front`/`left`/`right`/`cabin`) to its current image-stream node
using the USB bus path from `config/cameras.yaml`, distinguishing the image node from
the metadata node via `v4l2-ctl --device=X --info` (`Video Capture` vs `Metadata
Capture` in the Device Caps block).

Resolution fails closed: if a role's bus path matches zero or more than one camera,
or if the image node can't be uniquely identified, `CameraResolutionError` is raised
rather than guessing. See `tests/test_camera_resolver.py`.

## Video pipeline

`video/pipeline.py` builds the validated GStreamer pipeline:

```
v4l2src -> MJPEG 800x600@20 -> jpegdec -> I420 -> nvvidconv -> NVMM
  -> nvv4l2h264enc -> h264parse -> mpegtsmux -> srtsink
```

Encoder settings (`config/video.yaml`), confirmed via `gst-inspect-1.0
nvv4l2h264enc`/`h264parse` on the installed plugin versions rather than guessed:

- `idrinterval=20`, `iframeinterval=20` — ~1 second IDR spacing at 20 FPS
- `insert-sps-pps=true` — repeats SPS/PPS at every IDR
- `h264parse config-interval=-1` — repeats codec config with every IDR
- `mpegtsmux` before `srtsink` — required; feeding elementary H.264 directly to
  `srtsink` does not satisfy the validated payload contract (a late reader could not
  reliably attach)

## MediaMTX/SRT contract

Publisher URI construction (`video/pipeline.py:build_publish_uri`) follows the
proven template from `docs/JETSON_WAN_HANDOFF.md` section 2 exactly:

```
srt://<host>:<port>?streamid=publish:<path>:<username>:<password>&passphrase=<PATH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
```

The completed URI is never logged. `video/publisher.py:redact_argv()` replaces the
`uri=...` argument before any log line is emitted.

**Known limitation:** `gst-launch-1.0` receives the URI as a literal CLI argument,
so the completed URI (including credentials and the SRT passphrase) is visible via
`ps`/`/proc/<pid>/cmdline` for the lifetime of the process — this cannot be redacted
by application code, since it's the OS exposing the process's own argv. Never run
`ps aux`, `pgrep -af`, or `ps -o cmd` while a publisher pipeline is active; use
`ps -o pid,pcpu,pmem` (no `cmd`/`args` column) or filter by process name only
(`ps -C gst-launch-1.0 -o pid,pcpu,pmem`) for monitoring instead.

## Telemetry topic mapping

`telemetry/ros2_adapter.py` subscribes to the already-validated topics (no driver
changes):

| Topic | Type | Used for |
|---|---|---|
| `/sensing/gnss/fix` | `sensor_msgs/msg/NavSatFix` | `latitude_deg`, `longitude_deg`, `altitude_m`, `fix_valid` |
| `/pvtgeodetic` | `septentrio_gnss_driver/msg/PVTGeodetic` | `vn`/`ve`/`vu` (-> speed), `nr_sv`, `h_accuracy`/`v_accuracy` (-> meters), `cog` (-> heading) |
| `/filter/euler` | `geometry_msgs/msg/Vector3Stamped` | Xsens fused yaw (`vector.z`, degrees) — heading fallback |

A topic value is treated as stale (unavailable) if not received within 2 seconds —
a driver process can stay alive while no longer publishing (observed directly: the
Xsens driver process remained running with zero message throughput during this
phase's validation). Heading selection (`telemetry/schema.py:select_heading`):
prefer GNSS course-over-ground when finite, else a fresh Xsens yaw, else
`heading_source="UNAVAILABLE"` with `heading_deg=null`. Never fabricates a value.

## Telemetry schema

`telemetry/schema.py` implements schema version 1 exactly per
`docs/JETSON_WAN_HANDOFF.md` section 7: `schema_version=1`, `message_type=
"vehicle_visualization"`, with `gnss`/`system`/`cameras` sub-objects populated only
when real data is available. All non-finite floats (NaN/Infinity) are recursively
replaced with `null` before serialization (`sanitize()`), and `serialize()` calls
`json.dumps(..., allow_nan=False)` as a second, defense-in-depth check, and enforces
the 65,536-byte relay limit.

## Telemetry WebSocket publisher

`telemetry/publisher.py` connects to `ws://<host>:<port>/`, sends the `auth`
message with `TELEMETRY_PUBLISHER_TOKEN`, and requires `auth_ok` before publishing.
Messages are sent as **TEXT** frames (`str`, matching the proven smoke-test contract
exactly — not binary) at a ~10 Hz target using latest-state semantics: each tick
sends the current snapshot; there is no outgoing queue, so nothing can build an
unbounded backlog. A concurrent task listens for the relay's
`{"type":"error",...}` acknowledgements so a rejected message is observable rather
than silent.

## Secret file

Path only (never print contents):

```
/home/mila/.config/teleop_visualization/oci_visualization.env
```

Both `video/publisher.py` and `telemetry/publisher.py` read **only** the specific
variable names each one needs (a `JETSON_REQUIRED_VARS` allowlist per module) —
reader/subscriber credentials and the server-bind variable present in the file are
never read, even though they exist in that file. See "Known deviation from the
handoff doc" below.

## Startup

```bash
cd /home/mila/teleop_visualization_jetson

# FRONT video only (this phase):
./scripts/run_front_wan.sh

# Telemetry:
./scripts/run_telemetry.sh
```

Both scripts activate `.venv`, source `/opt/ros/jazzy/setup.bash`, and never use
`set -x` (constructing an SRT URI under shell tracing would print it).

## Shutdown

Send `SIGINT` (Ctrl-C) or `SIGTERM`. Each publisher's supervisor loop catches the
signal, terminates its own child process (the `gst-launch-1.0` pipeline, or the
WebSocket connection) cleanly, and exits — it does not affect the other publisher,
ROS2 sensor nodes, or vehicle control.

## Diagnostics

- Publisher logs redact the SRT URI (`uri=<redacted>`); the telemetry publisher
  never logs the auth token or a subscriber credential.
- `PublisherState`/`TelemetryPublisherState` track `connection_state`,
  `reconnect_count`, and `last_successful_publish_ts` without exposing secrets.
- See the "Known limitation" above regarding `ps`/`/proc/<pid>/cmdline` argv
  visibility for the video publisher's `gst-launch-1.0` child process.

## Reconnect behavior

Both publishers use the same bounded exponential backoff + jitter
(`compute_backoff_delay`): base ~1s, cap ~30s, jitter multiplier in `[0.5, 1.5)`.

- **Video (FRONT):** on pipeline exit, tears down and restarts only the FRONT
  pipeline. Failure is isolated — it does not affect LEFT/RIGHT/CABIN capture (not
  yet implemented), ROS2, or control.
- **Telemetry:** on WebSocket disconnect, keeps only the latest local ROS2-derived
  state (no queue), reconnects, re-authenticates, and resumes from the newest
  state. It never replays telemetry history.

## Control separation (non-negotiable)

This implementation never uses UDP port 4210, never imports or invokes G29/ESP32
control code, and never feeds any visualization data into vehicle control. Failure
of either publisher in this document cannot block or alter control transmission —
each runs as an independently supervised process with its own lifecycle.

## Known deviation from the handoff doc / open finding

The Jetson secret file (`/home/mila/.config/teleop_visualization/oci_visualization.env`)
currently contains **more variables than the handoff doc says the Jetson should
need** — it includes `MEDIA_READER_USERNAME`, `MEDIA_READER_PASSWORD`, all four
`*_SRT_READ_PASSPHRASE` variables, `TELEMETRY_SUBSCRIBER_TOKEN`, and
`TELEMETRY_HOST` (server-bind-only), none of which are classified "Jetson required"
in `docs/JETSON_WAN_HANDOFF.md` section 4. This code never reads or uses any of
them (see the `JETSON_REQUIRED_VARS` allowlists in `video/publisher.py` and
`telemetry/publisher.py`), but the file's current contents represent a
least-privilege gap worth addressing — the Jetson does not need reader/subscriber
credentials or the server-bind address to do its job.
