# Teleop Visualization — Jetson Side

This repository is **only** the onboard Jetson visualization/communication side of the
Internet teleoperation project. It is responsible for capturing camera and GNSS/IMU
data on the vehicle's Jetson AGX Orin and, in a later phase, transporting that data to
a remote operator GUI over the Internet.

## Explicitly separate from

This project does **not** include, and must never depend on:

- the Logitech G29 control sender;
- the OCI UDP control relay;
- the vehicle's ESP32 drive-by-wire firmware;
- any drive-by-wire control logic.

The control path (G29 → sender laptop → OCI UDP relay → ESP32 → drive-by-wire) is a
separate, already-working system. **Visualization must never be required for the
vehicle control path to work**, and a failure anywhere in this repository's code must
never be able to stop or degrade control.

## Long-term architecture

```
4 cameras (FRONT / LEFT / RIGHT / CABIN)
        +
ROS2 GNSS (Septentrio) / IMU (Xsens)
        |
        v
     Jetson AGX Orin
        |
        v
independent Internet visualization transport
        |
        v
  remote operator GUI
```

The visualization path is read-only from the operator's perspective: live camera
video, GPS, heading, speed, and system status. It is not used as feedback for vehicle
control.

## Status

- **Four-camera WAN video publishers (FRONT/LEFT/RIGHT/CABIN)**: implemented and
  validated end-to-end against the real OCI relay — one shared implementation
  (`video/publisher.py:CameraPublisher`) parameterized by role, real hardware H.264
  via `nvv4l2h264enc` with the locked `poc-type=2` configuration, real SRT
  publication to all four MediaMTX paths. Staged validation (FRONT → +LEFT →
  +RIGHT → +CABIN) passed, followed by all four running simultaneously for
  ~50 minutes with zero encoder errors and exactly one isolated, self-recovered
  USB blip (single reconnect, no repeats). See `docs/WAN_PUBLISHER.md`.
- **Telemetry WebSocket publisher**: implemented and validated end-to-end against the
  real OCI relay — real GNSS/Xsens data, ~10 Hz, authenticated, `cameras.front`/
  `left`/`right`/`cabin`/`system` all live simultaneously, zero relay errors. Includes
  a kernel-level TCP keepalive fix (Level 3B1-B4) for a diagnosed real defect where a
  network interface change could silently orphan the connection for 15+ minutes.
  See `docs/WAN_PUBLISHER.md`.
- Hardware baseline, camera topology, ROS2 sensor interface, encoder validation,
  and overall backend architecture/latency optimization are documented in `docs/`
  (see `docs/BACKEND_ARCHITECTURE.md`).

## Development environment

See `docs/DEVELOPMENT_ENVIRONMENT.md` for how to set up and activate the project's
Python environment alongside ROS2 Jazzy.

