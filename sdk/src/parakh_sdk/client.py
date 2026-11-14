from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from parakh_sdk.evaluation import evaluate

logger = logging.getLogger(__name__)


class ParakhClient:
    """Poll remote configuration and evaluate locally without per-check network I/O."""

    def __init__(
        self,
        sdk_key: str,
        base_url: str,
        poll_interval: float = 10,
        *,
        event_batch_size: int = 50,
        event_flush_interval: float = 2,
        request_timeout: float = 2,
    ) -> None:
        if poll_interval <= 0 or event_batch_size <= 0 or event_flush_interval <= 0:
            raise ValueError("polling and batching intervals must be positive")
        self._sdk_key = sdk_key
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._event_batch_size = event_batch_size
        self._event_flush_interval = event_flush_interval
        self._timeout = request_timeout
        self._config_lock = threading.Lock()
        self._config: dict[str, Any] | None = None
        self._etag: str | None = None
        self._exposure_lock = threading.Lock()
        self._seen_exposures: set[tuple[str, str]] = set()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10_000)
        self._closed = threading.Event()
        self._config_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._config_thread.start()
        self._event_thread.start()

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._sdk_key}"}
        if self._etag:
            headers["If-None-Match"] = f'"{self._etag}"'
        return headers

    def _poll_once(self) -> None:
        try:
            response = httpx.get(
                f"{self._base_url}/v1/config",
                headers=self._headers(),
                timeout=self._timeout,
            )
            if response.status_code == 304:
                return
            response.raise_for_status()
            config = response.json()
            etag = response.headers.get("ETag", "").strip('"') or None
            with self._config_lock:
                self._config = config
                self._etag = etag
        except (httpx.HTTPError, ValueError):
            logger.warning("Parakh config refresh failed; retaining last known configuration")

    def _poll_loop(self) -> None:
        while not self._closed.is_set():
            self._poll_once()
            self._closed.wait(self._poll_interval)

    def is_enabled(self, flag_key: str, user: str | dict[str, Any]) -> bool:
        return self.get_value(flag_key, user, False) is True

    def get_value(
        self,
        flag_key: str,
        user: str | dict[str, Any],
        default: Any = None,
    ) -> Any:
        user_id, attributes = self._normalize_user(user)
        with self._config_lock:
            config = self._config
        if config is None:
            return default
        flag = next((item for item in config.get("flags", []) if item.get("key") == flag_key), None)
        if flag is None:
            return default
        result = evaluate(flag, user_id, attributes)
        exposure_key = (flag_key, user_id)
        if flag.get("experiment") and result.get("variant") is not None:
            with self._exposure_lock:
                should_track_exposure = exposure_key not in self._seen_exposures
                self._seen_exposures.add(exposure_key)
            if not should_track_exposure:
                return result["value"]
            self.track(
                "$exposure",
                user_id,
                value=result["value"],
                properties={
                    "flag_key": flag_key,
                    "variant": result.get("variant", str(result["value"])),
                },
            )
        return result["value"]

    @staticmethod
    def _normalize_user(user: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if isinstance(user, str):
            return user, {}
        user_id = user.get("id")
        if user_id is None:
            raise ValueError("user dictionaries must contain an 'id' field")
        return str(user_id), {key: value for key, value in user.items() if key != "id"}

    def track(
        self,
        event_name: str,
        user: str | dict[str, Any],
        value: Any = None,
        *,
        properties: dict[str, Any] | None = None,
    ) -> None:
        user_id, _ = self._normalize_user(user)
        event = {
            "event_id": str(uuid.uuid4()),
            "event_name": event_name,
            "user_id": user_id,
            "value": value,
            "flag_key": (properties or {}).get("flag_key"),
            "variant": (properties or {}).get("variant"),
            "created_at": datetime.now(UTC).isoformat(),
            "properties": properties or {},
        }
        try:
            self._events.put_nowait(event)
        except queue.Full:
            logger.error("Parakh event queue is full; dropping event %s", event_name)

    def _event_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._closed.is_set() or not self._events.empty() or batch:
            timeout = min(
                0.25,
                max(0.05, self._event_flush_interval - (time.monotonic() - last_flush)),
            )
            try:
                batch.append(self._events.get(timeout=timeout))
            except queue.Empty:
                pass
            should_flush = batch and (
                len(batch) >= self._event_batch_size
                or time.monotonic() - last_flush >= self._event_flush_interval
                or (self._closed.is_set() and self._events.empty())
            )
            if should_flush:
                if self._flush_batch(batch):
                    for _ in batch:
                        self._events.task_done()
                    batch.clear()
                    last_flush = time.monotonic()
                else:
                    self._closed.wait(min(self._event_flush_interval, 2))
                    if self._closed.is_set():
                        # Leave failed events unsent rather than holding close() forever.
                        for _ in batch:
                            self._events.task_done()
                        batch.clear()

    def _flush_batch(self, batch: list[dict[str, Any]]) -> bool:
        try:
            response = httpx.post(
                f"{self._base_url}/v1/events",
                headers={"Authorization": f"Bearer {self._sdk_key}"},
                json={"events": batch},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            logger.warning("Parakh event batch could not be delivered")
            return False

    def close(self, timeout: float = 5) -> None:
        self._closed.set()
        self._config_thread.join(timeout=timeout)
        self._event_thread.join(timeout=timeout)

    def __enter__(self) -> ParakhClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
