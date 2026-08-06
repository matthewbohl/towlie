from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx

from .agent import TowelBarAgent
from .config import load_config
from .discovery import snapshot_portal
from .network import NetworkManager, interface_transport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="towelbar-agent")
    parser.add_argument("--config", default="/etc/towelbar-agent/config.yaml")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the control loop")
    sub.add_parser("scan", help="scan for Wi-Fi networks")

    connect = sub.add_parser("connect", help="connect to a configured controller")
    connect.add_argument("controller")

    snapshot = sub.add_parser(
        "snapshot", help="connect and save the captive portal plus JS/CSS assets"
    )
    snapshot.add_argument("controller")
    snapshot.add_argument("--output", default="./captures")

    request = sub.add_parser(
        "request", help="make an exploratory HTTP request through a controller hotspot"
    )
    request.add_argument("controller")
    request.add_argument("method")
    request.add_argument("path")
    request.add_argument("--json", dest="json_body")
    request.add_argument("--form", action="append", default=[])
    return parser


def controller_by_id(config: object, controller_id: str):
    for controller in config.controllers:
        if controller.id == controller_id:
            return controller
    raise SystemExit(f"Unknown controller: {controller_id}")


def connect_controller(config: object, controller_id: str):
    controller = controller_by_id(config, controller_id)
    network = NetworkManager(config.wifi_interface)
    gateway = network.connect(
        controller.ssid, controller.password, config.connect_timeout_seconds
    )
    return controller, gateway


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    if args.command == "run":
        TowelBarAgent(config).run()
    elif args.command == "scan":
        networks = NetworkManager(config.wifi_interface).scan()
        print(json.dumps([asdict(item) for item in networks], indent=2))
    elif args.command == "connect":
        controller, gateway = connect_controller(config, args.controller)
        print(json.dumps({"controller": controller.id, "gateway": gateway}, indent=2))
    elif args.command == "snapshot":
        controller, gateway = connect_controller(config, args.controller)
        base_url = controller.base_url or f"http://{gateway}/"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = Path(args.output) / controller.id / stamp
        result = snapshot_portal(
            base_url,
            destination,
            config.request_timeout_seconds,
            transport=interface_transport(config.wifi_interface),
        )
        print(json.dumps(asdict(result), indent=2))
    elif args.command == "request":
        controller, gateway = connect_controller(config, args.controller)
        base_url = (controller.base_url or f"http://{gateway}/").rstrip("/")
        data = dict(item.split("=", 1) for item in args.form)
        body = json.loads(args.json_body) if args.json_body else None
        with httpx.Client(
            transport=interface_transport(config.wifi_interface),
            timeout=config.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = client.request(
                args.method.upper(),
                f"{base_url}/{args.path.lstrip('/')}",
                json=body,
                data=data or None,
            )
        print(f"HTTP {response.status_code}")
        for key, value in response.headers.items():
            print(f"{key}: {value}")
        print()
        print(response.text)
