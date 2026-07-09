import json
import os
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(tmp_path, *args):
    # Use a base dir name that actually contains installer.py's
    # MANAGED_MARKER ("claude-code-notify"), matching the real default
    # install path (~/.claude/claude-code-notify/). The marker-in-path
    # tagging design only recognizes entries whose command path contains
    # that substring, so an arbitrary tmp dir name (e.g. "ccn") would make
    # every hook look "foreign" and break the merge/idempotency/uninstall
    # assertions below for reasons unrelated to install.sh itself.
    base = tmp_path / "claude-code-notify"
    settings = tmp_path / "settings.json"
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


@pytest.mark.skipif(subprocess.run(["which", "bash"]).returncode != 0, reason="bash required")
def test_install_places_files_and_merges(tmp_path):
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert (base / "hooks" / "stop.sh").exists()
    assert stat.S_IMODE(os.stat(base / "config.env").st_mode) == 0o600
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert any("claude-code-notify" in e["hooks"][0]["command"] for e in data["hooks"][event])


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
