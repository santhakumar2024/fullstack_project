from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.request
from typing import Iterable

from .config import load_config


def request_decision(broker_url: str, server: str, method: str, params: object) -> str:
    payload = json.dumps({
        "server": server,
        "source": "Claude Desktop",
        "method": method,
        "params": params,
    }).encode("utf-8")
    request = urllib.request.Request(
        broker_url.rstrip("/") + "/request",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=330) as response:
            body = json.loads(response.read().decode("utf-8"))
            decision = body.get("decision")
            return "allow" if decision == "allow" else "block"
    except Exception:
        return "block"


def denied_response(message_id: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32001, "message": "Request denied by user"},
    }


def is_tool_call(message: dict) -> bool:
    return message.get("method") == "tools/call"


def relay_outgoing(process: subprocess.Popen, server_name: str, broker_url: str) -> None:
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            sys.stdout.buffer.write(raw_line)
            sys.stdout.buffer.flush()
            continue

        if not isinstance(message, dict):
            sys.stdout.buffer.write(raw_line)
            sys.stdout.buffer.flush()
            continue

        method = message.get("method")
        is_call = method == "tools/call"
        if not is_call:
            decision = "allow"
        else:
            decision = request_decision(
                broker_url,
                server_name,
                method,
                message.get("params"),
            )

        if decision == "allow":
            process.stdin.buffer.write(raw_line)
            process.stdin.buffer.flush()
        else:
            response = denied_response(message.get("id"))
            sys.stdout.buffer.write((json.dumps(response) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


def relay_incoming(process: subprocess.Popen) -> int:
    assert process.stdout is not None
    for raw_line in process.stdout:
        sys.stdout.buffer.write(raw_line)
        sys.stdout.buffer.flush()
    return process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP stdio approval wrapper")
    parser.add_argument("--broker", default=load_config().broker_url)
    parser.add_argument("--server-name", default="unknown")
    parser.add_argument("--server-command", required=True)
    parser.add_argument("--server-arg", action="append", default=[])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    process = subprocess.Popen(
        [args.server_command, *args.server_arg],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    outgoing = threading.Thread(
        target=relay_outgoing,
        args=(process, args.server_name, args.broker),
        daemon=True,
    )
    outgoing.start()

    stderr_thread = threading.Thread(
        target=lambda: relay_stderr(process),
        daemon=True,
    )
    stderr_thread.start()

    try:
        exit_code = relay_incoming(process)
    except BrokenPipeError:
        exit_code = 1
    finally:
        if process.poll() is None:
            process.terminate()
    raise SystemExit(exit_code)


def relay_stderr(process: subprocess.Popen) -> None:
    assert process.stderr is not None
    for raw_line in process.stderr:
        sys.stderr.buffer.write(raw_line)
        sys.stderr.buffer.flush()
