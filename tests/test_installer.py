import json

from claude_code_notify import installer


def _empty_state():
    return {"commands": {}}


def test_merge_adds_three_events():
    settings = {}
    out, state = installer.merge_hooks(settings, "/home/u/.claude/claude-code-notify", _empty_state())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert event in out["hooks"]
        cmd = out["hooks"][event][0]["hooks"][0]["command"]
        assert "claude-code-notify" in cmd
        assert out["hooks"][event][0]["matcher"] == ""
        assert state["commands"][event] == cmd


def test_merge_is_idempotent():
    base = "/home/u/.claude/claude-code-notify"
    out, state = installer.merge_hooks({}, base, _empty_state())
    out2, state2 = installer.merge_hooks(out, base, state)
    assert len(out2["hooks"]["Stop"]) == 1  # not duplicated
    assert state2["commands"]["Stop"] == state["commands"]["Stop"]


def test_merge_preserves_foreign_hooks():
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    out, _ = installer.merge_hooks(settings, "/base/claude-code-notify", _empty_state())
    stops = out["hooks"]["Stop"]
    assert len(stops) == 2
    assert any("echo other" in e["hooks"][0]["command"] for e in stops)
    assert any("claude-code-notify" in e["hooks"][0]["command"] for e in stops)


def test_remove_only_ours():
    base = "/base/claude-code-notify"
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    merged, state = installer.merge_hooks(settings, base, _empty_state())
    removed = installer.remove_hooks(merged, state)
    stops = removed["hooks"]["Stop"]
    assert len(stops) == 1
    assert "echo other" in stops[0]["hooks"][0]["command"]


def test_remove_prunes_empty_event():
    base = "/base/claude-code-notify"
    merged, state = installer.merge_hooks({}, base, _empty_state())
    removed = installer.remove_hooks(merged, state)
    # All three events were only ours → they should be gone entirely.
    assert removed.get("hooks", {}) == {}


def test_merge_survives_base_dir_change():
    # Reinstalling at a completely different path (that doesn't even
    # contain the literal substring "claude-code-notify") must still
    # replace the old entry instead of duplicating it, as long as the
    # state recorded from the first install is passed in. Regression
    # test for todo.md issue 7 / ADR 0001.
    out_a, state_a = installer.merge_hooks({}, "/base/ccn-old", _empty_state())
    out_b, state_b = installer.merge_hooks(out_a, "/base/ccn-new", state_a)
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        entries = out_b["hooks"][event]
        assert len(entries) == 1
        assert "/base/ccn-new/" in entries[0]["hooks"][0]["command"]
        assert state_b["commands"][event] == entries[0]["hooks"][0]["command"]


def test_remove_uses_recorded_state_regardless_of_base_dir():
    out_a, state_a = installer.merge_hooks({}, "/base/ccn-old", _empty_state())
    out_b, state_b = installer.merge_hooks(out_a, "/base/ccn-new", state_a)
    removed = installer.remove_hooks(out_b, state_b)
    assert removed.get("hooks", {}) == {}


def test_merge_migrates_legacy_marker_entries_with_no_state():
    # Simulates upgrading from the pre-ADR-0001 installer: entries already
    # exist in settings.json (written by the old marker-substring logic)
    # but there's no state file yet. The first merge on the new code
    # should recognize and replace them, not duplicate.
    base = "/home/u/.claude/claude-code-notify"
    legacy_settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command",
                                    "command": f"{base}/hooks/stop.sh"}]}
    ]}}
    out, state = installer.merge_hooks(legacy_settings, base, _empty_state())
    assert len(out["hooks"]["Stop"]) == 1
    assert state["commands"]["Stop"] == out["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_merge_main_roundtrip(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    rc = installer.main(["merge", str(settings_file), "/base/claude-code-notify"])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert "Stop" in data["hooks"]
    state_file = tmp_path / installer.STATE_FILENAME
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["commands"]["Stop"] == data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_main_roundtrip_survives_base_dir_change(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    installer.main(["merge", str(settings_file), "/base/ccn-old"])
    rc = installer.main(["merge", str(settings_file), "/base/ccn-new"])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert len(data["hooks"]["Stop"]) == 1
    assert "/base/ccn-new/" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_main_remove_roundtrip_deletes_state(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    installer.main(["merge", str(settings_file), "/base/claude-code-notify"])
    rc = installer.main(["remove", str(settings_file)])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert data.get("hooks", {}) == {}
    assert not (tmp_path / installer.STATE_FILENAME).exists()


def test_load_state_missing_file_returns_default(tmp_path):
    assert installer.load_state(str(tmp_path / "nope.json")) == _empty_state()


def test_load_state_falls_back_on_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json{")
    assert installer.load_state(str(path)) == _empty_state()


def test_load_state_falls_back_on_non_dict_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("null")
    assert installer.load_state(str(path)) == _empty_state()

    path.write_text("[1, 2, 3]")
    assert installer.load_state(str(path)) == _empty_state()


def test_load_state_falls_back_when_commands_wrong_type(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"commands": "oops"}))
    assert installer.load_state(str(path)) == _empty_state()


def test_main_merge_recovers_from_corrupt_state_file(tmp_path):
    # hooks.py's core rule ("never let internal errors interrupt the user")
    # applies to installer.py too: a hand-corrupted state file must not
    # crash the install, just fall back to treating it as a fresh install.
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    (tmp_path / installer.STATE_FILENAME).write_text("not json{")
    rc = installer.main(["merge", str(settings_file), "/base/claude-code-notify"])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert "Stop" in data["hooks"]
    assert len(data["hooks"]["Stop"]) == 1


def test_main_remove_recovers_from_corrupt_state_file(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}))
    (tmp_path / installer.STATE_FILENAME).write_text("not json{")
    rc = installer.main(["remove", str(settings_file)])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    # Nothing recorded (corrupt state → empty) and no legacy marker match →
    # the foreign hook is left untouched.
    assert "echo other" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_merge_treats_multi_hook_entry_as_ours_via_legacy_marker():
    # Entries are matched as a whole: if any hook inside an entry matches,
    # the entire entry — including any other hook bundled into it — is
    # replaced. This documents existing behavior inherited from the
    # pre-ADR-0001 substring matching; bundling unrelated hooks into the
    # same entry as ours was never a supported configuration.
    base = "/home/u/.claude/claude-code-notify"
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [
            {"type": "command", "command": "echo other"},
            {"type": "command", "command": f"{base}/hooks/stop.sh"},
        ]}
    ]}}
    out, _ = installer.merge_hooks(settings, base, _empty_state())
    stops = out["hooks"]["Stop"]
    assert len(stops) == 1
    assert stops[0]["hooks"][0]["command"] == f"{base}/hooks/stop.sh"


def test_remove_matches_multi_hook_entry_by_recorded_command():
    base = "/base/claude-code-notify"
    merged, state = installer.merge_hooks({}, base, _empty_state())
    recorded = state["commands"]["Stop"]
    merged["hooks"]["Stop"] = [
        {"matcher": "", "hooks": [
            {"type": "command", "command": "echo other"},
            {"type": "command", "command": recorded},
        ]}
    ]
    removed = installer.remove_hooks(merged, state)
    assert removed.get("hooks", {}) == {}
