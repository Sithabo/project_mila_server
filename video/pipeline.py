"""Build the validated GStreamer pipeline argument list for one camera's WAN
publication: v4l2src -> MJPEG -> jpegdec -> I420 -> nvvidconv -> NVMM ->
nvv4l2h264enc -> h264parse -> explicit H.264 caps -> mpegtsmux -> srtsink.

This module never accepts or logs a complete SRT URI; the caller is
responsible for keeping the URI out of logs (see video/publisher.py).

Level 3B1-B2 fix: remote validation found MediaMTX logged MPEG-TS PES parsing
errors and decoded zero frames, even though SRT transport/auth were healthy.
Root cause: the pipeline left two things to implicit negotiation instead of
configuring them explicitly.

1. h264parse's output caps (stream-format/alignment) were not pinned before
   mpegtsmux, relying on whatever h264parse negotiated by default.
2. mpegtsmux's `alignment` property was left at its default (-1 = auto).
   `gst-inspect-1.0 mpegtsmux` documents this property directly: "-1 = auto,
   0 = all available packets, 7 for UDP streaming" — SRT runs over UDP, and
   7 * 188-byte TS packets = 1316 bytes, matching the validated SRT
   `pkt_size=1316` contract exactly. Leaving this at auto meant mpegtsmux was
   not producing buffers aligned to the transport's packet size, which is
   consistent with a receiver losing PES/TS packet sync.
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
    h264_stream_format: str
    h264_alignment: str
    mpegts_alignment: int
    mpegts_pat_interval_ticks: int
    mpegts_pmt_interval_ticks: int
    srt_pkt_size: int


def _build_common_encode_args(image_node: str, video_config: VideoConfig) -> list[str]:
    """The v4l2src..mpegtsmux portion shared by the local-file test pipeline
    and the real SRT publish pipeline, so both are guaranteed identical."""
    insert_sps_pps_value = "true" if video_config.insert_sps_pps else "false"
    return [
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
        # Explicit caps rather than implicit negotiation — see module
        # docstring. mpegtsmux's video/x-h264 sink pad template requires
        # exactly these two fields (confirmed via gst-inspect-1.0 mpegtsmux).
        (
            "video/x-h264,"
            f"stream-format={video_config.h264_stream_format},"
            f"alignment={video_config.h264_alignment}"
        ),
        "!",
        "mpegtsmux",
        f"alignment={video_config.mpegts_alignment}",
        f"pat-interval={video_config.mpegts_pat_interval_ticks}",
        f"pmt-interval={video_config.mpegts_pmt_interval_ticks}",
    ]


def build_gst_launch_args(image_node: str, video_config: VideoConfig, srt_uri: str) -> list[str]:
    """Return the argv list for gst-launch-1.0. Never join this into a shell
    string containing the URI — pass argv directly to subprocess so the URI
    never touches a shell (and therefore never a shell history/trace)."""
    return [
        "gst-launch-1.0",
        "-e",
        *_build_common_encode_args(image_node, video_config),
        "!",
        "srtsink",
        f"uri={srt_uri}",
    ]


def build_local_ts_test_args(image_node: str, video_config: VideoConfig, output_path: str) -> list[str]:
    """Same encode/mux chain as build_gst_launch_args, but terminated in a
    local .ts file instead of srtsink, for pre-WAN validation (Level 3B1-B2
    section 6)."""
    return [
        "gst-launch-1.0",
        "-e",
        *_build_common_encode_args(image_node, video_config),
        "!",
        "filesink",
        f"location={output_path}",
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
