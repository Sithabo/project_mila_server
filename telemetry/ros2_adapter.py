"""ROS2 subscriber adapter: consumes the already-validated GNSS/IMU topics and
exposes the latest values for the telemetry publisher. Does not implement any
new localization/fusion algorithm and does not modify any driver or config.

Subscribes to:
  /sensing/gnss/fix   sensor_msgs/msg/NavSatFix
  /pvtgeodetic        septentrio_gnss_driver/msg/PVTGeodetic
  /filter/euler       geometry_msgs/msg/Vector3Stamped (Xsens fused heading, z=yaw deg)
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import NavSatFix
from septentrio_gnss_driver.msg import PVTGeodetic
from geometry_msgs.msg import Vector3Stamped

from telemetry.schema import GnssSample

# A topic value older than this is treated as stale/unavailable rather than
# used, since a driver can stop publishing while its process stays alive
# (observed directly during validation: the Xsens process kept running with
# no actual message traffic).
STALE_AFTER_SECONDS = 2.0


def _ros_time_to_iso_utc(stamp) -> str:
    seconds = stamp.sec + stamp.nanosec / 1e9
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class TelemetrySourceNode(Node):
    def __init__(self):
        super().__init__("teleop_visualization_telemetry_source")
        self._latest_navsatfix: NavSatFix | None = None
        self._navsatfix_received_at: float | None = None
        self._latest_pvt: PVTGeodetic | None = None
        self._pvt_received_at: float | None = None
        self._latest_xsens_yaw_deg: float | None = None
        self._xsens_received_at: float | None = None

        self.create_subscription(NavSatFix, "/sensing/gnss/fix", self._on_navsatfix, 10)
        self.create_subscription(PVTGeodetic, "/pvtgeodetic", self._on_pvt, 10)
        self.create_subscription(Vector3Stamped, "/filter/euler", self._on_euler, 10)

    def _on_navsatfix(self, msg: NavSatFix) -> None:
        self._latest_navsatfix = msg
        self._navsatfix_received_at = time.monotonic()

    def _on_pvt(self, msg: PVTGeodetic) -> None:
        self._latest_pvt = msg
        self._pvt_received_at = time.monotonic()

    def _on_euler(self, msg: Vector3Stamped) -> None:
        self._latest_xsens_yaw_deg = msg.vector.z
        self._xsens_received_at = time.monotonic()

    def _is_fresh(self, received_at: float | None) -> bool:
        return received_at is not None and (time.monotonic() - received_at) <= STALE_AFTER_SECONDS

    def get_latest_gnss_sample(self) -> GnssSample | None:
        if not self._is_fresh(self._navsatfix_received_at):
            return None
        navsatfix = self._latest_navsatfix
        pvt = self._latest_pvt if self._is_fresh(self._pvt_received_at) else None

        return GnssSample(
            timestamp_utc=_ros_time_to_iso_utc(navsatfix.header.stamp),
            latitude_deg=navsatfix.latitude,
            longitude_deg=navsatfix.longitude,
            altitude_m=navsatfix.altitude,
            vn_mps=pvt.vn if pvt is not None else None,
            ve_mps=pvt.ve if pvt is not None else None,
            vu_mps=pvt.vu if pvt is not None else None,
            nr_sv=pvt.nr_sv if pvt is not None else None,
            h_accuracy_raw=pvt.h_accuracy if pvt is not None else None,
            v_accuracy_raw=pvt.v_accuracy if pvt is not None else None,
            cog_deg=pvt.cog if pvt is not None else math.nan,
            fix_valid=navsatfix.status.status >= 0,
        )

    def get_latest_xsens_heading_deg(self) -> float | None:
        if not self._is_fresh(self._xsens_received_at):
            return None
        return self._latest_xsens_yaw_deg


def spin_in_background(node: Node) -> None:
    """Helper for callers that want a background executor thread; the
    telemetry publisher owns its own event loop and drives rclpy.spin_once
    from within it instead, to keep everything on one thread."""
    rclpy.spin(node)
