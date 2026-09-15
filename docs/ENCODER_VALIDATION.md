# Hardware H.264 Encoder Validation

## Validated pipeline concept

```
v4l2src
  ->
MJPEG
  ->
jpegdec
  ->
I420
  ->
nvvidconv
  ->
video/x-raw(memory:NVMM)
  ->
nvv4l2h264enc
  ->
h264parse
```

## Successfully tested one-camera pipeline

```bash
gst-launch-1.0 -e \
  v4l2src device=/dev/video2 num-buffers=400 ! \
  image/jpeg,width=800,height=600,framerate=20/1 ! \
  jpegdec ! video/x-raw,format=I420 ! \
  nvvidconv ! video/x-raw\(memory:NVMM\),format=I420 ! \
  nvv4l2h264enc bitrate=4000000 ! \
  h264parse ! qtmux ! \
  filesink location=front_hw.mp4
```

Validated: `codec_name=h264`, resolution `800x600`, ~19.86 FPS (target 20), bitrate
within ~0.5% of the requested 4 Mbps, output file decodable via `ffprobe`/`ffmpeg`.

The four-camera version of this pipeline (one instance per camera, using the device
nodes and roles documented in `docs/CAMERA_TOPOLOGY.md`) was also validated for a
sustained 60-second run with all four cameras simultaneously — see
`docs/HARDWARE_BASELINE.md` for those results.

**`front_hw.mp4` and all other test output files are validation artifacts only and
are not, and must not be, committed to this repository** (see `.gitignore`).

## Implementation notes

- **JPEG decode currently uses software `jpegdec`.** This is a CPU-side decode step,
  not a hardware one.
- **H.264 encoding uses the Jetson hardware encoder, `nvv4l2h264enc`.** This is the
  part that matters for scaling to four simultaneous streams — verified via GStreamer
  element identity and the runtime `NvMMLiteOpen ... NVENC` hardware engine banner,
  and by CPU cost being roughly an order of magnitude lower than an equivalent
  software `libx264` stream (2–25% per core vs. ~230% of one core for one 720p30
  `libx264` stream measured earlier in discovery).
- **`nvjpegdec` (the hardware JPEG decoder) did not work** during discovery — its
  underlying hardware block (`NvMMLiteOpen`, `BlockType=277`) failed to open, and no
  further diagnostic detail was available without `sudo` access to `dmesg`.
- **This is not currently a blocker.** Software JPEG decode at these resolutions/
  frame rates is cheap and was never the bottleneck in any test — the actual
  encoding stage (the expensive part being offloaded) is confirmed real hardware.
  Revisiting `nvjpegdec` remains a worthwhile future optimization, not a
  prerequisite for moving forward.
