"""Best-effort, opt-in memory diagnostics for expensive SQLite reads."""
from __future__ import annotations

import ctypes
import gc
import os
import sys
import threading
import time
from datetime import datetime, timezone

from loguru import logger


_LAST_LOG_AT: dict[str, float] = {}
_LOCK = threading.Lock()
_MIN_LOG_INTERVAL_SEC = 30.0


def _enabled() -> bool:
    return os.environ.get("OUTCOME_DB_MEM_DIAG", "0").strip().lower() in {"1", "true", "yes", "on"}


def _rss_bytes() -> int | None:
    """Return current RSS; macOS uses task_info without an extra dependency."""
    try:
        if sys.platform != "darwin":
            return None

        class _MachTaskBasicInfo(ctypes.Structure):
            _fields_ = [
                # mach_task_basic_info_data_t on macOS.  The earlier layout
                # was not the ABI layout, so task_info correctly declined it
                # and the diagnostic emitted RSS as "na".
                ("suspend_count", ctypes.c_int),
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("user_seconds", ctypes.c_int),
                ("user_microseconds", ctypes.c_int),
                ("system_seconds", ctypes.c_int),
                ("system_microseconds", ctypes.c_int),
                ("policy", ctypes.c_int),
            ]

        info = _MachTaskBasicInfo()
        count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint32))
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libsystem.mach_task_self.restype = ctypes.c_uint32
        libsystem.task_info.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        libsystem.task_info.restype = ctypes.c_int
        result = libsystem.task_info(
            libsystem.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count),
        )
        return int(info.resident_size) if result == 0 else None
    except Exception:
        return None


def _mb(value: int | None) -> str:
    return "na" if value is None else str(round(value / (1024 * 1024), 1))


class DbMemDiag:
    """Best-effort, rate-limited diagnostic sample that cannot affect decisions."""

    def __init__(self, query: str) -> None:
        self.query, self.active = query, False
        self.started_at, self.started, self.query_finished = "", 0.0, 0.0
        self.rows = self.payload_bytes = 0
        self.rss_before: int | None = None
        self.rss_after_query: int | None = None
        try:
            if not _enabled():
                return
            now = time.monotonic()
            with _LOCK:
                if now - _LAST_LOG_AT.get(query, 0.0) < _MIN_LOG_INTERVAL_SEC:
                    return
                _LAST_LOG_AT[query] = now
            self.active, self.started = True, now
            self.started_at, self.rss_before = datetime.now(timezone.utc).isoformat(), _rss_bytes()
        except Exception:
            self.active = False

    def after_query(self, *, rows: int, payload_bytes: int = 0) -> None:
        try:
            if self.active:
                self.rows, self.payload_bytes = rows, payload_bytes
                self.query_finished, self.rss_after_query = time.monotonic(), _rss_bytes()
        except Exception:
            pass

    def finish(self, *, note: str = "") -> None:
        try:
            if not self.active:
                return
            before_gc = _rss_bytes()
            # Strictly opt-in diagnostic work; neither its result nor failure
            # controls execution, accounting, or a database operation.
            gc.collect()
            after_gc, ended = _rss_bytes(), time.monotonic()
            query_end = self.query_finished or ended
            gc_delta = None if before_gc is None or after_gc is None else before_gc - after_gc
            logger.info(
                "[DB_MEM_DIAG] query={} started_at={} rows={} payload_bytes={} query_ms={:.1f} "
                "processing_ms={:.1f} rss_before_mb={} rss_after_query_mb={} "
                "rss_after_processing_mb={} gc_rss_delta_mb={} thread={}{}",
                self.query, self.started_at, self.rows, self.payload_bytes,
                (query_end - self.started) * 1000, (ended - query_end) * 1000,
                _mb(self.rss_before), _mb(self.rss_after_query), _mb(before_gc), _mb(gc_delta),
                threading.current_thread().name, f" note={note}" if note else "",
            )
        except Exception:
            pass
