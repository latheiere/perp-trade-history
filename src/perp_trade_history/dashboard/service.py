from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from perp_trade_history.dashboard.models import DashboardFilters, DashboardSnapshot, empty_snapshot
from perp_trade_history.dashboard.providers import SnapshotProvider


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


class SnapshotService:
    """Coordinate revision polling and last-known-good filtered snapshots."""

    def __init__(self, provider: SnapshotProvider) -> None:
        self._provider = provider
        self._lock = RLock()
        self._revision = "pending"
        self._status = "loading"
        self._message = "Loading analytics data"
        self._sequence = 0
        self._cache: dict[tuple[str, DashboardFilters], DashboardSnapshot] = {}
        self._last_good: dict[DashboardFilters, DashboardSnapshot] = {}

    def poll(self) -> RefreshSignal:
        with self._lock:
            previous = (self._revision, self._status, self._message)
            try:
                revision = str(self._provider.revision()).strip() or "empty"
            except Exception as exc:  # provider errors become visible, non-fatal UI state
                self._status = "error"
                self._message = _safe_message(exc)
            else:
                if revision != self._revision:
                    self._revision = revision
                    self._cache.clear()
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
            key = (self._revision, filters)
            if key in self._cache:
                return self._cache[key]
            try:
                snapshot = self._provider.load(filters)
            except Exception as exc:  # keep the page usable while exposing the failure
                self._status = "error"
                self._message = _safe_message(exc)
                self._sequence += 1
                return self._last_good.get(filters) or empty_snapshot(
                    revision=self._revision,
                    message=(
                        "Analytics data could not be loaded. "
                        "The dashboard will retry automatically."
                    ),
                )
            self._cache[key] = snapshot
            if snapshot.status == "ready":
                self._last_good[filters] = snapshot
            self._status = snapshot.status
            self._message = snapshot.message
            return snapshot


def _safe_message(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return (text[:197] + "...") if len(text) > 200 else text or "Analytics provider failed"
