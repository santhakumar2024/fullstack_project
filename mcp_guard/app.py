from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import ttk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .approval import ApprovalQueue, PendingRequest


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "MCPApprovalGuard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/request":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.send_error(400)
            return

        request = self.server.approvals.submit(
            payload.get("server", "unknown"),
            payload.get("source", "unknown"),
            payload.get("method", ""),
            payload.get("params"),
        )
        decision = self.server.approvals.wait(request.id, timeout=self.server.timeout)
        self.server.approvals.finish(request.id)

        body = json.dumps({"decision": decision, "request_id": request.id}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True


class ConfirmationWindow:
    def __init__(self, approvals: ApprovalQueue) -> None:
        self.approvals = approvals
        self.dialog_open = False
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("MCP Approval Guard")
        self._start_polling()

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
        window = tk.Toplevel(self.root)
        window.title("MCP request approval")
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", lambda: decide("block"))

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
            self.approvals.decide(request.id, decision)
            window.grab_release()
            window.destroy()
            self.dialog_open = False

        button_row = ttk.Frame(frame)
        button_row.grid(column=0, row=8, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(button_row, text="Block", command=lambda: decide("block")).grid(column=0, row=0, padx=(0, 8))
        ttk.Button(button_row, text="Allow", command=lambda: decide("allow")).grid(column=1, row=0)

        window.update_idletasks()
        window.geometry(f"+{(window.winfo_screenwidth() - window.winfo_width()) // 2}+{(window.winfo_screenheight() - window.winfo_height()) // 3}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    from .config import load_config

    config = load_config()
    approvals = ApprovalQueue()
    server = BrokerServer((config.broker_host, config.broker_port), BrokerHandler)
    server.approvals = approvals
    server.timeout = config.approval_timeout

    broker_thread = threading.Thread(target=server.serve_forever, daemon=True)
    broker_thread.start()

    window = ConfirmationWindow(approvals)
    try:
        window.run()
    finally:
        server.shutdown()
        server.server_close()
