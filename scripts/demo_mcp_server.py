"""A minimal stdio MCP-like server for local approval demonstrations."""

import json
import sys


def main() -> None:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if not isinstance(message, dict):
            continue

        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
            }
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name", "unknown")
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [{"type": "text", "text": f"Executed demo tool: {name}"}]},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {},
            }
        sys.stdout.buffer.write((json.dumps(response) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
