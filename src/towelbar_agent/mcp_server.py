from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import load_config
from .diagnostics import AttemptTrace, DiagnosticSink, read_events, summarize_events
from .discovery import snapshot_portal
from .emmesteel import EmmeSteelController
from .network import NetworkManager, WifiLock, interface_transport
from .soak import SoakControl

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Install the MCP extra: pip install 'towelbar-agent[mcp]'") from exc


def _ensure_accessible_working_directory() -> None:
    """Avoid FastMCP's `.env` probe failing in an inherited private home."""
    if os.access(".", os.R_OK | os.X_OK):
        return
    preferred = Path(
        os.environ.get("TOWELBAR_WORKDIR", "/var/lib/towelbar-agent")
    )
    try:
        os.chdir(preferred)
    except OSError:
        os.chdir(tempfile.gettempdir())


_ensure_accessible_working_directory()

CONFIG_PATH = os.environ.get(
    "TOWELBAR_CONFIG", "/etc/towelbar-agent/config.yaml"
)
mcp = FastMCP("Towel Bar Discovery")


def soak_control() -> SoakControl:
    runtime = Path(
        os.environ.get("TOWELBAR_RUNTIME_STATE", "/var/lib/towelbar-agent/runtime.json")
    )
    return SoakControl(runtime.parent)


def context(controller_id: str):
    config = load_config(CONFIG_PATH)
    controller = next(
        (item for item in config.controllers if item.id == controller_id), None
    )
    if controller is None:
        raise ValueError(f"unknown controller: {controller_id}")
    network = NetworkManager(config.wifi_interface)
    gateway = network.connect(
        controller.ssid, controller.password, config.connect_timeout_seconds
    )
    base_url = (controller.base_url or f"http://{gateway}/").rstrip("/") + "/"
    return config, controller, base_url, network.interface


@mcp.tool()
def wifi_scan() -> list[dict[str, Any]]:
    """Scan the configured Wi-Fi interface and report visible networks."""
    config = load_config(CONFIG_PATH)
    with WifiLock(config.wifi_interface, timeout=2):
        return [asdict(item) for item in NetworkManager(config.wifi_interface).scan()]


@mcp.tool()
def portal_snapshot(controller_id: str, output_root: str = "./captures") -> dict[str, Any]:
    """Connect to a towel bar and save its portal plus local JS/CSS assets."""
    config = load_config(CONFIG_PATH)
    with WifiLock(config.wifi_interface, timeout=2):
        config, controller, base_url, interface = context(controller_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path(output_root) / controller.id / stamp
        return asdict(
            snapshot_portal(
                base_url,
                output,
                config.request_timeout_seconds,
                transport=interface_transport(interface),
            )
        )


@mcp.tool()
def http_request(
    controller_id: str,
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send an explicit exploratory request to a configured towel bar."""
    config = load_config(CONFIG_PATH)
    with WifiLock(config.wifi_interface, timeout=2):
        config, controller, base_url, interface = context(controller_id)
        with httpx.Client(
            transport=interface_transport(interface),
            timeout=config.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = client.request(
                method.upper(),
                base_url + path.lstrip("/"),
                json=json_body,
                data=form_body,
            )
    return {
        "status_code": response.status_code,
        "final_url": str(response.url),
        "headers": dict(response.headers),
        "body": response.text[:200_000],
    }


@mcp.tool()
def diagnostic_summary(hours: float = 24) -> dict[str, Any]:
    """Summarize recent poll/soak success, latency, and failure phases."""
    config = load_config(CONFIG_PATH)
    return summarize_events(read_events(config.diagnostics.events_path, hours))


@mcp.tool()
def recent_failures(controller_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent sanitized diagnostic failures, newest first."""
    config = load_config(CONFIG_PATH)
    failures = [
        event
        for event in read_events(config.diagnostics.events_path, 24 * config.diagnostics.retention_days)
        if event.get("event") == "poll_attempt"
        and not event.get("success")
        and (controller_id is None or event.get("controller_id") == controller_id)
    ]
    return list(reversed(failures[-max(1, min(limit, 100)) :]))


@mcp.tool()
def probe_controller(controller_id: str) -> dict[str, Any]:
    """Run one exclusive, status-only probe with a detailed phase timeline."""
    config = load_config(CONFIG_PATH)
    controller = next(
        (item for item in config.controllers if item.id == controller_id), None
    )
    if controller is None:
        raise ValueError(f"unknown controller: {controller_id}")
    sink = DiagnosticSink(config.diagnostics, force=True)
    trace = AttemptTrace(sink, controller.id, controller.ssid, mode="mcp_probe", status_only=True)
    network = NetworkManager(config.wifi_interface)
    try:
        with WifiLock(config.wifi_interface, timeout=2):
            gateway = network.connect(
                controller.ssid,
                controller.password,
                config.connect_timeout_seconds,
                config.rotation.settle_after_connect_seconds,
                trace,
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
        return trace.finish(True)
    except Exception as exc:
        if config.diagnostics.capture_network_on_failure:
            trace.update(failure_network=network.snapshot(include_details=True))
        trace.finish(False, exc)
        return trace.event


@mcp.tool()
def soak_start(
    duration_minutes: float = 30,
    intervals: list[float] | None = None,
    settle_seconds: list[float] | None = None,
) -> dict[str, Any]:
    """Queue a randomized, status-only soak test in the running agent."""
    control = soak_control()
    current = control.status()
    if current.get("status") in {"queued", "running"}:
        raise ValueError("a soak test is already queued or running")
    request = {
        "duration_minutes": duration_minutes,
        "intervals": intervals or [10, 15, 20, 30, 45, 60],
        "settle_seconds": settle_seconds or [0, 0.5, 1, 2],
    }
    control.request(request)
    control.set_status(status="queued", **request)
    return control.status()


@mcp.tool()
def soak_status() -> dict[str, Any]:
    """Return current soak progress or the most recent report."""
    return soak_control().status()


@mcp.tool()
def soak_stop() -> dict[str, Any]:
    """Ask the running agent to stop its active soak test safely."""
    control = soak_control()
    control.stop()
    return {"status": "stop_requested"}


@mcp.tool()
def soak_report() -> dict[str, Any]:
    """Return the most recent completed soak report."""
    return soak_control().status()


def main() -> None:
    mcp.run(transport="stdio")
