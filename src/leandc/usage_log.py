"""Metadata-only usage log (D19, D12).

Records: time, tool, duration, outcome, sizes, a short hash of the path, and for `run` the
command names used with counts (e.g. {"ls":3,"grep":1}). Never records arguments, paths or
content. Rotated by size.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .config import LogConfig


def path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:8]


class UsageLog:
    def __init__(self, cfg: LogConfig, state_dir: str) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._file = Path(state_dir) / "usage.jsonl"
        if cfg.enabled:
            self._file.parent.mkdir(parents=True, exist_ok=True)

    @property
    def file(self) -> Path:
        return self._file

    def record(
        self,
        tool: str,
        dur_s: float,
        ok: bool,
        code: str | None = None,
        exit_code: int | None = None,
        in_bytes: int = 0,
        out_bytes: int = 0,
        path: str | None = None,
        cmds: dict[str, int] | None = None,
    ) -> None:
        if not self._cfg.enabled:
            return
        entry = {
            "ts": round(time.time(), 3),
            "tool": tool,
            "dur_s": round(dur_s, 3),
            "ok": ok,
            "code": code,
            "exit": exit_code,
            "in": in_bytes,
            "out": out_bytes,
            "path": path_hash(path) if path else None,
        }
        if cmds:
            entry["cmds"] = cmds
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self._rotate_if_needed()
                with open(self._file, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass  # the log must never break a tool call

    def _rotate_if_needed(self) -> None:
        try:
            if self._file.stat().st_size < self._cfg.max_bytes:
                return
        except FileNotFoundError:
            return
        keep = max(self._cfg.keep, 1)
        oldest = self._file.with_name(f"{self._file.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for i in range(keep - 1, 0, -1):
            src = self._file.with_name(f"{self._file.name}.{i}")
            if src.exists():
                os.replace(src, self._file.with_name(f"{self._file.name}.{i + 1}"))
        os.replace(self._file, self._file.with_name(f"{self._file.name}.1"))
