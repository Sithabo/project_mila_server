# WAN Publisher (four-camera video + telemetry)

This document covers the WAN video publishers for all four camera roles — `front`,
`left`, `right`, `cabin` — and the ROS2-to-WebSocket telemetry publisher. All four
cameras share one implementation (`video/publisher.py:CameraPublisher`); only the
role/USB-path/SRT-path/passphrase selection differs between them (Level 3B2-A).

## Architecture

```
Jetson cameras (front/left/right/cabin) + GNSS/Xsens
        |
        v
     Jetson
        |
        |  4x SRT video (one per camera role) + WebSocket telemetry
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

# FRONT video (own script, unchanged since Level 3B1-B):
./scripts/run_front_wan.sh

# LEFT / RIGHT / CABIN video (shared generic script, Level 3B2-A):
./scripts/run_camera_wan.sh left
./scripts/run_camera_wan.sh right
./scripts/run_camera_wan.sh cabin

# Telemetry (one process serves all active cameras' status):
./scripts/run_telemetry.sh
```

Each camera runs as its own independent OS process — `CameraPublisher` in
`video/publisher.py`, parameterized only by `--role`. A failure in one camera's
process (crash, reconnect loop, USB blip) never affects the others; each has its
own supervisor loop, backoff state, and status file. All four use the exact same
locked encoder/mux/SRT configuration — only the resolved USB path, the MediaMTX
SRT path, and the per-role passphrase variable differ.

All scripts activate `.venv`, source `/opt/ros/jazzy/setup.bash`, and never use
`set -x` (constructing an SRT URI under shell tracing would print it).

**Restart telemetry after changing telemetry code.** The telemetry publisher does
not hot-reload — if `telemetry/publisher.py` or `telemetry/schema.py` changes
while a telemetry process is already running, that process keeps executing the
code it loaded at startup. Restart it to pick up changes (this was observed
directly during Level 3B2-A: a telemetry process still running from before the
multi-camera change kept reporting only `cameras.front` until restarted).

## Shutdown

Send `SIGINT` (Ctrl-C) or `SIGTERM` to the specific process you want to stop.
Each publisher's supervisor loop catches the signal, terminates its own child
process (the `gst-launch-1.0` pipeline, or the WebSocket connection) cleanly, and
exits — it does not affect any other camera's publisher, telemetry, ROS2 sensor
nodes, or vehicle control. There must be exactly one telemetry publisher process
at a time (the relay rejects a second concurrent publisher connection with close
code 1008); check `pgrep -f telemetry.publisher` (safe — telemetry's argv
contains no secrets) before starting a new one.

## Diagnostics

- Publisher logs redact the SRT URI (`uri=<redacted>`); the telemetry publisher
  never logs the auth token or a subscriber credential.
- `PublisherState`/`TelemetryPublisherState` track `connection_state`,
  `reconnect_count`, and `last_successful_publish_ts` without exposing secrets.
- See the "Known limitation" above regarding `ps`/`/proc/<pid>/cmdline` argv
  visibility for a video publisher's `gst-launch-1.0` child process — this
  applies to all four camera roles equally, not just FRONT. Use
  `ps -o pid,pcpu,pmem -C gst-launch-1.0` (no `cmd`/`args` column) or check the
  role-specific log file instead of a full-argv process listing.
- Status files: `runs/<role>_status.json` (gitignored), one per active camera,
  written once per second by that role's `CameraPublisher` and read by the
  telemetry publisher to populate `cameras.<role>`.

## Reconnect behavior

Both publishers use the same bounded exponential backoff + jitter
(`compute_backoff_delay`): base ~1s, cap ~30s, jitter multiplier in `[0.5, 1.5)`.

- **Video (FRONT):** on pipeline exit, tears down and restarts only the FRONT
  pipeline. Failure is isolated — it does not affect LEFT/RIGHT/CABIN capture (not
  yet implemented), ROS2, or control.
- **Telemetry:** on WebSocket disconnect, keeps only the latest local ROS2-derived
  state (no queue), reconnects, re-authenticates, and resumes from the newest
  state. It never replays telemetry history.

### Level 3B1-B4: silent dead-connection fix (TCP keepalive)

Diagnosed a real production incident: after the Jetson's Internet WiFi network
changed, the telemetry publisher's existing WebSocket connection was orphaned
— confirmed via `ss -tn`, its local socket address was still bound to the
*previous* network's now-invalid source IP, with data backed up unsent in the
kernel's TCP send buffer (`Send-Q` non-zero and growing). Neither the
application (`send()` kept "succeeding" — the OS accepted the bytes into its
buffer without error) nor the `websockets` library's default ping/pong
keepalive (`ping_interval=20s`, `ping_timeout=20s`) detected this for over 15
minutes, because the ping/pong frames suffer the identical fate — they queue
into the same doomed buffer instead of reaching the peer. Detection only
happens once Linux's own TCP retransmission-timeout logic eventually gives up,
which defaults to many minutes.

**Fix** (`telemetry/publisher.py:enable_aggressive_tcp_keepalive`): immediately
after connecting, configure kernel-level TCP keepalive on the raw socket
(`SO_KEEPALIVE` + `TCP_KEEPIDLE=5s` + `TCP_KEEPINTVL=3s` + `TCP_KEEPCNT=3`),
so the *kernel itself* probes for and detects an unreachable peer in
~5 + 3×3 = 14 seconds, surfacing it as a connection error that triggers the
existing (already-correct) reconnect/backoff logic much sooner. Verified live:
after applying the fix and restarting the publisher, its socket correctly
bound to the new network's current IP, and two samples taken 150 seconds
apart showed the sequence number advancing by ~1,577 (≈10.5 msg/s, matching
the 10 Hz target) with fresh timestamps and fresh `cameras.front`/`system`
data each time — confirming the earlier stale-cached-state failure mode no
longer reproduces.

## Cross-network test topology (Level 3B1-B4)

Validated test topology, all traffic routed through the public OCI relay —
no local peer-to-peer path between the Jetson and the operator laptop:

```
Jetson (ATT WiFi)
    |
    v
Internet
    |
    v
OCI (147.224.145.24)
    |
    v
Internet
    |
    v
Operator/sender laptop (OU WiFi)
```

The Jetson and the operator laptop are deliberately on two different, unrelated
networks (ATT WiFi vs. OU WiFi) to validate that the visualization path works
correctly over the public Internet rather than only over shared local-network
conditions. The Septentrio GNSS receiver's dedicated network interface
(`enx1a3202991545`, `192.168.3.0/24`, the `septentrio` NetworkManager profile)
is entirely separate from the Jetson's Internet-facing WiFi and is not affected
by which WiFi network the Jetson uses for Internet access.

## Temporary communication-test camera placement

**LEFT and RIGHT cameras are currently temporarily located inside the vehicle**
(not in their final exterior mounting positions) purely for convenience during
communication/streaming testing — the Orin is inside the vehicle, and routing
camera cables through the windows for this phase of testing was impractical.
Their logical roles (`left`/`right`) and USB bus paths
(`usb-4.1.2.2`/`usb-2.3`, see `docs/CAMERA_TOPOLOGY.md`) are unchanged; only the
physical camera lens position/aim is temporary. This placement is sufficient
for validating the communication pipeline (capture → encode → SRT → OCI →
reader) but **not** for validating final exterior camera views — that requires
a separate visual role/view confirmation once the cameras are mounted in their
final exterior positions. If the USB physical ports change when the cameras
are moved to their final positions, the four-camera USB topology must be
revalidated (see the existing "DO NOT MOVE THE VALIDATED CAMERA CABLING
WITHOUT REVALIDATION" warning in `docs/CAMERA_TOPOLOGY.md`).

## FRONT configuration lock

The FRONT encoder/mux/SRT configuration (`poc-type=2`, `idrinterval=20`,
`iframeinterval=20`, `insert-sps-pps=true`, `h264parse config-interval=-1`,
explicit `video/x-h264,stream-format=byte-stream,alignment=au` caps,
`mpegtsmux alignment=7 pat-interval=9000 pmt-interval=9000`, 800x600@20fps) is
**locked** after a successful ~10-minute remote stability test: 12,170 decoded
frames over 610 seconds, ~20 FPS decoded/wall-clock, 0 reordered-frame errors,
0 unexpected reader disconnects, 0 reconnects, 0 MediaMTX PES errors. Do not
modify this configuration without a documented reason and a fresh validation
pass.

## Minimal mode (Level 3C1-A)

Field testing over ATT WiFi and Starlink showed the full four-camera mode
(800x600@20fps/4Mbps per camera, ~16-18Mbps aggregate) is not reliable enough on
the available mobile uplink. **Minimal mode** is a separate, coexisting operating
mode — CABIN video only + GNSS-only telemetry — for use when uplink is
constrained (~5Mbps or lower). It does not replace or modify full mode; both
share the same underlying code (`CameraPublisher`, `TelemetryPublisher`,
resolver, reconnect logic), selected via config file / constructor flags.

**Video:** 640x480@10fps (a directly-supported discrete CABIN capture mode — no
rate-limiting stage needed), 800kbps, 1-second keyframe interval
(`idrinterval=10`/`iframeinterval=10` — not the full-mode value of 20, which
would be a 2-second GOP at this frame rate). Same stability settings as full
mode: `poc-type=2`, `insert-sps-pps=true`, `h264parse config-interval=-1`,
explicit H.264 caps, `mpegtsmux alignment=7`. Config: `config/video_minimal.yaml`
(full mode's `config/video.yaml` is untouched).

**Telemetry:** ~5Hz (vs. full mode's ~10Hz), GNSS only — no `cameras`, no
`system`, and IMU/Xsens heading is never consulted (even when available) so
`heading_source` is only ever `GNSS_COURSE` or `UNAVAILABLE`, never `IMU`. Same
`schema_version=1`/`message_type="vehicle_visualization"` contract, same
existing GNSS field names/units (`horizontal_speed_mps` stays in m/s — no
duplicate `speed_mph` field).

**Startup:** `./scripts/run_minimal_visualization.sh` (refuses to start if any
full-mode publisher, or another telemetry publisher, is already running — it
never kills an unrelated process to make room). **Stop:**
`./scripts/stop_minimal_visualization.sh` (targets only the CABIN and
minimal-mode telemetry processes).

**Validated:** ~22-minute continuous run, zero CABIN/telemetry errors or
reconnects, ~1.08-1.19 Mbps average/peak outbound throughput (target: near
1Mbps, comfortably under 2Mbps) — well within margin for a constrained uplink.

## Two-camera mode (Level 3C2-A)

**Two-camera mode** extends one-camera minimal mode with a second video
stream — logical FRONT + CABIN + GNSS-only telemetry — for uplinks that can
sustain a bit more than the ~1Mbps one-camera minimal mode but still can't
support the full four-camera mode. It coexists with, and does not modify,
either full mode or one-camera minimal mode.

**Role correction:** the user visually verified that the physical camera
config/cameras.yaml's full four-camera mapping calls **"left"** (USB path
`usb-4.1.2.2`) is actually the **front-facing** camera. `config/cameras.yaml`
itself is unchanged — full four-camera mode still calls that same physical
camera "left" at that same USB path. Two-camera mode instead loads a
dedicated override mapping, `config/cameras_two_camera.yaml`:

```yaml
front:
  usb_path: "usb-4.1.2.2"   # physically-verified front camera (was "left")
cabin:
  usb_path: "usb-4.2"       # same physical cabin camera as every other mode
```

`CameraPublisher` gained a `camera_config_filename` constructor/CLI parameter
(mirroring the existing `video_config_filename` minimal-mode parameter,
defaulting to `cameras.yaml` for full backward compatibility) — the role
correction is purely a matter of which mapping file a given publisher
instance loads; `video/camera_resolver.py`'s USB-topology resolution logic
itself is unchanged. The OCI SRT path and passphrase are keyed on the
publisher's logical `--role` (`front`), not on which physical camera is
behind it, so the operator still receives this stream as `front` and the
existing `FRONT_SRT_PUBLISH_PASSPHRASE` secret still applies unchanged.

**Video:** both cameras use the same settings as one-camera minimal mode —
640x480@10fps, 800kbps, `idrinterval=10`/`iframeinterval=10`, `poc-type=2`,
`insert-sps-pps=true`, `h264parse config-interval=-1`, explicit H.264 caps,
`mpegtsmux alignment=7`. Both load `config/video_minimal.yaml` — no new video
settings file was needed. Nominal load: ~0.8Mbps each, ~1.6Mbps aggregate
before SRT/mux overhead.

**Telemetry:** reuses the existing one-camera minimal mode's GNSS-only ~5Hz
telemetry (`--mode minimal`) completely unchanged — no cameras block, no
system block, no IMU/Xsens. Adding a second camera does not change telemetry
behavior in any way.

**Startup:** `./scripts/run_two_camera_visualization.sh` — starts exactly two
video publishers (logical front, cabin) plus one minimal telemetry publisher.
Refuses to start (rather than killing anything) if any of front/left/right/
cabin video publisher or any telemetry publisher is already running —
"left" is checked because it is the *same physical camera* as this mode's
logical front and cannot be opened twice. **Stop:**
`./scripts/stop_two_camera_visualization.sh` (targets only this mode's front
and cabin processes, distinguished from other modes' publishers by the
`--camera-config cameras_two_camera.yaml` argument, plus the shared minimal
telemetry process).

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
