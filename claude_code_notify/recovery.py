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


def _debug_config(base_dir):
    """Best-effort config load for debug logging only. This process only ever
    receives base_dir on argv, not a loaded Config, so each entry point loads
    its own — a failure here (e.g. no config.env) must never affect control
    flow, so it silently yields None, which _debug() already no-ops on."""
    try:
        return cfg.load(base=base_dir)
    except Exception:
        return None


def _debug(config, line):
    if config is None or not config.debug:
        return
    try:
        path = str(cfg.debug_log_path(config.base_dir))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        scrubbed = notifier.scrub(line, config.bot_token)
        with open(path, "a") as fh:
            fh.write(f"{datetime.now().isoformat()} {scrubbed}\n")
        os.chmod(path, 0o600)
    except Exception:
        pass  # debug logging must never itself break the sleeper


def spawn(base_dir, window, target_epoch):
    """Launch one detached sleeper for this window. Single-instance via an
    atomic claim. No secrets on argv. Never raises."""
    config = _debug_config(base_dir)
    if not usagelimit.claim(base_dir, window + ".sleeper"):
        _debug(config, f"recovery: sleeper already running for window={window} — spawn skipped")
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
        _debug(config, f"recovery: sleeper spawned window={window} target={int(target_epoch)}")
    except Exception:
        _debug(config, f"recovery: spawn failed window={window}")


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
    count = broadcast.send_all(config, message, send=send)
    _debug(config, f"recovery: reset broadcast to {count} destination(s) window={window}")
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
    config = _debug_config(base_dir)
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
        _debug(config, f"recovery: sleeper started window={window} target={int(target_epoch)} pid={os.getpid()}")
        _wait(target_epoch, time.time, time.sleep, lambda: os.path.exists(done_path))
        if time.time() >= target_epoch:
            _debug(config, f"recovery: target reached window={window} — firing")
            fire(base_dir, window, datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
        else:
            reason = "preempted by .done" if os.path.exists(done_path) else "cap exceeded"
            _debug(config, f"recovery: sleeper exiting without firing window={window} reason={reason}")
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
