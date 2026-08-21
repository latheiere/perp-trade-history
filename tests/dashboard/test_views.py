from __future__ import annotations

from dataclasses import replace

import dash_ag_grid as dag
import dash_mantine_components as dmc

from perp_trade_history.dashboard.models import (
    DashboardFilters,
    ExposureBand,
    NotableChange,
    empty_snapshot,
)
from perp_trade_history.dashboard.providers import SyntheticSnapshotProvider
from perp_trade_history.dashboard.views import (
    _cashflow_figure,
    _constant_price_range,
    _execution_figure,
    _number,
    _price_decimals,
    collection_build_report,
    data_quality_cards,
    episode_detail,
    episode_rows,
    exposure_panel,
    heatmap_figure,
    notable_changes,
    performance_figure,
    year_cards,
)


def _text(component: object) -> str:
    if isinstance(component, str):
        return component
    if isinstance(component, (list, tuple)):
        return " ".join(_text(child) for child in component)
    title = getattr(component, "title", "")
    children = getattr(component, "children", None)
    if isinstance(children, (list, tuple)):
        body = " ".join(_text(child) for child in children)
    else:
        body = _text(children) if children is not None else ""
    return f"{title} {body}".strip()


def test_notable_changes_has_explicit_unavailable_and_low_confidence_states() -> None:
    unavailable = notable_changes(())
    assert "Notable changes unavailable" in _text(unavailable)

    rendered = notable_changes(
        (
            NotableChange(
                1,
                "Observed frequency changed",
                "The latest comparison crossed the configured threshold.",
                7,
                "Low confidence",
                "info",
            ),
        )
    )
    assert "Low confidence" in _text(rendered)


def test_empty_snapshot_figures_explain_unavailable_data() -> None:
    snapshot = empty_snapshot()

    performance = performance_figure(snapshot)
    heatmap = heatmap_figure(snapshot)

    assert "No performance history" in performance.layout.annotations[0].text
    assert "No weekday or hour pattern" in heatmap.layout.annotations[0].text


def test_performance_selector_uses_only_supported_reporting_amount_views() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())

    for view, field in {
        "cumulative": "net",
        "period": "period",
        "rolling": "rolling",
        "drawdown": "drawdown",
    }.items():
        figure = performance_figure(snapshot, view)
        assert list(figure.data[0].y) == [getattr(point, field) for point in snapshot.performance]
        assert "benchmark" not in figure.data[0].name.lower()
        assert figure.data[0].type == ("bar" if view == "period" else "scatter")


def test_episode_views_present_supported_economics_and_source_details() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())
    episode = snapshot.episodes[0]
    row = episode_rows((episode,))[0]

    assert row["instrument"] == episode.instrument
    assert row["status"] == episode.status
    assert row["net_result"] == episode.net_result
    assert not {"r_multiple", "mae", "mfe"} & row.keys()

    text = _text(episode_detail(episode))
    assert "Executions" in text
    assert "Cashflows" in text
    assert episode.instrument in text
    assert "R multiple" not in text
    assert _execution_figure(episode.executions).data[0].type == "scatter"
    assert _cashflow_figure(episode.cashflows).data[0].type == "bar"

    annual = _text(year_cards(snapshot.years[:1], snapshot.currency))
    assert "Cashflow distribution" in annual
    assert "Result distribution (R)" not in annual


def test_episode_detail_defers_inactive_panels_and_virtualizes_evidence_rows() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())
    original = snapshot.episodes[0]
    episode = replace(
        original,
        executions=original.executions * 150,
        cashflows=original.cashflows * 150,
    )

    detail = episode_detail(episode)
    tabs = _components(detail, dmc.Tabs)
    grids = _components(detail, dag.AgGrid)

    assert len(tabs) == 1
    assert tabs[0].keepMounted is False
    assert len(grids) == 2
    assert len(grids[0].rowData) == len(episode.executions)
    assert len(grids[1].rowData) == len(episode.cashflows)
    assert all(grid.dashGridOptions["rowBuffer"] == 4 for grid in grids)
    assert all(grid.style["height"] == "320px" for grid in grids)


def test_execution_price_chart_uses_visible_hover_and_magnitude_precision() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())
    figure = _execution_figure(snapshot.episodes[0].executions)
    formatted_execution = replace(snapshot.episodes[0].executions[0], price="3,664.75")
    formatted_figure = _execution_figure((formatted_execution,))

    assert figure.layout.hoverlabel.font.color == "#f4f9fc"
    assert figure.layout.hoverlabel.bgcolor == "#102b3b"
    assert figure.layout.yaxis.tickformat in {",.2f", ",.4f", ",.6f", ",.8f", ",.10f"}
    assert "%{customdata[4]}" in figure.data[0].hovertemplate
    assert list(formatted_figure.data[0].y) == [3664.75]
    assert _number("3,664.75") == 3664.75
    assert _number("Unavailable") is None
    assert _price_decimals((125.0,)) == 2
    assert _price_decimals((1.25,)) == 4
    assert _price_decimals((0.021375,)) == 6
    assert _price_decimals((0.00021,)) == 8
    assert _constant_price_range((0.00641, 0.00641), decimals=8) == (
        0.0062818,
        0.0065382,
    )
    assert _constant_price_range((0.00641, 0.00642), decimals=8) is None


def test_exposure_rows_repeat_duration_labels_and_color_by_result() -> None:
    item = ExposureBand(
        "1h – 1d",
        "1h_to_1d",
        3,
        7.18,
        8,
        -743.59,
        -538.83,
    )
    rendered = exposure_panel((item,), "Avg USDT", "Avg USDT")
    text = _text(rendered)
    classes = _class_names(rendered)

    assert text.count("1h – 1d") == 3
    assert classes.count("metric-bar-fill positive") == 1
    assert classes.count("metric-bar-fill negative") == 2


def test_quality_area_separates_source_coverage_and_episode_boundaries() -> None:
    snapshot = SyntheticSnapshotProvider().load(DashboardFilters())

    cards = data_quality_cards(snapshot.coverage, snapshot.build)
    report = collection_build_report(snapshot.coverage_gaps)
    rendered = _text(cards)

    assert "Source interval coverage" in rendered
    assert "Reconstructed boundaries" in rendered
    assert "Unsupported metrics" not in rendered
    assert "Collection build report" in _text(report)
    assert report.value is None


def _class_names(component: object) -> list[str]:
    names: list[str] = []
    class_name = getattr(component, "className", None)
    if isinstance(class_name, str):
        names.append(class_name)
    children = getattr(component, "children", None)
    candidates = children if isinstance(children, (list, tuple)) else [children]
    for child in candidates:
        if child is not None:
            names.extend(_class_names(child))
    return names


def _components(component: object, component_type: type) -> list[object]:
    matches = [component] if isinstance(component, component_type) else []
    children = getattr(component, "children", None)
    candidates = children if isinstance(children, (list, tuple)) else [children]
    for child in candidates:
        if child is not None:
            matches.extend(_components(child, component_type))
    return matches
