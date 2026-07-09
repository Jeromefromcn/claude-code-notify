import json
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _unrelated_cwd(tmp_path):
    # Claude Code invokes hooks with cwd set to the user's project
    # directory, which has nothing to do with this repo or the install
    # dir. Running subprocesses from a directory that is neither proves
    # the shim's `$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)`
    # resolution is caller-cwd independent, rather than only passing by
    # accident because pytest happens to run from the repo root.
    d = tmp_path / "unrelated-caller-cwd"
    d.mkdir()
    return d


def test_stop_shim_forwards_stdin(tmp_path):
    # A pending background Agent → shim runs, exits 0, sends nothing.
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    payload = json.dumps({"session_id": "s", "transcript_path": str(transcript), "cwd": "/w"})
    env = dict(os.environ, CLAUDE_NOTIFY_HOME=str(tmp_path))
    result = subprocess.run(
        ["bash", os.path.join(REPO, "hooks", "stop.sh")],
        input=payload, capture_output=True, text=True, env=env,
        cwd=str(_unrelated_cwd(tmp_path)),
    )
    assert result.returncode == 0
    # No marker written because pending > 0 → nothing sent.
    assert not (tmp_path / "state" / "s.marker").exists()


def test_all_three_shims_exist_and_executable():
    for name in ("stop.sh", "stop_failure.sh", "permission_request.sh"):
        path = os.path.join(REPO, "hooks", name)
        assert os.path.exists(path)
        assert os.access(path, os.X_OK)


@pytest.mark.parametrize(
    "script,event",
    [
        ("stop.sh", "stop"),
        ("stop_failure.sh", "stop_failure"),
        ("permission_request.sh", "permission_request"),
    ],
)
def test_shim_forwards_event_to_correct_handler(tmp_path, script, event):
    # Regression test for todo.md issue 12: previously only stop.sh had
    # e2e subprocess coverage; stop_failure.sh / permission_request.sh
    # only had existence/executable checks, so nothing proved their
    # positional event-name argument actually reaches the matching
    # hooks.py handler (as opposed to e.g. both silently invoking "stop").
    #
    # TELEGRAM_API_BASE points at a closed local port (same technique as
    # test_stop_shim_forwards_stdin, deliberately not a real Telegram
    # call) so notifier.send() always fails with a connection error.
    # hooks.run()'s catch-all then logs "error in {event}: ..." where
    # {event} is exactly the argv value the shim passed through — so
    # observing that line via NOTIFY_DEBUG proves the shim threaded the
    # right event name to hooks.py, independent of network availability.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\nNOTIFY_DEBUG=1\n"
    )
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")  # no unresolved background dispatch → pending 0
    payload = json.dumps({"session_id": "s", "transcript_path": str(transcript), "cwd": "/w"})
    env = dict(os.environ, CLAUDE_NOTIFY_HOME=str(home))
    result = subprocess.run(
        ["bash", os.path.join(REPO, "hooks", script)],
        input=payload, capture_output=True, text=True, env=env,
        cwd=str(_unrelated_cwd(tmp_path)),
    )
    assert result.returncode == 0
    log = (home / "debug.log").read_text()
    assert f"error in {event}:" in log
