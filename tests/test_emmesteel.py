import pytest

from towelbar_agent.emmesteel import EmmeSteelController, parse_state


def test_parse_state_decodes_temperatures_and_timer():
    state = parse_state(
        "DT:P0=1P2=4P3=100P4=90P5=1P7=1P8=1P9=29P10=15P11=1"
    )
    assert state.power is True
    assert state.heat_level == 4
    assert state.target_temperature == 40
    assert state.current_temperature == 35
    assert state.temperature_sensor_enabled is True
    assert state.timer_minutes == 90
    assert state.timer_active is True
    assert state.heating is True


def test_disabled_temperature_probe_does_not_publish_sentinel_value():
    state = parse_state("DT:P0=0P3=160P4=20P5=0P11=0")
    assert state.target_temperature == 70
    assert state.temperature_sensor_enabled is False
    assert state.current_temperature is None


def test_timer_safety_limit_is_enforced_before_request():
    controller = EmmeSteelController(
        "http://192.168.1.1/", "wlan0", max_timer_minutes=240
    )
    with pytest.raises(ValueError, match="240"):
        controller.set_timer(241)
