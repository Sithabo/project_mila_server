import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video.pipeline import build_publish_uri  # noqa: E402
from video.publisher import compute_backoff_delay, load_jetson_secrets, redact_argv  # noqa: E402


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
