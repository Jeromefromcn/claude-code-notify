import os
import signal
import subprocess
import sys

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
