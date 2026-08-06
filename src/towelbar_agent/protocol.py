from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import ProtocolProfile, RequestSpec


class ProtocolError(RuntimeError):
    pass


@dataclass
class ControllerState:
    power: bool | None = None
    heat_level: int | None = None
    timer_minutes: int | None = None
    target_temperature: float | None = None
    current_temperature: float | None = None
    heating: bool | None = None
    timer_active: bool | None = None
    raw: Any = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def render_values(values: dict[str, Any], value: Any) -> dict[str, Any]:
    return {
        key: (
            item.replace("{value}", str(value))
            if isinstance(item, str)
            else item
        )
        for key, item in values.items()
    }


def dotted_get(payload: Any, path: str | None) -> Any:
    if path is None:
        return None
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "on", "true", "enabled"}:
            return True
        if normalized in {"0", "off", "false", "disabled"}:
            return False
    return None


class HttpController:
    def __init__(
        self,
        base_url: str,
        profile: ProtocolProfile,
        timeout: float = 8,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.profile = profile
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": "towelbar-agent/0.1"},
        )

    def close(self) -> None:
        self.client.close()

    def request(self, spec: RequestSpec, value: Any = None) -> httpx.Response:
        values = render_values(spec.values, value)
        kwargs: dict[str, Any] = {}
        if spec.encoding == "json":
            kwargs["json"] = values
        elif spec.encoding == "form":
            kwargs["data"] = values
        elif spec.encoding == "query":
            kwargs["params"] = values
        elif spec.encoding != "none":
            raise ProtocolError(f"unsupported request encoding: {spec.encoding}")
        response = self.client.request(
            spec.method, urljoin(self.base_url, spec.path.lstrip("/")), **kwargs
        )
        response.raise_for_status()
        return response

    def status(self) -> ControllerState:
        response = self.request(self.profile.status)
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload: Any = response.json()
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = {"text": response.text}
        mapping = self.profile.state
        power = dotted_get(payload, mapping.power)
        heat = dotted_get(payload, mapping.heat_level)
        timer = dotted_get(payload, mapping.timer_minutes)
        return ControllerState(
            power=to_bool(power),
            heat_level=int(heat) if heat is not None else None,
            timer_minutes=int(timer) if timer is not None else None,
            raw=payload,
        )

    def set_power(self, enabled: bool) -> None:
        if not self.profile.power:
            raise ProtocolError("power request is not configured")
        self.request(self.profile.power, 1 if enabled else 0)

    def set_heat_level(self, level: int) -> None:
        if not 1 <= level <= 5:
            raise ValueError("heat level must be between 1 and 5")
        if not self.profile.heat_level:
            raise ProtocolError("heat-level request is not configured")
        self.request(self.profile.heat_level, level)

    def set_timer_minutes(self, minutes: int) -> None:
        if not 0 <= minutes <= 1440:
            raise ValueError("timer must be between 0 and 1440 minutes")
        if not self.profile.timer_minutes:
            raise ProtocolError("timer request is not configured")
        self.request(self.profile.timer_minutes, minutes)


ENDPOINT_PATTERNS = (
    re.compile(r"""fetch\(\s*["']([^"']+)["']"""),
    re.compile(r"""(?:url|endpoint)\s*[:=]\s*["']([^"']+)["']""", re.I),
    re.compile(r"""XMLHttpRequest\(\).*?open\([^,]+,\s*["']([^"']+)["']""", re.S),
    re.compile(r"""["']((?:/|https?://)[^"' ]*(?:api|status|power|heat|timer)[^"' ]*)["']""", re.I),
)


def endpoint_candidates(source: str) -> list[str]:
    found: set[str] = set()
    for pattern in ENDPOINT_PATTERNS:
        found.update(match.group(1) for match in pattern.finditer(source))
    return sorted(found)
