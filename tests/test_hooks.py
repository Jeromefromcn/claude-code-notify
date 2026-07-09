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
