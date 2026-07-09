# claude-code-notify

Accurate completion, input-needed, and error notifications for [Claude Code](https://claude.com/claude-code) — starts with Telegram, built to support more channels.

> **Status: pre-implementation.** The design below is finalized (see [Doc/claude-notify-product-doc.md](Doc/claude-notify-product-doc.md)); the code has not been written yet. This README describes the tool as designed, not as currently installable.

## Why

Claude Code runs long, multi-step turns and exposes `Stop`, `StopFailure`, and `PermissionRequest` hooks you can wire to a notifier. A naive `Stop` hook gets one case wrong: it announces "finished" while a background task the turn launched — a subagent, or a `Bash` command run with `run_in_background=true` — is still running. That's because a background `Bash` call emits an *immediate* acknowledgment result the instant it's dispatched, long before it actually completes; a hook that treats that ack as "done" fires early.

`claude-code-notify` fixes this and packages it as a versioned, installable tool instead of an unshareable shell snippet buried in `~/.claude/settings.json`.

## What it does

- Sends a Telegram message **only** when a turn genuinely finishes, needs your input, or errors out.
- Correctly waits for background work (both `Agent` subagents and background `Bash`) before declaring completion.
- Installs with one command; the only runtime dependency is `python3`.
- Ships fixes and improvements as new versions — `install latest` upgrades notification accuracy.
- Keeps secrets out of `settings.json` entirely.

## How completion detection works

At `Stop` time, the tool answers: *are there background tasks this session launched that haven't finished?*

A background dispatch — `Agent` (background by default), or `Bash` with `run_in_background=true` — is marked **resolved only** by a `<task-notification>` whose `tool_use_id` matches the launch. An immediate ack `tool_result` never counts as resolution. If any dispatch is still unresolved, the hook stays silent; only once everything has resolved does it check a rate limit and send.

Transcripts are parsed incrementally (cached byte offset per session) and at the JSON envelope level — never by substring-matching text, so debug output that happens to contain the words "tool_use_id" can't produce a false signal.

## Installation (once released)

```bash
curl -fsSL https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main/install.sh | bash
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

## Architecture

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

See [Doc/claude-notify-product-doc.md](Doc/claude-notify-product-doc.md) for the full design, including test strategy and repository layout.

## Related work

Several existing tools notify from Claude Code hooks — [starpipi/claude-code-notify](https://github.com/starpipi/claude-code-notify), [777genius/claude-notifications-go](https://github.com/777genius/claude-notifications-go), [decko/claude-code-notify](https://github.com/decko/claude-code-notify), among others. All of them fire on the `Stop`/`Notification` hook directly, without tracking whether a background `Agent` or background `Bash` dispatch is still running — none of them solve the false-positive this project targets (§3 of the design doc). Reviewing their source also surfaced two corrections folded into this design: Claude Code hook input arrives as JSON on stdin, not env vars, and the native `SubagentStop` hook is unreliable for background agents ([anthropics/claude-code#25147](https://github.com/anthropics/claude-code/issues/25147)) — both documented in the design doc.

## License

[MIT](LICENSE)
