# Pending-Launch Staleness Cutoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A background dispatch (`Agent`/`Bash`/`SendMessage`) that never receives a matching `<task-notification>` must eventually stop blocking "finished" notifications for the rest of its session, instead of suppressing them forever — fixing [docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md](../../lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md).

**Architecture:** Give every tracked launch a timestamp (from the transcript envelope), persist it in the pending-tracker's state file, and let `compute_pending()` drop any unresolved launch older than a new `NOTIFY_PENDING_STALE_SECONDS` config knob (default 4h; `0` disables) from the pending count — logging the expiry via the existing debug channel. A launch with no known timestamp (including old-format state files written before this change) is treated as immediately stale once the feature is enabled, so already-stuck sessions self-heal on their next `Stop` event with no manual intervention.

**Tech Stack:** Python 3, pytest, stdlib only (`json`, `time`, `datetime`) — matches the rest of `claude_code_notify`.

## Global Constraints

- `hooks.py` must never raise or exit non-zero on internal errors (existing rule — this change must preserve it).
- Debug logging stays off by default, gated by `NOTIFY_DEBUG`; new log lines follow the existing `_debug(config, ...)` pattern.
- Core logic (`pending_tracker.py`, `transcript_parser.py`, `config.py`) must stay testable without a live Claude Code session — no new fixture requires anything beyond a hand-written JSONL file.
- Parse transcript signals at the JSON envelope level only — never substring-match text (existing rule; this change reads `envelope.get("timestamp")`, nothing new to violate this).
- All code, tests, docs, and commit messages are in English.
- Follow TDD for every code task below: write the failing test first, confirm it fails for the expected reason, then write the minimal implementation.
- Run `python3 -m pytest` from the repo root (`/home/ubuntu/jerome/claude-code-notify`) after every task; the full suite (245 tests before this plan) must stay green.

---

## File Structure

| File | Responsibility |
|---|---|
| `claude_code_notify/transcript_parser.py` | `LaunchEvent` gains a `timestamp` field, populated from the envelope's `timestamp` key. |
| `claude_code_notify/pending_tracker.py` | `State.launched` becomes `{tool_use_id: iso_timestamp_or_None}`; `compute_pending()` gains `stale_seconds`/`now`/`on_stale` params that prune expired launches before counting `pending`. |
| `claude_code_notify/config.py` | New `Config.pending_stale_seconds` field, parsed from `NOTIFY_PENDING_STALE_SECONDS` (default `14400`; `<= 0` → `None`, i.e. disabled). |
| `claude_code_notify/hooks.py` | `handle_stop()` passes `config.pending_stale_seconds` and a single `now` into `compute_pending()`, and logs any expired launches. |
| `tests/test_transcript_parser.py`, `tests/test_pending_tracker.py`, `tests/test_config.py`, `tests/test_hooks.py` | Updated/added coverage for all of the above. |
| `README.md`, `CLAUDE.md`, `docs/claude-notify-product-doc.md`, `docs/lessons-learned/0007-...md` | Document the new config knob and close out 0007. |

---

### Task 1: `transcript_parser.LaunchEvent` carries a launch timestamp

**Files:**
- Modify: `claude_code_notify/transcript_parser.py:8-16` (the `LaunchEvent` dataclass) and `:102-103` (`parse_events`'s launch-append loop)
- Test: `tests/test_transcript_parser.py`

**Interfaces:**
- Produces: `LaunchEvent(tool_use_id: str, timestamp: str | None = None)` — `timestamp` is the raw ISO8601 string from the envelope's `"timestamp"` key (e.g. `"2026-07-30T16:18:05.840Z"`), or `None` if the envelope has none. Existing callers that construct `LaunchEvent(some_id)` keep working unchanged (default applies).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_transcript_parser.py`:

```python
def test_launch_event_carries_envelope_timestamp(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-30T16:18:05.840Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    events, _ = tp.parse_events(str(path))
    assert events == [LaunchEvent("a", "2026-07-30T16:18:05.840Z")]


def test_launch_event_timestamp_none_when_envelope_has_none():
    evs = _events("bg_agent_pending.jsonl")
    assert evs == [LaunchEvent("toolu_ag1")]
    assert evs[0].timestamp is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_parser.py -k launch_event_carries -v`
Expected: FAIL — `TypeError: LaunchEvent() takes 2 positional arguments but 3 were given` (the dataclass doesn't have a `timestamp` field yet), and the equality in the second test fails with an `AttributeError` on `.timestamp`.

- [ ] **Step 3: Write minimal implementation**

In `claude_code_notify/transcript_parser.py`, change:

```python
@dataclass(frozen=True)
class LaunchEvent:
    tool_use_id: str
```

to:

```python
@dataclass(frozen=True)
class LaunchEvent:
    tool_use_id: str
    timestamp: str = None  # envelope's ISO8601 "timestamp" field, or None if absent
```

Then in `parse_events()`, change:

```python
        for tid in _launch_ids(envelope):
            events.append(LaunchEvent(tid))
```

to:

```python
        for tid in _launch_ids(envelope):
            events.append(LaunchEvent(tid, envelope.get("timestamp")))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript_parser.py -v`
Expected: PASS, all tests including the two new ones and every pre-existing one (they all construct `LaunchEvent(id)` with no timestamp, which now defaults to `None` and still compares equal).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/transcript_parser.py tests/test_transcript_parser.py
git commit -m "feat: carry envelope timestamp on LaunchEvent"
```

---

### Task 2: `pending_tracker` — timestamped launches, staleness pruning, legacy migration

**Files:**
- Modify: `claude_code_notify/pending_tracker.py` (whole file — `State`, `load_state`, `save_state`, `compute_pending`)
- Test: `tests/test_pending_tracker.py`

**Interfaces:**
- Consumes: `LaunchEvent(tool_use_id, timestamp)` from Task 1.
- Produces: `compute_pending(transcript_path, state_path, stale_seconds=None, now=None, on_stale=None) -> int`. `stale_seconds=None` (the default) means no expiry — identical behavior to before this task. When `stale_seconds` is a number, any unresolved launch whose timestamp is missing/unparseable, or older than `now - stale_seconds` (`now` defaults to `time.time()`), is dropped from the pending count *and* from persisted state; if any were dropped and `on_stale` is given, it's called once with the list of their ids.
- `State.launched` changes type from `set[str]` to `dict[str, str | None]` (id → ISO timestamp or `None`). `State.resolved` is unchanged (`set[str]`).

- [ ] **Step 1: Write the failing tests**

Replace the two format-sensitive assertions in `tests/test_pending_tracker.py` (they currently assume `launched` is a list) and add new tests. Full new content for the file's relevant sections — apply as edits:

```python
def test_state_persists_and_is_chmod_600(tmp_path):
    state_path = str(tmp_path / "s.state.json")
    pt.compute_pending(os.path.join(FIX, "bg_agent_pending.jsonl"), state_path)
    assert os.path.exists(state_path)
    mode = stat.S_IMODE(os.stat(state_path).st_mode)
    assert mode == 0o600
    data = json.loads(open(state_path).read())
    assert data["launched"] == {"toolu_ag1": None}  # fixture has no "timestamp" field
    assert data["resolved"] == []
    assert data["offset"] > 0
```

```python
def test_wrong_shape_state_falls_back(tmp_path):
    for bad_json in ("null", "[]", '"x"', "42"):
        state_path = tmp_path / "wrong_shape.state.json"
        state_path.write_text(bad_json)
        state = pt.load_state(str(state_path))
        assert state.offset == 0
        assert state.launched == {}
        assert state.resolved == set()
```

Append these new tests to the end of the file:

```python
def test_load_state_migrates_legacy_list_format(tmp_path):
    # State files written before staleness support stored "launched" as a
    # bare list of ids with no timestamp. They must load as a dict with an
    # unknown (None) launch time per id, not crash or silently drop data.
    state_path = tmp_path / "legacy.state.json"
    state_path.write_text('{"offset": 10, "launched": ["a", "b"], "resolved": ["a"]}')
    state = pt.load_state(str(state_path))
    assert state.launched == {"a": None, "b": None}
    assert state.resolved == {"a"}
    assert state.offset == 10


def test_staleness_disabled_by_default(tmp_path):
    # No stale_seconds passed → old behavior: never expires, however old.
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2020-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    assert pt.compute_pending(str(src), str(tmp_path / "s.state.json")) == 1


def test_fresh_launch_within_window_still_pending(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    now = pt._parse_iso("2026-01-01T01:00:00.000Z")  # 1h later, inside a 4h window
    pending = pt.compute_pending(str(src), str(tmp_path / "s.state.json"), stale_seconds=14400, now=now)
    assert pending == 1


def test_stale_launch_excluded_and_pruned_from_state(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    state_path = str(tmp_path / "s.state.json")
    now = pt._parse_iso("2026-01-01T05:00:00.000Z")  # 5h later, past a 4h window
    pending = pt.compute_pending(str(src), state_path, stale_seconds=14400, now=now)
    assert pending == 0
    data = json.loads(open(state_path).read())
    assert data["launched"] == {}  # expired entry is pruned, not just skipped


def test_missing_timestamp_treated_as_immediately_stale_when_enabled(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    pending = pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"),
        stale_seconds=14400, now=pt._parse_iso("2026-01-01T00:00:00.000Z"),
    )
    assert pending == 0


def test_on_stale_callback_receives_expired_ids(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    seen = []
    pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=60,
        now=pt._parse_iso("2026-01-01T01:00:00.000Z"), on_stale=seen.extend,
    )
    assert seen == ["a"]


def test_on_stale_not_called_when_nothing_expired(tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
    )
    seen = []
    pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=14400,
        now=pt._parse_iso("2026-01-01T00:00:10.000Z"), on_stale=seen.extend,
    )
    assert seen == []


def test_resolved_launch_never_counted_stale_or_not(tmp_path):
    # A launch that already resolved must not appear in on_stale even if old.
    src = tmp_path / "s.jsonl"
    src.write_text(
        '{"type":"assistant","isSidechain":false,"timestamp":"2020-01-01T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}\n'
        '{"type":"queue-operation","content":"<task-notification>\\n<tool-use-id>a</tool-use-id>\\n</task-notification>"}\n'
    )
    seen = []
    pending = pt.compute_pending(
        str(src), str(tmp_path / "s.state.json"), stale_seconds=60,
        now=pt._parse_iso("2026-01-01T00:00:00.000Z"), on_stale=seen.extend,
    )
    assert pending == 0
    assert seen == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_pending_tracker.py -v`
Expected: FAIL on the new/updated tests — `AttributeError: module 'claude_code_notify.pending_tracker' has no attribute '_parse_iso'`, `TypeError: compute_pending() got an unexpected keyword argument 'stale_seconds'`, and the two updated assertions failing because `launched` is still a list.

- [ ] **Step 3: Write minimal implementation**

Replace the full contents of `claude_code_notify/pending_tracker.py` with:

```python
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


def compute_pending(transcript_path, state_path, stale_seconds=None, now=None, on_stale=None):
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
            state.launched[event.tool_use_id] = event.timestamp
        elif isinstance(event, CompletionEvent):
            state.resolved.add(event.tool_use_id)
    state.offset = new_offset

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pending_tracker.py -v`
Expected: PASS — all tests, including `test_incremental_across_appends` and `test_rotation_triggers_full_rescan`, which don't pass `stale_seconds` and so are unaffected by this task's changes.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/pending_tracker.py tests/test_pending_tracker.py
git commit -m "feat: prune stale unresolved launches from PENDING"
```

---

### Task 3: `config.NOTIFY_PENDING_STALE_SECONDS`

**Files:**
- Modify: `claude_code_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.pending_stale_seconds: int | None`. Default `14400` (4 hours) when unset or unparseable. A configured value `<= 0` becomes `None` (staleness pruning disabled — restores pre-Task-2 "never expire" behavior).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_load_pending_stale_seconds_default(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.pending_stale_seconds == 14400


def test_load_pending_stale_seconds_from_file(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "NOTIFY_PENDING_STALE_SECONDS=60\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.pending_stale_seconds == 60


def test_load_pending_stale_seconds_zero_disables(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "NOTIFY_PENDING_STALE_SECONDS=0\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.pending_stale_seconds is None


def test_env_overrides_pending_stale_seconds(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={"NOTIFY_PENDING_STALE_SECONDS": "30"}, base=tmp_path)
    assert c.pending_stale_seconds == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -k pending_stale -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'pending_stale_seconds'`.

- [ ] **Step 3: Write minimal implementation**

In `claude_code_notify/config.py`, add the field to the dataclass:

```python
@dataclass
class Config:
    bot_token: str
    chat_id: str
    ratelimit_seconds: int
    api_base: str
    debug: bool
    base_dir: Path
    routes: list = field(default_factory=list)
    usage_limit: bool = False
    usage_limit_reset: bool = True
    pending_stale_seconds: int = 14400
```

Add `"NOTIFY_PENDING_STALE_SECONDS"` to the environ-override key tuple in `load()`:

```python
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE",
                "NOTIFY_RATELIMIT_SECONDS", "NOTIFY_DEBUG",
                "NOTIFY_USAGE_LIMIT", "NOTIFY_USAGE_LIMIT_RESET",
                "NOTIFY_PENDING_STALE_SECONDS"):
```

Parse it and wire it into the returned `Config`, right after the existing `ratelimit_seconds` parsing:

```python
    try:
        pending_stale_seconds = int(merged.get("NOTIFY_PENDING_STALE_SECONDS", "14400"))
    except ValueError:
        pending_stale_seconds = 14400
    if pending_stale_seconds <= 0:
        pending_stale_seconds = None
```

```python
    return Config(
        bot_token=token,
        chat_id=chat_id,
        ratelimit_seconds=ratelimit_seconds,
        api_base=merged.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        debug=_truthy(merged.get("NOTIFY_DEBUG", "false")),
        base_dir=base,
        routes=routing.parse_routes(merged),
        usage_limit=_truthy(merged.get("NOTIFY_USAGE_LIMIT", "false")),
        usage_limit_reset=_truthy(merged.get("NOTIFY_USAGE_LIMIT_RESET", "true")),
        pending_stale_seconds=pending_stale_seconds,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/config.py tests/test_config.py
git commit -m "feat: add NOTIFY_PENDING_STALE_SECONDS config knob"
```

---

### Task 4: Wire staleness into `hooks.handle_stop`

**Files:**
- Modify: `claude_code_notify/hooks.py:174-197` (`handle_stop`)
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `compute_pending(transcript, state_path, stale_seconds=config.pending_stale_seconds, now=now, on_stale=callback)` from Task 2; `config.pending_stale_seconds` from Task 3.
- No new public interface — this task only changes `handle_stop`'s internals and its debug-log output.

- [ ] **Step 1: Write the failing tests**

Two existing tests assume a launch with no timestamp stays pending forever (true only when staleness is disabled, which is no longer the production default). Update them to include a `timestamp` close to the mocked "now" — this is a real, deliberate change, not a workaround: production transcript envelopes always carry `timestamp`, so giving the test one keeps it representative.

Replace `test_stop_pending_does_not_send` in `tests/test_hooks.py`:

```python
def test_stop_pending_does_not_send(base, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T01:00:10.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s1", "transcript_path": transcript, "cwd": "/w"}
    rc = hooks.run("stop", json.dumps(payload))
    assert rc == 0
    assert sent == []  # launched 10s ago, well inside the default 4h staleness window
```

Replace `test_debug_log_written_and_scrubbed`:

```python
def test_debug_log_written_and_scrubbed(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nNOTIFY_DEBUG=true\nTELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    fixed_now = hooks._parse_ts("2026-07-11T01:00:10.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T01:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s6", "transcript_path": transcript, "cwd": "/w"}
    hooks.run("stop", json.dumps(payload))
    log = (tmp_path / "debug.log").read_text()
    assert "pending=1" in log
    assert "123:secret" not in log
```

Add two new tests exercising the staleness path end-to-end:

```python
def test_stop_stale_pending_expires_and_sends(base, tmp_path, monkeypatch):
    # A launch older than NOTIFY_PENDING_STALE_SECONDS must stop blocking the
    # "finished" notification — this is the fix for
    # docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md.
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    fixed_now = hooks._parse_ts("2026-07-11T05:00:00.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s_stale", "transcript_path": transcript, "cwd": "/w"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert len(sent) == 1


def test_stop_logs_expired_stale_launches(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\nNOTIFY_DEBUG=true\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\n"
    )
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: None)
    fixed_now = hooks._parse_ts("2026-07-11T05:00:00.000Z")
    monkeypatch.setattr(hooks, "_now", lambda: fixed_now)
    transcript = _write_transcript(tmp_path, [
        '{"type":"assistant","isSidechain":false,"timestamp":"2026-07-11T00:00:00.000Z",'
        '"message":{"content":[{"type":"tool_use","id":"a","name":"Agent","input":{}}]}}',
    ])
    payload = {"session_id": "s_stale2", "transcript_path": transcript, "cwd": "/w"}
    hooks.run("stop", json.dumps(payload))
    log = (tmp_path / "debug.log").read_text()
    assert "expired 1 stale launch" in log
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hooks.py -k "stop_pending_does_not_send or debug_log_written or stop_stale or stop_logs_expired" -v`
Expected: FAIL — the two updated tests fail because `handle_stop` doesn't pass `stale_seconds`/`now` to `compute_pending` yet (so the launch never expires and, for `test_stop_pending_does_not_send`, still correctly stays pending — but check `test_stop_stale_pending_expires_and_sends` and `test_stop_logs_expired_stale_launches`, which are new and must fail: `sent` stays empty / the log lacks "expired").

- [ ] **Step 3: Write minimal implementation**

In `claude_code_notify/hooks.py`, replace `handle_stop`:

```python
def handle_stop(payload, config):
    if _maybe_handle_usage_limit(payload, config):
        return
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"stop cwd={cwd} muted — no send")
        return
    now = _now()
    stale_ids = []
    pending = compute_pending(
        transcript,
        str(cfg.state_path(config.base_dir, session_id)),
        stale_seconds=config.pending_stale_seconds,
        now=now,
        on_stale=stale_ids.extend,
    )
    if stale_ids:
        _debug(config, f"stop session={session_id} expired {len(stale_ids)} stale "
                        f"launch(es) (older than {config.pending_stale_seconds}s): {sorted(stale_ids)}")
    _debug(config, f"stop session={session_id} pending={pending}")
    if pending > 0:
        return
    marker = str(cfg.marker_path(config.base_dir, session_id))
    if not ratelimit.should_send(marker, config.ratelimit_seconds, now):
        _debug(config, f"stop session={session_id} suppressed by rate-limit")
        return
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, now)
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("finished", cwd, _when(), title, duration))
    ratelimit.record_sent(marker, now)
    _debug(config, f"stop session={session_id} notified chat={res.chat_id}")
```

(This also collapses the three previously-separate `_now()` calls into one `now`, computed once, which is what `test_stop_stale_pending_expires_and_sends` and the duration/rate-limit tests rely on being consistent within a single hook invocation.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hooks.py -v`
Expected: PASS, all tests in the file (this includes the duration/rate-limit tests further down that already monkeypatch `hooks._now` — confirm none of them broke from the `_now()` call consolidation).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest`
Expected: PASS, all 245+ tests.

- [ ] **Step 6: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: expire stale pending launches in the Stop hook"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/claude-notify-product-doc.md`, `docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md`

- [ ] **Step 1: `docs/claude-notify-product-doc.md`** — add a new subsection after §4.5 (Dedup / rate-limit, currently ending around line 112):

```markdown
### 4.6 Staleness cutoff

A launched dispatch that never receives a matching `<task-notification>` (crashed shell, killed process, or a Claude Code bug in emitting the notification) would otherwise block `PENDING` — and therefore every future `Stop` notification in that session — forever. `NOTIFY_PENDING_STALE_SECONDS` (default `14400`, i.e. 4 hours) bounds this: any unresolved launch older than that, or with no known launch timestamp at all (including launches recorded by a pre-this-feature state file), is dropped from the pending count and from persisted state the next time `Stop` fires. Set `NOTIFY_PENDING_STALE_SECONDS=0` to disable expiry and restore the original wait-forever behavior. See [lessons learned 0007](lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md) for the incident that motivated this.
```

Also add the key to the `config.env` example block in §5.3 (around line 152-159):

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=8737165697
# optional
NOTIFY_RATELIMIT_SECONDS=120
NOTIFY_PENDING_STALE_SECONDS=14400            # 0 disables — see §4.6
TELEGRAM_API_BASE=https://api.telegram.org   # override for tests / self-hosted
NOTIFY_DEBUG=false                           # set true to enable debug.log for troubleshooting
```

- [ ] **Step 2: `README.md`** — add the same key to the config example block (around line 57-66):

```env
# ~/.claude/claude-code-notify/config.env  (chmod 600)
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=8737165697

# optional
NOTIFY_RATELIMIT_SECONDS=120
NOTIFY_PENDING_STALE_SECONDS=14400
TELEGRAM_API_BASE=https://api.telegram.org
NOTIFY_DEBUG=false
```

Directly below that block, add one sentence:

> A background task that never reports back (e.g. a killed process) stops blocking notifications after `NOTIFY_PENDING_STALE_SECONDS` (default 4h, `0` to disable).

- [ ] **Step 3: `CLAUDE.md`** — extend the existing completion-detection bullet (the first bullet under "Core rules") from:

```
- **Completion detection**: a background dispatch (`Agent`; `Bash` with `run_in_background=true`; or `SendMessage`, which always resumes a spawned agent async) is resolved **only** by a `<task-notification>` matching its `tool_use_id`. An immediate ack `tool_result` never resolves it. `PENDING = launched − resolved`; notify only when `PENDING == 0`. See [docs/lessons-learned/](docs/lessons-learned/).
```

to:

```
- **Completion detection**: a background dispatch (`Agent`; `Bash` with `run_in_background=true`; or `SendMessage`, which always resumes a spawned agent async) is resolved **only** by a `<task-notification>` matching its `tool_use_id`. An immediate ack `tool_result` never resolves it. `PENDING = launched − resolved`; notify only when `PENDING == 0`. Launches older than `NOTIFY_PENDING_STALE_SECONDS` (default 4h) expire out of `PENDING` so one stuck dispatch can't block a session forever. See [docs/lessons-learned/](docs/lessons-learned/).
```

- [ ] **Step 4: `docs/lessons-learned/0007-...md`** — flip the status and record the shipped fix. Change:

```markdown
## Status

Open. Root cause confirmed against a live production session; fix proposed below, not yet implemented.
```

to:

```markdown
## Status

Resolved. Fix shipped in [2026-07-31-pending-launch-staleness-cutoff.md](../superpowers/plans/2026-07-31-pending-launch-staleness-cutoff.md).
```

Change the `## Proposed fix` heading to `## Fix` and, at the end of that section, add:

```markdown
Shipped as designed above, with one refinement discovered during planning: rather than tracking staleness in the launch id alone, `compute_pending()` now takes the ids to prune, and `hooks.handle_stop` logs them via the existing `_debug()` channel (`"expired N stale launch(es) (older than Ns): [...]"`) so a future occurrence is diagnosable from `debug.log` directly.
```

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/claude-notify-product-doc.md docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md
git commit -m "docs: document the pending-launch staleness cutoff, close out 0007"
```

---

## Self-Review Notes

- **Spec coverage:** every element of 0007's "Proposed fix" (timestamp on `LaunchEvent`, dict-shaped `launched` in state, configurable cutoff, debug logging on expiry) has a task. The legacy-list migration (implicit in 0007's point 2, "safe to drop it from `state.launched`") is explicit in Task 2.
- **Placeholder scan:** no TBD/"add error handling"/"similar to Task N" — every step has literal code.
- **Type consistency:** `compute_pending(transcript_path, state_path, stale_seconds=None, now=None, on_stale=None)` (Task 2) matches its call site in Task 4 exactly (keyword args, same names). `Config.pending_stale_seconds` (Task 3) is the exact attribute name read in Task 4. `_parse_iso` (Task 2) is referenced with that exact name in Task 2's own tests — no other task calls it directly.
