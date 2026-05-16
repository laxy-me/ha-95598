"""Thread-safe shared state for the Ingress login UI.

Login / fetch threads publish their phase here; the QR server reads
it (plus the QR file mtime) to render an accurate status banner.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class _StateRegistry:
    IDLE = "idle"
    RUNNING = "running"
    WAITING_QR = "waiting_qr"
    LOGGED_IN = "logged_in"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = self.IDLE
        self._state_since = time.time()
        self._qr_active = False
        self._lockout_until: Optional[float] = None
        self._last_login_method: Optional[str] = None
        self._last_login_at: Optional[float] = None

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._state_since = time.time()
            if state == self.WAITING_QR:
                self._qr_active = True
            else:
                self._qr_active = False
            if state == self.LOGGED_IN:
                self._last_login_at = time.time()

    def set_login_method(self, method: Optional[str]) -> None:
        with self._lock:
            self._last_login_method = method

    def set_lockout_until(self, ts: Optional[float]) -> None:
        with self._lock:
            self._lockout_until = ts

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "state_since": self._state_since,
                "qr_active": self._qr_active,
                "lockout_until": self._lockout_until,
                "last_login_method": self._last_login_method,
                "last_login_at": self._last_login_at,
            }


registry = _StateRegistry()
