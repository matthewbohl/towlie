import json
from datetime import datetime, timedelta, timezone

from towelbar_agent.agent import TowelBarAgent
from towelbar_agent.config import load_config


def make_agent(tmp_path, monkeypatch, observed_at):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mqtt:
  host: mqtt.local
controllers:
  - id: bath
    ssid: EMMESTEEL_TEST
    password: test-password
"""
    )
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "controller_states": {
                    "bath": {
                        "observed_at": observed_at.isoformat(),
                        "state": {"power": True, "timer_minutes": 90},
                    }
                }
            }
        )
    )
    monkeypatch.setenv("TOWELBAR_RUNTIME_STATE", str(runtime_path))
    return TowelBarAgent(load_config(config_path))


def test_recent_runtime_state_is_trusted(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch, datetime.now(timezone.utc))
    cached = agent._trusted_cached_state("bath")
    assert cached is not None
    assert cached[1].power is True
    assert cached[1].timer_minutes == 90


def test_runtime_state_older_than_30_minutes_is_ignored(tmp_path, monkeypatch):
    agent = make_agent(
        tmp_path,
        monkeypatch,
        datetime.now(timezone.utc) - timedelta(minutes=31),
    )
    assert agent._trusted_cached_state("bath") is None
