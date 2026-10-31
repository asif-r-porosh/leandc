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
run dc-tuned --mcp-config "$WORK/mcp-dc-tuned.json"
# Optional: leandc over HTTPS. Put a config like this in $WORK/mcp-leandc-http.json (keep it out of the repo, the URL holds the secret):
#   {"mcpServers":{"leandc":{"type":"http","url":"https://HOST/SECRET/mcp"}}}
NAMES="none dc dc-tuned leandc"
if [ -f "$WORK/mcp-leandc-http.json" ]; then
  run leandc-http --mcp-config "$WORK/mcp-leandc-http.json"
  NAMES="$NAMES leandc-http"
  URL=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['mcpServers']['leandc']['url'])" "$WORK/mcp-leandc-http.json")
  for i in 1 2 3; do   # HTTP round trip of one MCP initialize call (connection setup included); the URL is not printed
    curl -sS -o /dev/null -w "http initialize call $i: status=%{http_code} time=%{time_total}s\n" -X POST "$URL" \
      -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  done
fi
NAMES="$NAMES" python3 - <<'EOF'
import json, os
for n in os.environ["NAMES"].split():
    d = json.load(open(n + ".json")); u = d["usage"]
    tot = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    print(f"{n:7} total_in={tot} cost=${d['total_cost_usd']:.4f} error={d['is_error']}")
EOF
