# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.2] - 2026-07-10

### Fixed
- `install.sh` referenced `BASH_SOURCE[0]` to detect "running from a local
  checkout," which is unset (and fatal under `set -u`) when the script runs
  the real `curl | bash` way, piped through stdin rather than executed as a
  file. Because a failing command substitution embedded in an argument
  doesn't trigger `set -e`, this silently fell back to treating the caller's
  current directory as the checkout root — so running the documented
  one-liner from inside a directory that happened to contain a
  `claude_code_notify/` folder (e.g. a clone of this repo) would silently
  copy those local files instead of downloading and verifying the real
  release tarball. Now only derives the local-checkout path from
  `BASH_SOURCE` when it points at a real file; otherwise always downloads.

## [0.1.1] - 2026-07-10

### Added
- CI now also runs the test matrix on `macos-latest`, not just `ubuntu-latest`.
- Test coverage for `install.sh`'s tarball-download path (todo.md issue 9),
  via a `file://`-served fixture tarball and a new test-only
  `CLAUDE_NOTIFY_TARBALL_BASE` override.
- Test guarding against `pyproject.toml`'s version and
  `claude_code_notify.__version__` drifting apart.

### Changed
- README version badge is now dynamic (tracks the latest GitHub Release)
  instead of a hardcoded string.

## [0.1.0] - 2026-07-09

### Added
- Initial release. Telegram notifications from Claude Code `Stop`,
  `StopFailure`, and `PermissionRequest` hooks.
- Correct completion detection: a background `Agent` or `Bash`
  (`run_in_background=true`) dispatch is resolved only by a
  `<task-notification>` matching its `tool_use_id`. The immediate
  "Command running in background" ack no longer causes a false "finished".
- Incremental transcript parsing with per-session state cache.
- Rate-limit/dedup marker (default 120s) to avoid Stop-storm spam.
- Idempotent `install.sh` with `--uninstall`, `--non-interactive`,
  `--version`; config isolated in `config.env` (chmod 600).
- Optional debug logging via `NOTIFY_DEBUG`.
