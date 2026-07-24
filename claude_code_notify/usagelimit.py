import collections
import datetime
import hashlib
import json
import os
import re

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 without the tzdata backport
    ZoneInfo = None


# text: the reset-time message; at: epoch of the error envelope's own
# `timestamp` (when the limit was actually hit), or None if the envelope
# carries no parseable timestamp. `at` anchors the reset-window computation
# to when the limit occurred rather than to when we happen to read the
# transcript, so a stale re-read of an old limit can't be mistaken for a
# fresh hit (docs/lessons-learned/0005).
UsageLimit = collections.namedtuple("UsageLimit", ["text", "at"])


def _parse_ts(ts):
    """Epoch from an ISO-8601 transcript timestamp (e.g. the trailing 'Z'
    UTC form Claude Code writes). None on anything unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


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


def _error_body(error_details):
    """Best-effort JSON body from a raw API error-detail value: a dict as-is,
    or a string like '429 {"type":"error",...}' (Claude Code's on-disk and
    payload representations both use this "<status> <json>" form). Returns {}
    on anything unparseable — this is advisory data only, never load-bearing
    for the rate_limit/isApiErrorMessage classification itself."""
    if isinstance(error_details, dict):
        return error_details
    if not isinstance(error_details, str):
        return {}
    brace = error_details.find("{")
    if brace == -1:
        return {}
    try:
        return json.loads(error_details[brace:])
    except ValueError:
        return {}


def is_model_credits_error(error_details):
    """True when the structured error body identifies a per-model usage-
    credits gate (e.g. a non-subscription model like Fable 5 without credits
    enabled) rather than an account-level session/weekly usage limit. Both
    the transcript envelope's `errorDetails` and StopFailure payload's
    `error_details` carry the same raw API error body; genuine account usage
    limits never populate this field."""
    details = _error_body(error_details).get("error", {})
    details = details.get("details", {}) if isinstance(details, dict) else {}
    return isinstance(details, dict) and details.get("error_code") == "credits_required"


def latest_usage_limit(path):
    """Return a UsageLimit(text, at) iff the transcript's last assistant
    (non-sidechain) envelope is a rate_limit API error and not a per-model
    credits gate (see is_model_credits_error), else None. `at` is the epoch of
    that envelope's own `timestamp` (when the limit was hit), or None if it
    carries no parseable timestamp. Envelope-level only."""
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
            last_assistant.get("error") == "rate_limit" and \
            not is_model_credits_error(last_assistant.get("errorDetails")):
        text = _message_text(last_assistant).strip()
        if not text:
            return None
        return UsageLimit(text, _parse_ts(last_assistant.get("timestamp")))
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


def _reset_hm(reset_text):
    """Parse the reported reset time into (hour_24, minute, tzinfo|None), or
    None when the text carries no recognizable h[:mm]am/pm token. The tz is
    the zone named in the text (e.g. "(Asia/Hong_Kong)") when present and
    resolvable, else None (host local time)."""
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
    return hour, minute, _resolve_tz(match.group(4))


def reset_epoch(reset_text, anchor):
    """Epoch of the first reported reset time strictly after `anchor` — the
    moment the limit was hit — computed in the timezone named in the text.
    This is the window's stable identity: it depends only on *when the limit
    occurred*, never on when we happen to read it, so a stale re-read of the
    same limit maps to the same window. Returns None on unparseable text
    (e.g. weekly-limit format) or an out-of-range date computation."""
    hm = _reset_hm(reset_text)
    if hm is None:
        return None
    hour, minute, tz = hm
    try:
        base = datetime.datetime.fromtimestamp(anchor, tz)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        epoch = target.timestamp()
        if epoch <= anchor:
            epoch = (target + datetime.timedelta(days=1)).timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return epoch


def parse_reset(reset_text, now, anchor=None):
    """Best-effort *schedulable* reset epoch: the next reset occurrence after
    the limit was hit (`anchor`, defaulting to `now`), but only if it is still
    in the future relative to `now` and within CAP_SECONDS. Anchoring the
    roll-forward to when the limit was hit — not to read time — means a stale
    limit whose reset has already passed returns None instead of being rolled
    forward to a spurious next-day occurrence (docs/lessons-learned/0005).
    Returns None on unparseable text or an out-of-range result."""
    epoch = reset_epoch(reset_text, now if anchor is None else anchor)
    if epoch is None:
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
