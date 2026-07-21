import json
import os
import stat

from claude_code_notify import usagelimit


def _write(tmp_path, envelopes):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in envelopes))
    return str(path)


def _rate_limit(text="You've hit your session limit · resets 9pm (Asia/Hong_Kong)"):
    return {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
            "error": "rate_limit", "apiErrorStatus": 429,
            "message": {"model": "<synthetic>",
                        "content": [{"type": "text", "text": text}]}}


def test_detects_rate_limit_as_last_assistant(tmp_path):
    path = _write(tmp_path, [_rate_limit()])
    assert usagelimit.latest_usage_limit(path) == \
        "You've hit your session limit · resets 9pm (Asia/Hong_Kong)"


def test_ignores_trailing_non_assistant_lines(tmp_path):
    path = _write(tmp_path, [
        _rate_limit(),
        {"type": "queue-operation", "content": "x"},
        {"type": "user", "isSidechain": False, "message": {"content": "hi"}},
    ])
    assert usagelimit.latest_usage_limit(path) is not None


def test_stale_rate_limit_before_normal_turn_is_ignored(tmp_path):
    path = _write(tmp_path, [
        _rate_limit(),
        {"type": "assistant", "isSidechain": False,
         "message": {"content": [{"type": "text", "text": "done"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_auth_error_is_not_a_usage_limit(tmp_path):
    path = _write(tmp_path, [
        {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
         "error": "authentication_failed",
         "message": {"content": [{"type": "text", "text": "OAuth expired"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_normal_finish_is_not_a_usage_limit(tmp_path):
    path = _write(tmp_path, [
        {"type": "assistant", "isSidechain": False,
         "message": {"content": [{"type": "text", "text": "all done"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_missing_file_returns_none(tmp_path):
    assert usagelimit.latest_usage_limit(str(tmp_path / "nope.jsonl")) is None


def test_window_key_stable_and_distinct():
    a = usagelimit.window_key("resets 9pm (Asia/Hong_Kong)")
    b = usagelimit.window_key("resets 9pm (Asia/Hong_Kong)")
    c = usagelimit.window_key("resets 10pm (Asia/Hong_Kong)")
    assert a == b and a != c
    assert len(a) == 16


def test_claim_is_single_winner(tmp_path):
    assert usagelimit.claim_hit(str(tmp_path), "w1") is True
    assert usagelimit.claim_hit(str(tmp_path), "w1") is False
    marker = os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.hit")
    assert os.path.exists(marker)
    mode = stat.S_IMODE(os.stat(marker).st_mode)
    assert mode == 0o600


def test_claim_generic_names(tmp_path):
    assert usagelimit.claim(str(tmp_path), "w1.sleeper") is True
    assert usagelimit.claim(str(tmp_path), "w1.sleeper") is False
    sleeper = os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.sleeper")
    mode = stat.S_IMODE(os.stat(sleeper).st_mode)
    assert mode == 0o600


def test_gc_removes_old_files_keeps_fresh(tmp_path):
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    old = os.path.join(d, "old.hit")
    fresh = os.path.join(d, "fresh.hit")
    open(old, "w").close()
    open(fresh, "w").close()
    now = 1_000_000_000.0
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    os.utime(fresh, (now - 1 * 86400, now - 1 * 86400))
    usagelimit.gc(str(tmp_path), now)
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


def test_cap_is_at_least_one_week():
    assert usagelimit.CAP_SECONDS >= 7 * 24 * 3600
