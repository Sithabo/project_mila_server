import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video.pipeline import VideoConfig, build_gst_launch_args, build_local_ts_test_args  # noqa: E402


def make_video_config(**overrides) -> VideoConfig:
    defaults = dict(
        width=800,
        height=600,
        framerate=20,
        bitrate=4000000,
        idr_interval_frames=20,
        iframe_interval_frames=20,
        insert_sps_pps=True,
        h264parse_config_interval=-1,
        h264_stream_format="byte-stream",
        h264_alignment="au",
        mpegts_alignment=7,
        mpegts_pat_interval_ticks=9000,
        mpegts_pmt_interval_ticks=9000,
        srt_pkt_size=1316,
    )
    defaults.update(overrides)
    return VideoConfig(**defaults)


def test_explicit_h264_caps_present_between_h264parse_and_mpegtsmux():
    # Level 3B1-B2 fix: h264parse's output caps must be pinned explicitly
    # (stream-format=byte-stream, alignment=au) rather than left to implicit
    # negotiation, matching mpegtsmux's video/x-h264 sink pad template.
    argv = build_gst_launch_args("/dev/video2", make_video_config(), "srt://host:1/")
    assert "video/x-h264,stream-format=byte-stream,alignment=au" in argv
    h264parse_idx = argv.index("h264parse")
    caps_idx = argv.index("video/x-h264,stream-format=byte-stream,alignment=au")
    mux_idx = argv.index("mpegtsmux")
    assert h264parse_idx < caps_idx < mux_idx


def test_mpegtsmux_alignment_is_seven_for_udp_streaming():
    # Confirmed via gst-inspect-1.0 mpegtsmux: "7 for UDP streaming"; SRT runs
    # over UDP and 7 * 188 = 1316 bytes, matching the SRT pkt_size contract.
    argv = build_gst_launch_args("/dev/video2", make_video_config(), "srt://host:1/")
    assert "alignment=7" in argv


def test_mpegtsmux_pat_pmt_interval_configured_explicitly():
    argv = build_gst_launch_args("/dev/video2", make_video_config(), "srt://host:1/")
    assert "pat-interval=9000" in argv
    assert "pmt-interval=9000" in argv


def test_idr_and_sps_pps_repetition_still_configured():
    argv = build_gst_launch_args("/dev/video2", make_video_config(), "srt://host:1/")
    assert "idrinterval=20" in argv
    assert "iframeinterval=20" in argv
    assert "insert-sps-pps=true" in argv
    assert "config-interval=-1" in argv


def test_local_ts_test_pipeline_uses_identical_encode_chain_as_wan_pipeline():
    wan_argv = build_gst_launch_args("/dev/video2", make_video_config(), "srt://host:1/")
    ts_argv = build_local_ts_test_args("/dev/video2", make_video_config(), "/tmp/test.ts")

    def up_to_mpegtsmux(argv: list[str]) -> list[str]:
        mux_idx = argv.index("mpegtsmux")
        # include the mpegtsmux element and its property args (next 3 tokens)
        return argv[: mux_idx + 4]

    assert up_to_mpegtsmux(wan_argv) == up_to_mpegtsmux(ts_argv)
    assert ts_argv[-2:] == ["filesink", "location=/tmp/test.ts"]
    assert "srtsink" not in ts_argv


def test_local_ts_pipeline_never_contains_a_uri():
    ts_argv = build_local_ts_test_args("/dev/video2", make_video_config(), "/tmp/test.ts")
    assert not any(arg.startswith("uri=") for arg in ts_argv)
