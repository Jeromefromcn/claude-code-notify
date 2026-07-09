import json

from claude_code_notify import installer


def test_merge_adds_three_events():
    settings = {}
    out = installer.merge_hooks(settings, "/home/u/.claude/claude-code-notify")
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert event in out["hooks"]
        cmd = out["hooks"][event][0]["hooks"][0]["command"]
        assert "claude-code-notify" in cmd
        assert out["hooks"][event][0]["matcher"] == ""


def test_merge_is_idempotent():
    base = "/home/u/.claude/claude-code-notify"
    out = installer.merge_hooks({}, base)
    out2 = installer.merge_hooks(out, base)
    assert len(out2["hooks"]["Stop"]) == 1  # not duplicated


def test_merge_preserves_foreign_hooks():
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    out = installer.merge_hooks(settings, "/base/claude-code-notify")
    stops = out["hooks"]["Stop"]
    assert len(stops) == 2
    assert any("echo other" in e["hooks"][0]["command"] for e in stops)
    assert any("claude-code-notify" in e["hooks"][0]["command"] for e in stops)


def test_remove_only_ours():
    base = "/base/claude-code-notify"
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    merged = installer.merge_hooks(settings, base)
    removed = installer.remove_hooks(merged)
    stops = removed["hooks"]["Stop"]
    assert len(stops) == 1
    assert "echo other" in stops[0]["hooks"][0]["command"]


def test_remove_prunes_empty_event():
    base = "/base/claude-code-notify"
    merged = installer.merge_hooks({}, base)
    removed = installer.remove_hooks(merged)
    # All three events were only ours → they should be gone entirely.
    assert removed.get("hooks", {}) == {}


def test_merge_main_roundtrip(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    rc = installer.main(["merge", str(settings_file), "/base/claude-code-notify"])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert "Stop" in data["hooks"]
