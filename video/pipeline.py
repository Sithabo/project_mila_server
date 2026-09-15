"""Build the validated GStreamer pipeline argument list for one camera's WAN
publication: v4l2src -> MJPEG -> jpegdec -> I420 -> nvvidconv -> NVMM ->
nvv4l2h264enc -> h264parse -> mpegtsmux -> srtsink.

This module never accepts or logs a complete SRT URI; the caller is
responsible for keeping the URI out of logs (see video/publisher.py).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoConfig:
    width: int
    height: int
    framerate: int
    bitrate: int
    idr_interval_frames: int
    iframe_interval_frames: int
    insert_sps_pps: bool
    h264parse_config_interval: int
    srt_pkt_size: int


def build_gst_launch_args(image_node: str, video_config: VideoConfig, srt_uri: str) -> list[str]:
    """Return the argv list for gst-launch-1.0. Never join this into a shell
    string containing the URI — pass argv directly to subprocess so the URI
    never touches a shell (and therefore never a shell history/trace)."""
    insert_sps_pps_value = "true" if video_config.insert_sps_pps else "false"
    return [
        "gst-launch-1.0",
        "-e",
        "v4l2src",
        f"device={image_node}",
        "!",
        (
            "image/jpeg,"
            f"width={video_config.width},"
            f"height={video_config.height},"
            f"framerate={video_config.framerate}/1"
        ),
        "!",
        "jpegdec",
        "!",
        "video/x-raw,format=I420",
        "!",
        "nvvidconv",
        "!",
        "video/x-raw(memory:NVMM),format=I420",
        "!",
        "nvv4l2h264enc",
        f"bitrate={video_config.bitrate}",
        f"idrinterval={video_config.idr_interval_frames}",
        f"iframeinterval={video_config.iframe_interval_frames}",
        f"insert-sps-pps={insert_sps_pps_value}",
        "!",
        "h264parse",
        f"config-interval={video_config.h264parse_config_interval}",
        "!",
        "mpegtsmux",
        "!",
        "srtsink",
        f"uri={srt_uri}",
    ]


def build_publish_uri(
    host: str,
    port: str,
    path: str,
    publisher_username: str,
    publisher_password: str,
    passphrase: str,
    pbkeylen: int,
    pkt_size: int,
) -> str:
    """Build the proven MediaMTX SRT publisher URI. The caller must never log
    or print the return value of this function."""
    return (
        f"srt://{host}:{port}?streamid=publish:{path}:{publisher_username}:"
        f"{publisher_password}&passphrase={passphrase}&pbkeylen={pbkeylen}"
        f"&pkt_size={pkt_size}"
    )
