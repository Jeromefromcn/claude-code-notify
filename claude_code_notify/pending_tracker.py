import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from .transcript_parser import parse_events, LaunchEvent, CompletionEvent


@dataclass
class State:
    offset: int = 0
    launched: dict = field(default_factory=dict)  # tool_use_id -> ISO8601 launch timestamp, or None
    resolved: set = field(default_factory=set)
    finished_sent: bool = False  # a Stop "finished"/"waiting" ping already covered this idle point


def load_state(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        launched = data.get("launched", {})
        if isinstance(launched, list):
            # Pre-staleness state files stored "launched" as a bare list of
            # ids with no timestamp. Migrate to {id: None} — compute_pending()
            # treats an unknown launch time as immediately stale once
            # staleness is enabled, so a session already stuck forever
            # (docs/lessons-learned/0007) self-heals on its next Stop event
            # instead of needing manual state-file surgery.
            launched = {tool_use_id: None for tool_use_id in launched}
        return State(
            int(data.get("offset", 0)),
            dict(launched),
            set(data.get("resolved", [])),
            bool(data.get("finished_sent", False)),
        )
    except Exception:
        # Best-effort loader: any missing/corrupt/wrong-shaped state file
        # (FileNotFoundError, JSON decode errors, non-dict JSON causing
        # AttributeError on .get(), etc.) must fall back to a fresh State()
        # rather than raise, so a bad state file only forces a full rescan
        # instead of silently skipping notification.
        return State()


def save_state(path, state):
    payload = {
        "offset": state.offset,
        "launched": dict(sorted(state.launched.items())),
        "resolved": sorted(state.resolved),
        "finished_sent": state.finished_sent,
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def compute_pending(transcript_path, state_path, stale_seconds=None, now=None, on_stale=None, on_resolved=None):
    state = load_state(state_path)
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        size = 0
    if size < state.offset:
        state = State()  # rotated/truncated → full rescan from offset 0

    resolved_before = set(state.resolved)
    events, new_offset = parse_events(transcript_path, state.offset)
    for event in events:
        if isinstance(event, LaunchEvent):
            state.launched[event.tool_use_id] = event.timestamp
        elif isinstance(event, CompletionEvent):
            state.resolved.add(event.tool_use_id)
    state.offset = new_offset

    # Report launches that were resolved by a <task-notification> in THIS pass
    # (and not merely already-resolved before it). This is what lets the Stop
    # handler distinguish "the last background task finished" from "a plain
    # turn ended" — the former announces finished, the latter stays silent.
    if on_resolved is not None:
        resolved_now = [
            event.tool_use_id for event in events
            if isinstance(event, CompletionEvent)
            and event.tool_use_id in state.launched
            and event.tool_use_id not in resolved_before
        ]
        if resolved_now:
            on_resolved(resolved_now)

    if stale_seconds is not None:
        cutoff = (now if now is not None else time.time()) - stale_seconds
        stale_ids = [
            tool_use_id for tool_use_id, ts in state.launched.items()
            if tool_use_id not in state.resolved
            and (_parse_iso(ts) is None or _parse_iso(ts) < cutoff)
        ]
        for tool_use_id in stale_ids:
            del state.launched[tool_use_id]
        if stale_ids and on_stale is not None:
            on_stale(stale_ids)

    save_state(state_path, state)
    return len(state.launched.keys() - state.resolved)


def finished_sent(state_path):
    return load_state(state_path).finished_sent


def mark_finished_sent(state_path):
    state = load_state(state_path)
    state.finished_sent = True
    save_state(state_path, state)
