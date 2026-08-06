import httpx

from towelbar_agent.config import ProtocolProfile
from towelbar_agent.protocol import HttpController, dotted_get, endpoint_candidates


def profile():
    return ProtocolProfile.from_dict(
        {
            "status": {"path": "/api/status", "encoding": "none"},
            "state": {
                "power": "device.enabled",
                "heat_level": "device.level",
                "timer_minutes": "device.timer",
            },
            "power": {
                "method": "POST",
                "path": "/api/power",
                "encoding": "json",
                "values": {"enabled": "{value}"},
            },
        }
    )


def test_status_mapping_and_power_request():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/api/status":
            return httpx.Response(
                200,
                json={"device": {"enabled": 1, "level": 4, "timer": 90}},
            )
        return httpx.Response(204)

    controller = HttpController(
        "http://192.168.4.1/",
        profile(),
        transport=httpx.MockTransport(handler),
    )
    try:
        state = controller.status()
        controller.set_power(False)
    finally:
        controller.close()

    assert state.power is True
    assert state.heat_level == 4
    assert state.timer_minutes == 90
    assert requests[1].content == b'{"enabled":"0"}'


def test_dotted_get_handles_lists():
    assert dotted_get({"items": [{"state": "on"}]}, "items.0.state") == "on"


def test_endpoint_candidate_extraction():
    source = """fetch('/api/status'); const endpoint = "/api/power";"""
    assert endpoint_candidates(source) == ["/api/power", "/api/status"]
