# 0002. `StopFailure` fires before the transcript write lands — usage-limit hits go undetected

## Status

Resolved, in two stages. An initial transcript-read retry mitigated the race empirically. A follow-up fix
(see "Follow-up" below) then used Claude Code's own documented `StopFailure` payload fields to make the
classification itself race-free — found only after checking the official hooks docs, which turned out to
already describe more of this than the retry alone could fix.

## Summary

Two real usage-limit hits, on two different days, produced no usage-limit Telegram notification even
though `NOTIFY_USAGE_LIMIT=true` was on and the detection code (`usagelimit.latest_usage_limit()`) is
correct. Both times the user instead received the generic `Claude Code stopped with error | ...`
notification. Root cause: Claude Code can fire the `StopFailure` hook *before* it finishes writing the
terminal (rate-limit) envelope to the transcript file on disk. `latest_usage_limit()` reads whatever is
currently on disk at the instant the hook runs; if that read lands before the write, the rate-limit line
simply isn't there yet, and detection correctly (given what it can see) concludes there is no usage limit.
This is a race between two things Claude Code itself does — signal the hook, and persist the transcript —
not a bug in this tool's parsing logic.

The second occurrence was caught with millisecond precision only because
[the commit before this one](../../CHANGELOG.md) had just added `_debug()` calls to every branch of the
usage-limit detection path. The first occurrence, weeks earlier in real time but investigated first, could
only be diagnosed forensically after the fact, from transcript timestamps — and its exact mechanism was
genuinely ambiguous until the second occurrence's debug log resolved it.

## Timeline

### Incident 1 — this repo's own session (forensic reconstruction, no debug log existed yet)

All timestamps below are local (Asia/Hong_Kong, UTC+8); the transcript itself stores UTC.

| Time (HKT) | Event |
|---|---|
| `14:11:24.795` | Transcript line 152 (of what was then 180 lines) is written: a synthetic assistant envelope, `isApiErrorMessage: true`, `error: "rate_limit"`, text `"You've hit your session limit · resets 2:20pm (Asia/Hong_Kong)"`. `model` is `"<synthetic>"` — this is Claude Code itself inserting a marker envelope, not a real model turn. |
| `14:11:24` | User receives `Claude Code stopped with error \| 46s \| 本地測試開發功能 \| /home/ubuntu/jerome/claude-code-notify \| 22/07/2026 14:11:24` — the **generic** StopFailure notification, timestamped to the same second as the rate-limit envelope. This confirms a `StopFailure` hook genuinely fired at that instant; it did not go unfired. |
| (later) | The conversation continues; 28 more transcript lines are appended after line 152 by the time this was investigated. |

At investigation time, replaying `latest_usage_limit()` against the transcript truncated to exactly line
152 correctly returned the reset text; against the full (now 180-line) transcript it correctly returned
`None`, per the tool's intentional "only the last transcript entry counts" rule
(`test_stale_rate_limit_before_normal_turn_is_ignored`). Because the conversation had moved on by
investigation time, it was impossible to tell whether the *original* `StopFailure` hook invocation (at
`14:11:24`) had already seen a transcript with more content past the rate-limit line (an auto-retry that
beat the hook to it), or whether it had raced the write and seen fewer lines. The state directory
(`~/.claude/claude-code-notify/state/usage_limit/`) did not exist at all, confirming detection had failed,
but not why.

### Incident 2 — a different project's session, with debug logging now on

| Time (HKT) | Source | Event |
|---|---|---|
| `00:13:14.643` | transcript line 144 (the **last** line — nothing followed it) | Synthetic rate-limit envelope written: `isApiErrorMessage: true`, `error: "rate_limit"`, `apiErrorStatus: 429`, text `"You've hit your session limit · resets 12:20am (Asia/Hong_Kong)"`. |
| `00:13:14.735881` | `debug.log` | `usage-limit: no rate-limit as last transcript entry (transcript=.../oss-devrel/d2b0c48a-a867-464b-ad54-ce6071169fa7.jsonl)` — the hook read the transcript and found nothing. |
| `00:13:14.755575` | transcript file mtime (`stat`) | The file's **last write** completed — i.e. the write that put the rate-limit line on disk landed **~19.7ms after** the hook had already read the file and moved on. |
| `00:13:15.467429` | `debug.log` | `stop_failure notified chat=8737165697` — the generic "stopped with error" notification is sent, because `_maybe_handle_usage_limit` had already returned `False`. |

Replaying `latest_usage_limit()` against this transcript file (post-hoc, after the write had long since
landed) correctly returns the reset text — proving the parser is not at fault. The only variable between
"correct" and "incorrect" is *when* the file was read relative to *when* it was written, and this time the
gap was measured directly: the hook's read is provably ~20ms earlier than the write it needed.

## Root cause

Claude Code fires the `StopFailure` hook without a happens-before guarantee that the terminal (error)
transcript envelope has already been flushed to disk. `usagelimit.latest_usage_limit(transcript_path)`
does exactly what it should with whatever bytes are on disk *at the moment it's called* — it has no way to
know a write is still in flight. Both real occurrences were on the `StopFailure` path specifically, which
is consistent with a plausible mechanism: on a graceful completion, the full response necessarily exists
before Claude Code can decide the turn is "done" and fire `Stop`, so there's no meaningful race. On an
*error* path, Claude Code may signal the hook based on detecting the error condition itself, concurrently
with — not strictly after — persisting that error to the transcript. `Stop` (the success path) has never
shown this symptom in any of the debug log's ~78 recorded invocations; every occurrence so far has been
`StopFailure`.

## Why this wasn't caught earlier

Every other piece of this tool that reads the transcript (`compute_pending`, `latest_ai_title`,
`turn_start_timestamp`, the original usage-limit design and its tests) reads it on paths where the file is,
in practice, always long-since-complete by hook time, so the race — if it exists there too — has never had
a large enough window to be observed. The usage-limit feature's own test suite writes the transcript file
completely, synchronously, *before* invoking the hook in every single test; a real hook invocation is never
racing anything in that setup, so this class of bug is structurally invisible to the test suite — only real
production timing, captured by debug logging, could ever surface it. This is also why the fix in
`659dc8d`/`9e78fe3` (the `window_key` collision fix) and the debug-logging commit immediately before this
one were both necessary but not sufficient on their own: neither one could have revealed *this* gap without
the other. The window_key fix made distinct dates distinguishable; the debug-logging commit made a missing
detection event visible; only having both let this incident's exact ~20ms number be measured at all.

## Fix

- `_maybe_handle_usage_limit(payload, config, retry_delays=())` gained a bounded retry: if the first
  `usagelimit.latest_usage_limit()` read finds nothing, sleep for each delay in `retry_delays` in turn and
  re-read, stopping as soon as one succeeds.
- `_STOP_FAILURE_RETRY_DELAYS = (0.2,)` — one retry, 200ms, roughly 10x the observed ~19.7ms gap, as a
  safety margin against filesystem/scheduler jitter without being large enough to feel sluggish on an
  error notification.
- The retry is wired **only** into `handle_stop_failure`, not `handle_stop` — both real occurrences were on
  the `StopFailure` path, and blanket-retrying on `handle_stop` would add latency (or at least an extra
  file read) to every single normal turn completion, the overwhelmingly common case, for a race that has
  never been observed there.
- Debug logging now distinguishes all three outcomes: detected on the first read (unchanged from before),
  detected only after N retries (`usage-limit: detected after N retry(ies)`), or still nothing after
  retries were exhausted (`usage-limit: no rate-limit as last transcript entry (..., retries=N)`) — so any
  future occurrence, or any case where even 200ms isn't enough, is immediately visible in the log without
  needing another forensic reconstruction like this one.
- `_sleep()` is a thin wrapper around `time.sleep()` purely so tests can monkeypatch it and run instantly
  instead of actually waiting.

Verified test-first: `test_stop_failure_retries_transcript_read_to_bridge_write_race` (a fake
`latest_usage_limit` that returns `None` once then the real value, asserting exactly one retry recovers
it), `test_stop_does_not_retry_transcript_read` (locks in that `handle_stop` never sleeps or re-reads), and
`test_stop_failure_retry_exhausted_falls_back_to_normal_error` (a genuine non-rate-limit error still falls
through to the normal notification once retries are exhausted). All three were run and confirmed failing
(`_sleep` didn't exist yet) before the fix was written.

## Follow-up: the official docs already documented a race-free field

After the retry fix above had already shipped, the natural next question was asked: is this a *known*
Claude Code behavior, and does its hook contract offer anything better than reading a file that might not
be flushed yet? Checking Claude Code's official hooks reference
([code.claude.com/docs/en/hooks.md](https://code.claude.com/docs/en/hooks.md)) directly answered both:

- The docs state outright: *"The transcript file is written asynchronously and may lag the in-memory
  conversation, so it may not yet include the current turn's most recent messages when a hook fires."* —
  the exact race measured in incident 2, documented as expected behavior, not something to report as a bug.
- `StopFailure`'s own hook input already includes a structured `error` field (an enum — `rate_limit`,
  `overloaded`, `authentication_failed`, `billing_error`, and others — used for matcher filtering) and an
  optional `last_assistant_message` holding "the rendered error text shown in the conversation". Both
  arrive as part of the hook's stdin JSON, which Claude Code constructs before the hook process even
  starts — **no transcript file read involved, ever, for these two fields.**
- A public GitHub issue, [`anthropics/claude-code#15813`](https://github.com/anthropics/claude-code/issues/15813)
  ("Stop hook receives stale transcript - race condition between file write and hook invocation"),
  independently confirms the same mechanism: the hook process is spawned before the transcript write
  flushes. It was closed by a stale-bot for inactivity, not resolved-and-linked, so it wasn't an
  official fix commitment — `last_assistant_message` on `Stop`/`SubagentStop` looks like Anthropic's answer
  to it for the success path; `StopFailure`'s copy of the same field carries the *error* text instead of
  conversational output, a related but distinct piece of the same mitigation.

This also revealed that this project's own `docs/claude-notify-product-doc.md` §5.1 — which enumerates the
hook payload as just `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name` — is stale
relative to Claude Code's current contract (depending on event, it now also sends `error`, `error_details`,
`last_assistant_message`, `stop_hook_active`, `background_tasks`, `session_crons`, and more). Earlier in
this same investigation, *that* internal doc (not the official one) was what got consulted, leading to the
conclusion "there's no native error-reason field to use" — which was wrong.

Detection was updated a second time: still prefer the transcript-derived text first (it remains the
richest source — the only one `parse_reset()`/`window_key()` can extract a specific, schedulable reset
time from), but if the transcript read is still empty after the existing retry, fall back to
`payload["error"] == "rate_limit"` (using `last_assistant_message`, or a fixed generic string if even that's
absent) so a genuine rate limit can never again be misclassified as a generic error — only the notification's
*richness* (specific time, reset-ping scheduling) stays best-effort. Verified test-first:
`test_stop_failure_falls_back_to_payload_error_when_transcript_unavailable`,
`test_stop_failure_falls_back_to_generic_text_when_payload_message_absent_too`,
`test_stop_failure_prefers_transcript_text_over_payload_when_both_present` — plus
`test_stop_failure_logs_raw_error_payload_fields` and `test_stop_failure_logs_raw_payload_fields_when_absent`,
added as a pure observability step *before* this fix, to log the raw fields for one real occurrence and
confirm their actual content before any logic was built on them.

## Lesson

**A hook payload's `transcript_path` is not a guarantee that the file is fully written — it's a guarantee
that the file exists.** Any logic that reads "the current state of a file some other process just
populated, at the instant a hook fires" must treat a negative result as provisional on any path that isn't
a graceful, fully-assembled completion. A normal `Stop` after a real successful response has a wide,
effectively-safe margin; an error/interrupt path (`StopFailure`) does not, because the error condition and
the transcript write it produces aren't necessarily ordered relative to the hook firing.

**A second, costlier lesson surfaced only after the retry fix had already shipped: this project's
understanding of Claude Code's hook contract was checked against its own stale internal notes, not the
current official docs, and that cost real effort before anyone caught it.** The retry fix, its dedicated
tests, and the millisecond-precision forensic mtime comparison in incident 2 are all genuine, still-useful
work — the retry remains real defense-in-depth for whatever the payload fallback doesn't cover, and the
forensic method (compare a debug-log timestamp against `stat`'s mtime) is reusable for the next unrelated
mystery. But none of the *specific numbers* — why ~20ms, why a 200ms retry, whether `Stop` is provably safe
— needed to be reverse-engineered from raw timestamps at all: Claude Code's official hooks reference
already stated the transcript can lag, and already named the exact fields (`last_assistant_message`, plus a
structured `error` enum on `StopFailure`) built for this. Reading `docs.claude.com`'s current hooks page —
a five-minute read — would have surfaced the race-free fallback before any retry-tuning or mtime math was
needed, and would have caught that this project's own product-doc's hook-payload-field list (§5.1) had gone
stale.

**How to apply this going forward:**
- Before adding new detection logic that reads the transcript from a `StopFailure` (or any
  error/interrupt-triggered) hook path, ask whether the same race applies, and default to a bounded retry
  there rather than assuming the file is complete.
- Do **not** apply the same retry to the `Stop` (success) path by default — the cost/benefit only works
  because `StopFailure` is rare. If a future incident shows the same race on `Stop`, that changes the
  cost/benefit calculation and is worth a fresh look, not a reflexive copy-paste of `_STOP_FAILURE_RETRY_DELAYS`.
- When a "no notification fired" report can't be explained from the code alone, the fastest path to ground
  truth is direct evidence, not more reasoning: `NOTIFY_DEBUG=true` plus comparing the debug log's read
  timestamp against `stat`'s mtime on the actual transcript file is what turned an ambiguous, unfalsifiable
  hypothesis (incident 1) into a proven one with a concrete number (incident 2). Instrument before
  hypothesizing further.
- **Before reverse-engineering any platform behavior empirically (timestamps, mtimes, retry-until-it-works),
  check the platform's current official documentation for the exact contract first** — not this project's
  own notes about it, which can go stale (as `claude-notify-product-doc.md` §5.1 did here) the moment the
  platform adds a field. A five-minute doc check can make an entire investigation unnecessary; an
  investigation can't tell you what the vendor already wrote down.

## Related

- `claude_code_notify/hooks.py` — `_maybe_handle_usage_limit`, `_STOP_FAILURE_RETRY_DELAYS`, `_sleep`,
  `handle_stop_failure`.
- `claude_code_notify/usagelimit.py` — `latest_usage_limit` (unchanged; proven correct against the same
  data post-hoc in both incidents).
- `tests/test_hooks.py` — retry: `test_stop_failure_retries_transcript_read_to_bridge_write_race`,
  `test_stop_does_not_retry_transcript_read`, `test_stop_failure_retry_exhausted_falls_back_to_normal_error`;
  payload fallback: `test_stop_failure_logs_raw_error_payload_fields`,
  `test_stop_failure_logs_raw_payload_fields_when_absent`,
  `test_stop_failure_falls_back_to_payload_error_when_transcript_unavailable`,
  `test_stop_failure_falls_back_to_generic_text_when_payload_message_absent_too`,
  `test_stop_failure_prefers_transcript_text_over_payload_when_both_present`.
- The commit immediately before this one (debug-logging coverage for every usage-limit branch in
  `hooks.py` and `recovery.py`) — the prerequisite instrumentation that made incident 2 diagnosable at all.
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks.md) — documents the transcript-lag
  behavior directly and the `error`/`last_assistant_message` fields the follow-up fix now uses.
- [`anthropics/claude-code#15813`](https://github.com/anthropics/claude-code/issues/15813) — a public
  report of the same race, independent of this incident.
- `docs/claude-notify-product-doc.md` §5.1 — this project's own hook-payload-field list, now known to be
  stale relative to Claude Code's current contract; worth a dedicated refresh.
- [`0001-sendmessage-untracked-background-dispatch.md`](0001-sendmessage-untracked-background-dispatch.md)
  — a different bug in the same tool, but the same underlying lesson: a missing-notification report is
  worth a forensic reconstruction from real transcripts/logs, not a guess.
