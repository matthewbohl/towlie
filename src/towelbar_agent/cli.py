from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx

from .agent import TowelBarAgent
from .config import load_config
from .diagnostics import read_events, summarize_events
from .discovery import snapshot_portal
from .network import NetworkManager, WifiLock, interface_transport
from .soak import SoakControl


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

    diagnostics = sub.add_parser("diagnostics", help="summarize structured poll diagnostics")
    diagnostics.add_argument("--hours", type=float, default=24)
    diagnostics.add_argument("--failures", type=int, default=0)
    diagnostics.add_argument("--controller")

    soak_start = sub.add_parser("soak-start", help="queue a status-only soak in the agent")
    soak_start.add_argument("--duration-minutes", type=float, default=30)
    soak_start.add_argument("--intervals", default="10,15,20,30,45,60")
    soak_start.add_argument("--settle-seconds", default="0,0.5,1,2")
    sub.add_parser("soak-status", help="show soak progress or the latest report")
    sub.add_parser("soak-stop", help="stop the active soak safely")
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
        with WifiLock(config.wifi_interface, timeout=2):
            networks = NetworkManager(config.wifi_interface).scan()
        print(json.dumps([asdict(item) for item in networks], indent=2))
    elif args.command == "connect":
        with WifiLock(config.wifi_interface, timeout=2):
            controller, gateway = connect_controller(config, args.controller)
        print(json.dumps({"controller": controller.id, "gateway": gateway}, indent=2))
    elif args.command == "snapshot":
        with WifiLock(config.wifi_interface, timeout=2):
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
        with WifiLock(config.wifi_interface, timeout=2):
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
    elif args.command == "diagnostics":
        events = read_events(config.diagnostics.events_path, args.hours)
        if args.failures:
            events = [
                event for event in events
                if event.get("event") == "poll_attempt"
                and not event.get("success")
                and (not args.controller or event.get("controller_id") == args.controller)
            ][-args.failures :]
            print(json.dumps(events, indent=2))
        else:
            print(json.dumps(summarize_events(events), indent=2))
    elif args.command.startswith("soak-"):
        runtime = Path(
            os.environ.get("TOWELBAR_RUNTIME_STATE", "/var/lib/towelbar-agent/runtime.json")
        )
        control = SoakControl(runtime.parent)
        if args.command == "soak-start":
            request = {
                "duration_minutes": args.duration_minutes,
                "intervals": [float(item) for item in args.intervals.split(",")],
                "settle_seconds": [float(item) for item in args.settle_seconds.split(",")],
            }
            control.request(request)
            control.set_status(status="queued", **request)
        elif args.command == "soak-stop":
            control.stop()
        print(json.dumps(control.status(), indent=2))
