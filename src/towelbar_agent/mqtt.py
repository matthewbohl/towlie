from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import AgentConfig, ControllerConfig
from .protocol import ControllerState

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Command:
    controller_id: str
    field: str
    value: Any


class HomeAssistantMqtt:
    def __init__(self, config: AgentConfig, on_command: Callable[[], None] | None = None):
        self.config = config
        self.controllers = {controller.id: controller for controller in config.controllers}
        self.on_command = on_command
        self.commands: queue.Queue[Command] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="towelbar-agent",
        )
        if config.mqtt.username:
            self.client.username_pw_set(
                config.mqtt.username, config.mqtt.password
            )
        self.client.will_set(
            f"{config.mqtt.topic_prefix}/availability",
            "offline",
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect(
            self.config.mqtt.host, self.config.mqtt.port, keepalive=60
        )
        self.client.loop_start()

    def stop(self) -> None:
        self.publish(
            f"{self.config.mqtt.topic_prefix}/availability",
            "offline",
            retain=True,
        )
        self.client.disconnect()
        self.client.loop_stop()

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"))
        self.client.publish(topic, payload, qos=1, retain=retain)

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code != 0:
            LOG.error("MQTT connection failed: %s", reason_code)
            return
        prefix = self.config.mqtt.topic_prefix
        client.subscribe(f"{prefix}/+/set/+", qos=1)
        self.publish(f"{prefix}/availability", "online", retain=True)
        for controller in self.config.controllers:
            self.publish_discovery(controller)
            if controller.id not in self.pending_controller_ids():
                self.publish(f"{prefix}/{controller.id}/pending", "OFF", retain=True)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        prefix = self.config.mqtt.topic_prefix.split("/")
        parts = message.topic.split("/")
        if parts[: len(prefix)] != prefix or len(parts) != len(prefix) + 3:
            return
        controller_id, marker, field = parts[-3:]
        if marker != "set" or controller_id not in self.controllers:
            return
        raw = message.payload.decode().strip()
        try:
            value: Any
            if field == "power":
                if raw.upper() not in {"ON", "OFF"}:
                    raise ValueError("power must be ON or OFF")
                value = raw.upper() == "ON"
            elif field in {
                "heat_level",
                "timer_minutes",
                "target_temperature",
                "default_timer_minutes",
            }:
                value = int(float(raw))
            elif field == "default_timer_enabled":
                if raw.upper() not in {"ON", "OFF"}:
                    raise ValueError("default timer must be ON or OFF")
                value = raw.upper() == "ON"
            else:
                return
            controller = self.controllers[controller_id]
            limits = {
                "heat_level": (0, 5),
                "timer_minutes": (0, controller.max_timer_minutes),
                "target_temperature": (30, 70),
                "default_timer_minutes": (1, controller.max_timer_minutes),
            }
            if field in limits and not limits[field][0] <= value <= limits[field][1]:
                raise ValueError(f"{field} is outside its configured range")
            self.commands.put(Command(controller_id, field, value))
            with self._pending_lock:
                self._pending.add(controller_id)
            self.publish(
                f"{self.config.mqtt.topic_prefix}/{controller_id}/pending",
                "ON",
                retain=True,
            )
            if self.on_command:
                self.on_command()
        except ValueError:
            LOG.warning("Ignoring invalid command %s=%r", field, raw)

    def drain_for(self, controller_id: str) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        postponed: list[Command] = []
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command.controller_id == controller_id:
                selected[command.field] = command.value
            else:
                postponed.append(command)
        for command in postponed:
            self.commands.put(command)
        return selected

    def requeue(self, controller_id: str, commands: dict[str, Any]) -> None:
        for field, value in commands.items():
            self.commands.put(Command(controller_id, field, value))

    def pending_controller_ids(self) -> set[str]:
        with self._pending_lock:
            return set(self._pending)

    def confirm(self, controller_id: str) -> None:
        with self._pending_lock:
            self._pending.discard(controller_id)
        self.publish(
            f"{self.config.mqtt.topic_prefix}/{controller_id}/pending",
            "OFF",
            retain=True,
        )

    def publish_discovery(self, controller: ControllerConfig) -> None:
        discovery = self.config.mqtt.discovery_prefix
        base = self.config.mqtt.topic_prefix
        device = {
            "identifiers": [f"towelbar_{controller.id}"],
            "name": controller.name,
            "manufacturer": "Amba Products",
            "model": "TDHC/TDHCR",
        }
        availability = {"availability_topic": f"{base}/availability"}
        entities = {
            ("switch", "power"): {
                "name": "Power",
                "command_topic": f"{base}/{controller.id}/set/power",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.power | upper }}",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
            ("number", "heat_level"): {
                "name": "Heat level",
                "command_topic": f"{base}/{controller.id}/set/heat_level",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.heat_level }}",
                "min": 0,
                "max": 5,
                "step": 1,
                "mode": "slider",
            },
            ("number", "target_temperature"): {
                "name": "Target temperature",
                "command_topic": f"{base}/{controller.id}/set/target_temperature",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.target_temperature }}",
                "min": 30,
                "max": 70,
                "step": 1,
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "mode": "slider",
            },
            ("number", "timer_minutes"): {
                "name": "Countdown",
                "command_topic": f"{base}/{controller.id}/set/timer_minutes",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.timer_minutes }}",
                "min": 0,
                "max": controller.max_timer_minutes,
                "step": 15,
                "unit_of_measurement": "min",
                "mode": "box",
            },
            ("switch", "default_timer_enabled"): {
                "name": "Default timer",
                "command_topic": f"{base}/{controller.id}/set/default_timer_enabled",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.default_timer_enabled | upper }}",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
            ("number", "default_timer_minutes"): {
                "name": "Default timer duration",
                "command_topic": f"{base}/{controller.id}/set/default_timer_minutes",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.default_timer_minutes }}",
                "min": 1,
                "max": controller.max_timer_minutes,
                "step": 5,
                "unit_of_measurement": "min",
                "mode": "box",
            },
            ("sensor", "current_temperature"): {
                "name": "Current temperature",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.current_temperature }}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
            },
            ("binary_sensor", "heating"): {
                "name": "Heating",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ 'ON' if value_json.heating else 'OFF' }}",
                "device_class": "heat",
            },
            ("binary_sensor", "command_pending"): {
                "name": "Command pending",
                "state_topic": f"{base}/{controller.id}/pending",
                "payload_on": "ON",
                "payload_off": "OFF",
                "entity_category": "diagnostic",
            },
            ("sensor", "last_seen"): {
                "name": "Last seen",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.last_seen }}",
                "device_class": "timestamp",
            },
            ("sensor", "connection"): {
                "name": "Connection",
                "state_topic": f"{base}/{controller.id}/state",
                "value_template": "{{ value_json.connection }}",
                "entity_category": "diagnostic",
            },
        }
        protocol = controller.protocol
        supported = {
            "power": controller.driver == "emmesteel" or bool(protocol and protocol.power),
            "heat_level": controller.driver == "emmesteel" or bool(protocol and protocol.heat_level),
            "target_temperature": controller.driver == "emmesteel",
            "timer_minutes": controller.driver == "emmesteel" or bool(protocol and protocol.timer_minutes),
            "default_timer_enabled": controller.driver == "emmesteel",
            "default_timer_minutes": controller.driver == "emmesteel",
            "current_temperature": controller.driver == "emmesteel",
            "heating": controller.driver == "emmesteel",
            "command_pending": True,
            "last_seen": True,
            "connection": True,
        }
        for (component, object_name), body in entities.items():
            if not supported[object_name]:
                continue
            unique = f"towelbar_{controller.id}_{object_name}"
            payload = {
                **body,
                **availability,
                "unique_id": unique,
                "object_id": unique,
                "device": device,
            }
            self.publish(
                f"{discovery}/{component}/{unique}/config", payload, retain=True
            )

    def publish_state(
        self,
        controller: ControllerConfig,
        state: ControllerState | None,
        connection: str,
        error: str | None = None,
        command_pending: bool = False,
        default_timer_enabled: bool | None = None,
        default_timer_minutes: int | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        payload = {
            "power": (
                "ON" if state and state.power is True
                else "OFF" if state and state.power is False
                else None
            ),
            "heat_level": state.heat_level if state else None,
            "timer_minutes": state.timer_minutes if state else None,
            "target_temperature": state.target_temperature if state else None,
            "current_temperature": state.current_temperature if state else None,
            "heating": state.heating if state else None,
            "timer_active": state.timer_active if state else None,
            "command_pending": command_pending,
            "default_timer_enabled": default_timer_enabled,
            "default_timer_minutes": default_timer_minutes,
            "last_seen": (
                (observed_at or datetime.now(timezone.utc)).isoformat()
                if state
                else None
            ),
            "connection": connection,
            "error": error,
        }
        self.publish(
            f"{self.config.mqtt.topic_prefix}/{controller.id}/state",
            payload,
            retain=True,
        )
