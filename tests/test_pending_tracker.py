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
    assert data["launched"] == {"toolu_ag1": None}  # fixture has no "timestamp" field
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


def test_wrong_shape_state_falls_back(tmp_path):
    # Syntactically valid JSON that isn't a dict (null, [], "x", 42) must
    # not raise AttributeError from data.get(...) - it should fall back
    # to a fresh State() just like missing/corrupt files do.
    for bad_json in ("null", "[]", '"x"', "42"):
        state_path = tmp_path / "wrong_shape.state.json"
        state_path.write_text(bad_json)
        state = pt.load_state(str(state_path))
        assert state.offset == 0
        assert state.launched == {}
        assert state.resolved == set()


def test_load_state_migrates_legacy_list_format(tmp_path):
    # State files written before staleness support stored "launched" as a
    # bare list of ids with no timestamp. They must load as a dict with an
    # unknown (None) launch time per id, not crash or silently drop data.
    state_path = tmp_path / "legacy.state.json"
    state_path.write_text('{"offset": 10, "launched": ["a", "b"], "resolved": ["a"]}')
    state = pt.load_state(str(state_path))
    assert state.launched == {"a": None, "b": None}
    assert state.resolved == {"a"}
    assert state.offset == 10


def test_staleness_disabled_by_default(tmp_path):
    # No stale_seconds passed → old behavior: never expires, however old.
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2020-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), str(tmp_path / "s.state.json")) == 1


def test_fresh_launch_within_window_still_pending(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    now = pt._parse_iso("2026-01-01T01:00:00.000Z")  # 1h later, inside a 4h window
    pending = pt.compute_pending(str(src), str(tmp_path / "s.state.json"), stale_seconds=14400, now=now)
    assert pending == 1


def test_stale_launch_excluded_and_pruned_from_state(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    state_path = str(tmp_path / "s.state.json")
    now = pt._parse_iso("2026-01-01T05:00:00.000Z")  # 5h later, past a 4h window
    pending = pt.compute_pending(str(src), state_path, stale_seconds=14400, now=now)
    assert pending == 0
    data = json.loads(open(state_path).read())
    assert data["launched"] == {}  # expired entry is pruned, not just skipped


def test_missing_timestamp_treated_as_immediately_stale_when_enabled(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    pending = pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"),
        stale_seconds=14400, now=pt._parse_iso("2026-01-01T00:00:00.000Z"),
    )
    assert pending == 0


def test_on_stale_callback_receives_expired_ids(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    seen = []
    pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=60,
        now=pt._parse_iso("2026-01-01T01:00:00.000Z"), on_stale=seen.extend,
    )
    assert seen == ["a"]


def test_on_stale_not_called_when_nothing_expired(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    seen = []
    pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=14400,
        now=pt._parse_iso("2026-01-01T00:00:10.000Z"), on_stale=seen.extend,
    )
    assert seen == []


def test_resolved_launch_never_counted_stale_or_not(tmp_path):
    # A launch that already resolved must not appear in on_stale even if old.
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2020-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n'
    )
    seen = []
    pending = pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=60,
        now=pt._parse_iso("2026-01-01T00:00:00.000Z"), on_stale=seen.extend,
    )
    assert pending == 0
    assert seen == []


def test_load_state_ignores_legacy_finished_sent_key(tmp_path):
    # State files written by 0.6.0-0.7.1 carry a "finished_sent" key that's
    # no longer part of the schema — must load cleanly and just ignore it.
    state_path = tmp_path / "legacy.state.json"
    state_path.write_text('{"offset": 10, "launched": ["a"], "resolved": [], "finished_sent": true}')
    state = pt.load_state(str(state_path))
    assert state.launched == {"a": None}
    assert state.resolved == set()
