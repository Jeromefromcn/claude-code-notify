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
- Correctly waits for background tasks (`Agent` subagents, background `Bash` commands, and agents resumed via `SendMessage`) before declaring completion.
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
| `SendMessage` | Always — it resumes a previously-spawned agent from its own transcript; there is no `run_in_background` flag to check | **Yes** (a delivery/queued ack) | `<task-notification>` with matching `<tool-use-id>` |

Foreground/synchronous calls always resolve *within* the turn, so they are never pending at Stop time and do not need tracking.

Each `SendMessage` call gets its **own** `tool_use_id`, distinct from the id of the `Agent` call that originally spawned the resumed agent. Resuming an agent is therefore a **new launch** to track, not a re-open of the original one — see [lessons learned 0001](lessons-learned/0001-sendmessage-untracked-background-dispatch.md) for the incident that surfaced this gap.

### 4.2 The unified resolution rule

> A background dispatch is **resolved only** by a `<task-notification>` whose `<tool-use-id>` matches the launch. An immediate ack `tool_result` never resolves it.

This single rule is robust across all three tools:

- Fixes the background-Bash false positive (the ack no longer counts).
- Works for background Agent (which has no ack anyway).
- Works for `SendMessage` (its delivery ack no longer counts either).
- The same underlying task can produce more than one `<task-notification>` over its lifetime — once when an agent first stops, and again each time it's resumed via `SendMessage` and stops again. Each of those is a **separate launch** (a distinct `tool_use_id`: the original `Agent` call, then one per `SendMessage` resume), each resolved independently by its own matching notification.

`PENDING = launched − resolved`. If `PENDING > 0`, the Stop hook exits silently (do not notify — background work is still running). If `PENDING == 0`, proceed to dedup/rate-limit, then send.

### 4.3 Transcript signals parsed

- **Launch:** an `assistant` entry with a `tool_use` content block where `name` is `Agent` (background unless `input.run_in_background == false`), `Bash` with `input.run_in_background == true`, or `SendMessage` (always). Record its `id`.
- **Completion:** a `<task-notification>` block — appears both as a `queue-operation` entry and as a `user` entry with `origin.kind == "task-notification"` — containing `<tool-use-id>…</tool-use-id>`. Record every matched id as resolved.

Parse at the JSON **envelope** level (entry `type`, `tool_use`/`tool_result` structure), never by substring-matching text. Debug output that happens to print the words "tool_use_id" or "task-notification" inside some Bash result text must not poison the count.

**Why not the native `SubagentStop` hook instead of transcript parsing?** It doesn't work for this case. Confirmed via multiple open upstream issues: background agents (`run_in_background=true`) bypass `Stop`/`SubagentStop` entirely ([anthropics/claude-code#25147](https://github.com/anthropics/claude-code/issues/25147)), subagent completion isn't reliably reported ([#33049](https://github.com/anthropics/claude-code/issues/33049)), and even when `SubagentStop` does fire it carries the parent's shared `session_id` with no per-subagent identifier ([#7881](https://github.com/anthropics/claude-code/issues/7881)). Transcript-based `<task-notification>`/`tool_use_id` matching is the only reliable signal available today.

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
| `config.py` | Locate and load config (bot token, chat id, threshold, `NOTIFY_DEBUG`); resolve global vs project. | env/file → config object |
| `hooks.py` | Entry points `stop`, `stop_failure`, `permission_request`; wire the pieces; read Claude Code's hook JSON from stdin; write debug log lines when enabled; never propagate an exception — catch, log, exit 0. | stdin JSON → side effect (notify or not) |

**Bash shims** (`hooks/*.sh`) — thin membranes only. Claude Code delivers all hook data (`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name`, etc.) as a single JSON object on **stdin** — not via env vars. (The only real Claude Code env vars are path placeholders: `$CLAUDE_PROJECT_DIR`, `$CLAUDE_PLUGIN_ROOT`, `$CLAUDE_PLUGIN_DATA`, plus `$CLAUDE_CODE_REMOTE`/`$CLAUDE_EFFORT`, none of which carry session/transcript/tool identity.) Each shim forwards stdin unchanged to `python3 -m claude_code_notify.hooks <event>`. No business logic.

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
NOTIFY_DEBUG=false                           # set true to enable debug.log for troubleshooting
```

File is created `chmod 600`. Because config is separate from code, upgrades replace only code files and never risk touching the user's token. (When project-level install lands, a project `config.env` will override the global one — see §11.)

### 5.3.1 Debug logging

Off by default (`NOTIFY_DEBUG=false`) — zero log writes, zero overhead. When set to `true`, `hooks.py` appends timestamped lines to `~/.claude/claude-code-notify/debug.log` (`chmod 600`) for each hook invocation: event name, parsed payload summary, computed `pending` count, rate-limit decision, and any caught exception. This is the primary troubleshooting path when a user reports a missing or wrongly-timed notification — ask them to set `NOTIFY_DEBUG=true`, reproduce, and share the log. Log content is scrubbed of secrets identically to error output (§9).

### 5.3.2 Directory routing (v0.3.0)

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

### 5.4 Hook integration with settings.json

The installer merges **only** `claude-code-notify`'s own hook entries into the `hooks` block of the target `settings.json`, using Python (`json` module) — never `sed`/string surgery. Which entries are "ours" is tracked by a sidecar state file (`.claude-code-notify-hooks.json`, next to `settings.json`) recording the exact command string written last time, so re-install replaces its own entries idempotently and leaves any other user hooks untouched even if the install path (`base_dir`) changes between runs. A one-time legacy substring match (command path containing `claude-code-notify`) claims pre-existing entries from installs that predate this state file. Uninstall removes exactly those entries. See [ADR 0001](adr/0001-hook-installation-tracking.md).

### 5.5 Usage-limit notifications (v0.4.0)

Opt-in (`NOTIFY_USAGE_LIMIT`, default off). Because a usage limit is
account-global, notifications **broadcast to every distinct destination**
(global default plus every route, deduped by `(bot_token, chat_id)`; mute is
not consulted).

**Detection** is envelope-level only — the transcript's terminal assistant
entry carrying `isApiErrorMessage == true` and `error == "rate_limit"` (both
session and weekly limits), and *not* carrying a structured `errorDetails`
body whose `error.details.error_code == "credits_required"` — Claude Code
reuses `error == "rate_limit"` for per-model usage-credits gates (e.g. Fable 5
without credits enabled) too, which are unrelated to the account's
subscription usage limit; see
[lessons learned 0003](lessons-learned/0003-model-credits-error-misclassified.md).
No text is matched to detect; the reset text is passed through as the message
body and used as an opaque per-window dedup key. When detected, the misleading
normal "finished"/"error" notification is suppressed for that turn.

On the `StopFailure` path, detection prefers the hook's own payload fields
(`error`, `last_assistant_message`, `error_details`) over the transcript —
they arrive in the hook's stdin JSON with no file read and no race, and a
real production event confirmed they carry the same text the transcript
does; see [lessons learned 0004](lessons-learned/0004-stopfailure-payload-is-sufficient.md).
The transcript (which `StopFailure` can fire before finishing flushing to
disk) is read only as a fallback, with one retry after a short delay, when
the payload itself doesn't classify as a usable rate limit — see
[lessons learned 0002](lessons-learned/0002-stopfailure-transcript-write-race.md).
The plain `Stop` path has no such payload fields and always reads the
transcript directly, with no retry.

**Reset ping** (`NOTIFY_USAGE_LIMIT_RESET`, default on when the feature is on;
set false for hit-only, zero background processes). At the reported reset time
a one-shot "usage limit reset" broadcast is delivered by a transient,
single-instance, detached background process ("sleeper") spawned from the hook:
best-effort parse of the reset moment in the timezone named in the reset text
(e.g. `(Asia/Hong_Kong)`) when resolvable, else the host machine's local time,
a wall-clock wait loop capped
at 8 days, no secrets on its argv, a PID file so uninstall can terminate it, and
**no fallback** if it is killed (miss-is-a-miss). The weekly-limit reset text
format is unverified and currently yields no reset ping (the hit broadcast still
fires). The whole feature consumes zero Claude tokens.

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
| `--uninstall` | Remove hook entries, installed code, state, and debug log. Always keeps `config.env` (prints its path for manual deletion) — no prompt, so uninstall stays safe to run non-interactively (e.g. `curl \| bash`). |
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

- **Fixtures:** hand-crafted JSONL snippets under `tests/fixtures/` covering: purely-foreground turn; background Agent still pending; background Agent completed via task-notification; background Bash with immediate ack but no completion (the regression case); background Bash completed; `SendMessage` still pending; `SendMessage` with immediate ack but no completion (the [lessons-learned 0001](lessons-learned/0001-sendmessage-untracked-background-dispatch.md) regression case); `SendMessage` completed; task-notification firing twice; corrupt/rotated state file.
- **Unit tests (pytest):** assert `pending_tracker.compute()` returns the right count for each fixture; assert incremental parsing matches full rescan; assert rate-limit window logic; assert secret scrubbing removes the token from error strings.
- **Notifier tests:** `notifier` sends to `TELEGRAM_API_BASE`; tests point it at a local mock HTTP server (or inject a fake transport) and assert the request payload — no real network.
- **Installer tests:** run `install.sh` against a temp `$HOME`, assert files land in the right place, `config.env` is `600`, settings.json merge adds exactly the expected hook entries and is idempotent on re-run, and `--uninstall` fully reverts.
- **CI:** GitHub Actions runs pytest + shellcheck on `install.sh` for push/PR; README carries a build badge.

## 9. Security considerations

- **Token isolation:** secrets live only in `config.env` (`chmod 600`), never in `settings.json`, never committed.
- **Secret scrubbing:** any error output from a failed Telegram call, and any line written to the optional debug log (§5.3.1), is scrubbed of the bot token before display or write (the token appears in the request URL).
- **hooks.py must not crash the user's turn:** all entry points catch every exception, log it (if `NOTIFY_DEBUG=true`), and exit 0. A bug in this tool must never surface as a blocked or broken Claude Code turn.
- **No secret echo:** interactive prompt for the token does not echo it to the terminal.
- **Least surprise on merge:** installer only ever adds/removes its own hook entries in settings.json, tracked via a sidecar state file rather than path/substring guessing (§5.4, ADR 0001).
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

- **Project-level install** (`--local`): install into `<project>/.claude/` with a project `config.env`. Note: the common "different projects → different bots/chats" need is now met centrally by directory routing (§5.3.2, v0.3.0); per-project install remains optional future work for fully isolated project configs.
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
