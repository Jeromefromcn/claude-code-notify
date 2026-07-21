# 0001. `SendMessage` was an untracked background dispatch — a premature "finished" notification

## Status

Resolved. Fix shipped in the commit that added this document.

## Summary

The user received a Telegram notification: `Claude Code finished | 16m36s | ... | 21/07/2026 21:59:25` for this
repository's own session. The turn had not actually finished — a background agent, resumed via the `SendMessage`
tool, was still running and completed **2 minutes 32 seconds after** the "finished" notification was sent. Root
cause: `transcript_parser._launch_ids()` only recognized `Agent` and `Bash` tool calls as background dispatches.
`SendMessage`, which resumes a previously-spawned agent asynchronously, was invisible to the pending-task tracker,
so `compute_pending()` undercounted and the `Stop` hook fired early.

This is the same *class* of bug the tool was originally built to fix (the background-`Bash` immediate-ack false
positive, see [claude-notify-product-doc.md §3](../claude-notify-product-doc.md#3-background-how-the-current-hook-behaves-and-why-its-wrong)),
recurring because a new async-dispatch tool (`SendMessage`) was added to the Claude Code tool surface after the
original design was written, and nothing forced a re-audit of the "what counts as a background dispatch" list
when it shipped.

## Timeline (all timestamps UTC, from the session transcript)

| Time | Event |
|---|---|
| `13:59:08.268` | Assistant calls `SendMessage` (`tool_use_id=toolu_01EYuWhMSyHX2HMnmo15q1qT`) to resume a background agent ("Implement Task 5: reset-time parsing") that had already been spawned and paused earlier in the session. |
| `13:59:08.288` | An immediate `tool_result` ack arrives for that call — a delivery/queued acknowledgment, not the agent's actual output. |
| `13:59:23.486` | Assistant's turn-ending text is written to the transcript. |
| `13:59:26.094` | The `Stop` hook (`hooks/stop.sh`) actually runs (confirmed via the transcript's `system`/`stop_hook_summary` entry, `durationMs: 844`). `compute_pending()` returns `0` because the `SendMessage` call was never added to `state.launched`. The hook sends the "finished" Telegram notification — this is the message the user received, at local time `21:59:25` (UTC+8). |
| `14:01:58.115` | The resumed agent actually stops. A `<task-notification>` is written to the transcript with `<tool-use-id>toolu_01EYuWhMSyHX2HMnmo15q1qT</tool-use-id>`, matching the `SendMessage` call from `13:59:08`. This is the point the turn was genuinely done — 2m32s after the false "finished" notification. |

## Root cause

`claude_code_notify/transcript_parser.py`'s `_launch_ids()` classified a `tool_use` block as a tracked background
launch using this rule:

```python
if name == "Agent" and run_bg is not False:
    yield tool_id
elif name == "Bash" and run_bg is True:
    yield tool_id
```

`SendMessage` calls fall through both branches and are silently ignored. But `SendMessage` behaves exactly like the
other two from the pending-tracker's point of view:

- It returns an **immediate ack `tool_result`** the instant it's called (`"Message delivered"`/queued-style
  content) — long before the resumed agent has actually finished responding.
- The resumed agent's real completion is reported **later**, via a `<task-notification>` whose `<tool-use-id>`
  matches the `SendMessage` call's own id — not the id of whatever `Agent` call originally spawned it.
- It has **no `run_in_background` flag at all** to gate on; unlike `Bash`, which defaults to foreground,
  `SendMessage` is unconditionally asynchronous, because "send a message to a teammate agent" only makes sense as
  a fire-and-continue operation — the whole point is that the agent keeps running independently after the message
  is delivered.

Because none of that was encoded in `_launch_ids()`, the `tool_use_id` from the `SendMessage` call never entered
`state.launched`, so it could never be counted as `PENDING` — and a `<task-notification>` that later arrived for
it was simply an orphaned completion event for an id nobody was tracking.

## Why the original design missed this

[claude-notify-product-doc.md §4.1](../claude-notify-product-doc.md#41-what-counts-as-a-background-dispatch) enumerates exactly two tools (`Agent`, `Bash`) as background
dispatches — this table was accurate for the Claude Code tool surface at the time the design was written. `§4.2`
even says *"a `<task-notification>` may fire more than once for the same task (an agent can be resumed)"* — the
author was aware resumption exists, but implicitly assumed the *original* launch's `tool_use_id` would simply
receive a second, later `<task-notification>`. In reality, each resumption is a **new tool call with its own
`tool_use_id`** (a `SendMessage` invocation, not another `Agent` invocation), so it needed to be treated as a new
launch in its own right — not a re-open of the old one. Nothing in the design doc, code, or test fixtures modeled
resumption as its own dispatch path, so the gap wasn't caught by review or by the test suite (which only ever
exercised `Agent` and `Bash` fixtures).

## Fix

- `_launch_ids()` now also yields the tool id for any `SendMessage` block, unconditionally (no flag to check).
- Added fixtures/tests mirroring the existing `Agent`/`Bash` coverage:
  `sendmessage_pending.jsonl`, `sendmessage_completed.jsonl`, `sendmessage_ack_only.jsonl` and their corresponding
  cases in `tests/test_transcript_parser.py`, following TDD (failing red, then minimal green).
- Updated `CLAUDE.md`, `README.md`, and `claude-notify-product-doc.md` §4.1–4.3 to list `SendMessage` alongside
  `Agent`/`Bash` as a tracked background dispatch, and to correct the resumption model: a resume is a *new*
  launch, not a reopening of the original one.

## Lesson

**"What counts as a background dispatch" is a list, not a rule — and lists rot as the tool surface grows.** The
original design correctly identified the *pattern* (immediate ack ≠ real completion; only a matching
`<task-notification>` resolves a dispatch) but encoded it as an enumeration of two specific tool names. Any new
Claude Code tool that follows the same async-dispatch-with-ack shape (as `SendMessage` does) is invisible to this
system by default, with no test failure or type error to catch the gap — the failure mode is a silent false
positive in production, not a crash.

**How to apply this going forward:**
- When a new tool is added to the Claude Code tool surface (or an existing tool changes shape), explicitly ask
  "does this tool return an immediate ack while doing real work asynchronously afterward?" If yes, it needs a
  `_launch_ids()` branch and fixture coverage, the same way `SendMessage` now has.
- Prefer detecting the *shape* (immediate ack + later out-of-band completion signal) over hardcoding tool names
  where feasible, so future async tools are covered without another incident like this one — see
  [claude-notify-product-doc.md §11 roadmap](../claude-notify-product-doc.md#11-roadmap-post-v1-explicitly-out-of-scope-now) for where this could go if it becomes worth the complexity.
- This is the second time this exact failure mode (immediate ack mistaken for completion) has shipped, once for
  background `Bash`, once for `SendMessage`. A third occurrence should prompt reconsidering the architecture —
  e.g., a single "does this envelope look like an async dispatch" heuristic — rather than adding a third
  special-cased branch.

## Related

- [claude-notify-product-doc.md §3](../claude-notify-product-doc.md#3-background-how-the-current-hook-behaves-and-why-its-wrong) — the original background-`Bash` false positive this bug echoes.
- [claude-notify-product-doc.md §4.1–4.3](../claude-notify-product-doc.md#41-what-counts-as-a-background-dispatch) — updated background-dispatch table and resolution rule.
- `claude_code_notify/transcript_parser.py` — `_launch_ids()`.
- `tests/test_transcript_parser.py` — `test_sendmessage_launch_detected`, `test_sendmessage_completed`,
  `test_sendmessage_ack_is_not_completion`.
