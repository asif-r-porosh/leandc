#!/usr/bin/env bash
# leandc bootstrap (D9, D10): same toolchain on the Mac and the Ubuntu VM.
#
#   ./bootstrap.sh                 install the Brewfile, Python 3.12, then build the bin dir
#   ./bootstrap.sh --links-only    only (re)build the bin dir from what is already installed
#   LEANDC_BIN=/path ./bootstrap.sh   bin dir (default ~/.local/share/leandc/bin)
#
# The bin dir is a directory of symlinks. leandc puts it first on PATH for every command, so Claude
# types plain names (sed, grep, find, fd, bat) and always gets the GNU/modern flavor on both machines.
# ssh, ps, pgrep, lsof are deliberately NOT linked: the system versions are used.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${LEANDC_BIN:-$HOME/.local/share/leandc/bin}"
LINKS_ONLY=0
[ "${1:-}" = "--links-only" ] && LINKS_ONLY=1

say() { printf '%s\n' "$*"; }
die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }

# ---- facts -------------------------------------------------------------------
OS="$(uname -s)"; ARCH="$(uname -m)"
say "os=$OS arch=$ARCH"
if [ "$OS" = "Linux" ]; then
  GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
  say "glibc=${GLIBC:-unknown}"
  if [ "$LINKS_ONLY" = 0 ]; then
    [ "$ARCH" = "x86_64" ] || say "warning: Homebrew on Linux is best supported on x86_64 (this is $ARCH)"
    awk -v v="${GLIBC:-0}" 'BEGIN { split(v, a, "."); exit !((a[1] > 2) || (a[1] == 2 && a[2] >= 35)) }' \
      || die "Homebrew on Linux needs glibc 2.35 or newer (found ${GLIBC:-unknown})"
  fi
elif [ "$OS" != "Darwin" ]; then
  die "unsupported OS: $OS"
fi

# ---- locate brew -------------------------------------------------------------
BREW=""
for c in "$(command -v brew || true)" /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
  [ -n "$c" ] && [ -x "$c" ] && { BREW="$c"; break; }
done

if [ "$LINKS_ONLY" = 0 ]; then
  [ -n "$BREW" ] || die "Homebrew not found. Install it from https://brew.sh (needs build-essential procps curl file git on Ubuntu), then rerun."
  "$BREW" bundle --file "$HERE/Brewfile"
  command -v uv >/dev/null 2>&1 || PATH="$("$BREW" --prefix)/bin:$PATH"
  uv python install 3.12
fi

PREFIX=""
[ -n "$BREW" ] && PREFIX="$("$BREW" --prefix)"

# ---- bin dir -----------------------------------------------------------------
mkdir -p "$BIN"
find "$BIN" -maxdepth 1 -type l -delete   # rebuild from scratch; only symlinks live here

# Prefer, in order: brew's GNU "gnubin" dirs, brew's own bin (Linux) or g-prefixed name (Mac), then PATH.
find_tool() {
  local t="$1" d c
  if [ -n "$PREFIX" ]; then
    for d in "$PREFIX"/opt/{coreutils,findutils,gnu-sed,grep,gawk,gnu-tar,diffutils}/libexec/gnubin; do
      [ -x "$d/$t" ] && { printf '%s' "$d/$t"; return 0; }
    done
    [ "$OS" = "Linux" ] && [ -x "$PREFIX/bin/$t" ] && { printf '%s' "$PREFIX/bin/$t"; return 0; }
    [ -x "$PREFIX/bin/g$t" ] && [ "$OS" = "Darwin" ] && { printf '%s' "$PREFIX/bin/g$t"; return 0; }
    [ -x "$PREFIX/bin/$t" ] && { printf '%s' "$PREFIX/bin/$t"; return 0; }
  fi
  # fall back to PATH, skipping our own bin dir
  local IFS=:
  for d in $PATH; do
    [ "$d" = "$BIN" ] && continue
    c="$d/$t"
    [ -x "$c" ] && [ ! -d "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

GNU="sed grep egrep fgrep find xargs awk gawk tar diff cmp diff3 sort uniq head tail cut tr wc date stat readlink realpath
ls cp mv rm mkdir rmdir chmod chown ln cat tee env basename dirname dd du df touch seq sleep timeout sha256sum sha1sum md5sum
base64 tac nl od expand unexpand split paste join comm fold fmt install mktemp nproc numfmt pr printenv shuf tsort truncate whoami id"
OTHER="bash rg fd eza bat jq yq uv git curl wget rsync tmux htop fzf tree"

linked=0; missing=""
for t in $GNU $OTHER; do
  if src="$(find_tool "$t")"; then
    ln -s "$src" "$BIN/$t"; linked=$((linked + 1))
  else
    missing="$missing $t"
  fi
done
say "bin_dir=$BIN linked=$linked"
[ -n "$missing" ] && say "not found (skipped):$missing"
say "next: set bin_dir = \"$BIN\" in your config, then run: leandc check"
