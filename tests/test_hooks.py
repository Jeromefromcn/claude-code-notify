import datetime
import io
import json
import os

import pytest

from claude_code_notify import hooks
from claude_code_notify.config import Config


@pytest.fixture
def base(tmp_path, monkeypatch):
    # Isolate config + state under a temp CLAUDE_NOTIFY_HOME.
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    return tmp_path


def _write_transcript(tmp_path, lines):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(l + "\n" for l in lines))
    return str(path)


def test_stop_pending_does_not_send(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s1", "transcript_path": transcript, "cwd": "/w"}
    rc = hooks.run("stop", json.dumps(payload))
    assert rc == 0
    assert sent == []  # background Agent still pending


def test_stop_completed_sends_once(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "s2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0] == "Claude Code finished | Do a thing | /w | " + sent[0].split(" | ")[-1]
    # Second immediate Stop is rate-limited.
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1


def test_stop_completed_no_ai_title_omits_title(base, tmp_path, monkeypatch):
    # Reproduces the real-world case: a background task resolves (pending
    # drops to 0) but Claude Code never wrote an ai-title envelope for this
    # session — the title segment must simply be omitted, not blank/garbled.
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
    ])
    payload = {"session_id": "s2b", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0] == "Claude Code finished | /w | " + sent[0].split(" | ")[-1]


def test_stop_failure_sends_directly(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0] == "Claude Code stopped with error | /w | " + sent[0].split(" | ")[-1]


def test_permission_request_sends_directly(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code needs your input | /w | " + sent[0].split(" | ")[-1]


def test_run_never_raises_on_bad_json(base):
    assert hooks.run("stop", "not json at all") == 0


def test_run_never_raises_on_missing_config(tmp_path, monkeypatch):
    # No config.env → ConfigError must be swallowed.
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert hooks.run("stop", "{}") == 0


def test_run_never_raises_on_notifier_error(base, tmp_path, monkeypatch):
    def boom(cfg, text):
        raise hooks.notifier.NotifierError("boom")
    monkeypatch.setattr(hooks.notifier, "send", boom)
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s5", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0


class _RaisingStdin:
    def isatty(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    def read(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def test_main_never_raises_on_stdin_read_failure(base, monkeypatch):
    monkeypatch.setattr(hooks.sys, "stdin", _RaisingStdin())
    assert hooks.main(["hooks", "stop"]) == 0


def test_main_happy_path_matches_run(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s7", "transcript_path": transcript, "cwd": "/w"}
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(["hooks", "stop_failure"]) == 0
    assert len(sent) == 1
    assert sent[0].startswith("Claude Code stopped with error |")


def test_debug_log_written_and_scrubbed(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nNOTIFY_DEBUG=true\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s6", "transcript_path": transcript, "cwd": "/w"}
    hooks.run("stop", json.dumps(payload))
    log = (tmp_path / "debug.log").read_text()
    assert "pending=1" in log
    assert "123:secret" not in log


def test_format_duration_seconds():
    assert hooks._format_duration(0) == "0s"
    assert hooks._format_duration(45) == "45s"
    assert hooks._format_duration(59) == "59s"


def test_format_duration_minutes():
    assert hooks._format_duration(60) == "1m00s"
    assert hooks._format_duration(192) == "3m12s"
    assert hooks._format_duration(3599) == "59m59s"


def test_format_duration_hours():
    assert hooks._format_duration(3600) == "1h00m"
    assert hooks._format_duration(3900) == "1h05m"


def test_format_duration_negative_or_none_is_none():
    assert hooks._format_duration(-1) is None
    assert hooks._format_duration(None) is None


def test_parse_ts_valid():
    assert hooks._parse_ts("2026-07-11T01:00:00.000Z") == pytest.approx(1783731600.0)


def test_parse_ts_invalid_returns_none():
    assert hooks._parse_ts("not a timestamp") is None
    assert hooks._parse_ts(None) is None


def test_turn_duration_no_turn_start_returns_none(tmp_path):
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Bash","input":{}}]}}',
    ])
    assert hooks._turn_duration(transcript, hooks._now()) is None


def test_turn_duration_computed_from_transcript(tmp_path):
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    start = hooks._parse_ts("2026-07-11T01:00:00.000Z")
    assert hooks._turn_duration(transcript, start + 192) == "3m12s"


def test_stop_includes_duration_when_turn_start_present(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:03:12.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "sdur1", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code finished | 3m12s | Do a thing | /w | " + sent[0].split(" | ")[-1]


def test_stop_omits_duration_without_turn_start(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "sdur2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code finished | Do a thing | /w | " + sent[0].split(" | ")[-1]


def test_stop_failure_includes_duration(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:00:45.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    payload = {"session_id": "sdur3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code stopped with error | 45s | /w | " + sent[0].split(" | ")[-1]


def test_permission_request_includes_duration(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:01:00.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    payload = {"session_id": "sdur4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code needs your input | 1m00s | /w | " + sent[0].split(" | ")[-1]


def _write_config(tmp_path, routes_block=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\n" + routes_block
    )


def test_stop_failure_routes_to_matching_destination(tmp_path, monkeypatch):
    _write_config(tmp_path,
                  "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\nROUTE_1_BOT_TOKEN=777:route\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    captured = []
    monkeypatch.setattr(hooks.notifier, "send",
                        lambda c, t: captured.append((c.bot_token, c.chat_id)))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r1", "transcript_path": transcript, "cwd": "/proj/acme/sub"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert captured == [("777:route", "111")]


def test_stop_failure_muted_route_does_not_send(tmp_path, monkeypatch):
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/scratch\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r2", "transcript_path": transcript, "cwd": "/proj/scratch/x"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert sent == []


def test_stop_muted_route_short_circuits(tmp_path, monkeypatch):
    # The Stop path has pending/rate-limit; mute must short-circuit before send.
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/scratch\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])  # nothing pending
    payload = {"session_id": "r3", "transcript_path": transcript, "cwd": "/proj/scratch/x"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent == []


def test_permission_request_unmatched_cwd_uses_global(tmp_path, monkeypatch):
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    captured = []
    monkeypatch.setattr(hooks.notifier, "send",
                        lambda c, t: captured.append((c.bot_token, c.chat_id)))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r4", "transcript_path": transcript, "cwd": "/somewhere/else"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert captured == [("123:secret", "999")]


def _rate_limit_line(text="You've hit your session limit · resets 9pm (Asia/Hong_Kong)"):
    return json.dumps({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "error": "rate_limit", "apiErrorStatus": 429,
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]}})


def _rate_limit_line_at(ts, text="You've hit your session limit · resets 5:20am (Asia/Hong_Kong)"):
    return json.dumps({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "error": "rate_limit", "apiErrorStatus": 429, "timestamp": ts,
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]}})


def _user_turn_line(ts, text="go on"):
    return json.dumps({
        "type": "user", "isSidechain": False, "timestamp": ts,
        "message": {"content": text}})


def _usage_config(tmp_path, extra=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\nNOTIFY_USAGE_LIMIT=true\n" + extra)


def test_usage_limit_feature_off_by_default(base, tmp_path, monkeypatch):
    # base fixture sets no NOTIFY_USAGE_LIMIT -> feature inert; normal path runs.
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u0", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "usage limit" not in sent[0]


def test_usage_limit_broadcasts_all_and_suppresses_finished(tmp_path, monkeypatch):
    _usage_config(tmp_path,
                  "NOTIFY_USAGE_LIMIT_RESET=false\n"    # keep the test process-free
                  "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append((c.chat_id, t)))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u1", "transcript_path": transcript, "cwd": "/proj/acme/x"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sorted(chat for chat, _ in sent) == ["111", "999"]
    assert all("Claude Code usage limit reached" in t for _, t in sent)
    assert all("finished" not in t for _, t in sent)


def test_usage_limit_same_window_does_not_resend(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert hooks.run("stop", json.dumps(payload)) == 0   # same window
    assert len(sent) == 1                                # no second broadcast
    assert all("finished" not in t for t in sent)        # still suppressed


def test_usage_limit_schedules_reset_when_enabled(tmp_path, monkeypatch):
    _usage_config(tmp_path)   # NOTIFY_USAGE_LIMIT_RESET defaults true
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    spawned = []
    monkeypatch.setattr(hooks.recovery, "spawn",
                        lambda base, window, target: spawned.append((window, target)))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(spawned) == 1
    assert spawned[0][1] is not None   # a parseable target epoch


def test_usage_limit_reset_disabled_does_not_spawn(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    spawned = []
    monkeypatch.setattr(hooks.recovery, "spawn",
                        lambda base, window, target: spawned.append(window))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert spawned == []


def test_usage_limit_enabled_normal_finish_sends_normally(tmp_path, monkeypatch):
    # Feature ON, but this turn's last assistant message is a normal reply, not
    # a rate limit -> _maybe_handle_usage_limit must return False and the
    # normal "finished" path must run untouched: no broadcast, no reset spawn.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    broadcasts = []
    monkeypatch.setattr(hooks.broadcast, "send_all",
                        lambda c, t: broadcasts.append(t) or 0)
    spawned = []
    monkeypatch.setattr(hooks.recovery, "spawn",
                        lambda base, window, target: spawned.append(window))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,'
        '"message":{"content":[{"type":"text","text":"All done, tests pass."}]}}',
    ])
    payload = {"session_id": "u5", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0].startswith("Claude Code finished")
    assert broadcasts == []
    assert spawned == []


def test_usage_limit_stop_failure_broadcasts_and_suppresses_error(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u6", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "Claude Code usage limit reached" in sent[0]
    assert "stopped with error" not in sent[0]


def test_usage_limit_stop_then_stop_failure_same_window_suppresses_without_resend(
    tmp_path, monkeypatch
):
    # Cross-handler case named in the design doc: Stop broadcasts first; a
    # later StopFailure for the *same* window must not re-broadcast, but must
    # still suppress its own normal "stopped with error" notification.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u7", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1                                  # Stop's broadcast
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1                                  # no second broadcast
    assert all("finished" not in t and "stopped with error" not in t for t in sent)


def test_usage_limit_same_reset_text_different_days_both_broadcast(tmp_path, monkeypatch):
    # Regression for window_key collisions across distinct reset dates. Two
    # genuinely separate usage-limit events on different calendar days can
    # render the *same* clock time ("resets 2pm" has no date). The pre-fix
    # text-only key hashed those to one value, so the second day's hit
    # collided with the first day's still-live marker and was silently dropped
    # — no broadcast, and the normal notification suppressed too. Both days
    # must broadcast. Days are 4 apart: inside gc's 30-day window, so the
    # day-1 marker is still on disk and only a date-aware key lets day 2 pass.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")  # keep it process-free
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    reset_text = "You've hit your session limit · resets 2pm (Asia/Hong_Kong)"

    # Day 1: rate limit at 10:00 local, so the next 2pm is later the same day.
    day1_dir = tmp_path / "day1"
    day1_dir.mkdir()
    t1 = _write_transcript(day1_dir, [_rate_limit_line(reset_text)])
    monkeypatch.setattr(hooks, "_now", lambda: datetime.datetime(2026, 7, 20, 10, 0, 0).timestamp())
    payload1 = {"session_id": "day1", "transcript_path": t1, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload1)) == 0
    assert len(sent) == 1
    assert "Claude Code usage limit reached" in sent[0]

    # Day 2 (four days later): a fresh transcript with identical displayed text
    # must broadcast again.
    day2_dir = tmp_path / "day2"
    day2_dir.mkdir()
    t2 = _write_transcript(day2_dir, [_rate_limit_line(reset_text)])
    monkeypatch.setattr(hooks, "_now", lambda: datetime.datetime(2026, 7, 24, 10, 0, 0).timestamp())
    payload2 = {"session_id": "day2", "transcript_path": t2, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload2)) == 0
    assert len(sent) == 2   # second, genuinely-separate hit was silently dropped pre-fix


def test_usage_limit_debug_logs_feature_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\nNOTIFY_DEBUG=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "d1", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    log = (tmp_path / "debug.log").read_text()
    assert "usage-limit: feature disabled" in log


def test_usage_limit_debug_logs_when_not_last_entry(tmp_path, monkeypatch):
    # This is the exact blind spot that made a real missing-notification report
    # impossible to diagnose: previously reset_text is None returned silently.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\nNOTIFY_DEBUG=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,'
        '"message":{"content":[{"type":"text","text":"All done."}]}}',
    ])
    payload = {"session_id": "d2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    log = (tmp_path / "debug.log").read_text()
    assert "usage-limit: no rate-limit as last transcript entry" in log
    assert transcript in log


def test_usage_limit_debug_logs_duplicate_suppressed(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\nNOTIFY_DEBUG=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "d3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert hooks.run("stop", json.dumps(payload)) == 0   # same window again
    log = (tmp_path / "debug.log").read_text()
    assert "already claimed — suppressing duplicate" in log


def test_stop_failure_retries_transcript_read_to_bridge_write_race(tmp_path, monkeypatch):
    # Regression for an observed Claude Code race: StopFailure can fire ~20ms
    # before the terminal (error) transcript envelope is flushed to disk, so
    # the very first read finds nothing. Simulate that by making the first
    # latest_usage_limit() call return None and the second the real reset
    # text, as if the write landed between the two reads.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    monkeypatch.setattr(hooks, "_sleep", lambda s: None)  # don't actually wait in tests
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    real_latest = hooks.usagelimit.latest_usage_limit
    calls = {"n": 0}

    def flaky_latest(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return None   # first read: as if the line weren't written yet
        return real_latest(path)

    monkeypatch.setattr(hooks.usagelimit, "latest_usage_limit", flaky_latest)
    payload = {"session_id": "r1", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert calls["n"] == 2   # one retry, then it saw the real content
    assert len(sent) == 1
    assert "Claude Code usage limit reached" in sent[0]


def test_stop_does_not_retry_transcript_read(tmp_path, monkeypatch):
    # The retry is scoped to StopFailure only (rare) so the far more common
    # Stop path never pays extra latency on every normal turn.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    slept = []
    monkeypatch.setattr(hooks, "_sleep", lambda s: slept.append(s))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,'
        '"message":{"content":[{"type":"text","text":"All done."}]}}',
    ])
    calls = {"n": 0}
    real_latest = hooks.usagelimit.latest_usage_limit

    def counting_latest(path):
        calls["n"] += 1
        return real_latest(path)

    monkeypatch.setattr(hooks.usagelimit, "latest_usage_limit", counting_latest)
    payload = {"session_id": "r2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert calls["n"] == 1   # no retry on the Stop path
    assert slept == []


def test_stop_failure_retry_exhausted_falls_back_to_normal_error(tmp_path, monkeypatch):
    # A genuine non-rate-limit error must still fall through to the normal
    # "stopped with error" notification after the retry is exhausted.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    monkeypatch.setattr(hooks, "_sleep", lambda s: None)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"isApiErrorMessage":true,'
        '"error":"overloaded_error","message":{"content":'
        '[{"type":"text","text":"Overloaded"}]}}',
    ])
    payload = {"session_id": "r3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "stopped with error" in sent[0]


def test_stop_failure_logs_raw_error_payload_fields(tmp_path, monkeypatch):
    # Verification step, not a behavior change: Claude Code's official hooks
    # docs say StopFailure's own JSON payload already carries a structured
    # `error` field and an `error_details`/`last_assistant_message` pair with
    # no transcript read involved at all -- a potentially race-free
    # alternative to the current transcript-scan detection. Log them raw
    # (regardless of what transcript-based detection concludes) so a real
    # production rate_limit event can be inspected to confirm whether
    # last_assistant_message actually carries the reset-time text before any
    # detection logic is redesigned around it.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\nNOTIFY_DEBUG=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {
        "session_id": "p1", "transcript_path": transcript, "cwd": "/w",
        "error": "rate_limit", "error_details": "429 Too Many Requests",
        "last_assistant_message": "You've hit your session limit · resets 9pm (Asia/Hong_Kong)",
    }
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    log = (tmp_path / "debug.log").read_text()
    assert "stop_failure payload" in log
    assert "rate_limit" in log
    assert "429 Too Many Requests" in log
    assert "resets 9pm" in log


def test_stop_failure_logs_raw_payload_fields_when_absent(tmp_path, monkeypatch):
    # Older Claude Code versions (or a StopFailure with no error text) may
    # omit these fields entirely -- must never raise, just log None.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\nNOTIFY_DEBUG=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "p2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    log = (tmp_path / "debug.log").read_text()
    assert "stop_failure payload" in log


def test_stop_failure_uses_payload_directly_without_reading_transcript(tmp_path, monkeypatch):
    # The primary path as of 0004: StopFailure's own error + last_assistant_
    # message fields are race-free and (per a real production event) just as
    # rich as the transcript, so they're used directly — the transcript is
    # never read and no retry/sleep happens at all.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    slept = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    monkeypatch.setattr(hooks, "_sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def unexpected_transcript_read(path):
        calls["n"] += 1
        return None

    monkeypatch.setattr(hooks.usagelimit, "latest_usage_limit", unexpected_transcript_read)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {
        "session_id": "f1", "transcript_path": transcript, "cwd": "/w",
        "error": "rate_limit",
        "last_assistant_message": "You've hit your session limit · resets 9pm (Asia/Hong_Kong)",
    }
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "Claude Code usage limit reached" in sent[0]
    assert "resets 9pm" in sent[0]
    assert calls["n"] == 0   # transcript never read
    assert slept == []       # no retry/sleep needed


def test_stop_failure_payload_fallback_excludes_model_credits_error(tmp_path, monkeypatch):
    # Claude Code tags a per-model credits gate (e.g. Fable 5 without usage
    # credits enabled) with the same error="rate_limit" StopFailure uses for
    # a genuine account usage limit -- the fallback must not misclassify it
    # just because the transcript read came up empty.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    monkeypatch.setattr(hooks, "_sleep", lambda s: None)
    monkeypatch.setattr(hooks.usagelimit, "latest_usage_limit", lambda path: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {
        "session_id": "f4", "transcript_path": transcript, "cwd": "/w",
        "error": "rate_limit",
        "error_details": '429 {"error":{"details":{"error_code":"credits_required",'
                          '"model":"claude-fable-5"}}}',
        "last_assistant_message": "Fable 5 requires usage credits. Run /usage-credits "
                                   "to continue or switch models with /model.",
    }
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "stopped with error" in sent[0]


def test_stop_failure_falls_back_to_generic_text_when_payload_message_absent_too(tmp_path, monkeypatch):
    # last_assistant_message is documented as optional even when error is
    # present -- still must not crash or fall through to the generic path.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    monkeypatch.setattr(hooks, "_sleep", lambda s: None)
    monkeypatch.setattr(hooks.usagelimit, "latest_usage_limit", lambda path: None)
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {
        "session_id": "f2", "transcript_path": transcript, "cwd": "/w",
        "error": "rate_limit",
    }
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "Claude Code usage limit reached" in sent[0]


def test_stop_failure_prefers_payload_text_over_transcript_when_both_present(tmp_path, monkeypatch):
    # Reversed in 0004: payload is race-free and proven sufficient, so it
    # wins even when the transcript happens to also be available and would
    # have contained different (here: richer) text. Accepting this
    # occasional richness loss is the deliberate tradeoff for never reading
    # the transcript in the common case.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        _rate_limit_line("You've hit your session limit · resets 3pm (Asia/Hong_Kong)")
    ])
    payload = {
        "session_id": "f3", "transcript_path": transcript, "cwd": "/w",
        "error": "rate_limit",
        "last_assistant_message": "API Error: Rate limit reached",
    }
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "API Error: Rate limit reached" in sent[0]
    assert "resets 3pm" not in sent[0]


def test_stop_ignores_stale_rate_limit_predating_current_turn(tmp_path, monkeypatch):
    # The 05:20 phantom-reset bug: a session that hit a limit yesterday is
    # resumed today; the current turn finishes normally, but its reply hasn't
    # flushed yet, so the transcript's last assistant entry is still the OLD
    # rate-limit envelope. It predates the current turn's user message, so it
    # must be treated as a normal completion -- not re-detected as a fresh hit.
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    broadcasts = []
    monkeypatch.setattr(hooks.broadcast, "send_all", lambda c, t: broadcasts.append(t) or 0)
    transcript = _write_transcript(tmp_path, [
        _rate_limit_line_at("2026-07-22T18:14:00.000Z"),   # yesterday's limit
        _user_turn_line("2026-07-23T05:27:15.000Z"),       # today's "go on"
    ])
    payload = {"session_id": "st1", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert broadcasts == []                              # no usage-limit broadcast
    assert len(sent) == 1
    assert sent[0].startswith("Claude Code finished")    # normal completion


def test_stop_reread_of_same_limit_maps_to_same_window(tmp_path, monkeypatch):
    # A limit hit at 02:14 (reset 05:20) must map to the SAME window whether we
    # read it at 02:20 or re-read the identical stale envelope hours later. The
    # window key is anchored to when the limit was hit, not to read time, so the
    # re-read dedups against the original hit instead of rolling the reset to a
    # spurious next-day window and re-broadcasting. No turn-start line here, so
    # this isolates the window-key anchoring from the turn-correlation guard.
    zoneinfo = pytest.importorskip("zoneinfo")
    hkt = zoneinfo.ZoneInfo("Asia/Hong_Kong")
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    broadcasts = []
    monkeypatch.setattr(hooks.broadcast, "send_all", lambda c, t: broadcasts.append(t) or 0)
    transcript = _write_transcript(tmp_path, [
        _rate_limit_line_at("2026-07-22T18:14:00.000Z"),   # 02:14 HKT, resets 05:20
    ])
    payload = {"session_id": "st2", "transcript_path": transcript, "cwd": "/w"}
    monkeypatch.setattr(hooks, "_now",
                        lambda: datetime.datetime(2026, 7, 23, 2, 20, 0, tzinfo=hkt).timestamp())
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(broadcasts) == 1                          # first hit broadcasts
    monkeypatch.setattr(hooks, "_now",
                        lambda: datetime.datetime(2026, 7, 23, 13, 28, 0, tzinfo=hkt).timestamp())
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(broadcasts) == 1                          # re-read dedups: no 2nd broadcast
