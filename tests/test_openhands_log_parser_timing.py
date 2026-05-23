import json
from datetime import datetime, timedelta

from harness.e2e.log_parser.openhands import OpenHandsLogParser
from harness.e2e.log_parser.models import ToolCallRecord


def _tool_call(call_id: str, timestamp: datetime) -> ToolCallRecord:
    return ToolCallRecord(
        id=call_id,
        name="Bash",
        timestamp=timestamp,
        success=True,
        input_size=1,
        output_size=1,
    )


def _write_event(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_openhands_trial_stats_separate_active_duration_from_wall_clock_gaps():
    parser = OpenHandsLogParser()
    start = datetime(2026, 3, 3, 0, 0, 0)
    second_session = start + timedelta(days=1)
    tool_calls = [
        _tool_call("a0", start),
        _tool_call("a1", start + timedelta(minutes=1)),
        _tool_call("b0", second_session),
        _tool_call("b1", second_session + timedelta(minutes=1)),
    ]

    stats = parser.compute_trial_stats(
        trial_name="trial",
        model="gpt-5",
        tool_calls=tool_calls,
        stdout_stats={
            "duration_ms": 86_460_000,
            "total_cost_usd": 0.0,
            "total_turns": 0,
            "modelUsage": {},
            "session_count": 1,
            "unique_session_count": 1,
        },
    )

    assert stats.start_time == start
    assert stats.end_time == second_session + timedelta(minutes=1)
    assert stats.wall_clock_ms == 86_460_000
    assert stats.duration_ms == 120_000
    assert stats.session_count == 2
    assert stats.unique_session_count == 2


def test_stdout_stats_use_timestamp_minmax_for_out_of_order_events(tmp_path):
    stdout_file = tmp_path / "agent_stdout.txt"
    later = {
        "kind": "MetricsEvent",
        "timestamp": "2026-03-04T00:01:00Z",
        "usage": {"model": "gpt-5", "input_tokens": 1, "output_tokens": 1},
    }
    earlier = {
        "kind": "MetricsEvent",
        "timestamp": "2026-03-04T00:00:00Z",
        "usage": {"model": "gpt-5", "input_tokens": 1, "output_tokens": 1},
    }
    stdout_file.write_text(
        f"--JSON Event--\n{json.dumps(later)}\n--JSON Event--\n{json.dumps(earlier)}\n",
        encoding="utf-8",
    )

    stats = OpenHandsLogParser().parse_stdout_stats(stdout_file)

    assert stats["duration_ms"] == 60_000


def test_timestamp_parser_normalizes_offsets_to_utc_naive():
    parsed = OpenHandsLogParser()._parse_timestamp("2026-01-01T01:00:00+01:00")

    assert parsed == datetime(2026, 1, 1, 0, 0, 0)


def test_raw_log_stats_count_conversation_dirs(tmp_path):
    parser = OpenHandsLogParser()
    _write_event(
        tmp_path / "session-a" / "events" / "event-00000.json",
        {
            "kind": "MetricsEvent",
            "timestamp": "2026-03-03T00:00:00Z",
            "llm_response_id": "a0",
            "usage": {"model": "gpt-5", "input_tokens": 1, "output_tokens": 1},
        },
    )
    _write_event(
        tmp_path / "session-b" / "events" / "event-00000.json",
        {
            "kind": "MetricsEvent",
            "timestamp": "2026-03-03T00:01:00Z",
            "llm_response_id": "b0",
            "usage": {"model": "gpt-5", "input_tokens": 1, "output_tokens": 1},
        },
    )

    stats = parser._parse_stats_from_raw_logs(tmp_path)

    assert stats["session_count"] == 2
    assert stats["unique_session_count"] == 2
