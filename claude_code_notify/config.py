import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class Config:
    bot_token: str
    chat_id: str
    ratelimit_seconds: int
    api_base: str
    debug: bool
    base_dir: Path


def default_base_dir(environ=None):
    environ = os.environ if environ is None else environ
    override = environ.get("CLAUDE_NOTIFY_HOME")
    if override:
        return Path(override)
    return Path(environ.get("HOME", str(Path.home()))) / ".claude" / "claude-code-notify"


def config_path(base):
    return Path(base) / "config.env"


def state_dir(base):
    return Path(base) / "state"


def state_path(base, session_id):
    return state_dir(base) / f"{session_id}.state.json"


def marker_path(base, session_id):
    return state_dir(base) / f"{session_id}.marker"


def debug_log_path(base):
    return Path(base) / "debug.log"


def parse_env_file(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            # Support `export KEY=value` for users who hand-edit config.env.
            # This is a plain line parser, not a shell — only a single
            # `export KEY=value` per line is supported (not e.g. bash's
            # `export A=1 B=2`, variable expansion, or command substitution).
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load(environ=None, base=None):
    environ = os.environ if environ is None else environ
    base = default_base_dir(environ) if base is None else Path(base)

    merged = {}
    cfg_file = config_path(base)
    if cfg_file.exists():
        merged.update(parse_env_file(cfg_file.read_text()))
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE",
                "NOTIFY_RATELIMIT_SECONDS", "NOTIFY_DEBUG"):
        if key in environ:
            merged[key] = environ[key]

    token = merged.get("TELEGRAM_BOT_TOKEN")
    chat_id = merged.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    try:
        ratelimit_seconds = int(merged.get("NOTIFY_RATELIMIT_SECONDS", "120"))
    except ValueError:
        ratelimit_seconds = 120

    return Config(
        bot_token=token,
        chat_id=chat_id,
        ratelimit_seconds=ratelimit_seconds,
        api_base=merged.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        debug=_truthy(merged.get("NOTIFY_DEBUG", "false")),
        base_dir=base,
    )
