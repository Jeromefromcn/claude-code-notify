# 0004. `StopFailure`'s payload is sufficient on its own — the transcript read was defense-in-depth for an untested case

## Status

Resolved. Supersedes the "transcript first, payload as fallback" design from
[0002](0002-stopfailure-transcript-write-race.md) for the `StopFailure` path specifically.

## Summary

[0002](0002-stopfailure-transcript-write-race.md) added a `StopFailure` payload fallback
(`payload["error"] == "rate_limit"`, using `payload["last_assistant_message"]`) but kept the
transcript read as the *primary* source, because at the time it was an open question whether
`last_assistant_message` reliably carries the same specific, parseable reset text the transcript does. A
`_debug()` call was added to unconditionally log `StopFailure`'s raw payload fields on every invocation,
specifically so the first real occurrence could answer that question with evidence instead of guesswork.

A real production event (`PA_Agent` session, 2026-07-23 02:14:18 HKT) answered it: `payload.last_assistant_message`
was `"You've hit your session limit · resets 5:20am (Asia/Hong_Kong)"` — byte-identical to what the transcript
held once the write race (also present in this same event; see 0002) resolved a moment later. This makes
sense structurally: Claude Code must already hold that exact string in memory to put it in the payload
before it can also flush it to the transcript, so the payload is if anything the more original copy, not a
lesser one.

## Decision

Given one real confirmation, was it safe to delete the transcript read from the `StopFailure` path
entirely? No — that was considered and rejected. The one data point only covers a synthetic single-block
rate-limit envelope; it says nothing about whether `last_assistant_message` is *always present* when
`error == "rate_limit"` (the codebase's own tests already anticipated it might not be — see
`test_stop_failure_falls_back_to_generic_text_when_payload_message_absent_too`), or about the weekly-limit
text format, which has never been observed via `StopFailure` at all.

The design adopted instead: **use the payload fields first when they're sufficient (`error == "rate_limit"`,
`errorDetails`/`error_details` not a [model-credits error](0003-model-credits-error-misclassified.md), and
`last_assistant_message` present) — this is the common case and needs no transcript read, no retry, no
sleep at all. Fall back to the transcript (with the existing 0.2s retry) only when the payload itself
doesn't classify as a usable rate limit** — i.e. exactly the untested edge case the evidence doesn't yet
rule out. This removes the retry/sleep from the path that actually fired in production, without deleting
the safety net for the case that hasn't been observed yet.

One consequence, accepted deliberately: if the payload's `last_assistant_message` and the transcript's text
ever genuinely differ (not observed, but not provably impossible), the payload version wins even if the
transcript would have been richer — see `test_stop_failure_prefers_payload_text_over_transcript_when_both_present`.

## Fix

- `_maybe_handle_usage_limit()` computes `payload_is_rate_limit` once
  (`payload.get("error") == "rate_limit"` and not a model-credits error) and branches on it:
  - If `payload_is_rate_limit` and `payload.get("last_assistant_message")` is present: use it directly,
    skip the transcript entirely.
  - Otherwise: fall back to `usagelimit.latest_usage_limit()` with the existing retry, and if that still
    comes up empty but `payload_is_rate_limit` was true, use the generic `"usage limit reached"` text
    (unchanged from 0002 — richness-only degradation, never a missed notification).
- The plain `Stop` path is unaffected: it never carries a `StopFailure`-style `error` field in observed
  payloads, so `payload_is_rate_limit` is false there and it falls straight to the (already
  retry-free) transcript read, same as before.

Verified test-first: `test_stop_failure_uses_payload_directly_without_reading_transcript` (asserts the
transcript-read function is called zero times and no sleep happens),
`test_stop_failure_prefers_payload_text_over_transcript_when_both_present` (locks in the new priority
even when both sources disagree), plus the existing 0002/0003 tests re-verified to still pass unchanged
where the payload doesn't classify as a rate limit at all.

## Lesson

**An assumption flagged as "unverified, being watched" is worth revisiting the moment real evidence
arrives — but one data point resolves the specific case it covers, not the whole hypothesis space.** 0002's
unconditional payload logging was exactly the right instrument to plant; the discipline that mattered here
was resisting the pull to either (a) ignore the new evidence and leave the untested conservative design in
place forever, or (b) over-generalize from n=1 and delete the fallback that still protects an unobserved
edge case. The middle path — promote the now-verified case to the fast path, keep the safety net scoped to
what's still unverified — used the evidence for exactly what it proved.

**How to apply this going forward:** when a piece of defense-in-depth code was added under an explicit
"we don't know yet, so be conservative" rationale, treat that rationale as having an expiration condition
(here: "the first real occurrence with logging on"), not as permanent. When that condition is met, revisit
the design rather than leaving the conservative version in place by default — but keep whatever part of it
still covers a genuinely unverified case.

## Related

- [`0002-stopfailure-transcript-write-race.md`](0002-stopfailure-transcript-write-race.md) — the design this
  supersedes for `StopFailure`'s priority order, and the source of the payload-logging instrumentation that
  made this decision possible.
- [`0003-model-credits-error-misclassified.md`](0003-model-credits-error-misclassified.md) — why
  `payload_is_rate_limit` also excludes model-credits errors, not just `error == "rate_limit"` alone.
- `claude_code_notify/hooks.py` — `_maybe_handle_usage_limit`.
- `tests/test_hooks.py` — `test_stop_failure_uses_payload_directly_without_reading_transcript`,
  `test_stop_failure_prefers_payload_text_over_transcript_when_both_present`.
