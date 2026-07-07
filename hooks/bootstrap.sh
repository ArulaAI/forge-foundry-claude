#!/usr/bin/env bash
set -euo pipefail
: "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"
ROOT="${CLAUDE_PLUGIN_ROOT}"
LOCK="$ROOT/engine/VENDOR_LOCK.json"
LOCK_SHA="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha"])' "$LOCK" 2>/dev/null || echo none)"

# The venv lives OUTSIDE the plugin cache dir: the plugin is re-cloned on
# /plugin update, which would wipe a venv built under the plugin root. Key it on
# the vendored source SHA so a re-vendor naturally lands in a fresh venv (and old
# SHA-keyed venvs can be GC'd). The engine source itself stays in the plugin dir;
# only the venv (and its editable install pointer) moves out.
VENV_HOME="${FORGE_FOUNDRY_VENV_HOME:-$HOME/.cache/forge-foundry-claude}"
VENV="$VENV_HOME/venv-$LOCK_SHA"

# Fast path: the SHA is implicit in the dirname, so an executable forge-validate
# at the SHA-keyed path means this SHA is already bootstrapped.
if [ -x "$VENV/bin/forge-validate" ]; then
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
mkdir -p "$VENV_HOME"
rm -rf "$VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -e "$ROOT/engine"
echo "forge-foundry-claude: engine ready." >&2
