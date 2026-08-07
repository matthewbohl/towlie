from __future__ import annotations

import json
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DiagnosticSettings:
    enabled: bool = False
    events_path: str = "/var/lib/towelbar-agent/diagnostics/events.jsonl"
    capture_network_on_failure: bool = True
    retention_days: int = 7
    max_file_megabytes: int = 20


class DiagnosticSink:
    def __init__(self, settings: DiagnosticSettings, force: bool = False):
        self.settings = settings
        self.enabled = settings.enabled or force
        self.path = Path(settings.events_path)

    def emit(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        event = {"timestamp": utc_now(), **event}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

    def _rotate_if_needed(self) -> None:
        limit = max(1, self.settings.max_file_megabytes) * 1024 * 1024
        try:
            if self.path.stat().st_size < limit:
                return
        except OSError:
            return
        rotated = self.path.with_suffix(".jsonl.1")
        try:
            rotated.unlink(missing_ok=True)
            self.path.replace(rotated)
        except OSError:
            pass


class AttemptTrace:
    def __init__(
        self,
        sink: DiagnosticSink,
        controller_id: str,
        ssid: str,
        mode: str = "normal",
        **fields: Any,
    ):
        self.sink = sink
        self.started = time.monotonic()
        self.event: dict[str, Any] = {
            "event": "poll_attempt",
            "attempt_id": os.urandom(6).hex(),
            "controller_id": controller_id,
            "ssid": ssid,
            "mode": mode,
            "phases": {},
            **fields,
        }
        self.current_phase: str | None = None

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        base_name = name
        suffix = 2
        while name in self.event["phases"]:
            name = f"{base_name}_{suffix}"
            suffix += 1
        started = time.monotonic()
        self.current_phase = name
        try:
            yield
        except Exception as exc:
            self.event["phases"][name] = {
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            raise
        else:
            self.event["phases"][name] = {
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "status": "ok",
            }
        finally:
            self.current_phase = None

    def update(self, **fields: Any) -> None:
        self.event.update(fields)

    def finish(self, success: bool, exc: Exception | None = None) -> dict[str, Any]:
        self.event["success"] = success
        self.event["total_ms"] = round((time.monotonic() - self.started) * 1000, 1)
        if exc is not None:
            self.event.update(
                failed_phase=self.current_phase or self._failed_phase(),
                error_type=type(exc).__name__,
                error=str(exc),
                errno=getattr(exc, "errno", None),
            )
        self.sink.emit(self.event)
        return self.event

    def _failed_phase(self) -> str:
        for name, value in reversed(list(self.event["phases"].items())):
            if value.get("status") == "failed":
                return name
        return "unknown"


def read_events(path: str | Path, hours: float = 24) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, hours))
    events: list[dict[str, Any]] = []
    for candidate in (Path(path).with_suffix(".jsonl.1"), Path(path)):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                observed = datetime.fromisoformat(event["timestamp"])
                if observed >= cutoff:
                    events.append(event)
            except (ValueError, TypeError, KeyError):
                continue
    return sorted(events, key=lambda item: item.get("timestamp", ""))


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = [item for item in events if item.get("event") == "poll_attempt"]
    groups: dict[str, list[dict[str, Any]]] = {}
    combinations: dict[str, list[dict[str, Any]]] = {}
    for item in attempts:
        controller = str(item.get("controller_id", "unknown"))
        groups.setdefault(controller, []).append(item)
        if item.get("mode") == "soak" and item.get("switch_interval_seconds") is not None:
            key = (
                f"{controller}|interval={item.get('switch_interval_seconds')}"
                f"|settle={item.get('settle_seconds')}"
            )
            combinations.setdefault(key, []).append(item)
    result = {
        "attempts": len(attempts),
        "controllers": {key: _aggregate(value) for key, value in groups.items()},
    }
    if combinations:
        result["soak_combinations"] = {
            key: _aggregate(value) for key, value in combinations.items()
        }
    return result


def _aggregate(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    failures: dict[str, int] = {}
    consecutive = maximum_consecutive = 0
    metrics: dict[str, list[float]] = {
        "total": [],
        "associate": [],
        "tcp_connect": [],
        "websocket_handshake": [],
        "first_state": [],
        "actual_revisit_seconds": [],
        "actual_switch_interval_seconds": [],
        "signal": [],
    }
    successes = 0
    for item in attempts:
        if item.get("success"):
            successes += 1
            consecutive = 0
        else:
            consecutive += 1
            maximum_consecutive = max(maximum_consecutive, consecutive)
            phase = str(item.get("failed_phase", "unknown"))
            failures[phase] = failures.get(phase, 0) + 1
        if isinstance(item.get("total_ms"), (int, float)):
            metrics["total"].append(float(item["total_ms"]))
        if isinstance(item.get("actual_revisit_seconds"), (int, float)):
            metrics["actual_revisit_seconds"].append(float(item["actual_revisit_seconds"]))
        if isinstance(item.get("actual_switch_interval_seconds"), (int, float)):
            metrics["actual_switch_interval_seconds"].append(
                float(item["actual_switch_interval_seconds"])
            )
        signal = item.get("network", {}).get("signal")
        if isinstance(signal, (int, float)):
            metrics["signal"].append(float(signal))
        phases = item.get("phases", {})
        for name in ("associate", "tcp_connect", "websocket_handshake", "first_state"):
            value = phases.get(name, {}).get("duration_ms")
            if isinstance(value, (int, float)):
                metrics[name].append(float(value))
    count = len(attempts)
    return {
        "attempts": count,
        "successes": successes,
        "success_rate_percent": round(100 * successes / count, 1) if count else None,
        "failures_by_phase": failures,
        "maximum_consecutive_failures": maximum_consecutive,
        "metrics": {
            key: {"p50": percentile(sorted(values), 0.50), "p95": percentile(sorted(values), 0.95)}
            for key, values in metrics.items()
            if values
        },
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, round((len(values) - 1) * quantile))
    return round(values[index], 1)


def randomized_soak_matrix(
    intervals: list[float], settle_seconds: list[float]
) -> list[tuple[float, float]]:
    matrix = [(interval, settle) for interval in intervals for settle in settle_seconds]
    random.shuffle(matrix)
    return matrix


SOAK_HEADER = (
    "TIME     SAMPLE CONTROLLER     RESULT          ACTIVE RAW_PWR SETTING "
    "HEATING TIMER STORED_C CURRENT   INTERVAL SETTLE TOTAL  SIGNAL"
)


def format_soak_event(event: dict[str, Any]) -> str | None:
    if event.get("mode") != "soak" or event.get("event") != "poll_attempt":
        return None
    state = event.get("state", {})
    result = "OK" if event.get("success") else f"FAIL@{event.get('failed_phase', 'unknown')}"
    signal = event.get("network", {}).get("signal")
    if signal is None:
        signal = event.get("failure_network", {}).get("signal")
    power = state.get("power")
    power_text = "on" if power is True else "off" if power is False else "?"
    heating = state.get("heating")
    heating_text = "yes" if heating is True else "no" if heating is False else "?"
    timer_active = state.get("timer_active")
    active = heating is True or timer_active is True
    active_text = "yes" if active else "no" if heating is False and timer_active is False else "?"
    current = state.get("current_temperature")
    sensor_enabled = state.get("temperature_sensor_enabled")
    current_text = (
        "disabled"
        if sensor_enabled is False
        else "unavailable"
        if current is None
        else f"{current}C"
    )
    requested_interval = event.get("switch_interval_seconds")
    fields = [
        str(event.get("timestamp", ""))[11:19] or "?",
        f"#{int(event.get('sample', 0)):03d}",
        str(event.get("controller_id", "?"))[:14],
        result[:15],
        active_text,
        power_text,
        str(state.get("heat_level", "?")),
        heating_text,
        f"{state.get('timer_minutes', '?')}m",
        f"{state.get('target_temperature', '?')}C",
        current_text,
        "warmup" if requested_interval is None else f"{requested_interval}s",
        f"{event.get('settle_seconds', '?')}s",
        f"{round(float(event.get('total_ms', 0)) / 1000, 1)}s",
        str(signal if signal is not None else "?"),
    ]
    widths = [8, 6, 14, 15, 6, 7, 7, 7, 6, 8, 9, 8, 6, 6, 6]
    line = " ".join(value.ljust(width) for value, width in zip(fields, widths)).rstrip()
    if event.get("error"):
        line += f"  {event['error']}"
    return line
