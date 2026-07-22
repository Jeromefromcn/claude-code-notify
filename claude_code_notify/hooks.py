import dataclasses
import json
import os
import sys
import time
from datetime import datetime

from . import broadcast
from . import config as cfg
from . import notifier
from . import ratelimit
from . import recovery
from . import routing
from . import usagelimit
from .pending_tracker import compute_pending
from .transcript_parser import latest_ai_title, turn_start_timestamp


def _now():
    return time.time()


def _when():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _format_duration(seconds):
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _turn_duration(transcript, now):
    start_str = turn_start_timestamp(transcript)
    if not start_str:
        return None
    start = _parse_ts(start_str)
    if start is None:
        return None
    return _format_duration(now - start)


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


def _sleep(seconds):
    time.sleep(seconds)


# StopFailure can fire before Claude Code finishes flushing the terminal
# transcript envelope to disk (observed gap: ~20ms in production). Retrying
# only here — not from handle_stop — bridges that race without adding
# latency to the far more common normal-completion path.
_STOP_FAILURE_RETRY_DELAYS = (0.2,)


def _maybe_handle_usage_limit(payload, config, retry_delays=()):
    """If this turn ended in a usage limit, broadcast to all destinations
    (once per window), optionally schedule the reset ping, and return True so
    the caller skips its normal notification. Never raises out."""
    if not config.usage_limit:
        _debug(config, "usage-limit: feature disabled — skipping detection")
        return False
    transcript = payload.get("transcript_path", "")
    reset_text = usagelimit.latest_usage_limit(transcript)
    retries_used = 0
    for delay in retry_delays:
        if reset_text is not None:
            break
        _sleep(delay)
        retries_used += 1
        reset_text = usagelimit.latest_usage_limit(transcript)
    if reset_text is None:
        _debug(config, f"usage-limit: no rate-limit as last transcript entry "
                        f"(transcript={transcript}, retries={retries_used})")
        return False
    if retries_used:
        _debug(config, f"usage-limit: detected after {retries_used} retry(ies) (transcript={transcript})")
    cwd = payload.get("cwd", "")
    now = _now()
    target = usagelimit.parse_reset(reset_text, now)
    key = usagelimit.window_key(reset_text, target)
    usagelimit.gc(config.base_dir, now)
    if usagelimit.claim_hit(config.base_dir, key):
        message = notifier.build_message("usage-limit", cwd, _when(), title=reset_text)
        count = broadcast.send_all(config, message)
        _debug(config, f"usage-limit hit broadcast to {count} destination(s)")
        if config.usage_limit_reset:
            if target is not None:
                recovery.spawn(config.base_dir, key, target)
                _debug(config, f"usage-limit reset scheduled at {int(target)}")
            else:
                _debug(config, "usage-limit reset time unparsed — no reset ping")
    else:
        _debug(config, f"usage-limit: hit detected (key={key}) but window already claimed — suppressing duplicate")
    return True


def handle_stop(payload, config):
    if _maybe_handle_usage_limit(payload, config):
        return
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"stop cwd={cwd} muted — no send")
        return
    pending = compute_pending(transcript, str(cfg.state_path(config.base_dir, session_id)))
    _debug(config, f"stop session={session_id} pending={pending}")
    if pending > 0:
        return
    marker = str(cfg.marker_path(config.base_dir, session_id))
    if not ratelimit.should_send(marker, config.ratelimit_seconds, _now()):
        _debug(config, f"stop session={session_id} suppressed by rate-limit")
        return
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("finished", cwd, _when(), title, duration))
    ratelimit.record_sent(marker, _now())
    _debug(config, f"stop session={session_id} notified chat={res.chat_id}")


def handle_stop_failure(payload, config):
    if _maybe_handle_usage_limit(payload, config, retry_delays=_STOP_FAILURE_RETRY_DELAYS):
        return
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"stop_failure cwd={cwd} muted — no send")
        return
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("error", cwd, _when(), title, duration))
    _debug(config, f"stop_failure notified chat={res.chat_id}")


def handle_permission_request(payload, config):
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"permission_request cwd={cwd} muted — no send")
        return
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("needs-input", cwd, _when(), title, duration))
    _debug(config, f"permission_request notified chat={res.chat_id}")


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
    try:
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    except Exception:
        stdin_text = ""  # never let a stdin read failure escape uncaught
    return run(event, stdin_text)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
