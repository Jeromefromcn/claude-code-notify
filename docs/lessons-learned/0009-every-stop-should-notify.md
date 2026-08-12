# 0009. Silencing plain turn-end Stops (0.6.0) traded one false positive for a false negative

## Status

Resolved. Fix shipped in 0.7.0: every `Stop` where nothing is pending now
notifies — "finished" if a background task resolved this turn, "waiting for
your input" otherwise. `SessionEnd` still covers the real session close and
dedups against an already-sent Stop ping for the same idle point.

## Summary

0.6.0 (see [0008](0008-premature-finished-on-every-turn.md)) stopped
announcing "finished" on a plain interactive turn with no background work,
because the Stop hook fires once per turn and was pinging "finished"
repeatedly during an active multi-turn conversation. The fix worked as
designed — and immediately produced the opposite failure: a session doing
ordinary foreground work (reading files, answering a question, no `Agent`/
background `Bash`/`SendMessage` launched) now stayed silent on every `Stop`,
with nothing until the whole process exited (`SessionEnd`). The user reported
finishing a task in a `docker-gitops` session and receiving no notification at
all, even though they had stepped away expecting one — "that's not what I
want; I want a message whenever the session isn't continuing, whether it's a
real end, waiting for my input, or hitting a usage-limit wall."

## Root cause

0.6.0's `handle_stop` treated "a background task resolved" and "the session
isn't continuing" as the same condition:

```python
if pending > 0 or not resolved_now:
    return
```

They aren't the same. `pending > 0` correctly means "background work is still
running — stay silent." But `not resolved_now` conflated "no background task
completed this turn" with "the user doesn't need to know Claude stopped" —
which is false. Every `Stop` hands control back to the user, whether or not a
background task happened to resolve in that turn. The user may have walked
away mid-turn just as easily as mid-background-task; from their side, "Claude
went quiet" looks the same either way, and both deserve a ping.

Verified against real evidence before changing anything (per
`systematic-debugging`): the reporting session's state file showed
`{"launched": {}, "resolved": [], "finished_sent": false}` (no background work
ever tracked), and its debug log showed `stop session=... pending=0` followed
by a silent return — no `notified` line. This confirmed the plain-turn Stop
suppression was firing exactly as 0.6.0 designed it to, not a code defect.

## Why 0.6.0 missed this

§3 of the product doc (as of 0.6.0) modeled "the work is done" and "the
session's real end" as the only two moments worth a ping, with everything else
staying silent to avoid the 0008 spam. That framing answers "did a background
task finish?" but not the actual question the user cares about: "is Claude
still working, or is the ball back in my court?" A `Stop` with `pending == 0`
always means the latter, regardless of what caused it.

## Fix

`handle_stop` now sends whenever `pending == 0`, not only when `resolved_now`
is non-empty:

```python
if pending > 0:
    return
...
kind = "finished" if resolved_now else "waiting"
```

A new `"waiting"` message kind ("Claude Code is waiting for your input")
keeps the notification honest — it never claims "finished" for a turn where
nothing actually completed, which is what made the 0008 pings misleading in
the first place. The existing 120s rate limit (unchanged) — not suppression —
is what keeps rapid back-to-back turns from spamming; turns more than 120s
apart each get their own ping, which is now the intended behavior, not a bug
to fix.

`SessionEnd`'s dedup (`finished_sent` flag) is untouched in mechanism but
now carries a broader meaning: it no longer means "a completion was already
announced," it means "this idle point was already announced" (by either a
"finished" or a "waiting" Stop ping) — so a normal session close right after
its last turn's Stop ping doesn't double-notify.

Separately, while investigating the third case the user named ("hitting a
usage-limit wall"), found `NOTIFY_USAGE_LIMIT` was unset in the live
`config.env` (defaults to `false`) — that path was fully built and tested but
switched off, so a real rate-limit hit produced no notification at all. Turned
it on; unrelated to the Stop/SessionEnd logic above, but the same
investigation surfaced it.

## Lesson

**"Fixes spam" and "fixes the right thing" are not the same claim — check
which one a fix actually makes.** 0.6.0 correctly diagnosed that per-turn
`Stop` firing was wrong to treat as "finished," but overcorrected by silencing
the event entirely instead of just correcting its wording. A per-event hook
firing frequently is a UX/wording problem to solve with rate-limiting and
honest labels, not a reason to suppress the event outright — suppression
trades a false positive for a false negative, and a false negative in a
notification tool (silently no ping) is worse: nothing distinguishes "quiet
because everything's fine" from "quiet because it broke."

## Related

- [0008-premature-finished-on-every-turn.md](0008-premature-finished-on-every-turn.md) — the incident this reverses part of; same `PENDING`/`Stop` model, opposite symptom.
- [0007-unresolved-background-task-blocks-all-future-notifications.md](0007-unresolved-background-task-blocks-all-future-notifications.md) — another case in this series of "silence is not free" failures.
- `claude_code_notify/hooks.py` — `handle_stop()`, `_send_finished()`.
- `claude_code_notify/notifier.py` — `_HEADS["waiting"]`.
