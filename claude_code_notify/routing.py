import os
import re
from dataclasses import dataclass
from typing import Optional

_ROUTE_KEY = re.compile(r"^ROUTE_(\d+)_(DIR|CHAT_ID|BOT_TOKEN|MUTE)$")


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _norm(path):
    """Absolute, symlink-resolved path. Never raises."""
    try:
        return os.path.realpath(path)
    except Exception:
        try:
            return os.path.normpath(os.path.abspath(path))
        except Exception:
            return path


@dataclass
class Route:
    dir: str
    chat_id: Optional[str]
    bot_token: Optional[str]
    mute: bool


def parse_routes(merged):
    """Build Route objects from ROUTE_<n>_* keys in a flat dict. Never raises."""
    groups = {}  # int index -> {FIELD: value}
    for key, value in merged.items():
        m = _ROUTE_KEY.match(key)
        if not m:
            continue
        groups.setdefault(int(m.group(1)), {})[m.group(2)] = value

    by_dir = {}  # normalized dir -> Route; later index overwrites (last-wins)
    for idx in sorted(groups):
        fields = groups[idx]
        raw_dir = fields.get("DIR")
        if not raw_dir:
            continue  # no key to match on
        mute = _truthy(fields.get("MUTE", ""))
        chat_id = fields.get("CHAT_ID")
        if not mute and not chat_id:
            continue  # nowhere to send and not muted
        route = Route(
            dir=_norm(raw_dir),
            chat_id=chat_id,
            bot_token=fields.get("BOT_TOKEN"),
            mute=mute,
        )
        by_dir[route.dir] = route
    return list(by_dir.values())
