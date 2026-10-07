# Camera Topology (Validated)

> **DO NOT MOVE THE VALIDATED CAMERA CABLING WITHOUT REVALIDATION.**
>
> The current physical USB layout was reached after multiple rounds of empirical
> testing (see the Level 3B0 / 3B0A / 3B0C discovery sessions). Moving any camera's
> physical USB connection requires re-running the four-camera simultaneous capture
> test before trusting the system again — the bottleneck is USB periodic-bandwidth
> sharing between whichever cameras share a root controller/hub branch, and that
> sharing pattern is exactly what was tuned to reach a working configuration.

## Update 2026-10-06: six-camera rig (current)

The rig was rebuilt with six "HD USB Camera" units, all behind one hub on USB
port 4.4. The two-root-branch layout described below no longer exists; it is
kept for history. Every camera was identified from a still frame and its
placement confirmed by the user.

| Camera        | USB bus path   | Full mode (`config/cameras.yaml`) | Two-camera mode |
|---------------|----------------|-----------------------------------|-----------------|
| Front (hood)  | `usb-4.4.2.2`  | FRONT                             | FRONT           |
| Cabin         | `usb-4.4.3.4`  | CABIN                             | CABIN           |
| Top left      | `usb-4.4.3.2`  | LEFT                              | —               |
| Top right     | `usb-4.4.2.1`  | RIGHT                             | —               |
| Back left     | `usb-4.4.4.2`  | —                                 | —               |
| Back right    | `usb-4.4.2.3`  | —                                 | —               |

These cameras offer MJPG 640x480 and 800x600 only at **30 fps** (15 fps only
at 1280x960 and above), so both video configs now request 30 fps; 10 or 20 fps
fails caps negotiation ("not-negotiated").

**Simultaneous four-camera test (2026-10-06):** FRONT, LEFT, RIGHT and CABIN
captured at the same time at 800x600 MJPG @ 30/1 (`gst-launch-1.0 … v4l2src …
num-buffers=150 … ! fakesink`, cameras selected by `/dev/v4l/by-path`): all
four streamed to completion, no `No space left on device`, no errors. Unlike
the old rig's shared-hub failures, this single-hub layout reserves enough
USB bandwidth for full mode. Caveat: indoors the cameras auto-exposed down to
about 12 fps actual; repeat the test outdoors at a real 30 fps before relying
on full mode in the field.

## Previous validated mapping (old four-camera rig, superseded)

| Role  | Device node   | USB bus path   |
|-------|---------------|----------------|
| CABIN | `/dev/video0` | `usb-4.2`      |
| FRONT | `/dev/video2` | `usb-2.4`      |
| LEFT  | `/dev/video4` | `usb-4.1.2.2`  |
| RIGHT | `/dev/video6` | `usb-2.3`      |

## Root-controller branch grouping

```
Root branch / Port 002:
- FRONT
- RIGHT

Root branch / Port 004:
- CABIN
- LEFT
```

This split across two separate root-controller branches is what makes simultaneous
four-camera capture at 800x600@20fps MJPEG work without USB isochronous-bandwidth
exhaustion. Earlier topologies where two cameras shared a single nested hub tier
alongside other active cameras reliably failed with `VIDIOC_STREAMON: No space left
on device` — confirmed to be a bandwidth/topology issue, not a broken camera,
insufficient resolution/FPS margin, or a CPU limitation (lowering resolution/FPS
did not help; only separating the root-controller branch did).

## `/dev/videoX` numbering is NOT stable

`/dev/video0`, `/dev/video2`, `/dev/video4`, `/dev/video6` are the node numbers
observed under the current, specific USB enumeration order. This numbering is
**not guaranteed to remain stable** after a reboot, a USB replug, or any other
re-enumeration event — device numbers have already been observed to shift between
sessions during discovery even when the physical cabling did not change (see the
3B0C discovery notes: LEFT's node shifted from `/dev/video6` to `/dev/video4` purely
because RIGHT's disconnect/reconnect elsewhere changed enumeration order, with no
cable touched on LEFT itself).

**Future implementation should identify cameras using stable USB
topology/udev information (e.g. `by-path` symlinks, USB VID:PID + bus-path
matching, or dedicated udev rules) rather than blindly hard-coding
`/dev/video0`, `/dev/video2`, `/dev/video4`, `/dev/video6`.**

All four cameras report an identical VID:PID (`32e4:0234`) and identical serial
string, so USB serial number cannot be used to distinguish them — role identity
must come from bus-path/topology, not device identity fields.
