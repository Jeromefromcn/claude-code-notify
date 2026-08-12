# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.7.0] - 2026-08-12

### Changed
- Reverted part of 0.6.0: every `Stop` where nothing is pending now sends a
  notification again, not only ones that resolved a tracked background task.
  0.6.0 fixed a real problem (misleading "finished" pings on every turn of an
  interactive session) but overcorrected by silencing plain turn-ends
  entirely — which meant walking away mid-conversation, with no background
  task running, produced no notification at all until the whole session
  closed. The message wording now carries the distinction instead: "Claude
  Code finished" when the turn resolved a tracked background launch,
  "Claude Code is waiting for your input" for a plain turn-end (including one
  that only pruned a stale launch). Repeats across closely-spaced turns are
  still collapsed by the existing 120s rate limit — that mechanism, not
  suppression, is what prevents spam. See
  [docs/lessons-learned/0009](docs/lessons-learned/0009-every-stop-should-notify.md).
- `SessionEnd`'s dedup against an already-sent Stop ping now applies to either
  message kind ("finished" or "waiting"), not just "finished" — a normal
  session close right after its last turn's Stop ping no longer double-pings.

### Added
- New `"waiting"` message kind (`Claude Code is waiting for your input`) in
  `notifier.py`.

## [0.6.0] - 2026-08-12

### Changed
- "Claude Code finished" no longer fires on every `Stop` (turn) event. Claude
  Code's `Stop` hook fires at the end of **every** turn, so in an interactive
  multi-turn session the tool was pinging "finished" repeatedly while the
  session was still in use — exactly the "task not done yet" false positives
  reported from a live `docker-gitops` session (PENDING was 0 on every turn
  because nothing was ever launched in the background). "finished" is now
  announced in exactly two situations:
  1. A `Stop` event where a tracked background launch (`Agent`, background
     `Bash`, `SendMessage`) was resolved by a `<task-notification>` that turn
     and nothing is left pending — the "your background task just finished"
     ping, unchanged in intent.
  2. A new `SessionEnd` hook firing with `PENDING == 0` and no "finished"
     already sent for the session — the session's real end (one-shot
     `claude "..."` runs, closing an interactive session). This is the first
     hook wired to Claude Code's `SessionEnd` event.
- A plain turn with no background work (however long or short) stays silent on
  `Stop`. Staleness expiry alone (a launch pruned by `NOTIFY_PENDING_STALE_SECONDS`)
  also stays silent — it unblocks future notifications but is not a completion.
- The `Stop` handler now sends via a shared `_send_finished` helper, and the
  session state file records a `finished_sent` flag so `SessionEnd` dedups
  against a `Stop`-already-announced completion instead of double-pinging.

### Added
- `SessionEnd` hook (`hooks/session_end.sh`) registered by the installer.
- `pending_tracker.compute_pending()` gains an `on_resolved` callback reporting
  launches resolved by a `<task-notification>` in the current parse pass — the
  signal the `Stop` handler uses to distinguish a real completion from a plain
  turn ending. `pending_tracker.finished_sent()` / `mark_finished_sent()`
  persist and read the dedup flag.

### Fixed
- Repeated premature "Claude Code finished" notifications during interactive
  sessions (see Changed above). See
  [docs/lessons-learned/0008](docs/lessons-learned/0008-premature-finished-on-every-turn.md).

## [0.5.0] - 2026-08-02

### Added
- A staleness cutoff for the pending-task tracker. Previously, a single
  background dispatch (`Agent`, `Bash` with `run_in_background=true`, or
  `SendMessage`) that never received a matching `<task-notification>` — a
  crashed shell, a killed process, or a Claude Code bug in emitting the
  notification — left that launch unresolved forever, permanently blocking
  `PENDING` from reaching 0 and silencing every future "finished" notification
  for the rest of that session. `NOTIFY_PENDING_STALE_SECONDS` (default
  `14400`, i.e. 4 hours; `0` disables) now drops any unresolved launch older
  than the threshold — or with no known launch timestamp at all — from the
  pending count and from persisted state on the next `Stop` event. State
  files written before this change (bare list of launched ids, no
  timestamps) are read and migrated to the new shape automatically, so an
  already-stuck session self-heals without manual intervention. See
  [docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md](docs/lessons-learned/0007-unresolved-background-task-blocks-all-future-notifications.md).

## [0.4.1] - 2026-07-24

### Fixed
- A phantom `usage-limit-reset` notification could fire ~a day late for a reset
  that never happened. Resuming a session that had previously hit a usage limit
  and letting the new turn finish normally could, under the transcript write
  race, leave the old rate-limit envelope as the transcript's apparent last
  entry on the plain `Stop` path — re-detecting a resolved limit as a fresh hit.
  `parse_reset` then anchored "next reset time" to *read time* rather than *hit
  time*, rolling the already-past reset forward to a spurious next-day window
  (which also dodged dedup, since the window key folds in the reset date). Two
  fixes: (1) a rate-limit envelope whose timestamp predates the current turn's
  start is treated as a normal completion, not a hit; (2) the reset-window
  computation is anchored to when the limit was actually hit (the envelope's own
  timestamp), so a stale re-read maps to the same window and dedups instead of
  inventing a new one. See
  [docs/lessons-learned/0006-stale-reread-wallclock-anchored-reset.md](docs/lessons-learned/0006-stale-reread-wallclock-anchored-reset.md).

## [0.4.0] - 2026-07-23

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

### Changed
- `StopFailure` usage-limit detection prefers the hook's own payload fields
  (`error`, `last_assistant_message`, `error_details`) over reading the
  transcript: they arrive in the hook's stdin JSON with no file read and no
  race, and a real production event confirmed they carry the same text the
  transcript does. The transcript (with a 0.2s retry) is used only as a
  fallback when the payload itself doesn't classify as a usable rate limit.
  The plain `Stop` path is unaffected. See
  [docs/lessons-learned/0004-stopfailure-payload-is-sufficient.md](docs/lessons-learned/0004-stopfailure-payload-is-sufficient.md).

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
  "stopped with error" notification was sent. Claude Code's own `StopFailure`
  payload already carries a structured `error` field and a
  `last_assistant_message` fallback text, sourced from the hook's stdin JSON
  with no transcript read involved — these are now the primary detection
  source (see "Changed" above), with a 200ms transcript retry as a fallback,
  so a genuine rate limit can no longer be misclassified as a generic error.
  See
  [docs/lessons-learned/0002-stopfailure-transcript-write-race.md](docs/lessons-learned/0002-stopfailure-transcript-write-race.md).
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
  only when the zone name is absent or unresolvable. See
  [docs/lessons-learned/0005-reset-timezone-and-ci-exposed-test-assumptions.md](docs/lessons-learned/0005-reset-timezone-and-ci-exposed-test-assumptions.md).

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
