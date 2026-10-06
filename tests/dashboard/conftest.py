import pytest

from perp_trade_history.dashboard.service import SnapshotService


@pytest.fixture(autouse=True)
def close_report_cache_workers(monkeypatch):
    services = []
    initialize = SnapshotService.__init__

    def tracked_initialize(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        services.append(self)

    monkeypatch.setattr(SnapshotService, "__init__", tracked_initialize)
    yield
    for service in services:
        service.close()
