"""
Process-stable dashboard stats runtime.

This module centralizes queue/size stats caching and background refresh so
Streamlit reruns do not recreate thread-local globals repeatedly.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

import streamlit as st

from core.queue_manager import get_queue_manager


def _is_real_data(data: Any, min_keys: int = 2) -> bool:
    """Return True when a payload looks like real stats."""
    return bool(data and isinstance(data, dict) and len(data) >= min_keys)


def _fmt_ts(ts: float) -> str:
    """Render epoch timestamp as UTC ISO string."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return ""


class DashboardStatsRuntime:
    """Background-refresh runtime for dashboard queue and size stats."""

    def __init__(self, queue_interval: float = 5.0, size_interval: float = 15.0):
        self.queue_interval = max(1.0, float(queue_interval))
        self.size_interval = max(3.0, float(size_interval))

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="dashboard-stats",
        )

        self._state: Dict[str, Any] = {
            "queue_stats": None,
            "size_stats": None,
            "queue_ts": 0.0,
            "size_ts": 0.0,
            "last_error": "",
            "last_error_ts": 0.0,
            "consecutive_errors": 0,
            "started_at": 0.0,
            "last_queue_warmup": 0.0,
            "last_size_warmup": 0.0,
        }
        self._last_known_good: Dict[str, Any] = {
            "queue_stats": None,
            "size_stats": None,
        }

    def start(self) -> None:
        """Start background fetch loop once per runtime instance."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._state["started_at"] = time.time()
            self._thread = threading.Thread(
                target=self._run_fetch_loop,
                daemon=True,
                name="stats-bg",
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop background thread (mainly for tests)."""
        self._stop_event.set()
        with self._lock:
            th = self._thread
        if th and th.is_alive():
            th.join(timeout=1.5)
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        # Recreate executor so start()/fetch can be called again after stop().
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="dashboard-stats",
        )

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state["last_error"] = str(message)[:500]
            self._state["last_error_ts"] = time.time()
            self._state["consecutive_errors"] = int(self._state["consecutive_errors"]) + 1

    def _record_success(self) -> None:
        with self._lock:
            self._state["consecutive_errors"] = 0
            self._state["last_error"] = ""
            self._state["last_error_ts"] = 0.0

    def _run_fetch_loop(self) -> None:
        """Refresh queue stats and size stats on intervals with retry backoff."""
        last_queue = 0.0
        last_size = 0.0

        while not self._stop_event.is_set():
            now = time.time()

            if now - last_queue >= self.queue_interval:
                queue_stats = self._fetch_queue_once(timeout_sec=4.0)
                if _is_real_data(queue_stats):
                    last_queue = now

            if now - last_size >= self.size_interval:
                size_stats = self._fetch_size_once(timeout_sec=4.0)
                if _is_real_data(size_stats, min_keys=1):
                    last_size = now

            with self._lock:
                err_count = int(self._state["consecutive_errors"])

            if err_count <= 2:
                sleep_seconds = 1.0
            elif err_count <= 5:
                sleep_seconds = 3.0
            elif err_count <= 10:
                sleep_seconds = 5.0
            else:
                sleep_seconds = 20.0

            self._stop_event.wait(sleep_seconds)

    def _fetch_queue_once(self, timeout_sec: float = 3.0) -> Dict[str, Any]:
        """Fetch queue stats once with timeout and update cache on success."""
        future = self._executor.submit(lambda: get_queue_manager().get_queue_statistics())
        try:
            payload = future.result(timeout=float(timeout_sec))
        except Exception as exc:
            future.cancel()
            self._set_error(f"queue fetch failed: {exc}")
            return {}

        if not _is_real_data(payload):
            self._set_error("queue fetch returned empty payload")
            return {}

        now = time.time()
        payload["_cached_at"] = now
        with self._lock:
            self._state["queue_stats"] = payload
            self._state["queue_ts"] = now
            self._last_known_good["queue_stats"] = payload
        self._record_success()
        return payload

    def _fetch_size_once(self, timeout_sec: float = 3.0) -> Dict[str, Any]:
        """Fetch size stats once with timeout and update cache on success."""
        future = self._executor.submit(lambda: get_queue_manager().get_size_statistics())
        try:
            payload = future.result(timeout=float(timeout_sec))
        except Exception as exc:
            future.cancel()
            self._set_error(f"size fetch failed: {exc}")
            return {}

        if not _is_real_data(payload, min_keys=1):
            self._set_error("size fetch returned empty payload")
            return {}

        now = time.time()
        with self._lock:
            self._state["size_stats"] = payload
            self._state["size_ts"] = now
            self._last_known_good["size_stats"] = payload
        self._record_success()
        return payload

    def get_queue_stats(self) -> Dict[str, Any]:
        """Read queue stats with fallback to last-known-good and warmup fetch."""
        self.start()
        now = time.time()
        with self._lock:
            cached = self._state["queue_stats"]
            lkg = self._last_known_good["queue_stats"]
            last_warmup = float(self._state["last_queue_warmup"])

        if _is_real_data(cached):
            return cached
        if _is_real_data(lkg):
            return lkg

        if now - last_warmup >= 3.0:
            with self._lock:
                self._state["last_queue_warmup"] = now
            refreshed = self._fetch_queue_once(timeout_sec=2.5)
            if _is_real_data(refreshed):
                return refreshed

        return {}

    def get_size_stats(self) -> Dict[str, Any]:
        """Read size stats with fallback to last-known-good and warmup fetch."""
        self.start()
        now = time.time()
        with self._lock:
            cached = self._state["size_stats"]
            lkg = self._last_known_good["size_stats"]
            last_warmup = float(self._state["last_size_warmup"])

        if _is_real_data(cached, min_keys=1):
            return cached
        if _is_real_data(lkg, min_keys=1):
            return lkg

        if now - last_warmup >= 3.0:
            with self._lock:
                self._state["last_size_warmup"] = now
            refreshed = self._fetch_size_once(timeout_sec=2.5)
            if _is_real_data(refreshed, min_keys=1):
                return refreshed

        return {}

    def force_refresh(self, timeout_sec: float = 5.0) -> Dict[str, Dict[str, Any]]:
        """Force refresh queue and size stats with bounded wait."""
        self.start()
        queue_stats = self._fetch_queue_once(timeout_sec=timeout_sec)
        size_stats = self._fetch_size_once(timeout_sec=timeout_sec)
        return {
            "queue_stats": queue_stats if _is_real_data(queue_stats) else self.get_queue_stats(),
            "size_stats": size_stats if _is_real_data(size_stats, min_keys=1) else self.get_size_stats(),
        }

    def invalidate(self) -> None:
        """Mark timestamps stale but keep data and last-known-good payloads."""
        with self._lock:
            self._state["queue_ts"] = 0.0
            self._state["size_ts"] = 0.0

    def clear(self) -> None:
        """Clear all cached data including last-known-good payloads."""
        with self._lock:
            self._state["queue_stats"] = None
            self._state["size_stats"] = None
            self._state["queue_ts"] = 0.0
            self._state["size_ts"] = 0.0
            self._state["last_error"] = ""
            self._state["last_error_ts"] = 0.0
            self._state["consecutive_errors"] = 0
            self._last_known_good["queue_stats"] = None
            self._last_known_good["size_stats"] = None

    def health(self) -> Dict[str, Any]:
        """Expose runtime health diagnostics for dashboard monitoring."""
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            queue_ts = float(self._state["queue_ts"])
            size_ts = float(self._state["size_ts"])
            started_at = float(self._state["started_at"])
            last_error_ts = float(self._state["last_error_ts"])
            return {
                "thread_alive": thread_alive,
                "started_at": _fmt_ts(started_at),
                "last_queue_refresh_utc": _fmt_ts(queue_ts),
                "last_size_refresh_utc": _fmt_ts(size_ts),
                "queue_age_sec": max(0.0, time.time() - queue_ts) if queue_ts else None,
                "size_age_sec": max(0.0, time.time() - size_ts) if size_ts else None,
                "consecutive_errors": int(self._state["consecutive_errors"]),
                "last_error": self._state["last_error"],
                "last_error_utc": _fmt_ts(last_error_ts),
            }

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


@st.cache_resource(show_spinner=False)
def get_dashboard_stats_runtime() -> DashboardStatsRuntime:
    """Return process-stable dashboard runtime cache manager."""
    runtime = DashboardStatsRuntime()
    runtime.start()
    return runtime
