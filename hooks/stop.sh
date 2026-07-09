#!/usr/bin/env bash
# Thin membrane: forward Claude Code's stdin hook JSON unchanged to the core.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m claude_code_notify.hooks stop
