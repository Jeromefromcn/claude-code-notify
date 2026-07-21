import hashlib
import json


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


def window_key(reset_text):
    """Opaque, filesystem-safe dedup key for one reset window."""
    return hashlib.sha1((reset_text or "").strip().encode("utf-8")).hexdigest()[:16]
