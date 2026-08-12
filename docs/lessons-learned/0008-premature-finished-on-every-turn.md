# 0008. "Claude Code finished" fired on every turn of an interactive session

## Status

Resolved. Fix shipped in 0.6.0: "finished" is announced only when a `Stop` turn
resolves the last pending background task, or when the new `SessionEnd` hook
fires with nothing pending — never on a plain turn-end.

## Summary

The user reported receiving "Claude Code finished" while the task was still
running, "frequently" (translated from Chinese: "最近頻繁收到還沒有完成的任務的消息" —
lately I keep getting messages for tasks that haven't finished). Investigation
of the specific reported message (`Claude Code finished | 15s | 檢視 IDE 警告 |
/home/ubuntu/jerome/docker-gitops | 12/08/2026 14:44:52`) traced it to session
`3294327c-…` in `docker-gitops`.

That session was **interactive**: user messages at 14:41, 14:44, 14:46 local,
three `stop_hook_summary` events (one per turn), and two "finished" pings
(14:44:52 and 14:47:36; the middle turn was suppressed by the 120s rate limit).
The session had **no background dispatches at all**, so `PENDING` was 0 on
every Stop. The Stop hook fired at the end of every turn — which is exactly how
Claude Code behaves — and the tool announced "finished" each time `PENDING ==
0`, i.e. after every message, while the user was still actively working.

This is the mirror image of [0007](0007-unresolved-background-task-blocks-all-future-notifications.md):
that incident was notifications **stopping** forever (one unresolved launch);
this one is notifications **firing too early**, for the simplest possible
reason: the design assumed "a task = a turn", but a Stop event is a per-turn
event and interactive sessions have many turns.

## Evidence (session `3294327c-1489-4506-96b2-2730961526e1`, `docker-gitops`)

- `06:41:15Z` user message ("檢視 IDE 警告" / "view IDE warnings") → turn 1 →
  `stop_hook_summary` at `06:41:50Z` (`hooks/stop.sh`, 851ms). No notification
  (a transient send failure on this turn; no `NOTIFY_DEBUG` trail to pin it).
- `06:44:37Z` user message → turn 2 → `stop_hook_summary` at `06:44:53Z`.
  `PENDING == 0` (state: `{"launched": {}, "resolved": []}`) → "finished" sent
  at 14:44:52 local, duration 15s.
- `06:46:36Z` user message → turn 3 (the session kept going after "finished").
  Its Stop was within 120s of the send, so the rate limit suppressed a second
  ping; a later turn sent another at `06:47:36Z`.
- The session state never contained a single launch — all tool calls were
  foreground (`Read`, `Bash`, `Edit`), so the premature pings were **not** a
  missed-launch detection bug. Every notification today across all sessions had
  `PENDING == 0` at send time.

## Root cause

Claude Code's `Stop` hook fires at the end of **every turn** (user prompt →
Claude's agentic loop → response completes → control returns). It has no
payload field distinguishing "turn ended, session continues" from "session
ending now" (confirmed against the hooks docs; there is no documented
`stop_reason`/`interrupted`). The notify tool's `handle_stop` treated any Stop
with `PENDING == 0` as "finished". In a single-turn task run that is correct,
but in an interactive multi-turn session every turn is a "finished" candidate,
so the user is pinged mid-task. The `PENDING == 0` gate only protects against
background-work-still-running; it says nothing about whether the **session** is
still being used.

## Why the original design missed this

§3 of the product doc models the workflow as "a developer kicks off a task and
walks away" — one user message, one turn, one Stop, `PENDING == 0` means done.
That model holds for `claude "prompt"` one-shots and for a turn that finishes
while the user is away, but it silently equated "turn" with "task". Nothing in
the design accounted for the Stop hook firing per-turn in an interactive
session where the user keeps typing.

## Fix

1. **Stop handler**: only announce "finished" when the turn's newly-parsed
   transcript events include a `<task-notification>` resolving a known launch
   **and** nothing is left pending (`PENDING` → 0 via a real completion).
   `pending_tracker.compute_pending()` gained an `on_resolved` callback that
   reports launches resolved this parse pass, so the handler can tell a real
   completion from a plain turn ending. Staleness expiry alone (0007's prune)
   is deliberately not a completion: it unblocks the session but does not ping.
2. **New `SessionEnd` hook** (`hooks/session_end.sh`, wired by the installer):
   fires once per session when the process exits. It announces "finished" when
   `PENDING == 0` and a `finished_sent` flag (persisted in the per-session state
   file) wasn't already set by a Stop-side completion ping — deduping the
   "background task finished" ping from the later "session closed" ping.

This preserves the walk-away use cases: a one-shot `claude "…"` run now pings at
`SessionEnd`; a background task that completes while the session stays open
still pings at the Stop whose turn resolved it.

## Lesson

**A per-event hook and a per-session notion of "done" are different things.**
`Stop` is a per-turn event, so gating on `PENDING == 0` alone answers "is any
background work still open?" — not "is the session over?". When a notification
tool promises "done", it needs an event that actually means done (`SessionEnd`),
or an explicit rule for what a per-turn event may announce. Also: a 120s
rate-limit masks repeated-per-turn sends during bursts but is **not** a
substitute for correct trigger semantics — turns more than 2 minutes apart still
produced false "finished" pings, which is how the user noticed.

## Related

- [0007-unresolved-background-task-blocks-all-future-notifications.md](0007-unresolved-background-task-blocks-all-future-notifications.md) — the opposite failure mode (notifications never fire), same `PENDING` model.
- [claude-notify-product-doc.md §3](../claude-notify-product-doc.md) — the "a task = a turn" assumption this incident exposes.
- [claude-notify-product-doc.md §5.2](../claude-notify-product-doc.md) — updated Stop/SessionEnd data flow.
- `claude_code_notify/hooks.py` — `handle_stop()`, `handle_session_end()`, `_send_finished()`.
- `claude_code_notify/pending_tracker.py` — `on_resolved`, `finished_sent`/`mark_finished_sent`.
