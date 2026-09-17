import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video.pipeline import build_publish_uri  # noqa: E402
from video.publisher import (  # noqa: E402
    CameraPublisher,
    PASSPHRASE_VAR_BY_ROLE,
    compute_backoff_delay,
    load_jetson_secrets,
    redact_argv,
)


def test_redact_argv_hides_uri_value():
    argv = ["gst-launch-1.0", "-e", "srtsink", "uri=srt://host:1234?passphrase=SECRET"]
    redacted = redact_argv(argv)
    assert redacted[-1] == "uri=<redacted>"
    assert "SECRET" not in " ".join(redacted)
    # Non-secret args are untouched.
    assert redacted[:3] == argv[:3]


def test_build_publish_uri_matches_proven_template():
    uri = build_publish_uri(
        host="203.0.113.10",
        port="8890",
        path="front",
        publisher_username="pubuser",
        publisher_password="pubpass",
        passphrase="frontpass",
        pbkeylen=32,
        pkt_size=1316,
    )
    assert uri == (
        "srt://203.0.113.10:8890?streamid=publish:front:pubuser:pubpass"
        "&passphrase=frontpass&pbkeylen=32&pkt_size=1316"
    )


def test_load_jetson_secrets_reads_only_required_names(tmp_path):
    secret_file = tmp_path / "oci_visualization.env"
    secret_file.write_text(
        "\n".join(
            [
                "OCI_VISUALIZATION_HOST=203.0.113.10",
                "SRT_PORT=8890",
                "MEDIA_PUBLISHER_USERNAME=pubuser",
                "MEDIA_PUBLISHER_PASSWORD=pubpass",
                "FRONT_SRT_PUBLISH_PASSPHRASE=frontpub",
                "LEFT_SRT_PUBLISH_PASSPHRASE=leftpub",
                "RIGHT_SRT_PUBLISH_PASSPHRASE=rightpub",
                "CABIN_SRT_PUBLISH_PASSPHRASE=cabinpub",
                # Reader/subscriber/server-only variables must never be
                # required or surfaced by this loader.
                "MEDIA_READER_USERNAME=readuser",
                "MEDIA_READER_PASSWORD=readpass",
                "FRONT_SRT_READ_PASSPHRASE=frontread",
                "TELEMETRY_SUBSCRIBER_TOKEN=subtoken",
                "TELEMETRY_HOST=0.0.0.0",
            ]
        )
    )
    secret_file.chmod(0o600)

    secrets = load_jetson_secrets(secret_file)

    assert secrets["OCI_VISUALIZATION_HOST"] == "203.0.113.10"
    assert "MEDIA_READER_USERNAME" not in secrets
    assert "MEDIA_READER_PASSWORD" not in secrets
    assert "FRONT_SRT_READ_PASSPHRASE" not in secrets
    assert "TELEMETRY_SUBSCRIBER_TOKEN" not in secrets
    assert "TELEMETRY_HOST" not in secrets


def test_load_jetson_secrets_missing_required_raises(tmp_path):
    secret_file = tmp_path / "incomplete.env"
    secret_file.write_text("OCI_VISUALIZATION_HOST=203.0.113.10\n")
    secret_file.chmod(0o600)

    with pytest.raises(RuntimeError, match="missing required variable"):
        load_jetson_secrets(secret_file)


def test_backoff_delay_is_bounded_and_increasing():
    delays = [compute_backoff_delay(attempt, rand=0.0) for attempt in range(1, 8)]
    # With rand=0.0 the multiplier is fixed at 0.5, isolating the exponential
    # growth and cap behavior.
    assert delays[0] == pytest.approx(0.5)
    assert delays[1] == pytest.approx(1.0)
    assert delays[2] == pytest.approx(2.0)
    # Capped at 30s base before the 0.5 multiplier -> 15s.
    assert max(delays) <= 15.0
    assert delays == sorted(delays)


def test_backoff_delay_jitter_range():
    low = compute_backoff_delay(1, rand=0.0)
    high = compute_backoff_delay(1, rand=0.999999)
    assert low < high
    # rand in [0, 1) maps the multiplier into [0.5, 1.5).
    assert low == pytest.approx(0.5)
    assert high == pytest.approx(1.5, rel=1e-3)


def test_write_status_produces_valid_json_telemetry_can_read(tmp_path):
    publisher = CameraPublisher(role="front", repo_root=tmp_path)
    publisher._write_status(healthy=True, fps=20.0)

    status_file = tmp_path / "runs" / "front_status.json"
    assert status_file.exists()
    data = json.loads(status_file.read_text())
    assert data["stream_path"] == "front"
    assert data["healthy"] is True
    assert data["fps"] == 20.0
    assert isinstance(data["updated_at_monotonic"], float)


def test_write_status_unhealthy_on_shutdown(tmp_path):
    publisher = CameraPublisher(role="front", repo_root=tmp_path)
    publisher._write_status(healthy=True, fps=20.0)
    publisher._write_status(healthy=False, fps=None)

    status_file = tmp_path / "runs" / "front_status.json"
    data = json.loads(status_file.read_text())
    assert data["healthy"] is False
    assert data["fps"] is None


# --- Level 3B2-A: multi-camera role support -------------------------------


def test_all_four_roles_have_a_distinct_passphrase_variable():
    assert PASSPHRASE_VAR_BY_ROLE == {
        "front": "FRONT_SRT_PUBLISH_PASSPHRASE",
        "left": "LEFT_SRT_PUBLISH_PASSPHRASE",
        "right": "RIGHT_SRT_PUBLISH_PASSPHRASE",
        "cabin": "CABIN_SRT_PUBLISH_PASSPHRASE",
    }
    # Each role's variable name must be unique — never share a passphrase
    # across two camera paths.
    assert len(set(PASSPHRASE_VAR_BY_ROLE.values())) == 4


@pytest.mark.parametrize("role", ["front", "left", "right", "cabin"])
def test_build_publish_uri_uses_role_as_srt_path(role):
    uri = build_publish_uri(
        host="203.0.113.10",
        port="8890",
        path=role,
        publisher_username="pubuser",
        publisher_password="pubpass",
        passphrase="rolepass",
        pbkeylen=32,
        pkt_size=1316,
    )
    assert f"streamid=publish:{role}:pubuser:pubpass" in uri


@pytest.mark.parametrize("role", ["front", "left", "right", "cabin"])
def test_camera_publisher_status_file_named_by_role(tmp_path, role):
    publisher = CameraPublisher(role=role, repo_root=tmp_path)
    publisher._write_status(healthy=True, fps=20.0)

    status_file = tmp_path / "runs" / f"{role}_status.json"
    assert status_file.exists()
    data = json.loads(status_file.read_text())
    assert data["stream_path"] == role


@pytest.mark.parametrize("role", ["front", "left", "right", "cabin"])
def test_camera_publisher_selects_correct_passphrase_variable_per_role(monkeypatch, tmp_path, role):
    cameras_config = tmp_path / "config"
    cameras_config.mkdir()
    (cameras_config / "cameras.yaml").write_text(
        "\n".join(f"{r}:\n  usb_path: usb-test-{r}" for r in PASSPHRASE_VAR_BY_ROLE)
    )
    (cameras_config / "video.yaml").write_text(
        "\n".join(
            [
                "capture: {width: 800, height: 600, framerate: 20}",
                "encoder: {bitrate: 4000000, idr_interval_frames: 20, "
                "iframe_interval_frames: 20, insert_sps_pps: true, poc_type: 2}",
                "h264parse: {config_interval: -1, stream_format: byte-stream, alignment: au}",
                "mpegts: {alignment: 7, pat_interval_ticks: 9000, pmt_interval_ticks: 9000}",
                "srt: {pkt_size: 1316, pbkeylen: 32}",
            ]
        )
    )

    # Entirely synthetic, non-secret test fixture values — never read from or
    # written to the real secret file. Monkeypatching the function itself
    # (not a module-level path constant) guarantees this, since a function's
    # default-argument value is bound once at definition time and would NOT
    # be affected by reassigning a module attribute after the fact.
    fake_secrets = {
        "OCI_VISUALIZATION_HOST": "203.0.113.10",
        "SRT_PORT": "8890",
        "MEDIA_PUBLISHER_USERNAME": "pubuser",
        "MEDIA_PUBLISHER_PASSWORD": "pubpass",
        "FRONT_SRT_PUBLISH_PASSPHRASE": "frontpass",
        "LEFT_SRT_PUBLISH_PASSPHRASE": "leftpass",
        "RIGHT_SRT_PUBLISH_PASSPHRASE": "rightpass",
        "CABIN_SRT_PUBLISH_PASSPHRASE": "cabinpass",
    }
    import video.publisher as publisher_module

    monkeypatch.setattr(publisher_module, "load_jetson_secrets", lambda: fake_secrets)

    publisher = CameraPublisher(role=role, repo_root=tmp_path)
    publisher._resolve_camera = lambda: type(
        "R", (), {"image_node": f"/dev/video_{role}"}
    )()

    argv, image_node, _video_config = publisher._build_argv()

    expected_passphrase = {
        "front": "frontpass",
        "left": "leftpass",
        "right": "rightpass",
        "cabin": "cabinpass",
    }[role]
    uri_arg = next(arg for arg in argv if arg.startswith("uri="))
    assert f"streamid=publish:{role}:pubuser:pubpass" in uri_arg
    assert f"passphrase={expected_passphrase}" in uri_arg
    assert image_node == f"/dev/video_{role}"


def test_locked_encoder_settings_identical_across_all_four_roles(tmp_path):
    """The FRONT encoder/mux configuration is locked; every role must produce
    byte-identical encoder/parser/mux settings — only the image node and SRT
    path/passphrase may differ."""
    from video.pipeline import build_gst_launch_args
    from video.publisher import load_video_config

    video_config = load_video_config(
        Path("/home/mila/teleop_visualization_jetson/config/video.yaml")
    )

    def encoder_mux_settings(argv):
        # Strip v4l2src device and srtsink uri — everything else must match.
        return [
            arg
            for arg in argv
            if not arg.startswith("device=") and not arg.startswith("uri=")
        ]

    reference = encoder_mux_settings(
        build_gst_launch_args("/dev/video0", video_config, "srt://host/front")
    )
    for role, node in [("left", "/dev/video1"), ("right", "/dev/video2"), ("cabin", "/dev/video3")]:
        other = encoder_mux_settings(
            build_gst_launch_args(node, video_config, f"srt://host/{role}")
        )
        assert other == reference, f"encoder/mux settings diverged for role={role}"


# --- Level 3C1-A: minimal mode config selection ----------------------------


def test_camera_publisher_defaults_to_full_mode_config():
    """Backward compatibility: constructing a CameraPublisher without
    specifying video_config_filename must keep using the locked full-mode
    config, exactly as before minimal mode existed."""
    publisher = CameraPublisher(role="cabin")
    assert publisher.video_config_filename == "video.yaml"


def test_camera_publisher_can_select_minimal_config():
    publisher = CameraPublisher(role="cabin", video_config_filename="video_minimal.yaml")
    assert publisher.video_config_filename == "video_minimal.yaml"


def test_cabin_minimal_publisher_uses_minimal_settings_and_stable_resolver(monkeypatch, tmp_path):
    """End-to-end (within _build_argv): minimal-mode CABIN publisher resolves
    via the same stable USB-topology resolver as full mode, and produces a
    pipeline using the minimal (640x480/10fps/800kbps) settings, not the
    locked full-mode settings."""
    cameras_config = tmp_path / "config"
    cameras_config.mkdir()
    (cameras_config / "cameras.yaml").write_text("cabin:\n  usb_path: usb-test-cabin\n")
    (cameras_config / "video_minimal.yaml").write_text(
        "\n".join(
            [
                "capture: {width: 640, height: 480, framerate: 10}",
                "encoder: {bitrate: 800000, idr_interval_frames: 10, "
                "iframe_interval_frames: 10, insert_sps_pps: true, poc_type: 2}",
                "h264parse: {config_interval: -1, stream_format: byte-stream, alignment: au}",
                "mpegts: {alignment: 7, pat_interval_ticks: 9000, pmt_interval_ticks: 9000}",
                "srt: {pkt_size: 1316, pbkeylen: 32}",
            ]
        )
    )
    fake_secrets = {
        "OCI_VISUALIZATION_HOST": "203.0.113.10",
        "SRT_PORT": "8890",
        "MEDIA_PUBLISHER_USERNAME": "pubuser",
        "MEDIA_PUBLISHER_PASSWORD": "pubpass",
        "CABIN_SRT_PUBLISH_PASSPHRASE": "cabinpass",
    }
    import video.publisher as publisher_module

    monkeypatch.setattr(publisher_module, "load_jetson_secrets", lambda: fake_secrets)

    publisher = CameraPublisher(
        role="cabin", repo_root=tmp_path, video_config_filename="video_minimal.yaml"
    )
    publisher._resolve_camera = lambda: type("R", (), {"image_node": "/dev/video_cabin"})()

    argv, image_node, video_config = publisher._build_argv()

    assert video_config.width == 640
    assert video_config.height == 480
    assert video_config.framerate == 10
    assert video_config.bitrate == 800000
    assert "streamid=publish:cabin:pubuser:pubpass" in next(
        arg for arg in argv if arg.startswith("uri=")
    )
    assert image_node == "/dev/video_cabin"
