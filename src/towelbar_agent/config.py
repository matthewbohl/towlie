from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .diagnostics import DiagnosticSettings


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RequestSpec:
    method: str = "GET"
    path: str = "/"
    encoding: str = "json"
    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RequestSpec | None":
        if value is None:
            return None
        return cls(
            method=str(value.get("method", "GET")).upper(),
            path=str(value.get("path", "/")),
            encoding=str(value.get("encoding", "json")),
            values=dict(value.get("values", {})),
        )


@dataclass(frozen=True)
class StateMapping:
    power: str | None = None
    heat_level: str | None = None
    timer_minutes: str | None = None


@dataclass(frozen=True)
class ProtocolProfile:
    status: RequestSpec
    power: RequestSpec | None = None
    heat_level: RequestSpec | None = None
    timer_minutes: RequestSpec | None = None
    state: StateMapping = field(default_factory=StateMapping)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProtocolProfile":
        status = RequestSpec.from_dict(value.get("status"))
        if status is None:
            raise ConfigError("protocol.status is required")
        mapping = value.get("state", {})
        return cls(
            status=status,
            power=RequestSpec.from_dict(value.get("power")),
            heat_level=RequestSpec.from_dict(value.get("heat_level")),
            timer_minutes=RequestSpec.from_dict(value.get("timer_minutes")),
            state=StateMapping(
                power=mapping.get("power"),
                heat_level=mapping.get("heat_level"),
                timer_minutes=mapping.get("timer_minutes"),
            ),
        )


@dataclass(frozen=True)
class ControllerConfig:
    id: str
    name: str
    ssid: str
    password: str
    base_url: str | None
    protocol: ProtocolProfile | None
    driver: str = "generic"
    default_timer_enabled: bool = True
    default_timer_minutes: int = 120
    max_timer_minutes: int = 240


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "towelbar"
    discovery_prefix: str = "homeassistant"


@dataclass(frozen=True)
class RotationConfig:
    target_revisit_seconds: float = 30
    settle_after_connect_seconds: float = 1
    retry_delay_seconds: float = 3
    retries: int = 0
    command_retries: int = 2


@dataclass(frozen=True)
class AgentConfig:
    wifi_interface: str
    poll_interval_seconds: float
    connect_timeout_seconds: float
    request_timeout_seconds: float
    mqtt: MqttConfig
    controllers: tuple[ControllerConfig, ...]
    rotation: RotationConfig = field(default_factory=RotationConfig)
    diagnostics: DiagnosticSettings = field(default_factory=DiagnosticSettings)


def load_config(path: str | Path) -> AgentConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    mqtt_raw = raw.get("mqtt", {})
    rotation_raw = raw.get("rotation", {})
    diagnostics_raw = raw.get("diagnostics", {})
    if not mqtt_raw.get("host"):
        raise ConfigError("mqtt.host is required")
    controllers: list[ControllerConfig] = []
    seen: set[str] = set()
    for item in raw.get("controllers", []):
        controller_id = str(item["id"])
        ssid = str(item["ssid"])
        if not item.get("password"):
            raise ConfigError(f"{controller_id}: controller password is required")
        if controller_id in seen:
            raise ConfigError(f"duplicate controller id: {controller_id}")
        seen.add(controller_id)
        controllers.append(
            ControllerConfig(
                id=controller_id,
                name=str(item.get("name", controller_id)),
                ssid=ssid,
                password=str(item["password"]),
                base_url=item.get("base_url"),
                protocol=(
                    ProtocolProfile.from_dict(item["protocol"])
                    if item.get("protocol")
                    else None
                ),
                driver=str(
                    item.get(
                        "driver",
                        "emmesteel" if ssid.lower().startswith("emmesteel") else "generic",
                    )
                ),
                default_timer_enabled=bool(item.get("default_timer_enabled", True)),
                default_timer_minutes=int(item.get("default_timer_minutes", 120)),
                max_timer_minutes=int(item.get("max_timer_minutes", 240)),
            )
        )
        controller = controllers[-1]
        if not 1 <= controller.default_timer_minutes <= controller.max_timer_minutes:
            raise ConfigError(
                f"{controller_id}: default_timer_minutes must be between 1 and max_timer_minutes"
            )
    if not controllers:
        raise ConfigError("at least one controller is required")
    return AgentConfig(
        wifi_interface=str(raw.get("wifi_interface", "wlan0")),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 30)),
        connect_timeout_seconds=float(raw.get("connect_timeout_seconds", 20)),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 8)),
        mqtt=MqttConfig(
            host=str(mqtt_raw["host"]),
            port=int(mqtt_raw.get("port", 1883)),
            username=mqtt_raw.get("username"),
            password=mqtt_raw.get("password"),
            topic_prefix=str(mqtt_raw.get("topic_prefix", "towelbar")).strip("/"),
            discovery_prefix=str(
                mqtt_raw.get("discovery_prefix", "homeassistant")
            ).strip("/"),
        ),
        controllers=tuple(controllers),
        rotation=RotationConfig(
            target_revisit_seconds=max(
                1,
                float(
                    rotation_raw.get(
                        "target_revisit_seconds", raw.get("poll_interval_seconds", 30)
                    )
                )
            ),
            settle_after_connect_seconds=max(
                0, float(rotation_raw.get("settle_after_connect_seconds", 1))
            ),
            retry_delay_seconds=max(
                0, float(rotation_raw.get("retry_delay_seconds", 3))
            ),
            retries=max(0, int(rotation_raw.get("retries", 0))),
            command_retries=max(0, int(rotation_raw.get("command_retries", 2))),
        ),
        diagnostics=DiagnosticSettings(
            enabled=bool(diagnostics_raw.get("enabled", False)),
            events_path=str(
                diagnostics_raw.get(
                    "events_path",
                    "/var/lib/towelbar-agent/diagnostics/events.jsonl",
                )
            ),
            capture_network_on_failure=bool(
                diagnostics_raw.get("capture_network_on_failure", True)
            ),
            retention_days=max(1, int(diagnostics_raw.get("retention_days", 7))),
            max_file_megabytes=max(
                1, int(diagnostics_raw.get("max_file_megabytes", 20))
            ),
        ),
    )
