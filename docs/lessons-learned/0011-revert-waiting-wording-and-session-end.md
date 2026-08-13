# 0011. Reverting 0.6.0/0.7.0: the "waiting" wording and `SessionEnd` hook weren't earning their complexity

## Status

Resolved. Fix shipped in 0.8.0.

## Summary

After [0010](0010-timeout-promoted-bash-untracked-background-dispatch.md) fixed a real `PENDING`
under-detection bug, the user asked to compare 0.5.0's notification behavior against the current
(0.7.1) behavior directly. Walking through it together surfaced something neither 0008 nor 0009 had
stated explicitly: **the notification cadence never changed across 0.5.0 → 0.7.1.** Every version in
that range sends a ping on every `Stop` where `PENDING == 0` — 0.6.0's attempt to change that cadence
(silence plain turn-ends) was itself reverted by 0.7.0 one release later. The only things that *did*
change across that range were: the message wording (0.7.0 added a "finished" vs "waiting for your
input" split) and a `SessionEnd` hook + `finished_sent` dedup flag (0.6.0), which turned out — once
traced through — to exist solely to patch a gap 0.6.0's own cadence change had created, not a gap
0.5.0 ever had. With 0.8.0's plain "notify on every `Stop` with nothing pending" cadence restored,
neither `SessionEnd` nor the wording split has anything left to do.

## The trace that found this

Reconstructing why `SessionEnd` was added (0.6.0, see [0008](0008-premature-finished-on-every-turn.md)):
0.6.0 restricted `Stop` to only announce "finished" when a tracked background launch actually resolved
that turn. Under that restriction, a one-shot `claude "…"` run — or any turn where nothing was ever
backgrounded — stopped getting a `Stop`-side ping at all. `SessionEnd` was added specifically to cover
that case: "a one-shot `claude '…'` run now pings at `SessionEnd`" (0008's own fix notes).

But pre-0.6.0 (0.5.0 and earlier), `Stop` already sent "finished" unconditionally whenever
`pending == 0` — no `resolved_now` check existed yet. A one-shot run under 0.5.0's logic already got a
`Stop`-side ping; `SessionEnd` would have been redundant from day one. `SessionEnd`'s reason to exist
was created by 0.6.0's own restriction and never predated it. Once 0.7.0 reverted that restriction
(back to "notify unconditionally on `pending == 0`," see [0009](0009-every-stop-should-notify.md)),
`SessionEnd` quietly lost its reason to exist too — but nothing removed it, because 0009's fix was
scoped to the wording/cadence question, not to auditing whether the supporting machinery from 0008 was
still load-bearing afterward.

The wording split ("finished" vs "waiting for your input") has a real motivation — 0008's actual
complaint was that "finished" is misleading when nothing really finished, not that per-turn firing
itself was wrong — but the user's read was that they didn't need the distinction preserved in the
message text specifically; a uniform "finished" on every `Stop` with nothing pending was what they
actually wanted, matching 0.5.0's original wording.

## Why this took three releases to see

0008 and 0009 were each scoped to the specific symptom in front of them (spam, then silence) and each
correctly fixed that symptom. Neither asked the broader question this incident asks: "does the
supporting machinery this fix added (a new hook, a new dedup flag, a new wording branch) still have a
reason to exist once the *next* fix lands?" 0009's revert of 0.6.0's cadence change left 0.6.0's
`SessionEnd` hook and `finished_sent` flag in place by default, because reverting *a* behavior change
is not the same review step as auditing *all* the scaffolding the original change brought with it.

## Fix

- `handle_stop` reverted to unconditional: `if pending > 0: return`, else send "finished" — no
  `resolved_now` tracking, no wording branch.
- `handle_session_end` and the `SessionEnd` hook wiring removed entirely (`hooks/session_end.sh`
  deleted; `_HANDLERS` no longer has a `session_end` entry).
- `pending_tracker`'s `on_resolved` callback and `finished_sent`/`mark_finished_sent` removed — nothing
  reads either anymore.
- `notifier._HEADS` no longer has a `"waiting"` kind.
- `installer.py` gained `_DECOMMISSIONED_EVENTS`: on `merge`, any event listed there has its
  previously-recorded `settings.json` entry actively stripped and dropped from the sidecar state file,
  rather than left wired to a script the new package no longer ships. Verified against a copy of the
  actual `settings.json`/state file this machine had installed from 0.7.1 — confirmed the stale
  `SessionEnd` entry is correctly removed and `Stop` is left untouched (not duplicated). `install.sh`
  separately deletes the orphaned `hooks/session_end.sh` file, since `cp -R` only adds/overwrites, never
  removes a file no longer present in the source package.

## Lesson

**A revert should audit the scaffolding the reverted change brought with it, not just the behavior
line that changed.** 0009 correctly reverted 0.6.0's cadence restriction but left 0.6.0's supporting
infrastructure (`SessionEnd`, `finished_sent`) in place, because "does this still have a reason to
exist" wasn't part of that fix's scope — it was scoped to the wording/silence question in front of it.
The infrastructure kept working (it wasn't buggy), so nothing forced a second look; it just stopped
being *necessary*, which is a much quieter signal than a test failure or a user complaint.

**A design that's been revised twice in the same direction is worth re-deriving from scratch, not
patching a third time.** Comparing 0.5.0 directly against 0.7.1 — rather than reasoning incrementally
from 0.7.1's current state — is what surfaced that the cadence was identical the whole time. Incremental
patches (0.6.0 → 0.7.0) each looked locally justified; only a direct before/after comparison across the
full range showed that two releases of added complexity produced zero net behavior change in the one
dimension (cadence) that mattered, and a wording nuance in the other (labeling) that the user didn't
actually want.

**How to apply this going forward:** when reverting part of a previous change, explicitly ask "what did
that original change add besides the behavior I'm reverting, and does any of it still have a job?"
before considering the revert complete. And when the same area of the codebase gets a second
behavior-changing patch in a row, pause and diff the *current* state against the state *before the
first* patch, not just against the immediately preceding version — that's the comparison that exposes
churn that individual incremental diffs each hide.

## Related

- [0008-premature-finished-on-every-turn.md](0008-premature-finished-on-every-turn.md) — introduced the
  `SessionEnd` hook and the `resolved_now`-gated "finished," motivated by a real spam complaint.
- [0009-every-stop-should-notify.md](0009-every-stop-should-notify.md) — reverted the cadence
  restriction but not the supporting machinery it had motivated.
- `claude_code_notify/hooks.py` — `handle_stop()` (simplified), `handle_session_end` (removed).
- `claude_code_notify/pending_tracker.py` — `on_resolved`, `finished_sent`/`mark_finished_sent` (removed).
- `claude_code_notify/installer.py` — `_DECOMMISSIONED_EVENTS`, `merge_hooks()`.
- `install.sh` — orphaned-shim cleanup.
