import datetime
import hashlib
import json
import os
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 without the tzdata backport
    ZoneInfo = None


def _message_text(envelope):
    message = envelope.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return ""


def latest_usage_limit(path):
    """Return the reset text iff the transcript's last assistant (non-sidechain)
    envelope is a rate_limit API error, else None. Envelope-level only."""
    last_assistant = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    envelope = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                if envelope.get("type") == "assistant" and not envelope.get("isSidechain"):
                    last_assistant = envelope
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    if last_assistant is None:
        return None
    if last_assistant.get("isApiErrorMessage") is True and \
            last_assistant.get("error") == "rate_limit":
        text = _message_text(last_assistant).strip()
        return text or None
    return None


def window_key(reset_text, target_epoch=None):
    """Opaque, filesystem-safe dedup key for one reset window.

    Reset text alone is not date-specific (e.g. "resets 9pm" repeats every
    day), so two unrelated limit events on different days can render
    identical text. When a parsed target epoch is available, its date is
    folded into the key so distinct occurrences never collide; falls back
    to text-only when the reset time couldn't be parsed (e.g. weekly-limit
    text), which still avoids duplicate work within one unparseable window.
    """
    text = (reset_text or "").strip()
    if target_epoch is not None:
        date_part = datetime.date.fromtimestamp(target_epoch).isoformat()
        text = f"{text}|{date_part}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


CAP_SECONDS = 8 * 24 * 3600


_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?", re.IGNORECASE)


def _resolve_tz(tz_name):
    """Best-effort IANA zone lookup for the tz name Claude Code embeds in the
    reset text (e.g. "Asia/Hong_Kong"). None (host local time) if the name is
    absent, unrecognized, or zoneinfo isn't available — identical to prior
    behavior, so this is a pure enhancement, never a regression."""
    if not tz_name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name.strip())
    except Exception:
        return None


def parse_reset(reset_text, now):
    """Best-effort next-occurrence epoch of the reported wall-clock reset
    time, computed in the timezone named in the text (e.g. "(Asia/Hong_Kong)")
    when present and resolvable — not the host machine's timezone, which may
    differ. Returns None on any unparseable text or out-of-range result. Only
    the known session format (h[:mm]am/pm) is handled; weekly formats return
    None."""
    match = _RESET_RE.search(reset_text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None
    hour = hour % 12
    if match.group(3).lower() == "pm":
        hour += 12
    tz = _resolve_tz(match.group(4))
    try:
        base = datetime.datetime.fromtimestamp(now, tz)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        epoch = target.timestamp()
        if epoch <= now:
            epoch = (target + datetime.timedelta(days=1)).timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if epoch <= now or epoch > now + CAP_SECONDS:
        return None
    return epoch


def usage_state_dir(base_dir):
    """Return the directory path for state files."""
    return os.path.join(str(base_dir), "state", "usage_limit")


def claim(base_dir, filename):
    """Atomically create a marker; True only for the creating caller. Never raises."""
    directory = usage_state_dir(base_dir)
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, filename),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def claim_hit(base_dir, key):
    """Atomically claim a hit marker for a given key. True only for the creating caller."""
    return claim(base_dir, key + ".hit")


def gc(base_dir, now, max_age_seconds=30 * 24 * 3600):
    """Best-effort removal of stale window markers. Never raises."""
    directory = usage_state_dir(base_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
        except OSError:
            pass
