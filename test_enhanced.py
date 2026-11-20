#!/usr/bin/env python3
"""Test script for enhanced MCP Guard components."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


# Simple auto-allow broker for testing
class AutoAllowHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if urlparse(self.path).path != "/request":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"decision": "allow", "request_id": "auto"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(404)

    def log_message(self, *args):
        pass


def run_broker():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), AutoAllowHandler)
    server.serve_forever()


def test_wrapper():
    """Test the enhanced wrapper with demo server."""
    broker_thread = threading.Thread(target=run_broker, daemon=True)
    broker_thread.start()
    time.sleep(0.5)  # Let broker start

    # Test with demo server
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_guard.stdio_wrapper_enhanced",
         "--broker", "http://127.0.0.1:8765",
         "--server-name", "demo",
         "--server-command", sys.executable,
         "--server-arg", "scripts/demo_mcp_server.py",
         "--health-check"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Send initialize
    init_msg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    proc.stdin.write(init_msg)
    proc.stdin.flush()

    # Read response
    response = proc.stdout.readline()
    print(f"Initialize response: {response.strip()}")

    # Send tool call
    tool_msg = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_search","arguments":{"query":"test"}}}\n'
    proc.stdin.write(tool_msg)
    proc.stdin.flush()

    response = proc.stdout.readline()
    print(f"Tool call response: {response.strip()}")

    proc.terminate()
    proc.wait(timeout=2)
    print("Test completed successfully!")


if __name__ == "__main__":
    test_wrapper()