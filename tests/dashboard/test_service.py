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
