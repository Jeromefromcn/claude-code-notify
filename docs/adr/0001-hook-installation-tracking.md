# 0001. Use a state file instead of path-string matching to identify our own hooks

## Status

Accepted

## Context

`installer.py` manages the hook entries it writes into `settings.json` separately from the user's other hook entries, so that `merge` (install/upgrade) and `remove` (uninstall) only ever touch its own entries.

The v0.1.0 approach (`_is_ours`) checks whether a hook's `command` string contains the substring `"claude-code-notify"`. This always holds under the only documented default install path (`~/.claude/claude-code-notify/`), but has two structural problems:

1. **Decoupled from `base_dir`**: if the install path (`CLAUDE_NOTIFY_HOME`, currently test-only and not documented externally) changes, old and new entries are judged by completely different logic (a pure string coincidence) — reinstalling produces duplicate hooks, and uninstalling leaves stale entries that can't be cleaned up. This is exactly [todo.md](../todo.md) issue 7.
2. **The substring match itself is imprecise**: in principle, any user-defined hook whose command path happens to contain this substring (e.g. the user's own project happens to share this name) would be misidentified as "ours" and get overwritten or removed.

Simply changing the logic from "substring contains" to "command path is prefixed by the current `base_dir`" would make the match more precise, but doesn't solve point 1: if two calls (e.g. first installed at path A, then reinstalled or uninstalled using path B) use different `base_dir` values, the new call still has no way of knowing that the old entry under path A was one we installed.

## Decision

`installer.py` now uses a separate state file that records "the exact hook command string actually written to `settings.json` last time," replacing any guesswork based on path content:

- The state file is named `.claude-code-notify-hooks.json` and **lives in the same directory as `settings.json`** (rather than under `base_dir`). Reason: `base_dir` (`CLAUDE_NOTIFY_HOME`) is allowed to change across use cases (this is exactly what triggers issue 7), but the path to `settings.json` (`CLAUDE_SETTINGS`) is effectively constant for real users, and is also the reference point that test sandboxing already overrides together with `base_dir` (see the `_run` helper in `tests/test_install_e2e.py`). Tying the state file to `settings.json` ensures it can still be found after `base_dir` changes.
- `merge_hooks(settings, base_dir, state)` uses the "exact command string written last time" recorded in the state file to match and remove the old entry in `settings.json` (instead of guessing the path), then writes a new entry pointing at the current `base_dir`, and returns the updated state.
- `remove_hooks(settings, state)` likewise uses the exact command string recorded in the state file to remove the corresponding entry; the caller deletes the state file after success.
- **Legacy migration**: if a given event has no record in the state file (e.g. a user upgrading from the old v0.1.0, which never produced a state file), it falls back to the legacy substring-matching method, one-time "claiming" the existing entry to avoid producing a duplicate hook after upgrade. This fallback path only triggers when there's no state record; every call after that has an exact record to use.

## Consequences

**Positive:**
- Reinstalling and uninstalling no longer depend on the current call's `base_dir` happening to match the past; as long as `settings.json` itself hasn't moved, state stays trackable no matter how the install path changes.
- No more risk of a user's own hook being misidentified just because its command path happens to contain the substring (matching is now exact string equality).
- Transparent for existing (v0.1.0) installs: migration happens automatically on first upgrade, with no manual user intervention needed.

**Costs:**
- Adds one small file under `~/.claude/` (the same directory as `settings.json`, rather than this tool's own `~/.claude/claude-code-notify/`). This deviates slightly from the existing convention of "keep config files under our own directory" (see the [product doc](../claude-notify-product-doc.md) §5.3), but this state file contains no sensitive information and is removed on uninstall.
- The function signatures of `merge_hooks`/`remove_hooks` in `installer.py` change (an added `state` parameter, and `merge_hooks`'s return value becomes a tuple); this is an internal API, and the caller (`main()`) has been updated accordingly, with no impact on `install.sh`'s external calling interface (CLI arguments).
- Still only protects the scenario "`settings.json` path unchanged, `base_dir` changed." If a user also changes `CLAUDE_SETTINGS` without moving the state file, tracking is lost the same way — but that's already outside v1's documented "global-install-only" use case.

## Related

- [todo.md](../todo.md) issue 7
- [claude-notify-product-doc.md](../claude-notify-product-doc.md) §5.4 (hook integration design)
