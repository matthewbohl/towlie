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
from .diagnostics import AttemptTrace, DiagnosticSink
from .emmesteel import EmmeSteelController, EmmeSteelError
from .mqtt import HomeAssistantMqtt
from .network import NetworkManager, WifiLock, interface_transport
from .protocol import ControllerState, HttpController, ProtocolError
from .soak import SoakControl, run_soak

LOG = logging.getLogger(__name__)


class TowelBarAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.network = NetworkManager(config.wifi_interface)
        self.diagnostics = DiagnosticSink(config.diagnostics)
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
        self.soak_control = SoakControl(self.runtime_path.parent)
        self.last_attempt_started: dict[str, float] = {}
        self._load_runtime_state()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        self.mqtt.start()
        next_due = {controller.id: 0.0 for controller in self.config.controllers}
        try:
            while not self.stop_event.is_set():
                soak_request = self.soak_control.take_request()
                if soak_request:
                    run_soak(
                        self.config,
                        self.network,
                        self.mqtt,
                        DiagnosticSink(self.config.diagnostics, force=True),
                        self.soak_control,
                        soak_request,
                        self.stop_event,
                        self.timer_settings,
                    )
                    now = time.monotonic()
                    next_due = {controller.id: now for controller in self.config.controllers}
                    continue
                pending = self.mqtt.pending_controller_ids()
                controller = min(
                    self.config.controllers,
                    key=lambda item: (
                        item.id not in pending,
                        next_due[item.id],
                    ),
                )
                wait_seconds = 0 if controller.id in pending else max(
                    0, next_due[controller.id] - time.monotonic()
                )
                if wait_seconds:
                    self.wake_event.wait(wait_seconds)
                    self.wake_event.clear()
                    continue
                started = time.monotonic()
                self.poll(controller)
                next_due[controller.id] = (
                    started + self.config.rotation.target_revisit_seconds
                )
                self.wake_event.clear()
        finally:
            self.mqtt.stop()

    def _stop(self, signum: int, frame: object) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def poll(self, controller: ControllerConfig) -> None:
        LOG.info("Connecting to %s (%s)", controller.name, controller.ssid)
        started = time.monotonic()
        previous = self.last_attempt_started.get(controller.id)
        self.last_attempt_started[controller.id] = started
        last_error: Exception | None = None
        for attempt in range(self.config.rotation.retries + 1):
            trace = AttemptTrace(
                self.diagnostics,
                controller.id,
                controller.ssid,
                attempt=attempt + 1,
                actual_revisit_seconds=(
                    round(started - previous, 3) if previous is not None else None
                ),
                target_revisit_seconds=self.config.rotation.target_revisit_seconds,
            )
            try:
                with WifiLock(
                    self.config.wifi_interface,
                    self.config.connect_timeout_seconds + 2,
                ):
                    self._poll_once(controller, trace)
                trace.finish(True)
                return
            except Exception as exc:
                last_error = exc
                if self.config.diagnostics.capture_network_on_failure:
                    try:
                        trace.update(failure_network=self.network.snapshot(include_details=True))
                    except Exception as snapshot_exc:
                        trace.update(snapshot_error=str(snapshot_exc))
                trace.finish(False, exc)
                LOG.exception(
                    "Poll attempt %s/%s failed for %s during %s",
                    attempt + 1,
                    self.config.rotation.retries + 1,
                    controller.id,
                    trace.event.get("failed_phase", "unknown"),
                )
                if attempt < self.config.rotation.retries:
                    if self.stop_event.wait(self.config.rotation.retry_delay_seconds):
                        break
        cached = self._trusted_cached_state(controller.id)
        self.mqtt.publish_state(
            controller,
            cached[1] if cached else None,
            "stale" if cached else "error",
            str(last_error) if last_error else "poll interrupted",
            observed_at=cached[0] if cached else None,
        )

    def _poll_once(self, controller: ControllerConfig, trace: AttemptTrace) -> None:
        gateway = self.network.connect(
            controller.ssid,
            controller.password,
            self.config.connect_timeout_seconds,
            settle_seconds=self.config.rotation.settle_after_connect_seconds,
            trace=trace,
        )
        if controller.driver == "emmesteel":
            self._poll_emmesteel(controller, gateway, trace)
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
            with trace.phase("http_commands"):
                self._apply_commands(client, commands)
            with trace.phase("http_status"):
                state = client.status()
            self.mqtt.confirm(controller.id)
            self._record_state(controller.id, state)
            self.mqtt.publish_state(controller, state, "online")
        finally:
            client.close()

    def _poll_emmesteel(
        self, controller: ControllerConfig, gateway: str, trace: AttemptTrace
    ) -> None:
        base_url = controller.base_url or f"http://{gateway}/"
        client = EmmeSteelController(
            base_url,
            self.config.wifi_interface,
            self.config.request_timeout_seconds,
            controller.max_timer_minutes,
            trace,
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
            trace.update(state=state.as_dict(), command_names=sorted(commands))
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
