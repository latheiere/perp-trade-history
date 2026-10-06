from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from math import isfinite
from sys import getsizeof
from threading import Event, RLock, Thread
from time import monotonic

from perp_trade_history.dashboard.models import DashboardFilters, DashboardSnapshot, empty_snapshot
from perp_trade_history.dashboard.providers import AnalyticsSnapshotProvider, SnapshotProvider


@dataclass(frozen=True, slots=True)
class RefreshSignal:
    revision: str
    status: str
    message: str
    sequence: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "revision": self.revision,
            "status": self.status,
            "message": self.message,
            "sequence": self.sequence,
        }


@dataclass(slots=True)
class _SnapshotEntry:
    revision: str
    snapshot: DashboardSnapshot
    last_good: DashboardSnapshot | None
    expires_at: float
    size: int


class SnapshotService:
    """Coordinate revision polling and last-known-good filtered snapshots."""

    def __init__(
        self, provider: SnapshotProvider, *, max_entries: int = 32,
        max_bytes: int = 8 * 1024 * 1024, ttl_seconds: float = 300,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            not isinstance(max_entries, int) or max_entries < 1
            or not isinstance(max_bytes, int) or max_bytes < 1
            or not isfinite(ttl_seconds) or ttl_seconds <= 0
        ):
            raise ValueError("snapshot cache limits must be finite and positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl = ttl_seconds
        self._clock = clock
        self._stop = Event()
        self._sweeper: Thread | None = None
        self._provider = provider
        self._lock = RLock()
        self._revision = "pending"
        self._status = "loading"
        self._message = "Loading analytics data"
        self._sequence = 0
        self._cache: OrderedDict[DashboardFilters, _SnapshotEntry] = OrderedDict()
        self._cache_bytes = 0

    def poll(self) -> RefreshSignal:
        with self._lock:
            self._expire()
            previous = (self._revision, self._status, self._message)
            try:
                revision = str(self._provider.revision()).strip() or "empty"
            except Exception as exc:  # provider errors become visible, non-fatal UI state
                self._status = "error"
                self._message = _safe_message(exc)
            else:
                if revision != self._revision:
                    self._revision = revision
                self._status = "ready"
                self._message = ""
            if previous != (self._revision, self._status, self._message):
                self._sequence += 1
            return self.signal()

    def signal(self) -> RefreshSignal:
        return RefreshSignal(
            revision=self._revision,
            status=self._status,
            message=self._message,
            sequence=self._sequence,
        )

    def get(self, filters: DashboardFilters) -> DashboardSnapshot:
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("snapshot service is closed")
            self._expire()
            entry = self._cache.get(filters)
            if entry:
                self._cache.move_to_end(filters)
                entry.expires_at = self._clock() + self._ttl
                if entry.revision == self._revision:
                    return entry.snapshot
            try:
                snapshot = self._provider.load(filters)
            except Exception as exc:  # keep the page usable while exposing the failure
                self._status = "error"
                self._message = _safe_message(exc)
                self._sequence += 1
                return (entry.last_good if entry else None) or empty_snapshot(
                    revision=self._revision,
                    message=(
                        "Analytics data could not be loaded. "
                        "The dashboard will retry automatically."
                    ),
                )
            last_good = (
                snapshot if snapshot.status == "ready" else entry.last_good if entry else None
            )
            self._retain(filters, snapshot, last_good)
            self._status = snapshot.status
            self._message = snapshot.message
            return snapshot

    def _retain(
        self, filters: DashboardFilters, snapshot: DashboardSnapshot,
        last_good: DashboardSnapshot | None,
    ) -> None:
        previous = self._cache.pop(filters, None)
        if previous:
            self._cache_bytes -= previous.size
        size = _retained_size((filters, snapshot, last_good), limit=self._max_bytes) + 256
        if size > self._max_bytes:
            return
        while self._cache and (
            len(self._cache) >= self._max_entries or self._cache_bytes + size > self._max_bytes
        ):
            self._cache_bytes -= self._cache.popitem(last=False)[1].size
        self._cache[filters] = _SnapshotEntry(
            self._revision, snapshot, last_good, self._clock() + self._ttl, size,
        )
        self._cache_bytes += size
        if self._sweeper is None:
            self._sweeper = Thread(target=self._sweep, name="report-cache-expiry", daemon=True)
            self._sweeper.start()

    def _expire(self) -> None:
        now = self._clock()
        for key, entry in tuple(self._cache.items()):
            if entry.expires_at <= now:
                self._cache_bytes -= self._cache.pop(key).size

    def _sweep(self) -> None:
        while not self._stop.wait(min(self._ttl / 2, 60)):
            with self._lock:
                self._expire()
                if not self._cache and isinstance(self._provider, AnalyticsSnapshotProvider):
                    self._provider.clear_cache()

    def close(self) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=2)
        with self._lock:
            self._cache.clear()
            self._cache_bytes = 0
            if isinstance(self._provider, AnalyticsSnapshotProvider):
                self._provider.clear_cache()


def _retained_size(value: object, *, limit: int) -> int:
    """Count a snapshot graph with bounded traversal and no container copies."""
    pending = [iter((value,))]
    seen: set[int] = set()
    size = 0
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if id(item) in seen:
            continue
        seen.add(id(item))
        size += getsizeof(item)
        if size > limit:
            return size
        if is_dataclass(item) and not isinstance(item, type):
            pending.append(map(lambda field, owner=item: getattr(owner, field.name), fields(item)))
        elif isinstance(item, (tuple, list)):
            pending.append(iter(item))
    return size


def _safe_message(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return (text[:197] + "...") if len(text) > 200 else text or "Analytics provider failed"
