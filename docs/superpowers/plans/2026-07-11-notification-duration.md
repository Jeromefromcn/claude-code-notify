# Notification Duration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show how long the current turn has been running in every notification (`finished` / `error` / `needs-input`), e.g. `Claude Code finished | 3m12s | Add duration | /home/x | 11/07/2026 01:15:00`.

**Architecture:** A new `transcript_parser.turn_start_timestamp()` scans the transcript (same linear-scan style as the existing `latest_ai_title()`) for the last genuine user-turn-start envelope's ISO-8601 `timestamp`. `hooks.py` parses that timestamp, diffs it against "now", and formats it compactly (`45s` / `3m12s` / `1h05m`). `notifier.build_message()` gets one new optional `duration` parameter inserted right after the head.

**Tech Stack:** Python 3.8+, stdlib only (`json`, `datetime`, `re`), pytest.

## Global Constraints

- Parse transcripts at the JSON envelope level only — never substring-match text.
- `hooks.py` must never raise or exit non-zero on internal errors; catch, no-op.
- Core must be testable without a live Claude Code session and without hitting real Telegram.
- Preserve existing behavior: any transcript with no matching turn-start entry produces `duration=None` and the field is silently omitted (same convention `title` already uses).

Full design context: [`docs/superpowers/specs/2026-07-11-notification-duration-design.md`](../specs/2026-07-11-notification-duration-design.md).

---

### Task 1: `turn_start_timestamp()` in `transcript_parser.py`

**Files:**
- Modify: `claude_code_notify/transcript_parser.py`
- Test: `tests/test_transcript_parser.py`

**Interfaces:**
- Produces: `turn_start_timestamp(path: str) -> str | None` — returns the raw ISO-8601 `timestamp` string of the last genuine user-turn-start envelope, or `None` if none found / file missing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript_parser.py`:

```python
def test_turn_start_timestamp_picks_last_real_user_entry(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"first question"}]}}\n'
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T01:00:05.000Z",'
        '"message":{"content":[{"type":"text","text":"answer"}]}}\n'
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:05:00.000Z",'
        '"message":{"content":[{"type":"text","text":"follow up"}]}}\n'
    )
    assert tp.turn_start_timestamp(str(path)) == "2026-07-11T01:05:00.000Z"


def test_turn_start_timestamp_ignores_sidechain(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"real"}]}}\n'
        '{"type":"user","isSidechain":true,"timestamp":"2026-07-11T01:10:00.000Z",'
        '"message":{"content":[{"type":"text","text":"subagent internal"}]}}\n'
    )
    assert tp.turn_start_timestamp(str(path)) == "2026-07-11T01:00:00.000Z"


def test_turn_start_timestamp_ignores_tool_result_only(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"real turn start"}]}}\n'
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:20:00.000Z",'
        '"message":{"content":[{"type":"tool_result","tool_use_id":"a","content":"done"}]}}\n'
    )
    # The tool_result envelope is a background task reporting back mid-turn,
    # not a new turn start, so the earlier real entry still wins.
    assert tp.turn_start_timestamp(str(path)) == "2026-07-11T01:00:00.000Z"


def test_turn_start_timestamp_no_match_returns_none(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Bash","input":{}}]}}\n'
    )
    assert tp.turn_start_timestamp(str(path)) is None


def test_turn_start_timestamp_missing_file_returns_none():
    assert tp.turn_start_timestamp("/no/such/file.jsonl") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_parser.py -k turn_start_timestamp -v`
Expected: FAIL with `AttributeError: module 'claude_code_notify.transcript_parser' has no attribute 'turn_start_timestamp'`

- [ ] **Step 3: Implement `turn_start_timestamp()`**

Add to `claude_code_notify/transcript_parser.py`, after `_completion_ids` and before `parse_events` (keeps the file's existing top-to-bottom order: helpers, then the two public entry points `parse_events` and `latest_ai_title`):

```python
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
```

Add to the end of `claude_code_notify/transcript_parser.py`, after `latest_ai_title`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript_parser.py -v`
Expected: all PASS, including the 5 new tests and all pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/transcript_parser.py tests/test_transcript_parser.py
git commit -m "feat: add turn_start_timestamp to transcript_parser"
```

---

### Task 2: Duration parsing/formatting helpers in `hooks.py`

**Files:**
- Modify: `claude_code_notify/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `transcript_parser.turn_start_timestamp(path) -> str | None` (Task 1).
- Produces: `_format_duration(seconds: float | None) -> str | None`, `_parse_ts(ts: str) -> float | None`, `_turn_duration(transcript: str, now: float) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hooks.py`:

```python
def test_format_duration_seconds():
    assert hooks._format_duration(0) == "0s"
    assert hooks._format_duration(45) == "45s"
    assert hooks._format_duration(59) == "59s"


def test_format_duration_minutes():
    assert hooks._format_duration(60) == "1m00s"
    assert hooks._format_duration(192) == "3m12s"
    assert hooks._format_duration(3599) == "59m59s"


def test_format_duration_hours():
    assert hooks._format_duration(3600) == "1h00m"
    assert hooks._format_duration(3900) == "1h05m"


def test_format_duration_negative_or_none_is_none():
    assert hooks._format_duration(-1) is None
    assert hooks._format_duration(None) is None


def test_parse_ts_valid():
    assert hooks._parse_ts("2026-07-11T01:00:00.000Z") == pytest.approx(1783731600.0)


def test_parse_ts_invalid_returns_none():
    assert hooks._parse_ts("not a timestamp") is None
    assert hooks._parse_ts(None) is None


def test_turn_duration_no_turn_start_returns_none(tmp_path):
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Bash","input":{}}]}}',
    ])
    assert hooks._turn_duration(transcript, hooks._now()) is None


def test_turn_duration_computed_from_transcript(tmp_path):
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    start = hooks._parse_ts("2026-07-11T01:00:00.000Z")
    assert hooks._turn_duration(transcript, start + 192) == "3m12s"
```

`test_parse_ts_valid`'s expected epoch value is only used for the `pytest.approx` shape; the exact number doesn't matter since `test_turn_duration_computed_from_transcript` (which drives real behavior) computes its expected `start` the same way the implementation will, so it can't drift from the implementation's own arithmetic.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hooks.py -k "format_duration or parse_ts or turn_duration" -v`
Expected: FAIL with `AttributeError: module 'claude_code_notify.hooks' has no attribute '_format_duration'` (and similarly for the others).

- [ ] **Step 3: Implement the helpers**

In `claude_code_notify/hooks.py`, change the import line near the top from:

```python
from .transcript_parser import latest_ai_title
```

to:

```python
from .transcript_parser import latest_ai_title, turn_start_timestamp
```

Then add, after `_when()` and before `_debug()`:

```python
def _format_duration(seconds):
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def _turn_duration(transcript, now):
    start_str = turn_start_timestamp(transcript)
    if not start_str:
        return None
    start = _parse_ts(start_str)
    if start is None:
        return None
    return _format_duration(now - start)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hooks.py -v`
Expected: all PASS, including all pre-existing tests (nothing else in `hooks.py` changed behavior yet).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: add duration parsing/formatting helpers to hooks.py"
```

---

### Task 3: `duration` parameter in `notifier.build_message()`

**Files:**
- Modify: `claude_code_notify/notifier.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Produces: `build_message(kind, cwd, when, title=None, duration=None) -> str`, field order `head | duration | title | cwd | when` (falsy fields omitted).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifier.py`:

```python
def test_build_message_with_duration_and_title():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task", "3m12s")
    assert msg == "Claude Code finished | 3m12s | My Task | /home/x | 09/07/2026 10:00:00"


def test_build_message_with_duration_no_title():
    msg = notifier.build_message("error", "/home/x", "09/07/2026 10:00:00", None, "45s")
    assert msg == "Claude Code stopped with error | 45s | /home/x | 09/07/2026 10:00:00"


def test_build_message_omits_absent_duration():
    msg = notifier.build_message("finished", "/home/x", "09/07/2026 10:00:00", "My Task")
    assert msg == "Claude Code finished | My Task | /home/x | 09/07/2026 10:00:00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_notifier.py -k build_message_with_duration -v`
Expected: FAIL — `test_build_message_with_duration_and_title` gets `"Claude Code finished | My Task | /home/x | 09/07/2026 10:00:00"` (extra `"3m12s"` positional arg currently has no effect since `build_message` doesn't accept a 5th parameter, so this actually fails with `TypeError: build_message() takes from 3 to 4 positional arguments but 5 were given`).

- [ ] **Step 3: Implement**

In `claude_code_notify/notifier.py`, replace:

```python
def build_message(kind, cwd, when, title=None):
    parts = [_HEADS[kind]]
    if title:
        parts.append(title)
    parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)
```

with:

```python
def build_message(kind, cwd, when, title=None, duration=None):
    parts = [_HEADS[kind]]
    if duration:
        parts.append(duration)
    if title:
        parts.append(title)
    parts.append(cwd)
    parts.append(when)
    return " | ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_notifier.py -v`
Expected: all PASS, including all pre-existing `build_message` tests (they don't pass `duration`, so it defaults to `None` and output is unchanged).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/notifier.py tests/test_notifier.py
git commit -m "feat: add duration parameter to notifier.build_message"
```

---

### Task 4: Wire duration into the hook handlers

**Files:**
- Modify: `claude_code_notify/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `_turn_duration(transcript, now) -> str | None` (Task 2), `notifier.build_message(kind, cwd, when, title=None, duration=None)` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hooks.py`:

```python
def test_stop_includes_duration_when_turn_start_present(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:03:12.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "sdur1", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code finished | 3m12s | Do a thing | /w | " + sent[0].split(" | ")[-1]


def test_stop_omits_duration_without_turn_start(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}',
        '{"type":"ai-title","aiTitle":"Do a thing"}',
    ])
    payload = {"session_id": "sdur2", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code finished | Do a thing | /w | " + sent[0].split(" | ")[-1]


def test_stop_failure_includes_duration(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:00:45.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    payload = {"session_id": "sdur3", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code stopped with error | 45s | /w | " + sent[0].split(" | ")[-1]


def test_permission_request_includes_duration(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:01:00.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"user","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"text","text":"go"}]}}',
    ])
    payload = {"session_id": "sdur4", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert sent[0] == "Claude Code needs your input | 1m00s | /w | " + sent[0].split(" | ")[-1]
```

Note `_now` is patched as a module attribute (`hooks._now`), so the handlers must call it as `_now()` at call time (already true today — see Step 3) rather than capturing it at import time.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hooks.py -k "includes_duration or omits_duration" -v`
Expected: FAIL — messages don't contain the duration segment yet (handlers don't call `_turn_duration`/pass `duration` to `build_message`).

- [ ] **Step 3: Implement**

In `claude_code_notify/hooks.py`, update the three handlers:

```python
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
    duration = _turn_duration(transcript, _now())
    notifier.send(config, notifier.build_message("finished", cwd, _when(), title, duration))
    ratelimit.record_sent(marker, _now())
    _debug(config, f"stop session={session_id} notified")


def handle_stop_failure(payload, config):
    cwd = payload.get("cwd", "")
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    notifier.send(config, notifier.build_message("error", cwd, _when(), title, duration))
    _debug(config, "stop_failure notified")


def handle_permission_request(payload, config):
    cwd = payload.get("cwd", "")
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    notifier.send(config, notifier.build_message("needs-input", cwd, _when(), title, duration))
    _debug(config, "permission_request notified")
```

(`handle_stop_failure` and `handle_permission_request` previously read `payload.get("transcript_path", "")` inline twice via `latest_ai_title(payload.get("transcript_path", ""))`; both now bind it to a local `transcript` once so `_turn_duration` can reuse it without a second `.get()` call.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS — every pre-existing test plus all new ones from Tasks 1-4.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: include turn duration in Stop/StopFailure/PermissionRequest notifications"
```

---

### Task 5: Update README and CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Update README**

In `README.md`, under `## What it does`, add a bullet after the existing "Sends a Telegram message..." bullet:

```markdown
- Shows how long the turn took (e.g. `3m12s`) right in the notification.
```

In `README.md`, under `## Configuration`, no change needed (no new config knob). In the message-shape example nowhere currently shown verbatim in README — skip.

- [ ] **Step 2: Add a CHANGELOG entry**

Read the top of `CHANGELOG.md` first to match its existing format (heading style, `Added`/`Changed`/`Fixed` sections, version placeholder convention), then add an entry under the "Unreleased" (or equivalent top) section:

```markdown
### Added
- Notifications now include how long the turn took (e.g. `3m12s`).
```

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document turn duration in notifications"
```

---

## Self-Review Notes

- **Spec coverage:** turn-start detection → Task 1; duration compute/format → Task 2; message field/order → Task 3; wiring into all three handlers → Task 4; docs → Task 5. Edge cases (no match, malformed timestamp, negative delta) are covered by dedicated tests in Tasks 1 and 2.
- **Type consistency:** `turn_start_timestamp` (Task 1) returns `str | None`; `_parse_ts` (Task 2) takes that `str` and returns `float | None`; `_format_duration` (Task 2) takes `float | None` and returns `str | None`; `_turn_duration` (Task 2) composes all three and is what Task 4 calls — signatures match end to end.
- **No placeholder steps** — the one intentional "placeholder line" shown in Task 2 Step 3 is explicitly called out as scaffolding to delete, with the real, complete replacement given immediately after.
