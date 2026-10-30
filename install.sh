#!/usr/bin/env bash
# Install (or update) leandc. Two ways to run it:
#   curl -fsSL https://raw.githubusercontent.com/asif-r-porosh/leandc/main/install.sh | bash
#   ./install.sh                      from a clone: runs manual-install.sh next to it
# Piped, it downloads the repo (no git needed), runs manual-install.sh from a temp
# directory, then deletes that directory. The installed copy does not need it.
#   LEANDC_REF=v0.1.0   branch, tag or commit to install (default: main)
# LEANDC_HOME, LEANDC_CONFIG and LEANDC_PYTHON are passed through to manual-install.sh.
set -euo pipefail

REPO="asif-r-porosh/leandc"

main() {
  # In a checkout: hand over to the sibling installer, no download.
  local here="${BASH_SOURCE[0]:-}"
  if [ -n "$here" ] && [ -f "$(dirname "$here")/manual-install.sh" ]; then
    exec bash "$(dirname "$here")/manual-install.sh" "$@"
  fi

  # Piped (stdin is the script, no file on disk): fetch the source.
  command -v curl >/dev/null || { echo "install: curl is required" >&2; exit 1; }
  command -v tar  >/dev/null || { echo "install: tar is required"  >&2; exit 1; }

  local ref="${LEANDC_REF:-main}"
  local url="${LEANDC_TARBALL_URL:-https://github.com/$REPO/archive/$ref.tar.gz}"
  TMP="$(mktemp -d)"   # global on purpose: the EXIT trap runs after main returns
  trap 'rm -rf "$TMP"' EXIT

  echo "install: downloading $url"
  curl -fsSL "$url" | tar -xz -C "$TMP" --strip-components=1 \
    || { echo "install: download or unpack failed (check LEANDC_REF=$ref)" >&2; exit 1; }
  [ -f "$TMP/manual-install.sh" ] \
    || { echo "install: manual-install.sh not found in $ref (too old a ref?)" >&2; exit 1; }

  # </dev/null: the child must not read the rest of this script from the pipe.
  bash "$TMP/manual-install.sh" "$@" </dev/null
}

main "$@"
