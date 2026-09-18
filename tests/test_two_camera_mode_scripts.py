"""Level 3C2-A: lightweight content checks on the two-camera-mode shell
scripts.

These are not full behavioral tests of the bash scripts (no test harness
executes them), but they guard against an accidental future edit silently
adding right/legacy-left-as-left to two-camera mode, breaking the role
correction, or introducing any control-path reference, by checking the
actual committed script text.
"""
from pathlib import Path

REPO_ROOT = Path("/home/mila/teleop_visualization_jetson")

FORBIDDEN_CONTROL_STRINGS = ["4210", "ESP32", "esp32", "G29", "g29", "CMD", "MANEUVER", "BUTTON"]


def _read(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text()


def test_run_two_camera_starts_logical_front_via_front_launcher():
    text = _read("run_two_camera_visualization.sh")
    assert "run_front_wan.sh" in text


def test_run_two_camera_starts_cabin():
    text = _read("run_two_camera_visualization.sh")
    assert "run_camera_wan.sh cabin" in text


def test_run_two_camera_does_not_start_right():
    text = _read("run_two_camera_visualization.sh")
    assert "run_camera_wan.sh right" not in text


def test_run_two_camera_does_not_start_legacy_left_wan_path():
    text = _read("run_two_camera_visualization.sh")
    assert "run_camera_wan.sh left" not in text


def test_run_two_camera_uses_two_camera_role_override_config():
    text = _read("run_two_camera_visualization.sh")
    assert "cameras_two_camera.yaml" in text


def test_run_two_camera_uses_minimal_video_settings():
    text = _read("run_two_camera_visualization.sh")
    assert "video_minimal.yaml" in text


def test_run_two_camera_uses_minimal_telemetry_mode():
    text = _read("run_two_camera_visualization.sh")
    assert "--mode minimal" in text


def test_run_two_camera_checks_for_conflicting_publishers():
    text = _read("run_two_camera_visualization.sh")
    # Must refuse to start rather than blindly killing anything, and must
    # check all four roles (including "left", the same physical camera as
    # this mode's logical front — cannot be opened twice).
    assert "pgrep" in text
    assert "Refusing to start" in text or "ERROR" in text
    assert "front left right cabin" in text


def test_stop_two_camera_only_targets_front_and_cabin_two_camera_variants():
    text = _read("stop_two_camera_visualization.sh")
    assert "cameras_two_camera" in text
    assert "video.publisher --role front" in text.replace("video\\.publisher", "video.publisher")
    assert "video.publisher --role cabin" in text.replace("video\\.publisher", "video.publisher")
    assert "telemetry.publisher --mode minimal" in text.replace(
        "telemetry\\.publisher", "telemetry.publisher"
    )
    # Must not touch right, or the legacy-left WAN path, at all.
    for other_role in ["right", "left"]:
        assert f"video.publisher --role {other_role}" not in text.replace(
            "video\\.publisher", "video.publisher"
        )


def test_two_camera_scripts_never_reference_control_path():
    for script_name in ["run_two_camera_visualization.sh", "stop_two_camera_visualization.sh"]:
        text = _read(script_name)
        for forbidden in FORBIDDEN_CONTROL_STRINGS:
            assert forbidden not in text, f"{script_name} must not reference {forbidden!r}"


def test_two_camera_role_config_never_references_control_path():
    text = (REPO_ROOT / "config" / "cameras_two_camera.yaml").read_text()
    for forbidden in FORBIDDEN_CONTROL_STRINGS:
        assert forbidden not in text, f"cameras_two_camera.yaml must not reference {forbidden!r}"


def test_two_camera_role_config_maps_front_to_physically_verified_path():
    """The core role correction: logical 'front' in this override file must
    resolve to usb-4.1.2.2 (the physically-verified front camera, previously
    labelled 'left' in the full four-camera config/cameras.yaml)."""
    text = (REPO_ROOT / "config" / "cameras_two_camera.yaml").read_text()
    assert "usb-4.1.2.2" in text


def test_two_camera_role_config_keeps_cabin_unchanged():
    text = (REPO_ROOT / "config" / "cameras_two_camera.yaml").read_text()
    assert "usb-4.2" in text


def test_full_mode_cameras_config_untouched_by_role_correction():
    """The role correction must live only in the new override file — the
    full four-camera config/cameras.yaml must still map 'left' to
    usb-4.1.2.2 and 'front' to its original usb-2.4, unmodified."""
    text = (REPO_ROOT / "config" / "cameras.yaml").read_text()
    assert "usb-2.4" in text
    assert "usb-4.1.2.2" in text


def test_one_camera_minimal_mode_scripts_still_exist_unmodified_in_shape():
    """Level 3C2-A must not remove or restructure the existing one-camera
    (CABIN-only) minimal mode."""
    run_text = _read("run_minimal_visualization.sh")
    assert "run_camera_wan.sh cabin" in run_text
    assert "run_camera_wan.sh front" not in run_text
    stop_text = _read("stop_minimal_visualization.sh")
    assert "video.publisher --role cabin" in stop_text.replace("video\\.publisher", "video.publisher")


def test_four_camera_full_mode_launchers_still_exist():
    """Level 3C2-A must not remove the full four-camera mode's launchers."""
    assert (REPO_ROOT / "scripts" / "run_front_wan.sh").exists()
    front_text = _read("run_front_wan.sh")
    assert "--role front" in front_text
    camera_wan_text = _read("run_camera_wan.sh")
    assert "left|right|cabin" in camera_wan_text
