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
  -> nvv4l2h264enc -> h264parse -> explicit video/x-h264 caps -> mpegtsmux -> srtsink
```

Encoder settings (`config/video.yaml`), confirmed via `gst-inspect-1.0
nvv4l2h264enc`/`h264parse`/`mpegtsmux` on the installed plugin versions rather than
guessed:

- `idrinterval=20`, `iframeinterval=20` — ~1 second IDR spacing at 20 FPS
- `insert-sps-pps=true` — repeats SPS/PPS at every IDR
- `h264parse config-interval=-1` — repeats codec config with every IDR
- `video/x-h264,stream-format=byte-stream,alignment=au` — explicit caps between
  `h264parse` and `mpegtsmux`, pinned rather than left to implicit negotiation
- `mpegtsmux alignment=7` — **the Level 3B1-B2 fix** (see below)
- `mpegtsmux pat-interval=9000 pmt-interval=9000` — PAT/PMT repeated every 9000
  ticks of the 90kHz MPEG-TS clock (100ms), well within the sub-second range a
  late reader needs to discover the program quickly
- `mpegtsmux` before `srtsink` — required; feeding elementary H.264 directly to
  `srtsink` does not satisfy the validated payload contract (a late reader could not
  reliably attach)

### Level 3B1-B2: MPEG-TS/SRT decodability fix

Remote validation of the first FRONT implementation found: SRT transport and
authentication were healthy, MediaMTX recognized an H.264 track, but the reader
decoded **zero frames** and MediaMTX logged MPEG-TS PES parsing errors.

Root cause, found via `gst-inspect-1.0 mpegtsmux` rather than guessed: the
`alignment` property was left at its default (`-1` = auto). The property's own
documentation states: *"Number of packets per buffer ... (-1 = auto, 0 = all
available packets, **7 for UDP streaming**)"*. SRT runs over UDP, and `7 * 188`-byte
TS packets = 1316 bytes — exactly the validated SRT `pkt_size=1316`. Leaving this at
auto meant `mpegtsmux` was not producing buffers aligned to the transport's packet
size, consistent with a receiver losing PES/TS packet sync. The `h264parse` output
caps were also left to implicit negotiation rather than pinned explicitly.

**Fix:** added the explicit `video/x-h264,stream-format=byte-stream,alignment=au`
caps filter after `h264parse`, and set `mpegtsmux alignment=7` plus explicit
`pat-interval`/`pmt-interval`.

**Local validation before re-publishing to OCI** (`tests/test_video_pipeline.py`
covers the argv structure; the actual GStreamer run was manual, not unit-tested,
since it requires real hardware):
- 30-second local `.ts` file capture using the identical encode/mux chain
  (`video/pipeline.py:build_local_ts_test_args`), terminated via natural EOS
  (`num-buffers`, not an external signal — an abrupt `SIGTERM` was found to
  truncate the final buffer, which is a file-write artifact, not a live-streaming
  concern, but worth knowing: use `-e` and let a live stream reach EOS/shutdown
  gracefully rather than SIGKILL).
- `ffprobe`: `mpegts` container, `h264` codec, `800x600`, `20/1` frame rate.
- Full decode via `ffmpeg -f null -`: 600/600 frames, zero warnings, zero corrupt
  packets.
- **Late-start decodability** (the actual reported failure mode — a reader
  attaching mid-stream, not from byte zero): seeking 20 seconds into the 30-second
  file and decoding from there succeeded cleanly (200/200 remaining frames, codec
  correctly identified as `h264 (Constrained Baseline)`), confirming a late
  reader can find SPS/PPS/PAT/PMT and decode without needing the stream start.

Real OCI re-publication after the fix ran stably for 179.6s and, in a later
combined run, another 183.6s — both well past the required 2-minute window, zero
reconnects, zero GStreamer errors either time.

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

### `cameras.front` and `system` health blocks

The telemetry publisher and video publisher are separate processes with no direct
coupling. `video/publisher.py` writes a small non-secret status file
(`runs/front_status.json`, gitignored) once per second while its pipeline is
running — `stream_path`, `healthy` (real process-alive state), `fps` (the
configured capture rate — accurate for a live, real-time `v4l2src` pipeline, not
a batch/offline one), and a `time.monotonic()` timestamp. `time.monotonic()` is
`CLOCK_MONOTONIC` on Linux, a system-wide clock, so comparing a timestamp written
by one process against `time.monotonic()` read in another is valid.

`telemetry/publisher.py:read_front_camera_status()` reads that file each tick. If
it's missing, malformed, or older than 3 seconds (the video publisher died without
cleanup), the `cameras.front` block is **omitted entirely** rather than reporting
fabricated health — matching the handoff doc's "use null or omit ... do not invent
sensor values" rule. On its own shutdown, the video publisher writes a final
`healthy: false` status so a lingering stale-but-recent file doesn't misreport
health during its own shutdown window.

The `system` block uses real, locally-measured values: `psutil.cpu_percent()`/
`psutil.virtual_memory().percent` (system-wide, not per-process), `/proc/uptime`
for `uptime_s`, `/sys/class/thermal/thermal_zone0/temp` for `temperature_c`, and
`telemetry_rate_hz` computed from the actual measured interval between the last 20
sent messages (not the configured target) — confirmed in a real run to read
~9.7 Hz against a 10 Hz target, i.e. an honest measurement, not the hardcoded
target value.

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
