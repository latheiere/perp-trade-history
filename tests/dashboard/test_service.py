from __future__ import annotations

from dataclasses import replace

from perp_trade_history.dashboard.models import DashboardFilters
from perp_trade_history.dashboard.providers import SyntheticSnapshotProvider
from perp_trade_history.dashboard.service import SnapshotService


class MutableProvider:
    def __init__(self) -> None:
        self.current_revision = "revision-a"
        self.loads = 0
        self.fail_load = False
        self.snapshot = SyntheticSnapshotProvider(revision=self.current_revision).load(
            DashboardFilters()
        )

    def revision(self) -> str:
        return self.current_revision

    def load(self, filters: DashboardFilters):
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("temporary analytics failure")
        return replace(self.snapshot, revision=self.current_revision)


def test_service_caches_by_revision_and_filter() -> None:
    provider = MutableProvider()
    service = SnapshotService(provider)
    service.poll()

    first = service.get(DashboardFilters())
    second = service.get(DashboardFilters())

    assert first is second
    assert provider.loads == 1


def test_service_retains_last_good_snapshot_across_failed_revision() -> None:
    provider = MutableProvider()
    service = SnapshotService(provider)
    service.poll()
    previous = service.get(DashboardFilters())

    provider.current_revision = "revision-b"
    provider.fail_load = True
    signal = service.poll()
    current = service.get(DashboardFilters())

    assert signal.revision == "revision-b"
    assert current is previous
    assert service.signal().status == "error"


def test_service_returns_explicit_empty_state_on_initial_failure() -> None:
    provider = MutableProvider()
    provider.fail_load = True
    service = SnapshotService(provider)
    service.poll()

    snapshot = service.get(DashboardFilters())

    assert snapshot.status == "empty"
    assert "retry automatically" in snapshot.message


def test_cache_bounds_current_and_last_good_reports_with_lru_eviction() -> None:
    provider = MutableProvider()
    service = SnapshotService(provider, max_entries=2)
    service.poll()
    first, second, third = (DashboardFilters(query=value) for value in ("first", "second", "third"))
    first_snapshot = service.get(first)
    service.get(second)
    assert service.get(first) is first_snapshot
    service.get(third)
    assert provider.loads == 3

    provider.current_revision = "revision-b"
    provider.fail_load = True
    service.poll()
    assert service.get(first) is first_snapshot
    assert service.get(second).status == "empty"
    assert len(service._cache) == 2
    assert service._cache_bytes <= service._max_bytes

    provider.fail_load = False
    tiny = SnapshotService(provider, max_bytes=256)
    tiny.poll()
    tiny.get(first)
    tiny.get(first)
    assert len(tiny._cache) == 0
    assert tiny._cache_bytes == 0
    assert tiny._sweeper is not None


def test_cache_releases_idle_reports_without_requests_and_stops_on_close() -> None:
    import time

    import pytest

    provider = MutableProvider()
    service = SnapshotService(provider, ttl_seconds=0.05)
    service.poll()
    service.get(DashboardFilters())
    deadline = time.monotonic() + 1
    while service._cache and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not service._cache
    assert service._cache_bytes == 0
    service.get(DashboardFilters())
    assert provider.loads == 2
    service.close()
    assert not service._sweeper.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        service.get(DashboardFilters())


def test_cache_rejects_invalid_retention_limits() -> None:
    import pytest

    provider = MutableProvider()
    for options in (
        {"max_entries": 0}, {"max_entries": float("inf")},
        {"max_bytes": 0}, {"max_bytes": float("inf")},
        {"ttl_seconds": 0}, {"ttl_seconds": float("nan")},
    ):
        with pytest.raises(ValueError, match="finite and positive"):
            SnapshotService(provider, **options)
