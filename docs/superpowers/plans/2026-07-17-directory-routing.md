# Directory-Based Notification Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each notification to a different Telegram destination based on the session's working directory, with subtree inheritance, deeper-directory-wins precedence, and per-subtree muting.

**Architecture:** A new pure `routing.py` module parses `ROUTE_<n>_*` keys from the existing `config.env` into `Route` objects and resolves a `cwd` to a destination by longest directory-prefix match. `config.load()` attaches the parsed routes to `Config`. Each hook handler resolves the destination for its `cwd`; a muted subtree short-circuits (no send), otherwise the handler sends via a `dataclasses.replace`'d `Config` carrying the routed `bot_token`/`chat_id` — leaving `notifier.send`'s two-arg contract (and its secret-scrubbing) unchanged.

**Tech Stack:** Python 3.8+ standard library only (`dataclasses`, `re`, `os.path`). pytest for tests. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-07-17-directory-routing-design.md](../specs/2026-07-17-directory-routing-design.md)

## Global Constraints

Every task's requirements implicitly include these:

- **Python floor `requires-python = ">=3.8"`.** No PEP 604 (`X | None`) or PEP 585 (`list[...]`, `dict[...]`) syntax in **runtime** annotations — they raise at import on 3.8/3.9 and would break the hook. Use `typing.Optional` / `typing.List`, or bare builtins (`list`). Do not use `tomllib` (3.11+).
- **No new runtime dependency** beyond the Python standard library.
- **`hooks.py` never raises or exits non-zero on internal errors** — catch, log if `NOTIFY_DEBUG`, no-op. `parse_routes` and `resolve` must never raise.
- **Never log a bot token** (global or per-route) in debug output; existing secret-scrubbing must stay intact.
- **Routes are read from `config.env` only** — not env-overridable. Global `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` stay **required** and act as the catch-all.
- **Backward compatible:** with no `ROUTE_*` keys, behavior is byte-for-byte identical to today.
- **Tests run without a live Claude Code session and without real Telegram.**
- **All remote-pushed content (code, docs, commits) in English.**

---

### Task 1: `routing.py` — `Route` model and `parse_routes`

**Files:**
- Create: `claude_code_notify/routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: nothing (pure; takes a flat `dict`).
- Produces:
  - `Route` dataclass: `dir: str` (realpath-normalized), `chat_id: Optional[str]`, `bot_token: Optional[str]`, `mute: bool`.
  - `parse_routes(merged) -> list[Route]` — buckets `ROUTE_<n>_(DIR|CHAT_ID|BOT_TOKEN|MUTE)` keys by integer `<n>`; skips groups missing `DIR`, and groups that are neither muted nor have a `CHAT_ID`; de-dupes by normalized `dir` with higher index winning; never raises.
  - Helpers `_truthy(value) -> bool`, `_norm(path) -> str` (realpath, lexical fallback, never raises).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routing.py`:

```python
import os

from claude_code_notify import routing


def test_parse_routes_basic():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/home/me/work",
        "ROUTE_1_CHAT_ID": "111",
    })
    assert len(routes) == 1
    r = routes[0]
    assert r.dir == os.path.realpath("/home/me/work")
    assert r.chat_id == "111"
    assert r.bot_token is None
    assert r.mute is False


def test_parse_routes_bot_token_override_and_mute():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111", "ROUTE_1_BOT_TOKEN": "777:xyz",
        "ROUTE_2_DIR": "/b", "ROUTE_2_MUTE": "true",
    })
    by_dir = {r.dir: r for r in routes}
    a = by_dir[os.path.realpath("/a")]
    b = by_dir[os.path.realpath("/b")]
    assert a.bot_token == "777:xyz"
    assert a.mute is False
    assert b.mute is True
    assert b.chat_id is None


def test_parse_routes_missing_dir_skipped():
    assert routing.parse_routes({"ROUTE_1_CHAT_ID": "111"}) == []


def test_parse_routes_no_chat_and_not_muted_skipped():
    assert routing.parse_routes({"ROUTE_1_DIR": "/a"}) == []


def test_parse_routes_duplicate_dir_last_index_wins():
    routes = routing.parse_routes({
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111",
        "ROUTE_2_DIR": "/a", "ROUTE_2_CHAT_ID": "222",
    })
    assert len(routes) == 1
    assert routes[0].chat_id == "222"


def test_parse_routes_ignores_non_route_and_unknown_keys():
    routes = routing.parse_routes({
        "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "9",
        "ROUTE_1_DIR": "/a", "ROUTE_1_CHAT_ID": "111",
        "ROUTE_1_UNKNOWN": "ignore-me",
    })
    assert len(routes) == 1
    assert routes[0].chat_id == "111"


def test_parse_routes_non_contiguous_indices():
    routes = routing.parse_routes({
        "ROUTE_5_DIR": "/a", "ROUTE_5_CHAT_ID": "111",
        "ROUTE_42_DIR": "/b", "ROUTE_42_CHAT_ID": "222",
    })
    assert {r.chat_id for r in routes} == {"111", "222"}


def test_parse_routes_mute_truthiness():
    for val in ("true", "1", "yes", "on", "TRUE"):
        routes = routing.parse_routes({"ROUTE_1_DIR": "/a", "ROUTE_1_MUTE": val})
        assert routes and routes[0].mute is True
    # A non-truthy MUTE with no CHAT_ID is skipped (not muted, nowhere to send).
    assert routing.parse_routes({"ROUTE_1_DIR": "/a", "ROUTE_1_MUTE": "false"}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_routing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_code_notify.routing'`

- [ ] **Step 3: Write the minimal implementation**

Create `claude_code_notify/routing.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_routing.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/routing.py tests/test_routing.py
git commit -m "feat: parse ROUTE_<n>_* keys into Route objects"
```

---

### Task 2: `routing.py` — `Resolution` and `resolve`

**Files:**
- Modify: `claude_code_notify/routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `Route`, `_norm` from Task 1.
- Produces:
  - `Resolution` dataclass: `muted: bool`, `bot_token: Optional[str]`, `chat_id: Optional[str]`, `matched_dir: Optional[str]`.
  - `resolve(cwd, routes, default_bot_token, default_chat_id) -> Resolution` — longest-prefix, path-segment-aware match; muted route → `muted=True`; no match / empty cwd / any error → global default; never raises.
  - `_matches(cwd, route_dir) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
def _mk(dirpath, chat_id=None, bot_token=None, mute=False):
    return routing.Route(dir=os.path.realpath(dirpath), chat_id=chat_id,
                         bot_token=bot_token, mute=mute)


def test_resolve_exact_match():
    res = routing.resolve("/home/me/work", [_mk("/home/me/work", chat_id="111")],
                          "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "111"
    assert res.bot_token == "G:tok"  # no per-route bot -> global
    assert res.matched_dir == os.path.realpath("/home/me/work")


def test_resolve_subtree_inheritance():
    res = routing.resolve("/home/me/work/proj/sub",
                          [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "111"


def test_resolve_deeper_overrides_shallower():
    routes = [_mk("/home/me/work", chat_id="111"),
              _mk("/home/me/work/acme", chat_id="222")]
    res = routing.resolve("/home/me/work/acme/sub", routes, "G:tok", "999")
    assert res.chat_id == "222"


def test_resolve_segment_boundary_no_false_match():
    res = routing.resolve("/home/me/workspace",
                          [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "999"  # global, not 111
    assert res.matched_dir is None


def test_resolve_no_match_uses_global():
    res = routing.resolve("/tmp/other", [], "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "999"
    assert res.bot_token == "G:tok"
    assert res.matched_dir is None


def test_resolve_muted_subtree():
    res = routing.resolve("/home/me/scratch/x",
                          [_mk("/home/me/scratch", mute=True)], "G:tok", "999")
    assert res.muted is True
    assert res.matched_dir == os.path.realpath("/home/me/scratch")


def test_resolve_muted_parent_normal_deeper_child():
    routes = [_mk("/home/me/scratch", mute=True),
              _mk("/home/me/scratch/keep", chat_id="333")]
    res = routing.resolve("/home/me/scratch/keep/x", routes, "G:tok", "999")
    assert res.muted is False
    assert res.chat_id == "333"


def test_resolve_normal_parent_muted_deeper_child():
    routes = [_mk("/home/me/work", chat_id="111"),
              _mk("/home/me/work/secret", mute=True)]
    res = routing.resolve("/home/me/work/secret/x", routes, "G:tok", "999")
    assert res.muted is True


def test_resolve_per_route_bot_override():
    res = routing.resolve("/home/me/work",
                          [_mk("/home/me/work", chat_id="111", bot_token="777:xyz")],
                          "G:tok", "999")
    assert res.bot_token == "777:xyz"
    assert res.chat_id == "111"


def test_resolve_empty_cwd_uses_global():
    res = routing.resolve("", [_mk("/home/me/work", chat_id="111")], "G:tok", "999")
    assert res.chat_id == "999"
    assert res.matched_dir is None


def test_resolve_normalizes_paths(tmp_path):
    work = tmp_path / "work"
    (work / "proj").mkdir(parents=True)
    routes = [_mk(str(work), chat_id="111")]
    res = routing.resolve(str(work / "proj" / ".."), routes, "G:tok", "999")
    assert res.chat_id == "111"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_routing.py -k resolve -v`
Expected: FAIL — `AttributeError: module 'claude_code_notify.routing' has no attribute 'resolve'`

- [ ] **Step 3: Write the minimal implementation**

Add to `claude_code_notify/routing.py` (after `parse_routes`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_routing.py -v`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/routing.py tests/test_routing.py
git commit -m "feat: resolve cwd to a destination by longest directory prefix"
```

---

### Task 3: `config.py` — attach parsed routes to `Config`

**Files:**
- Modify: `claude_code_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `routing.parse_routes` (Task 1).
- Produces: `Config.routes` — a `list` of `Route`, default empty. `load()` populates it from the merged config dict.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_load_parses_routes(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
        "ROUTE_1_DIR=/home/me/work\nROUTE_1_CHAT_ID=111\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert len(c.routes) == 1
    assert c.routes[0].chat_id == "111"


def test_load_no_routes_gives_empty_list(tmp_path):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
    )
    c = cfg.load(environ={}, base=tmp_path)
    assert c.routes == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -k routes -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'routes'` (or `AttributeError: 'Config' object has no attribute 'routes'`)

- [ ] **Step 3: Write the minimal implementation**

In `claude_code_notify/config.py`:

Change the imports (lines 1-3) from:

```python
import os
from dataclasses import dataclass
from pathlib import Path
```

to:

```python
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import routing
```

Add a `routes` field to `Config` (after `base_dir`):

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
```

In `load()`, change the `return Config(...)` block (currently lines 96-103) to:

```python
    return Config(
        bot_token=token,
        chat_id=chat_id,
        ratelimit_seconds=ratelimit_seconds,
        api_base=merged.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        debug=_truthy(merged.get("NOTIFY_DEBUG", "false")),
        base_dir=base,
        routes=routing.parse_routes(merged),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: PASS (existing config tests + the two new ones)

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/config.py tests/test_config.py
git commit -m "feat: load directory routes into Config"
```

---

### Task 4: `hooks.py` — resolve per-`cwd`, mute short-circuit, routed send

**Files:**
- Modify: `claude_code_notify/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `routing.resolve` (Task 2), `Config.routes` (Task 3), `dataclasses.replace`.
- Produces: no new public API; each handler now sends to a routed `Config`. `notifier.send` stays two-arg — existing doubles remain valid.

**Note on existing tests:** the current doubles `lambda c, t: sent.append(t)` keep working unchanged, because an unrouted `cwd` (e.g. `/w` in existing tests) resolves to the global default, so the replaced `Config` carries the same `bot_token`/`chat_id` and the message text is identical. New tests below capture `c.bot_token`/`c.chat_id` to assert routing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hooks.py`:

```python
def _write_config(tmp_path, routes_block=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:secret\nTELEGRAM_CHAT_ID=999\n"
        "TELEGRAM_API_BASE=http://127.0.0.1:1\n" + routes_block
    )


def test_stop_failure_routes_to_matching_destination(tmp_path, monkeypatch):
    _write_config(tmp_path,
                  "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\nROUTE_1_BOT_TOKEN=777:route\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    captured = []
    monkeypatch.setattr(hooks.notifier, "send",
                        lambda c, t: captured.append((c.bot_token, c.chat_id)))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r1", "transcript_path": transcript, "cwd": "/proj/acme/sub"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert captured == [("777:route", "111")]


def test_stop_failure_muted_route_does_not_send(tmp_path, monkeypatch):
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/scratch\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r2", "transcript_path": transcript, "cwd": "/proj/scratch/x"}
    assert hooks.run("stop_failure", json.dumps(payload)) == 0
    assert sent == []


def test_stop_muted_route_short_circuits(tmp_path, monkeypatch):
    # The Stop path has pending/rate-limit; mute must short-circuit before send.
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/scratch\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(hooks.notifier, "send", lambda c, t: sent.append(t))
    transcript = _write_transcript(tmp_path, [])  # nothing pending
    payload = {"session_id": "r3", "transcript_path": transcript, "cwd": "/proj/scratch/x"}
    assert hooks.run("stop", json.dumps(payload)) == 0
    assert sent == []


def test_permission_request_unmatched_cwd_uses_global(tmp_path, monkeypatch):
    _write_config(tmp_path, "ROUTE_1_DIR=/proj/acme\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    captured = []
    monkeypatch.setattr(hooks.notifier, "send",
                        lambda c, t: captured.append((c.bot_token, c.chat_id)))
    transcript = _write_transcript(tmp_path, [])
    payload = {"session_id": "r4", "transcript_path": transcript, "cwd": "/somewhere/else"}
    assert hooks.run("permission_request", json.dumps(payload)) == 0
    assert captured == [("123:secret", "999")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hooks.py -k "routes or muted or unmatched" -v`
Expected: FAIL — routed tests see the global destination instead of the routed one; muted tests see a message sent (routing not wired yet).

- [ ] **Step 3: Write the minimal implementation**

In `claude_code_notify/hooks.py`:

Add to the standard-library imports at the top (after `import time`):

```python
import dataclasses
```

Add to the package imports (after `from . import config as cfg`):

```python
from . import routing
```

Replace `handle_stop` (currently lines 66-82) with:

```python
def handle_stop(payload, config):
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"stop cwd={cwd} muted — no send")
        return
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
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("finished", cwd, _when(), title, duration))
    ratelimit.record_sent(marker, _now())
    _debug(config, f"stop session={session_id} notified chat={res.chat_id}")
```

Replace `handle_stop_failure` (currently lines 85-91) with:

```python
def handle_stop_failure(payload, config):
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"stop_failure cwd={cwd} muted — no send")
        return
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("error", cwd, _when(), title, duration))
    _debug(config, f"stop_failure notified chat={res.chat_id}")
```

Replace `handle_permission_request` (currently lines 94-100) with:

```python
def handle_permission_request(payload, config):
    cwd = payload.get("cwd", "")
    res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
    if res.muted:
        _debug(config, f"permission_request cwd={cwd} muted — no send")
        return
    transcript = payload.get("transcript_path", "")
    title = latest_ai_title(transcript)
    duration = _turn_duration(transcript, _now())
    dest = dataclasses.replace(config, bot_token=res.bot_token, chat_id=res.chat_id)
    notifier.send(dest, notifier.build_message("needs-input", cwd, _when(), title, duration))
    _debug(config, f"permission_request notified chat={res.chat_id}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hooks.py -v`
Expected: PASS — the four new routing tests **and** all pre-existing hooks tests (unchanged behavior for unrouted `cwd`).

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/hooks.py tests/test_hooks.py
git commit -m "feat: route hook notifications by cwd, honor muted subtrees"
```

---

### Task 5: `--check-route` diagnostic

**Files:**
- Modify: `claude_code_notify/__main__.py`
- Test: `tests/test_check_route.py`

**Interfaces:**
- Consumes: `config.load` (Task 3), `routing.resolve` (Task 2).
- Produces: `python3 -m claude_code_notify --check-route [dir]` prints the winning route, `chat_id`, bot scope (global vs per-route), and muted state; never prints a bot token; returns `1` on config error, `0` otherwise.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_check_route.py`:

```python
from claude_code_notify import __main__ as m


def _write_cfg(tmp_path, extra=""):
    (tmp_path / "config.env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n" + extra
    )


def test_check_route_matched(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={work}\nROUTE_1_CHAT_ID=111\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(work / "sub")]) == 0
    out = capsys.readouterr().out
    assert "chat_id: 111" in out
    assert "global default bot" in out
    assert "123:abc" not in out


def test_check_route_bot_override(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={work}\nROUTE_1_CHAT_ID=111\nROUTE_1_BOT_TOKEN=777:xyz\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(work)]) == 0
    out = capsys.readouterr().out
    assert "per-route override" in out
    assert "777:xyz" not in out


def test_check_route_muted(tmp_path, monkeypatch, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write_cfg(tmp_path, f"ROUTE_1_DIR={scratch}\nROUTE_1_MUTE=true\n")
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(scratch)]) == 0
    assert "MUTED" in capsys.readouterr().out


def test_check_route_no_match_uses_global(tmp_path, monkeypatch, capsys):
    _write_cfg(tmp_path)
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))
    assert m.main(["prog", "--check-route", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "chat_id: 999" in out
    assert "none — using global default" in out


def test_check_route_config_error_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_NOTIFY_HOME", str(tmp_path))  # no config.env
    assert m.main(["prog", "--check-route", str(tmp_path)]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_check_route.py -v`
Expected: FAIL — `--check-route` is unhandled, so `main` prints the banner and the assertions miss.

- [ ] **Step 3: Write the minimal implementation**

Replace the entire contents of `claude_code_notify/__main__.py` with:

```python
import os
import sys

from . import __version__
from . import config as cfg
from . import routing


def _check_route(argv):
    target = None
    if "--check-route" in argv:
        i = argv.index("--check-route")
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            target = argv[i + 1]
    if not target:
        target = os.getcwd()
    try:
        config = cfg.load()
    except cfg.ConfigError as exc:
        print(f"config error: {exc}")
        return 1
    res = routing.resolve(target, config.routes, config.bot_token, config.chat_id)
    print(f"cwd: {os.path.realpath(target)}")
    if res.matched_dir:
        print(f"matched route: {res.matched_dir}")
    else:
        print("matched route: (none — using global default)")
    if res.muted:
        print("result: MUTED — no notification will be sent")
        return 0
    bot_scope = "per-route override" if res.bot_token != config.bot_token else "global default bot"
    print(f"chat_id: {res.chat_id}")
    print(f"bot: {bot_scope}")
    return 0


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    if "--check-route" in argv:
        return _check_route(argv)
    print(f"claude-code-notify {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_check_route.py tests/test_version.py -v`
Expected: PASS (new check-route tests and the pre-existing `--version` tests)

- [ ] **Step 5: Commit**

```bash
git add claude_code_notify/__main__.py tests/test_check_route.py
git commit -m "feat: add --check-route diagnostic for directory routing"
```

---

### Task 6: Version bump to 0.3.0 and documentation

**Files:**
- Modify: `claude_code_notify/__init__.py`, `pyproject.toml`, `tests/test_version.py`
- Modify: `CHANGELOG.md`, `docs/claude-notify-product-doc.md`, `README.md`
- Propose (needs user sign-off): `CLAUDE.md`

**Interfaces:** none (release + docs).

- [ ] **Step 1: Update the version test to the target version (failing)**

In `tests/test_version.py`, change both `"0.2.1"` assertions (lines 12 and 21) to `"0.3.0"`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_version.py -v`
Expected: FAIL — package/pyproject still report `0.2.1`.

- [ ] **Step 3: Bump the version in both source locations**

In `claude_code_notify/__init__.py`, change:

```python
__version__ = "0.2.1"
```

to:

```python
__version__ = "0.3.0"
```

In `pyproject.toml`, change the `version = "0.2.1"` line under `[project]` to `version = "0.3.0"`.

- [ ] **Step 4: Run to verify the version test passes**

Run: `python3 -m pytest tests/test_version.py -v`
Expected: PASS (including `test_pyproject_version_matches_package_version`)

- [ ] **Step 5: Add the CHANGELOG entry**

In `CHANGELOG.md`, insert a new section immediately above `## [0.2.1] - 2026-07-11`:

```markdown
## [0.3.0] - 2026-07-17

### Added
- Directory-based notification routing. `config.env` can now map directories
  to Telegram destinations with `ROUTE_<n>_DIR` / `ROUTE_<n>_CHAT_ID` (plus an
  optional `ROUTE_<n>_BOT_TOKEN` override and `ROUTE_<n>_MUTE=true`). A
  session's working directory is matched by longest directory prefix: a
  configured directory covers its whole subtree, a deeper directory overrides
  a shallower one, and a muted subtree sends nothing. Directories that match
  no route fall back to the global `TELEGRAM_CHAT_ID`, so existing setups are
  unaffected.
- `python3 -m claude_code_notify --check-route [dir]` prints how a directory
  resolves (winning route, chat id, global vs per-route bot, muted) without
  printing any bot token.
```

- [ ] **Step 6: Update the product doc**

In `docs/claude-notify-product-doc.md`, immediately after the `config.env` example block in §5.3 (the fenced `env` block ending with the `NOTIFY_DEBUG=false ...` line), add:

```markdown

#### 5.3.2 Directory routing (v0.3.0)

Optional `ROUTE_<n>_*` keys route notifications to different destinations by
the session's `cwd`:

| Key | Required | Meaning |
|---|---|---|
| `ROUTE_<n>_DIR` | yes | Absolute directory path |
| `ROUTE_<n>_CHAT_ID` | yes, unless muted | Target chat for this subtree |
| `ROUTE_<n>_BOT_TOKEN` | no | Override bot for this subtree; absent → global bot |
| `ROUTE_<n>_MUTE` | no | `true` → suppress notifications for this subtree |

Resolution is longest directory-prefix match over the realpath-normalized
`cwd`: a configured directory covers its whole subtree, a deeper directory
overrides a shallower one, and a muted subtree sends nothing. Any `cwd`
matching no route uses the global `TELEGRAM_CHAT_ID`. Inspect resolution with
`python3 -m claude_code_notify --check-route [dir]`.
```

Then in §11 (Roadmap), replace the first bullet:

```markdown
- **Project-level install** (`--local`): install into `<project>/.claude/` with a project `config.env` that overrides the global one, so different projects can use different bots/chats.
```

with:

```markdown
- **Project-level install** (`--local`): install into `<project>/.claude/` with a project `config.env`. Note: the common "different projects → different bots/chats" need is now met centrally by directory routing (§5.3.2, v0.3.0); per-project install remains optional future work for fully isolated project configs.
```

- [ ] **Step 7: Update the README**

In `README.md`, under the Configuration section (after the documented `config.env` keys / example), add a `### Directory routing` subsection:

````markdown
### Directory routing

Send notifications from different directories to different Telegram chats (and
optionally different bots) by adding indexed `ROUTE_<n>_*` keys to
`config.env`:

```env
# clientA and everything under it -> chat 111
ROUTE_1_DIR=/home/me/work/clientA
ROUTE_1_CHAT_ID=111

# a subtree can use a different bot entirely
ROUTE_2_DIR=/home/me/work/clientB
ROUTE_2_CHAT_ID=222
ROUTE_2_BOT_TOKEN=987654:XYZ

# mute a subtree — no notifications at all
ROUTE_3_DIR=/home/me/scratch
ROUTE_3_MUTE=true
```

A directory covers its whole subtree; the deepest matching directory wins; a
directory matching no route falls back to `TELEGRAM_CHAT_ID`. Check how a path
resolves with:

```bash
python3 -m claude_code_notify --check-route /home/me/work/clientA/sub
```
````

- [ ] **Step 8: Run the full suite and commit the release**

Run: `python3 -m pytest -q`
Expected: PASS (entire suite green)

```bash
git add claude_code_notify/__init__.py pyproject.toml tests/test_version.py \
        CHANGELOG.md docs/claude-notify-product-doc.md README.md
git commit -m "chore: release v0.3.0 — directory-based notification routing"
```

- [ ] **Step 9: Propose the `CLAUDE.md` wording change (STOP for user sign-off)**

Do **not** edit `CLAUDE.md` unilaterally. Present this proposed change to the user and apply only on approval.

Current core-rules line:

```markdown
- v1 is Telegram-only, global-install-only (`~/.claude/`). Don't add other channels or project-level install without checking the roadmap (doc §11).
```

Proposed replacement:

```markdown
- Telegram-only. Global-install-only (`~/.claude/`); per-directory routing (`config.env` `ROUTE_<n>_*`) sends different directory subtrees to different chats/bots — longest-prefix match, deeper dir wins, subtree inherits, mute supported (doc §5.3.2). Don't add other channels or project-level (`--local`) install without checking the roadmap (doc §11).
```

If approved:

```bash
git add CLAUDE.md
git commit -m "docs: note directory routing in CLAUDE.md core rules"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- Config schema `ROUTE_<n>_*` → Task 1 (parse) + Task 6 (docs).
- `parse_routes` (grouping, skips, last-wins, never-raises) → Task 1.
- `resolve` (realpath norm, segment-aware longest-prefix, mute, global fallback, never-raises) → Task 2.
- Path normalization (realpath + lexical fallback) → Task 1 `_norm`, exercised in Task 2.
- `config.py` `routes` field + `load()` wiring → Task 3.
- `hooks.py` per-`cwd` resolve, mute short-circuit before pending/rate-limit, routed `dataclasses.replace` send → Task 4.
- `notifier.py` unchanged; scrubbing via routed config → Task 4 (verified by existing `test_notifier.py`, untouched).
- `--check-route` (winning dir, chat, bot scope, muted, no token printed) → Task 5.
- Backward compat (no routes → identical) → Task 4 (existing tests pass unchanged).
- Versioning v0.3.0 → Task 6.
- Docs (product §5.3/§11, README, CHANGELOG) + CLAUDE.md sign-off → Task 6.

**Placeholder scan:** none — every step carries exact code/commands.

**Type/name consistency:** `Route(dir, chat_id, bot_token, mute)`, `Resolution(muted, bot_token, chat_id, matched_dir)`, `parse_routes(merged)`, `resolve(cwd, routes, default_bot_token, default_chat_id)`, `_norm`, `_matches`, `_truthy`, `Config.routes` — used identically across Tasks 1-6. `notifier.send(config, text)` two-arg contract preserved throughout.

**3.8 safety:** all runtime annotations use `typing.Optional` or bare `list`; no `X | None` / `list[...]`.
