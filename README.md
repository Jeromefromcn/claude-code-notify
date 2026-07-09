# claude-code-notify

Accurate completion, input-needed, and error notifications for [Claude Code](https://claude.com/claude-code) — starts with Telegram, built to support more channels.

See [docs/claude-notify-product-doc.md](docs/claude-notify-product-doc.md) for the full design and rationale.

## Why

Claude Code runs long, multi-step turns and exposes `Stop`, `StopFailure`, and `PermissionRequest` hooks you can wire to a notifier. A naive `Stop` hook gets one case wrong: it announces "finished" while a background task the turn launched — a subagent, or a `Bash` command run with `run_in_background=true` — is still running. That's because a background `Bash` call emits an *immediate* acknowledgment result the instant it's dispatched, long before it actually completes; a hook that treats that ack as "done" fires early.

`claude-code-notify` fixes this and packages it as a versioned, installable tool instead of an unshareable shell snippet buried in `~/.claude/settings.json`.

## What it does

- Sends a Telegram message **only** when a turn genuinely finishes, needs your input, or errors out.
- Correctly waits for background work (both `Agent` subagents and background `Bash`) before declaring completion.
- Installs with one command; the only runtime dependency is `python3`.
- Ships fixes and improvements as new versions — `install latest` upgrades notification accuracy.
- Keeps secrets out of `settings.json` entirely.

## Installation

One command:

```bash
curl -fsSL https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main/install.sh | bash
```

Or clone and run locally:

```bash
git clone https://github.com/Jeromefromcn/claude-code-notify.git
cd claude-code-notify
./install.sh
```

The installer verifies `python3` is present, downloads the pinned latest release into `~/.claude/claude-code-notify/`, prompts once for your Telegram bot token and chat id (skipped on upgrade — your existing config is kept), and merges its own hook entries into `settings.json` without touching anything else you've configured.

| Flag | Effect |
|---|---|
| `--version <tag>` | Install a specific release instead of latest. |
| `--uninstall` | Remove hook entries and installed code (prompts before deleting `config.env`). |
| `--non-interactive` | Read `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from env — for CI/automation. |

Re-running the install command is the upgrade path: it overwrites code and leaves your config and other hooks untouched.

## Configuration

Config lives in its own file, never inside `settings.json`:

```env
# ~/.claude/claude-code-notify/config.env  (chmod 600)
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=8737165697

# optional
NOTIFY_RATELIMIT_SECONDS=120
TELEGRAM_API_BASE=https://api.telegram.org
NOTIFY_DEBUG=false
```

## Uninstall

```bash
./install.sh --uninstall
```

(or re-run the one-command installer with the same flag if you no longer have a local clone). This removes the tool's tagged hook entries from `settings.json` and the installed code under `~/.claude/claude-code-notify/`, prompting before it also deletes `config.env` (so you can keep your bot token if you plan to reinstall).

## How it works

At `Stop` time, the tool answers: *are there background tasks this session launched that haven't finished?*

A background dispatch — `Agent` (background by default), or `Bash` with `run_in_background=true` — is marked **resolved only** by a `<task-notification>` whose `tool_use_id` matches the launch. An immediate ack `tool_result` never counts as resolution — this is the fix for the background-Bash false positive: a background `Bash` command emits an *immediate* ack `tool_result` (`"Command running in background with ID: …"`) the instant it's dispatched, long before it actually finishes, and a naive hook that treats that ack as "done" fires early. If any dispatch is still unresolved, the hook stays silent; only once everything has resolved does it check a rate limit and send.

Transcripts are parsed incrementally (cached byte offset per session) and at the JSON envelope level — never by substring-matching text, so debug output that happens to contain the words "tool_use_id" can't produce a false signal.

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

`StopFailure` and `PermissionRequest` skip the pending/rate-limit checks and notify immediately — an error or a block should always be reported promptly.

Bash shims under `hooks/*.sh` are thin membranes: Claude Code delivers all hook data as JSON on stdin (not env vars — the only real Claude Code env vars are path placeholders like `$CLAUDE_PROJECT_DIR`), so shims just forward stdin unchanged to the Python core (`claude_code_notify/`), which holds all logic and is unit-testable without a live session or a real Telegram API. `hooks.py` never lets an internal error escape — every entry point catches, optionally logs (see below), and exits 0, so a bug here can never block your Claude Code turn.

### Troubleshooting

Notifications not firing as expected? Set `NOTIFY_DEBUG=true` in `config.env` (default `false`, zero overhead) and reproduce — `hooks.py` will append timestamped, secret-scrubbed detail to `~/.claude/claude-code-notify/debug.log` (`chmod 600`) for every hook invocation: event, parsed payload summary, computed pending count, and rate-limit decision.

## Security

- The bot token lives only in `config.env` (`chmod 600`) and is never written to `settings.json` or committed.
- Any error output from a failed Telegram call is scrubbed of the token before being shown.
- The interactive token prompt doesn't echo input.
- The installer only ever adds or removes its own tagged entries in `settings.json`.

## Roadmap

Out of scope for v1, planned for later:

- Project-level install (`--local`), so different projects can use different bots/chats.
- Additional notification channels (Slack, Discord, generic webhook) behind the existing notifier interface.
- Configurable message templates and localization.
- Windows support.

See [docs/claude-notify-product-doc.md](docs/claude-notify-product-doc.md) for the full design, including test strategy and repository layout.

## Related work

This project generalizes a personal `Stop`/`StopFailure`/`PermissionRequest` hook setup that lived as an inline shell snippet in one developer's own `~/.claude/settings.json` — untested, unversioned, and un-shareable. That setup's transcript-parsing approach is the starting point for `pending_tracker.py`/`transcript_parser.py` here, but it had a correctness bug this project fixes: it resolved a background dispatch on *any* matching `tool_use_id`, including a background `Bash` command's immediate "running in background" ack, so it could announce "finished" while the command was still running (see "How it works" above, and design doc §3).

Several other existing tools notify from Claude Code hooks — [starpipi/claude-code-notify](https://github.com/starpipi/claude-code-notify), [777genius/claude-notifications-go](https://github.com/777genius/claude-notifications-go), [decko/claude-code-notify](https://github.com/decko/claude-code-notify), among others. All of them fire on the `Stop`/`Notification` hook directly, without tracking whether a background `Agent` or background `Bash` dispatch is still running — none of them solve the false-positive this project targets (design doc §3). Reviewing their source also confirmed that Claude Code hook input arrives as JSON on stdin, not env vars.

The decision to use transcript-based `<task-notification>`/`tool_use_id` matching instead of the native `SubagentStop` hook is backed by several open upstream issues showing `SubagentStop` is unreliable for this case: background agents (`run_in_background=true`) bypass `Stop`/`SubagentStop` entirely ([anthropics/claude-code#25147](https://github.com/anthropics/claude-code/issues/25147)), subagent completion isn't reliably reported ([anthropics/claude-code#33049](https://github.com/anthropics/claude-code/issues/33049)), and even when `SubagentStop` does fire it carries the parent's shared `session_id` with no per-subagent identifier ([anthropics/claude-code#7881](https://github.com/anthropics/claude-code/issues/7881)). See design doc §4.3 for the full context.

## License

[MIT](LICENSE)
