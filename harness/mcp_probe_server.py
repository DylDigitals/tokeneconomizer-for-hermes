"""Minimal stdio MCP server for sandbox tests: two synthetic tools, newline-delimited JSON-RPC."""
import json, sys

TOOLS = [{"name": "alpha_echo", "description": "Echo text back (test tool).",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "beta_ping", "description": "Return pong (test tool).",
          "inputSchema": {"type": "object", "properties": {}}}]

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if method == "initialize":
        res = {"protocolVersion": msg["params"].get("protocolVersion", "2024-11-05"),
               "capabilities": {"tools": {}}, "serverInfo": {"name": "te-probe", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        args = msg["params"].get("arguments") or {}
        text = args.get("text", "pong")
        res = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
    sys.stdout.flush()
