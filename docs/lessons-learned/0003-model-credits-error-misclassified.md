# 0003. A per-model usage-credits error was misclassified as an account usage limit

## Status

Resolved.

## Summary

A real production event sent a "Claude Code usage limit reached" Telegram notification for a message that
had nothing to do with the account's Claude subscription usage limit:

> Fable 5 requires usage credits. Run /usage-credits to continue or switch models with /model.

Fable 5 is a model that draws on its own usage-credits balance rather than the subscription's session/weekly
allowance. The notification was still technically correct in the narrow sense that detection matched
exactly what it was designed to match — the bug is that Claude Code reuses the same envelope-level fields
for both cases.

## Root cause

`usagelimit.latest_usage_limit()` classified a transcript's terminal assistant envelope as a usage-limit hit
whenever `isApiErrorMessage is True and error == "rate_limit"`. Both of these fields are also set, identically,
on a per-model usage-credits error. Comparing the two real envelopes side by side:

| Field | Genuine session limit | Fable 5 credits gate |
|---|---|---|
| `isApiErrorMessage` | `true` | `true` |
| `error` | `"rate_limit"` | `"rate_limit"` |
| `apiErrorStatus` | `429` | `429` |
| `errorDetails` | *(absent)* | `"429 {\"error\":{\"details\":{\"error_code\":\"credits_required\",\"model\":\"claude-fable-5\",...}}}"` |

The only structural difference is `errorDetails`: absent on both real usage-limit hits inspected (the
`567ff0c0` session's own earlier hit, and a separate `PA_Agent` session hit), present with a
`credits_required` error code on the Fable 5 case. `latest_usage_limit()` never looked at it.

The event went through the plain `stop` path (not `stop_failure`) — Claude Code apparently renders this
particular model error as a graceful turn completion rather than firing `StopFailure` — and detection
matched on the very first transcript read, no race involved. `parse_reset()` correctly failed to extract a
reset time from the credits-gate text (it doesn't match `resets H(:MM)am/pm`), so no bogus reset-ping
sleeper was spawned — only the incorrect "hit" broadcast fired, not a follow-up reset notification.

## Fix

- `usagelimit.is_model_credits_error(error_details)` parses the raw API error body (handles both the `dict`
  form and the `"<status> <json>"` string form Claude Code uses on disk and in hook payloads) and returns
  `True` iff `error.details.error_code == "credits_required"`.
- `latest_usage_limit()` now also requires `not is_model_credits_error(envelope.get("errorDetails"))` before
  classifying an envelope as a usage-limit hit.
- The `StopFailure` payload fallback in `hooks._maybe_handle_usage_limit()` (added in
  [0002](0002-stopfailure-transcript-write-race.md)) applies the same check against
  `payload.get("error_details")`, since Claude Code's payload and transcript representations carry the same
  raw error body under different key casing (`error_details` vs `errorDetails`) — the fallback would have
  the identical misclassification risk if a credits-gate error ever fires through `StopFailure` instead of
  `stop`.
- Both checks are envelope/structured-field-level only, consistent with this project's "never substring-match
  text" rule — `is_model_credits_error` inspects a specific structured error code, not the human-readable
  message.

Verified test-first: `test_model_credits_error_is_not_a_usage_limit`,
`test_genuine_rate_limit_without_error_details_still_detected`, and direct unit tests of
`is_model_credits_error` in `tests/test_usagelimit.py`; `test_stop_failure_payload_fallback_excludes_model_credits_error`
in `tests/test_hooks.py` for the payload path.

## Lesson

**A single boolean-looking API error field (`error == "rate_limit"`) can be reused by the platform for
semantically distinct conditions.** Detection logic built around "does this field equal this value" should
be re-verified against every real production envelope it encounters, not just the one it was designed
against — this project's own usage-limit test fixtures only ever encoded genuine session/weekly limit
envelopes, so this gap was structurally invisible to the test suite until a real non-subscription-model
error was observed via debug logging.

**How to apply this going forward:**
- When a platform's error taxonomy is only partially documented, prefer checking the most specific
  structured field available (`errorDetails.error.details.error_code` here) over the broadest one
  (`error == "rate_limit"`) whenever a more specific field exists, even if it means an extra parse step.
- Any new model or account-error mode Claude Code introduces in the future could plausibly reuse
  `error: "rate_limit"` again for something else entirely — this fix narrows one known false-positive, not
  the whole class. Treat a future unexplained usage-limit notification as worth an `errorDetails` diff
  against this table before assuming it's a duplicate of this bug.

## Related

- `claude_code_notify/usagelimit.py` — `is_model_credits_error`, `_error_body`, `latest_usage_limit`.
- `claude_code_notify/hooks.py` — `_maybe_handle_usage_limit` payload fallback.
- `tests/test_usagelimit.py`, `tests/test_hooks.py`.
- [`0002-stopfailure-transcript-write-race.md`](0002-stopfailure-transcript-write-race.md) — the payload
  fallback this bug also had to be patched into.
