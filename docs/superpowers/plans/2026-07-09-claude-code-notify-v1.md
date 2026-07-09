# claude-code-notify v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, versioned tool that sends Telegram notifications from Claude Code `Stop`/`StopFailure`/`PermissionRequest` hooks — firing only when a turn truly finishes (all background tasks resolved), errors, or blocks on input.

**Architecture:** A pure-Python stdlib core (`claude_code_notify/`) does all logic and is unit-testable with no live Claude Code session and no real Telegram. Thin Bash shims forward Claude Code's stdin hook JSON to `python3 -m claude_code_notify.hooks <event>`. An idempotent `install.sh` places the package under `~/.claude/claude-code-notify/`, writes `config.env` (chmod 600), and merges only its own tagged entries into `settings.json`. Completion detection parses the session JSONL transcript at the envelope level, tracking background `Agent`/`Bash` launches vs. `<task-notification>` resolutions.

**Tech Stack:** Python 3 (stdlib only — `json`, `urllib`, `re`, `dataclasses`; no third-party runtime deps), pytest for tests, Bash for shims/installer, GitHub Actions for CI.

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from `docs/claude-notify-product-doc.md` and `CLAUDE.md`.

- **Runtime deps:** `python3` only. No pip-installable runtime dependencies. HTTP via `urllib` from the stdlib. (pytest is a test-only dep.)
- **Completion rule:** a background dispatch (`Agent`, or `Bash` with `run_in_background=true`) is resolved **only** by a `<task-notification>` matching its `tool_use_id`. An immediate ack `tool_result` never resolves it. `PENDING = launched − resolved`; notify only when `PENDING == 0`.
- **Parse at the JSON envelope level** (`type`, `tool_use`/`message.content` fields, `origin.kind`). Never substring-match free text to classify an entry. The `<tool-use-id>` is extracted by regex **only** from within an entry already structurally identified as a task-notification.
- **Bash shims are thin membranes** — forward stdin unchanged to `python3 -m claude_code_notify.hooks <event>`. No business logic in shell.
- **hooks entry points never raise or exit non-zero** on internal errors — catch, log if `NOTIFY_DEBUG` is on, exit 0.
- **Debug logging off by default**, gated by `NOTIFY_DEBUG`; writes to `~/.claude/claude-code-notify/debug.log` (chmod 600, secrets scrubbed) only when enabled.
- **Config lives in `~/.claude/claude-code-notify/config.env`** (chmod 600), never inlined into `settings.json`, never committed.
- **Installer merges/removes only its own tagged hook entries** in `settings.json` via the `json` module — never `sed`/string surgery. Idempotent.
- **Secrets scrubbed** from any error/log output (the bot token appears in the request URL) before display or write.
- **Core testable without a live Claude Code session and without real Telegram.**
- **Credit external work** in README.md "Related work". No uncredited borrowing.
- **v1 scope:** Telegram-only, global-install-only (`~/.claude/`). No other channels, no project-level install.
- **Hook event names & message formats** (confirmed against the live `settings.json` this replaces):
  - `Stop` → `Claude Code finished | <title> | <cwd> | <time>`
  - `StopFailure` → `Claude Code stopped with error | <title> | <cwd> | <time>`
  - `PermissionRequest` → `Claude Code needs your input | <title> | <cwd> | <time>`
  - `<title>` is optional (the transcript's latest `aiTitle`); omit that segment if absent. `<time>` format: `%d/%m/%Y %H:%M:%S`. Matcher for all three is `""`.

### Grounded transcript facts (verified against real `~/.claude/projects/*.jsonl`)

Fixtures and parser MUST match these real shapes:

- **Launch (background):** `{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_...","name":"Bash","input":{"command":"...","run_in_background":true}}]}}`. For `Agent`, `input` is `{description, model, prompt}` with **no** `run_in_background` key (Agent defaults to background → track unless `run_in_background` is explicitly `false`).
- **Completion, variant A (queue-operation):** `{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>..</task-id>\n<tool-use-id>toolu_..</tool-use-id>\n..<status>completed</status>..</task-notification>"}` — `content` is a **string**.
- **Completion, variant B (user):** `{"type":"user","origin":{"kind":"task-notification"},"message":{"role":"user","content":"<task-notification>..<tool-use-id>toolu_..</tool-use-id>..</task-notification>"}}` — `message.content` is a **string**. The same `tool_use_id` appears in **both** variants; set-based `resolved` dedups.
- **Statuses observed:** `completed`, `failed`, `killed`, `stopped` — **all** count as resolved (any matching task-notification resolves the launch, regardless of status).
- **Title:** `{"type":"ai-title","aiTitle":"..."}` — take the last such entry's `aiTitle`.
- **Sidechain:** entries generated inside a subagent carry `isSidechain:true`; launches from those MUST be ignored (only the main turn's launches count).

---

## File Structure

Created under repo root:

```
claude_code_notify/
├── __init__.py            # __version__ = "0.1.0"
├── __main__.py            # `python -m claude_code_notify --version`
├── config.py              # Config dataclass, path helpers, load()
├── transcript_parser.py   # parse_events(), latest_ai_title()
├── pending_tracker.py     # State, load/save_state, compute_pending()
├── ratelimit.py           # should_send(), record_sent()
├── notifier.py            # scrub(), build_message(), send(), NotifierError
└── hooks.py               # handle_* , run(), main() — never raises
hooks/
├── stop.sh
├── stop_failure.sh
└── permission_request.sh
tests/
├── conftest.py
├── fixtures/*.jsonl
├── test_config.py
├── test_transcript_parser.py
├── test_pending_tracker.py
├── test_ratelimit.py
├── test_notifier.py
├── test_hooks.py
└── test_install.py
install.sh
pyproject.toml
CHANGELOG.md
CONTRIBUTING.md
CODE_OF_CONDUCT.md
.github/workflows/ci.yml
```

Dependency order: Task 1 (scaffold) → 2 (config) → 3 (parser) → 4 (tracker, uses parser) → 5 (ratelimit) → 6 (notifier, uses config) → 7 (hooks, wires all) → 8 (shims) → 9 (installer) → 10 (installer tests) → 11 (hygiene/CI).

---

## Task 1: Package scaffold, version, `pyproject.toml`

**Files:**
- Create: `claude_code_notify/__init__.py`
- Create: `claude_code_notify/__main__.py`
- Create: `pyproject.toml`
- Create: `tests/conftest.py`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `claude_code_notify.__version__: str` (value `"0.1.0"`); `python -m claude_code_notify --version` prints the bare version and exits 0; `tests/conftest.py` makes the repo root importable so `import claude_code_notify` works under pytest.

- [ ] **Step 1: Write the failing test**

`tests/test_version.py`:
```python
import subprocess
import sys

import claude_code_notify


def test_version_string():
    assert claude_code_notify.__version__ == "0.1.0"


def test_version_cli():
    out = subprocess.run(
        [sys.executable, "-m", "claude_code_notify", "--version"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "0.1.0"
```

`tests/conftest.py`:
```python
import os
import sys

# Make the repo root importable so `import claude_code_notify` works.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_version.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/__init__.py`:
```python
__version__ = "0.1.0"
```

`claude_code_notify/__main__.py`:
```python
import sys

from . import __version__


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    print(f"claude-code-notify {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "claude-code-notify"
version = "0.1.0"
description = "Telegram notifications from Claude Code hooks: turn finished, blocked, or errored."
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.8"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=7"]

[tool.setuptools]
packages = ["claude_code_notify"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_version.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/__init__.py claude_code_notify/__main__.py pyproject.toml tests/conftest.py tests/test_version.py
git commit -m "feat: package scaffold, version, and pyproject"
```

---

## Task 2: `config.py` — config loading & path helpers

**Files:**
- Create: `claude_code_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class ConfigError(Exception)`
  - `@dataclass class Config: bot_token: str; chat_id: str; ratelimit_seconds: int; api_base: str; debug: bool; base_dir: pathlib.Path`
  - `def default_base_dir(environ=None) -> Path` — `CLAUDE_NOTIFY_HOME` env override, else `~/.claude/claude-code-notify`.
  - `def config_path(base: Path) -> Path` → `base/"config.env"`
  - `def state_dir(base: Path) -> Path` → `base/"state"`
  - `def state_path(base: Path, session_id: str) -> Path` → `state_dir/f"{session_id}.state.json"`
  - `def marker_path(base: Path, session_id: str) -> Path` → `state_dir/f"{session_id}.marker"`
  - `def debug_log_path(base: Path) -> Path` → `base/"debug.log"`
  - `def parse_env_file(text: str) -> dict[str, str]` — parses `KEY=value`, ignores blank lines and `#` comments, strips whitespace and surrounding quotes.
  - `def load(environ=None, base=None) -> Config` — reads `config.env` if present, overlays `environ` (env wins), requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (else `ConfigError`), defaults `NOTIFY_RATELIMIT_SECONDS=120`, `TELEGRAM_API_BASE=https://api.telegram.org`, `NOTIFY_DEBUG=false`.

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import pytest

from claude_code_notify import config as cfg


def test_parse_env_file_basic():
    text = "# comment\nTELEGRAM_BOT_TOKEN=123:abc\n\nTELEGRAM_CHAT_ID=999\n"
    d = cfg.parse_env_file(text)
    assert d == {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "999"}


def test_parse_env_file_strips_quotes():
    assert cfg.parse_env_file('K="v"\n') == {"K": "v"}
    assert cfg.parse_env_file("K='v'\n") == {"K": "v"}


def test_load_from_file(tmp_path):
    base = tmp_path
    (base / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=base)
    assert c.bot_token == "123:abc"
    assert c.chat_id == "999"
    assert c.ratelimit_seconds == 120
    assert c.api_base == "https://api.telegram.org"
    assert c.debug is False
    assert c.base_dir == base


def test_env_overrides_file(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=fromfile\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(
        environ={"TELEGRAM_BOT_TOKEN": "fromenv", "NOTIFY_DEBUG": "true",
                 "NOTIFY_RATELIMIT_SECONDS": "5"},
        base=tmp_path,
    )
    assert c.bot_token == "fromenv"
    assert c.debug is True
    assert c.ratelimit_seconds == 5


def test_missing_required_raises(tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.load(environ={}, base=tmp_path)


def test_default_base_dir_env_override():
    p = cfg.default_base_dir(environ={"CLAUDE_NOTIFY_HOME": "/tmp/xyz"})
    assert str(p) == "/tmp/xyz"


def test_path_helpers(tmp_path):
    assert cfg.config_path(tmp_path).name == "config.env"
    assert cfg.state_path(tmp_path, "sess1").name == "sess1.state.json"
    assert cfg.marker_path(tmp_path, "sess1").name == "sess1.marker"
    assert cfg.debug_log_path(tmp_path).name == "debug.log"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.config'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/config.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/config.py tests/test_config.py
git commit -m "feat: config loading and path helpers"
```

---

## Task 3: `transcript_parser.py` — envelope-level event parsing

**Files:**
- Create: `claude_code_notify/transcript_parser.py`
- Create: `tests/fixtures/foreground_only.jsonl`
- Create: `tests/fixtures/bg_agent_pending.jsonl`
- Create: `tests/fixtures/bg_agent_completed.jsonl`
- Create: `tests/fixtures/bg_bash_ack_only.jsonl`
- Create: `tests/fixtures/bg_bash_completed.jsonl`
- Create: `tests/fixtures/notif_twice.jsonl`
- Create: `tests/fixtures/sidechain_launch.jsonl`
- Test: `tests/test_transcript_parser.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass(frozen=True) class LaunchEvent: tool_use_id: str`
  - `@dataclass(frozen=True) class CompletionEvent: tool_use_id: str`
  - `def parse_events(path, offset=0) -> tuple[list, int]` — returns `(events_in_order, new_offset_bytes)`; consumes only complete newline-terminated lines; missing file → `([], offset)`; skips malformed lines.
  - `def latest_ai_title(path) -> str | None`

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/foreground_only.jsonl` (a foreground Bash whose result appears in-turn — no background launch, no notification):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_fg1","name":"Bash","input":{"command":"ls","run_in_background":false}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_fg1","content":"a\nb"}]}}
{"type":"ai-title","aiTitle":"List files"}
```

`tests/fixtures/bg_agent_pending.jsonl` (background Agent launched, never completes):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_ag1","name":"Agent","input":{"description":"do work","model":"sonnet","prompt":"go"}}]}}
```

`tests/fixtures/bg_agent_completed.jsonl` (background Agent + queue-operation task-notification):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_ag2","name":"Agent","input":{"description":"do work","model":"sonnet","prompt":"go"}}]}}
{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>t1</task-id>\n<tool-use-id>toolu_ag2</tool-use-id>\n<status>completed</status>\n</task-notification>"}
```

`tests/fixtures/bg_bash_ack_only.jsonl` (THE REGRESSION CASE: bg Bash + immediate ack tool_result, NO task-notification → still pending):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_bg1","name":"Bash","input":{"command":"sleep 100","run_in_background":true}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_bg1","content":"Command running in background with ID: bt7ccudal"}]}}
```

`tests/fixtures/bg_bash_completed.jsonl` (bg Bash ack THEN task-notification):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_bg2","name":"Bash","input":{"command":"sleep 1","run_in_background":true}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_bg2","content":"Command running in background with ID: xyz"}]}}
{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<tool-use-id>toolu_bg2</tool-use-id>\n<status>completed</status>\n</task-notification>"}
```

`tests/fixtures/notif_twice.jsonl` (same id notified via BOTH variants → resolved once):
```
{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"toolu_ag3","name":"Agent","input":{"description":"x","model":"sonnet","prompt":"y"}}]}}
{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<tool-use-id>toolu_ag3</tool-use-id>\n<status>completed</status>\n</task-notification>"}
{"type":"user","origin":{"kind":"task-notification"},"message":{"role":"user","content":"<task-notification>\n<tool-use-id>toolu_ag3</tool-use-id>\n<status>completed</status>\n</task-notification>"}
```

`tests/fixtures/sidechain_launch.jsonl` (a bg launch INSIDE a subagent must be ignored):
```
{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"tool_use","id":"toolu_side1","name":"Bash","input":{"command":"sleep 5","run_in_background":true}}]}}
```

- [ ] **Step 2: Write the failing test**

`tests/test_transcript_parser.py`:
```python
import os

from claude_code_notify import transcript_parser as tp
from claude_code_notify.transcript_parser import LaunchEvent, CompletionEvent

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _events(name):
    events, _ = tp.parse_events(os.path.join(FIX, name))
    return events


def test_foreground_only_no_events():
    # Foreground Bash (run_in_background:false) is never a tracked launch.
    assert _events("foreground_only.jsonl") == []


def test_background_agent_launch_detected():
    assert _events("bg_agent_pending.jsonl") == [LaunchEvent("toolu_ag1")]


def test_background_agent_completed():
    evs = _events("bg_agent_completed.jsonl")
    assert LaunchEvent("toolu_ag2") in evs
    assert CompletionEvent("toolu_ag2") in evs


def test_background_bash_ack_is_not_completion():
    # The immediate "Command running in background" ack must NOT resolve.
    evs = _events("bg_bash_ack_only.jsonl")
    assert LaunchEvent("toolu_bg1") in evs
    assert CompletionEvent("toolu_bg1") not in evs


def test_background_bash_completed():
    evs = _events("bg_bash_completed.jsonl")
    assert LaunchEvent("toolu_bg2") in evs
    assert CompletionEvent("toolu_bg2") in evs


def test_task_notification_both_variants():
    evs = _events("notif_twice.jsonl")
    completions = [e for e in evs if isinstance(e, CompletionEvent)]
    # Both a queue-operation and a user/origin.kind entry are parsed.
    assert len(completions) == 2
    assert all(e.tool_use_id == "toolu_ag3" for e in completions)


def test_sidechain_launch_ignored():
    assert _events("sidechain_launch.jsonl") == []


def test_incremental_matches_full(tmp_path):
    path = tmp_path / "t.jsonl"
    line1 = '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    line2 = '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n'
    path.write_text(line1)
    ev1, off1 = tp.parse_events(str(path), 0)
    assert ev1 == [LaunchEvent("a")]
    with open(path, "a") as fh:
        fh.write(line2)
    ev2, off2 = tp.parse_events(str(path), off1)
    assert ev2 == [CompletionEvent("a")]
    ev_full, off_full = tp.parse_events(str(path), 0)
    assert ev_full == [LaunchEvent("a"), CompletionEvent("a")]
    assert off2 == off_full


def test_partial_trailing_line_not_consumed(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_bytes(b'{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n{"type":"assist')
    events, off = tp.parse_events(str(path), 0)
    assert events == [LaunchEvent("a")]
    # Offset stops at the end of the last complete line, not the partial one.
    assert off == path.read_bytes().index(b"\n") + 1


def test_missing_file_returns_offset():
    assert tp.parse_events("/no/such/file.jsonl", 42) == ([], 42)


def test_malformed_line_skipped(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text('not json\n{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n')
    events, _ = tp.parse_events(str(path), 0)
    assert events == [LaunchEvent("a")]


def test_latest_ai_title():
    assert tp.latest_ai_title(os.path.join(FIX, "foreground_only.jsonl")) == "List files"
    assert tp.latest_ai_title(os.path.join(FIX, "bg_agent_pending.jsonl")) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_transcript_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.transcript_parser'`.

- [ ] **Step 4: Write minimal implementation**

`claude_code_notify/transcript_parser.py`:
```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_transcript_parser.py -v`
Expected: PASS (all tests green).

- [ ] **Step 6: Commit**

```bash
git add claude_code_notify/transcript_parser.py tests/fixtures tests/test_transcript_parser.py
git commit -m "feat: envelope-level transcript parser with grounded fixtures"
```

---

## Task 4: `pending_tracker.py` — resolution rule, state file, PENDING count

**Files:**
- Create: `claude_code_notify/pending_tracker.py`
- Test: `tests/test_pending_tracker.py`

**Interfaces:**
- Consumes: `transcript_parser.parse_events`, `LaunchEvent`, `CompletionEvent`.
- Produces:
  - `@dataclass class State: offset: int = 0; launched: set = <factory>; resolved: set = <factory>`
  - `def load_state(path) -> State` — missing/corrupt → `State()`.
  - `def save_state(path, state) -> None` — atomic write via `.tmp` + `os.replace`, `chmod 600`, creates parent dir.
  - `def compute_pending(transcript_path, state_path) -> int` — loads state, resets to `State()` if `transcript` size `< state.offset` (rotation/truncation), parses new events from `state.offset`, folds launches/resolutions into sets, saves state, returns `len(launched - resolved)`.

- [ ] **Step 1: Write the failing test**

`tests/test_pending_tracker.py`:
```python
import json
import os
import stat

from claude_code_notify import pending_tracker as pt

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _pending(name, tmp_path):
    return pt.compute_pending(
        os.path.join(FIX, name), str(tmp_path / f"{name}.state.json")
    )


def test_foreground_only_pending_zero(tmp_path):
    assert _pending("foreground_only.jsonl", tmp_path) == 0


def test_background_agent_pending_one(tmp_path):
    assert _pending("bg_agent_pending.jsonl", tmp_path) == 1


def test_background_agent_completed_zero(tmp_path):
    assert _pending("bg_agent_completed.jsonl", tmp_path) == 0


def test_background_bash_ack_only_still_pending(tmp_path):
    # Regression: the immediate ack must not resolve the launch.
    assert _pending("bg_bash_ack_only.jsonl", tmp_path) == 1


def test_background_bash_completed_zero(tmp_path):
    assert _pending("bg_bash_completed.jsonl", tmp_path) == 0


def test_notification_twice_resolves_once(tmp_path):
    assert _pending("notif_twice.jsonl", tmp_path) == 0


def test_state_persists_and_is_chmod_600(tmp_path):
    state_path = str(tmp_path / "s.state.json")
    pt.compute_pending(os.path.join(FIX, "bg_agent_pending.jsonl"), state_path)
    assert os.path.exists(state_path)
    mode = stat.S_IMODE(os.stat(state_path).st_mode)
    assert mode == 0o600
    data = json.loads(open(state_path).read())
    assert data["launched"] == ["toolu_ag1"]
    assert data["resolved"] == []
    assert data["offset"] > 0


def test_incremental_across_appends(tmp_path):
    src = tmp_path / "live.jsonl"
    state_path = str(tmp_path / "live.state.json")
    src.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), state_path) == 1
    with open(src, "a") as fh:
        fh.write('{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n')
    assert pt.compute_pending(str(src), state_path) == 0


def test_rotation_triggers_full_rescan(tmp_path):
    src = tmp_path / "rot.jsonl"
    state_path = str(tmp_path / "rot.state.json")
    src.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), state_path) == 1
    # File shrinks (rotated) and now shows a completed, different task.
    src.write_text('{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n')
    # Full rescan from 0: launch "a" is gone, only its completion remains → 0 pending.
    assert pt.compute_pending(str(src), state_path) == 0


def test_corrupt_state_falls_back(tmp_path):
    state_path = tmp_path / "c.state.json"
    state_path.write_text("{not valid json")
    result = pt.compute_pending(os.path.join(FIX, "bg_agent_pending.jsonl"), str(state_path))
    assert result == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pending_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.pending_tracker'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/pending_tracker.py`:
```python
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pending_tracker.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/pending_tracker.py tests/test_pending_tracker.py
git commit -m "feat: pending tracker with incremental state and rotation fallback"
```

---

## Task 5: `ratelimit.py` — dedup / anti-spam marker

**Files:**
- Create: `claude_code_notify/ratelimit.py`
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `def should_send(marker_path, threshold, now) -> bool` — `True` if no marker or `now - last >= threshold`; missing/corrupt marker → `True`.
  - `def record_sent(marker_path, now) -> None` — atomic write of `str(now)`, creates parent dir.

- [ ] **Step 1: Write the failing test**

`tests/test_ratelimit.py`:
```python
from claude_code_notify import ratelimit as rl


def test_no_marker_allows(tmp_path):
    assert rl.should_send(str(tmp_path / "m"), 120, now=1000.0) is True


def test_within_threshold_blocks(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1050.0) is False


def test_after_threshold_allows(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1121.0) is True


def test_exactly_at_threshold_allows(tmp_path):
    marker = str(tmp_path / "m")
    rl.record_sent(marker, now=1000.0)
    assert rl.should_send(marker, 120, now=1120.0) is True


def test_corrupt_marker_allows(tmp_path):
    marker = tmp_path / "m"
    marker.write_text("garbage")
    assert rl.should_send(str(marker), 120, now=1000.0) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.ratelimit'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/ratelimit.py`:
```python
import os


def should_send(marker_path, threshold, now):
    try:
        with open(marker_path) as fh:
            last = float(fh.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return True
    return (now - last) >= threshold


def record_sent(marker_path, now):
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    tmp = f"{marker_path}.tmp"
    with open(tmp, "w") as fh:
        fh.write(str(now))
    os.replace(tmp, marker_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/ratelimit.py tests/test_ratelimit.py
git commit -m "feat: rate-limit marker for notification dedup"
```

---

## Task 6: `notifier.py` — message formatting, secret scrubbing, Telegram send

**Files:**
- Create: `claude_code_notify/notifier.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `config.Config` (uses `.api_base`, `.bot_token`, `.chat_id`).
- Produces:
  - `class NotifierError(Exception)`
  - `def scrub(text, token) -> str` — replaces the token with `***` (no-op if token falsy).
  - `def build_message(kind, cwd, when, title=None) -> str` — `kind` in `{"finished","error","needs-input"}`; format `"<head> | [<title> | ]<cwd> | <when>"`.
  - `def send(config, text) -> None` — POSTs `chat_id` + `text` to `{api_base}/bot{token}/sendMessage` via `urllib`; on failure raises `NotifierError` whose message is scrubbed of the token (and uses `raise ... from None` so the token-bearing URL never leaks via the exception chain).

- [ ] **Step 1: Write the failing test**

`tests/test_notifier.py`:
```python
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from claude_code_notify import notifier
from claude_code_notify.config import Config


def _cfg(api_base, token="123:secret", chat_id="999"):
    from pathlib import Path
    return Config(bot_token=token, chat_id=chat_id, ratelimit_seconds=120,
                  api_base=api_base, debug=False, base_dir=Path("/tmp"))


def test_scrub_removes_token():
    assert notifier.scrub("url bot123:secret/x", "123:secret") == "url bot***/x"


def test_scrub_noop_without_token():
    assert notifier.scrub("hello", "") == "hello"


def test_build_message_finished_with_title():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task")
    assert msg == "Claude Code finished | My Task | /home/x | 09/07/2026 10:00:00"


def test_build_message_omits_absent_title():
    msg = notifier.build_message("error", "/home/x", "09/07/2026 10:00:00", None)
    assert msg == "Claude Code stopped with error | /home/x | 09/07/2026 10:00:00"


def test_build_message_needs_input():
    msg = notifier.build_message("needs-input", "/home/x", "T")
    assert msg.startswith("Claude Code needs your input |")


class _Capture(BaseHTTPRequestHandler):
    received = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        _Capture.received = {"path": self.path, "form": parse_qs(body)}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def test_send_posts_expected_payload():
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.handle_request, daemon=True).start()
    host, port = server.server_address
    cfg = _cfg(f"http://{host}:{port}")
    notifier.send(cfg, "hello world")
    assert _Capture.received["path"] == "/bot123:secret/sendMessage"
    assert _Capture.received["form"]["chat_id"] == ["999"]
    assert _Capture.received["form"]["text"] == ["hello world"]


def test_send_failure_scrubs_token():
    # Nothing is listening on this port → connection refused.
    cfg = _cfg("http://127.0.0.1:1")
    with pytest.raises(notifier.NotifierError) as exc:
        notifier.send(cfg, "hi")
    assert "123:secret" not in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.notifier'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/notifier.py`:
```python
import urllib.error
import urllib.parse
import urllib.request


class NotifierError(Exception):
    pass


_HEADS = {
    "finished": "Claude Code finished",
    "error": "Claude Code stopped with error",
    "needs-input": "Claude Code needs your input",
}


def scrub(text, token):
    if token:
        text = text.replace(token, "***")
    return text


def build_message(kind, cwd, when, title=None):
    parts = [_HEADS[kind]]
    if title:
        parts.append(title)
    parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)


def send(config, text):
    url = f"{config.api_base}/bot{config.bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": config.chat_id, "text": text}).encode()
    request = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        # `from None` prevents the token-bearing URL leaking via __context__.
        raise NotifierError(scrub(str(exc), config.bot_token)) from None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notifier.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/notifier.py tests/test_notifier.py
git commit -m "feat: telegram notifier with secret scrubbing"
```

---

## Task 7: `hooks.py` — entry points wiring everything, never raises

**Files:**
- Create: `claude_code_notify/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `config.load`, `config.state_path`, `config.marker_path`, `config.debug_log_path`; `pending_tracker.compute_pending`; `ratelimit.should_send`/`record_sent`; `notifier.build_message`/`send`/`scrub`; `transcript_parser.latest_ai_title`.
- Produces:
  - `def handle_stop(payload, config) -> None` — computes pending; if `> 0` returns; else rate-limit gate; on send, `record_sent`.
  - `def handle_stop_failure(payload, config) -> None` — sends `"error"` directly (no pending/rate-limit gate).
  - `def handle_permission_request(payload, config) -> None` — sends `"needs-input"` directly.
  - `def run(event, stdin_text) -> int` — parses stdin JSON (bad JSON → `{}`), loads config, dispatches, **catches all exceptions**, returns 0 always.
  - `def main(argv) -> int` — reads stdin, calls `run(argv[1], stdin)`; `python -m claude_code_notify.hooks <event>` entry.
  - Payload keys read: `session_id`, `transcript_path`, `cwd`.

- [ ] **Step 1: Write the failing test**

`tests/test_hooks.py`:
```python
import json
import os

import pytest

from claude_code_notify import hooks
from claude_code_notify.config import Config


@pytest.fixture
def base(tmp_path, monkeypatch):
    # Isolate config + state under a temp CLAUDE_NOTIFY_HOME.
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    return tmp_path


def _write_transcript(tmp_path, lines):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(l + "\n" for l in lines))
    return str(path)


def test_stop_pending_does_not_send(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s1", "transcript_path": transcript, "cwd": "/w"}
    rc = hooks.run("stop", json.dumps(payload))
    assert rc == 0
    assert sent == []  # background Agent still pending


def test_stop_completed_sends_once(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "s2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0] == "Claude Code finished | Do a thing | /w | " + sent[0].split(" | ")[-1]
    # Second immediate Stop is rate-limited.
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1


def test_stop_failure_sends_directly(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert len(sent) == 1
    assert sent[0].startswith("Claude Code stopped with error |")


def test_permission_request_sends_directly(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert sent[0].startswith("Claude Code needs your input |")


def test_run_never_raises_on_bad_json(base):
    assert hooks.run("stop", "not json at all") == 0


def test_run_never_raises_on_missing_config(tmp_path, monkeypatch):
    # No config.env → ConfigError must be swallowed.
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert hooks.run("stop", "{}") == 0


def test_run_never_raises_on_notifier_error(base, tmp_path, monkeypatch):
    def boom(cfg, text):
        raise hooks.notifier.NotifierError("boom")
    monkeypatch.setattr(hooks.notifier, "send", boom)
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "s5", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0


def test_debug_log_written_and_scrubbed(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nNOTIFY_DEBUG=true\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s6", "transcript_path": transcript, "cwd": "/w"}
    hooks.run("stop", json.dumps(payload))
    log = (tmp_path / "debug.log").read_text()
    assert "pending=1" in log
    assert "123:secret" not in log
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.hooks'`.

- [ ] **Step 3: Write minimal implementation**

`claude_code_notify/hooks.py`:
```python
import json
import os
import sys
import time
from datetime import datetime

from . import config as cfg
from . import notifier
from . import ratelimit
from .pending_tracker import compute_pending
from .transcript_parser import latest_ai_title


def _now():
    return time.time()


def _when():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _debug(config, line):
    if config is None or not config.debug:
        return
    try:
        path = str(cfg.debug_log_path(config.base_dir))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        scrubbed = notifier.scrub(line, config.bot_token)
        with open(path, "a") as fh:
            fh.write(f"{datetime.now().isoformat()} {scrubbed}\n")
        os.chmod(path, 0o600)
    except Exception:
        pass  # debug logging must never itself break the hook


def handle_stop(payload, config):
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")
    pending = compute_pending(transcript, str(cfg.state_path(config.base_dir, session_id)))
    _debug(config, f"stop session={session_id} pending={pending}")
    if pending > 0:
        return
    marker = str(cfg.marker_path(config.base_dir, session_id))
    if not ratelimit.should_send(marker, config.ratelimit_seconds, _now()):
        _debug(config, f"stop session={session_id} suppressed by rate-limit")
        return
    title = latest_ai_title(transcript)
    notifier.send(config, notifier.build_message("finished", cwd, _when(), title))
    ratelimit.record_sent(marker, _now())
    _debug(config, f"stop session={session_id} notified")


def handle_stop_failure(payload, config):
    cwd = payload.get("cwd", "")
    title = latest_ai_title(payload.get("transcript_path", ""))
    notifier.send(config, notifier.build_message("error", cwd, _when(), title))
    _debug(config, "stop_failure notified")


def handle_permission_request(payload, config):
    cwd = payload.get("cwd", "")
    title = latest_ai_title(payload.get("transcript_path", ""))
    notifier.send(config, notifier.build_message("needs-input", cwd, _when(), title))
    _debug(config, "permission_request notified")


_HANDLERS = {
    "stop": handle_stop,
    "stop_failure": handle_stop_failure,
    "permission_request": handle_permission_request,
}


def run(event, stdin_text):
    config = None
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        config = cfg.load()
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(payload, config)
    except Exception as exc:  # never propagate — must not break the user's turn
        _debug(config, f"error in {event}: {exc!r}")
    return 0


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    return run(event, stdin_text)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS (every test from Tasks 1–7).

- [ ] **Step 6: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: hook entry points that never crash the turn"
```

---

## Task 8: Bash shims — thin membranes forwarding stdin

**Files:**
- Create: `hooks/stop.sh`
- Create: `hooks/stop_failure.sh`
- Create: `hooks/permission_request.sh`
- Test: `tests/test_shims.py`

**Interfaces:**
- Consumes: `python3 -m claude_code_notify.hooks <event>` (Task 7).
- Produces: three executable scripts. Each computes the package root as its own parent dir, sets `PYTHONPATH` to it, and `exec`s `python3 -m claude_code_notify.hooks <event>` so stdin passes through untouched.

- [ ] **Step 1: Write the failing test**

`tests/test_shims.py`:
```python
import json
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_stop_shim_forwards_stdin(tmp_path):
    # A pending background Agent → shim runs, exits 0, sends nothing.
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    payload = json.dumps({"session_id": "s", "transcript_path": str(transcript), "cwd": "/w"})
    env = dict(os.environ, CLAUDE_NOTIFY_HOME=str(tmp_path))
    result = subprocess.run(
        ["bash", os.path.join(REPO, "hooks", "stop.sh")],
        input=payload, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    # No marker written because pending > 0 → nothing sent.
    assert not (tmp_path / "state" / "s.marker").exists()


def test_all_three_shims_exist_and_executable():
    for name in ("stop.sh", "stop_failure.sh", "permission_request.sh"):
        path = os.path.join(REPO, "hooks", name)
        assert os.path.exists(path)
        assert os.access(path, os.X_OK)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_shims.py -v`
Expected: FAIL — shim scripts do not exist.

- [ ] **Step 3: Write the shims**

`hooks/stop.sh`:
```bash
#!/usr/bin/env bash
# Thin membrane: forward Claude Code's stdin hook JSON unchanged to the core.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m claude_code_notify.hooks stop
```

`hooks/stop_failure.sh`:
```bash
#!/usr/bin/env bash
# Thin membrane: forward Claude Code's stdin hook JSON unchanged to the core.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m claude_code_notify.hooks stop_failure
```

`hooks/permission_request.sh`:
```bash
#!/usr/bin/env bash
# Thin membrane: forward Claude Code's stdin hook JSON unchanged to the core.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m claude_code_notify.hooks permission_request
```

Then mark executable:
```bash
chmod +x hooks/stop.sh hooks/stop_failure.sh hooks/permission_request.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_shims.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck hooks/*.sh`
Expected: no output, exit 0. (If `shellcheck` is not installed, note it for CI and continue.)

- [ ] **Step 6: Commit**

```bash
git add hooks/stop.sh hooks/stop_failure.sh hooks/permission_request.sh tests/test_shims.py
git commit -m "feat: bash shims forwarding stdin to the python core"
```

---

## Task 9: `install.sh` — idempotent install / upgrade / uninstall

**Files:**
- Create: `install.sh`
- Create: `claude_code_notify/installer.py` (Python helper for the settings.json merge — installer calls it so the merge is `json`-module based and unit-testable, never `sed`)
- Test: `tests/test_installer.py` (unit-tests the merge helper)

**Interfaces:**
- Consumes: the package files (Tasks 1–8).
- Produces:
  - `installer.py`:
    - `MANAGED_MARKER = "claude-code-notify"` — tag identifying our entries (their `command` path contains it).
    - `def hook_entry(base_dir, event_script) -> dict` → `{"matcher": "", "hooks": [{"type": "command", "command": "<base_dir>/hooks/<event_script>"}]}`.
    - `def merge_hooks(settings: dict, base_dir: str) -> dict` — for each of `Stop`/`StopFailure`/`PermissionRequest`, removes any existing entry whose command contains `MANAGED_MARKER`, then appends our entry. Idempotent. Leaves unrelated hooks intact.
    - `def remove_hooks(settings: dict) -> dict` — removes only entries whose command contains `MANAGED_MARKER`; prunes emptied event lists.
    - `def main(argv) -> int` — subcommands `merge <settings_path> <base_dir>` and `remove <settings_path>`; reads/writes the JSON file atomically.
  - `install.sh`: verifies `python3`; copies/downloads package + `hooks/` into `$CLAUDE_NOTIFY_HOME` (default `~/.claude/claude-code-notify`); on first install prompts (no-echo) for token + chat id and writes `config.env` chmod 600 (skips if it already exists — upgrade); calls `python3 .../installer.py merge <settings.json> <base_dir>`; supports `--uninstall`, `--non-interactive`, `--version <tag>`.

- [ ] **Step 1: Write the failing test for the merge helper**

`tests/test_installer.py`:
```python
import json

from claude_code_notify import installer


def test_merge_adds_three_events():
    settings = {}
    out = installer.merge_hooks(settings, "/home/u/.claude/claude-code-notify")
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert event in out["hooks"]
        cmd = out["hooks"][event][0]["hooks"][0]["command"]
        assert "claude-code-notify" in cmd
        assert out["hooks"][event][0]["matcher"] == ""


def test_merge_is_idempotent():
    base = "/home/u/.claude/claude-code-notify"
    out = installer.merge_hooks({}, base)
    out2 = installer.merge_hooks(out, base)
    assert len(out2["hooks"]["Stop"]) == 1  # not duplicated


def test_merge_preserves_foreign_hooks():
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    out = installer.merge_hooks(settings, "/base/claude-code-notify")
    stops = out["hooks"]["Stop"]
    assert len(stops) == 2
    assert any("echo other" in e["hooks"][0]["command"] for e in stops)
    assert any("claude-code-notify" in e["hooks"][0]["command"] for e in stops)


def test_remove_only_ours():
    base = "/base/claude-code-notify"
    settings = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    ]}}
    merged = installer.merge_hooks(settings, base)
    removed = installer.remove_hooks(merged)
    stops = removed["hooks"]["Stop"]
    assert len(stops) == 1
    assert "echo other" in stops[0]["hooks"][0]["command"]


def test_remove_prunes_empty_event():
    base = "/base/claude-code-notify"
    merged = installer.merge_hooks({}, base)
    removed = installer.remove_hooks(merged)
    # All three events were only ours → they should be gone entirely.
    assert removed.get("hooks", {}) == {}


def test_merge_main_roundtrip(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    rc = installer.main(["merge", str(settings_file), "/base/claude-code-notify"])
    assert rc == 0
    data = json.loads(settings_file.read_text())
    assert "Stop" in data["hooks"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_installer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.installer'`.

- [ ] **Step 3: Write the merge helper**

`claude_code_notify/installer.py`:
```python
import json
import os
import sys

MANAGED_MARKER = "claude-code-notify"

_EVENTS = {
    "Stop": "stop.sh",
    "StopFailure": "stop_failure.sh",
    "PermissionRequest": "permission_request.sh",
}


def hook_entry(base_dir, event_script):
    command = os.path.join(base_dir, "hooks", event_script)
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _is_ours(entry):
    for hook in entry.get("hooks", []):
        if MANAGED_MARKER in hook.get("command", ""):
            return True
    return False


def merge_hooks(settings, base_dir):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    for event, script in _EVENTS.items():
        existing = [e for e in hooks.get(event, []) if not _is_ours(e)]
        existing.append(hook_entry(base_dir, script))
        hooks[event] = existing
    settings["hooks"] = hooks
    return settings


def remove_hooks(settings):
    settings = dict(settings)
    hooks = dict(settings.get("hooks", {}))
    for event in list(hooks.keys()):
        kept = [e for e in hooks[event] if not _is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def _load(path):
    if os.path.exists(path):
        with open(path) as fh:
            text = fh.read().strip()
        if text:
            return json.loads(text)
    return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv):
    if len(argv) >= 3 and argv[0] == "merge":
        _save(argv[1], merge_hooks(_load(argv[1]), argv[2]))
        return 0
    if len(argv) >= 2 and argv[0] == "remove":
        _save(argv[1], remove_hooks(_load(argv[1])))
        return 0
    sys.stderr.write("usage: installer.py merge <settings> <base_dir> | remove <settings>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_installer.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Write `install.sh`**

`install.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main"
REPO_TARBALL="https://github.com/Jeromefromcn/claude-code-notify/archive/refs"
BASE_DIR="${CLAUDE_NOTIFY_HOME:-$HOME/.claude/claude-code-notify}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
MODE="install"
VERSION="main"
NONINTERACTIVE="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE="uninstall" ;;
    --non-interactive) NONINTERACTIVE="1" ;;
    --version) shift; VERSION="$1" ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

if [ "$MODE" = "uninstall" ]; then
  python3 "$BASE_DIR/claude_code_notify/installer.py" remove "$SETTINGS"
  rm -rf "$BASE_DIR/claude_code_notify" "$BASE_DIR/hooks"
  echo "Removed hook entries and code. config.env kept at $BASE_DIR/config.env (delete manually if desired)."
  exit 0
fi

mkdir -p "$BASE_DIR"

# Obtain the package. If running from a checkout (install.sh sits next to the
# package), copy locally; otherwise download the pinned tarball.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SRC_DIR/claude_code_notify" ]; then
  cp -R "$SRC_DIR/claude_code_notify" "$SRC_DIR/hooks" "$BASE_DIR/"
else
  TMP="$(mktemp -d)"
  curl -fsSL "$REPO_TARBALL/heads/$VERSION.tar.gz" -o "$TMP/pkg.tgz" \
    || curl -fsSL "$REPO_TARBALL/tags/$VERSION.tar.gz" -o "$TMP/pkg.tgz"
  tar -xzf "$TMP/pkg.tgz" -C "$TMP" --strip-components=1
  cp -R "$TMP/claude_code_notify" "$TMP/hooks" "$BASE_DIR/"
  rm -rf "$TMP"
fi
chmod +x "$BASE_DIR"/hooks/*.sh

# Config: keep existing (upgrade) or create (first install).
CONFIG="$BASE_DIR/config.env"
if [ -f "$CONFIG" ]; then
  echo "Existing config.env kept (upgrade)."
else
  if [ "$NONINTERACTIVE" = "1" ]; then
    : "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN for --non-interactive}"
    : "${TELEGRAM_CHAT_ID:?set TELEGRAM_CHAT_ID for --non-interactive}"
  else
    read -r -s -p "Telegram bot token: " TELEGRAM_BOT_TOKEN; echo
    read -r -p "Telegram chat id: " TELEGRAM_CHAT_ID
  fi
  umask 177
  cat > "$CONFIG" <<EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
NOTIFY_RATELIMIT_SECONDS=120
NOTIFY_DEBUG=false
EOF
  chmod 600 "$CONFIG"
  echo "Wrote $CONFIG (chmod 600)."
fi

python3 "$BASE_DIR/claude_code_notify/installer.py" merge "$SETTINGS" "$BASE_DIR"

VER="$(python3 -c "import sys; sys.path.insert(0, '$BASE_DIR'); import claude_code_notify; print(claude_code_notify.__version__)")"
echo "claude-code-notify $VER installed. Test: echo '{}' | bash $BASE_DIR/hooks/stop.sh && echo ok"
```

Then mark executable:
```bash
chmod +x install.sh
```

- [ ] **Step 6: Manually smoke-test the installer against a temp HOME**

Run:
```bash
tmp=$(mktemp -d)
CLAUDE_NOTIFY_HOME="$tmp/ccn" CLAUDE_SETTINGS="$tmp/settings.json" \
  TELEGRAM_BOT_TOKEN=123:x TELEGRAM_CHAT_ID=999 \
  bash install.sh --non-interactive
cat "$tmp/settings.json"
stat -c '%a' "$tmp/ccn/config.env"
```
Expected: `settings.json` has `Stop`/`StopFailure`/`PermissionRequest` entries pointing at `$tmp/ccn/hooks/*.sh`; `config.env` mode is `600`.

- [ ] **Step 7: Commit**

```bash
git add claude_code_notify/installer.py install.sh tests/test_installer.py
git commit -m "feat: idempotent installer with json-based settings merge"
```

---

## Task 10: End-to-end installer test (temp `$HOME`, idempotency, uninstall)

**Files:**
- Test: `tests/test_install_e2e.py`

**Interfaces:**
- Consumes: `install.sh`, `installer.py`.
- Produces: a pytest that runs `install.sh` in a sandbox `$HOME` and asserts file placement, `config.env` mode `600`, exact settings merge, idempotency on re-run, and full revert on `--uninstall`.

- [ ] **Step 1: Write the failing test**

`tests/test_install_e2e.py`:
```python
import json
import os
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(tmp_path, *args):
    base = tmp_path / "ccn"
    settings = tmp_path / "settings.json"
    env = dict(
        os.environ,
        CLAUDE_NOTIFY_HOME=str(base),
        CLAUDE_SETTINGS=str(settings),
        TELEGRAM_BOT_TOKEN="123:secret",
        TELEGRAM_CHAT_ID="999",
    )
    result = subprocess.run(
        ["bash", os.path.join(REPO, "install.sh"), *args],
        capture_output=True, text=True, env=env,
    )
    return result, base, settings


@pytest.mark.skipif(subprocess.run(["which", "bash"]).returncode != 0, reason="bash required")
def test_install_places_files_and_merges(tmp_path):
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert (base / "claude_code_notify" / "hooks.py").exists()
    assert (base / "hooks" / "stop.sh").exists()
    assert stat.S_IMODE(os.stat(base / "config.env").st_mode) == 0o600
    data = json.loads(settings.read_text())
    for event in ("Stop", "StopFailure", "PermissionRequest"):
        assert any("claude-code-notify" in e["hooks"][0]["command"] for e in data["hooks"][event])


def test_install_is_idempotent(tmp_path):
    _run(tmp_path, "--non-interactive")
    result, base, settings = _run(tmp_path, "--non-interactive")
    assert result.returncode == 0
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["Stop"]) == 1  # no duplicate entry


def test_upgrade_keeps_config(tmp_path):
    _run(tmp_path, "--non-interactive")
    base = tmp_path / "ccn"
    (base / "config.env").write_text("TELEGRAM_BOT_TOKEN=keepme\nTELEGRAM_CHAT_ID=1\n")
    _run(tmp_path, "--non-interactive")
    assert "keepme" in (base / "config.env").read_text()


def test_uninstall_reverts_settings(tmp_path):
    _run(tmp_path, "--non-interactive")
    result, base, settings = _run(tmp_path, "--uninstall")
    assert result.returncode == 0
    data = json.loads(settings.read_text())
    assert data.get("hooks", {}) == {}
    assert not (base / "hooks").exists()
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_install_e2e.py -v`
Expected: PASS if Task 9's `install.sh` is correct. If any assertion fails, fix `install.sh` (not the test) until green. (These tests exercise the real script; they may pass immediately since the implementation exists — that is acceptable for an end-to-end task.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_install_e2e.py
git commit -m "test: end-to-end installer idempotency and uninstall"
```

---

## Task 11: Project hygiene — CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, README "Related work", CI

**Files:**
- Create: `CHANGELOG.md`
- Create: `CONTRIBUTING.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `.github/workflows/ci.yml`
- Modify: `README.md` (ensure install/config/uninstall/"How it works"/"Related work" sections exist and match this implementation)

**Interfaces:**
- Consumes: the whole package + `install.sh`.
- Produces: CI that runs `pytest` and `shellcheck`; standard OSS docs; README that credits prior art per the CLAUDE.md rule.

- [ ] **Step 1: Write `CHANGELOG.md`**

`CHANGELOG.md`:
```markdown
# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-07-09

### Added
- Initial release. Telegram notifications from Claude Code `Stop`,
  `StopFailure`, and `PermissionRequest` hooks.
- Correct completion detection: a background `Agent` or `Bash`
  (`run_in_background=true`) dispatch is resolved only by a
  `<task-notification>` matching its `tool_use_id`. The immediate
  "Command running in background" ack no longer causes a false "finished".
- Incremental transcript parsing with per-session state cache.
- Rate-limit/dedup marker (default 120s) to avoid Stop-storm spam.
- Idempotent `install.sh` with `--uninstall`, `--non-interactive`,
  `--version`; config isolated in `config.env` (chmod 600).
- Optional debug logging via `NOTIFY_DEBUG`.
```

- [ ] **Step 2: Write `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`**

`CONTRIBUTING.md`:
```markdown
# Contributing

Thanks for helping improve claude-code-notify.

## Development

- Runtime requires only `python3` (stdlib). Tests require `pytest`.
- Install dev deps: `pip install -e ".[dev]"`
- Run the suite: `python -m pytest -v`
- Lint shell: `shellcheck install.sh hooks/*.sh`

## Ground rules

- The core must stay testable without a live Claude Code session and
  without hitting real Telegram. Add fixtures under `tests/fixtures/`.
- Parse transcripts at the JSON envelope level — never substring-match
  free text to classify an entry.
- `hooks.py` entry points must never raise or exit non-zero.
- Never commit secrets. Config lives only in `config.env` (chmod 600).
- Credit any external project consulted in README's "Related work".

## Pull requests

- One logical change per PR. Update `CHANGELOG.md`.
- Every accuracy fix should state the concrete case it corrects.
```

`CODE_OF_CONDUCT.md`:
```markdown
# Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)
v2.1. Be respectful and constructive. Report unacceptable behavior to the
repository owner via a GitHub issue or direct contact.
```

- [ ] **Step 3: Write `.github/workflows/ci.yml`**

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.8", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: python -m pytest -v
  shellcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: sudo apt-get update && sudo apt-get install -y shellcheck
      - run: shellcheck install.sh hooks/*.sh
```

- [ ] **Step 4: Update `README.md`**

Read the current `README.md`, then ensure it contains, in order: what it is; one-command install (`curl -fsSL https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main/install.sh | bash`) and the `git clone && ./install.sh` alternative; configuration (`config.env` keys, chmod 600); uninstall (`install.sh --uninstall`); "How it works" (the completion-detection rule and the background-Bash fix); troubleshooting (`NOTIFY_DEBUG=true` → `debug.log`); and a **"Related work"** section crediting the prior personal Stop-hook setup this generalizes and any upstream issues referenced (anthropics/claude-code #25147, #33049, #7881). Preserve existing accurate content; only add/correct.

- [ ] **Step 5: Run the full suite and shellcheck**

Run: `python -m pytest -v && shellcheck install.sh hooks/*.sh`
Expected: all tests PASS; shellcheck clean.

- [ ] **Step 6: Commit**

```bash
git add CHANGELOG.md CONTRIBUTING.md CODE_OF_CONDUCT.md .github/workflows/ci.yml README.md
git commit -m "docs: changelog, contributing, code of conduct, CI, README"
```

---

## Self-Review

**1. Spec coverage** (Doc §-by-§):
- §4.1–4.2 background dispatch table & unified resolution rule → Task 3 (`_launch_ids`, `_completion_ids`) + Task 4 (`compute_pending`).
- §4.3 signals parsed at envelope level → Task 3, verified against real fixtures.
- §4.4 incremental state cache → Task 4 (`offset`, rotation fallback).
- §4.5 dedup/rate-limit → Task 5 + Task 7 gate.
- §5.1 module table → Tasks 2–7 (one module each). §5.2 data flow → Task 7 `handle_stop`. §5.3 config storage → Task 2 + Task 9 (`config.env` chmod 600). §5.3.1 debug logging → Task 7 `_debug`. §5.4 settings.json merge → Task 9 `installer.py`.
- §6 install/upgrade/flags → Task 9. §7 versioning → Task 1 (`__version__`, `--version`). §8 testing → every task's tests + Task 10 e2e. §9 security (token isolation, scrubbing, no-crash, no echo, tagged merge) → Tasks 6/7/9. §10 repo layout → all tasks. §11 roadmap → out of scope (respected). §12 decisions → reflected in names/paths.

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" — every code step contains complete code. README step (Task 11 Step 4) describes sections rather than pasting full prose; acceptable because it edits an existing file whose current content must be read first, and the required sections are enumerated exactly.

**3. Type consistency:** `Config` fields consistent across Tasks 2/6/7. `LaunchEvent`/`CompletionEvent` consistent Task 3→4. `compute_pending(transcript_path, state_path)` signature consistent Task 4→7. `should_send(marker, threshold, now)`/`record_sent(marker, now)` consistent Task 5→7. `build_message(kind, cwd, when, title)`/`send(config, text)`/`scrub(text, token)` consistent Task 6→7. `merge_hooks`/`remove_hooks`/`MANAGED_MARKER` consistent Task 9→10. Path helpers (`state_path`, `marker_path`, `debug_log_path`, `default_base_dir` with `CLAUDE_NOTIFY_HOME`) defined in Task 2, used in Tasks 4/7/9.

**Open follow-up (non-blocking):** first real install should migrate the token currently in plaintext in `~/.claude/settings.json` into `config.env` and remove the inline hook commands — call this out to the user during rollout, not in code.
