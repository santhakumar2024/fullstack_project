from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    broker_host: str = "127.0.0.1"
    broker_port: int = 8765
    approval_timeout: float = 300.0

    @property
    def broker_url(self) -> str:
        return f"http://{self.broker_host}:{self.broker_port}"


def load_config() -> Config:
    path = Path(os.environ.get("MCP_GUARD_CONFIG", "~/.mcp-approval-guard.json")).expanduser()
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Config(
            broker_host=str(data.get("broker_host", "127.0.0.1")),
            broker_port=int(data.get("broker_port", 8765)),
            approval_timeout=float(data.get("approval_timeout", 300)),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return Config()
