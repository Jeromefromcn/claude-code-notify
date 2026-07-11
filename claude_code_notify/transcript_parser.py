import json
import re
from dataclasses import dataclass

_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")


@dataclass(frozen=True)
class LaunchEvent:
    tool_use_id: str


@dataclass(frozen=True)
class CompletionEvent:
    tool_use_id: str


def _launch_ids(envelope):
    if envelope.get("type") != "assistant" or envelope.get("isSidechain"):
        return
    message = envelope.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_id = block.get("id")
        if not tool_id:
            continue
        name = block.get("name")
        run_bg = (block.get("input") or {}).get("run_in_background")
        # Agent defaults to background; Bash defaults to foreground.
        if name == "Agent" and run_bg is not False:
            yield tool_id
        elif name == "Bash" and run_bg is True:
            yield tool_id


def _completion_content(envelope):
    """Return the notification content string IFF this envelope is
    structurally a task-notification, else None. Envelope-level only."""
    if envelope.get("type") == "queue-operation":
        content = envelope.get("content")
        return content if isinstance(content, str) else None
    if (envelope.get("origin") or {}).get("kind") == "task-notification":
        content = (envelope.get("message") or {}).get("content")
        return content if isinstance(content, str) else None
    return None


def _completion_ids(envelope):
    content = _completion_content(envelope)
    if content is None or "<task-notification>" not in content:
        return
    for match in _TOOL_USE_ID_RE.finditer(content):
        yield match.group(1)


def _is_turn_start(envelope):
    if envelope.get("type") != "user" or envelope.get("isSidechain"):
        return False
    message = envelope.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return any(
            not (isinstance(block, dict) and block.get("type") == "tool_result")
            for block in content
        )
    return bool(content)


def parse_events(path, offset=0):
    events = []
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return events, offset

    consumed = 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break  # partial trailing line; leave it for the next read
        consumed += len(raw)
        line = raw.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(envelope, dict):
            continue
        for tid in _launch_ids(envelope):
            events.append(LaunchEvent(tid))
        for tid in _completion_ids(envelope):
            events.append(CompletionEvent(tid))
    return events, offset + consumed


def latest_ai_title(path):
    title = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b"ai-title" not in raw:
                    continue
                try:
                    envelope = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(envelope, dict) and envelope.get("type") == "ai-title" and envelope.get("aiTitle"):
                    title = envelope["aiTitle"]
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    return title


def turn_start_timestamp(path):
    result = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    envelope = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                if _is_turn_start(envelope):
                    ts = envelope.get("timestamp")
                    if ts:
                        result = ts
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    return result
