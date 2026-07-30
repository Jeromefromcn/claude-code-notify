# 0006. A phantom reset ping: a stale limit re-read on the plain `Stop` path, rolled to the wrong day by wall-clock anchoring

## Status

Resolved. Two complementary fixes — one addressing *why a stale limit was re-detected at all* (turn
correlation), one addressing *why the re-detection produced a brand-new future window instead of deduping*
(error-time anchoring). Builds directly on the write race from
[0002](0002-stopfailure-transcript-write-race.md) and the reset-time machinery from
[0005](0005-reset-timezone-and-ci-exposed-test-assumptions.md).

## Summary

A `usage-limit-reset` Telegram notification fired at **2026-07-24 05:20** for a reset that never happened —
nothing was rate-limited at 00:20 that would reset then. Tracing it back through `debug.log` and the
session transcripts showed the ping was the delayed product of a **false hit detected ~24 hours earlier**,
at 2026-07-23 13:28, on a session that had genuinely hit a limit the previous day.

The genuine limit: `PA_Agent` session, `2026-07-22T18:14:18Z` (02:14 HKT 07-23), text
`"You've hit your session limit · resets 5:20am (Asia/Hong_Kong)"`. It arrived via `StopFailure`, was
handled correctly (window `907e8236`, target 07-23 05:20), and its reset ping fired on time at 07-23 05:20 —
the *real* reset the user remembered.

The false hit: at 13:27 HKT the user resumed that same session ("go on"); the turn finished normally at
13:28:39. Its plain `Stop` hook fired at 13:28:40 and re-read the transcript — but the current turn's normal
reply had not yet flushed to disk (the same ~ms write race as [0002](0002-stopfailure-transcript-write-race.md),
here on the `Stop` path rather than `StopFailure`). So the transcript's *last assistant envelope on disk* was
still the 11-hour-old rate-limit line. It was treated as a fresh hit happening *now* (13:28), and
`parse_reset` — anchoring "next 5:20am" to **wall-clock now** — rolled it forward to **07-24** 05:20, since
today's 05:20 had already passed. A sleeper was spawned for that spurious next-day window and slept ~16 hours
before firing the phantom.

```
13:27:45 usage-limit: no rate-limit as last transcript entry   ← a *different* session, correctly None
13:27:46 stop ... notified                                     ← normal "finished" for it
13:28:40 usage-limit hit broadcast to 2 destination(s)         ← the FALSE positive (PA_Agent Stop)
13:28:40 recovery: sleeper spawned window=5054a20c target=1784841600   (= 07-24 05:20)
...
07-24 05:20:00 recovery: target reached window=5054a20c — firing   ← the phantom
```

## Root cause: two independent defects, both masked until they lined up

### Defect A — the plain `Stop` path re-detects a limit that isn't from this turn

`_maybe_handle_usage_limit` on the plain `Stop` path reads `latest_usage_limit(transcript)`, which returns a
reset iff *the transcript's last assistant envelope* is a rate-limit error — **with no check that the error
belongs to the current turn**. Under the write race, a normal turn whose reply hasn't flushed exposes an
older turn's rate-limit line as the apparent tail, so a normal completion is misread as a fresh limit hit.
Real limits arrive via `StopFailure` with a sufficient payload (see
[0004](0004-stopfailure-payload-is-sufficient.md)); the plain-`Stop` transcript scan was defensive
defense-in-depth, and it was catching phantoms more than real hits.

### Defect B — `parse_reset` anchors the day roll-forward to read time, not hit time

`parse_reset(reset_text, now)` computed "the next `5:20am` after `now`". For a genuine hit `now ≈ hit time`,
so this is correct. But for a *stale re-read*, `now` is hours after the hit, so a reset that was in the past
relative to the hit gets rolled forward to the next day — inventing a future window that never existed. This
single mis-anchor caused **two**破绽 at once:

1. **Wrong reset date.** Anchored to the hit time (02:14) instead, "next 5:20am" is 07-23 05:20 — already
   past by real-now 13:28, so `parse_reset`'s own `epoch <= now` guard would have rejected it outright.
2. **Bypassed dedup.** `window_key` folds the target's *date* into the key. The wall-clock roll produced date
   07-24 → key `5054a20c` ≠ the genuine hit's `907e8236` (date 07-23) → the `.hit` marker didn't match → the
   duplicate wasn't suppressed. Anchored to hit time, both compute date 07-23 → same key → the re-read would
   have been deduped as a duplicate of the still-on-disk genuine marker, silently and completely.

## Fix

Two changes, giving three layers of defense (any one of which stops this bug):

- **Turn correlation (Defect A)** — `latest_usage_limit()` now returns `UsageLimit(text, at)`, where `at` is
  the error envelope's own `timestamp` epoch. On the transcript path, if `at` predates the current turn's
  start (`transcript_parser.turn_start_timestamp`), the error is stale — a normal turn merely mid-flush over
  an old limit — and is treated as a normal completion (`return False`). This suppresses the false hit
  entirely, including the immediate "usage limit hit" broadcast.

- **Error-time anchoring (Defect B)** — the reset-window computation is anchored to when the limit was hit,
  not to read time:
  - `reset_epoch(reset_text, anchor)` — new: the first reset strictly after `anchor`, with **no** future/CAP
    gate. This is the window's stable identity; a stale re-read of the same limit maps to the same window.
  - `parse_reset(reset_text, now, anchor=None)` — the *schedulable* reset: rolls forward from `anchor`
    (defaulting to `now`) but still validated against real `now` (`epoch <= now` / `> now + CAP` → None). A
    stale limit whose reset already passed returns None → no sleeper.
  - `hooks` passes `anchor = error_at if error_at is not None else now`, and keys the window off
    `reset_epoch(reset_text, anchor)` so genuine hit and stale re-read collide on one key → dedup.

For genuine fresh hits `error_at ≈ now`, so both new functions reduce to the old behavior exactly — verified
by `test_parse_reset_anchor_defaults_to_now`.

Verified test-first: `test_stop_ignores_stale_rate_limit_predating_current_turn` (Defect A, the exact
resume-after-limit scenario), `test_stop_reread_of_same_limit_maps_to_same_window` (Defect B, the wall-clock
day-roll dedup bypass — red before, green after), plus `reset_epoch`/anchored-`parse_reset` unit tests, all
anchored to an explicit `Asia/Hong_Kong` and re-run under `TZ=UTC`/`America/Los_Angeles`/`Pacific/Kiritimati`
per [0005](0005-reset-timezone-and-ci-exposed-test-assumptions.md)'s lesson.

## Lesson

**A time computation that reads "now" is silently assuming it runs at the moment the event occurred.** Every
place this code used wall-clock `now` was really reaching for "when the limit was hit" — an assumption that
holds on the fast path and quietly breaks the instant a value is *re-read* later. The durable fix wasn't a
freshness threshold (a magic number that only narrows the race window); it was giving the event its own
timestamp and anchoring every derived quantity to *that*, so read time stops mattering at all.

**How to apply this going forward:**
- When code derives a scheduled/future time from "now", ask whether "now" is guaranteed to equal *when the
  triggering event happened*. If the same input can be re-processed later (a transcript re-read, a retry, a
  replayed queue), anchor the derivation to the event's own recorded time, not to processing time.
- A dedup key that folds in a *derived* field (here, the reset date) is only as stable as that derivation.
  If the derivation depends on read time, the key does too — and the dedup you're relying on to be idempotent
  silently isn't. Anchor the key's inputs to the event, not to when you happened to look.
- Defense-in-depth added "just in case" (the plain-`Stop` transcript scan) still needs a correctness
  boundary. "Is the last envelope an error?" is not the same question as "did *this turn* end in an error?" —
  scope the scan to the current turn so it can't resurrect a resolved past one.

## Related

- [`0002-stopfailure-transcript-write-race.md`](0002-stopfailure-transcript-write-race.md) — the transcript
  write race, here manifesting on the `Stop` path (not just `StopFailure`) as the trigger for the stale read.
- [`0004-stopfailure-payload-is-sufficient.md`](0004-stopfailure-payload-is-sufficient.md) — why real hits
  arrive via `StopFailure`'s payload, making the plain-`Stop` transcript scan defensive rather than primary.
- [`0005-reset-timezone-and-ci-exposed-test-assumptions.md`](0005-reset-timezone-and-ci-exposed-test-assumptions.md)
  — the `parse_reset`/`_resolve_tz` machinery this refactors, and the tz-anchoring discipline the new tests follow.
- `claude_code_notify/usagelimit.py` — `UsageLimit`, `_parse_ts`, `_reset_hm`, `reset_epoch`, `parse_reset`.
- `claude_code_notify/hooks.py` — `_maybe_handle_usage_limit` (turn correlation + error-time anchoring).
- `tests/test_hooks.py` — `test_stop_ignores_stale_rate_limit_predating_current_turn`,
  `test_stop_reread_of_same_limit_maps_to_same_window`.
- `tests/test_usagelimit.py` — `test_reset_epoch_anchored_to_hit_time_returns_reset_after_hit`,
  `test_parse_reset_stale_hit_returns_none_when_reset_already_passed`,
  `test_latest_usage_limit_returns_error_timestamp`.
