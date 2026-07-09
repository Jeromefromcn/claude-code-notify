import os

from claude_code_notify import transcript_parser as tp
from claude_code_notify.transcript_parser import LaunchEvent, CompletionEvent

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _events(name):
    events, _ = tp.parse_events(os.path.join(FIX, name))
    return events


def test_foreground_only_no_events():
    # Foreground Bash (run_in_background:false) is never a tracked launch.
    assert _events("foreground_only.jsonl") == []


def test_background_agent_launch_detected():
    assert _events("bg_agent_pending.jsonl") == [LaunchEvent("toolu_ag1")]


def test_background_agent_completed():
    evs = _events("bg_agent_completed.jsonl")
    assert LaunchEvent("toolu_ag2") in evs
    assert CompletionEvent("toolu_ag2") in evs


def test_background_bash_ack_is_not_completion():
    # The immediate "Command running in background" ack must NOT resolve.
    evs = _events("bg_bash_ack_only.jsonl")
    assert LaunchEvent("toolu_bg1") in evs
    assert CompletionEvent("toolu_bg1") not in evs


def test_background_bash_completed():
    evs = _events("bg_bash_completed.jsonl")
    assert LaunchEvent("toolu_bg2") in evs
    assert CompletionEvent("toolu_bg2") in evs


def test_task_notification_both_variants():
    evs = _events("notif_twice.jsonl")
    completions = [e for e in evs if isinstance(e, CompletionEvent)]
    # Both a queue-operation and a user/origin.kind entry are parsed.
    assert len(completions) == 2
    assert all(e.tool_use_id == "toolu_ag3" for e in completions)


def test_sidechain_launch_ignored():
    assert _events("sidechain_launch.jsonl") == []


def test_incremental_matches_full(tmp_path):
    path = tmp_path / "t.jsonl"
    line1 = '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    line2 = '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n'
    path.write_text(line1)
    ev1, off1 = tp.parse_events(str(path), 0)
    assert ev1 == [LaunchEvent("a")]
    with open(path, "a") as fh:
        fh.write(line2)
    ev2, off2 = tp.parse_events(str(path), off1)
    assert ev2 == [CompletionEvent("a")]
    ev_full, off_full = tp.parse_events(str(path), 0)
    assert ev_full == [LaunchEvent("a"), CompletionEvent("a")]
    assert off2 == off_full


def test_partial_trailing_line_not_consumed(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_bytes(b'{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n{"type":"assist')
    events, off = tp.parse_events(str(path), 0)
    assert events == [LaunchEvent("a")]
    # Offset stops at the end of the last complete line, not the partial one.
    assert off == path.read_bytes().index(b"\n") + 1


def test_missing_file_returns_offset():
    assert tp.parse_events("/no/such/file.jsonl", 42) == ([], 42)


def test_malformed_line_skipped(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text('not json\n{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n')
    events, _ = tp.parse_events(str(path), 0)
    assert events == [LaunchEvent("a")]


def test_latest_ai_title():
    assert tp.latest_ai_title(os.path.join(FIX, "foreground_only.jsonl")) == "List files"
    assert tp.latest_ai_title(os.path.join(FIX, "bg_agent_pending.jsonl")) is None
