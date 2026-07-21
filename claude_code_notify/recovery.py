import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import broadcast
from . import config as cfg
from . import notifier
from . import usagelimit


def spawn(base_dir, window, target_epoch):
    """Launch one detached sleeper for this window. Single-instance via an
    atomic claim. No secrets on argv. Never raises."""
    if not usagelimit.claim(base_dir, window + ".sleeper"):
        return
    try:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(base_dir) + (os.pathsep + existing if existing else "")
        subprocess.Popen(
            [sys.executable, "-m", "claude_code_notify.recovery",
             "--base-dir", str(base_dir), "--window", str(window),
             "--target", str(int(target_epoch))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    except Exception:
        pass  # a spawn failure must never break the hook


def kill_all(base_dir):
    """SIGTERM every live sleeper recorded under the usage-limit state dir."""
    directory = usagelimit.usage_state_dir(base_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".pid"):
            continue
        try:
            with open(os.path.join(directory, name)) as fh:
                pid = int(fh.read().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass


def _wait(target, now_fn, sleep_fn, is_done):
    deadline = now_fn() + usagelimit.CAP_SECONDS
    while True:
        now = now_fn()
        if now >= target or now >= deadline:
            return
        if is_done():
            return
        sleep_fn(min(target - now, 60))


def fire(base_dir, window, when_str, load=None, send=None):
    """Broadcast the reset message exactly once per window (guarded by a .done
    claim). Never raises."""
    if not usagelimit.claim(base_dir, window + ".done"):
        return False
    loader = cfg.load if load is None else load
    try:
        config = loader(base=base_dir)
    except Exception:
        return False
    message = notifier.build_message("usage-limit-reset", "", when_str)
    broadcast.send_all(config, message, send=send)
    return True


def _parse_args(argv):
    opts = {"kill_all": False, "base_dir": None, "window": None, "target": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--kill-all":
            opts["kill_all"] = True
        elif arg == "--base-dir" and i + 1 < len(argv):
            opts["base_dir"] = argv[i + 1]; i += 1
        elif arg == "--window" and i + 1 < len(argv):
            opts["window"] = argv[i + 1]; i += 1
        elif arg == "--target" and i + 1 < len(argv):
            opts["target"] = argv[i + 1]; i += 1
        i += 1
    return opts


def main(argv):
    opts = _parse_args(argv)
    if opts["kill_all"]:
        if opts["base_dir"]:
            kill_all(opts["base_dir"])
        return 0
    base_dir, window, target = opts["base_dir"], opts["window"], opts["target"]
    if not (base_dir and window and target):
        return 0
    try:
        target_epoch = float(target)
    except ValueError:
        return 0
    directory = usagelimit.usage_state_dir(base_dir)
    pid_path = os.path.join(directory, window + ".pid")
    done_path = os.path.join(directory, window + ".done")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(pid_path, "w") as fh:
            fh.write(str(os.getpid()))
        try:
            os.chmod(pid_path, 0o600)
        except OSError:
            pass
        _wait(target_epoch, time.time, time.sleep, lambda: os.path.exists(done_path))
        if time.time() >= target_epoch:
            fire(base_dir, window, datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    except Exception:
        pass
    finally:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
