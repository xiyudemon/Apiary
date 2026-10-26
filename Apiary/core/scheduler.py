from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from core.backend import ApiaryBackend

logger = logging.getLogger(__name__)


class AutoScheduler:
    def __init__(self, backend: ApiaryBackend, interval: float = 1.0):
        self.backend = backend
        self.interval = interval
        self._thread = None
        self._stop_event = threading.Event()
        self.last_error: str | None = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _current_interval(self) -> float:
        speed = self.backend.runtime.get("speed", 1.0) if hasattr(self.backend, "runtime") else 1.0
        speed = max(0.25, float(speed))
        return max(0.35, self.interval / speed)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.backend.tick_simulation()
                self.last_error = None
            except Exception as exc:  # 调度线程必须存活，但异常不能再被静默丢弃
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("自动调度 tick 执行失败")
            self._stop_event.wait(self._current_interval())
