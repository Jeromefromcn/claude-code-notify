#!/usr/bin/env bash
# Thin membrane: idempotent install / upgrade / uninstall entry point for
# claude-code-notify. All settings.json editing goes through installer.py
# (json module), never sed/string surgery.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/Jeromefromcn/claude-code-notify/main"
REPO_TARBALL="https://github.com/Jeromefromcn/claude-code-notify/archive/refs"
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
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SRC_DIR/claude_code_notify" ]; then
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

  TMP="$(mktemp -d)"
  curl -fsSL "$REPO_TARBALL/heads/$VERSION.tar.gz" -o "$TMP/pkg.tgz" \
    || curl -fsSL "$REPO_TARBALL/tags/$VERSION.tar.gz" -o "$TMP/pkg.tgz"
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
