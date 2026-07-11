# Design: bidirectional Telegram reply for permission requests (phase 1)

> **Status: Shelved (2026-07-12).** Not being implemented for now. Two
> hook-level constraints, confirmed against the official Claude Code hooks
> reference, made the scope too narrow to be worth it:
>
> - `AskUserQuestion` (multi-option + free-text decisions) cannot be
>   answered from a hook at all. It does trigger `PermissionRequest` (a
>   known Claude Code quirk — upstream issue
>   [anthropics/claude-code#15400](https://github.com/anthropics/claude-code/issues/15400),
>   closed "not planned"), exposing `tool_name`/`tool_input`, but the
>   `decision` output only supports `behavior: allow|deny` — allowing just
>   lets the tool run normally (falls back to the local terminal prompt);
>   denying just blocks the question outright. Neither answers it. An
>   upstream request to add hook support for this
>   ([anthropics/claude-code#15872](https://github.com/anthropics/claude-code/issues/15872))
>   was also closed "not planned". A production implementation would need
>   to special-case `tool_name == "AskUserQuestion"` and skip the
>   button/wait flow entirely for it (plain notify only).
> - For ordinary tool permission requests, `decision.behavior` supports
>   only `allow`/`deny` — there is no session-scoped "always allow" third
>   option (no `scope`/`remember` field in the schema). The only way to
>   express that would be a hook writing directly to
>   `.claude/settings.local.json`, bypassing the `decision` output
>   entirely — undocumented, races with Claude Code's own reads/writes of
>   that file, and not guaranteed stable across versions.
>
> With `AskUserQuestion` unreachable and no "always allow" option, the
> remaining scope (bare Allow/Deny on plain tool permission prompts) was
> judged not worth the implementation cost. Revisit if Claude Code ever
> exposes richer hook-level control over `AskUserQuestion` or session-scoped
> permission grants. The design below is kept as-is as a record of what was
> explored; it does not reflect any of the above constraints in its body.

## Goal

Today `claude-code-notify` is push-only: it tells you Claude needs a
permission decision, but you still have to go back to the terminal to answer
it. This phase lets you answer **allow / deny** directly from the Telegram
message, so a permission request no longer requires being at the keyboard.

Free-text follow-ups to a finished turn (replying to a `Stop` notification
with instructions for what to do next) are a related but separate capability,
deferred to a phase-2 spec — see "Deferred: phase 2" below.

## Why this needs no daemon

`PermissionRequest` is already a **blocking** hook: Claude Code pauses and
waits for the hook process to exit before proceeding, for up to `timeout`
seconds (default 600, configurable per-hook in `settings.json`). Its JSON
output can carry a decision:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": { "behavior": "allow" }
  }
}
```

(`behavior` is `"allow"` or `"deny"`; there is no reason/feedback field on
this hook — see "Not doing: deny-with-reason" below.)

So the hook script itself — already alive and blocking — can send the
Telegram message, poll for a reply, and emit this JSON before exiting. No
persistent process is introduced; this preserves the product doc's §2
non-goal of "no daemon."

## Reply transport: inline keyboard only

The Telegram message includes two inline-keyboard buttons, **Allow** and
**Deny**. Each button's `callback_data` carries a short opaque token unique
to this request (e.g. `f"perm:{session_id}:{tool_use_id}"`, truncated to
Telegram's 64-byte `callback_data` limit via a hash if needed).

Free text is **not** parsed as a reply to a permission request in this
phase — see "Not doing" below for why.

## Retrieving replies: `getUpdates` long polling

Telegram delivers button taps as `callback_query` updates through the same
`getUpdates` long-poll API used for messages — no webhook, no inbound port.
The hook calls:

```
GET {api_base}/bot{token}/getUpdates?offset={N}&timeout=25
```

which blocks server-side for up to 25s waiting for new updates, then returns.
The hook loops this until it either finds a `callback_query` whose `data`
matches its own token, or its own reply-wait budget is spent (see "Timeout &
fallback").

Each returned update carries an `update_id`; after processing a batch, the
next call passes `offset = max(update_id) + 1` so Telegram doesn't redeliver
already-seen updates. This offset must be **persisted across hook
invocations** (a fresh `python3 -m claude_code_notify.hooks` process starts
with no memory of prior polling), so it lives in a shared state file, not
in-memory.

## Concurrency: single-poller coordination

Telegram allows only **one** concurrent long-poll per bot token — a second
overlapping `getUpdates` call gets `409 Conflict`. Since you run multiple
Claude Code sessions in parallel, two `PermissionRequest` hooks can easily be
waiting at the same time. Design:

- A lock file, `<base_dir>/telegram-poll.lock`, guarded with `flock`
  (`fcntl.flock`, non-blocking acquire).
- Whoever acquires the lock becomes **the poller** for this round: it runs
  the `getUpdates` loop, persists the offset (`<base_dir>/telegram-offset.json`),
  and for every `callback_query` it sees, writes the decision into a shared
  inbox file keyed by token — `<base_dir>/telegram-inbox/<token>.json` — via
  atomic rename (write to a `.tmp` path, then `os.replace`), so a concurrent
  reader never observes a partial write.
- A hook that fails to acquire the lock is **not** blocked — it just polls
  its own inbox file locally (`Path.exists()` + read, no network) every
  ~0.5s. This is cheap and requires no coordination beyond the filesystem.
- The lock holder keeps polling until *either* its own token resolves *or*
  its reply-wait budget expires — not just until its own answer arrives —
  so other waiters aren't abandoned the moment the lock holder is done.
  Before exiting, it releases the lock so a new poller can take over if
  other requests are still waiting.
- If the lock holder crashes without releasing (process killed, machine
  sleep), `flock` releases automatically on process exit, so a waiting hook
  retrying the non-blocking acquire will succeed on its next attempt (poll
  interval ~0.5s already covers this — no separate stale-lock detection
  needed).

This mirrors the existing pending-tracker pattern: correctness through a
small persisted-state file plus atomic writes, not a separate process.

## Security: chat_id allowlist

Any `callback_query` (or, in phase 2, any message) is only honored if
`update["callback_query"]["from"]["id"]` — the Telegram user id of whoever
tapped the button — corresponds to the configured chat. Since `TELEGRAM_CHAT_ID`
is the chat this bot already only talks to, and a private bot conversation
only has one counterpart, this is mostly inherent — but the poller still
explicitly checks the sender against `config.chat_id` and **discards** any
update that doesn't match, rather than assuming a private chat can only ever
contain one sender. This must happen before an update is ever written to the
shared inbox.

## Timeout & fallback behavior

- New config key `NOTIFY_PERMISSION_REPLY_TIMEOUT_SECONDS` (default `55`,
  comfortably under the hook's own 600s ceiling, leaving headroom for
  install to raise the hook's `timeout` if a longer wait is wanted later).
- If no matching reply arrives within the budget, the hook emits **no
  decision JSON** and exits 0 — Claude Code falls back to its normal local
  permission prompt exactly as it does today. A slow or absent Telegram
  reply must never itself deny or hang the request.
- Any network error calling Telegram (timeout, DNS failure, non-2xx) is
  caught the same way `notifier.send` already handles errors: logged via
  `NOTIFY_DEBUG` if enabled, otherwise silently swallowed, decision omitted.
  Consistent with the existing rule that `hooks.py` never raises or exits
  non-zero on internal failure.

## Config additions (`config.env`)

```env
NOTIFY_PERMISSION_REPLY=true                    # opt-out switch, default on
NOTIFY_PERMISSION_REPLY_TIMEOUT_SECONDS=55      # optional override
```

Setting `NOTIFY_PERMISSION_REPLY=false` restores today's exact behavior
(notify only, hook returns immediately) for this hook.

## Upgrade notice

Existing installs default this feature **on**, which is a behavior change
(the permission hook now visibly waits instead of returning instantly). The
installer must print an explicit one-time notice on upgrade when it detects
an existing `config.env` that predates this key:

```
claude-code-notify: new in this version — permission requests now wait up to
55s for an Allow/Deny reply from Telegram before falling back to the local
prompt. Set NOTIFY_PERMISSION_REPLY=false in
~/.claude/claude-code-notify/config.env to disable.
```

(Exact installer plumbing — detecting "predates this key" — is an
implementation detail for the plan, not this spec.)

## New/changed components

| Module | Change |
|---|---|
| `telegram_reply.py` (new) | `getUpdates` client, offset persistence, chat_id filtering, inbox read/write. |
| `reply_coordinator.py` (new) | Lock acquisition, poller/waiter roles, `wait_for_reply(token, budget_seconds) -> "allow" \| "deny" \| None`. |
| `notifier.py` | New `send_with_buttons(config, text, buttons)` sending `reply_markup` with an inline keyboard; existing `send()` unchanged for `Stop`/`StopFailure`. |
| `config.py` | New `Config` fields `permission_reply_enabled`, `permission_reply_timeout_seconds`. |
| `hooks.py` | `handle_permission_request` builds a token, sends the buttoned message, calls `reply_coordinator.wait_for_reply(...)`, and returns the decision JSON (new: `run()` must now support hooks that produce stdout JSON, not just side effects). |

## Data flow

```
PermissionRequest fires
  → hooks.py handle_permission_request
    → config.load()
    → if not config.permission_reply_enabled: notify only (today's behavior), return
    → token = build_token(session_id, tool_use_id)
    → notifier.send_with_buttons(config, "...needs your input...", [Allow, Deny])
    → decision = reply_coordinator.wait_for_reply(token, config.permission_reply_timeout_seconds)
        → try to acquire poll lock
          → held: loop getUpdates(offset, timeout=25) until token resolves or budget spent;
                   write every valid callback_query to the shared inbox; persist offset
          → not held: poll own inbox file locally until token resolves or budget spent
    → if decision is not None: emit {"hookSpecificOutput": {..., "decision": {"behavior": decision}}}
    → else: emit nothing (exit 0) — local prompt takes over as today
```

## Testing

- Fixtures/fakes for the Telegram transport (same pattern as `notifier`
  tests today — inject a fake HTTP layer, no real network):
  - reply arrives before timeout → Allow
  - reply arrives before timeout → Deny
  - no reply → timeout → no decision JSON emitted
  - `callback_query` from a non-configured chat id → ignored, request still
    times out
  - two concurrent `wait_for_reply` calls (simulated via two coordinator
    instances sharing a temp `base_dir`): one becomes poller, the other
    reads its answer from the inbox without ever calling `getUpdates`
  - poller crash mid-wait (lock file deleted mid-test) → a waiting instance
    acquires the lock and continues polling
  - offset persistence: a second `wait_for_reply` invocation resumes from
    the persisted offset rather than re-fetching already-seen updates
- `test_hooks.py`: extend `handle_permission_request` tests to assert the
  emitted JSON matches the resolved decision, and that `NOTIFY_PERMISSION_REPLY=false`
  reproduces exactly today's side-effect-only behavior.
- Installer test: upgrading a `config.env` lacking `NOTIFY_PERMISSION_REPLY`
  prints the upgrade notice once; a fresh install's `config.env` includes
  the key with its default.

## Not doing (this phase)

- **Deny-with-reason.** `PermissionRequest`'s decision schema has no field
  for attaching free text to Claude when denying (unlike `Stop`'s `reason`).
  Adding this would mean hooking `PreToolUse` instead, which fires on every
  tool call rather than only real permission dialogs — a materially
  different (and noisier) integration point. Out of scope here; revisit
  only if Claude Code adds a reason field to `PermissionRequest` itself.
- **Free-text replies of any kind.** Only the two button values are
  accepted; a text reply to a permission-request message is ignored (it
  doesn't match either button's callback token, so it simply never resolves
  the wait — the request falls back to the local prompt on timeout, exactly
  as if no reply had come at all).
- **Webhook-based delivery.** Requires a public HTTPS endpoint and a
  persistent process; rejected in favor of poll-during-block (see "Why this
  needs no daemon").

## Deferred: phase 2 (separate spec)

Free-text follow-ups on `Stop` — replying to a "finished" notification with
what to do next, fed back via `decision: "block", reason: "<text>"` — reuse
the same `telegram_reply.py`/`reply_coordinator.py` building blocks (long
polling, lock, inbox, chat_id filtering) but need their own design pass for:
quick-reply buttons alongside free text, how long `Stop` should wait by
default, and interaction with the existing rate-limit/dedup logic. Not
addressed here.
