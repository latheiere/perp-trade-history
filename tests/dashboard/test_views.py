from __future__ import annotations

from perp_trade_history.dashboard.models import DashboardFilters, NotableChange, empty_snapshot
from perp_trade_history.dashboard.providers import SyntheticSnapshotProvider
from perp_trade_history.dashboard.views import (
    _cashflow_figure,
    _execution_figure,
    collection_build_report,
    data_quality_cards,
    episode_detail,
    episode_rows,
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
