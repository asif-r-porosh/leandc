#!/usr/bin/env bash
# Build the benchmark work dir (default ~/.cache/leandc-bench, override with LEANDC_BENCH_HOME).
#   fixture-src/  a snapshot of this repo at HEAD (without bench/): the project the tasks work on
#   bench-venv/   python with pytest + the repo's pinned requirements + a copy of leandc
#   home-dc/      throwaway HOME for DC (copy of your .bashrc so its real shell behaviour is kept)
#   mcp-dc.json, mcp-leandc.json   one MCP server each, for `claude --strict-mcp-config`
# Needs: git, python3.12+, node/npx, the `claude` CLI (logged in), network for npx. Re-run to rebuild.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${LEANDC_BENCH_HOME:-$HOME/.cache/leandc-bench}"
FIXTURE_REF="${FIXTURE_REF:-1a4f2d0}"   # pinned: the verifiers expect this snapshot (131 tests, shaping.py = 109 lines, cmdnames.py as of then)
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
printf '{"telemetryEnabled": false, "defaultShell": "%s", "allowedDirectories": []}\n' "$DC_SHELL" > "$WORK/home-dc/.claude-server-commander/config.json"

cat > "$WORK/mcp-dc.json" <<EOF
{"mcpServers": {"dc": {"command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander@$DC_VERSION"],
  "env": {"HOME": "$WORK/home-dc", "npm_config_cache": "$HOME/.npm", "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}}}
EOF
cat > "$WORK/mcp-leandc.json" <<EOF
{"mcpServers": {"leandc": {"command": "$LEANDC_BIN", "args": ["serve", "--config", "$LEANDC_CONFIG"],
  "env": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"}}}}
EOF
echo "bench work dir ready: $WORK (fixture $(cut -c1-7 "$WORK/fixture-commit"), DC $DC_VERSION)"
echo "next: export LEANDC_BENCH_HOME=$WORK; ./bench/smoke.sh; python3 bench/bench.py --reps 3; python3 bench/report.py"
