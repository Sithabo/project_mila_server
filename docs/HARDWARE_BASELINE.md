# Hardware Baseline (Validated)

This document records the onboard hardware and software baseline as validated during
the discovery phases (Levels 3B0 – 3B0E) prior to any WAN implementation work.

## Onboard computer

- NVIDIA Jetson AGX Orin Developer Kit
- 64 GB RAM
- aarch64
- Ubuntu 24.04.4 LTS
- L4T R39.2.0
- ROS2 Jazzy

## NVIDIA multimedia

- `nvidia-l4t-gstreamer` installed at the version matching the installed BSP
  (`39.2.0`, matching `nvidia-l4t-core`/`nvidia-l4t-multimedia`'s build timestamp)
- `nvv4l2h264enc` available (GStreamer element, hardware H.264 encoder)
- `nvv4l2h265enc` available (present, not yet functionally tested)
- `nvvidconv` available (hardware colorspace conversion / NVMM memory upload)

Prior to installing `nvidia-l4t-gstreamer`, none of the above elements existed, and
neither `h264_nvenc` (ffmpeg — wrong API, Jetson's iGPU does not implement the
discrete-GPU NVENC API) nor `h264_v4l2m2m` (ffmpeg — cannot discover Jetson's
non-standard `/dev/v4l2-nvenc` node) worked as hardware encoders. See
`docs/ENCODER_VALIDATION.md` for the full pipeline detail.

## Validated four-camera acquisition

- Four simultaneous Global Shutter Cameras (USB2-native UVC devices, VID:PID
  `32e4:0234`)
- 800x600, MJPEG, 20 FPS each
- 60-second simultaneous capture test passed for all four cameras
- Zero `ENOSPC` / V4L2 errors, zero dropped frames (1198–1200 frames per camera
  depending on the specific run)

This required a specific USB topology fix — see `docs/CAMERA_TOPOLOGY.md`. The
cameras are USB2-native and will never negotiate above 480 Mbps regardless of which
physical port type they're plugged into; the fix was separating which root USB
controller/branch each camera's traffic shares, not increasing per-camera
resolution/FPS margin.

## Validated four-camera H.264 hardware encoding

- Encoder: `nvv4l2h264enc` (confirmed via GStreamer element identity and the
  runtime `NvMMLiteOpen ... NVENC` hardware engine banner — not a software
  encoder)
- All four cameras simultaneously
- 800x600 @ 20 FPS
- 1200/1200 frames each over approximately 60 seconds
- Zero encoder errors
- Comfortable CPU/RAM/thermal headroom: CPU 8–31% per core (one core
  persistently pegged at 100% independent of encoding — USB isochronous
  handling overhead, present even at idle), GPU/encoder engine (`GR3D_FREQ`)
  peaking at 65% with clear headroom remaining, temperatures ~48–53°C with no
  thermal throttling
- Bitrate control confirmed accurate (tested 1–6 Mbps, within 1–3% of target)

## Known open items (not currently blockers)

- The hardware JPEG-decode path (`nvjpegdec`) fails to initialize
  (`NvMMLiteOpen` `BlockType=277` error) on this system as configured. JPEG
  decode currently uses software `jpegdec`, which is cheap relative to H.264
  encoding and was not the bottleneck in any test.
- `nvv4l2h265enc` is present but has not been functionally tested.
