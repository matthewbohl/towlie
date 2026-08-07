import subprocess

import pytest

from towelbar_agent.network import NetworkError, NetworkManager, WifiBusyError, WifiLock


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_scan_parses_escaped_colons_and_keeps_strongest():
    def runner(args, **kwargs):
        return completed(
            "emmesteel\\:one:60:WPA2\n"
            "other:20:OPEN\n"
            "emmesteel\\:one:80:WPA2\n"
        )

    networks = NetworkManager("wlan0", runner=runner).scan()
    assert [(item.ssid, item.signal) for item in networks] == [
        ("emmesteel:one", 80),
        ("other", 20),
    ]


def test_subnet_first_host_uses_interface_address():
    def runner(args, **kwargs):
        if args[:3] == ["ip", "-j", "-4"]:
            return completed(
                '[{"addr_info":[{"family":"inet","local":"192.168.1.2",'
                '"prefixlen":24}]}]'
            )
        return completed()

    assert NetworkManager("wlan0", runner=runner).subnet_first_host() == "192.168.1.1"


def test_network_error_redacts_wifi_password():
    def runner(args, **kwargs):
        return completed("", 1)

    manager = NetworkManager("wlan0", runner=runner)
    with pytest.raises(NetworkError) as caught:
        manager._command(
            ["nmcli", "connection", "modify", "x", "wifi-sec.psk", "secret"]
        )
    assert "secret" not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_wifi_lock_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWELBAR_LOCK_DIR", str(tmp_path))
    with WifiLock("wlan0"):
        with pytest.raises(WifiBusyError):
            with WifiLock("wlan0"):
                pass
