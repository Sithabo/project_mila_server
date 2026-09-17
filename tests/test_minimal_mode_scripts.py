"""Level 3C1-A: lightweight content checks on the minimal-mode shell scripts.

These are not full behavioral tests of the bash scripts (no test harness
executes them), but they guard against an accidental future edit silently
adding front/left/right to minimal mode, or introducing any control-path
reference, by checking the actual committed script text.
"""
from pathlib import Path

REPO_ROOT = Path("/home/mila/teleop_visualization_jetson")

FORBIDDEN_CONTROL_STRINGS = ["4210", "ESP32", "esp32", "G29", "g29", "CMD", "MANEUVER", "BUTTON"]


def _read(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text()


def test_run_minimal_visualization_starts_cabin():
    text = _read("run_minimal_visualization.sh")
    assert "run_camera_wan.sh cabin" in text


def test_run_minimal_visualization_does_not_start_front():
    text = _read("run_minimal_visualization.sh")
    assert "run_camera_wan.sh front" not in text
    assert "run_front_wan.sh" not in text


def test_run_minimal_visualization_does_not_start_left_or_right():
    text = _read("run_minimal_visualization.sh")
    assert "run_camera_wan.sh left" not in text
    assert "run_camera_wan.sh right" not in text


def test_run_minimal_visualization_uses_minimal_video_config():
    text = _read("run_minimal_visualization.sh")
    assert "video_minimal.yaml" in text


def test_run_minimal_visualization_uses_minimal_telemetry_mode():
    text = _read("run_minimal_visualization.sh")
    assert "--mode minimal" in text


def test_run_minimal_visualization_checks_for_conflicting_publishers():
    text = _read("run_minimal_visualization.sh")
    # Must refuse to start rather than blindly killing anything.
    assert "pgrep" in text
    assert "Refusing to start" in text or "ERROR" in text


def test_stop_script_only_targets_cabin_and_minimal_telemetry():
    text = _read("stop_minimal_visualization.sh")
    assert "video.publisher --role cabin" in text.replace("video\\.publisher", "video.publisher")
    assert "telemetry.publisher --mode minimal" in text.replace(
        "telemetry\\.publisher", "telemetry.publisher"
    )
    # Must not touch other roles.
    for other_role in ["front", "left", "right"]:
        assert f"video.publisher --role {other_role}" not in text.replace(
            "video\\.publisher", "video.publisher"
        )


def test_minimal_scripts_never_reference_control_path():
    for script_name in ["run_minimal_visualization.sh", "stop_minimal_visualization.sh"]:
        text = _read(script_name)
        for forbidden in FORBIDDEN_CONTROL_STRINGS:
            assert forbidden not in text, f"{script_name} must not reference {forbidden!r}"


def test_minimal_video_config_never_references_control_path():
    text = (REPO_ROOT / "config" / "video_minimal.yaml").read_text()
    for forbidden in FORBIDDEN_CONTROL_STRINGS:
        assert forbidden not in text, f"video_minimal.yaml must not reference {forbidden!r}"
