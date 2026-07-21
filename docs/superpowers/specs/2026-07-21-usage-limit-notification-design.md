# Design: usage-limit notification (broadcast + scheduled reset ping)

## Goal

When the Claude account hits a usage limit (the 5-hour session limit or the
weekly limit), notify the user over Telegram, **broadcasting to every distinct
configured destination** (the global default plus every route), not just the
one that matches the current `cwd`. A usage limit is account-global, so every
audience the user configured should learn about it.

Two independent notifications:

1. **hit** — the moment the limit is reached: broadcast "you've hit your
   limit, resets at X" to all destinations. Pure hook, no background process.
2. **reset** — at the reported reset time: broadcast "usage limit reset" to
   all destinations, exactly once. Requires a scheduled wake-up (a transient
   background process), because when the user is waiting for the reset no
   Claude Code hook is firing.

The whole feature is **opt-in and off by default**. With it disabled the tool
behaves exactly as today. It runs entirely locally plus Telegram HTTP and
**consumes zero Claude/Anthropic tokens** at runtime — nothing here calls an
LLM.

## Detection signal (envelope-level, no text matching)

Ground truth, confirmed against 30 real occurrences in live transcripts: a
usage limit is written to the JSONL transcript as an assistant envelope
carrying structured error fields. Example (trimmed):

```json
{ "type": "assistant",
  "isApiErrorMessage": true,
  "error": "rate_limit",
  "apiErrorStatus": 429,
  "message": { "model": "<synthetic>",
    "content": [{ "type": "text",
      "text": "You've hit your session limit · resets 7:50pm (Asia/Hong_Kong)" }] } }
```

**Detection is purely structural** and never substring-matches text, honoring
the core rule:

```
type == "assistant"  AND  isApiErrorMessage == true  AND  error == "rate_limit"
```

This cleanly separates a usage limit from every other API-error envelope seen
in the wild (`error == "authentication_failed"`, `error` absent for
"No response requested.") — verified: `error == "rate_limit"` matched all 30
session-limit messages and nothing else. `apiErrorStatus == 429` is a
corroborating cross-check. Both session and weekly limits surface as
`rate_limit`, so both are detected.

The reset time exists **only inside the message text** (there is no structured
reset-timestamp field; the `usage` block is all zeros). The text is used in
exactly two narrow ways, both documented below, **never** to decide *what
happened* (that is always the structural rule above):

- as the human-readable body of the notification (pass-through display), and
- as an opaque per-window dedup key, and — for the reset ping only —
  best-effort parsed for a wall-clock time.

## Config schema (`config.env`)

Two new keys, both optional; global token/chat_id stay required as today.

```env
NOTIFY_USAGE_LIMIT=false          # master switch for the whole feature (default false)
NOTIFY_USAGE_LIMIT_RESET=true     # when the feature is on, also schedule the reset ping
                                  # (default true). Set false to keep only the hit
                                  # broadcast and never spawn a background process.
```

| Key | Default | Meaning |
|---|---|---|
| `NOTIFY_USAGE_LIMIT` | `false` | Off → feature entirely inert (today's behavior). On → hit broadcast active. |
| `NOTIFY_USAGE_LIMIT_RESET` | `true` | Only consulted when the master is on. On → also schedule the reset ping (transient sleeper). Off → hit broadcast only, **zero background processes ever**. |

Both are truthy-parsed with the existing `_truthy` helper and are
env-overridable (added to `load()`'s env key list alongside `NOTIFY_DEBUG`).
`Config` gains two bool fields, `usage_limit` and `usage_limit_reset`, with
literal defaults (`False` / `True`) — bare annotations, 3.8-safe.

## Runtime floor (applies to every new module)

The project floor is `requires-python = ">=3.8"`. Two consequences:

- **No PEP 585/604 runtime annotations** (`list[...]`, `X | None`) — use
  `typing.Optional`/`typing.List` or bare annotations, exactly as `routing.py`
  does.
- **`zoneinfo` is 3.9+**, so the reset-time parser must not hard-depend on it
  (see [Reset-time parsing](#reset-time-parsing-best-effort)). Zero third-party
  dependencies remain a hard constraint; `python3` stays the only runtime dep.

## Trigger and suppression (`hooks.py`)

Detection piggybacks on the reliably-firing `Stop` hook (and, defensively, on
`StopFailure`). A new shared helper runs **first** in both handlers:

```python
def _maybe_handle_usage_limit(payload, config):
    """Return True if this turn ended in a usage limit and was handled here
    (caller must then return without sending its normal notification)."""
    if not config.usage_limit:
        return False
    transcript = payload.get("transcript_path", "")
    reset_text = usagelimit.latest_usage_limit(transcript)  # None if not a limit turn
    if reset_text is None:
        return False
    # From here the turn ended in a usage limit: always suppress the normal
    # finished/error notification, even if we already broadcast this window.
    key = usagelimit.window_key(reset_text)
    if usagelimit.claim_hit(config.base_dir, key):   # atomic, once per window
        broadcast.send_all(config, notifier.build_message(
            "usage-limit", payload.get("cwd", ""), _when(), title=reset_text))
        if config.usage_limit_reset:
            target = usagelimit.parse_reset(reset_text, _now())
            if target is not None:
                recovery.spawn(config.base_dir, key, target)  # O_EXCL claim + detached
    return True
```

`handle_stop` / `handle_stop_failure` each start with:

```python
if _maybe_handle_usage_limit(payload, config):
    return
```

- Placing it before the pending / rate-limit work means a limit turn never
  emits a misleading "finished" and never touches the per-session rate-limit
  marker.
- The unconditional `return` when `reset_text is not None` suppresses the
  normal notification on every limit turn (including retries within the same
  window). `claim_hit` gates the *broadcast* to once per window; the
  suppression is independent of it.
- If both `Stop` and `StopFailure` fire for the same limit turn, the per-window
  `claim_hit` guarantees a single broadcast and both suppress their own
  normal message.

`notifier.py` gains two entries in `_HEADS` for `build_message` — e.g.
`"usage-limit": "Claude Code usage limit reached"` and
`"usage-limit-reset": "Claude Code usage limit reset"` — and is otherwise
unchanged; its `send`/`scrub` contract is reused as-is.

`latest_usage_limit(path)` returns the reset text **iff the transcript's last
assistant (non-sidechain) envelope is a `rate_limit`** — i.e. the model's most
recent output was a limit — else `None`. Requiring it to be the *last*
assistant entry (trailing `queue-operation` / injected `user` lines ignored)
means a stale limit followed by any later normal turn does not re-fire, and
prevents a spurious broadcast for old history when the feature is first
enabled.

Everything on this path is wrapped by `hooks.run`'s existing crash-free outer
handler; additionally `spawn`, `parse_reset`, and each send are individually
guarded so no failure can break the turn.

## Broadcast destinations (`broadcast.py`)

New single-purpose module, unit-testable with an injected `send`.

```python
def destinations(config):
    """List of distinct (bot_token, chat_id), deduped. Never raises."""

def send_all(config, text, send=notifier.send):
    """Send text to every distinct destination; per-destination try/except."""
```

`destinations(config)` collects:

- the global default `(config.bot_token, config.chat_id)`, plus
- for every route with a `chat_id`, `(route.bot_token or config.bot_token,
  route.chat_id)`,

then de-duplicates by the `(bot_token, chat_id)` pair (order-preserving).

**Mute is not consulted.** A usage limit is account-global; a muted subtree
simply contributes no destination because a mute without a `chat_id` has
nowhere to send. (Per the product owner: mute is orthogonal here — "muted
routes don't configure a chat".) A muted route that *does* carry a `chat_id`
still counts as a configured destination and receives the broadcast; this is
intentional and documented.

`send_all` builds a routed `Config` per destination via
`dataclasses.replace(config, bot_token=bt, chat_id=cid)` and calls
`notifier.send`, so each send scrubs errors with *its own* token (the §9
guarantee extends to every destination for free). Each destination is wrapped
in its own `try/except NotifierError`: one dead chat never aborts the rest;
failures are `_debug`-logged (scrubbed), never raised.

## Hit dedup — once per reset window (`usagelimit.py`)

```python
def window_key(reset_text):
    """Opaque, filesystem-safe dedup key for one reset window."""
    return hashlib.sha1(reset_text.strip().encode("utf-8")).hexdigest()[:16]
```

Every rate_limit envelope within one window carries the *same* reset text
(same "resets 7:50pm"), so hashing the trimmed text yields one stable key per
window; a new window (new reset time) yields a new key. This is the **only**
place text drives control flow, and only as an equality key — never a
substring/pattern match, never a detection decision.

`claim_hit(base_dir, key)` atomically creates
`state/usage_limit/<key>.hit` with `open(path, "x")` and returns `True` on the
creating call, `False` if it already exists — race-free single-broadcast per
window across concurrent sessions and retries on one machine.

## Reset ping — transient sleeper (`recovery.py`)

### Reset-time parsing (best-effort)

`parse_reset` lives in **`usagelimit.py`**, not the sleeper: the hook computes
the target epoch and hands it to the sleeper as an argument, so the sleeper
never parses text. `CAP` is a shared module constant (8 days), referenced by
both the clamp here and the sleeper loop below.

`parse_reset(reset_text, now) -> Optional[float]`:

1. Narrow regex over the (already structurally-confirmed) reset text for the
   known **session** format: `resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)`
   (optional minutes; e.g. `9pm`, `7:50pm`), optionally capturing a
   `(<TZ name>)` suffix.
2. No match → return `None` (the reset ping is silently skipped; the hit
   broadcast already happened). **The weekly-limit text format is unverified**
   (no sample exists yet) and will typically carry a weekday/date the session
   regex does not match, so weekly limits simply get no reset ping until the
   regex is extended against a real sample — an accepted, documented
   limitation.
3. Compute the **next occurrence** of that wall-clock time:
   - **Primary, 3.8-safe, zero-dep:** interpret the time in the machine's
     **local** timezone — Claude Code prints the reset time in the user's local
     TZ, which is the machine's TZ — using a naive `datetime`: today at H:M
     local, rolled to tomorrow if already past `now`. `.timestamp()` → epoch.
   - **Optional precision (3.9+ only):** if `zoneinfo` imports and the captured
     TZ name resolves, compute in that zone instead. Never a hard dependency;
     any failure falls back to local.
4. Clamp: if the target is in the past or beyond `now + CAP`, return `None`.

Design note: this is deliberately forgiving. Detection is exact; scheduling is
a nice-to-have that degrades to "no reset ping" whenever the time can't be
parsed confidently.

### Spawn (from the hook)

`recovery.spawn(base_dir, key, target_epoch)`:

1. Atomically claim `state/usage_limit/<key>.sleeper` via `open(path, "x")`.
   Already exists → a sleeper for this window is already scheduled → return
   (no second process).
2. On winning the claim, launch a **detached** child:
   `subprocess.Popen([sys.executable, "-m", "claude_code_notify.recovery",
   "--base-dir", base_dir, "--window", key, "--target", str(int(target_epoch))],
   start_new_session=True, stdin/stdout/stderr=DEVNULL)`.
   - `start_new_session=True` detaches from the controlling terminal so the
     process survives Claude Code / terminal exit; the short-lived hook returns
     immediately and the child reparents to init, which reaps it on exit — **no
     zombie**.
   - **No secrets on argv** — the child re-loads `config.env` at fire time and
     computes destinations itself, so no bot token appears in `ps`.
3. Any spawn failure is caught and `_debug`-logged; the hook never raises.

### Sleeper process (`python3 -m claude_code_notify.recovery`)

- **Bounded wall-clock loop** (not a single long `sleep`):
  ```
  deadline = start + CAP
  while now() < target and now() < deadline:
      if <key>.done exists: exit(0)      # already broadcast elsewhere
      sleep(min(target - now(), 60))
  ```
  Re-checking every ≤60s self-corrects after machine suspend and lets the
  process exit early if the window is already handled.
- `CAP = 8 days` (covers the weekly reset's ≤7-day horizon plus margin). A
  sleeper can therefore **never** outlive the cap regardless of a bad parse —
  the guard against an unbounded "resident" process.
- At `target`: atomically create `state/usage_limit/<key>.done`; the creating
  process broadcasts the reset message via `broadcast.send_all(config,
  build_message("usage-limit-reset", ...))` to all distinct destinations,
  exactly once; then exits.
- Writes its PID to `state/usage_limit/<key>.pid` on start and removes it on
  exit, so uninstall can find and kill a live sleeper.
- The process is **inert** while waiting: it holds no transcript fd and no
  network connection, ~0% CPU, ~15 MB RSS, for one window at most.
- **No hook fallback.** If the sleeper is killed (reboot/logout), the reset
  ping is simply missed — an accepted "nice-to-have, miss-is-a-miss" tradeoff
  chosen by the product owner. Nothing re-fires it later.

## State layout (global, not per-session)

Under `base_dir/state/usage_limit/`, all files `chmod 600`:

| File | Written by | Purpose |
|---|---|---|
| `<key>.hit` | hook (`claim_hit`) | one hit broadcast per window |
| `<key>.sleeper` | hook (`spawn`) | one sleeper per window |
| `<key>.pid` | sleeper | PID for uninstall/kill; removed on exit |
| `<key>.done` | sleeper | reset broadcast sent; sleeper exit signal |

Usage-limit state is **account-global**, so it lives in a fixed
`usage_limit/` subdir keyed by window hash, independent of `session_id`
(unlike the per-session `<session>.state.json` / `.marker`). A best-effort GC
on hook entry removes `usage_limit/*` files older than 30 days so stale window
markers never accumulate.

## Uninstall

`install.sh --uninstall` (and its `installer.py remove` path) additionally:

- iterate `state/usage_limit/*.pid`, `kill` each live PID (guarded), then
- remove the `state/usage_limit/` directory.

Consistent with today's uninstall: hooks, code, state, and debug log go;
`config.env` is kept and its path printed.

## Error handling & security

- `hooks.py` keeps its absolute crash-free contract: detection, dedup, spawn,
  parsing, and every send are guarded; failures no-op (and `_debug`-log when
  `NOTIFY_DEBUG` is on).
- Secrets are scrubbed from all error/log output exactly as §9, per
  destination token; **no token is ever passed on a command line** (sleeper
  re-loads config itself).
- Core stays testable without a live Claude Code session and without hitting
  real Telegram: `broadcast.send_all` takes an injectable `send`; sleeper
  timing logic (target computation, cap clamp, single-instance claim) is
  factored into pure functions tested with injected `now`, never a real sleep.

## Testing

- `test_usagelimit.py`:
  - `latest_usage_limit`: rate_limit envelope as last assistant entry →
    returns text; auth-error / absent-error / normal-finish → `None`; a
    rate_limit followed by a later normal assistant turn → `None`; trailing
    `queue-operation`/injected-`user` lines ignored.
  - `window_key`: same reset text → same key; different reset time → different
    key.
  - `parse_reset`: `9pm`, `7:50pm`, am/pm, with/without TZ suffix → correct
    next-occurrence epoch (injected `now`, both before and after the time
    today); unparseable/weekly-style text → `None`; past or beyond-cap →
    `None`.
- `test_broadcast.py`: `destinations` dedup (global + routes, `(bot,chat)`
  de-dup, route without chat_id skipped, route bot override vs. global);
  `send_all` calls the injected `send` once per distinct destination and
  continues past a raising destination.
- `test_hooks.py`: feature **off by default** → no usage-limit behavior,
  normal Stop path intact; feature on + last entry rate_limit → `send_all`
  gets the routed configs and the normal "finished" is **not** sent; second
  Stop in the same window → no second broadcast but still suppressed; `Stop`
  and `StopFailure` for one window → single broadcast.
- `test_recovery.py`: single-instance `spawn` (`O_EXCL` claim blocks the
  second); the wall-clock loop exits at `target`, exits early on `<key>.done`,
  and clamps at `CAP` — all with injected time, no real sleeping; PID file
  written then removed; no bot token appears in the constructed argv.
- `test_config.py`: `usage_limit` / `usage_limit_reset` load with correct
  defaults (`False` / `True`) and env overrides; backward-compat unchanged.

## Versioning

Purely additive and opt-in (off by default) → **MINOR** bump per semver §7 →
target **v0.4.0**.

## Docs to update (implementation step)

- Product doc: a new architecture subsection (detection signal, broadcast
  semantics, the two switches with defaults, background-process lifecycle and
  the 8-day cap, uninstall killing the sleeper); move nothing from the roadmap
  that isn't this.
- `README.md` configuration section (both keys, the hit-only mode, the
  zero-token note). **Related work:** no external code is borrowed —
  detection was derived by inspecting real local transcripts — but if any
  auto-resume / limit-detection project is consulted during implementation it
  must be credited here.
- `CHANGELOG.md` (v0.4.0 entry).
- **Requires user sign-off during implementation:** `CLAUDE.md` core rules
  currently frame the tool around per-hook, no-daemon behavior. This feature
  adds an opt-in transient background process (the reset sleeper). A one-line
  core-rule addition should record: the feature is off by default; the reset
  sleeper is the one sanctioned background process, bounded by an 8-day cap and
  killed on uninstall. Proposed for review, not made unilaterally.

## Out of scope

- Any LLM/token use — the feature is purely local + Telegram HTTP.
- A persistent daemon, OS scheduler (`at`/`systemd`/`launchd`/cron), or a
  hook-based fallback for a killed sleeper — reset ping is best-effort only.
- Telegram-side scheduling — the Bot API has no `schedule_date`; only the
  client/MTProto (user-account) API can schedule, which is out of scope.
- Weekly-limit reset-time parsing until a real sample verifies its text format
  (weekly limits still get the hit broadcast; only their reset ping is
  deferred).
- Per-destination rate-limit windows or message templates.
