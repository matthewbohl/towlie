from __future__ import annotations

import base64
import hashlib
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from .network import interface_transport
from .protocol import ControllerState


class EmmeSteelError(RuntimeError):
    pass


def parse_state(message: str) -> ControllerState:
    if not message.startswith("DT:"):
        raise EmmeSteelError(f"unexpected controller message: {message[:80]!r}")
    values: dict[int, int] = {}
    for pair in message[3:].split("P"):
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        try:
            values[int(key)] = int(value)
        except ValueError:
            continue
    timer = None
    if 8 in values and 9 in values:
        timer = values[8] * 60 + values[9]
        if values.get(10, 0) > 0:
            timer += 1
    sensor_enabled = bool(values[5]) if 5 in values else None
    return ControllerState(
        power=bool(values[0]) if 0 in values else None,
        heat_level=values.get(2),
        timer_minutes=timer,
        target_temperature=(values[3] - 20) / 2 if 3 in values else None,
        current_temperature=(
            (values[4] - 20) / 2
            if 4 in values and sensor_enabled is not False
            else None
        ),
        temperature_sensor_enabled=sensor_enabled,
        heating=bool(values[11]) if 11 in values else None,
        timer_active=bool(values[7]) if 7 in values else None,
        raw=values,
    )


class BoundWebSocket:
    def __init__(self, url: str, interface: str, timeout: float = 8, trace: Any | None = None):
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError("EmmeSteel WebSocket URL must use ws://")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.interface = interface
        self.timeout = timeout
        self.trace = trace
        self.sock: socket.socket | None = None
        self.buffer = bytearray()

    def __enter__(self) -> "BoundWebSocket":
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(
            socket.SOL_SOCKET,
            getattr(socket, "SO_BINDTODEVICE", 25),
            self.interface.encode() + b"\0",
        )
        phase = self.trace.phase("tcp_connect") if self.trace else _noop_context()
        with phase:
            sock.connect((self.host, self.port))
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        phase = self.trace.phase("websocket_handshake") if self.trace else _noop_context()
        with phase:
            sock.sendall(request)
            response, remainder = self._read_until(sock, b"\r\n\r\n")
            status = response.split(b"\r\n", 1)[0]
            if b" 101 " not in status:
                sock.close()
                raise EmmeSteelError(f"WebSocket upgrade failed: {status.decode('latin-1')}")
            expected = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            )
            if expected.lower() not in response.lower():
                sock.close()
                raise EmmeSteelError("WebSocket handshake returned an invalid accept key")
        self.sock = sock
        self.buffer.extend(remainder)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    @staticmethod
    def _read_until(sock: socket.socket, marker: bytes) -> tuple[bytes, bytes]:
        data = bytearray()
        while marker not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise EmmeSteelError("connection closed during WebSocket handshake")
            data.extend(chunk)
        end = data.index(marker) + len(marker)
        return bytes(data[:end]), bytes(data[end:])

    def send_text(self, text: str) -> None:
        if not self.sock:
            raise EmmeSteelError("WebSocket is not connected")
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes((0x81, 0x80 | len(payload)))
        elif len(payload) < 65536:
            header = bytes((0x81, 0xFE)) + len(payload).to_bytes(2, "big")
        else:
            raise ValueError("controller command is too long")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def receive_text(self) -> str:
        if not self.sock:
            raise EmmeSteelError("WebSocket is not connected")
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read_exact(8), "big")
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x1:
                return payload.decode("utf-8", "replace")
            if opcode == 0x8:
                raise EmmeSteelError("controller closed the WebSocket")
            if opcode == 0x9:
                self._send_control(0xA, payload)

    def _send_control(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            return
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(payload))) + mask + masked)

    def _read_exact(self, length: int) -> bytes:
        assert self.sock is not None
        data = bytearray(self.buffer[:length])
        del self.buffer[:length]
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise EmmeSteelError("controller closed the WebSocket")
            data.extend(chunk)
        return bytes(data)


@dataclass
class EmmeSteelController:
    base_url: str
    interface: str
    timeout: float = 8
    max_timer_minutes: int = 240
    trace: Any | None = None

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.base_url)
        return f"ws://{parsed.hostname}:{parsed.port or 80}/ws"

    def status(self) -> ControllerState:
        with BoundWebSocket(self.websocket_url, self.interface, self.timeout, self.trace) as ws:
            phase = self.trace.phase("first_state") if self.trace else _noop_context()
            with phase:
                return self._receive_state(ws)

    def apply(self, desired: dict[str, object]) -> ControllerState:
        with BoundWebSocket(self.websocket_url, self.interface, self.timeout, self.trace) as ws:
            phase = self.trace.phase("first_state") if self.trace else _noop_context()
            with phase:
                state = self._receive_state(ws)
            # P0 is not a reliable power indicator on these controllers: the
            # Primary unit reports P0=0 while its timer and heating output are
            # both active.  Use those observed operating signals instead.
            if "power" in desired and state.active != bool(desired["power"]):
                ws.send_text("on-off")
            if "heat_level" in desired:
                target_level = int(desired["heat_level"])
                if not 0 <= target_level <= 5:
                    raise ValueError("heat level must be between 0 and 5")
                current_level = state.heat_level or 0
                command = "power-up" if target_level > current_level else "power-dn"
                for _ in range(abs(target_level - current_level)):
                    ws.send_text(command)
            if "target_temperature" in desired:
                temperature = float(desired["target_temperature"])
                if not 30 <= temperature <= 70 or temperature % 1:
                    raise ValueError("temperature must be a whole degree from 30 to 70 °C")
                ws.send_text(f"tempSlider{int(temperature * 2 + 20)}")

        if "timer_minutes" in desired:
            self.set_timer(int(desired["timer_minutes"]))
        return self.status()

    def set_timer(self, minutes: int) -> None:
        if not 0 <= minutes <= self.max_timer_minutes:
            raise ValueError(
                f"timer must be between 0 and {self.max_timer_minutes} minutes"
            )
        query = urlencode({"ore": minutes // 60, "min": minutes % 60})
        phase = self.trace.phase("timer_http") if self.trace else _noop_context()
        with phase:
            with httpx.Client(
                transport=interface_transport(self.interface),
                timeout=self.timeout,
            ) as client:
                response = client.get(self.base_url.rstrip("/") + "/timerSet?" + query)
                response.raise_for_status()

    @staticmethod
    def _receive_state(ws: BoundWebSocket) -> ControllerState:
        while True:
            message = ws.receive_text()
            if message.startswith("DT:"):
                return parse_state(message)


class _noop_context:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None
