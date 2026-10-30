#!/usr/bin/env bash
# Install (or update) leandc from this checkout into its own venv. Safe to re-run.
#   ./install.sh                         venv in ~/.local/share/leandc/venv
#   LEANDC_HOME=/opt/leandc ./install.sh   somewhere else
#   LEANDC_PYTHON=/path/to/python3.12 ./install.sh
# Needs Python 3.12+ with venv and pip. The installed copy does not depend on this checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${LEANDC_HOME:-$HOME/.local/share/leandc}"
CONFIG="${LEANDC_CONFIG:-$HOME/.config/leandc/config.toml}"

PY="${LEANDC_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.13 python3.14 python3; do
    p="$(command -v "$c" || true)"
    [ -n "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null && { PY="$p"; break; }
  done
fi
[ -n "$PY" ] || { echo "install: Python 3.12 or newer not found (set LEANDC_PYTHON=/path/to/python3.12)" >&2; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null || { echo "install: $PY has no venv/pip (Ubuntu: sudo apt install python3-venv)" >&2; exit 1; }

"$PY" -m venv --clear "$HOME_DIR/venv"   # stop a running server first; this rebuilds the venv
"$HOME_DIR/venv/bin/pip" install -q -r "$HERE/requirements.txt"
"$HOME_DIR/venv/bin/pip" install -q --no-deps --force-reinstall "$HERE"

if [ ! -e "$CONFIG" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  cp "$HERE/config.example.toml" "$CONFIG"
  chmod 600 "$CONFIG"
  echo "install: wrote $CONFIG from the example; edit target and [http] before serving"
fi

BIN="$HOME_DIR/venv/bin/leandc"
"$BIN" check --config "$CONFIG" || true
echo
echo "installed: $BIN"
echo "Claude desktop (Mac): command \"$BIN\", args [\"serve\", \"--config\", \"$CONFIG\"]"
