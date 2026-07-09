import json
import os
import shutil
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "bash"]).returncode != 0, reason="bash required"
)


def _run(tmp_path, *args, base_name="claude-code-notify", settings=None):
    # installer.py identifies its own hook entries via a state file kept
    # beside settings.json (see docs/adr/0001-hook-installation-tracking.md),
    # not by the base dir name containing a marker substring, so an
    # arbitrary base_name is safe here and exercises the real behavior.
    base = tmp_path / base_name
    settings = settings if settings is not None else tmp_path / "settings.json"
    env = dict(
        os.environ,
        CLAUDE_NOTIFY_HOME=str(base),
        CLAUDE_SETTINGS=str(settings),
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", os.path.join(REPO, "install.sh"), *args],
        capture_output=True, text=True, env=env,
    )
    return result, base, settings


def test_install_places_files_and_merges(tmp_path):
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert (base / "hooks" / "stop.sh").exists()
    assert stat.S_IMODE(os.stat(base / "config.env").st_mode) == 0o600
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert any(str(base) in e["hooks"][0]["command"] for e in data["hooks"][event])


def test_install_is_idempotent(tmp_path):
    _run(tmp_path, "--non-interactive")
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["Stop"]) == 1  # no duplicate entry


def test_upgrade_keeps_config(tmp_path):
    _run(tmp_path, "--non-interactive")
    base = tmp_path / "claude-code-notify"
    (base / "config.env").write_text("TELEGRAM_BOT_TOKEN=keepme\nTELEGRAM_CHAT_ID=1\n")
    _run(tmp_path, "--non-interactive")
    assert "keepme" in (base / "config.env").read_text()


def test_uninstall_reverts_settings(tmp_path):
    _run(tmp_path, "--non-interactive")
    result, base, settings = _run(tmp_path, "--uninstall")
    assert result.returncode == 0
    data = json.loads(settings.read_text())
    assert data.get("hooks", {}) == {}
    assert not (base / "hooks").exists()


def test_reinstall_after_home_change_replaces_hooks(tmp_path):
    # Regression test for todo.md issue 7 / ADR 0001: CLAUDE_NOTIFY_HOME
    # changing between two installs (same settings.json) must not leave a
    # stale hook entry pointing at the old base dir behind.
    settings = tmp_path / "settings.json"
    _run(tmp_path, "--non-interactive", base_name="old-home", settings=settings)
    result, new_base, settings = _run(
        tmp_path, "--non-interactive", base_name="new-home", settings=settings
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        entries = data["hooks"][event]
        assert len(entries) == 1
        assert str(new_base) in entries[0]["hooks"][0]["command"]


def test_uninstall_after_home_change_still_clears_settings(tmp_path):
    settings = tmp_path / "settings.json"
    _run(tmp_path, "--non-interactive", base_name="old-home", settings=settings)
    _run(tmp_path, "--non-interactive", base_name="new-home", settings=settings)
    result, _, settings = _run(
        tmp_path, "--uninstall", base_name="new-home", settings=settings
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(settings.read_text())
    assert data.get("hooks", {}) == {}


def test_latest_release_lookup_failure_warns_before_falling_back_to_main(tmp_path):
    # Regression test for todo.md issue 8: if resolving the latest GitHub
    # release tag fails (offline, rate-limited, no releases yet), install.sh
    # must print a warning instead of silently using "main".
    #
    # Force the tarball-download branch (not the local-checkout `cp` branch)
    # by copying install.sh alone into a directory with no sibling
    # claude_code_notify package.
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    shutil.copy(os.path.join(REPO, "install.sh"), isolated / "install.sh")

    # Stub `curl` on PATH to simulate every network call failing, as it
    # would if the machine were offline or GitHub rate-limited it.
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    curl_stub = stub_bin / "curl"
    curl_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl_stub.chmod(0o755)

    env = dict(
        os.environ,
        PATH=f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        CLAUDE_NOTIFY_HOME=str(tmp_path / "ccn-home"),
        CLAUDE_SETTINGS=str(tmp_path / "settings.json"),
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", str(isolated / "install.sh"), "--non-interactive"],
        capture_output=True, text=True, env=env,
    )
    # The tarball download also fails offline, so install still aborts --
    # this test only asserts the warning is printed before that happens.
    assert "main" in result.stderr
    assert "warn" in result.stderr.lower()


def test_install_migrates_legacy_entries_without_state_file(tmp_path):
    # Simulates upgrading a real v0.1.0 install: settings.json already has
    # entries written by the old marker-substring installer, and no state
    # file exists yet (it's a new concept). A fresh install.sh run must
    # adopt and replace them, not duplicate. See ADR 0001.
    base_name = "claude-code-notify"
    base = tmp_path / base_name
    settings = tmp_path / "settings.json"
    legacy_hooks = {
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": str(base / "hooks" / "stop.sh")}]}],
        "StopFailure": [{"matcher": "", "hooks": [
            {"type": "command", "command": str(base / "hooks" / "stop_failure.sh")}]}],
        "PermissionRequest": [{"matcher": "", "hooks": [
            {"type": "command", "command": str(base / "hooks" / "permission_request.sh")}]}],
    }
    settings.write_text(json.dumps({"hooks": legacy_hooks}))
    result, base, settings = _run(
        tmp_path, "--non-interactive", base_name=base_name, settings=settings
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert len(data["hooks"][event]) == 1  # adopted, not duplicated
    state_file = settings.parent / ".claude-code-notify-hooks.json"
    assert state_file.exists()  # migration produced a state file going forward
