#!/usr/bin/env bash
# Build the benchmark work dir (default ~/.cache/leandc-bench, override with LEANDC_BENCH_HOME).
#   fixture-src/  a snapshot of this repo at HEAD (without bench/): the project the tasks work on
#   bench-venv/   python with pytest + the repo's pinned requirements + a copy of leandc
#   home-dc/      throwaway HOME for DC (copy of your .bashrc so its real shell behaviour is kept)
#   home-dc-tuned/  the same with fileReadLineLimit 300 and fileWriteLineLimit 800
#   mcp-dc.json, mcp-dc-tuned.json, mcp-leandc.json   one MCP server each, for `claude --strict-mcp-config`
# Needs: git, python3.12+, node/npx, the `claude` CLI (logged in), network for npx. Re-run to rebuild.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${LEANDC_BENCH_HOME:-$HOME/.cache/leandc-bench}"
FIXTURE_REF="${FIXTURE_REF:-3000e65}"   # pinned: the verifiers expect this snapshot (131 tests, shaping.py = 109 lines, cmdnames.py as of then)
DC_VERSION="${DC_VERSION:-0.3.1}"                                   # @wonderwhy-er/desktop-commander
LEANDC_BIN="${LEANDC_BIN:-$HOME/.local/share/leandc/venv/bin/leandc}"
LEANDC_CONFIG="${LEANDC_CONFIG:-$HOME/.config/leandc/config.toml}"
DC_SHELL="${DC_SHELL:-$(command -v bash)}"
PY="${LEANDC_PYTHON:-python3}"

mkdir -p "$WORK"
rm -rf "$WORK/fixture-src" && mkdir "$WORK/fixture-src"
git -C "$REPO" archive "$FIXTURE_REF" -- . ':(exclude)bench' | tar -x -C "$WORK/fixture-src"
git -C "$REPO" rev-parse "$FIXTURE_REF" > "$WORK/fixture-commit"

rm -rf "$WORK/bench-venv"
"$PY" -m venv "$WORK/bench-venv"
"$WORK/bench-venv/bin/pip" install -q -r "$WORK/fixture-src/requirements.txt" pytest
"$WORK/bench-venv/bin/pip" install -q --no-deps "$WORK/fixture-src"   # the transport tests spawn `python -m leandc.server`
rm -rf "$WORK/fixture-src/build"

mkdir -p "$WORK/home-dc/.claude-server-commander"
[ -f "$HOME/.bashrc" ] && cp "$HOME/.bashrc" "$WORK/home-dc/.bashrc" && chmod 600 "$WORK/home-dc/.bashrc"
printf '{"telemetryEnabled": false, "defaultShell": "%s", "allowedDirectories": [], "welcomeOnboardingEligible": false, "pendingWelcomeOnboarding": false}\n' "$DC_SHELL" > "$WORK/home-dc/.claude-server-commander/config.json"

cat > "$WORK/mcp-dc.json" <<EOF
{"mcpServers": {"dc": {"command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander@$DC_VERSION"],
  "env": {"HOME": "$WORK/home-dc", "npm_config_cache": "$HOME/.npm", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}}}
EOF
# Tuned DC: same server with a hard 300-line read cap (leandc: 300 default, 1000 max) and an 800-line write limit
# (as leandc's write_max_lines), so it is stricter than leandc on reads. DC has no setting
# to hide tools, so its schema cost cannot be tuned away.
mkdir -p "$WORK/home-dc-tuned/.claude-server-commander"
[ -f "$HOME/.bashrc" ] && cp "$HOME/.bashrc" "$WORK/home-dc-tuned/.bashrc" && chmod 600 "$WORK/home-dc-tuned/.bashrc"
printf '{"telemetryEnabled": false, "defaultShell": "%s", "allowedDirectories": [], "welcomeOnboardingEligible": false, "pendingWelcomeOnboarding": false, "fileReadLineLimit": 300, "fileWriteLineLimit": 800}\n' "$DC_SHELL" > "$WORK/home-dc-tuned/.claude-server-commander/config.json"
cat > "$WORK/mcp-dc-tuned.json" <<EOF
{"mcpServers": {"dc": {"command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander@$DC_VERSION"],
  "env": {"HOME": "$WORK/home-dc-tuned", "npm_config_cache": "$HOME/.npm", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}}}
EOF
# leandc runs from a private copy of your config (same caps) whose state_dir points into the work dir, so the
# benchmark does not write into your real usage log.
BENCH_CONFIG="$WORK/leandc-bench.toml"
sed -E "s|^state_dir[[:space:]]*=[[:space:]]*\"[^\"]*\"|state_dir = \"$WORK/leandc-state\"|" "$LEANDC_CONFIG" > "$BENCH_CONFIG"
chmod 600 "$BENCH_CONFIG"
grep -q "leandc-state" "$BENCH_CONFIG" || { echo "setup: could not set state_dir in $LEANDC_CONFIG" >&2; exit 1; }
cat > "$WORK/mcp-leandc.json" <<EOF
{"mcpServers": {"leandc": {"command": "$LEANDC_BIN", "args": ["serve", "--config", "$BENCH_CONFIG"],
  "env": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"}}}}
EOF
echo "bench work dir ready: $WORK (fixture $(cut -c1-7 "$WORK/fixture-commit"), DC $DC_VERSION)"
echo "next: export LEANDC_BENCH_HOME=$WORK; ./bench/smoke.sh; python3 bench/bench.py --reps 3 --arms dc,dc-tuned,leandc; python3 bench/report.py"
