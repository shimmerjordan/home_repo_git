"""Logging:
  - In-memory ring buffer (5000 entries) for fast UI access via /api/logs.
  - File handler with daily rotation, 90-day retention, in /app/data/logs/app.log.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "/app/data/logs"))
RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))
RING_CAPACITY = int(os.getenv("LOG_RING_CAPACITY", "5000"))


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 5000):
        super().__init__()
        self.buffer: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with self._lock:
            self._counter += 1
            entry = {
                "id": self._counter,
                "time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": msg,
            }
            if record.exc_info:
                try:
                    entry["message"] += "\n" + self.format(record).split("\n", 1)[-1]
                except Exception:
                    pass
            self.buffer.append(entry)

    def snapshot(self, since_id: int = 0, level: str | None = None) -> list[dict]:
        with self._lock:
            items = [e for e in self.buffer if e["id"] > since_id]
        if level:
            wanted = level.upper()
            order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
            min_lvl = order.get(wanted, 0)
            items = [e for e in items if order.get(e["level"], 0) >= min_lvl]
        return items


_ring = RingBufferHandler(capacity=RING_CAPACITY)
_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
_ring.setFormatter(_formatter)


def install() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture DEBUG too; UI filters

    # Ring buffer
    if _ring not in root.handlers:
        root.addHandler(_ring)

    # File with daily rotation, ~3 months retention
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = LOG_DIR / "app.log"
        already = any(
            isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "") == str(file_path)
            for h in root.handlers
        )
        if not already:
            file_h = TimedRotatingFileHandler(
                str(file_path), when="D", interval=1,
                backupCount=RETENTION_DAYS, encoding="utf-8", utc=False,
            )
            file_h.setFormatter(_formatter)
            file_h.setLevel(logging.DEBUG)
            root.addHandler(file_h)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("file logging disabled: %s", exc)

    # Capture uvicorn/fastapi loggers explicitly so their records also reach the ring.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # Prevent duplicate by relying on propagation to root.


def get_buffer() -> RingBufferHandler:
    return _ring


# Dedicated app logger.
app_log = logging.getLogger("storage")
app_log.setLevel(logging.DEBUG)
