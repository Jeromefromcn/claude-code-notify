import json
import os
from dataclasses import dataclass, field

from .transcript_parser import parse_events, LaunchEvent, CompletionEvent


@dataclass
class State:
    offset: int = 0
    launched: set = field(default_factory=set)
    resolved: set = field(default_factory=set)


def load_state(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return State(
            int(data.get("offset", 0)),
            set(data.get("launched", [])),
            set(data.get("resolved", [])),
        )
    except (FileNotFoundError, ValueError, TypeError, OSError):
        return State()


def save_state(path, state):
    payload = {
        "offset": state.offset,
        "launched": sorted(state.launched),
        "resolved": sorted(state.resolved),
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def compute_pending(transcript_path, state_path):
    state = load_state(state_path)
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        size = 0
    if size < state.offset:
        state = State()  # rotated/truncated → full rescan from offset 0

    events, new_offset = parse_events(transcript_path, state.offset)
    for event in events:
        if isinstance(event, LaunchEvent):
            state.launched.add(event.tool_use_id)
        elif isinstance(event, CompletionEvent):
            state.resolved.add(event.tool_use_id)
    state.offset = new_offset
    save_state(state_path, state)
    return len(state.launched - state.resolved)
