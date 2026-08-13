import json
import os
import shutil
import stat
import subprocess
import tarfile

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


def test_install_downloads_and_extracts_tarball(tmp_path):
    # Regression test for todo.md issue 9: the tarball-download branch of
    # install.sh — the one every real `curl | bash` install actually takes —
    # previously had no coverage; only the local-checkout `cp` branch did.
    # Builds a real gzipped tarball matching GitHub's archive layout (one
    # top-level wrapper dir, removed via --strip-components=1) and serves it
    # over a file:// URL via CLAUDE_NOTIFY_TARBALL_BASE, so this exercises
    # the real curl+tar code path without needing network access.
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    shutil.copy(os.path.join(REPO, "install.sh"), isolated / "install.sh")

    staging = tmp_path / "staging" / "claude-code-notify-main"
    shutil.copytree(
        os.path.join(REPO, "claude_code_notify"), staging / "claude_code_notify",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(os.path.join(REPO, "hooks"), staging / "hooks")

    tarball_root = tmp_path / "tarball_root"
    tarball_root.mkdir()
    with tarfile.open(tarball_root / "main.tar.gz", "w:gz") as tf:
        tf.add(staging, arcname="claude-code-notify-main")

    base = tmp_path / "claude-code-notify"
    settings = tmp_path / "settings.json"
    env = dict(
        os.environ,
        CLAUDE_NOTIFY_HOME=str(base),
        CLAUDE_SETTINGS=str(settings),
        CLAUDE_NOTIFY_TARBALL_BASE=f"file://{tarball_root}",
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", str(isolated / "install.sh"), "--non-interactive", "--version", "main"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert (base / "hooks" / "stop.sh").exists()
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert any(str(base) in e["hooks"][0]["command"] for e in data["hooks"][event])


def test_install_tag_version_no_spurious_curl_error(tmp_path):
    # Regression test: install.sh fetched the tarball by guessing "branch"
    # first (archive/refs/heads/<ref>.tar.gz), falling back to "tag"
    # (archive/refs/tags/<ref>.tar.gz) only after that failed. But the
    # default install path (no --version) resolves VERSION to a release
    # tag, not a branch, via the GitHub API — so every default `curl | bash`
    # install hit a guaranteed 404 on the first guess before the fallback
    # quietly succeeded, printing a misleading curl error to stderr on every
    # single ordinary install. Reproduces this with a tag-shaped --version
    # and a tarball fixture served at the flat (ref-type-agnostic) path
    # GitHub's archive/<ref>.tar.gz endpoint actually uses — asserts both
    # that install still succeeds AND that stderr carries no curl error
    # noise from a failed first guess.
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    shutil.copy(os.path.join(REPO, "install.sh"), isolated / "install.sh")

    staging = tmp_path / "staging" / "claude-code-notify-v9.9.9"
    shutil.copytree(
        os.path.join(REPO, "claude_code_notify"), staging / "claude_code_notify",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(os.path.join(REPO, "hooks"), staging / "hooks")

    tarball_root = tmp_path / "tarball_root"
    tarball_root.mkdir()
    with tarfile.open(tarball_root / "v9.9.9.tar.gz", "w:gz") as tf:
        tf.add(staging, arcname="claude-code-notify-v9.9.9")

    base = tmp_path / "claude-code-notify"
    settings = tmp_path / "settings.json"
    env = dict(
        os.environ,
        CLAUDE_NOTIFY_HOME=str(base),
        CLAUDE_SETTINGS=str(settings),
        CLAUDE_NOTIFY_TARBALL_BASE=f"file://{tarball_root}",
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", str(isolated / "install.sh"), "--non-interactive", "--version", "v9.9.9"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert result.stderr == ""


def test_install_via_stdin_pipe_ignores_unrelated_cwd_package(tmp_path):
    # Regression test: install.sh derived SRC_DIR from BASH_SOURCE[0], which
    # is unset when the script runs the real `curl | bash` way (fed through
    # stdin, not executed as a file). Under `set -u` this threw "BASH_SOURCE
    # [0]: unbound variable" — but since a failing command substitution
    # embedded in an argument doesn't trigger `set -e`, SRC_DIR silently fell
    # back to the caller's $PWD via `cd ""`. If that directory happened to
    # contain a claude_code_notify/ folder (e.g. someone ran the documented
    # one-liner from inside a clone of this repo out of habit), the installer
    # would silently copy those local files instead of downloading and
    # verifying the real release tarball — discovered when reinstalling on a
    # dev machine whose shell cwd was the repo checkout.
    #
    # Reproduces the real invocation style (`bash -c "$(cat install.sh)" --
    # args`, so BASH_SOURCE is genuinely unset, same as piping through `bash`)
    # from a cwd seeded with decoy claude_code_notify/hooks directories, and
    # asserts the real tarball (via the file:// fixture) is what gets
    # installed, not the decoy content.
    decoy_cwd = tmp_path / "decoy_cwd"
    (decoy_cwd / "claude_code_notify").mkdir(parents=True)
    (decoy_cwd / "claude_code_notify" / "DECOY.txt").write_text("should never be copied")
    (decoy_cwd / "hooks").mkdir()
    (decoy_cwd / "hooks" / "DECOY_STOP.sh").write_text("#!/usr/bin/env bash\necho decoy\n")

    staging = tmp_path / "staging" / "claude-code-notify-main"
    shutil.copytree(
        os.path.join(REPO, "claude_code_notify"), staging / "claude_code_notify",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(os.path.join(REPO, "hooks"), staging / "hooks")
    tarball_root = tmp_path / "tarball_root"
    tarball_root.mkdir()
    with tarfile.open(tarball_root / "main.tar.gz", "w:gz") as tf:
        tf.add(staging, arcname="claude-code-notify-main")

    install_script = open(os.path.join(REPO, "install.sh")).read()
    base = tmp_path / "claude-code-notify"
    settings = tmp_path / "settings.json"
    env = dict(
        os.environ,
        CLAUDE_NOTIFY_HOME=str(base),
        CLAUDE_SETTINGS=str(settings),
        CLAUDE_NOTIFY_TARBALL_BASE=f"file://{tarball_root}",
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", "-c", install_script, "--", "--non-interactive", "--version", "main"],
        capture_output=True, text=True, env=env, cwd=decoy_cwd,
    )
    assert result.returncode == 0, result.stderr
    assert not (base / "claude_code_notify" / "DECOY.txt").exists()
    assert not (base / "hooks" / "DECOY_STOP.sh").exists()
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert (base / "hooks" / "stop.sh").exists()


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


def test_install_with_corrupt_settings_fails_cleanly(tmp_path):
    # Regression test for todo.md issue 13: a hand-corrupted settings.json
    # must abort install.sh with a clean message via installer.py's own
    # exit code, not a raw Python traceback — and must be left untouched
    # so the user can fix or inspect it.
    settings = tmp_path / "settings.json"
    settings.write_text("not json{")
    result, base, settings = _run(tmp_path, "--non-interactive", settings=settings)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "not valid JSON" in result.stderr
    assert settings.read_text() == "not json{"


def test_uninstall_kills_live_sleeper(tmp_path):
    import sys
    import time
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0, result.stderr
    # A live sleeper recorded under the usage-limit state dir.
    state = base / "state" / "usage_limit"
    state.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (state / "w1.pid").write_text(str(proc.pid))
    result, base, settings = _run(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=10) is not None       # sleeper was terminated
    assert not (base / "state").exists()           # state removed
