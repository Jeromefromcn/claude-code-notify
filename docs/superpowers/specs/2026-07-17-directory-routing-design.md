# Design: directory-based notification routing

## Goal

Route each notification to a different Telegram destination based on the
session's working directory (`cwd`), so notifications from different project
trees land in different Telegram chats (and optionally different bots). A
configured directory applies to its whole subtree; a deeper configuration
overrides a shallower one; a subtree can be muted entirely.

This is opt-in and fully backward-compatible: with no routes configured the
tool behaves exactly as today, sending everything to the single global
destination.

## What a "route" is

A route maps an **absolute directory** to a notification **destination**:

- a `chat_id` (required unless the route is muted), and
- an optional `bot_token` that overrides the global bot for that subtree
  (absent → the subtree uses the global bot), or
- a `mute` flag that suppresses all notifications for that subtree.

The global `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` remain **required** and
serve as the catch-all destination for any `cwd` that matches no route. This
preserves current behavior for existing users and guarantees there is always
a fallback.

Routing is evaluated **per hook event**, using that event's `cwd`. All three
notification kinds (`finished`, `error`, `needs-input`) route by the same
`cwd`; a muted subtree suppresses all three.

## Config schema (`config.env`)

Global default (unchanged, still required):

```env
TELEGRAM_BOT_TOKEN=123:ABC
TELEGRAM_CHAT_ID=999
```

Routes are expressed as indexed keys. One `ROUTE_<n>_*` group describes one
route (`<n>` is any integer; groups need not be contiguous):

| Key | Required | Meaning |
|---|---|---|
| `ROUTE_<n>_DIR` | yes | Absolute directory path |
| `ROUTE_<n>_CHAT_ID` | yes, unless muted | Target chat for this subtree |
| `ROUTE_<n>_BOT_TOKEN` | no | Override bot for this subtree; absent → global bot |
| `ROUTE_<n>_MUTE` | no | `true` → suppress notifications for this subtree |

Example:

```env
TELEGRAM_BOT_TOKEN=123:ABC   # global default bot
TELEGRAM_CHAT_ID=999         # catch-all for unmatched cwd

ROUTE_1_DIR=/home/me/work
ROUTE_1_CHAT_ID=111

ROUTE_2_DIR=/home/me/work/acme
ROUTE_2_CHAT_ID=222
ROUTE_2_BOT_TOKEN=987:XYZ    # this subtree uses a different bot

ROUTE_3_DIR=/home/me/scratch
ROUTE_3_MUTE=true            # mute this whole subtree
```

Routes are read from `config.env` only. The existing environment-variable
override path (`load()`) continues to cover just the global keys
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_API_BASE`,
`NOTIFY_RATELIMIT_SECONDS`, `NOTIFY_DEBUG`) — routes are not env-overridable.

## Route parsing (`routing.py`)

New module, single-purpose, unit-testable in isolation.

```python
@dataclass
class Route:
    dir: str                  # realpath-normalized absolute directory
    chat_id: Optional[str]    # None only when muted
    bot_token: Optional[str]  # None -> use the global bot
    mute: bool

def parse_routes(merged):
    """Parse ROUTE_<n>_* keys into a list of Route. Never raises."""
    ...
```

The project floor is `requires-python = ">=3.8"` and the existing modules use
no `X | None` unions or `list[...]` subscripts in runtime annotations (both
raise at import time on 3.8/3.9, which would break the hook). Keep this module
3.8-safe: use `typing.Optional` / `typing.List`, or plain untyped/bare-`list`
annotations, never PEP 585/604 syntax.

`parse_routes` takes the already-merged flat dict (output of
`parse_env_file` plus env overrides) and:

1. Collects keys matching `ROUTE_<n>_(DIR|CHAT_ID|BOT_TOKEN|MUTE)` and
   buckets them by the integer `<n>`.
2. For each bucket, builds a `Route`:
   - Missing `DIR` → **skip** the route (it has no key to match on).
   - `mute = _truthy(MUTE)` (reuses the existing `_truthy` helper).
   - Not muted and missing `CHAT_ID` → **skip** (nowhere to send).
   - `dir` is realpath-normalized (see [Path normalization](#path-normalization)).
3. Orders routes by `<n>` ascending.
4. De-duplicates by normalized `dir`: if two routes normalize to the same
   directory, the one with the **higher index wins** (last-wins). This is the
   "same directory configured twice" rule, and it is trivial to implement by
   letting later entries overwrite earlier ones in an insertion-ordered map.

`parse_routes` **never raises**. Malformed or incomplete route groups are
skipped (and logged when `NOTIFY_DEBUG` is on, via the caller). A bad route
must never break config loading or the user's turn.

## Resolution algorithm (`routing.py`)

```python
@dataclass
class Resolution:
    muted: bool
    bot_token: Optional[str]   # set when not muted
    chat_id: Optional[str]     # set when not muted

def resolve(cwd, routes, default_bot_token, default_chat_id):
    """Returns a Resolution. Never raises."""
    ...
```

Steps:

1. Normalize `cwd` (realpath). If `cwd` is empty/missing, there is nothing to
   match on → return the global default destination.
2. **Path-segment-aware prefix match.** A route matches when
   `norm_cwd == route.dir` or `norm_cwd.startswith(route.dir + os.sep)`. The
   trailing-separator check is what stops `/a/b` from matching `/a/bc`. A
   route whose normalized `dir` is the filesystem root matches everything.
3. Among all matching routes, pick the one with the **longest** `route.dir`.
   Because every matching route is an ancestor of `cwd` on the same
   root-to-`cwd` chain, the longest string is necessarily the deepest / most
   specific — this is what makes a lower directory override a higher one.
   Post-dedup, no two distinct matching routes can tie on length.
4. If the winning route is muted → `Resolution(muted=True, ...)`.
5. Otherwise → `Resolution(muted=False, bot_token=route.bot_token or
   default_bot_token, chat_id=route.chat_id)`.
6. No match → global default destination
   (`bot_token=default_bot_token, chat_id=default_chat_id`).

Mute and send routes share the exact same longest-prefix logic, so a muted
parent with a deeper normal child (or vice versa) resolves correctly by
specificity without any special casing.

`resolve` **never raises**; a normalization failure degrades to the global
default (see error handling).

### Worked example

Config: `R1=/home/me/work→111`, `R2=/home/me/work/acme→222`,
`R3=/home/me/scratch→mute`, global default chat `999`.

| `cwd` | Winning route | Result |
|---|---|---|
| `/home/me/work` | R1 | chat 111 |
| `/home/me/work/acme/sub` | R2 (deeper than R1) | chat 222 |
| `/home/me/work/other` | R1 | chat 111 |
| `/home/me/workspace` | none (segment boundary: `work` ≠ `workspace`) | global 999 |
| `/home/me/scratch/x` | R3 | **muted — no send** |
| `/home/me/random` | none | global 999 |

## Path normalization

Both `cwd` and every `route.dir` are normalized with `os.path.realpath` so
they live in the same space (resolves `..`, symlinks, and trailing
separators; makes a symlinked project path match a route written against
either the link or its target). `route.dir` is normalized once at parse time;
`cwd` is normalized at resolve time.

`realpath` on a non-existent path still normalizes lexically for the
non-existent tail, which is fine for a route dir that does not exist yet. If
`realpath` raises for any reason, fall back to
`os.path.normpath(os.path.abspath(path))` (lexical only). Normalization must
never raise out of `parse_routes` / `resolve`.

## Integration

| File | Change |
|---|---|
| `routing.py` (new) | `Route`, `Resolution`, `parse_routes`, `resolve` |
| `config.py` | `Config` gains a `routes` field (`field(default_factory=list)`, bare-`list` annotation for the 3.8 floor); `load()` calls `parse_routes(merged)` |
| `hooks.py` | Each handler resolves the destination from `cwd`; muted → `_debug` + return; otherwise send to the resolved destination |
| `notifier.py` | `send` accepts an explicit destination; scrubs errors with the **token actually used** |
| `__main__.py` | Optional `--check-route [dir]` diagnostic |

### `config.py`

`Config` gains a `routes` field defaulting to an empty list (so any existing
construction without routes keeps working). `load()` builds
`routes = routing.parse_routes(merged)` after the existing merge, before
constructing `Config`. Global token/chat_id stay required (`ConfigError` as
today — caught by `hooks.run`'s outer handler, so a missing global config
no-ops rather than crashing the turn).

### `hooks.py`

Each of `handle_stop`, `handle_stop_failure`, `handle_permission_request`
already reads `cwd = payload.get("cwd", "")`. Each now, at the top:

```python
res = routing.resolve(cwd, config.routes, config.bot_token, config.chat_id)
if res.muted:
    _debug(config, f"{event} cwd={cwd} muted — no send")
    return
```

For `handle_stop`, resolving and the mute short-circuit happen **before** the
pending/rate-limit work: a muted subtree never notifies, so there is no
reason to compute pending or touch the rate-limit marker. (Consequence: a
muted session's incremental state offset does not advance; harmless, since it
never notifies.) The final send passes the resolved destination:

```python
notifier.send(config, message, bot_token=res.bot_token, chat_id=res.chat_id)
```

Debug lines may log the resolved `chat_id` and mute decision but **never** a
bot token (global or per-route).

### `notifier.py`

```python
def send(config, text, bot_token=None, chat_id=None):
    bot_token = bot_token or config.bot_token
    chat_id = chat_id or config.chat_id
    ...
    error_message = scrub(str(exc), bot_token)   # scrub the token in use
```

The signature stays backward-compatible (existing `send(config, text)` calls
keep working via the `None` defaults). **Security-critical:** the error path
must scrub the `bot_token` actually used for the request, not
`config.bot_token`, because a per-route token appears in the request URL and
would otherwise leak in an error string. This keeps the §9 "secret scrubbing"
guarantee intact for per-route tokens.

### `--check-route` diagnostic (optional but recommended)

`python3 -m claude_code_notify --check-route [dir]` (defaults to the current
directory) loads config, resolves the directory, and prints: the winning
route dir (or "no match → global default"), the resulting `chat_id`, whether
the bot is the global one or a per-route override, and whether it is muted.
It never prints a full bot token (masked to `***`). This is the primary way a
user answers "why didn't I get a notification in this directory."

## Backward compatibility & versioning

- No `ROUTE_*` keys → zero routes → identical to today's behavior.
- Global token/chat_id still required → there is always a catch-all.
- Purely additive, opt-in behavior → **MINOR** bump per semver §7 →
  target **v0.3.0**.

## Error handling principles

- `parse_routes` and `resolve` never raise; malformed routes are skipped,
  normalization failures degrade to lexical/global-default.
- The `hooks.py` crash-free guarantee is preserved: nothing on the routing
  path can propagate an exception into the user's turn.
- Losing a single malformed route is acceptable degradation; crashing or
  dropping an otherwise-valid notification is not.

## Testing

All tests stay decoupled from real Telegram and from a live Claude Code
session (routes injected via config, `cwd` injected via payload dicts).

- `test_routing.py`:
  - `parse_routes`: indexed grouping; non-contiguous indices; missing `DIR`
    skipped; not-muted-and-missing-`CHAT_ID` skipped; `MUTE` truthiness;
    optional `BOT_TOKEN`; duplicate normalized `dir` → higher index wins.
  - `resolve`: exact-dir match; subtree inheritance (cwd deeper than dir);
    deeper route overrides shallower (longest-prefix); segment-boundary
    safety (`/a/b` does not match `/a/bc`); no match → global default; muted
    subtree; muted-parent-with-normal-deeper-child and the reverse; per-route
    `bot_token` override vs. fallback to global; realpath/trailing-slash
    normalization; empty/missing `cwd` → global default.
- `test_config.py`: routes load into `Config`; backward-compat (no routes)
  unchanged.
- `test_hooks.py`: matching route → `notifier.send` receives the routed
  `chat_id`/`bot_token`; muted route → `notifier.send` is **not** called;
  no route → global default used. Use a fake/captured notifier.
- `test_notifier.py`: `send` with an explicit destination hits the right
  bot/chat; an error involving a per-route token is scrubbed.

## Docs to update (implementation step)

- Product doc §5.3 (config schema example with routes) and §11 (this item
  moves from roadmap to landed; reword).
- `README.md` configuration section.
- `CHANGELOG.md` (v0.3.0 entry).
- **Requires user sign-off during implementation:** `CLAUDE.md` currently
  states "v1 is Telegram-only, global-install-only … Don't add … project-level
  install without checking the roadmap." This feature is *not* project-level
  install (no per-project files, no `--local`), but it delivers the intent of
  roadmap §11, so that line and the core-rules list need a one-line update to
  reflect directory routing. This wording change will be proposed for review,
  not made unilaterally.

## Out of scope

- Per-project `config.env` files / `--local` install (the distinct roadmap
  §11 mechanism; this central table addresses the same need without it).
- Environment-variable overrides for routes.
- A CLI to add/edit routes (`--add-route`); routes are hand-edited for now.
- Routing on anything other than a literal directory prefix (no globs, no
  git-repo / project-name keys).
- Per-route rate-limit windows or message templates.
