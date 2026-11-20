from __future__ import annotations

import atexit
import json
import logging
import signal
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk
from urllib.parse import urlparse

from .approval import ApprovalQueue, PendingRequest
from .config import load_config

# ─── Logging setup ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mcp_guard.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mcp_guard")


# ─── Broker HTTP Handler ──────────────────────────────────────────────────
class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "MCPApprovalGuard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        logger.debug(fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/request":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            logger.warning("Invalid request payload: %s", e)
            self.send_error(400, "Invalid JSON")
            return

        try:
            request = self.server.approvals.submit(
                payload.get("server", "unknown"),
                payload.get("source", "unknown"),
                payload.get("method", ""),
                payload.get("params"),
            )
            logger.info("Request %s from %s: %s", request.id, request.server, request.method)
            decision = self.server.approvals.wait(request.id, timeout=self.server.timeout)
            self.server.approvals.finish(request.id)
            logger.info("Request %s decided: %s", request.id, decision)

            body = json.dumps({"decision": decision, "request_id": request.id}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.exception("Error handling request: %s", e)
            self.send_error(500, "Internal error")


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    approvals: ApprovalQueue
    timeout: float


# ─── Confirmation UI with System Tray ─────────────────────────────────────
class ConfirmationWindow:
    def __init__(self, approvals: ApprovalQueue, config) -> None:
        self.approvals = approvals
        self.config = config
        self.dialog_open = False
        self._current_request_id = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("MCP Approval Guard")
        self._setup_tray()
        self._start_polling()
        self._setup_signal_handlers()

    def _setup_tray(self) -> None:
        """System tray using pystray if available, else simple tkinter fallback."""
        try:
            import pystray
            from PIL import Image, ImageDraw

            # Create a simple tray icon
            image = Image.new("RGB", (64, 64), (0, 120, 212))
            draw = ImageDraw.Draw(image)
            draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

            menu = pystray.Menu(
                pystray.MenuItem("Show", self._show_window, default=True),
                pystray.MenuItem("Quit", self._quit_app),
            )
            self.tray_icon = pystray.Icon("MCP Guard", image, "MCP Approval Guard", menu)
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()
            self.has_tray = True
            logger.info("System tray enabled (pystray)")
        except ImportError:
            # Fallback: minimal tray using tkinter only (icon in taskbar)
            self.has_tray = False
            self._setup_tkinter_fallback()
            logger.info("System tray: using tkinter fallback (no tray icon)")

    def _setup_tkinter_fallback(self) -> None:
        """Minimal fallback - keep root window hidden but allow Alt+Tab access."""
        # Add protocol handler for window close
        self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_background)

    def _minimize_to_background(self) -> None:
        """Instead of closing, hide the root window (simulates tray minimize)."""
        self.root.withdraw()
        if self.has_tray:
            # With pystray, we don't get here
            pass
        # Without tray, user can't easily restore - show a message
        logger.info("Window minimized to background. Use task manager or restart to recover.")

    def _show_window(self) -> None:
        self.root.after(0, self.root.deiconify)

    def _quit_app(self) -> None:
        self.root.after(0, self._shutdown)

    def _setup_signal_handlers(self) -> None:
        def handle_signal(signum, frame):
            logger.info("Received signal %s, shutting down...", signum)
            self._shutdown()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        if hasattr(signal, "SIGBREAK"):  # Windows
            signal.signal(signal.SIGBREAK, handle_signal)

    def _start_polling(self) -> None:
        def poll() -> None:
            try:
                if not self.dialog_open:
                    request = self.approvals.ui_queue.get_nowait()
                    self._show_request(request)
            except Exception:
                pass
            self.root.after(100, poll)

        self.root.after(100, poll)

    def _show_request(self, request: PendingRequest) -> None:
        self.dialog_open = True
        self._current_request_id = request.id  # Track for decision
        window = tk.Toplevel(self.root)
        window.title("MCP request approval")
        window.attributes("-topmost", True)  # Always on top
        window.resizable(False, False)
        window.grab_set()  # Modal
        window.protocol("WM_DELETE_WINDOW", lambda: self._decide(window, "block"))

        # Ensure it stays on top even if other windows try to steal focus
        window.lift()
        window.focus_force()

        frame = ttk.Frame(window, padding=18)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="MCP request requires approval", font=("Segoe UI", 12, "bold")).grid(
            column=0, row=0, columnspan=2, sticky="w"
        )

        rows = [
            ("Source", request.source),
            ("MCP server", request.server),
            ("Method", request.method),
            ("Tool", request.tool or "(not a tool call)"),
        ]
        for index, (label, value) in enumerate(rows, start=1):
            ttk.Label(frame, text=f"{label}:").grid(column=0, row=index, sticky="nw", pady=2)
            ttk.Label(frame, text=value, wraplength=520).grid(column=1, row=index, sticky="nw", pady=2)

        try:
            params_text = json.dumps(request.params, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            params_text = repr(request.params)

        ttk.Label(frame, text="Parameters").grid(column=0, row=6, columnspan=2, sticky="w", pady=(12, 4))
        text = tk.Text(frame, width=70, height=12, wrap="none", font=("Consolas", 9))
        text.insert("1.0", params_text)
        text.configure(state="disabled")
        text.grid(column=0, row=7, columnspan=2, sticky="nsew")

        def decide(decision: str) -> None:
            self._decide(window, decision)

        button_row = ttk.Frame(frame)
        button_row.grid(column=0, row=8, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(button_row, text="Block", command=lambda: decide("block")).grid(column=0, row=0, padx=(0, 8))
        ttk.Button(button_row, text="Allow", command=lambda: decide("allow")).grid(column=1, row=0)

        # Center on screen
        window.update_idletasks()
        x = (window.winfo_screenwidth() - window.winfo_width()) // 2
        y = (window.winfo_screenheight() - window.winfo_height()) // 3
        window.geometry(f"+{x}+{y}")

    def _decide(self, window: tk.Toplevel, decision: str) -> None:
        if hasattr(self, "_current_request_id"):
            self.approvals.decide(self._current_request_id, decision)
            delattr(self, "_current_request_id")
        window.grab_release()
        window.destroy()
        self.dialog_open = False

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        if self.has_tray:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        logger.info("Starting UI main loop")
        try:
            self.root.mainloop()
        except Exception as e:
            logger.exception("UI error: %s", e)
        finally:
            self._shutdown()


# ─── Main entry point ─────────────────────────────────────────────────────
def main() -> None:
    config = load_config()
    approvals = ApprovalQueue()
    server = BrokerServer((config.broker_host, config.broker_port), BrokerHandler)
    server.approvals = approvals
    server.timeout = config.approval_timeout

    broker_thread = threading.Thread(target=server.serve_forever, daemon=True, name="BrokerThread")
    broker_thread.start()
    logger.info("Broker started on http://%s:%d", config.broker_host, config.broker_port)

    # Register cleanup
    def cleanup():
        logger.info("Cleanup: shutting down broker")
        server.shutdown()
        server.server_close()

    atexit.register(cleanup)

    window = ConfirmationWindow(approvals, config)
    try:
        window.run()
    finally:
        cleanup()


if __name__ == "__main__":
    main()