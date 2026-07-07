#!/usr/bin/env bash
set -euo pipefail
: "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"
ROOT="${CLAUDE_PLUGIN_ROOT}"
VENV="$ROOT/engine/.venv"
STAMP="$VENV/.forge-vendor-sha"
LOCK="$ROOT/engine/VENDOR_LOCK.json"
LOCK_SHA="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha"])' "$LOCK" 2>/dev/null || echo none)"

if [ -x "$VENV/bin/forge-validate" ] && [ "$(cat "$STAMP" 2>/dev/null || echo x)" = "$LOCK_SHA" ]; then
  exit 0
fi

if command -v python3.11 >/dev/null 2>&1; then PY=python3.11
elif command -v python3 >/dev/null 2>&1; then PY=python3
else
  echo "forge-foundry-claude: python3.11+ not found; PLB skills unavailable until it is installed." >&2
  exit 0
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
  echo "forge-foundry-claude: python 3.11+ required; found $($PY --version 2>&1)." >&2
  exit 0
fi

echo "forge-foundry-claude: bootstrapping engine venv (one-time)…" >&2
rm -rf "$VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -e "$ROOT/engine"
echo "$LOCK_SHA" > "$STAMP"
echo "forge-foundry-claude: engine ready." >&2
