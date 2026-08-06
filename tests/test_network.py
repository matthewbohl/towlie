import subprocess

from towelbar_agent.network import NetworkManager


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
