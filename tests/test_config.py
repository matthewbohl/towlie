from pathlib import Path

import pytest

from towelbar_agent.config import ConfigError, load_config


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_minimal_config(tmp_path: Path):
    config = load_config(
        write_config(
            tmp_path,
            """
mqtt:
  host: mqtt.local
controllers:
  - id: bath
    ssid: emmesteel-123
    password: test-password
""",
        )
    )
    assert config.wifi_interface == "wlan0"
    assert config.controllers[0].password == "test-password"
    assert config.controllers[0].protocol is None


def test_duplicate_controller_ids_are_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        """
mqtt:
  host: mqtt.local
controllers:
  - {id: bath, ssid: one, password: test-password}
  - {id: bath, ssid: two, password: test-password}
""",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_controller_password_is_required(tmp_path: Path):
    path = write_config(
        tmp_path,
        """
mqtt:
  host: mqtt.local
controllers:
  - {id: bath, ssid: EMMESTEEL_TEST}
""",
    )
    with pytest.raises(ConfigError, match="password is required"):
        load_config(path)


def test_example_config_loads():
    config = load_config(Path(__file__).parents[1] / "config.example.yaml")
    assert len(config.controllers) == 2
    assert config.controllers[0].protocol is None
    assert config.controllers[0].driver == "emmesteel"


def test_loads_rotation_and_diagnostics_settings(tmp_path: Path):
    config = load_config(
        write_config(
            tmp_path,
            """
poll_interval_seconds: 99
rotation:
  target_revisit_seconds: 20
  settle_after_connect_seconds: 1.5
  retry_delay_seconds: 2
  retries: 1
diagnostics:
  enabled: true
  events_path: /tmp/towelbar-events.jsonl
  capture_network_on_failure: false
  retention_days: 3
  max_file_megabytes: 4
mqtt:
  host: mqtt.local
controllers:
  - id: bath
    ssid: EMMESTEEL_TEST
    password: test-password
""",
        )
    )
    assert config.rotation.target_revisit_seconds == 20
    assert config.rotation.settle_after_connect_seconds == 1.5
    assert config.rotation.retries == 1
    assert config.diagnostics.enabled is True
    assert config.diagnostics.events_path == "/tmp/towelbar-events.jsonl"
    assert config.diagnostics.capture_network_on_failure is False
