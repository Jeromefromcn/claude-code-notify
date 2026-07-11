#!/usr/bin/env bash
# Thin membrane: idempotent install / upgrade / uninstall entry point for
# claude-code-notify. All settings.json editing goes through installer.py
# (json module), never sed/string surgery.
set -euo pipefail

# CLAUDE_NOTIFY_TARBALL_BASE is test-only (like CLAUDE_NOTIFY_HOME): lets
# tests point the download path at a file:// tarball instead of GitHub, so
# the actual curl+tar install path can be exercised without network access.
REPO_TARBALL="${CLAUDE_NOTIFY_TARBALL_BASE:-https://github.com/Jeromefromcn/claude-code-notify/archive}"
BASE_DIR="${CLAUDE_NOTIFY_HOME:-$HOME/.claude/claude-code-notify}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
MODE="install"
VERSION="main"
VERSION_EXPLICIT="0"
NONINTERACTIVE="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE="uninstall" ;;
    --non-interactive) NONINTERACTIVE="1" ;;
    --version) shift; [ $# -gt 0 ] || { echo "--version requires an argument" >&2; exit 2; }; VERSION="$1"; VERSION_EXPLICIT="1" ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

if [ "$MODE" = "uninstall" ]; then
  python3 "$BASE_DIR/claude_code_notify/installer.py" remove "$SETTINGS"
  rm -rf "$BASE_DIR/claude_code_notify" "$BASE_DIR/hooks" "$BASE_DIR/state" "$BASE_DIR/debug.log"
  echo "Removed hook entries, code, state, and debug log. config.env kept at $BASE_DIR/config.env (delete manually if desired)."
  exit 0
fi

mkdir -p "$BASE_DIR"

# Obtain the package. If running from a checkout (install.sh sits next to the
# package), copy locally; otherwise download the pinned tarball.
#
# BASH_SOURCE is unset when the script runs via `curl | bash` (fed through
# stdin, not executed as a file), and this script runs under `set -u`. Only
# derive SRC_DIR from BASH_SOURCE when it actually points at a real file;
# otherwise fall straight through to the download branch. Do NOT fall back to
# deriving SRC_DIR from the caller's $PWD — that would make the local-checkout
# detection depend on whatever directory happened to be current, which could
# silently pick up an unrelated (or stale) claude_code_notify/ directory there.
SRC_DIR=""
if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SRC_DIR" ] && [ -d "$SRC_DIR/claude_code_notify" ]; then
  cp -R "$SRC_DIR/claude_code_notify" "$SRC_DIR/hooks" "$BASE_DIR/"
else
  # Default (no --version given): resolve the latest published GitHub release
  # tag so a plain install/upgrade tracks "latest release", not the moving
  # tip of main. An explicit --version <tag> always wins and skips this.
  if [ "$VERSION_EXPLICIT" = "0" ]; then
    LATEST_TAG="$(curl -fsSL https://api.github.com/repos/Jeromefromcn/claude-code-notify/releases/latest 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name',''))" 2>/dev/null || true)"
    if [ -n "$LATEST_TAG" ]; then
      VERSION="$LATEST_TAG"
    else
      # GitHub API unreachable (offline, rate-limited) or no releases
      # published yet — warn instead of silently tracking main.
      echo "Warning: could not resolve latest release tag; falling back to 'main'." >&2
    fi
  fi

  # GitHub's archive/<ref>.tar.gz endpoint resolves branches, tags, and
  # commit SHAs uniformly in a single request — unlike archive/refs/heads/
  # or archive/refs/tags/, which require knowing the ref type up front.
  # VERSION is a branch only when explicitly overridden; by default (see
  # above) it's a release tag, so a heads-then-tags guess-and-fallback
  # would 404 on every ordinary install before its fallback quietly
  # succeeded — this single request has no such guess to get wrong.
  TMP="$(mktemp -d)"
  curl -fsSL "$REPO_TARBALL/$VERSION.tar.gz" -o "$TMP/pkg.tgz"
  tar -xzf "$TMP/pkg.tgz" -C "$TMP" --strip-components=1
  cp -R "$TMP/claude_code_notify" "$TMP/hooks" "$BASE_DIR/"
  rm -rf "$TMP"
fi
chmod +x "$BASE_DIR"/hooks/*.sh

# Config: keep existing (upgrade) or create (first install).
CONFIG="$BASE_DIR/config.env"
if [ -f "$CONFIG" ]; then
  echo "Existing config.env kept (upgrade)."
else
  if [ "$NONINTERACTIVE" = "1" ]; then
    : "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN for --non-interactive}"
    : "${TELEGRAM_CHAT_ID:?set TELEGRAM_CHAT_ID for --non-interactive}"
  else
    # stdin (fd 0) may be a curl|bash pipe, not a terminal — read the
    # prompts from the controlling tty explicitly so this works for
    # `curl -fsSL ... | bash` and not just `git clone && ./install.sh`.
    if [ ! -e /dev/tty ]; then
      echo "No terminal available to prompt for Telegram credentials." >&2
      echo "Re-run with --non-interactive and TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set." >&2
      exit 1
    fi
    read -r -s -p "Telegram bot token: " TELEGRAM_BOT_TOKEN </dev/tty; echo
    read -r -p "Telegram chat id: " TELEGRAM_CHAT_ID </dev/tty
  fi
  umask 177
  cat > "$CONFIG" <<EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
NOTIFY_RATELIMIT_SECONDS=120
NOTIFY_DEBUG=false
EOF
  chmod 600 "$CONFIG"
  echo "Wrote $CONFIG (chmod 600)."
fi

python3 "$BASE_DIR/claude_code_notify/installer.py" merge "$SETTINGS" "$BASE_DIR"

VER="$(python3 -c "import sys; sys.path.insert(0, '$BASE_DIR'); import claude_code_notify; print(claude_code_notify.__version__)")"
echo "claude-code-notify $VER installed. Test: echo '{}' | bash $BASE_DIR/hooks/stop.sh && echo ok"
