from towelbar_agent.diagnostics import (
    AttemptTrace,
    DiagnosticSettings,
    DiagnosticSink,
    read_events,
    summarize_events,
)
from towelbar_agent.soak import SoakControl


def test_attempt_trace_records_phase_failure_and_summary(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = DiagnosticSink(DiagnosticSettings(enabled=True, events_path=str(path)))
    trace = AttemptTrace(sink, "bath", "EMMESTEEL_TEST")
    try:
        with trace.phase("associate"):
            raise TimeoutError("no response")
    except TimeoutError as exc:
        trace.finish(False, exc)

    events = read_events(path)
    assert events[0]["failed_phase"] == "associate"
    summary = summarize_events(events)
    assert summary["controllers"]["bath"]["attempts"] == 1
    assert summary["controllers"]["bath"]["failures_by_phase"] == {"associate": 1}


def test_soak_control_round_trip(tmp_path):
    control = SoakControl(tmp_path)
    control.request({"duration_minutes": 30})
    assert control.take_request() == {"duration_minutes": 30}
    assert control.take_request() is None
    control.set_status(status="running", samples=2)
    assert control.status()["status"] == "running"
    assert control.status()["samples"] == 2
    control.stop()
    assert control.should_stop()


def test_soak_summary_groups_test_combinations():
    summary = summarize_events(
        [
            {
                "event": "poll_attempt",
                "controller_id": "bath",
                "mode": "soak",
                "switch_interval_seconds": 30,
                "settle_seconds": 1,
                "success": True,
                "total_ms": 100,
                "actual_revisit_seconds": 61,
                "network": {"signal": 75},
                "phases": {"associate": {"duration_ms": 80}},
            }
        ]
    )
    combination = summary["soak_combinations"]["bath|interval=30|settle=1"]
    assert combination["success_rate_percent"] == 100
    assert combination["metrics"]["signal"]["p50"] == 75
