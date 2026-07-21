# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `SendMessage` (resuming a previously-spawned background agent) was not
  tracked as a background dispatch, so the `Stop` hook could announce
  "finished" while a resumed agent was still running in the background — the
  same class of false positive as the original background-`Bash` bug, but
  for a tool added after that fix landed. See
  [docs/lessons-learned/0001-sendmessage-untracked-background-dispatch.md](docs/lessons-learned/0001-sendmessage-untracked-background-dispatch.md).

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

## [0.2.1] - 2026-07-11

### Fixed
- `install.sh` guessed "branch" before "tag" when fetching the release
  tarball (`archive/refs/heads/` then `archive/refs/tags/` as fallback).
  Since the default install path resolves to a release tag, not a branch,
  every ordinary `curl | bash` install hit a guaranteed 404 on the first
  guess before the fallback quietly succeeded. Switched to GitHub's
  `archive/<ref>.tar.gz` endpoint, which resolves branches, tags, and
  commit SHAs uniformly with no guessing.

## [0.2.0] - 2026-07-11

### Added
- Notifications now include how long the turn took (e.g. `3m12s`).

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
