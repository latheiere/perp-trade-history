from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from dash import no_update

from perp_trade_history.dashboard.app import _pattern_drill, _pattern_label, create_app
from perp_trade_history.dashboard.cli import _browser_url, build_parser, main
from perp_trade_history.dashboard.layout import EPISODE_COLUMNS, build_layout
from perp_trade_history.dashboard.models import DashboardFilters, FilterOption
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


def _text(component: object) -> str:
    if isinstance(component, str):
        return component
    children = getattr(component, "children", None)
    if isinstance(children, (list, tuple)):
        return " ".join(_text(child) for child in children)
    return _text(children) if children is not None else ""


def test_app_factory_builds_all_adaptive_sections_and_ready_endpoint() -> None:
    app = create_app(refresh_interval_ms=1_000)
    ids = _component_ids(app.layout)

    assert {
        "snapshot-poll",
        "market-class-filter",
        "date-range-filter",
        "timezone-filter",
        "performance-view-filter",
        "pattern-filter",
        "kpi-grid",
        "performance-graph",
        "notable-changes",
        "heatmap-graph",
        "year-grid",
        "episode-grid",
        "episode-detail",
        "data-quality-grid",
        "collection-build-report",
    } <= ids
    response = app.server.test_client().get("/readyz")
    assert response.status_code == 200
    assert response.text == "OK"
    assert len(app.callback_map) == 6
    callback_inputs = {
        (item["id"], item["property"])
        for callback in app.callback_map.values()
        for item in callback["inputs"]
    }
    assert ("heatmap-graph", "clickData") in callback_inputs
    assert ("date-range-filter", "start_date") in callback_inputs
    assert ("performance-view-filter", "value") in callback_inputs


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
    layout = build_layout(
        replace(
            snapshot,
            timezone="UTC",
            timezone_options=(FilterOption("UTC", "UTC"),),
        ),
        refresh_interval_ms=5_000,
    )
    timezone = _find_component(layout, "timezone-filter")

    assert timezone.data == [{"label": "UTC", "value": "UTC"}]


def test_episode_grid_only_exposes_supported_economic_fields() -> None:
    fields = {column["field"] for column in EPISODE_COLUMNS}

    assert {"instrument", "status", "net_result", "currency", "outcome"} <= fields
    assert not {"r_multiple", "mae", "mfe", "benchmark"} & fields
    net_result = next(column for column in EPISODE_COLUMNS if column["field"] == "net_result")
    assert set(net_result["cellClassRules"]) == {"positive-cell", "negative-cell"}

    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())
    layout = build_layout(snapshot, refresh_interval_ms=5_000)
    outcome = _find_component(layout, "outcome-filter")
    assert {"Win", "Loss", "Flat", "Open"} <= set(outcome.data)
    rendered = _text(layout)
    assert all(control not in rendered for control in ("Theme", "Settings", "Export", "View all"))


def test_pattern_drill_maps_heatmap_and_exposure_to_supported_filters() -> None:
    heatmap, duration, direction = _pattern_drill(
        "heatmap-graph",
        {"points": [{"x": "06", "y": "Thu"}]},
    )
    assert heatmap == {"source": "heatmap", "weekday": 3, "hour_bucket": 6}
    assert duration is no_update
    assert direction is no_update

    exposure, duration, direction = _pattern_drill(
        {"type": "exposure-drill", "value": "1d_to_7d", "side": "short"},
        None,
    )
    assert exposure == {"source": "exposure", "label": "1d_to_7d"}
    assert duration == "1d_to_7d"
    assert direction == "Short"


def test_pattern_label_reflects_effective_episode_filters_during_drill_refresh() -> None:
    assert _pattern_label(None, "Long", "under_1h") == "Long · Under 1 hour"
    assert _pattern_label({}, "All directions", "all") == "No pattern focus"
    assert (
        _pattern_label(
            {"source": "exposure", "label": "1d_to_7d"},
            "Short",
            "1d_to_7d",
        )
        == "1 – 7 days"
    )


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
