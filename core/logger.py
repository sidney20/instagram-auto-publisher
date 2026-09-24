from __future__ import annotations

import logging
import queue
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_gui_queue: queue.Queue | None = None


def setup_logger(logs_dir: Path, level_name: str = "INFO") -> tuple[logging.Logger, queue.Queue]:
    global _gui_queue
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("iap")
    logger.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    fh = RotatingFileHandler(logs_dir / "publisher.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    _gui_queue = queue.Queue()
    gh = QueueLogHandler(_gui_queue)
    gh.setFormatter(fmt)
    logger.addHandler(gh)

    return logger, _gui_queue


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(self.format(record))
        except Exception:
            pass


def get_logger() -> logging.Logger:
    return logging.getLogger("iap")
