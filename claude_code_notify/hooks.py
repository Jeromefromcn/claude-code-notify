import json
import os
import sys
import time
from datetime import datetime

from . import config as cfg
from . import notifier
from . import ratelimit
from .pending_tracker import compute_pending
from .transcript_parser import latest_ai_title


def _now():
    return time.time()


def _when():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


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
        pass  # debug logging must never itself break the hook


def handle_stop(payload, config):
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")
    pending = compute_pending(transcript, str(cfg.state_path(config.base_dir, session_id)))
    _debug(config, f"stop session={session_id} pending={pending}")
    if pending > 0:
        return
    marker = str(cfg.marker_path(config.base_dir, session_id))
    if not ratelimit.should_send(marker, config.ratelimit_seconds, _now()):
        _debug(config, f"stop session={session_id} suppressed by rate-limit")
        return
    title = latest_ai_title(transcript)
    notifier.send(config, notifier.build_message("finished", cwd, _when(), title))
    ratelimit.record_sent(marker, _now())
    _debug(config, f"stop session={session_id} notified")


def handle_stop_failure(payload, config):
    cwd = payload.get("cwd", "")
    title = latest_ai_title(payload.get("transcript_path", ""))
    notifier.send(config, notifier.build_message("error", cwd, _when(), title))
    _debug(config, "stop_failure notified")


def handle_permission_request(payload, config):
    cwd = payload.get("cwd", "")
    title = latest_ai_title(payload.get("transcript_path", ""))
    notifier.send(config, notifier.build_message("needs-input", cwd, _when(), title))
    _debug(config, "permission_request notified")


_HANDLERS = {
    "stop": handle_stop,
    "stop_failure": handle_stop_failure,
    "permission_request": handle_permission_request,
}


def run(event, stdin_text):
    config = None
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        config = cfg.load()
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(payload, config)
    except Exception as exc:  # never propagate — must not break the user's turn
        _debug(config, f"error in {event}: {exc!r}")
    return 0


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    return run(event, stdin_text)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
