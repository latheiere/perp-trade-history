from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from perp_trade_history.dashboard.app import create_app
from perp_trade_history.dashboard.cli import _browser_url, build_parser, main
from perp_trade_history.dashboard.layout import build_layout
from perp_trade_history.dashboard.models import DashboardFilters
from perp_trade_history.dashboard.providers import SyntheticSnapshotProvider


def _component_ids(component: object) -> set[str]:
    found: set[str] = set()
    identifier = getattr(component, "id", None)
    if isinstance(identifier, str):
        found.add(identifier)
    children = getattr(component, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            found.update(_component_ids(child))
    elif children is not None:
        found.update(_component_ids(children))
    return found


def _find_component(component: object, identifier: str):
    if getattr(component, "id", None) == identifier:
        return component
    children = getattr(component, "children", None)
    candidates = children if isinstance(children, (list, tuple)) else [children]
    for child in candidates:
        if child is not None and (found := _find_component(child, identifier)) is not None:
            return found
    return None


def test_app_factory_builds_all_adaptive_sections_and_ready_endpoint() -> None:
    app = create_app(refresh_interval_ms=1_000)
    ids = _component_ids(app.layout)

    assert {
        "snapshot-poll",
        "market-class-filter",
        "kpi-grid",
        "performance-graph",
        "notable-changes",
        "heatmap-graph",
        "year-grid",
        "episode-grid",
        "episode-detail",
        "data-quality-grid",
    } <= ids
    response = app.server.test_client().get("/readyz")
    assert response.status_code == 200
    assert response.text == "OK"
    assert len(app.callback_map) == 4


def test_app_factory_rejects_excessive_refresh_frequency() -> None:
    with pytest.raises(ValueError, match="at least 1000"):
        create_app(refresh_interval_ms=999)


def test_app_factory_exposes_initial_provider_failure() -> None:
    class FailingProvider:
        def revision(self) -> str:
            return "failed-revision"

        def load(self, filters: DashboardFilters):
            raise RuntimeError("temporary analytics failure")

    app = create_app(FailingProvider(), refresh_interval_ms=1_000)
    banner = _find_component(app.layout, "data-state-banner")

    assert banner is not None
    assert banner.className == "data-state-banner"


def test_utc_source_timezone_is_not_duplicated_in_filter_options() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())
    layout = build_layout(replace(snapshot, timezone="UTC"), refresh_interval_ms=5_000)
    timezone = _find_component(layout, "timezone-filter")

    assert timezone.data == ["UTC"]


def test_cli_supports_runtime_arguments_and_wildcard_browser_target(monkeypatch) -> None:
    parsed = build_parser().parse_args(
        ["--synthetic", "--host", "0.0.0.0", "--port", "8090", "--no-browser"]
    )
    assert parsed.synthetic
    assert parsed.no_browser
    assert _browser_url(parsed.host, parsed.port) == "http://127.0.0.1:8090"

    served: dict[str, object] = {}
    monkeypatch.setattr(
        "perp_trade_history.dashboard.cli.create_app",
        lambda provider: SimpleNamespace(server="server"),
    )
    monkeypatch.setattr(
        "perp_trade_history.dashboard.cli.serve",
        lambda server, **kwargs: served.update(server=server, **kwargs),
    )
    monkeypatch.setattr(
        "perp_trade_history.dashboard.cli._open_browser_later",
        lambda url: pytest.fail(f"browser should not open: {url}"),
    )

    main(["--synthetic", "--host", "0.0.0.0", "--port", "8090", "--no-browser"])

    assert served == {"server": "server", "host": "0.0.0.0", "port": 8090, "threads": 8}
