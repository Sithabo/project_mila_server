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

- **FRONT camera WAN video publisher**: implemented and validated end-to-end against
  the real OCI relay (real hardware H.264 via `nvv4l2h264enc`, real SRT publication,
  187.8s and 365.1s stable test runs, zero reconnects, zero errors). See
  `docs/WAN_PUBLISHER.md`.
- **Telemetry WebSocket publisher**: implemented and validated end-to-end against the
  real OCI relay (real GNSS/Xsens data, ~10 Hz, authenticated, zero relay errors,
  zero reconnects over a 5-minute concurrent run). See `docs/WAN_PUBLISHER.md`.
- **LEFT / RIGHT / CABIN WAN video**: not yet implemented (FRONT-only in this phase,
  by design).
- Hardware baseline, camera topology, ROS2 sensor interface, and encoder validation
  are documented in `docs/`.

## Development environment

See `docs/DEVELOPMENT_ENVIRONMENT.md` for how to set up and activate the project's
Python environment alongside ROS2 Jazzy.
