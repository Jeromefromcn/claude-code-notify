import json
import os
import stat

from claude_code_notify import pending_tracker as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _pending(name, tmp_path):
    return pt.compute_pending(
        os.path.join(FIX, name), str(tmp_path / f"{name}.state.json")
    )


def test_foreground_only_pending_zero(tmp_path):
    assert _pending("foreground_only.jsonl", tmp_path) == 0


def test_background_agent_pending_one(tmp_path):
    assert _pending("bg_agent_pending.jsonl", tmp_path) == 1


def test_background_agent_completed_zero(tmp_path):
    assert _pending("bg_agent_completed.jsonl", tmp_path) == 0


def test_background_bash_ack_only_still_pending(tmp_path):
    # Regression: the immediate ack must not resolve the launch.
    assert _pending("bg_bash_ack_only.jsonl", tmp_path) == 1


def test_background_bash_completed_zero(tmp_path):
    assert _pending("bg_bash_completed.jsonl", tmp_path) == 0


def test_notification_twice_resolves_once(tmp_path):
    assert _pending("notif_twice.jsonl", tmp_path) == 0


def test_state_persists_and_is_chmod_600(tmp_path):
    state_path = str(tmp_path / "s.state.json")
    pt.compute_pending(os.path.join(FIX, "bg_agent_pending.jsonl"), state_path)
    assert os.path.exists(state_path)
    mode = stat.S_IMODE(os.stat(state_path).st_mode)
    assert mode == 0o600
    data = json.loads(open(state_path).read())
    assert data["launched"] == ["toolu_ag1"]
    assert data["resolved"] == []
    assert data["offset"] > 0


def test_incremental_across_appends(tmp_path):
    src = tmp_path / "live.jsonl"
    state_path = str(tmp_path / "live.state.json")
    src.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), state_path) == 1
    with open(src, "a") as fh:
        fh.write('{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n')
    assert pt.compute_pending(str(src), state_path) == 0


def test_rotation_triggers_full_rescan(tmp_path):
    src = tmp_path / "rot.jsonl"
    state_path = str(tmp_path / "rot.state.json")
    src.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), state_path) == 1
    # File shrinks (rotated) and now shows a completed, different task.
    src.write_text('{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n')
    # Full rescan from 0: launch "a" is gone, only its completion remains → 0 pending.
    assert pt.compute_pending(str(src), state_path) == 0


def test_corrupt_state_falls_back(tmp_path):
    state_path = tmp_path / "c.state.json"
    state_path.write_text("{not valid json")
    result = pt.compute_pending(os.path.join(FIX, "bg_agent_pending.jsonl"), str(state_path))
    assert result == 1
