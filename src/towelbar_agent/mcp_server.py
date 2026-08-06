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
from .discovery import snapshot_portal
from .network import NetworkManager, interface_transport

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
    return [asdict(item) for item in NetworkManager(config.wifi_interface).scan()]


@mcp.tool()
def portal_snapshot(controller_id: str, output_root: str = "./captures") -> dict[str, Any]:
    """Connect to a towel bar and save its portal plus local JS/CSS assets."""
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


def main() -> None:
    mcp.run(transport="stdio")
