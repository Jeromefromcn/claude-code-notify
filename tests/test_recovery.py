import os
import signal
import subprocess
import sys
import time

from claude_code_notify import recovery, usagelimit


def test_spawn_is_single_instance_and_detached(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(recovery.subprocess, "Popen", FakePopen)
    recovery.spawn(str(tmp_path), "w1", 1_800_000_000)
    recovery.spawn(str(tmp_path), "w1", 1_800_000_000)  # same window -> blocked

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "claude_code_notify.recovery"]
    assert "--target" in argv and "1800000000" in argv
    assert kwargs.get("start_new_session") is True
    assert "secret" not in " ".join(argv)  # never a token on argv
    assert os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.sleeper"))


def test_spawn_never_raises_when_popen_fails(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no such file or directory")

    monkeypatch.setattr(recovery.subprocess, "Popen", boom)
    recovery.spawn(str(tmp_path), "w1", 1_800_000_000)  # must not raise
    # the claim was still taken, even though the launch itself failed
    assert os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.sleeper"))


def test_kill_all_signals_valid_pids_only(tmp_path, monkeypatch):
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "w1.pid"), "w") as fh:
        fh.write("4242")
    with open(os.path.join(d, "bad.pid"), "w") as fh:
        fh.write("not-an-int")
    killed = []
    monkeypatch.setattr(recovery.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    recovery.kill_all(str(tmp_path))
    assert killed == [(4242, signal.SIGTERM)]


def test_kill_all_terminates_a_real_process(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "w1.pid"), "w") as fh:
        fh.write(str(proc.pid))
    recovery.kill_all(str(tmp_path))
    assert proc.wait(timeout=10) is not None  # actually died
