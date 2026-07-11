# Design: turn duration in notifications

## Goal

Include how long the current turn took in every notification message —
`finished`, `error`, and `needs-input` — so the user can tell at a glance
whether Claude Code just ran for 10 seconds or 20 minutes.

## What "duration" means

Elapsed time from the last genuine user-turn-start message in the transcript
to the moment the hook fires (Stop / StopFailure / PermissionRequest). This
matches what a user perceives as "how long did this turn take" in the Claude
Code UI, and includes any background-task wait time folded into the turn.

Confirmed on a live transcript on this machine that every JSONL envelope
carries an ISO-8601 `timestamp` field (e.g. `"2026-07-11T00:58:37.570Z"`),
including `type: "user"` entries — this is not a guess.

## Turn-start detection (`transcript_parser.py`)

New function, same shape and scanning style as the existing
`latest_ai_title(path)`:

```python
def turn_start_timestamp(path):
    """Return the ISO-8601 timestamp of the last genuine user-turn-start
    envelope in the transcript, or None if none is found."""
```

An envelope counts as a genuine turn start when:
- `type == "user"`
- `isSidechain` is falsy
- `message.content` is **not** purely `tool_result` blocks (a background
  task's completion feeding back into the transcript mid-turn must not reset
  the clock — only content that represents new user-provided input counts,
  which includes plain user text and injected context like
  `<ide_opened_file>`)

Scans the whole file (same cost profile as `latest_ai_title` today) and
keeps the last match found. This is a separate function, not merged into
`latest_ai_title` and not wired into `pending_tracker`'s incremental
offset/state — each function stays single-purpose, and the tested
pending-tracking logic is untouched.

## Duration computation & formatting (`hooks.py`)

In each of `handle_stop`, `handle_stop_failure`, `handle_permission_request`,
alongside the existing `title = latest_ai_title(transcript)` call:

```python
start = transcript_parser.turn_start_timestamp(transcript)
duration = _format_duration(_now() - _parse_ts(start)) if start else None
```

`_format_duration(seconds)` renders compactly:
- `< 60s` → `"45s"`
- `< 1h` → `"3m12s"`
- `>= 1h` → `"1h05m"` (no day unit — a multi-day session still renders as
  e.g. `"30h05m"`; not worth the added complexity for a rare case)

Both `_parse_ts` and the surrounding computation are wrapped so that any
failure (malformed timestamp, negative delta from clock skew, etc.) yields
`duration = None` rather than raising — this must not fall through to
`hooks.py`'s outer catch-all, which would drop the *entire* notification.
Losing just the duration field is an acceptable degradation; losing the
whole message is not. A delta of exactly zero is not a failure — it renders
as `"0s"`, same as any other sub-minute duration.

## Message format (`notifier.py`)

```python
def build_message(kind, cwd, when, title=None, duration=None):
```

Field order: `head | duration | title | cwd | when`, joined by `" | "`,
falsy fields filtered out — same convention `title` already uses today.

## Edge cases

- No matching turn-start entry, missing/corrupt transcript, unparsable
  timestamp, or a negative delta → duration omitted, message still sends
  normally with title/cwd/when. A zero delta is not omitted — it renders
  as `"0s"`.
- `needs-input` (PermissionRequest) duration reads as "elapsed so far",
  since the turn hasn't finished yet — this is intentional per the "three
  types get duration" decision.

## Testing

- New/updated fixtures under `tests/fixtures/` carrying realistic
  `timestamp` fields on `user`/`assistant` envelopes (current fixtures omit
  them entirely).
- Unit tests for `turn_start_timestamp()`: picks the last genuine user
  entry; ignores sidechains; ignores tool-result-only entries; returns
  `None` on no-match or missing file.
- Unit tests for `_format_duration()`: boundaries at `0s`, `59s`/`60s`,
  `59m59s`/`1h00m`, and negative → `None`.
- `test_hooks.py`: extend the existing Stop/StopFailure/PermissionRequest
  tests to assert `build_message` receives the expected formatted duration,
  using a fixture with a known timestamp delta (control `_now()` via
  monkeypatch).

## Out of scope

- Configurable duration format/locale.
- Day-granularity formatting.
- Feeding duration back through `pending_tracker`'s incremental state.
