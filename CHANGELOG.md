# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- `StopFailure` usage-limit detection now prefers the hook's own payload
  fields (`error`, `last_assistant_message`, `error_details`) over reading
  the transcript, instead of the other way around. A real production event
  confirmed the payload carries the same text the transcript does, race-free
  and without a file read; the transcript (with its existing 0.2s retry) is
  now used only as a fallback when the payload itself doesn't classify as a
  usable rate limit. The plain `Stop` path is unaffected. See
  [docs/lessons-learned/0004-stopfailure-payload-is-sufficient.md](docs/lessons-learned/0004-stopfailure-payload-is-sufficient.md).

### Fixed
- A per-model usage-credits error (e.g. Fable 5 without usage credits
  enabled) was misclassified as an account-level usage limit, because Claude
  Code tags both with the same envelope-level `error == "rate_limit"` field.
  Detection now also checks the structured `errorDetails`/`error_details`
  body and excludes `error_code == "credits_required"`, on both the
  transcript and `StopFailure`-payload paths. See
  [docs/lessons-learned/0003-model-credits-error-misclassified.md](docs/lessons-learned/0003-model-credits-error-misclassified.md).
- The reset-ping sleeper computed the reset time in the host machine's local
  timezone, ignoring the timezone Claude Code embeds in the reset text (e.g.
  `(Asia/Hong_Kong)`). If the host's timezone ever differs from the account's
  reported reset timezone, this silently fired the reset notification at the
  wrong wall-clock time. `parse_reset` now resolves and uses the reported
  timezone via `zoneinfo` when available, falling back to host local time
  only when the zone name is absent or unresolvable.

## [0.4.0] - 2026-07-22

### Added
- Usage-limit notifications (opt-in, off by default). When the account hits a
  usage limit, broadcast a Telegram alert to every distinct configured
  destination (global default plus every route), detected purely at the
  transcript envelope level (`error == "rate_limit"`). Enable with
  `NOTIFY_USAGE_LIMIT=true`.
- Optional reset ping: at the reported reset time, a one-shot notification that
  the limit has reset, delivered by a transient bounded background process.
  Controlled by `NOTIFY_USAGE_LIMIT_RESET` (default `true`; set `false` to keep
  only the hit broadcast and never spawn a background process). Best-effort —
  missed if the machine is off at reset time; weekly-limit reset times are not
  yet parsed. Uninstall terminates any live sleeper.

### Fixed
- `SendMessage` (resuming a previously-spawned background agent) was not
  tracked as a background dispatch, so the `Stop` hook could announce
  "finished" while a resumed agent was still running in the background — the
  same class of false positive as the original background-`Bash` bug, but
  for a tool added after that fix landed. See
  [docs/lessons-learned/0001-sendmessage-untracked-background-dispatch.md](docs/lessons-learned/0001-sendmessage-untracked-background-dispatch.md).
- `StopFailure` can fire before Claude Code finishes writing the terminal
  rate-limit envelope to the transcript (observed gap: ~20ms), so a genuine
  usage-limit hit was read as "not a usage limit" and only the generic
  "stopped with error" notification was sent. Add one bounded retry (200ms)
  to the `StopFailure` detection path only — `Stop` is unaffected. Follow-up:
  Claude Code's own `StopFailure` payload already carries a structured
  `error` field and a `last_assistant_message` fallback text, sourced from
  the hook's stdin JSON with no transcript read involved — use these when
  the transcript is still unavailable after the retry, so a genuine rate
  limit can no longer be misclassified as a generic error even in the worst
  case. See
  [docs/lessons-learned/0002-stopfailure-transcript-write-race.md](docs/lessons-learned/0002-stopfailure-transcript-write-race.md).

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
