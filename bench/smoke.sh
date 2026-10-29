#!/usr/bin/env bash
# Schema-cost smoke test: the same trivial prompt with no MCP, DC only, leandc only.
# Input-token difference between runs = what each server's tool schemas cost per session.
WORK="${LEANDC_BENCH_HOME:-$HOME/.cache/leandc-bench}"
mkdir -p "$WORK/runs/smoke" && cd "$WORK/runs/smoke" || exit 1
export ENABLE_TOOL_SEARCH=false
run() {  # name, extra args...
  n=$1; shift
  claude -p "Reply with the single word OK." --model sonnet --tools "" --strict-mcp-config \
    --system-prompt "You are a coding assistant. Use the provided tools when needed." \
    --setting-sources "" --disable-slash-commands --no-session-persistence \
    --output-format json "$@" > "$n.json" 2> "$n.err"
  echo "$n exit=$?"
}
run none --mcp-config '{"mcpServers":{}}'
run dc --mcp-config "$WORK/mcp-dc.json"
run leandc --mcp-config "$WORK/mcp-leandc.json"
python3 - <<'EOF'
import json
for n in ("none", "dc", "leandc"):
    d = json.load(open(n + ".json")); u = d["usage"]
    tot = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    print(f"{n:7} total_in={tot} cost=${d['total_cost_usd']:.4f} error={d['is_error']}")
EOF
