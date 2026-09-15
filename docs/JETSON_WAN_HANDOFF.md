# Jetson WAN interface handoff

This document is the non-secret interface contract for implementing the Jetson
visualization publisher. It describes the interfaces deployed and validated in
Level 3B1-A. Placeholder names refer to variables already present in the
out-of-repository handoff file. No credential value appears here.

> **CONTROL SEPARATION**
>
> UDP 4210 is reserved for the G29/ESP32 control plane. Visualization software
> must never use UDP 4210, send `CMD`, `MANEUVER`, or `BUTTON` packets, depend on
> the ESP32, or become feedback for vehicle control. Camera, GNSS, telemetry,
> Internet, and GUI failures must not block or alter control transmission.

## 1. OCI endpoints

| Interface | Public endpoint | Transport | Purpose |
|---|---|---|---|
| MediaMTX SRT | `147.224.145.24:8890` | UDP/SRT | Four camera streams |
| Visualization telemetry | `ws://147.224.145.24:8767/` | TCP/WebSocket | GNSS, camera, and Jetson health |
| Protected control relay | `147.224.145.24:4210` | UDP | **CONTROL ONLY — visualization must not use it** |

MediaMTX is version 1.21.0. The telemetry endpoint currently uses plain
`ws://` with role-specific token authentication because no trusted TLS
certificate/domain is deployed. A later hardening phase should replace it with
`wss://` without changing the message contract.

## 2. SRT camera contract

The four case-sensitive path names are:

| Camera | MediaMTX path | Jetson passphrase variable | Reader passphrase variable |
|---|---|---|---|
| FRONT | `front` | `FRONT_SRT_PUBLISH_PASSPHRASE` | `FRONT_SRT_READ_PASSPHRASE` |
| LEFT | `left` | `LEFT_SRT_PUBLISH_PASSPHRASE` | `LEFT_SRT_READ_PASSPHRASE` |
| RIGHT | `right` | `RIGHT_SRT_PUBLISH_PASSPHRASE` | `RIGHT_SRT_READ_PASSPHRASE` |
| CABIN | `cabin` | `CABIN_SRT_PUBLISH_PASSPHRASE` | `CABIN_SRT_READ_PASSPHRASE` |

### Proven publisher URI syntax

The successful external test used MediaMTX's custom SRT stream ID:

```text
srt://147.224.145.24:8890?streamid=publish:<path>:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<PATH_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
```

Exact per-camera templates:

```text
srt://147.224.145.24:8890?streamid=publish:front:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<FRONT_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
srt://147.224.145.24:8890?streamid=publish:left:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<LEFT_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
srt://147.224.145.24:8890?streamid=publish:right:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<RIGHT_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
srt://147.224.145.24:8890?streamid=publish:cabin:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<CABIN_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316
```

The `streamid` components are, in order:

```text
publish : path : publisher username : publisher password
```

The SRT encryption passphrase is a separate `passphrase` query parameter. It
must not be embedded inside `streamid`. `pbkeylen=32` selects a 256-bit SRT key.
The validated publisher used `pkt_size=1316`.

No explicit `mode` or `latency` query option was used or required by the
successful test. The client used the normal SRT caller behavior and MediaMTX
used listener mode. Do not add an unvalidated latency override in the first
Jetson implementation.

### Proven reader URI syntax

```text
srt://147.224.145.24:8890?streamid=read:<path>:<MEDIA_READER_USERNAME>:<MEDIA_READER_PASSWORD>&passphrase=<PATH_SRT_READ_PASSPHRASE>&pbkeylen=32
```

Exact per-camera templates:

```text
srt://147.224.145.24:8890?streamid=read:front:<MEDIA_READER_USERNAME>:<MEDIA_READER_PASSWORD>&passphrase=<FRONT_SRT_READ_PASSPHRASE>&pbkeylen=32
srt://147.224.145.24:8890?streamid=read:left:<MEDIA_READER_USERNAME>:<MEDIA_READER_PASSWORD>&passphrase=<LEFT_SRT_READ_PASSPHRASE>&pbkeylen=32
srt://147.224.145.24:8890?streamid=read:right:<MEDIA_READER_USERNAME>:<MEDIA_READER_PASSWORD>&passphrase=<RIGHT_SRT_READ_PASSPHRASE>&pbkeylen=32
srt://147.224.145.24:8890?streamid=read:cabin:<MEDIA_READER_USERNAME>:<MEDIA_READER_PASSWORD>&passphrase=<CABIN_SRT_READ_PASSPHRASE>&pbkeylen=32
```

The reader uses its own MediaMTX account and its own per-path SRT passphrase.
Publisher credentials cannot be substituted for reader credentials.

## 3. Media payload contract

The successful Level 3B1-A synthetic publisher sent:

```text
H.264 video -> MPEG-TS muxing -> SRT transport
```

The validated FFmpeg encoder used YUV420p, a periodic IDR/keyframe every ten
frames at 10 FPS (approximately once per second), scene-cut keyframes disabled,
and MPEG-TS header resending:

```bash
ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=320x240:rate=10 \
  -t 18 \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -pix_fmt yuv420p \
  -g 10 -keyint_min 10 -sc_threshold 0 \
  -mpegts_flags resend_headers \
  -f mpegts \
  "srt://147.224.145.24:8890?streamid=publish:front:<MEDIA_PUBLISHER_USERNAME>:<MEDIA_PUBLISHER_PASSWORD>&passphrase=<FRONT_SRT_PUBLISH_PASSPHRASE>&pbkeylen=32&pkt_size=1316"
```

The first synthetic attempt allowed the server to recognize H.264, but a
reader joining after publication could not begin decoding promptly when it had
missed the initial keyframe and stream headers. The passing test therefore
established these requirements:

- Generate periodic IDR/keyframes. Do not emit only an initial IDR.
- Repeat SPS/PPS with IDR access points so a late reader can initialize.
- Repeat the MPEG-TS program headers so a late reader can discover the stream.
- Keep each camera stream independently decodable after a reader reconnects.
- Do not build an unbounded video backlog; live visualization should resume at
  current video.

The Jetson's current hardware pipeline ends with:

```text
nvv4l2h264enc -> h264parse
```

It must add an MPEG-TS muxer before SRT:

```text
nvv4l2h264enc
  -> h264parse
  -> mpegtsmux
  -> srtsink
```

Configure `nvv4l2h264enc` to generate periodic IDRs and insert/repeat SPS/PPS.
Configure `h264parse` to repeat codec configuration with IDRs where supported
(for the standard GStreamer parser this is `config-interval=-1`). The required
post-parser element is `mpegtsmux`; feeding elementary H.264 directly from
`h264parse` to `srtsink` does not satisfy the validated payload contract.
Jetson-specific encoder property names must be confirmed against the installed
JetPack/GStreamer plugin before choosing values. The first Jetson test should
retain approximately one-second IDR spacing until late-reader attachment is
verified.

## 4. Secret variable contract

These are the exact variable names in the existing local handoff file. Values
must be transferred securely; do not print the file.

| Variable | Classification | Use |
|---|---|---|
| `OCI_VISUALIZATION_HOST` | Jetson required; remote-reader required | Public OCI host |
| `SRT_PORT` | Jetson required; remote-reader required | SRT UDP port |
| `TELEMETRY_PORT` | Jetson required; remote-reader required | WebSocket TCP port |
| `MEDIA_PUBLISHER_USERNAME` | Jetson required | MediaMTX publish account |
| `MEDIA_PUBLISHER_PASSWORD` | Jetson required | MediaMTX publish password |
| `MEDIA_READER_USERNAME` | Remote-reader required | MediaMTX read account |
| `MEDIA_READER_PASSWORD` | Remote-reader required | MediaMTX read password |
| `FRONT_SRT_PUBLISH_PASSPHRASE` | Jetson required | FRONT SRT encryption |
| `FRONT_SRT_READ_PASSPHRASE` | Remote-reader required | FRONT SRT decryption |
| `LEFT_SRT_PUBLISH_PASSPHRASE` | Jetson required | LEFT SRT encryption |
| `LEFT_SRT_READ_PASSPHRASE` | Remote-reader required | LEFT SRT decryption |
| `RIGHT_SRT_PUBLISH_PASSPHRASE` | Jetson required | RIGHT SRT encryption |
| `RIGHT_SRT_READ_PASSPHRASE` | Remote-reader required | RIGHT SRT decryption |
| `CABIN_SRT_PUBLISH_PASSPHRASE` | Jetson required | CABIN SRT encryption |
| `CABIN_SRT_READ_PASSPHRASE` | Remote-reader required | CABIN SRT decryption |
| `TELEMETRY_PUBLISHER_TOKEN` | Jetson required | Publisher WebSocket authentication |
| `TELEMETRY_SUBSCRIBER_TOKEN` | Remote-reader required | Subscriber WebSocket authentication |
| `TELEMETRY_HOST` | Server only | OCI bind address; do not use as a client destination |

The Jetson needs only variables classified **Jetson required**. It should not
receive reader credentials or the server bind variable.

## 5. Jetson secret file

Store the securely transferred Jetson subset at:

```text
/home/mila/.config/teleop_visualization/oci_visualization.env
```

Create the parent directory with mode `0700` and the file with mode `0600`.

- Never commit this file.
- Never echo its values into logs.
- Never place credentials directly in source code.
- Avoid placing expanded secret values in command output or shell tracing.
- Disable `set -x` in scripts that construct SRT URIs.
- Complete SRT URIs are secret because their query strings contain credentials
  and encryption passphrases.

## 6. Telemetry WebSocket contract

The externally validated endpoint is:

```text
ws://147.224.145.24:8767/
```

The service has no additional WebSocket path. The external smoke test used the
authority form `ws://147.224.145.24:8767`, which resolves to the root path `/`.

The client must send exactly one authentication message immediately after the
WebSocket opens. The authentication timeout is five seconds.

Jetson publisher authentication:

```json
{"type":"auth","role":"publisher","token":"<PUBLISHER_TOKEN>"}
```

Successful publisher acknowledgement:

```json
{"type":"auth_ok","role":"publisher"}
```

Remote subscriber authentication:

```json
{"type":"auth","role":"subscriber","token":"<SUBSCRIBER_TOKEN>"}
```

Successful subscriber acknowledgement:

```json
{"type":"auth_ok","role":"subscriber"}
```

Authentication failure closes the WebSocket with policy-violation close code
`1008`. Missing/late authentication, invalid message shape, invalid token, and
a second active publisher are rejected. At most one publisher may be active.

After authentication, an invalid telemetry message does not intentionally
close the publisher connection. It receives:

```json
{"type":"error","reason":"<NON_SECRET_REASON>"}
```

A newly authenticated subscriber receives the cached latest valid message, if
one exists, after its `auth_ok` acknowledgement.

## 7. Telemetry schema version 1

### Required and enforced by the current relay

Every telemetry message must be a UTF-8 JSON object no larger than 65,536
bytes and must contain:

| Field | Required value |
|---|---|
| `schema_version` | JSON number `1` |
| `message_type` | JSON string `"vehicle_visualization"` |

These are the only application fields currently enforced by the relay.

### Optional and accepted by the current relay

The relay preserves additional JSON-compatible fields without interpreting
them. The Jetson implementation should use the following stable structure so
future subscribers do not have to guess. These fields are optional to the
current server, although the Jetson should populate them when data is
available:

```json
{
  "schema_version": 1,
  "message_type": "vehicle_visualization",
  "source_id": "jetson_onboard",
  "sequence": 123,
  "timestamp_utc": "2030-01-01T00:00:00.000Z",
  "timestamp_monotonic_s": 456.75,
  "gnss": {
    "timestamp_utc": "2030-01-01T00:00:00.000Z",
    "timestamp_monotonic_s": 456.74,
    "latitude_deg": 0.001,
    "longitude_deg": 0.002,
    "altitude_m": 12.5,
    "horizontal_speed_mps": 3.2,
    "heading_deg": 87.0,
    "heading_source": "GNSS_COURSE",
    "satellite_count": 9,
    "horizontal_accuracy_m": 1.8,
    "vertical_accuracy_m": 3.5,
    "fix_valid": true
  },
  "cameras": {
    "front": {
      "stream_path": "front",
      "healthy": true,
      "fps": 20.0,
      "last_frame_age_s": 0.04
    },
    "left": {
      "stream_path": "left",
      "healthy": true,
      "fps": 20.0,
      "last_frame_age_s": 0.05
    },
    "right": {
      "stream_path": "right",
      "healthy": true,
      "fps": 20.0,
      "last_frame_age_s": 0.05
    },
    "cabin": {
      "stream_path": "cabin",
      "healthy": true,
      "fps": 15.0,
      "last_frame_age_s": 0.06
    }
  },
  "system": {
    "uptime_s": 3600.0,
    "cpu_percent": 27.5,
    "memory_percent": 41.0,
    "temperature_c": 52.0,
    "telemetry_rate_hz": 10.0
  }
}
```

All numbers above are dummy demonstration values. They do not represent a real
location, vehicle, or measurement. Use UTC and monotonic timestamps together.
Use `null` or omit an optional measurement that is unavailable; do not invent
sensor values. `heading_source` should identify the actual source, for example
`GNSS_COURSE`, `IMU`, `FUSED`, or `UNAVAILABLE`.

### Not currently accepted

- A top-level JSON array, scalar, or null instead of an object.
- Missing or non-numeric `schema_version`, or any version other than `1`.
- Missing `message_type`, or a type other than `vehicle_visualization`.
- Invalid UTF-8 or malformed JSON.
- A serialized message larger than 65,536 bytes.
- Non-finite JSON numbers such as `NaN`, positive infinity, or negative
  infinity.
- `CMD`, `MANEUVER`, `BUTTON`, or any other control message family.

Unknown additional JSON keys are currently accepted; that does not authorize
using this channel for control.

## 8. Reconnect and backpressure contract

### Telemetry publisher

- Run telemetry publication independently of vehicle control.
- If the WebSocket drops or OCI is unreachable, keep acquiring the latest
  visualization state locally without building an unbounded transmit queue.
- Reconnect with bounded exponential backoff and jitter, for example starting
  near one second and capping near 30 seconds.
- After reconnecting, authenticate again, wait for `auth_ok`, and publish the
  newest complete state. Do not replay a telemetry history.
- Treat close code `1008` as an authentication/configuration error. Backing off
  indefinitely with bad credentials is preferable to a tight retry loop.
- Only one Jetson publisher connection may exist. Ensure the previous client is
  closed or dead before creating overlapping reconnect workers.

### SRT publishers

- Supervise the four camera pipelines independently. Failure of one camera
  must not stop the other cameras, telemetry, or vehicle control.
- On SRT failure, tear down and recreate only the failed visualization
  pipeline with bounded exponential backoff and jitter.
- Drop stale video rather than accumulating a backlog during disconnection.
- Resume with current frames and periodic IDR/SPS/PPS/MPEG-TS headers so a new
  reader can attach promptly.

### Relay behavior

- Each subscriber queue has size `1`.
- Latest state wins; an unsent old message is replaced by the new message.
- A subscriber send operation has a two-second timeout; a persistently slow
  subscriber is closed with code `1013`.
- The publisher does not wait for subscriber sends.
- Only the most recent telemetry state is retained in memory.
- No telemetry or GNSS history is persisted.

## 9. Sanitized client smoke tests

Load the applicable mode-0600 environment file without enabling shell tracing:

```bash
set -a
. /home/mila/.config/teleop_visualization/oci_visualization.env
set +a
```

### A. SRT publish smoke test

This is the sanitized form of the external test that passed. It publishes a
synthetic FRONT stream; it does not require a physical camera.

```bash
PUBLISH_URI="srt://${OCI_VISUALIZATION_HOST}:${SRT_PORT}?streamid=publish:front:${MEDIA_PUBLISHER_USERNAME}:${MEDIA_PUBLISHER_PASSWORD}&passphrase=${FRONT_SRT_PUBLISH_PASSPHRASE}&pbkeylen=32&pkt_size=1316"

ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=320x240:rate=10 \
  -t 18 \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -pix_fmt yuv420p \
  -g 10 -keyint_min 10 -sc_threshold 0 \
  -mpegts_flags resend_headers \
  -f mpegts "$PUBLISH_URI"
```

Do not print `PUBLISH_URI`; it contains secrets.

### B. SRT read smoke test

Run this while the publisher is active:

```bash
READ_URI="srt://${OCI_VISUALIZATION_HOST}:${SRT_PORT}?streamid=read:front:${MEDIA_READER_USERNAME}:${MEDIA_READER_PASSWORD}&passphrase=${FRONT_SRT_READ_PASSPHRASE}&pbkeylen=32"

ffmpeg -hide_banner -loglevel error \
  -i "$READ_URI" \
  -frames:v 20 \
  -f null -
```

Do not print `READ_URI`; it contains secrets.

### C. WebSocket publisher smoke test

This requires Python and `websockets==15.0.1` and uses the actual validated
authentication/message flow:

```bash
python3 - <<'PY'
import asyncio
import json
import os
from websockets.asyncio.client import connect

async def main():
    uri = f"ws://{os.environ['OCI_VISUALIZATION_HOST']}:{os.environ['TELEMETRY_PORT']}/"
    async with connect(uri, compression=None, open_timeout=5) as websocket:
        await websocket.send(json.dumps({
            "type": "auth",
            "role": "publisher",
            "token": os.environ["TELEMETRY_PUBLISHER_TOKEN"],
        }))
        acknowledgement = json.loads(await websocket.recv())
        assert acknowledgement == {"type": "auth_ok", "role": "publisher"}
        await websocket.send(json.dumps({
            "schema_version": 1,
            "message_type": "vehicle_visualization",
            "source_id": "jetson_smoke_test",
            "sequence": 1,
            "timestamp_utc": "2030-01-01T00:00:00.000Z",
            "gnss": {"fix_valid": False},
            "system": {"test": "sanitized_smoke"},
        }))

asyncio.run(main())
PY
```

### D. WebSocket subscriber smoke test

Run this before or during the publisher test. If a latest state is already
cached, it is delivered immediately after `auth_ok`:

```bash
python3 - <<'PY'
import asyncio
import json
import os
from websockets.asyncio.client import connect

async def main():
    uri = f"ws://{os.environ['OCI_VISUALIZATION_HOST']}:{os.environ['TELEMETRY_PORT']}/"
    async with connect(uri, compression=None, open_timeout=5) as websocket:
        await websocket.send(json.dumps({
            "type": "auth",
            "role": "subscriber",
            "token": os.environ["TELEMETRY_SUBSCRIBER_TOKEN"],
        }))
        acknowledgement = json.loads(await websocket.recv())
        assert acknowledgement == {"type": "auth_ok", "role": "subscriber"}
        message = json.loads(await asyncio.wait_for(websocket.recv(), 10))
        assert message["schema_version"] == 1
        assert message["message_type"] == "vehicle_visualization"
        print(json.dumps(message, indent=2))

asyncio.run(main())
PY
```

The subscriber example prints visualization data only. Do not include tokens or
authentication messages in diagnostic output.

## 10. Non-negotiable control separation

The Jetson visualization implementation must never:

- use UDP port 4210;
- send `CMD` packets;
- send `MANEUVER` packets;
- send `BUTTON` packets;
- import or invoke the G29 sender or control relay;
- depend on the ESP32;
- feed camera, GNSS, telemetry, or GUI state into vehicle control;
- allow visualization reconnects, encoding, or backpressure to block control.

Implement camera pipelines and telemetry as visualization-only services with
independent lifecycle, resource bounds, and failure handling.
