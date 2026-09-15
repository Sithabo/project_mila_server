# ROS2 Sensor Interface (Validated)

> Do not change the ROS2 drivers or configs described here. This document only
> records how to consume the already-working sensor stack.

## Sensor startup

```bash
~/start_sensors.sh --no-lidar
```

This starts the Xsens IMU and Septentrio GNSS drivers (and vehicle TF) without
touching `end0`/LiDAR. See the script itself (`~/start_sensors.sh`) for the full
set of options.

## GNSS topics

| Topic               | Type                                        | Rate      |
|----------------------|---------------------------------------------|-----------|
| `/sensing/gnss/fix`  | `sensor_msgs/msg/NavSatFix`                 | ~10 Hz    |
| `/pvtgeodetic`       | `septentrio_gnss_driver/msg/PVTGeodetic`    | ~10 Hz    |

### Useful `PVTGeodetic` fields

- `latitude`, `longitude` — **radians**, not degrees (unlike `NavSatFix`, which is
  degrees)
- `vn`, `ve`, `vu` — North/East/Up velocity in m/s
- `nr_sv` — satellite count
- `h_accuracy`, `v_accuracy` — horizontal/vertical accuracy, in units of 0.01 m
  (divide by 100 for meters)
- `cog` — course over ground, degrees

### Derived values

Horizontal speed is not a direct field; compute it as:

```
speed = sqrt(vn**2 + ve**2)
```

`cog` (course over ground) **can be `NaN` while the vehicle is stationary or at low
speed** — this was observed directly during validation. Do not treat a NaN/invalid
`cog` as an error condition; it is expected at low speed for this single-antenna,
stand-alone receiver configuration.

**For low-speed/stationary heading, use the Xsens fused orientation instead of GNSS
course-over-ground.**

## Useful Xsens (IMU) topics

- `/filter/euler`
- `/filter/quaternion`
- `/imu/data_raw`
- `/imu/angular_velocity`
- `/imu/acceleration`

These provide a magnetometer+gyro fused heading that remains valid at zero speed,
unlike GNSS course-over-ground.

## Do not

- Do not change the Septentrio (`mila_septentrio_launch`) or Xsens
  (`mila_xsens_driver`) driver configs.
- Do not restart these nodes unnecessarily — if they are already running, leave
  them alone.
