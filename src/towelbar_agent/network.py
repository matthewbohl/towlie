from __future__ import annotations

import json
import http.client
import fcntl
import ipaddress
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx


class NetworkError(RuntimeError):
    pass


class WifiBusyError(NetworkError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int | None
    security: str


class NetworkManager:
    def __init__(self, interface: str, runner: Runner = subprocess.run):
        self.interface = interface
        self._run = runner

    def _command(self, args: list[str], check: bool = True) -> str:
        result = self._run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise NetworkError(f"{' '.join(self._redact_args(args))} failed: {detail}")
        return result.stdout

    @staticmethod
    def _redact_args(args: list[str]) -> list[str]:
        redacted = list(args)
        for index, value in enumerate(redacted[:-1]):
            if value in {"wifi-sec.psk", "password", "passwd"}:
                redacted[index + 1] = "<redacted>"
        return redacted

    def scan(self) -> list[WifiNetwork]:
        output = self._command(
            [
                "nmcli",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "ifname",
                self.interface,
                "--rescan",
                "yes",
            ]
        )
        networks: dict[str, WifiNetwork] = {}
        for line in output.splitlines():
            # nmcli's terse mode escapes literal colons.
            fields, current, escaped = [], "", False
            for char in line:
                if escaped:
                    current += char
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == ":":
                    fields.append(current)
                    current = ""
                else:
                    current += char
            fields.append(current)
            if len(fields) < 3 or not fields[0]:
                continue
            signal = int(fields[1]) if fields[1].isdigit() else None
            candidate = WifiNetwork(fields[0], signal, fields[2])
            previous = networks.get(candidate.ssid)
            if previous is None or (candidate.signal or -1) > (previous.signal or -1):
                networks[candidate.ssid] = candidate
        return sorted(networks.values(), key=lambda item: item.signal or -1, reverse=True)

    def connect(
        self,
        ssid: str,
        password: str,
        timeout: float = 20,
        settle_seconds: float = 0,
        trace: Any | None = None,
    ) -> str:
        connection_name = f"towelbar-{self._safe_name(ssid)}"
        with self._phase(trace, "profile_lookup"):
            existing = self._command(
                ["nmcli", "-t", "-f", "NAME", "connection", "show"], check=False
            ).splitlines()
        if connection_name not in existing:
            with self._phase(trace, "profile_create"):
                self._command(
                    [
                        "nmcli", "connection", "add", "type", "wifi", "ifname",
                        self.interface, "con-name", connection_name, "ssid", ssid,
                    ]
                )
        # Passing the password as an argv value is unavoidable with nmcli, but the
        # systemd unit is restricted to the dedicated service account.
        with self._phase(trace, "profile_configure"):
            self._command(
                [
                    "nmcli", "connection", "modify", connection_name,
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                    "connection.autoconnect", "no", "ipv4.never-default", "yes",
                    "ipv6.method", "disabled",
                ]
            )
        with self._phase(trace, "associate"):
            self._command(
                [
                    "nmcli", "--wait", str(max(1, int(timeout))), "connection", "up",
                    connection_name, "ifname", self.interface,
                ]
            )
        deadline = time.monotonic() + timeout
        selected: str | None = None
        source = "unknown"
        with self._phase(trace, "network_ready"):
            while time.monotonic() < deadline:
                selected = self.gateway()
                if selected:
                    source = "gateway"
                    break
                selected = self.subnet_first_host()
                if selected:
                    source = "subnet_first_host"
                    break
                time.sleep(0.25)
            if not selected:
                raise NetworkError(
                    f"connected to {ssid}, but no IPv4 gateway or interface address was assigned"
                )
        if settle_seconds > 0:
            with self._phase(trace, "settle"):
                time.sleep(settle_seconds)
        snapshot = self.snapshot()
        if trace:
            trace.update(gateway=selected, gateway_source=source, network=snapshot)
        actual_ssid = snapshot.get("ssid")
        if actual_ssid and actual_ssid != ssid:
            raise NetworkError(f"expected SSID {ssid}, but {self.interface} reports {actual_ssid}")
        return selected

    @staticmethod
    def _phase(trace: Any | None, name: str):
        return trace.phase(name) if trace else _noop_context()

    def snapshot(self, include_details: bool = False) -> dict[str, Any]:
        connection = self._command(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", self.interface],
            check=False,
        ).strip()
        wifi = self._command(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list", "ifname", self.interface],
            check=False,
        )
        active = next((line for line in wifi.splitlines() if line.startswith("yes:")), "")
        parts = active.split(":", 2)
        addresses = self._command(
            ["ip", "-j", "-4", "address", "show", "dev", self.interface], check=False
        )
        routes = self._command(
            ["ip", "-j", "route", "show", "dev", self.interface], check=False
        )
        result: dict[str, Any] = {
            "interface": self.interface,
            "connection": connection or None,
            "ssid": parts[1] if len(parts) > 1 else None,
            "signal": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
            "addresses": self._json_or_text(addresses),
            "routes": self._json_or_text(routes),
        }
        if include_details:
            result["neighbors"] = self._json_or_text(
                self._command(["ip", "-j", "neigh", "show", "dev", self.interface], check=False)
            )
            result["iw_link"] = self._command(["iw", "dev", self.interface, "link"], check=False).strip()
        return result

    @staticmethod
    def _json_or_text(value: str) -> Any:
        try:
            return json.loads(value or "[]")
        except json.JSONDecodeError:
            return value.strip()

    def subnet_first_host(self) -> str | None:
        """Infer a captive device at the first host address of wlan's subnet."""
        output = self._command(
            ["ip", "-j", "-4", "address", "show", "dev", self.interface],
            check=False,
        )
        try:
            devices = json.loads(output or "[]")
        except json.JSONDecodeError:
            return None
        for device in devices:
            for address in device.get("addr_info", []):
                local = address.get("local")
                prefix = address.get("prefixlen")
                if address.get("family") != "inet" or not local or prefix is None:
                    continue
                network = ipaddress.ip_network(f"{local}/{prefix}", strict=False)
                first = next(network.hosts(), None)
                if first is not None and str(first) != local:
                    return str(first)
        return None

    def gateway(self) -> str | None:
        output = self._command(
            ["ip", "-j", "route", "show", "default", "dev", self.interface],
            check=False,
        )
        try:
            routes = json.loads(output or "[]")
        except json.JSONDecodeError:
            return None
        for route in routes:
            if route.get("gateway"):
                return str(route["gateway"])
        # Captive devices sometimes omit a default route. Use the DHCP gateway.
        output = self._command(
            [
                "nmcli",
                "-g",
                "IP4.GATEWAY",
                "device",
                "show",
                self.interface,
            ],
            check=False,
        )
        return next((line.strip() for line in output.splitlines() if line.strip()), None)

    @staticmethod
    def _safe_name(value: str) -> str:
        return "".join(char if char.isalnum() else "-" for char in value)[:40]


class InterfaceHTTPTransport(httpx.BaseTransport):
    """Small HTTP/1.1 transport for fragile captive servers, pinned to an interface."""

    def __init__(self, interface: str, timeout: float = 20):
        self.interface = interface
        self.timeout = timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "http":
            raise ValueError("towel bar transport only supports plain HTTP")
        host = request.url.host
        port = request.url.port or 80
        target = request.url.raw_path
        body = b"".join(request.stream)
        headers = list(request.headers.raw)
        if body and not any(key.lower() == b"content-length" for key, _ in headers):
            headers.append((b"Content-Length", str(len(body)).encode("ascii")))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            bind_option = getattr(socket, "SO_BINDTODEVICE", 25)
            sock.setsockopt(
                socket.SOL_SOCKET,
                bind_option,
                self.interface.encode() + b"\0",
            )
            sock.connect((host, port))
            head = [request.method.encode("ascii") + b" " + target + b" HTTP/1.1"]
            head.extend(key + b": " + value for key, value in headers)
            sock.sendall(b"\r\n".join(head) + b"\r\n\r\n" + body)

            incoming = http.client.HTTPResponse(sock)
            incoming.begin()
            content = incoming.read()
            return httpx.Response(
                incoming.status,
                headers=list(incoming.getheaders()),
                content=content,
                request=request,
            )
        finally:
            sock.close()


def interface_transport(interface: str) -> InterfaceHTTPTransport:
    """Create a captive-server-compatible transport pinned to one interface."""
    return InterfaceHTTPTransport(interface)


@contextmanager
def _noop_context() -> Iterator[None]:
    yield


class WifiLock:
    def __init__(self, interface: str, timeout: float = 0):
        root = Path(os.environ.get("TOWELBAR_LOCK_DIR", "/run/lock"))
        self.path = root / f"towelbar-agent-{NetworkManager._safe_name(interface)}.lock"
        self.timeout = timeout
        self.handle: Any | None = None

    def __enter__(self) -> "WifiLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle.seek(0)
                self.handle.truncate()
                self.handle.write(f"pid={os.getpid()}\n")
                self.handle.flush()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise WifiBusyError(f"{self.interface_name} is busy")
                time.sleep(0.1)

    @property
    def interface_name(self) -> str:
        return self.path.stem.removeprefix("towelbar-agent-")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
