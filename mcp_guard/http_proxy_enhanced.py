from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any
from aiohttp import ClientSession, ClientTimeout, web
from urllib.parse import urlparse

from .config import load_config

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mcp_proxy.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mcp_proxy")


# ─── Broker Client with Retries ───────────────────────────────────────────
class BrokerClient:
    def __init__(self, broker_url: str, max_retries: int = 3, retry_delay: float = 1.0):
        self.broker_url = broker_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._session: ClientSession | None = None

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=ClientTimeout(total=330))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request_decision(self, server: str, method: str, params: Any) -> str:
        payload = {
            "server": server,
            "source": "Claude Web",
            "method": method,
            "params": params,
        }

        url = f"{self.broker_url}/request"
        last_error = None

        for attempt in range(self.max_retries):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload) as response:
                    body = await response.json()
                    decision = body.get("decision", "block")
                    logger.debug("Broker decision for %s: %s", server, decision)
                    return "allow" if decision == "allow" else "block"
            except asyncio.TimeoutError:
                logger.warning("Broker timeout (attempt %d/%d)", attempt + 1, self.max_retries)
                last_error = "timeout"
            except Exception as e:
                logger.warning("Broker error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                last_error = str(e)

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_delay * (attempt + 1))

        logger.error("All broker attempts failed: %s", last_error)
        return "block"  # Fail closed

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self.broker_url}/health", timeout=ClientTimeout(total=5)) as response:
                return response.status == 200
        except Exception:
            return False


def denied_response(message_id: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32001, "message": "Request denied by user"},
    }


def is_tool_call(payload: dict) -> bool:
    return payload.get("method") == "tools/call"


# ─── Proxy Application ────────────────────────────────────────────────────
async def create_proxy_app(
    upstream: str,
    broker_client: BrokerClient,
    server_name: str,
) -> web.Application:
    app = web.Application()

    # Health endpoint for the proxy itself
    async def health(request: web.Request) -> web.Response:
        broker_ok = await broker_client.health_check()
        return web.json_response({
            "status": "ok" if broker_ok else "degraded",
            "broker": "ok" if broker_ok else "unreachable",
            "upstream": upstream,
        })

    async def proxy_handler(request: web.Request) -> web.Response:
        # Only accept POST for JSON-RPC
        if request.method != "POST":
            return web.Response(status=405, text="Method Not Allowed")

        # Parse request body
        try:
            body_bytes = await request.read()
            if not body_bytes:
                return web.json_response({"error": "Empty body"}, status=400)
            payload = json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON: %s", e)
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.exception("Error reading request: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        # Handle batch requests (array of JSON-RPC)
        if isinstance(payload, list):
            results = []
            for item in payload:
                result = await handle_single_request(item, broker_client, server_name, upstream)
                if result is not None:
                    results.append(result)
            return web.json_response(results)

        # Single request
        result = await handle_single_request(payload, broker_client, server_name, upstream)
        if result is None:
            return web.Response(status=400, text="Invalid request")
        return web.json_response(result)

    async def handle_single_request(
        payload: dict,
        broker: BrokerClient,
        server: str,
        upstream_url: str,
    ) -> dict | None:
        """Process a single JSON-RPC request."""
        if not isinstance(payload, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        method = payload.get("method")
        msg_id = payload.get("id")

        # Intercept tool calls
        if is_tool_call(payload):
            tool_name = payload.get("params", {}).get("name", "unknown")
            logger.info("Tool call intercepted: %s (id=%s)", tool_name, msg_id)

            decision = await broker.request_decision(server, method, payload.get("params"))
            if decision != "allow":
                logger.info("Tool call blocked: %s", tool_name)
                return denied_response(msg_id)

            logger.info("Tool call allowed: %s", tool_name)

        # Forward to upstream (including non-tool calls)
        try:
            session = await broker._get_session()
            async with session.post(
                upstream_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=ClientTimeout(total=120),
            ) as upstream_response:
                resp_body = await upstream_response.read()
                # Return raw response to preserve streaming/SSE if needed
                return web.Response(
                    body=resp_body,
                    status=upstream_response.status,
                    content_type=upstream_response.headers.get("Content-Type", "application/json"),
                )
        except asyncio.TimeoutError:
            logger.error("Upstream timeout: %s", upstream_url)
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Upstream timeout"}}
        except Exception as e:
            logger.exception("Upstream error: %s", e)
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": f"Upstream error: {e}"}}

    app.router.add_get("/health", health)
    app.router.add_post("/{tail:.*}", proxy_handler)
    app.router.add_get("/{tail:.*}", lambda r: web.Response(status=405))

    # Store broker client for cleanup
    app["broker_client"] = broker_client

    return app


# ─── Main ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP HTTP approval proxy (enhanced)")
    parser.add_argument("--broker", default=load_config().broker_url, help="Broker URL")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)")
    parser.add_argument("--listen-port", type=int, default=8766, help="Listen port (default: 8766)")
    parser.add_argument("--upstream", required=True, help="Upstream MCP server URL")
    parser.add_argument("--server-name", default="remote", help="Server name for approval requests")
    parser.add_argument("--max-retries", type=int, default=3, help="Max broker retries")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Base retry delay (seconds)")
    parser.add_argument("--health-check", action="store_true", help="Check broker health on startup")
    return parser


async def main_async() -> int:
    args = build_parser().parse_args()

    # Validate listen host
    if args.listen_host != "127.0.0.1":
        logger.error("Refusing to bind to non-loopback address. Use 127.0.0.1.")
        return 1

    broker_client = BrokerClient(args.broker, args.max_retries, args.retry_delay)

    # Health check on startup
    if args.health_check:
        logger.info("Checking broker health at %s...", args.broker)
        if not await broker_client.health_check():
            logger.warning("Broker health check failed; continuing anyway (fail-closed behavior)")

    logger.info("Starting HTTP proxy on %s:%d -> %s", args.listen_host, args.listen_port, args.upstream)

    app = await create_proxy_app(args.upstream, broker_client, args.server_name)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.listen_host, args.listen_port)
    await site.start()

    logger.info("Proxy running. Press Ctrl+C to stop.")

    # Wait for shutdown signal
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for all signals

    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        await broker_client.close()
        await runner.cleanup()

    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())