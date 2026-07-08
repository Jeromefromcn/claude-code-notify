# claude-code-notify — Product & Design Document

> **Status:** Draft (v1 design, pre-implementation)
> **License:** MIT
> **Repository / package name:** `claude-code-notify` (Python package `claude_code_notify`)
> **Config file format:** `.env` (`KEY=value`)
> **GitHub owner:** `Jeromefromcn` — repo `https://github.com/Jeromefromcn/claude-code-notify`

---

## 1. Motivation

Claude Code runs long, multi-step turns. A developer who kicks off a task wants to walk away and be pulled back **only** at the two moments that matter:

1. **The turn is truly finished** — every foreground tool call *and* every background task it spawned has completed.
2. **Claude is blocked** — it needs input (a permission decision) or it stopped with an error.

Claude Code exposes `Stop`, `StopFailure`, and `PermissionRequest` hooks that can fire a notification. A naive Stop hook, however, is wrong in a common case: it pings "finished" while a background task the turn launched is still running. It also has no version story — the logic lives as an inline shell string inside a personal `~/.claude/settings.json`, impossible to share, test, or upgrade.

`claude-code-notify` extracts this capability into a standalone, open-source, versioned tool that:

- Sends a Telegram message **only** when a turn genuinely completes, or when Claude needs input, or errors out.
- Correctly waits for background tasks (both `Agent` subagents and background `Bash` commands) before declaring completion.
- Installs in one command with minimal configuration (bot token + chat id).
- Evolves by semantic version, so `install latest` upgrades notification accuracy over time.
- Has a test suite that runs fully decoupled from Claude Code (no live session, no real Telegram required).

## 2. Goals & Non-Goals

### Goals

- One-command install (`curl | bash`) on macOS/Linux; only runtime dependency is `python3`.
- Correct completion detection, including background-task tracking.
- Configuration isolated from `settings.json` internals, so upgrades never touch user secrets or unrelated config.
- Idempotent install = upgrade; re-running the installer updates code and preserves existing config.
- Global install (`~/.claude/`). Project-level install is deferred to a later version (see §11).
- Clean uninstall.
- Fully unit-testable core, decoupled from Claude Code and from the real Telegram API.
- Standard open-source hygiene: LICENSE, README, CHANGELOG, semver, CI, CONTRIBUTING.

### Non-Goals (v1)

- Notification channels other than Telegram. (Notifier is structured so channels *can* be added later; v1 ships Telegram only — YAGNI.)
- Windows native support. curl|bash targets macOS/Linux for v1; Windows is future work.
- User-customizable message templates or localization. Messages are fixed English strings in v1.
- A daemon or long-running process. The tool is invoked by Claude Code hooks and exits immediately.

## 3. Background: how the current hook behaves, and why it's wrong

The existing personal setup wires three hooks in `~/.claude/settings.json`:

| Hook | Fires when | Message |
|---|---|---|
| `Stop` | A turn ends | `Claude Code finished \| <title> \| <cwd> \| <time>` |
| `StopFailure` | A turn ends with an error | `Claude Code stopped with error \| …` |
| `PermissionRequest` | A tool call awaits approval | `Claude Code needs your input \| …` |

The `Stop` hook parses the session transcript (`~/.claude/projects/<slug>/<session-id>.jsonl`) to decide whether background work is still pending, then applies a dedup/rate-limit before sending.

**The correctness bug it needs to fix:** the current logic treats *any* `user` `tool_result` whose `tool_use_id` matches a launched task as "that task resolved." That is correct for a synchronous call, but a **background Bash** command emits an *immediate* ack `tool_result` (`"Command running in background with ID: …"`) the instant it is dispatched — long before it finishes. Matching on that ack marks the background command "resolved" immediately, so the Stop hook can announce "finished" while the command is still running. The current hook also only tracks `Agent` launches, ignoring background `Bash` entirely.

## 4. Pending-task tracking (the core correctness feature)

This is the heart of the tool. It answers one question at Stop time: **are there background tasks this session launched that have not yet completed?**

### 4.1 What counts as a background dispatch

| Tool | Background when | Immediate ack `tool_result`? | Completion signal |
|---|---|---|---|
| `Agent` | `run_in_background != false` (i.e. `true` **or absent** — Agent defaults to background) | No | `<task-notification>` with matching `<tool-use-id>` |
| `Bash` | `run_in_background == true` (Bash defaults to foreground) | **Yes** (`"Command running in background with ID: …"`) | `<task-notification>` with matching `<tool-use-id>` |

Foreground/synchronous calls always resolve *within* the turn, so they are never pending at Stop time and do not need tracking.

### 4.2 The unified resolution rule

> A background dispatch is **resolved only** by a `<task-notification>` whose `<tool-use-id>` matches the launch. An immediate ack `tool_result` never resolves it.

This single rule is robust for both tools:

- Fixes the background-Bash false positive (the ack no longer counts).
- Works for background Agent (which has no ack anyway).
- A `<task-notification>` may fire more than once for the same task (an agent can be resumed). Matching by the stable launch `tool_use_id` makes re-notification idempotent.

`PENDING = launched − resolved`. If `PENDING > 0`, the Stop hook exits silently (do not notify — background work is still running). If `PENDING == 0`, proceed to dedup/rate-limit, then send.

### 4.3 Transcript signals parsed

- **Launch:** an `assistant` entry with a `tool_use` content block where `name` is `Agent` (background unless `input.run_in_background == false`) or `Bash` with `input.run_in_background == true`. Record its `id`.
- **Completion:** a `<task-notification>` block — appears both as a `queue-operation` entry and as a `user` entry with `origin.kind == "task-notification"` — containing `<tool-use-id>…</tool-use-id>`. Record every matched id as resolved.

Parse at the JSON **envelope** level (entry `type`, `tool_use`/`tool_result` structure), never by substring-matching text. Debug output that happens to print the words "tool_use_id" or "task-notification" inside some Bash result text must not poison the count.

### 4.4 Incremental state (performance)

A transcript only grows within a session and past lines never change. Re-parsing the whole file on every Stop is O(file size) per turn. Instead, cache per-session state in `/<state-dir>/<session-id>.state.json`:

```json
{ "offset": <bytes read>, "launched": [<ids>], "resolved": [<ids>] }
```

Each run seeks past `offset` and parses only newly appended lines — O(growth since last Stop). If the cache is missing, corrupt, or the file is shorter than `offset` (rotated/stale), fall back to a full rescan from offset 0.

### 4.5 Dedup / rate-limit

Within one session, `Stop` can fire many times (e.g. rapid follow-up questions). To avoid spamming: keep a marker file per session; if the last notification was sent less than `THRESHOLD` seconds ago (default 120), skip. Otherwise send and update the marker. This is purely anti-spam and independent of completion detection.

## 5. Architecture

### 5.1 Components

**Python core** (`claude_code_notify/`) — all logic, unit-testable in isolation:

| Module | Responsibility | Key inputs / outputs |
|---|---|---|
| `transcript_parser.py` | Read a JSONL transcript incrementally; yield structured launch/completion events. | path + cached offset → events, new offset |
| `pending_tracker.py` | Apply the resolution rule; maintain the per-session state file; compute `PENDING`. | transcript path, state path → `pending: int` |
| `ratelimit.py` | Dedup/rate-limit marker logic. | session id, threshold → `should_send: bool` |
| `notifier.py` | Format and send a Telegram message; scrub secrets from any error output. | message fields, config → send result |
| `config.py` | Locate and load config (bot token, chat id, threshold); resolve global vs project. | env/file → config object |
| `hooks.py` | Entry points `stop`, `stop_failure`, `permission_request`; wire the pieces; read Claude Code env vars. | env → side effect (notify or not) |

**Bash shims** (`hooks/*.sh`) — thin membranes only. Each reads env (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_TOOL_NAME`, `PWD`) and calls `python3 -m claude_code_notify.hooks <event>`. No business logic.

### 5.2 Data flow (Stop event)

```
Claude Code turn ends
  → Stop hook (settings.json) runs hooks/stop.sh
    → python3 -m claude_code_notify.hooks stop
      → config.load()                     # token, chat id, threshold
      → pending = pending_tracker.compute(transcript, state)
      → if pending > 0: exit 0            # background work still running
      → if not ratelimit.should_send():   exit 0
      → notifier.send("Claude Code finished | …")
```

`StopFailure` and `PermissionRequest` skip pending/rate-limit checks and send directly (an error or a block should always notify promptly).

### 5.3 Configuration storage

Config lives in a dedicated file, **never** inlined into `settings.json`. v1 uses a single global location:

- `~/.claude/claude-code-notify/config.env`

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=8737165697
# optional
NOTIFY_RATELIMIT_SECONDS=120
TELEGRAM_API_BASE=https://api.telegram.org   # override for tests / self-hosted
```

File is created `chmod 600`. Because config is separate from code, upgrades replace only code files and never risk touching the user's token. (When project-level install lands, a project `config.env` will override the global one — see §11.)

### 5.4 Hook integration with settings.json

The installer merges **only** `claude-code-notify`'s own hook entries into the `hooks` block of the target `settings.json`, using Python (`json` module) — never `sed`/string surgery. Entries are tagged (e.g. a stable command path under `~/.claude/claude-code-notify/`) so re-install replaces its own entries idempotently and leaves any other user hooks untouched. Uninstall removes exactly those entries.

## 6. Installation & upgrade

### 6.1 One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main/install.sh | bash
```

The installer:

1. Verifies `python3` is available (the only runtime dependency); aborts with a clear message otherwise.
2. Downloads the pinned latest release into `~/.claude/claude-code-notify/`.
3. **Config:** if `config.env` already exists, keep it (this is an *upgrade*) and skip prompts. If not, interactively prompt for bot token and chat id, then write `config.env` (`chmod 600`).
4. Merges its hook entries into the target `settings.json`.
5. Prints the installed version and a one-line test hint.

### 6.2 Flags

| Flag | Effect |
|---|---|
| `--version <tag>` | Pin a specific release instead of latest. |
| `--uninstall` | Remove hook entries and installed code (prompt before deleting `config.env`). |
| `--non-interactive` | Read token/chat id from env (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) for CI/automation. |

### 6.3 Upgrade model

Re-running the install command is the upgrade path: it fetches the newest release, overwrites code, and leaves config and other settings intact. Because completion-detection logic lives entirely in the versioned Python core, "install latest" is exactly how a user gets improved notification accuracy (e.g. the background-Bash fix). `CHANGELOG.md` records what each version corrects.

## 7. Versioning & releases

- **Semantic Versioning** (`MAJOR.MINOR.PATCH`). PATCH = accuracy/bug fixes; MINOR = new opt-in behavior; MAJOR = breaking config/hook changes.
- Git tags drive GitHub Releases; `install.sh` resolves "latest" to the newest release tag.
- `CHANGELOG.md` (Keep a Changelog format) — every entry states the concrete accuracy issue fixed.
- A version constant in the package is printed by the installer and available via `python3 -m claude_code_notify --version`.

## 8. Testing & verification

Design constraint: the core must be verifiable **without** Claude Code and **without** hitting real Telegram.

- **Fixtures:** hand-crafted JSONL snippets under `tests/fixtures/` covering: purely-foreground turn; background Agent still pending; background Agent completed via task-notification; background Bash with immediate ack but no completion (the regression case); background Bash completed; task-notification firing twice; corrupt/rotated state file.
- **Unit tests (pytest):** assert `pending_tracker.compute()` returns the right count for each fixture; assert incremental parsing matches full rescan; assert rate-limit window logic; assert secret scrubbing removes the token from error strings.
- **Notifier tests:** `notifier` sends to `TELEGRAM_API_BASE`; tests point it at a local mock HTTP server (or inject a fake transport) and assert the request payload — no real network.
- **Installer tests:** run `install.sh` against a temp `$HOME`, assert files land in the right place, `config.env` is `600`, settings.json merge adds exactly the expected hook entries and is idempotent on re-run, and `--uninstall` fully reverts.
- **CI:** GitHub Actions runs pytest + shellcheck on `install.sh` for push/PR; README carries a build badge.

## 9. Security considerations

- **Token isolation:** secrets live only in `config.env` (`chmod 600`), never in `settings.json`, never committed.
- **Secret scrubbing:** any error/log output from a failed Telegram call is scrubbed of the bot token before display (the token appears in the request URL).
- **No secret echo:** interactive prompt for the token does not echo it to the terminal.
- **Least surprise on merge:** installer only ever adds/removes its own tagged hook entries in settings.json.
- **Supply-chain honesty:** `curl | bash` is convenient but opaque; README documents the exact steps the script performs and offers a `git clone && ./install.sh` path for users who want to read it first.

## 10. Repository layout

```
claude-code-notify/
├── LICENSE                    # MIT
├── README.md                  # what it is, install, config, uninstall, how it works
├── CHANGELOG.md               # Keep a Changelog; accuracy fixes per version
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── install.sh                 # curl|bash entry; shellcheck-clean
├── pyproject.toml             # package metadata, version, pytest config
├── claude_code_notify/
│   ├── __init__.py            # __version__
│   ├── __main__.py            # --version, diagnostics
│   ├── config.py
│   ├── transcript_parser.py
│   ├── pending_tracker.py
│   ├── ratelimit.py
│   ├── notifier.py
│   └── hooks.py
├── hooks/
│   ├── stop.sh
│   ├── stop_failure.sh
│   └── permission_request.sh
├── tests/
│   ├── fixtures/*.jsonl
│   ├── test_pending_tracker.py
│   ├── test_transcript_parser.py
│   ├── test_ratelimit.py
│   ├── test_notifier.py
│   └── test_install.sh        # or pytest-driven installer test
└── .github/
    ├── workflows/ci.yml
    └── ISSUE_TEMPLATE/
```

## 11. Roadmap (post-v1, explicitly out of scope now)

- **Project-level install** (`--local`): install into `<project>/.claude/` with a project `config.env` that overrides the global one, so different projects can use different bots/chats.
- Additional channels (Slack, Discord, generic webhook) behind the `notifier` interface.
- Configurable message templates and localization.
- Tracking other long-lived background work if Claude Code adds it (e.g. `ScheduleWakeup`, `Monitor`) — evaluate per-signal; a scheduled wake-up is intentional idle, not incomplete work.
- Windows support (PowerShell installer).

## 12. Decisions (all resolved)

1. **Repo/package name** — `claude-code-notify` (package `claude_code_notify`).
2. **Config format** — `.env`.
3. **Project-level install** — deferred to a post-v1 release; v1 is global-only.
4. **GitHub owner** — `Jeromefromcn`; repo `https://github.com/Jeromefromcn/claude-code-notify`.
```
