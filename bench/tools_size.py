"""Launch an MCP stdio server from a claude-style mcp config and report tool-schema size."""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(cfg_path, name):
    spec = json.load(open(cfg_path))["mcpServers"][name]
    params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env"))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
    blob = json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools], separators=(",", ":"))
    print(f"{name}: server={init.server_info.name} {init.server_info.version} tools={len(tools)} schema_chars={len(blob)} (~{len(blob)//4} tok)")
    print("  " + ", ".join(sorted(t.name for t in tools)))


asyncio.run(main(sys.argv[1], sys.argv[2]))
