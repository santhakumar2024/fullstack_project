from __future__ import annotations

import argparse
import json
from aiohttp import ClientSession, web
from urllib.parse import urlparse

from .config import load_config


def denied_response(message_id: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32001, "message": "Request denied by user"},
    }


async def request_decision(broker_url: str, server: str, method: str, params: object) -> str:
    payload = {
        "server": server,
        "source": "Claude Web",
        "method": method,
        "params": params,
    }
    try:
        async with ClientSession() as session:
            async with session.post(
                broker_url.rstrip("/") + "/request",
                json=payload,
                timeout=330,
            ) as response:
                body = await response.json()
                return "allow" if body.get("decision") == "allow" else "block"
    except Exception:
        return "block"


def create_app(upstream: str, broker_url: str, server_name: str) -> web.Application:
    app = web.Application()

    async def proxy(request: web.Request) -> web.Response:
        if request.method not in ("POST", "GET"):
            return web.Response(status=405)

        if request.method == "POST":
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "Invalid JSON"}, status=400)

            if isinstance(payload, dict) and payload.get("method") == "tools/call":
                decision = await request_decision(
                    broker_url,
                    server_name,
                    payload["method"],
                    payload.get("params"),
                )
                if decision != "allow":
                    return web.json_response(denied_response(payload.get("id")))

            async with ClientSession() as session:
                async with session.post(
                    upstream,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timeout=120,
                ) as upstream_response:
                    body = await upstream_response.read()
                    return web.Response(
                        body=body,
                        status=upstream_response.status,
                        content_type=upstream_response.headers.get("Content-Type", "application/json"),
                    )

        return web.Response(status=400)

    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP HTTP approval proxy")
    parser.add_argument("--broker", default=load_config().broker_url)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8766)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--server-name", default="remote")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    host = args.listen_host
    if host != "127.0.0.1":
        raise SystemExit("Refusing to bind to a non-loopback address. Use 127.0.0.1.")
    app = create_app(args.upstream, args.broker, args.server_name)
    web.run_app(app, host=host, port=args.listen_port)
