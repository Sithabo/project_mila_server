import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video.camera_resolver import (  # noqa: E402
    CameraResolutionError,
    _is_image_capture_node,
    _list_devices,
    resolve_camera,
)

LIST_DEVICES_OK = """\
NVIDIA Tegra Video Input Device (platform:tegra-capture-vi):
\t/dev/media0

Global Shutter Camera: Global S (usb-3610000.usb-2.3):
\t/dev/video6
\t/dev/video7
\t/dev/media4

Global Shutter Camera: Global S (usb-3610000.usb-2.4):
\t/dev/video2
\t/dev/video3
\t/dev/media2

Global Shutter Camera: Global S (usb-3610000.usb-4.1.2.2):
\t/dev/video4
\t/dev/video5
\t/dev/media3

Global Shutter Camera: Global S (usb-3610000.usb-4.2):
\t/dev/video0
\t/dev/video1
\t/dev/media1
"""

IMAGE_INFO = """\
Driver Info:
\tDriver name      : uvcvideo
Device Caps      : 0x04200001
\t\tVideo Capture
\t\tStreaming
\t\tExtended Pix Format
"""

METADATA_INFO = """\
Driver Info:
\tDriver name      : uvcvideo
Device Caps      : 0x04a00000
\t\tMetadata Capture
\t\tStreaming
\t\tExtended Pix Format
"""


def fake_device_info(node: str) -> str:
    # Odd-numbered nodes are metadata in this fixture, matching real hardware
    # (e.g. video2 = image, video3 = metadata).
    number = int(node.rsplit("video", 1)[1])
    return METADATA_INFO if number % 2 == 1 else IMAGE_INFO


def test_list_devices_parses_groups():
    groups = _list_devices(LIST_DEVICES_OK)
    assert groups["usb-3610000.usb-2.4"] == ["/dev/video2", "/dev/video3"]
    assert groups["usb-3610000.usb-4.2"] == ["/dev/video0", "/dev/video1"]


def test_is_image_capture_node():
    assert _is_image_capture_node(IMAGE_INFO) is True
    assert _is_image_capture_node(METADATA_INFO) is False


def test_resolve_camera_happy_path():
    resolved = resolve_camera(
        "front",
        "usb-2.4",
        list_devices_output=LIST_DEVICES_OK,
        device_info_fn=fake_device_info,
    )
    assert resolved.image_node == "/dev/video2"
    assert resolved.metadata_node == "/dev/video3"
    assert resolved.role == "front"


def test_resolve_camera_missing_raises():
    with pytest.raises(CameraResolutionError, match="no camera found"):
        resolve_camera(
            "front",
            "usb-9.9",
            list_devices_output=LIST_DEVICES_OK,
            device_info_fn=fake_device_info,
        )


def test_resolve_camera_ambiguous_bus_path_raises():
    # A too-short suffix like "4" matches multiple bus_info strings that all
    # end with "4" (usb-2.4 and usb-4.1.2.2 does NOT end with "4", but craft a
    # fixture where two groups legitimately share a suffix).
    ambiguous_listing = LIST_DEVICES_OK + (
        "\nGlobal Shutter Camera: Global S (usb-9999.usb-2.4):\n"
        "\t/dev/video8\n\t/dev/video9\n"
    )
    with pytest.raises(CameraResolutionError, match="ambiguous"):
        resolve_camera(
            "front",
            "usb-2.4",
            list_devices_output=ambiguous_listing,
            device_info_fn=fake_device_info,
        )


def test_resolve_camera_no_image_node_raises():
    def all_metadata(_node: str) -> str:
        return METADATA_INFO

    with pytest.raises(CameraResolutionError, match="exactly one image-capture node"):
        resolve_camera(
            "front",
            "usb-2.4",
            list_devices_output=LIST_DEVICES_OK,
            device_info_fn=all_metadata,
        )


def test_resolve_camera_ambiguous_image_nodes_raises():
    def all_image(_node: str) -> str:
        return IMAGE_INFO

    with pytest.raises(CameraResolutionError, match="exactly one image-capture node"):
        resolve_camera(
            "front",
            "usb-2.4",
            list_devices_output=LIST_DEVICES_OK,
            device_info_fn=all_image,
        )
