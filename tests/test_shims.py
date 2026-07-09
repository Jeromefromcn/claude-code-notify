import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    )
    assert result.returncode == 0
    # No marker written because pending > 0 → nothing sent.
    assert not (tmp_path / "state" / "s.marker").exists()


def test_all_three_shims_exist_and_executable():
    for name in ("stop.sh", "stop_failure.sh", "permission_request.sh"):
        path = os.path.join(REPO, "hooks", name)
        assert os.path.exists(path)
        assert os.access(path, os.X_OK)
