# claude-code-notify

Standalone, versioned tool that sends Telegram notifications from Claude Code hooks: turn finished, blocked on input, or errored. Full spec: [docs/claude-notify-product-doc.md](docs/claude-notify-product-doc.md).

## Status

Pre-implementation. Design is final; no code yet (see doc §12 Decisions).

## Working principles

- Do the right thing, not the easy thing.
- If any existing rule here seems wrong, ask immediately — don't blindly follow it.

## Core rules

- **Completion detection**: a background dispatch (`Agent`, or `Bash` with `run_in_background=true`) is resolved **only** by a `<task-notification>` matching its `tool_use_id`. An immediate ack `tool_result` never resolves it. `PENDING = launched − resolved`; notify only when `PENDING == 0`.
- **Parse transcripts at the JSON envelope level** (`type`, `tool_use`/`tool_result` fields). Never substring-match text.
- **Bash shims (`hooks/*.sh`) are thin membranes only** — forward Claude Code's hook JSON (stdin, not env vars — see doc §5.1) unchanged to `python3 -m claude_code_notify.hooks <event>`. No business logic in shell.
- **hooks.py never raises or exits non-zero on internal errors** — catch, log if `NOTIFY_DEBUG` is on, no-op.
- **Debug logging is off by default**, gated by `NOTIFY_DEBUG` in config.env; writes to `~/.claude/claude-code-notify/debug.log` (chmod 600, secrets scrubbed) only when enabled.
- **Config lives in `~/.claude/claude-code-notify/config.env`** (`chmod 600`), never inlined into `settings.json`, never committed.
- **Installer only merges/removes its own tagged hook entries** in `settings.json` via the `json` module — never `sed`/string surgery. Must be idempotent.
- **Secrets are scrubbed** from any error/log output, including the debug log, before display or write.
- **Core must be testable without a live Claude Code session and without hitting real Telegram.**
- **Credit any external project or reference consulted in README.md's "Related work" section.** No uncredited borrowing of code or ideas.
- v1 is Telegram-only, global-install-only (`~/.claude/`). Don't add other channels or project-level install without checking the roadmap (doc §11).

## Editing this file

Keep it short and direct: one line per rule, no prose, no restating the product doc — link to it instead. If a rule needs more than 2 lines to explain, it belongs in `docs/`, not here.
