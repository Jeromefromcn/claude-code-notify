# 0010. A timeout-auto-promoted `Bash` background dispatch was untracked — a false "waiting for your input" notification

## Status

Resolved. Fix shipped in the commit that added this document.

## Summary

The user received a Telegram notification: `Claude Code is waiting for your input | 2m53s | Execute K3s phase D
Vikunja stack migration | /home/ubuntu/jerome/docker-gitops | 12/08/2026 22:59:46`. The session was not actually
waiting on the user — an `argocd app sync root` command had been running past its 120s Bash timeout and was
auto-promoted to a background task by Claude Code itself; the assistant's own turn-ending text said exactly that
("The `argocd app sync root` call is taking longer than 120s and moved to background; I'll wait for it to finish
rather than poll."). The task resolved on its own less than a minute later, with no user input involved at any
point. Root cause: `transcript_parser._launch_ids()` only recognized a `Bash` call as a background dispatch via
the *assistant's* `tool_use` envelope, gated on `input.run_in_background == true`. A command that starts in the
foreground and is later auto-promoted after its timeout never has that flag set at all — the dispatch was
invisible to `PENDING` tracking, so `Stop` saw `pending=0` and fired the "waiting" ping immediately.

This is the same *class* of bug as [0001](0001-sendmessage-untracked-background-dispatch.md) (an async dispatch
shape not recognized as a launch) — the third occurrence of it, which that document's "Lesson" section explicitly
flagged as the point to "prompt reconsidering the architecture." The fix taken here does that: it stops keying
launch detection off tool-specific, launch-time declared intent and instead detects the *shape* — a structural ack
field common to every way a `Bash` call can end up backgrounded.

## Evidence (session `d68a44c1-3edf-4840-84d1-a4fafa260386`, `docker-gitops` project)

- `2026-08-12T14:59:34.575Z` (approx.) — assistant calls `Bash` (`tool_use_id=toolu_015ar8ZgU1kHkVb6EL2cEz82`):
  ```json
  {"type":"tool_use","name":"Bash","input":{"command":"argocd app sync root 2>&1 | tail -15","description":"Sync ArgoCD root to create apprise Application"}}
  ```
  No `run_in_background` key anywhere in `input` — this was dispatched as an ordinary foreground command.
- `2026-08-12T14:59:36.376Z` — the tool_result ack arrives:
  ```
  Command did not complete within its 120s timeout and was moved to the background (ID: bmu0t4mno).
  Output is being written to: .../bmu0t4mno.output. You will be notified when it completes.
  ```
  with `toolUseResult: {"stdout":"","stderr":"","interrupted":false,"backgroundTaskId":"bmu0t4mno","timedOutAfterMs":120000}`
  on the envelope. Structurally, this carries the same `backgroundTaskId` field an *explicit*
  `run_in_background=true` Bash ack carries (confirmed against a separate real transcript,
  `e63071bb-.../docker-gitops`, `toolUseResult: {"backgroundTaskId":"b2bon6orh"}`) — the only difference is how the
  command got there.
- `2026-08-12T14:59:45.957Z` — the assistant's turn ends (`stop_reason: "end_turn"`) with the text: *"The `argocd
  app sync root` call is taking longer than 120s and moved to background; I'll wait for it to finish rather than
  poll."* This is the point `Stop` fired and, under the old code, sent the false "waiting for your input" ping
  (timestamped `22:59:46` local time in the user's report).
- `2026-08-12T15:00:20.898Z` — a genuine `<task-notification>` arrives with `<tool-use-id>toolu_015ar8ZgU1kHkVb6EL2cEz82</tool-use-id>`
  and `<status>completed</status>` — proving the dispatch was real background work that resolved on its own, no
  user input ever involved.

## Root cause

`_launch_ids()` classified a `Bash` call as tracked only by inspecting the *assistant's declared intent at call
time*:

```python
elif name == "Bash" and run_bg is True:
    yield tool_id
```

But Claude Code has two independent triggers for a `Bash` command ending up backgrounded: the model asking for it
up front (`run_in_background: true`), or the harness auto-promoting a plain foreground command that's still
running when its 120s timeout elapses. Only the first sets the `input` flag the code checked; the second is a
harness-side decision made *after* the tool call was already dispatched, so there is no `input` field that could
ever reflect it. The assistant-side check was structurally incapable of seeing the auto-promoted case, no matter
how the condition was tuned — the signal it needed doesn't exist at that envelope.

## Why the original design missed this

[§4.1 of the product doc](../claude-notify-product-doc.md#41-what-counts-as-a-background-dispatch) enumerated
`Bash`'s background trigger as a single condition, `input.run_in_background == true` — accurate for the
explicitly-requested case, but written before (or without accounting for) the fact that Claude Code's own Bash
timeout behavior can promote a command to background unprompted. [0001](0001-sendmessage-untracked-background-dispatch.md)'s
lesson already named the general failure mode — "what counts as a background dispatch is a list, not a rule" —
and predicted that a third occurrence should prompt reconsidering the architecture rather than adding another
special case. This is that third occurrence, arriving not from a new tool (as with `SendMessage`) but from an
existing tool gaining a second, harness-driven path to the same outcome.

## Fix

Stop detecting `Bash` background dispatch from the assistant's declared intent; detect it from the harness's own
confirmation instead, which is the one place both triggers agree:

1. `_launch_ids()` now dispatches by envelope type: `assistant` envelopes are checked for `Agent`/`SendMessage`
   launches only (unchanged rules); `user` envelopes are checked for a `tool_result` block whose sibling
   `toolUseResult.backgroundTaskId` is set — set by the harness whenever a `Bash` call is actually running in the
   background, regardless of why.
2. Guarded against `toolUseResult` being a plain string rather than a dict — real transcripts show this shape for
   a *failed* foreground command (`toolUseResult` holding the raw error text), which surfaced as a crash the first
   time the fix was run against a real production transcript rather than only synthetic fixtures.
3. Added `bg_bash_timeout_promoted.jsonl` (anonymized from the real `argocd app sync root` transcript) and
   `bash_error_string_result.jsonl` fixtures, following TDD (failing red for both cases, then minimal green).
   Updated the existing `bg_bash_ack_only.jsonl`/`bg_bash_completed.jsonl` fixtures to include the
   `toolUseResult.backgroundTaskId` field the new detection now requires.
4. Updated `CLAUDE.md`, `README.md`, and `claude-notify-product-doc.md` §4.1/§4.3 to describe `Bash` launch
   detection as an ack-side structural check, not a launch-time flag check.

## Lesson

**Detecting the shape of an async dispatch means detecting it at the point where the shape is actually confirmed
— not at the point where it was merely requested.** The original design already had the right idea (an immediate
ack tool_result ≠ completion) but still read *launch* from the wrong side of the exchange: the caller's intent,
rather than the callee's confirmation. Intent and confirmation happen to coincide for an explicitly-requested
background `Bash` call, which is why the bug hid for as long as it did — but they diverge for any trigger the
*harness* decides on unilaterally, and nothing about "detect launches on the assistant's tool_use block" was ever
going to generalize to that. The `toolUseResult.backgroundTaskId` field is a better anchor precisely because it's
harness-authored and populated identically no matter which of the two triggers produced it — the reusable version
of this lesson is to prefer a signal authored by the system whose behavior you're actually trying to observe, over
a signal authored by the party merely requesting that behavior.

**How to apply this going forward:** when a new way for an existing tracked tool to end up dispatched
asynchronously is discovered (this is the second trigger found for `Bash` alone, after the original explicit-flag
case), check whether the *existing* detection field still covers it before adding a new branch — often, as here,
the fix is to move detection to a shared, harness-authored signal rather than enumerate another special case on
top of the caller-declared one.

## Related

- [0001-sendmessage-untracked-background-dispatch.md](0001-sendmessage-untracked-background-dispatch.md) — the
  first two occurrences of this failure class (background-`Bash` immediate-ack false positive, then untracked
  `SendMessage`), and the explicit prediction that a third should prompt an architecture rethink.
- [claude-notify-product-doc.md §4.1](../claude-notify-product-doc.md#41-what-counts-as-a-background-dispatch) —
  updated background-dispatch table.
- `claude_code_notify/transcript_parser.py` — `_launch_ids()`, `_assistant_launch_ids()`, `_bash_background_ack_ids()`.
- `tests/test_transcript_parser.py` — `test_background_bash_timeout_promotion_detected`,
  `test_bash_error_string_tooluseresult_not_treated_as_background`.
