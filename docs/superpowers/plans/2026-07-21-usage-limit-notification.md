# Usage-Limit Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the Claude account hits a usage limit, broadcast a Telegram alert to every distinct configured destination, and — opt-in — schedule a one-shot "reset" ping at the reported reset time. Off by default.

**Architecture:** Detection piggybacks on the existing `Stop`/`StopFailure` hooks using a purely structural transcript signal (`error == "rate_limit"`). A broadcast helper fans a message out to every distinct `(bot_token, chat_id)`. The reset ping is a detached, bounded, single-instance background process (a "sleeper") spawned from the hook; there is no fallback if it is killed.

**Tech Stack:** Python 3.8+ stdlib only (`urllib`, `subprocess`, `hashlib`, `re`, `datetime`, `os`, `signal`), pytest. Bash shim for uninstall wiring. Telegram Bot API.

Full spec: [docs/superpowers/specs/2026-07-21-usage-limit-notification-design.md](../specs/2026-07-21-usage-limit-notification-design.md).

## Global Constraints

- **Runtime floor `requires-python = ">=3.8"`.** No PEP 585/604 runtime annotations (`list[...]`, `X | None`); use `typing.Optional`/`typing.List` or bare annotations. `zoneinfo` is 3.9+ — never a hard dependency.
- **Zero third-party runtime dependencies.** `python3` is the only runtime dep.
- **`hooks.py` never raises or exits non-zero.** Every new call it makes is guarded; failures no-op (and `_debug`-log when `NOTIFY_DEBUG` is on).
- **Detection is envelope-level only** (`type`, `isApiErrorMessage`, `error`). Never substring-match transcript text to decide *what happened*. Text is used only as display content and as an opaque dedup key.
- **Secrets are scrubbed** from all error/log output and **never passed on a command line** (the sleeper re-loads config itself).
- **Core is testable without a live Claude Code session and without real Telegram** (`send` injectable; sleeper timing uses injected clocks — no real sleeping in tests, no real background process spawned in tests).
- **All committed text (code, docs, commits) is English.**
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

| File | New/Modify | Responsibility |
|---|---|---|
| `claude_code_notify/config.py` | Modify | Load `NOTIFY_USAGE_LIMIT` / `NOTIFY_USAGE_LIMIT_RESET` into `Config`. |
| `claude_code_notify/notifier.py` | Modify | Two new message heads; omit an empty `cwd` segment. |
| `claude_code_notify/usagelimit.py` | Create | Envelope-level detection, window key, reset-time parse, global state claims + GC. |
| `claude_code_notify/broadcast.py` | Create | Distinct-destination fan-out. |
| `claude_code_notify/recovery.py` | Create | Sleeper: spawn, wait loop, fire, kill-all; module entrypoint. |
| `claude_code_notify/hooks.py` | Modify | Wire usage-limit handling ahead of the normal turn-end path. |
| `install.sh` | Modify | Kill live sleepers on uninstall (before removing state). |
| `claude_code_notify/__init__.py`, `pyproject.toml` | Modify | Version bump to 0.4.0. |
| Docs (`docs/…`, `README.md`, `CHANGELOG.md`, `CLAUDE.md`) | Modify | Document the feature. |
| `tests/test_usagelimit.py`, `tests/test_broadcast.py`, `tests/test_recovery.py` | Create | Unit tests for new modules. |
| `tests/test_config.py`, `tests/test_notifier.py`, `tests/test_hooks.py`, `tests/test_install_e2e.py` | Modify | Extend existing suites. |

---

## Task 1: Config switches

**Files:**
- Modify: `claude_code_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.usage_limit: bool` (default `False`), `Config.usage_limit_reset: bool` (default `True`); `cfg.load()` reads `NOTIFY_USAGE_LIMIT` / `NOTIFY_USAGE_LIMIT_RESET` from file and env.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
def test_load_usage_limit_defaults(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.usage_limit is False
    assert c.usage_limit_reset is True


def test_load_usage_limit_from_file(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "NOTIFY_USAGE_LIMIT=true\nNOTIFY_USAGE_LIMIT_RESET=false\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.usage_limit is True
    assert c.usage_limit_reset is False


def test_env_overrides_usage_limit(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={"NOTIFY_USAGE_LIMIT": "1"}, base=tmp_path)
    assert c.usage_limit is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_config.py -k usage_limit -v`
Expected: FAIL (`AttributeError: 'Config' object has no attribute 'usage_limit'`).

- [ ] **Step 3: Implement**

In `claude_code_notify/config.py`, add two fields to the `Config` dataclass, after `routes`:

```python
    routes: list = field(default_factory=list)
    usage_limit: bool = False
    usage_limit_reset: bool = True
```

In `load()`, add the two keys to the env-override loop:

```python
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE",
                "NOTIFY_RATELIMIT_SECONDS", "NOTIFY_DEBUG",
                "NOTIFY_USAGE_LIMIT", "NOTIFY_USAGE_LIMIT_RESET"):
        if key in environ:
            merged[key] = environ[key]
```

And pass them to the `Config(...)` constructor (after `routes=...`):

```python
        routes=routing.parse_routes(merged),
        usage_limit=_truthy(merged.get("NOTIFY_USAGE_LIMIT", "false")),
        usage_limit_reset=_truthy(merged.get("NOTIFY_USAGE_LIMIT_RESET", "true")),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: PASS (all config tests).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/config.py tests/test_config.py
git commit -m "feat: load NOTIFY_USAGE_LIMIT / NOTIFY_USAGE_LIMIT_RESET into Config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Notifier message heads

**Files:**
- Modify: `claude_code_notify/notifier.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Produces: `build_message("usage-limit", cwd, when, title=...)` and `build_message("usage-limit-reset", cwd, when)`; `build_message` omits the `cwd` segment when `cwd` is falsy.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_notifier.py`:

```python
def test_build_message_usage_limit():
    assert notifier.build_message(
        "usage-limit", "/w", "WHEN",
        title="You've hit your session limit · resets 9pm") == (
        "Claude Code usage limit reached | "
        "You've hit your session limit · resets 9pm | /w | WHEN")


def test_build_message_usage_limit_reset_omits_empty_cwd():
    assert notifier.build_message("usage-limit-reset", "", "WHEN") == (
        "Claude Code usage limit reset | WHEN")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_notifier.py -k usage -v`
Expected: FAIL (`KeyError: 'usage-limit'`).

- [ ] **Step 3: Implement**

In `claude_code_notify/notifier.py`, extend `_HEADS`:

```python
_HEADS = {
    "finished": "Claude Code finished",
    "error": "Claude Code stopped with error",
    "needs-input": "Claude Code needs your input",
    "usage-limit": "Claude Code usage limit reached",
    "usage-limit-reset": "Claude Code usage limit reset",
}
```

In `build_message`, guard the `cwd` append (only change is `if cwd:`):

```python
def build_message(kind, cwd, when, title=None, duration=None):
    parts = [_HEADS[kind]]
    if duration:
        parts.append(duration)
    if title:
        parts.append(title)
    if cwd:
        parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_notifier.py -v`
Expected: PASS (existing tests still pass — they all pass a non-empty `cwd`).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/notifier.py tests/test_notifier.py
git commit -m "feat: add usage-limit message heads; omit empty cwd segment

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Usage-limit detection + window key

**Files:**
- Create: `claude_code_notify/usagelimit.py`
- Test: `tests/test_usagelimit.py`

**Interfaces:**
- Produces: `latest_usage_limit(path) -> Optional[str]` (reset text iff the transcript's last assistant envelope is a `rate_limit`, else `None`); `window_key(reset_text) -> str` (stable 16-hex key).

- [ ] **Step 1: Write the failing tests** — create `tests/test_usagelimit.py`:

```python
import json

from claude_code_notify import usagelimit


def _write(tmp_path, envelopes):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in envelopes))
    return str(path)


def _rate_limit(text="You've hit your session limit · resets 9pm (Asia/Hong_Kong)"):
    return {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
            "error": "rate_limit", "apiErrorStatus": 429,
            "message": {"model": "<synthetic>",
                        "content": [{"type": "text", "text": text}]}}


def test_detects_rate_limit_as_last_assistant(tmp_path):
    path = _write(tmp_path, [_rate_limit()])
    assert usagelimit.latest_usage_limit(path) == \
        "You've hit your session limit · resets 9pm (Asia/Hong_Kong)"


def test_ignores_trailing_non_assistant_lines(tmp_path):
    path = _write(tmp_path, [
        _rate_limit(),
        {"type": "queue-operation", "content": "x"},
        {"type": "user", "isSidechain": False, "message": {"content": "hi"}},
    ])
    assert usagelimit.latest_usage_limit(path) is not None


def test_stale_rate_limit_before_normal_turn_is_ignored(tmp_path):
    path = _write(tmp_path, [
        _rate_limit(),
        {"type": "assistant", "isSidechain": False,
         "message": {"content": [{"type": "text", "text": "done"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_auth_error_is_not_a_usage_limit(tmp_path):
    path = _write(tmp_path, [
        {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
         "error": "authentication_failed",
         "message": {"content": [{"type": "text", "text": "OAuth expired"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_normal_finish_is_not_a_usage_limit(tmp_path):
    path = _write(tmp_path, [
        {"type": "assistant", "isSidechain": False,
         "message": {"content": [{"type": "text", "text": "all done"}]}},
    ])
    assert usagelimit.latest_usage_limit(path) is None


def test_missing_file_returns_none(tmp_path):
    assert usagelimit.latest_usage_limit(str(tmp_path / "nope.jsonl")) is None


def test_window_key_stable_and_distinct():
    a = usagelimit.window_key("resets 9pm (Asia/Hong_Kong)")
    b = usagelimit.window_key("resets 9pm (Asia/Hong_Kong)")
    c = usagelimit.window_key("resets 10pm (Asia/Hong_Kong)")
    assert a == b and a != c
    assert len(a) == 16
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_usagelimit.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'claude_code_notify.usagelimit'`).

- [ ] **Step 3: Implement** — create `claude_code_notify/usagelimit.py`:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_usagelimit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/usagelimit.py tests/test_usagelimit.py
git commit -m "feat: detect usage limit from transcript at envelope level

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Global state claims + GC

**Files:**
- Modify: `claude_code_notify/usagelimit.py`
- Test: `tests/test_usagelimit.py`

**Interfaces:**
- Produces: `CAP_SECONDS` (int, 8 days); `usage_state_dir(base_dir) -> str`; `claim(base_dir, filename) -> bool` (atomic O_EXCL create, `True` on the creating call); `claim_hit(base_dir, key) -> bool`; `gc(base_dir, now, max_age_seconds=...) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_usagelimit.py`:

```python
import os


def test_claim_is_single_winner(tmp_path):
    assert usagelimit.claim_hit(str(tmp_path), "w1") is True
    assert usagelimit.claim_hit(str(tmp_path), "w1") is False
    marker = os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.hit")
    assert os.path.exists(marker)


def test_claim_generic_names(tmp_path):
    assert usagelimit.claim(str(tmp_path), "w1.sleeper") is True
    assert usagelimit.claim(str(tmp_path), "w1.sleeper") is False


def test_gc_removes_old_files_keeps_fresh(tmp_path):
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    old = os.path.join(d, "old.hit")
    fresh = os.path.join(d, "fresh.hit")
    open(old, "w").close()
    open(fresh, "w").close()
    now = 1_000_000_000.0
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    os.utime(fresh, (now - 1 * 86400, now - 1 * 86400))
    usagelimit.gc(str(tmp_path), now)
    assert not os.path.exists(old)
    assert os.path.exists(fresh)


def test_cap_is_at_least_one_week():
    assert usagelimit.CAP_SECONDS >= 7 * 24 * 3600
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_usagelimit.py -k "claim or gc or cap" -v`
Expected: FAIL (`AttributeError: module ... has no attribute 'claim_hit'`).

- [ ] **Step 3: Implement** — add to `claude_code_notify/usagelimit.py` (add `import os` at the top with the others):

```python
import os

CAP_SECONDS = 8 * 24 * 3600


def usage_state_dir(base_dir):
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_usagelimit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/usagelimit.py tests/test_usagelimit.py
git commit -m "feat: add global usage-limit state claims and GC

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Reset-time parsing (best-effort)

**Files:**
- Modify: `claude_code_notify/usagelimit.py`
- Test: `tests/test_usagelimit.py`

**Interfaces:**
- Produces: `parse_reset(reset_text, now) -> Optional[float]` — next-occurrence epoch of the reported local wall-clock time, or `None` when unparseable / out of `[now, now+CAP_SECONDS]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_usagelimit.py`:

```python
import datetime


def test_parse_reset_returns_next_local_occurrence():
    now = datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()  # 10:00 local
    got = usagelimit.parse_reset("resets 9pm (Asia/Hong_Kong)", now)
    assert got is not None
    dt = datetime.datetime.fromtimestamp(got)
    assert (dt.hour, dt.minute) == (21, 0)
    assert now < got <= now + 24 * 3600


def test_parse_reset_rolls_to_tomorrow_when_past():
    now = datetime.datetime(2026, 7, 21, 23, 30, 0).timestamp()  # 23:30 local
    got = usagelimit.parse_reset("resets 9pm (Asia/Hong_Kong)", now)
    assert got is not None
    dt = datetime.datetime.fromtimestamp(got)
    assert (dt.hour, dt.minute) == (21, 0)
    assert dt.date() == datetime.date(2026, 7, 22)  # next day


def test_parse_reset_handles_minutes_and_am():
    now = datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()
    got = usagelimit.parse_reset("resets 7:50am", now)
    dt = datetime.datetime.fromtimestamp(got)
    assert (dt.hour, dt.minute) == (7, 50)


def test_parse_reset_weekly_style_text_is_unparsed():
    now = datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()
    assert usagelimit.parse_reset("You've hit your weekly limit · resets Monday", now) is None


def test_parse_reset_invalid_hour_is_none():
    now = datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()
    assert usagelimit.parse_reset("resets 13pm", now) is None


def test_parse_reset_no_match_is_none():
    now = datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()
    assert usagelimit.parse_reset("nothing useful here", now) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_usagelimit.py -k parse_reset -v`
Expected: FAIL (`AttributeError: ... 'parse_reset'`).

- [ ] **Step 3: Implement** — add to `claude_code_notify/usagelimit.py` (add `import datetime` and `import re` at the top):

```python
import datetime
import re

_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


def parse_reset(reset_text, now):
    """Best-effort next-occurrence epoch of the reported local wall-clock reset
    time. Returns None on any unparseable text or out-of-range result. Only the
    known session format (h[:mm]am/pm) is handled; weekly formats return None."""
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
    try:
        base = datetime.datetime.fromtimestamp(now)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        epoch = target.timestamp()
        if epoch <= now:
            epoch = (target + datetime.timedelta(days=1)).timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if epoch <= now or epoch > now + CAP_SECONDS:
        return None
    return epoch
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_usagelimit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/usagelimit.py tests/test_usagelimit.py
git commit -m "feat: best-effort parse of usage-limit reset time (local wall clock)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Broadcast fan-out

**Files:**
- Create: `claude_code_notify/broadcast.py`
- Test: `tests/test_broadcast.py`

**Interfaces:**
- Consumes: `Config` (`.bot_token`, `.chat_id`, `.routes`), `notifier.send`.
- Produces: `destinations(config) -> List[Tuple[str, str]]` (distinct `(bot_token, chat_id)`); `send_all(config, text, send=None) -> int` (count sent; per-destination guarded).

- [ ] **Step 1: Write the failing tests** — create `tests/test_broadcast.py`:

```python
from pathlib import Path

from claude_code_notify import broadcast
from claude_code_notify.config import Config
from claude_code_notify.routing import Route


def _cfg(routes):
    return Config(bot_token="G:tok", chat_id="999", ratelimit_seconds=120,
                  api_base="http://127.0.0.1:1", debug=False, base_dir=Path("/tmp"),
                  routes=routes)


def test_destinations_global_only():
    assert broadcast.destinations(_cfg([])) == [("G:tok", "999")]


def test_destinations_include_routes_and_dedupe():
    routes = [
        Route(dir="/a", chat_id="111", bot_token=None, mute=False),      # uses global bot
        Route(dir="/b", chat_id="222", bot_token="B:tok", mute=False),   # own bot
        Route(dir="/c", chat_id="999", bot_token="G:tok", mute=False),   # dup of global
    ]
    got = broadcast.destinations(_cfg(routes))
    assert got == [("G:tok", "999"), ("G:tok", "111"), ("B:tok", "222")]


def test_destinations_skip_route_without_chat():
    routes = [Route(dir="/m", chat_id=None, bot_token=None, mute=True)]  # muted, no chat
    assert broadcast.destinations(_cfg(routes)) == [("G:tok", "999")]


def test_destinations_muted_route_with_chat_still_included():
    # Mute is not consulted here; a muted route that carries a chat_id is a
    # configured destination and receives the account-global broadcast.
    routes = [Route(dir="/m", chat_id="333", bot_token=None, mute=True)]
    assert broadcast.destinations(_cfg(routes)) == [("G:tok", "999"), ("G:tok", "333")]


def test_send_all_hits_each_destination_and_survives_failures():
    routes = [Route(dir="/a", chat_id="111", bot_token=None, mute=False)]
    seen = []

    def fake_send(cfg, text):
        if cfg.chat_id == "999":
            raise Exception("boom")   # one dead destination
        seen.append((cfg.bot_token, cfg.chat_id, text))

    sent = broadcast.send_all(_cfg(routes), "hello", send=fake_send)
    assert seen == [("G:tok", "111", "hello")]
    assert sent == 1  # the failing destination is not counted, others proceed
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_broadcast.py -v`
Expected: FAIL (`ModuleNotFoundError: ... 'broadcast'`).

- [ ] **Step 3: Implement** — create `claude_code_notify/broadcast.py`:

```python
import dataclasses

from . import notifier


def destinations(config):
    """Distinct (bot_token, chat_id) across the global default and every route
    that has a chat_id. Order-preserving; mute is not consulted."""
    out = []
    seen = set()

    def add(bot_token, chat_id):
        if not chat_id:
            return
        pair = (bot_token, chat_id)
        if pair in seen:
            return
        seen.add(pair)
        out.append(pair)

    add(config.bot_token, config.chat_id)
    for route in config.routes:
        add(route.bot_token or config.bot_token, route.chat_id)
    return out


def send_all(config, text, send=None):
    """Send text to every distinct destination. Each send is guarded so one
    dead destination never aborts the rest. Returns the count sent."""
    sender = notifier.send if send is None else send
    sent = 0
    for bot_token, chat_id in destinations(config):
        dest = dataclasses.replace(config, bot_token=bot_token, chat_id=chat_id)
        try:
            sender(dest, text)
            sent += 1
        except Exception:
            pass
    return sent
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_broadcast.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/broadcast.py tests/test_broadcast.py
git commit -m "feat: broadcast a message to every distinct configured destination

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Recovery — spawn + kill-all

**Files:**
- Create: `claude_code_notify/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Consumes: `usagelimit.usage_state_dir`, `usagelimit.claim`.
- Produces: `spawn(base_dir, window, target_epoch) -> None` (single-instance, detached, no token on argv); `kill_all(base_dir) -> None` (SIGTERM every `*.pid`).

- [ ] **Step 1: Write the failing tests** — create `tests/test_recovery.py`:

```python
import os
import signal
import subprocess
import sys
import time

from claude_code_notify import recovery, usagelimit


def test_spawn_is_single_instance_and_detached(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(recovery.subprocess, "Popen", FakePopen)
    recovery.spawn(str(tmp_path), "w1", 1_800_000_000)
    recovery.spawn(str(tmp_path), "w1", 1_800_000_000)  # same window -> blocked

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "claude_code_notify.recovery"]
    assert "--target" in argv and "1800000000" in argv
    assert kwargs.get("start_new_session") is True
    assert "secret" not in " ".join(argv)  # never a token on argv
    assert os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.sleeper"))


def test_kill_all_signals_valid_pids_only(tmp_path, monkeypatch):
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "w1.pid"), "w") as fh:
        fh.write("4242")
    with open(os.path.join(d, "bad.pid"), "w") as fh:
        fh.write("not-an-int")
    killed = []
    monkeypatch.setattr(recovery.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    recovery.kill_all(str(tmp_path))
    assert killed == [(4242, signal.SIGTERM)]


def test_kill_all_terminates_a_real_process(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    d = usagelimit.usage_state_dir(str(tmp_path))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "w1.pid"), "w") as fh:
        fh.write(str(proc.pid))
    recovery.kill_all(str(tmp_path))
    assert proc.wait(timeout=10) is not None  # actually died
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_recovery.py -v`
Expected: FAIL (`ModuleNotFoundError: ... 'recovery'`).

- [ ] **Step 3: Implement** — create `claude_code_notify/recovery.py`:

```python
import os
import signal
import subprocess
import sys

from . import usagelimit


def spawn(base_dir, window, target_epoch):
    """Launch one detached sleeper for this window. Single-instance via an
    atomic claim. No secrets on argv. Never raises."""
    if not usagelimit.claim(base_dir, window + ".sleeper"):
        return
    try:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(base_dir) + (os.pathsep + existing if existing else "")
        subprocess.Popen(
            [sys.executable, "-m", "claude_code_notify.recovery",
             "--base-dir", str(base_dir), "--window", str(window),
             "--target", str(int(target_epoch))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    except Exception:
        pass  # a spawn failure must never break the hook


def kill_all(base_dir):
    """SIGTERM every live sleeper recorded under the usage-limit state dir."""
    directory = usagelimit.usage_state_dir(base_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".pid"):
            continue
        try:
            with open(os.path.join(directory, name)) as fh:
                pid = int(fh.read().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/recovery.py tests/test_recovery.py
git commit -m "feat: spawn detached single-instance reset sleeper; kill-all for uninstall

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Recovery — wait loop, fire, entrypoint

**Files:**
- Modify: `claude_code_notify/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Consumes: `usagelimit.CAP_SECONDS`, `usagelimit.claim`, `usagelimit.usage_state_dir`, `cfg.load`, `broadcast.send_all`, `notifier.build_message`.
- Produces: `_wait(target, now_fn, sleep_fn, is_done) -> None`; `fire(base_dir, window, when_str, load=None, send=None) -> bool` (broadcasts once via a `.done` claim); `main(argv) -> int` (sleeper mode + `--kill-all`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_recovery.py`:

```python
def test_wait_returns_when_target_reached():
    clock = iter([0, 100, 200])       # deadline-probe, then two loop reads
    slept = []
    recovery._wait(150, lambda: next(clock),
                   lambda s: slept.append(s), lambda: False)
    assert slept == [50]              # slept min(150-100, 60) once, then exited


def test_wait_exits_early_when_done():
    clock = iter([0, 100, 100])
    slept = []
    recovery._wait(150, lambda: next(clock),
                   lambda s: slept.append(s), lambda: True)
    assert slept == []                # done flag short-circuits before sleeping


def test_fire_broadcasts_once(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n")
    sent = []
    ok = recovery.fire(str(tmp_path), "w1", "WHEN",
                       send=lambda c, t: sent.append((c.chat_id, t)))
    assert ok is True
    assert sent == [("999", "Claude Code usage limit reset | WHEN")]
    # Second fire for the same window is blocked by the .done claim.
    sent.clear()
    ok2 = recovery.fire(str(tmp_path), "w1", "WHEN",
                        send=lambda c, t: sent.append(t))
    assert ok2 is False
    assert sent == []


def test_main_kill_all_delegates(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(recovery, "kill_all", lambda base: called.append(base))
    assert recovery.main(["--kill-all", "--base-dir", str(tmp_path)]) == 0
    assert called == [str(tmp_path)]


def test_main_sleeper_fires_when_target_is_past(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(recovery, "fire",
                        lambda base, window, when: fired.append((base, window)))
    rc = recovery.main(["--base-dir", str(tmp_path), "--window", "w1", "--target", "1"])
    assert rc == 0
    assert fired == [(str(tmp_path), "w1")]
    # pid file is cleaned up on exit
    assert not os.path.exists(
        os.path.join(usagelimit.usage_state_dir(str(tmp_path)), "w1.pid"))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_recovery.py -k "wait or fire or main" -v`
Expected: FAIL (`AttributeError: ... '_wait'`).

- [ ] **Step 3: Implement** — add to `claude_code_notify/recovery.py` (add `import time` and `from datetime import datetime` at the top, plus `from . import broadcast`, `from . import config as cfg`, `from . import notifier`):

```python
import time
from datetime import datetime

from . import broadcast
from . import config as cfg
from . import notifier


def _wait(target, now_fn, sleep_fn, is_done):
    deadline = now_fn() + usagelimit.CAP_SECONDS
    while True:
        now = now_fn()
        if now >= target or now >= deadline:
            return
        if is_done():
            return
        sleep_fn(min(target - now, 60))


def _when():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def fire(base_dir, window, when_str, load=None, send=None):
    """Broadcast the reset message exactly once per window (guarded by a .done
    claim). Never raises."""
    if not usagelimit.claim(base_dir, window + ".done"):
        return False
    loader = cfg.load if load is None else load
    try:
        config = loader(base=base_dir)
    except Exception:
        return False
    message = notifier.build_message("usage-limit-reset", "", when_str)
    broadcast.send_all(config, message, send=send)
    return True


def _parse_args(argv):
    opts = {"kill_all": False, "base_dir": None, "window": None, "target": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--kill-all":
            opts["kill_all"] = True
        elif arg == "--base-dir" and i + 1 < len(argv):
            opts["base_dir"] = argv[i + 1]; i += 1
        elif arg == "--window" and i + 1 < len(argv):
            opts["window"] = argv[i + 1]; i += 1
        elif arg == "--target" and i + 1 < len(argv):
            opts["target"] = argv[i + 1]; i += 1
        i += 1
    return opts


def main(argv):
    opts = _parse_args(argv)
    if opts["kill_all"]:
        if opts["base_dir"]:
            kill_all(opts["base_dir"])
        return 0
    base_dir, window, target = opts["base_dir"], opts["window"], opts["target"]
    if not (base_dir and window and target):
        return 0
    try:
        target_epoch = float(target)
    except ValueError:
        return 0
    directory = usagelimit.usage_state_dir(base_dir)
    pid_path = os.path.join(directory, window + ".pid")
    done_path = os.path.join(directory, window + ".done")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(pid_path, "w") as fh:
            fh.write(str(os.getpid()))
        try:
            os.chmod(pid_path, 0o600)
        except OSError:
            pass
        _wait(target_epoch, time.time, time.sleep, lambda: os.path.exists(done_path))
        if time.time() >= target_epoch:
            fire(base_dir, window, _when())
    except Exception:
        pass
    finally:
        try:
            os.remove(pid_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/recovery.py tests/test_recovery.py
git commit -m "feat: reset sleeper wait loop, one-shot fire, and CLI entrypoint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Wire usage-limit handling into hooks

**Files:**
- Modify: `claude_code_notify/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `usagelimit.latest_usage_limit`, `usagelimit.window_key`, `usagelimit.gc`, `usagelimit.claim_hit`, `usagelimit.parse_reset`, `broadcast.send_all`, `recovery.spawn`, `notifier.build_message`.
- Produces: `_maybe_handle_usage_limit(payload, config) -> bool`; `handle_stop` and `handle_stop_failure` short-circuit through it.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_hooks.py`:

```python
def _rate_limit_line(text="You've hit your session limit · resets 9pm (Asia/Hong_Kong)"):
    return json.dumps({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "error": "rate_limit", "apiErrorStatus": 429,
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]}})


def _usage_config(tmp_path, extra=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\nNOTIFY_USAGE_LIMIT=true\n" + extra)


def test_usage_limit_feature_off_by_default(base, tmp_path, monkeypatch):
    # base fixture sets no NOTIFY_USAGE_LIMIT -> feature inert; normal path runs.
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u0", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert "usage limit" not in sent[0]


def test_usage_limit_broadcasts_all_and_suppresses_finished(tmp_path, monkeypatch):
    _usage_config(tmp_path,
                  "NOTIFY_USAGE_LIMIT_RESET=false\n"    # keep the test process-free
                  "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append((c.chat_id, t)))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u1", "transcript_path": transcript, "cwd": "/proj/acme/x"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sorted(chat for chat, _ in sent) == ["111", "999"]
    assert all("Claude Code usage limit reached" in t for _, t in sent)
    assert all("finished" not in t for _, t in sent)


def test_usage_limit_same_window_does_not_resend(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert hooks.run("stop", json.dumps(payload)) == 0   # same window
    assert len(sent) == 1                                # no second broadcast
    assert all("finished" not in t for t in sent)        # still suppressed


def test_usage_limit_schedules_reset_when_enabled(tmp_path, monkeypatch):
    _usage_config(tmp_path)   # NOTIFY_USAGE_LIMIT_RESET defaults true
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    spawned = []
    monkeypatch.setattr(hooks.recovery, "spawn",
                        lambda base, window, target: spawned.append((window, target)))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(spawned) == 1
    assert spawned[0][1] is not None   # a parseable target epoch


def test_usage_limit_reset_disabled_does_not_spawn(tmp_path, monkeypatch):
    _usage_config(tmp_path, "NOTIFY_USAGE_LIMIT_RESET=false\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    spawned = []
    monkeypatch.setattr(hooks.recovery, "spawn",
                        lambda base, window, target: spawned.append(window))
    transcript = _write_transcript(tmp_path, [_rate_limit_line()])
    payload = {"session_id": "u4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert spawned == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_hooks.py -k usage_limit -v`
Expected: FAIL (`AttributeError: module 'claude_code_notify.hooks' has no attribute 'recovery'`).

- [ ] **Step 3: Implement**

In `claude_code_notify/hooks.py`, add three imports alongside the existing `from . import ...` lines:

```python
from . import broadcast
from . import recovery
from . import usagelimit
```

Add the helper just above `handle_stop`:

```python
def _maybe_handle_usage_limit(payload, config):
    """If this turn ended in a usage limit, broadcast to all destinations
    (once per window), optionally schedule the reset ping, and return True so
    the caller skips its normal notification. Never raises out."""
    if not config.usage_limit:
        return False
    transcript = payload.get("transcript_path", "")
    reset_text = usagelimit.latest_usage_limit(transcript)
    if reset_text is None:
        return False
    cwd = payload.get("cwd", "")
    key = usagelimit.window_key(reset_text)
    usagelimit.gc(config.base_dir, _now())
    if usagelimit.claim_hit(config.base_dir, key):
        message = notifier.build_message("usage-limit", cwd, _when(), title=reset_text)
        count = broadcast.send_all(config, message)
        _debug(config, f"usage-limit hit broadcast to {count} destination(s)")
        if config.usage_limit_reset:
            target = usagelimit.parse_reset(reset_text, _now())
            if target is not None:
                recovery.spawn(config.base_dir, key, target)
                _debug(config, f"usage-limit reset scheduled at {int(target)}")
            else:
                _debug(config, "usage-limit reset time unparsed — no reset ping")
    return True
```

Add this as the first line of both `handle_stop` and `handle_stop_failure`:

```python
    if _maybe_handle_usage_limit(payload, config):
        return
```

(For `handle_stop` it goes before `session_id = payload.get(...)`; for `handle_stop_failure` before `cwd = payload.get(...)`.)

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_hooks.py -v`
Expected: PASS (all hooks tests, existing and new).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: broadcast usage-limit alert and schedule reset ping on turn end

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Kill sleepers on uninstall

**Files:**
- Modify: `install.sh:30-35` (the uninstall block)
- Test: `tests/test_install_e2e.py`

**Interfaces:**
- Consumes: `recovery.main(["--kill-all", ...])` (still-installed code at uninstall time).

- [ ] **Step 1: Write the failing test** — append to `tests/test_install_e2e.py`:

```python
def test_uninstall_kills_live_sleeper(tmp_path):
    import sys
    import time
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0, result.stderr
    # A live sleeper recorded under the usage-limit state dir.
    state = base / "state" / "usage_limit"
    state.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (state / "w1.pid").write_text(str(proc.pid))
    result, base, settings = _run(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=10) is not None       # sleeper was terminated
    assert not (base / "state").exists()           # state removed
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_install_e2e.py::test_uninstall_kills_live_sleeper -v`
Expected: FAIL (the sleeper keeps running; `proc.wait(timeout=10)` raises `TimeoutExpired`).

- [ ] **Step 3: Implement**

In `install.sh`, in the `if [ "$MODE" = "uninstall" ]; then` block, add the kill line **before** the `rm -rf` (the code is still present at this point, so the module import resolves):

```bash
if [ "$MODE" = "uninstall" ]; then
  python3 "$BASE_DIR/claude_code_notify/installer.py" remove "$SETTINGS"
  PYTHONPATH="$BASE_DIR" python3 -m claude_code_notify.recovery --kill-all --base-dir "$BASE_DIR" 2>/dev/null || true
  rm -rf "$BASE_DIR/claude_code_notify" "$BASE_DIR/hooks" "$BASE_DIR/state" "$BASE_DIR/debug.log"
  echo "Removed hook entries, code, state, and debug log. config.env kept at $BASE_DIR/config.env (delete manually if desired)."
  exit 0
fi
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_install_e2e.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add install.sh tests/test_install_e2e.py
git commit -m "feat: terminate live reset sleepers on uninstall

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Version bump + documentation

**Files:**
- Modify: `claude_code_notify/__init__.py`, `pyproject.toml`, `CHANGELOG.md`, `README.md`, `docs/claude-notify-product-doc.md`, `CLAUDE.md`
- Test: `tests/test_version.py` (existing — must stay green)

**Interfaces:** none (docs + version).

- [ ] **Step 1: Bump the version**

`claude_code_notify/__init__.py`:

```python
__version__ = "0.4.0"
```

`pyproject.toml` (line 8):

```toml
version = "0.4.0"
```

- [ ] **Step 2: Run the version test**

Run: `python3 -m pytest tests/test_version.py -v`
Expected: PASS (asserts `__init__` and `pyproject` agree).

- [ ] **Step 3: Update `CHANGELOG.md`** — add a new entry at the top of the version list, matching the file's existing style:

```markdown
## [0.4.0]

### Added
- Usage-limit notifications (opt-in, off by default). When the account hits a
  usage limit, broadcast a Telegram alert to every distinct configured
  destination (global default plus every route), detected purely at the
  transcript envelope level (`error == "rate_limit"`). Enable with
  `NOTIFY_USAGE_LIMIT=true`.
- Optional reset ping: at the reported reset time, a one-shot notification that
  the limit has reset, delivered by a transient bounded background process.
  Controlled by `NOTIFY_USAGE_LIMIT_RESET` (default `true`; set `false` to keep
  only the hit broadcast and never spawn a background process). Best-effort —
  missed if the machine is off at reset time; weekly-limit reset times are not
  yet parsed. Uninstall terminates any live sleeper.
```

- [ ] **Step 4: Update `README.md`** — in the configuration section, document both keys and the zero-token/opt-in nature:

```markdown
### Usage-limit notifications (opt-in)

Off by default. When enabled, a Telegram alert is broadcast to **every**
distinct configured destination (the global chat plus every `ROUTE_*` chat)
the moment the account hits a usage limit — because a usage limit is
account-global. Add to `config.env`:

```env
NOTIFY_USAGE_LIMIT=true          # enable the feature (default false)
NOTIFY_USAGE_LIMIT_RESET=true    # also ping when the limit resets (default true);
                                 # set false to keep only the hit alert and never
                                 # spawn a background process
```

Detection is purely structural (the transcript's `rate_limit` error envelope),
so it never mis-fires on ordinary output. The optional reset ping is delivered
by a short-lived background process that waits until the reported reset time,
sends once, and exits (bounded to at most 8 days; killed on uninstall). It is
best-effort: if the machine is off at reset time the ping is simply missed.
The whole feature runs locally plus Telegram HTTP and uses **zero Claude
tokens**.
```

- [ ] **Step 5: Update the product doc** — add a subsection `### 5.5 Usage-limit notifications (v0.4.0)` to `docs/claude-notify-product-doc.md` (after §5.4):

```markdown
### 5.5 Usage-limit notifications (v0.4.0)

Opt-in (`NOTIFY_USAGE_LIMIT`, default off). Because a usage limit is
account-global, notifications **broadcast to every distinct destination**
(global default plus every route, deduped by `(bot_token, chat_id)`; mute is
not consulted).

**Detection** is envelope-level only — the transcript's terminal assistant
entry carrying `isApiErrorMessage == true` and `error == "rate_limit"` (both
session and weekly limits). No text is matched to detect; the reset text is
passed through as the message body and used as an opaque per-window dedup key.
When detected, the misleading normal "finished"/"error" notification is
suppressed for that turn.

**Reset ping** (`NOTIFY_USAGE_LIMIT_RESET`, default on when the feature is on;
set false for hit-only, zero background processes). At the reported reset time
a one-shot "usage limit reset" broadcast is delivered by a transient,
single-instance, detached background process ("sleeper") spawned from the hook:
best-effort local-time parse of the reset moment, a wall-clock wait loop capped
at 8 days, no secrets on its argv, a PID file so uninstall can terminate it, and
**no fallback** if it is killed (miss-is-a-miss). The weekly-limit reset text
format is unverified and currently yields no reset ping (the hit broadcast still
fires). The whole feature consumes zero Claude tokens.
```

- [ ] **Step 6: Update `CLAUDE.md` core rules** — add one line to the core-rules list (this is the sign-off item flagged in the spec):

```markdown
- **Usage-limit notifications are opt-in (`NOTIFY_USAGE_LIMIT`, default off)** and broadcast to every distinct destination; the reset ping's transient background "sleeper" is the one sanctioned exception to the no-daemon rule — bounded (≤8 days), single-instance, best-effort, killed on uninstall (doc §5.5).
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS (entire suite green).

- [ ] **Step 8: Commit**

```bash
git add claude_code_notify/__init__.py pyproject.toml CHANGELOG.md README.md \
        docs/claude-notify-product-doc.md CLAUDE.md
git commit -m "docs: document usage-limit notifications; release v0.4.0

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**

- Detection signal (`error == "rate_limit"`, envelope-level, last assistant entry) → Task 3.
- Two config switches with defaults, env-overridable → Task 1.
- Broadcast to distinct destinations, mute ignored, per-destination guard → Task 6.
- Hit dedup once per window (`claim_hit`), suppress normal notification, GC → Tasks 4 + 9.
- Reset parse (best-effort, 3.8-safe local time, weekly → None, cap clamp) → Task 5.
- Sleeper: single-instance spawn, no token on argv, wall-clock loop, 8-day cap, `.done` one-shot, PID file, no fallback → Tasks 7 + 8.
- Message heads / empty-cwd → Task 2.
- Uninstall kills sleeper → Task 10.
- Versioning v0.4.0, docs, CLAUDE.md sign-off line, weekly-unverified note, zero-token note → Task 11.
- `hooks.py` never raises; secrets scrubbed & never on argv; core testable offline → enforced across Tasks 3–9 (guards + injected `send`/clocks; tests never spawn a real sleeper except the explicit real-kill test in Task 7 and the uninstall e2e in Task 10, both of which terminate quickly).

**Optional `zoneinfo` precision** (spec §Reset-time parsing): intentionally deferred — Task 5 implements the 3.8-safe local-time path only. Claude Code prints the reset time in the machine's local timezone, so local-time computation is correct on the common path; layering `zoneinfo` when the account TZ differs from the OS TZ is a future refinement and is listed here so it is not mistaken for a gap.

**Placeholder scan:** none — every code and test step contains complete content.

**Type consistency:** `latest_usage_limit`/`window_key`/`parse_reset`/`claim`/`claim_hit`/`gc`/`usage_state_dir`/`CAP_SECONDS` (usagelimit); `destinations`/`send_all` (broadcast); `spawn`/`kill_all`/`_wait`/`fire`/`main` (recovery); `_maybe_handle_usage_limit` (hooks) — names and signatures are used identically across defining and consuming tasks.

## Execution Handoff

Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.
