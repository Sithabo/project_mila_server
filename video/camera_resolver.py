"""Resolve a stable camera role (front/left/right/cabin) to the current
/dev/videoN *image* stream node, using USB bus-path identity rather than
/dev/videoN numbering.

/dev/videoN numbers are not stable across reboots or USB re-enumeration (see
docs/CAMERA_TOPOLOGY.md — this was observed directly during discovery). The
USB bus path (e.g. "usb-2.4") is the stable identity for a given physical
port. This module never hard-codes a /dev/videoN number.

Fails closed: if a role's bus path does not match exactly one camera, or if
the image node cannot be distinguished unambiguously from the metadata node,
resolution raises CameraResolutionError rather than guessing.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_HEADER_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<bus_info>[^)]+)\):$")
_DEVICE_RE = re.compile(r"^\t(/dev/video\d+)\s*$")


class CameraResolutionError(RuntimeError):
    """Raised when a camera role cannot be resolved unambiguously."""


@dataclass(frozen=True)
class ResolvedCamera:
    role: str
    usb_path: str
    image_node: str
    metadata_node: str | None


def load_camera_config(config_path: str | Path) -> dict[str, str]:
    """Load config/cameras.yaml into {role: usb_path}."""
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    result: dict[str, str] = {}
    for role, entry in raw.items():
        if not isinstance(entry, dict) or "usb_path" not in entry:
            raise CameraResolutionError(
                f"config entry for role {role!r} is missing 'usb_path'"
            )
        result[role] = str(entry["usb_path"])
    return result


def _list_devices(list_devices_output: str) -> dict[str, list[str]]:
    """Parse `v4l2-ctl --list-devices` output into {bus_info: [/dev/videoN, ...]}."""
    groups: dict[str, list[str]] = {}
    current_bus_info: str | None = None
    for line in list_devices_output.splitlines():
        header_match = _HEADER_RE.match(line)
        if header_match:
            current_bus_info = header_match.group("bus_info")
            groups.setdefault(current_bus_info, [])
            continue
        device_match = _DEVICE_RE.match(line)
        if device_match and current_bus_info is not None:
            groups[current_bus_info].append(device_match.group(1))
    return groups


def _run_list_devices() -> str:
    result = subprocess.run(
        ["v4l2-ctl", "--list-devices"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _is_image_capture_node(device_info_output: str) -> bool:
    """True if the `v4l2-ctl --device=X --info` Device Caps block advertises
    Video Capture (as opposed to only Metadata Capture)."""
    marker = "Device Caps"
    idx = device_info_output.find(marker)
    if idx == -1:
        return False
    block = device_info_output[idx:]
    lines = block.splitlines()[1:]
    caps: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            break
        caps.append(stripped)
    return "Video Capture" in caps


def _run_device_info(device: str) -> str:
    result = subprocess.run(
        ["v4l2-ctl", f"--device={device}", "--info"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def resolve_camera(
    role: str,
    usb_path: str,
    list_devices_output: str | None = None,
    device_info_fn=None,
) -> ResolvedCamera:
    """Resolve one role to its current image-stream /dev/videoN node.

    `list_devices_output` and `device_info_fn` are injectable for testing;
    in production they default to real `v4l2-ctl` subprocess calls.
    """
    if list_devices_output is None:
        list_devices_output = _run_list_devices()
    if device_info_fn is None:
        device_info_fn = _run_device_info

    groups = _list_devices(list_devices_output)

    matching_bus_infos = [
        bus_info for bus_info in groups if bus_info.endswith(usb_path)
    ]
    if len(matching_bus_infos) == 0:
        raise CameraResolutionError(
            f"role={role}: no camera found at usb_path={usb_path!r} "
            "(camera disconnected or moved?)"
        )
    if len(matching_bus_infos) > 1:
        raise CameraResolutionError(
            f"role={role}: usb_path={usb_path!r} matched multiple devices "
            f"({matching_bus_infos!r}) — ambiguous, refusing to guess"
        )
    bus_info = matching_bus_infos[0]
    candidate_nodes = groups[bus_info]
    if not candidate_nodes:
        raise CameraResolutionError(
            f"role={role}: bus_info={bus_info!r} has no /dev/video nodes"
        )

    image_nodes: list[str] = []
    metadata_nodes: list[str] = []
    for node in candidate_nodes:
        try:
            info = device_info_fn(node)
        except subprocess.CalledProcessError as exc:
            raise CameraResolutionError(
                f"role={role}: failed to query {node}: {exc}"
            ) from exc
        if _is_image_capture_node(info):
            image_nodes.append(node)
        else:
            metadata_nodes.append(node)

    if len(image_nodes) != 1:
        raise CameraResolutionError(
            f"role={role}: expected exactly one image-capture node under "
            f"bus_info={bus_info!r}, found {image_nodes!r} — refusing to guess"
        )

    resolved = ResolvedCamera(
        role=role,
        usb_path=usb_path,
        image_node=image_nodes[0],
        metadata_node=metadata_nodes[0] if len(metadata_nodes) == 1 else None,
    )
    logger.info(
        "resolved camera role=%s usb_path=%s image_node=%s metadata_node=%s",
        resolved.role,
        resolved.usb_path,
        resolved.image_node,
        resolved.metadata_node,
    )
    return resolved


def resolve_all(config_path: str | Path) -> dict[str, ResolvedCamera]:
    """Resolve every role in config/cameras.yaml. Fails closed per-role."""
    roles = load_camera_config(config_path)
    list_devices_output = _run_list_devices()
    return {
        role: resolve_camera(role, usb_path, list_devices_output=list_devices_output)
        for role, usb_path in roles.items()
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    default_config = Path(__file__).resolve().parent.parent / "config" / "cameras.yaml"
    for role_name, resolved_camera in resolve_all(default_config).items():
        print(
            f"{role_name}: image_node={resolved_camera.image_node} "
            f"usb_path={resolved_camera.usb_path}"
        )
