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


@dataclass
class Resolution:
    muted: bool
    bot_token: Optional[str]
    chat_id: Optional[str]
    matched_dir: Optional[str]


def _matches(cwd, route_dir):
    if cwd == route_dir:
        return True
    if route_dir == os.sep:  # a route at the filesystem root matches everything
        return True
    return cwd.startswith(route_dir + os.sep)


def resolve(cwd, routes, default_bot_token, default_chat_id):
    """Resolve cwd to a Resolution via longest-prefix match. Never raises."""
    default = Resolution(muted=False, bot_token=default_bot_token,
                         chat_id=default_chat_id, matched_dir=None)
    if not cwd:
        return default
    try:
        norm_cwd = _norm(cwd)
        best = None
        for route in routes:
            if _matches(norm_cwd, route.dir) and (
                best is None or len(route.dir) > len(best.dir)
            ):
                best = route
        if best is None:
            return default
        if best.mute:
            return Resolution(muted=True, bot_token=None, chat_id=None,
                              matched_dir=best.dir)
        return Resolution(muted=False,
                          bot_token=best.bot_token or default_bot_token,
                          chat_id=best.chat_id, matched_dir=best.dir)
    except Exception:
        return default  # never break the hook
