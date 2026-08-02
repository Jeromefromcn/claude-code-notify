# 0007. One never-resolved background task silently blocks every later "finished" notification in the session

## Status

Resolved. Fix shipped in [2026-07-31-pending-launch-staleness-cutoff.md](../superpowers/plans/2026-07-31-pending-launch-staleness-cutoff.md).

## Summary

The user asked why a session (`lab-environment` project, several recent turns driven with the prompt
"继续下一步") had stopped sending "finished" Telegram notifications. Investigation traced it to a single
backgrounded `Bash` dispatch, launched hours earlier, that has not yet been resolved by a matching
`<task-notification>` — and per [§4.2 of the product doc](../claude-notify-product-doc.md#42-the-unified-resolution-rule),
`PENDING = launched − resolved` gates the notification for **every** `Stop` event in the session, not just
the one that launched the task. As long as that one dispatch stays open, no turn in that session — however
unrelated to it — will ever produce a "finished" ping.

This is not a bug in the completion-detection logic; it is working exactly as specified. The gap is that the
design has no upper bound on how long it will wait, and no visibility into why it's waiting, so from the
user's side the failure mode is indistinguishable from a real bug: notifications just silently stop.

## Evidence (session `e2d47e3b-4e27-45b8-ae5f-a4d0642e292e`, `lab-environment` project)

- `2026-07-30T16:18:05.840Z` — assistant calls `Bash` with `run_in_background=true`
  (`tool_use_id=toolu_019kkuGCuy61sXJZCEF6yQBE`, background id `blmnrk8cf`):
  ```
  until ! pgrep -f "python3 ./scripts/run_eval.py" > /dev/null 2>&1; do sleep 5; done; echo "run_eval.py background process has exited"
  ```
  This is a poller waiting on a separate long-running eval script, launched to run unattended in the
  background while the conversation continued.
- As of this writing (`2026-07-30T18:33Z`, over 2h15m later), `run_eval.py` is **still running**
  (confirmed live via `pgrep`), so the poller hasn't printed its `echo` yet and Claude Code has not emitted
  the `<task-notification>` that would resolve `toolu_019kkuGCuy61sXJZCEF6yQBE`.
- The persisted pending-tracker state (`~/.claude/claude-code-notify/state/e2d47e3b-....state.json`) shows
  19 launched ids and 18 resolved — exactly this one outstanding. Every `Stop` event since `16:18:05.840Z`
  computes `pending=1` and returns silently at [hooks.py:186](../../claude_code_notify/hooks.py#L186),
  regardless of what that turn actually did.
- `NOTIFY_DEBUG` was off in the live config, so there was no log trail explaining the suppression — the only
  way to find the cause was to manually recompute `launched − resolved` from the raw state file and cross-
  reference the transcript by hand.

## Root cause

The design in [§4.2](../claude-notify-product-doc.md#42-the-unified-resolution-rule) is correct as far as it
goes — an immediate ack must never be mistaken for completion, and any dispatch the assistant is still
waiting on should suppress a premature "done" message. But it implicitly assumes every launched dispatch
*will* eventually produce a matching `<task-notification>` in a reasonable time. Nothing in
`pending_tracker.py` records *when* a task was launched, only *that* it was — `state.launched` is a bare set
of ids ([pending_tracker.py:11](../../claude_code_notify/pending_tracker.py#L11)). So there is no way to
distinguish "a background task that will resolve in 30 more seconds" from "a background task that, for
whatever reason (crashed shell, killed process, a Claude Code bug in emitting the notification, or — as
here — a script that will legitimately run for many hours), will never resolve in this session again." Both
look identical to `compute_pending()`: one unresolved id, `pending > 0`, suppress forever.

## Why the original design missed this

[§4.2](../claude-notify-product-doc.md#42-the-unified-resolution-rule) and
[§11's roadmap note](../claude-notify-product-doc.md#11-roadmap-post-v1-explicitly-out-of-scope-now) both
show the author was aware that *some* background work is intentionally long-lived ("a scheduled wake-up is
intentional idle, not incomplete work") — but that awareness was applied to future tools
(`ScheduleWakeup`/`Monitor`), not to the dispatches already tracked today. A long-running background `Bash`
poller hits the same case through a tool that was already in scope for v1, so it fell through a gap between
"resolved" and "explicitly out of scope."

## Fix

Add a staleness cutoff to pending-task tracking, so an old unresolved launch stops counting against
`PENDING` instead of blocking indefinitely:

1. Extend `LaunchEvent` (`transcript_parser.py`) to carry the envelope's `timestamp` alongside the
   `tool_use_id`, and persist `{tool_use_id: launch_timestamp}` in `state.json` instead of a bare id set for
   `launched` (`resolved` can stay a bare set — nothing needs its age).
2. In `compute_pending()`, treat any launched id older than a new `NOTIFY_PENDING_STALE_SECONDS` config knob
   (proposed default: a few hours — long enough that it never fires on genuinely-fast background agents, short
   enough that a stuck session recovers same-day) as expired: drop it from the pending count. It's safe to
   drop it from `state.launched` entirely once expired — if a `<task-notification>` for it arrives later
   anyway, `resolved` simply gains an id with no matching `launched` entry, which is already a harmless no-op
   (the same shape as an orphaned completion event, see
   [0001](0001-sendmessage-untracked-background-dispatch.md)).
3. Log the expiry via the existing `_debug()` call in `handle_stop` (e.g.
   `"stop session=... pending=0 (1 stale launch expired after Nh, id=...)"`) so a future occurrence is
   diagnosable from `debug.log` directly, without needing to hand-recompute set differences from the raw
   state file the way this investigation did.

Shipped as designed above, with one refinement discovered during planning: rather than tracking staleness in the launch id alone, `compute_pending()` now takes the ids to prune, and `hooks.handle_stop` logs them via the existing `_debug()` channel (`"expired N stale launch(es) (older than Ns): [...]"`) so a future occurrence is diagnosable from `debug.log` directly.

Not proposed: changing `PENDING` to only consider tasks launched in the *current* turn. That would silence
the suppression faster but throws away the actual intent of §4.2 — telling the user "not really done, X is
still running" for background work started a turn or two ago is a feature, not the bug. The staleness cutoff
preserves that for the normal case (minutes to low hours) and only gives up on dispatches that have gone on
long enough to be presumptively abandoned or lost.

## Lesson

**A "wait until resolved" design needs an answer to "resolved, or given up waiting?" — not just "resolved,
or not yet."** `PENDING = launched − resolved` is a correct model of "is anything still open," but treated
alone it has no failure mode for "will never close." Any tracker that blocks a user-visible action on an
async signal from an external system (here, Claude Code's own `<task-notification>` emission) should carry
an expiry, because the tracker cannot distinguish "still working" from "signal lost" — and in a
notification tool specifically, "silently never fires again" is the worst of the available failure modes:
worse than a late notification, worse than a false-positive one, because it gives the user no signal at all
that something is wrong.

**How to apply this going forward:** any future addition to the pending-tracking model (new background-
dispatch tool, `ScheduleWakeup`/`Monitor` per the §11 roadmap note) should ask not just "what launches it and
what resolves it" but "what's the maximum reasonable time between launch and resolution, and what happens if
that's exceeded" — the same question this incident answers retroactively for `Agent`/`Bash`/`SendMessage`.

## Related

- [claude-notify-product-doc.md §4.2](../claude-notify-product-doc.md#42-the-unified-resolution-rule) — the
  resolution rule this incident doesn't violate, but exposes a gap in.
- [claude-notify-product-doc.md §11](../claude-notify-product-doc.md#11-roadmap-post-v1-explicitly-out-of-scope-now)
  — prior acknowledgment that some background work is intentionally long-lived, applied here to already-in-scope tools.
- [0001-sendmessage-untracked-background-dispatch.md](0001-sendmessage-untracked-background-dispatch.md) — the
  opposite failure mode (an untracked launch caused a *premature* notification); this incident is a launch
  that's tracked correctly but never expires.
- `claude_code_notify/pending_tracker.py` — `compute_pending()`, `State.launched`.
- `claude_code_notify/hooks.py` — `handle_stop()`.
