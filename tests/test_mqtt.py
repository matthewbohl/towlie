from dataclasses import replace
from pathlib import Path

from towelbar_agent.config import ProtocolProfile, load_config
from towelbar_agent.mqtt import HomeAssistantMqtt


def test_emmesteel_discovery_uses_climate_and_removes_raw_controls():
    config = load_config(Path(__file__).parents[1] / "config.example.yaml")
    mqtt = HomeAssistantMqtt(config)
    published = {}

    def capture(topic, payload, retain=False):
        published[topic] = (payload, retain)

    mqtt.publish = capture
    controller = replace(
        config.controllers[0],
        protocol=ProtocolProfile.from_dict(
            {
                "status": {"path": "/legacy/status", "encoding": "none"},
                "power": {"path": "/legacy/power", "encoding": "none"},
            }
        ),
    )
    mqtt.publish_discovery(controller)

    prefix = f"homeassistant/climate/towelbar_{controller.id}_climate/config"
    climate, retained = published[prefix]
    assert retained is True
    assert climate["modes"] == ["off", "heat"]
    assert climate["mode_command_topic"].endswith("/set/mode")
    assert climate["temperature_command_topic"].endswith(
        "/set/target_temperature"
    )
    assert climate["min_temp"] == 30
    assert climate["max_temp"] == 70

    raw_power = f"homeassistant/switch/towelbar_{controller.id}_power/config"
    raw_target = (
        f"homeassistant/number/towelbar_{controller.id}_target_temperature/config"
    )
    assert published[raw_power] == ("", True)
    assert published[raw_target] == ("", True)
