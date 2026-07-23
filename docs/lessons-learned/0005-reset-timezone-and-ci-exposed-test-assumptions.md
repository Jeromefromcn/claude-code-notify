# 0005. Fixing the reset-time timezone bug exposed two more bugs — both only visible off this dev machine

## Status

Resolved. Three layered fixes in one push cycle, each only discovered because the previous one was
verified somewhere other than the original dev machine.

## Summary

While investigating the `PA_Agent` 5:20am reset-ping event (see [0002](0002-stopfailure-transcript-write-race.md)/
[0004](0004-stopfailure-payload-is-sufficient.md)), a separate, unrelated bug turned up: `parse_reset()`
computed the reset time in the **host machine's own local timezone**, silently discarding the timezone name
Claude Code embeds directly in the reset text (e.g. `"resets 5:20am (Asia/Hong_Kong)"`). It had produced the
correct result every time so far purely because the machine running `claude-code-notify` happened to also be
set to `Asia/Hong_Kong`. Fixing it — and then trying to ship it — surfaced two further bugs that had been
silently masked by that same coincidence, each one only visible once the code ran somewhere with a different
timezone or Python version than the dev machine.

## Bug 1 — the reset time itself: host tz instead of reported tz

`parse_reset(reset_text, now)` matched the hour/minute/am-pm out of the reset text but never looked at the
timezone name in parentheses. It computed the target moment via `datetime.datetime.fromtimestamp(now)` —
implicitly the *host's* local time — and compared/rolled it forward in that same implicit zone. If the
account's reported reset timezone ever differed from the host's, the sleeper (see 0002) would fire the reset
notification at the wrong wall-clock time, silently — no exception, no log line, just a misleading
notification some hours off.

**Fix:** `_RESET_RE` gained an optional trailing capture group for the parenthesized zone name;
`_resolve_tz()` resolves it via the stdlib `zoneinfo.ZoneInfo` when the name is present and valid, falling
back to `None` (host local time — the prior, unfixed behavior) when the name is absent, unresolvable, or
`zoneinfo` itself isn't importable (Python <3.9 without the `backports.zoneinfo` package — see Bug 3).
`datetime.datetime.fromtimestamp(now, tz)` then does the actual zone-aware computation.

## Bug 2 — CI caught two tests whose correctness silently depended on the dev machine's own timezone

Local `pytest` was green after Bug 1's fix (236/236). Pushing to GitHub Actions immediately failed two
**pre-existing** tests on the `ubuntu-latest` runner (which runs in UTC):

```
tests/test_usagelimit.py::test_parse_reset_returns_next_local_occurrence FAILED
tests/test_usagelimit.py::test_parse_reset_rolls_to_tomorrow_when_past FAILED
E   assert (13, 0) == (21, 0)
```

Both tests built `now` from a naive `datetime.datetime(2026, 7, 21, 10, 0, 0).timestamp()` (interpreted as
host-local) and verified `parse_reset()`'s result by converting back with `datetime.fromtimestamp(got)` (also
host-local) — checking for `resets 9pm (Asia/Hong_Kong)`. Under the *old*, buggy `parse_reset()`, this was
accidentally self-consistent on any host: both the input and the check used the same (whatever) host
timezone, so the test passed everywhere even though it was silently validating the wrong thing on any host
that wasn't HKT. Bug 1's fix made `parse_reset()` correctly compute in `Asia/Hong_Kong` regardless of host
timezone — which broke the test's now-mismatched assumption the moment it ran somewhere that wasn't HKT (13:00
UTC *is* 21:00 HKT; the test's own host-local conversion just labeled it wrong).

This dev machine's own timezone is `Asia/Hong_Kong` (confirmed via `timedatectl`), so this had been invisible
locally through every prior test run — the same class of masking that let Bug 1 itself ship unnoticed.

**Fix:** anchor both `now` and the assertion to an explicit `zoneinfo.ZoneInfo("Asia/Hong_Kong")`, so the
test's correctness no longer depends on the runner's own timezone. Verified locally with `TZ=UTC`,
`TZ=America/Los_Angeles`, and `TZ=Pacific/Kiritimati` before re-pushing.

## Bug 3 — Python 3.8 has no `zoneinfo` at all

Re-pushing the Bug 2 fix failed CI again, differently:

```
tests/test_usagelimit.py::test_parse_reset_uses_reported_timezone_not_host FAILED
E   ModuleNotFoundError: No module named 'zoneinfo'
```

`zoneinfo` is stdlib only from Python 3.9 onward; `pyproject.toml` declares `requires-python = ">=3.8"` and
CI's matrix includes 3.8 explicitly. Production code (`usagelimit.py`) already handled this correctly with a
`try/except ImportError` import guard at module load time. The four tests that directly exercise timezone
resolution (two fixed in Bug 2, two added alongside Bug 1) did not have the same guard — they did a bare
`from zoneinfo import ZoneInfo`, which is a hard `ModuleNotFoundError` on 3.8, not the graceful fallback the
production code degrades to.

A same-run red herring worth naming explicitly: `macos-latest` Python 3.8 also showed as failed in this run,
but its actual log was `The operation was canceled` at the `setup-python` step — a `fail-fast` cascade
cancellation from the *other* (real) failures, not an independent bug. Diagnosing which failures are real and
which are fail-fast noise mattered here: treating the macOS cancellation as a fourth bug would have been a
wrong lead.

**Fix:** `pytest.importorskip("zoneinfo")` in each of the four tests, so they skip cleanly on 3.8 instead of
failing — testing the same thing production code does: degrade gracefully, don't require the optional
dependency to exist.

## Why none of this showed up locally

All three bugs share one root cause pattern: **this dev machine is simultaneously in `Asia/Hong_Kong` and on
a Python version with `zoneinfo` available (3.12)** — exactly the one combination where none of the three
bugs can manifest. Bug 1 needs a host whose timezone differs from the reported one. Bug 2 needs the same.
Bug 3 needs a Python version without `zoneinfo`. A single-environment dev machine is structurally unable to
surface any of them; only CI's actual matrix (multiple OSes, multiple Python versions, `ubuntu-latest`
defaulting to UTC) could.

## Lesson

**A green local test suite proves the code is self-consistent with the machine that ran it — not that it's
correct.** Every layer here passed cleanly in this exact repo, on this exact machine, right up until it ran
somewhere else. The fix that mattered wasn't any one of the three patches — it was running the actual CI
matrix *before* tagging a release, instead of trusting "236 passed" locally as sufficient. Had the release
been tagged immediately after Bug 1's local green run, it would have shipped a **correct** reset-time fix
riding on a **silently broken** test suite — the tests would have kept passing by accident until some future
change collided with the same masked assumption, at which point the actual regression they were supposed to
catch would go undetected too.

**How to apply this going forward:**
- Treat "tests pass locally" and "tests pass in CI" as different claims. The gap between them is exactly
  where machine-specific assumptions (timezone, installed optional stdlib modules, OS-specific paths) hide.
- When a test hardcodes a real-world value that *could* vary by environment (a timezone name, a locale, a
  path separator), ask whether the test's *correctness*, not just its *output*, depends on the runner
  matching some specific environment — and if so, anchor it explicitly instead of relying on symmetry with
  a value derived from that same environment.
- When production code already carries a graceful-degradation guard for an optional feature (here: the
  `try/except ImportError` around `zoneinfo`), any new test exercising that feature needs the identical
  guard — it's easy to forget that the tests must degrade the same way the code under test does.
- Distinguish real CI failures from fail-fast cascade noise before treating every red job as an independent
  bug to fix — check each job's actual log, not just its pass/fail color.

## Related

- `claude_code_notify/usagelimit.py` — `_resolve_tz`, `parse_reset`.
- `tests/test_usagelimit.py` — `test_parse_reset_uses_reported_timezone_not_host`,
  `test_parse_reset_reported_timezone_independent_of_host_offset`,
  `test_parse_reset_falls_back_to_host_tz_when_zone_unresolvable`,
  `test_parse_reset_returns_next_local_occurrence`, `test_parse_reset_rolls_to_tomorrow_when_past`.
- [`0002-stopfailure-transcript-write-race.md`](0002-stopfailure-transcript-write-race.md) — the investigation
  that surfaced the original PA_Agent event where this timezone bug was first noticed (though unrelated to
  0002's own subject).
- GitHub Actions CI (`.github/workflows/ci.yml`) — the `ubuntu-latest` × Python 3.8/3.11/3.12 and
  `macos-latest` × Python 3.8/3.11/3.12 matrix that caught both Bug 2 and Bug 3; commits
  `517c1a0`, `fb9aa31`, `c88f334`.
