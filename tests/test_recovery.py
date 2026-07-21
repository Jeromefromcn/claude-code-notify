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


def test_wait_returns_when_target_reached():
    clock = iter([0, 100, 200])       # deadline-probe, then two loop reads
    slept = []
    recovery._wait(150, lambda: next(clock),
                   lambda s: slept.append(s), lambda: False)
    assert slept == [50]              # slept min(150-100, 60) once, then exited


def test_wait_exits_early_when_done():
    clock = iter([0, 100, 100])
    slept = []
    recovery._wait(150, lambda: next(clock),
                   lambda s: slept.append(s), lambda: True)
    assert slept == []                # done flag short-circuits before sleeping


def test_fire_broadcasts_once(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n")
    sent = []
    ok = recovery.fire(str(tmp_path), "w1", "WHEN",
                       send=lambda c, t: sent.append((c.chat_id, t)))
    assert ok is True
    assert sent == [("999", "Claude Code usage limit reset | WHEN")]
    # Second fire for the same window is blocked by the .done claim.
    sent.clear()
    ok2 = recovery.fire(str(tmp_path), "w1", "WHEN",
                        send=lambda c, t: sent.append(t))
    assert ok2 is False
    assert sent == []


def test_main_kill_all_delegates(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(recovery, "kill_all", lambda base: called.append(base))
    assert recovery.main(["--kill-all", "--base-dir", str(tmp_path)]) == 0
    assert called == [str(tmp_path)]


def test_main_sleeper_fires_when_target_is_past(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(recovery, "fire",
                        lambda base, window, when: fired.append((base, window)))
    rc = recovery.main(["--base-dir", str(tmp_path), "--window", "w1", "--target", "1"])
    assert rc == 0
    assert fired == [(str(tmp_path), "w1")]
    # pid file is cleaned up on exit
    assert not os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.pid"))


def test_main_sleeper_does_not_fire_when_cap_trips_before_target(tmp_path, monkeypatch):
    # Not in the brief; added because this is the single correctness-critical
    # property called out for this task: a sleeper whose _wait() loop exits
    # via the 8-day CAP guard (not because the target was reached) must not
    # fire. Simulates that by making the process clock jump past the CAP
    # while the target stays far in the future.
    fired = []
    monkeypatch.setattr(recovery, "fire",
                        lambda base, window, when: fired.append((base, window)))
    base_time = 1_000_000.0
    calls = {"n": 0}

    def fake_time():
        calls["n"] += 1
        # call 1 is _wait's deadline probe; every call after jumps past the
        # CAP so the very first loop read trips the deadline branch.
        if calls["n"] == 1:
            return base_time
        return base_time + usagelimit.CAP_SECONDS + 1

    monkeypatch.setattr(recovery.time, "time", fake_time)
    monkeypatch.setattr(recovery.time, "sleep", lambda s: None)
    target = base_time + usagelimit.CAP_SECONDS + 1_000_000  # far beyond the cap
    rc = recovery.main(["--base-dir", str(tmp_path), "--window", "w1", "--target", str(target)])
    assert rc == 0
    assert fired == []  # capped loop must NOT fire
    assert not os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.pid"))


def test_main_removes_pid_file_even_if_fire_raises(tmp_path, monkeypatch):
    # Not in the brief; added to exercise the stated constraint that pid
    # cleanup happens in a finally block, even when fire() raises.
    def boom(base, window, when):
        raise RuntimeError("boom")

    monkeypatch.setattr(recovery, "fire", boom)
    rc = recovery.main(["--base-dir", str(tmp_path), "--window", "w1", "--target", "1"])
    assert rc == 0  # main() never propagates
    assert not os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.pid"))
