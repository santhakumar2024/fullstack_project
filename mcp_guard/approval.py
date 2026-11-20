from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal


Decision = Literal["allow", "block"]


@dataclass(frozen=True)
class PendingRequest:
    id: str
    server: str
    source: str
    tool: str | None
    method: str
    params: object
    created_at: float


@dataclass
class DecisionResult:
    request_id: str
    decision: Decision


@dataclass
class ApprovalQueue:
    pending: dict[str, PendingRequest] = field(default_factory=dict)
    results: dict[str, Decision] = field(default_factory=dict)
    events: dict[str, threading.Event] = field(default_factory=dict)
    ui_queue: queue.Queue[PendingRequest] = field(default_factory=queue.Queue)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def submit(self, server: str, source: str, method: str, params: object) -> PendingRequest:
        tool = None
        if method == "tools/call" and isinstance(params, dict):
            value = params.get("name")
            if isinstance(value, str):
                tool = value

        import time

        request = PendingRequest(
            id=str(uuid.uuid4()),
            server=server,
            source=source,
            tool=tool,
            method=method,
            params=params,
            created_at=time.time(),
        )
        with self._lock:
            self.pending[request.id] = request
            self.events[request.id] = threading.Event()
            self.ui_queue.put(request)
        return request

    def decide(self, request_id: str, decision: Decision) -> bool:
        with self._lock:
            if request_id not in self.pending:
                return False
            self.results[request_id] = decision
            event = self.events[request_id]
        event.set()
        return True

    def wait(self, request_id: str, timeout: float | None = None) -> Decision:
        with self._lock:
            event = self.events.get(request_id)
        if event is None:
            return "block"
        if not event.wait(timeout):
            with self._lock:
                self.results.setdefault(request_id, "block")
            return "block"
        with self._lock:
            return self.results.get(request_id, "block")

    def finish(self, request_id: str) -> None:
        with self._lock:
            self.pending.pop(request_id, None)
            self.events.pop(request_id, None)
            self.results.pop(request_id, None)
