# Security Policy

## Supported Versions

Only the latest released version is supported with security fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately via one of:

- [GitHub Security Advisories](https://github.com/Jeromefromcn/claude-code-notify/security/advisories/new) (preferred)
- Email the maintainer at jeromefromcn@gmail.com

Include what you found, steps to reproduce, and the potential impact. We aim
to acknowledge reports within 5 business days.

## Scope

This project stores a Telegram bot token in `~/.claude/claude-code-notify/config.env`
and calls the Telegram Bot API. Relevant reports include (but aren't limited to):
token leakage (logs, error output, `settings.json`), unsafe parsing of hook
input or `config.env`, and installer bugs that could write outside the
intended install directory.
