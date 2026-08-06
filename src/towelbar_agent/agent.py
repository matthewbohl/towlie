from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AgentConfig, ControllerConfig
from .emmesteel import EmmeSteelController, EmmeSteelError
from .mqtt import HomeAssistantMqtt
from .network import NetworkManager, interface_transport
from .protocol import ControllerState, HttpController, ProtocolError

LOG = logging.getLogger(__name__)


class TowelBarAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.network = NetworkManager(config.wifi_interface)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.mqtt = HomeAssistantMqtt(config, on_command=self.wake_event.set)
        self.timer_settings = {
            controller.id: {
                "enabled": controller.default_timer_enabled,
                "minutes": controller.default_timer_minutes,
            }
            for controller in config.controllers
        }
        self.cached_states: dict[str, tuple[datetime, ControllerState]] = {}
        self.runtime_path = Path(
            os.environ.get(
                "TOWELBAR_RUNTIME_STATE", "/var/lib/towelbar-agent/runtime.json"
            )
        )
        self._load_runtime_state()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.mqtt.start()
        try:
            while not self.stop_event.is_set():
                pending = self.mqtt.pending_controller_ids()
                controllers = sorted(
                    self.config.controllers,
                    key=lambda item: item.id not in pending,
                )
                for controller in controllers:
                    if self.stop_event.is_set():
                        break
                    self.poll(controller)
                self.wake_event.wait(self.config.poll_interval_seconds)
                self.wake_event.clear()
        finally:
            self.mqtt.stop()

    def _stop(self, signum: int, frame: object) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def poll(self, controller: ControllerConfig) -> None:
        LOG.info("Connecting to %s (%s)", controller.name, controller.ssid)
        try:
            gateway = self.network.connect(
                controller.ssid,
                controller.password,
                self.config.connect_timeout_seconds,
            )
            if controller.driver == "emmesteel":
                self._poll_emmesteel(controller, gateway)
                return
            if controller.protocol is None:
                self.mqtt.publish_state(
                    controller, None, "discovery_required", "protocol is not configured"
                )
                return
            base_url = controller.base_url or f"http://{gateway}/"
            client = HttpController(
                base_url,
                controller.protocol,
                self.config.request_timeout_seconds,
                transport=interface_transport(self.config.wifi_interface),
            )
            try:
                commands = self.mqtt.drain_for(controller.id)
                self._apply_commands(client, commands)
                state = client.status()
                self.mqtt.confirm(controller.id)
                self._record_state(controller.id, state)
                self.mqtt.publish_state(controller, state, "online")
            finally:
                client.close()
        except Exception as exc:
            LOG.exception("Poll failed for %s", controller.id)
            cached = self._trusted_cached_state(controller.id)
            self.mqtt.publish_state(
                controller,
                cached[1] if cached else None,
                "stale" if cached else "error",
                str(exc),
                observed_at=cached[0] if cached else None,
            )

    def _poll_emmesteel(self, controller: ControllerConfig, gateway: str) -> None:
        base_url = controller.base_url or f"http://{gateway}/"
        client = EmmeSteelController(
            base_url,
            self.config.wifi_interface,
            self.config.request_timeout_seconds,
            controller.max_timer_minutes,
        )
        commands = self.mqtt.drain_for(controller.id)
        settings = self.timer_settings[controller.id]
        if "default_timer_enabled" in commands:
            settings["enabled"] = bool(commands.pop("default_timer_enabled"))
        if "default_timer_minutes" in commands:
            minutes = int(commands.pop("default_timer_minutes"))
            if not 1 <= minutes <= controller.max_timer_minutes:
                raise ValueError(
                    f"default timer must be 1-{controller.max_timer_minutes} minutes"
                )
            settings["minutes"] = minutes
        if commands.get("power") is True and settings["enabled"]:
            commands.setdefault("timer_minutes", settings["minutes"])
        if commands.get("power") is False:
            commands.setdefault("timer_minutes", 0)

        try:
            state = client.apply(commands) if commands else client.status()
            needs_default_timer = (
                state.power is True
                and settings["enabled"]
                and state.timer_active is False
                and "timer_minutes" not in commands
            )
            if needs_default_timer:
                state = client.apply({"timer_minutes": settings["minutes"]})
            self._verify_commands(state, commands)
            self._record_state(controller.id, state)
            self.mqtt.confirm(controller.id)
            self.mqtt.publish_state(
                controller,
                state,
                "online",
                command_pending=False,
                default_timer_enabled=bool(settings["enabled"]),
                default_timer_minutes=int(settings["minutes"]),
            )
        except Exception:
            self.mqtt.requeue(controller.id, commands)
            raise

    @staticmethod
    def _verify_commands(state: object, commands: dict[str, object]) -> None:
        checks = {
            "power": "power",
            "heat_level": "heat_level",
            "target_temperature": "target_temperature",
        }
        for command, attribute in checks.items():
            if command in commands and getattr(state, attribute, None) != commands[command]:
                raise EmmeSteelError(f"controller did not confirm {command}")
        if "timer_minutes" in commands:
            expected_active = int(commands["timer_minutes"]) > 0
            if getattr(state, "timer_active", None) is not expected_active:
                raise EmmeSteelError("controller did not confirm timer state")

    def _load_runtime_state(self) -> None:
        try:
            payload = json.loads(self.runtime_path.read_text())
        except (OSError, ValueError):
            return
        for controller_id, saved in payload.get("timer_settings", {}).items():
            if controller_id not in self.timer_settings or not isinstance(saved, dict):
                continue
            current = self.timer_settings[controller_id]
            if isinstance(saved.get("enabled"), bool):
                current["enabled"] = saved["enabled"]
            if isinstance(saved.get("minutes"), int):
                current["minutes"] = saved["minutes"]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for controller_id, saved in payload.get("controller_states", {}).items():
            if controller_id not in self.timer_settings or not isinstance(saved, dict):
                continue
            try:
                observed_at = datetime.fromisoformat(saved["observed_at"])
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                if observed_at < cutoff:
                    continue
                state_data = saved["state"]
                state = ControllerState(
                    **{
                        key: state_data.get(key)
                        for key in ControllerState.__dataclass_fields__
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.cached_states[controller_id] = (observed_at, state)

    def _save_runtime_state(self) -> None:
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "timer_settings": self.timer_settings,
                    "controller_states": {
                        controller_id: {
                            "observed_at": observed_at.isoformat(),
                            "state": state.as_dict(),
                        }
                        for controller_id, (observed_at, state) in self.cached_states.items()
                    },
                },
                indent=2,
            )
            + "\n"
        )
        temporary.replace(self.runtime_path)

    def _record_state(self, controller_id: str, state: ControllerState) -> None:
        self.cached_states[controller_id] = (datetime.now(timezone.utc), state)
        self._save_runtime_state()

    def _trusted_cached_state(
        self, controller_id: str
    ) -> tuple[datetime, ControllerState] | None:
        cached = self.cached_states.get(controller_id)
        if cached is None:
            return None
        if datetime.now(timezone.utc) - cached[0] >= timedelta(minutes=30):
            self.cached_states.pop(controller_id, None)
            self._save_runtime_state()
            return None
        return cached

    @staticmethod
    def _apply_commands(client: HttpController, commands: dict[str, object]) -> None:
        if "power" in commands:
            client.set_power(bool(commands["power"]))
        if "heat_level" in commands:
            client.set_heat_level(int(commands["heat_level"]))
        if "timer_minutes" in commands:
            client.set_timer_minutes(int(commands["timer_minutes"]))
