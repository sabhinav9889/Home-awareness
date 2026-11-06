"""Async client for the Home Awareness server's device API, plus an in-memory event queue.

The device key is only ever sent in the X-Device-Key header; it is never logged.
The queue is memory-only by design (privacy and simplicity).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_RETRY_AFTER_S = 1.0
MAX_QUEUE_ITEMS = 500


class ApiError(Exception):
    """Base class for server API errors."""


class DeviceRevoked(ApiError):
    """Server returned 401: the device key is unknown or revoked."""


class RateLimited(ApiError):
    def __init__(self, retry_after_s: float = DEFAULT_RETRY_AFTER_S):
        super().__init__(f"rate limited; retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class ServerUnavailable(ApiError):
    """Network error or 5xx."""


class Rejected(ApiError):
    """Server rejected the request (4xx other than 401/429); do not retry."""

    def __init__(self, detail: str = ""):
        super().__init__(detail)
        self.detail = detail


class PairingFailed(ApiError):
    """Pairing code invalid or expired."""


@dataclass(frozen=True)
class DeviceConfig:
    enabled_sounds: frozenset[str]
    heartbeat_interval_s: int


@dataclass(frozen=True)
class EventResult:
    event_id: int
    merged: bool
    alerts: int


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail", "")
        return d if isinstance(d, str) else str(d)
    except (ValueError, AttributeError):
        return ""


def _retry_after(resp: httpx.Response) -> float:
    try:
        v = float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return v if v >= 0 else DEFAULT_RETRY_AFTER_S


class ServerClient:
    def __init__(
        self,
        base_url: str,
        device_key: str | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._key = device_key
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict | None = None, auth: bool = True) -> httpx.Response:
        headers = {"X-Device-Key": self._key} if auth and self._key else {}
        try:
            resp = await self._http.post(self._base + path, json=body, headers=headers)
        except httpx.HTTPError as e:
            # type name only: keep URLs/headers out of the message
            raise ServerUnavailable(type(e).__name__) from None
        if resp.status_code == 401:
            raise DeviceRevoked()
        if resp.status_code == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status_code >= 500:
            raise ServerUnavailable(f"HTTP {resp.status_code}")
        return resp

    async def pair(self, code: str) -> tuple[int, str]:
        try:
            resp = await self._post("/device/pair", {"code": code}, auth=False)
        except DeviceRevoked:
            raise PairingFailed("pairing refused") from None
        if resp.status_code == 400:
            raise PairingFailed(_detail(resp) or "invalid or expired pairing code")
        if resp.status_code != 201:
            raise Rejected(_detail(resp) or f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            return int(data["device_id"]), str(data["device_key"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ServerUnavailable("malformed pair response") from None

    async def heartbeat(self) -> DeviceConfig:
        resp = await self._post("/device/heartbeat")
        if resp.status_code != 200:
            raise Rejected(_detail(resp) or f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            return DeviceConfig(
                frozenset(data["enabled_sounds"]), int(data["heartbeat_interval_s"])
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ServerUnavailable("malformed heartbeat response") from None

    async def send_event(self, sound: str, confidence: float, occurred_at: datetime) -> EventResult:
        if occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        body = {
            "type": sound,
            "confidence": confidence,
            "occurred_at": occurred_at.isoformat(),
        }
        resp = await self._post("/device/events", body)
        if resp.status_code != 202:
            raise Rejected(_detail(resp) or f"HTTP {resp.status_code}")
        # 202 means the server accepted the event. If the body is unreadable, still report
        # success (event_id=-1) so the queue does not resend and create a duplicate.
        try:
            data = resp.json()
            return EventResult(int(data["event_id"]), bool(data["merged"]), int(data["alerts"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            return EventResult(-1, False, 0)


@dataclass(frozen=True)
class _Pending:
    sound: str
    confidence: float
    occurred_at: datetime


class EventQueue:
    """In-memory FIFO of unsent events. Never persisted. Capped; oldest dropped first."""

    def __init__(self, max_age_s: float = 600, max_items: int = MAX_QUEUE_ITEMS):
        self._max_age = timedelta(seconds=max_age_s)
        self._max_items = max_items
        self._items: deque[_Pending] = deque()
        self._overflow_logged = False

    def __len__(self) -> int:
        return len(self._items)

    def oldest_occurred_at(self) -> datetime | None:
        return self._items[0].occurred_at if self._items else None

    def put(self, sound: str, conf: float, occurred_at: datetime) -> None:
        if occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        self._items.append(_Pending(sound, conf, occurred_at))
        while len(self._items) > self._max_items:
            self._items.popleft()
            if not self._overflow_logged:
                self._overflow_logged = True
                log.warning("event queue full (%d); dropping oldest events", self._max_items)

    def _pop_if_head(self, item: _Pending) -> None:
        # A put() during an awaited send may have evicted this item at the cap.
        if self._items and self._items[0] is item:
            self._items.popleft()

    async def drain(self, client: ServerClient, now: datetime) -> int:
        """Send oldest first; return the number delivered.

        Stale items (older than max_age_s) and Rejected items are dropped.
        ServerUnavailable and RateLimited propagate with the failed item (and the rest)
        kept; the caller backs off (RateLimited.retry_after_s). DeviceRevoked propagates.
        """
        sent = 0
        while self._items:
            item = self._items[0]
            if item.occurred_at.utcoffset() is None or now - item.occurred_at > self._max_age:
                self._pop_if_head(item)  # naive (should be impossible) or stale
                continue
            try:
                await client.send_event(item.sound, item.confidence, item.occurred_at)
            except Rejected:
                self._pop_if_head(item)
                continue
            self._pop_if_head(item)
            sent += 1
        if not self._items:
            self._overflow_logged = False
        return sent
