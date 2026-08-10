"""Minimal JSON-file state store.

LiteLLMBackend uses this as a local project→team_id cache (and legacy usage
rows). Tests reuse it for MockBackend. Not a second “app mode”—the running
app still requires LiteLLM for admin APIs.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict


class StateStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        if not os.path.exists(path):
            self._write({"teams": {}, "usage": []})

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"teams": {}, "usage": []}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, self._path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._read()

    def update(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        """Run ``fn`` against the loaded state under lock, persist, return fn's result."""
        with self._lock:
            data = self._read()
            result = fn(data)
            self._write(data)
            return result
