from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Iterable

from .config import load_config

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler("mcp_wrapper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mcp_wrapper")


# ─── Broker Communication with Retries ────────────────────────────────────
class BrokerClient:
    def __init__(self, broker_url: str, max_retries: int = 3, retry_delay: float = 1.0):
        self.broker_url = broker_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._session_id = os.urandom(8).hex()

    def request_decision(self, server: str, method: str, params: object) -> str:
        payload = json.dumps({
            "server": server,
            "source": "Claude Desktop",
            "method": method,
            "params": params,
        }).encode("utf-8")

        url = f"{self.broker_url}/request"
        headers = {
            "Content-Type": "application/json",
            "X-Session-ID": self._session_id,
        }

        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=330) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    decision = body.get("decision", "block")
                    logger.debug("Broker decision for %s: %s", server, decision)
                    return "allow" if decision == "allow" else "block"
            except urllib.error.HTTPError as e:
                logger.warning("Broker HTTP error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                last_error = e
            except urllib.error.URLError as e:
                logger.warning("Broker connection error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                last_error = e
            except json.JSONDecodeError as e:
                logger.warning("Broker response decode error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                last_error = e
            except Exception as e:
                logger.exception("Unexpected broker error (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                last_error = e

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay * (attempt + 1))  # Exponential backoff

        logger.error("All broker attempts failed: %s", last_error)
        return "block"  # Fail closed

    def health_check(self) -> bool:
        """Check if broker is reachable."""
        try:
            req = urllib.request.Request(f"{self.broker_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status == 200
        except Exception:
            return False


def denied_response(message_id: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32001, "message": "Request denied by user"},
    }


def is_tool_call(message: dict) -> bool:
    return message.get("method") == "tools/call"


# ─── Stream Relay with Error Handling ─────────────────────────────────────
class StreamRelay:
    def __init__(self, broker_client: BrokerClient, server_name: str):
        self.broker = broker_client
        self.server_name = server_name
        self._running = True
        self._process: subprocess.Popen | None = None

    def relay_outgoing(self, process: subprocess.Popen) -> None:
        """Read from stdin, filter through broker, write to process stdin."""
        self._process = process
        try:
            for raw_line in sys.stdin.buffer:
                if not self._running:
                    break
                if not raw_line.strip():
                    continue
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Pass through non-JSON lines
                    self._write_to_process(raw_line)
                    continue

                if not isinstance(message, dict):
                    self._write_to_process(raw_line)
                    continue

                method = message.get("method")
                if not is_tool_call(message):
                    # Non-tool calls pass through
                    self._write_to_process(raw_line)
                    continue

                # Tool call - request decision
                logger.info("Tool call intercepted: %s", message.get("params", {}).get("name", "unknown"))
                decision = self.broker.request_decision(self.server_name, method, message.get("params"))

                if decision == "allow":
                    self._write_to_process(raw_line)
                else:
                    response = denied_response(message.get("id"))
                    self._write_to_stdout((json.dumps(response) + "\n").encode("utf-8"))

        except BrokenPipeError:
            logger.info("Stdin pipe broken, shutting down outgoing relay")
        except Exception as e:
            logger.exception("Error in outgoing relay: %s", e)
        finally:
            self._running = False

    def relay_incoming(self, process: subprocess.Popen) -> int:
        """Read from process stdout, write to stdout."""
        try:
            for raw_line in process.stdout:
                if not self._running:
                    break
                self._write_to_stdout(raw_line)
        except BrokenPipeError:
            logger.info("Stdout pipe broken")
        except Exception as e:
            logger.exception("Error in incoming relay: %s", e)
        finally:
            self._running = False
        return process.wait()

    def relay_stderr(self, process: subprocess.Popen) -> None:
        """Forward stderr to our stderr."""
        try:
            for raw_line in process.stderr:
                sys.stderr.buffer.write(raw_line)
                sys.stderr.buffer.flush()
        except Exception as e:
            logger.exception("Error in stderr relay: %s", e)

    def _write_to_process(self, data: bytes) -> None:
        if self._process and self._process.stdin:
            try:
                # In binary mode, stdin is already a BufferedWriter
                self._process.stdin.write(data)
                self._process.stdin.flush()
            except BrokenPipeError:
                self._running = False
                raise
            except Exception as e:
                logger.exception("Error writing to process: %s", e)
                self._running = False
                raise

    def _write_to_stdout(self, data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            self._running = False
            raise
        except Exception as e:
            logger.exception("Error writing to stdout: %s", e)
            self._running = False
            raise

    def stop(self) -> None:
        self._running = False


# ─── Main ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP stdio approval wrapper (enhanced)")
    parser.add_argument("--broker", default=load_config().broker_url, help="Broker URL (default from config)")
    parser.add_argument("--server-name", default="unknown", help="Server name for approval requests")
    parser.add_argument("--server-command", required=True, help="Command to run the MCP server")
    parser.add_argument("--server-arg", action="append", default=[], help="Arguments for server command")
    parser.add_argument("--max-retries", type=int, default=3, help="Max broker retries")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Base retry delay (seconds)")
    parser.add_argument("--health-check", action="store_true", help="Check broker health on startup")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    broker_client = BrokerClient(args.broker, args.max_retries, args.retry_delay)

    # Health check on startup
    if args.health_check:
        logger.info("Checking broker health at %s...", args.broker)
        if not broker_client.health_check():
            logger.warning("Broker health check failed; continuing anyway (fail-closed behavior)")

    logger.info("Starting MCP server: %s %s", args.server_command, args.server_arg)
    # Use binary mode for proper JSON-RPC framing
    process = subprocess.Popen(
        [args.server_command, *args.server_arg],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    relay = StreamRelay(broker_client, args.server_name)

    # Start relay threads
    outgoing_thread = threading.Thread(target=relay.relay_outgoing, args=(process,), daemon=True, name="OutgoingRelay")
    incoming_thread = threading.Thread(target=lambda: relay.relay_incoming(process), daemon=True, name="IncomingRelay")
    stderr_thread = threading.Thread(target=relay.relay_stderr, args=(process,), daemon=True, name="StderrRelay")

    outgoing_thread.start()
    incoming_thread.start()
    stderr_thread.start()

    # Wait for process to complete
    try:
        exit_code = process.wait()
        logger.info("MCP server exited with code %d", exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted, terminating server...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        exit_code = 130
    except Exception as e:
        logger.exception("Error waiting for process: %s", e)
        exit_code = 1
    finally:
        relay.stop()
        # Give threads time to clean up
        outgoing_thread.join(timeout=2)
        incoming_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())