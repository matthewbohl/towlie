from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

from .config import AgentConfig
from .diagnostics import (
    AttemptTrace,
    DiagnosticSink,
    randomized_soak_matrix,
    read_events,
    summarize_events,
)
from .emmesteel import EmmeSteelController
from .network import NetworkManager, WifiLock


class SoakControl:
    def __init__(self, state_root: str | Path):
        self.root = Path(state_root) / "soak"
        self.request_path = self.root / "request.json"
        self.stop_path = self.root / "stop"
        self.state_path = self.root / "state.json"

    def request(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_path.unlink(missing_ok=True)
        self._write(self.request_path, payload)

    def take_request(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.request_path.read_text())
        except (OSError, ValueError):
            return None
        self.request_path.unlink(missing_ok=True)
        return payload

    def stop(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_path.touch()

    def should_stop(self) -> bool:
        return self.stop_path.exists()

    def status(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"status": "idle"}

    def set_status(self, **values: Any) -> None:
        current = self.status()
        current.update(values, updated_at=datetime.now(timezone.utc).isoformat())
        self._write(self.state_path, current)

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(path)


def run_soak(
    config: AgentConfig,
    network: NetworkManager,
    mqtt: Any,
    sink: DiagnosticSink,
    control: SoakControl,
    request: dict[str, Any],
    stop_event: Event,
    timer_settings: dict[str, dict[str, object]] | None = None,
) -> dict[str, Any]:
    duration_minutes = min(24 * 60, max(0.1, float(request.get("duration_minutes", 30))))
    intervals = _numbers(request.get("intervals", [10, 15, 20, 30, 45, 60]), 1, 3600)
    settles = _numbers(request.get("settle_seconds", [0, 0.5, 1, 2]), 0, 60)
    matrix = randomized_soak_matrix(intervals, settles)
    started_wall = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_minutes * 60
    sample = 0
    last_started: dict[str, float] = {}
    last_switch_started: float | None = None
    control.set_status(
        status="running",
        started_at=started_wall.isoformat(),
        duration_minutes=duration_minutes,
        intervals=intervals,
        settle_seconds=settles,
        samples=0,
    )
    sink.emit(
        {
            "event": "soak_started",
            "duration_minutes": duration_minutes,
            "intervals": intervals,
            "settle_seconds": settles,
        }
    )
    while time.monotonic() < deadline and not stop_event.is_set() and not control.should_stop():
        controller = config.controllers[sample % len(config.controllers)]
        if sample == 0:
            interval = None
            settle = config.rotation.settle_after_connect_seconds
        else:
            interval, settle = matrix[(sample - 1) % len(matrix)]
            if _wait(stop_event, control, min(deadline, time.monotonic() + interval)):
                break
        if time.monotonic() >= deadline:
            break
        now = time.monotonic()
        revisit = now - last_started[controller.id] if controller.id in last_started else None
        switch_elapsed = now - last_switch_started if last_switch_started is not None else None
        last_started[controller.id] = now
        last_switch_started = now
        trace = AttemptTrace(
            sink,
            controller.id,
            controller.ssid,
            mode="soak",
            sample=sample + 1,
            switch_interval_seconds=interval,
            actual_switch_interval_seconds=(
                round(switch_elapsed, 3) if switch_elapsed is not None else None
            ),
            settle_seconds=settle,
            actual_revisit_seconds=round(revisit, 3) if revisit is not None else None,
            status_only=True,
        )
        state = None
        try:
            with WifiLock(config.wifi_interface, config.connect_timeout_seconds + 2):
                gateway = network.connect(
                    controller.ssid,
                    controller.password,
                    config.connect_timeout_seconds,
                    settle_seconds=settle,
                    trace=trace,
                )
                client = EmmeSteelController(
                    controller.base_url or f"http://{gateway}/",
                    config.wifi_interface,
                    config.request_timeout_seconds,
                    controller.max_timer_minutes,
                    trace,
                )
                state = client.status()
            trace.update(state=state.as_dict())
            trace.finish(True)
            settings = (timer_settings or {}).get(
                controller.id,
                {"enabled": controller.default_timer_enabled, "minutes": controller.default_timer_minutes},
            )
            mqtt.publish_state(
                controller,
                state,
                "online",
                command_pending=controller.id in mqtt.pending_controller_ids(),
                default_timer_enabled=bool(settings["enabled"]),
                default_timer_minutes=int(settings["minutes"]),
            )
        except Exception as exc:
            if config.diagnostics.capture_network_on_failure:
                try:
                    trace.update(failure_network=network.snapshot(include_details=True))
                except Exception as snapshot_exc:
                    trace.update(snapshot_error=str(snapshot_exc))
            trace.finish(False, exc)
            mqtt.publish_state(controller, None, "error", str(exc))
        sample += 1
        control.set_status(samples=sample, last_controller=controller.id)

    stopped = stop_event.is_set() or control.should_stop()
    events = [
        event
        for event in read_events(config.diagnostics.events_path, hours=duration_minutes / 60 + 1)
        if event.get("mode") == "soak" and event.get("timestamp", "") >= started_wall.isoformat()
    ]
    report = summarize_events(events)
    report.update(
        status="stopped" if stopped else "completed",
        started_at=started_wall.isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_minutes=duration_minutes,
        samples=sample,
    )
    control.stop_path.unlink(missing_ok=True)
    control.set_status(**report)
    sink.emit({"event": "soak_finished", **report})
    return report


def _numbers(value: Any, minimum: float, maximum: float) -> list[float]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("soak values must be a list")
    result = [float(item) for item in value]
    if not result or any(item < minimum or item > maximum for item in result):
        raise ValueError(f"soak values must be between {minimum} and {maximum}")
    return result


def _wait(stop_event: Event, control: SoakControl, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if stop_event.wait(min(1, deadline - time.monotonic())) or control.should_stop():
            return True
    return False
