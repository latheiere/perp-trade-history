from __future__ import annotations

from perp_trade_history.dashboard.models import NotableChange, empty_snapshot
from perp_trade_history.dashboard.views import (
    heatmap_figure,
    notable_changes,
    performance_figure,
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
