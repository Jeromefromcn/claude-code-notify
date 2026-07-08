# claude-code-notify

Standalone, versioned tool that sends Telegram notifications from Claude Code hooks: turn finished, blocked on input, or errored. Full spec: [Doc/claude-notify-product-doc.md](Doc/claude-notify-product-doc.md).

## Status

Pre-implementation. Design is final; no code yet (see doc §12 Decisions).

## Core rules

- **Completion detection**: a background dispatch (`Agent`, or `Bash` with `run_in_background=true`) is resolved **only** by a `<task-notification>` matching its `tool_use_id`. An immediate ack `tool_result` never resolves it. `PENDING = launched − resolved`; notify only when `PENDING == 0`.
- **Parse transcripts at the JSON envelope level** (`type`, `tool_use`/`tool_result` fields). Never substring-match text.
- **Bash shims (`hooks/*.sh`) are thin membranes only** — they read env vars and call `python3 -m claude_code_notify.hooks <event>`. No business logic in shell.
- **Config lives in `~/.claude/claude-code-notify/config.env`** (`chmod 600`), never inlined into `settings.json`, never committed.
- **Installer only merges/removes its own tagged hook entries** in `settings.json` via the `json` module — never `sed`/string surgery. Must be idempotent.
- **Secrets are scrubbed** from any error/log output before display.
- **Core must be testable without a live Claude Code session and without hitting real Telegram.**
- v1 is Telegram-only, global-install-only (`~/.claude/`). Don't add other channels or project-level install without checking the roadmap (doc §11).

## Editing this file

Keep it short and direct: one line per rule, no prose, no restating the product doc — link to it instead. If a rule needs more than 2 lines to explain, it belongs in `Doc/`, not here.
