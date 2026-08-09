from dataclasses import replace
from pathlib import Path

from towelbar_agent.config import ProtocolProfile, load_config
from towelbar_agent.mqtt import HomeAssistantMqtt


def test_emmesteel_discovery_uses_mode_only_climate_and_removes_unsupported_controls():
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
    assert "temperature_command_topic" not in climate
    assert climate["name"] == "Towel Bar Control"
    assert "current_temperature_topic" not in climate

    raw_power = f"homeassistant/switch/towelbar_{controller.id}_power/config"
    raw_target = (
        f"homeassistant/number/towelbar_{controller.id}_target_temperature/config"
    )
    old_current_temperature = (
        f"homeassistant/sensor/towelbar_{controller.id}_current_temperature/config"
    )
    assert published[raw_power] == ("", True)
    assert published[raw_target] == ("", True)
    assert published[old_current_temperature] == ("", True)


def test_failed_command_retries_are_bounded():
    config = load_config(Path(__file__).parents[1] / "config.example.yaml")
    mqtt = HomeAssistantMqtt(config)
    published = []
    mqtt.publish = lambda *args, **kwargs: published.append((args, kwargs))
    controller_id = config.controllers[0].id
    mqtt._pending.add(controller_id)

    assert mqtt.requeue(controller_id, {"heat_level": 3}) is True
    assert mqtt.requeue(controller_id, {"heat_level": 3}) is True
    assert mqtt.requeue(controller_id, {"heat_level": 3}) is False
    assert controller_id not in mqtt.pending_controller_ids()
    assert published[-1][0][1] == "OFF"
