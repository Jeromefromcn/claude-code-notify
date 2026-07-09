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
NONINTERACTIVE="0"

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE="uninstall" ;;
    --non-interactive) NONINTERACTIVE="1" ;;
    --version) shift; VERSION="$1" ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

if [ "$MODE" = "uninstall" ]; then
  python3 "$BASE_DIR/claude_code_notify/installer.py" remove "$SETTINGS"
  rm -rf "$BASE_DIR/claude_code_notify" "$BASE_DIR/hooks"
  echo "Removed hook entries and code. config.env kept at $BASE_DIR/config.env (delete manually if desired)."
  exit 0
fi

mkdir -p "$BASE_DIR"

# Obtain the package. If running from a checkout (install.sh sits next to the
# package), copy locally; otherwise download the pinned tarball.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$SRC_DIR/claude_code_notify" ]; then
  cp -R "$SRC_DIR/claude_code_notify" "$SRC_DIR/hooks" "$BASE_DIR/"
else
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
    read -r -s -p "Telegram bot token: " TELEGRAM_BOT_TOKEN; echo
    read -r -p "Telegram chat id: " TELEGRAM_CHAT_ID
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
