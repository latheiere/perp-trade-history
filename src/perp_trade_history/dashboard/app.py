from __future__ import annotations

from pathlib import Path
from typing import Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, no_update

from perp_trade_history.dashboard.layout import build_layout
from perp_trade_history.dashboard.models import DashboardFilters
from perp_trade_history.dashboard.providers import SnapshotProvider, SyntheticSnapshotProvider
from perp_trade_history.dashboard.service import SnapshotService
from perp_trade_history.dashboard.views import (
    context_card,
    data_quality_cards,
    episode_detail,
    episode_rows,
    exposure_panel,
    heatmap_figure,
    kpi_cards,
    notable_changes,
    performance_figure,
    year_cards,
)


def create_app(
    provider: SnapshotProvider | None = None,
    *,
    refresh_interval_ms: int = 5_000,
    title: str = "Local analytics",
) -> Dash:
    if refresh_interval_ms < 1_000:
        raise ValueError("refresh_interval_ms must be at least 1000")
    service = SnapshotService(provider or SyntheticSnapshotProvider())
    service.poll()
    initial = service.get(DashboardFilters())
    assets = Path(__file__).with_name("assets")
    app = Dash(
        __name__,
        title=title,
        assets_folder=str(assets),
        health_endpoint="readyz",
        suppress_callback_exceptions=False,
    )
    app.layout = build_layout(initial, refresh_interval_ms=refresh_interval_ms)
    app.snapshot_service = service  # type: ignore[attr-defined]
    _register_callbacks(app, service)
    return app


def _register_callbacks(app: Dash, service: SnapshotService) -> None:
    @app.callback(
        Output("snapshot-signal", "data"),
        Output("refresh-status", "children"),
        Input("snapshot-poll", "n_intervals"),
        State("snapshot-signal", "data"),
    )
    def poll_snapshot(_tick: int, previous: dict[str, Any] | None):
        service.poll()
        snapshot = service.get(DashboardFilters())
        signal = service.signal()
        payload = signal.to_dict()
        status_text = "Refresh delayed" if signal.status == "error" else snapshot.refreshed_label
        if previous == payload:
            return no_update, status_text
        return payload, status_text

    @app.callback(
        Output("profile-filter", "data"),
        Output("profile-filter", "value"),
        Output("market-class-filter", "data"),
        Output("market-class-filter", "value"),
        Input("snapshot-signal", "data"),
        State("profile-filter", "value"),
        State("market-class-filter", "value"),
    )
    def update_filter_options(
        _signal: dict[str, Any],
        current_profile: str | None,
        current_market_class: str | None,
    ):
        snapshot = service.get(DashboardFilters())
        profiles = [{"label": item.label, "value": item.value} for item in snapshot.profiles]
        market_classes = [
            {"label": item.label, "value": item.value} for item in snapshot.market_classes
        ]
        valid_profiles = {item["value"] for item in profiles}
        valid_market_classes = {item["value"] for item in market_classes}
        return (
            profiles,
            current_profile if current_profile in valid_profiles else "all",
            market_classes,
            current_market_class if current_market_class in valid_market_classes else "all",
        )

    @app.callback(
        Output("kpi-grid", "children"),
        Output("performance-graph", "figure"),
        Output("performance-caption", "children"),
        Output("notable-changes", "children"),
        Output("heatmap-graph", "figure"),
        Output("exposure-panel", "children"),
        Output("context-card", "children"),
        Output("year-grid", "children"),
        Output("episode-grid", "rowData"),
        Output("episode-grid", "selectedRows"),
        Output("episode-count", "children"),
        Output("data-quality-grid", "children"),
        Output("footer-timezone", "children"),
        Output("data-state-banner", "children"),
        Output("data-state-banner", "className"),
        Input("snapshot-signal", "data"),
        Input("profile-filter", "value"),
        Input("market-class-filter", "value"),
        Input("interval-filter", "value"),
        Input("horizon-filter", "value"),
        Input("direction-filter", "value"),
        Input("duration-filter", "value"),
        Input("outcome-filter", "value"),
        Input("episode-search", "value"),
    )
    def render_snapshot(
        signal: dict[str, Any] | None,
        profile: str | None,
        market_class: str | None,
        interval: str | None,
        horizon: str | None,
        direction: str | None,
        duration: str | None,
        outcome: str | None,
        query: str | None,
    ):
        filters = DashboardFilters(
            profile=profile or "all",
            market_class=market_class or "all",
            interval=interval or "month",
            horizon=horizon or "all",
            direction=_normalized_filter(direction, "All directions"),
            duration=duration or "all",
            outcome=_normalized_filter(outcome, "All outcomes"),
            query=query or "",
        )
        snapshot = service.get(filters)
        rows = episode_rows(snapshot.episodes)
        selected_rows = rows[:1]
        banner, banner_class = _state_banner(snapshot.status, snapshot.message, signal)
        return (
            kpi_cards(snapshot.kpis),
            performance_figure(snapshot),
            (
                f"{snapshot.coverage.percent:.1f}% complete coverage"
                if snapshot.coverage.total
                else "Coverage unavailable"
            ),
            notable_changes(snapshot.notable_changes),
            heatmap_figure(snapshot),
            exposure_panel(
                snapshot.exposure,
                snapshot.exposure_metric_label,
                snapshot.exposure_net_label,
            ),
            context_card(snapshot.coverage, snapshot.timezone),
            year_cards(snapshot.years, snapshot.currency),
            rows,
            selected_rows,
            f"Showing 1–{min(20, len(rows))} of {len(rows):,}" if rows else "No matching episodes",
            data_quality_cards(snapshot.coverage, snapshot.unsupported_metrics, snapshot.build),
            f"All times shown in {snapshot.timezone}",
            banner,
            banner_class,
        )

    @app.callback(
        Output("episode-detail", "children"),
        Input("episode-grid", "selectedRows"),
        Input("snapshot-signal", "data"),
        State("profile-filter", "value"),
        State("market-class-filter", "value"),
        State("interval-filter", "value"),
        State("horizon-filter", "value"),
        State("direction-filter", "value"),
        State("duration-filter", "value"),
        State("outcome-filter", "value"),
        State("episode-search", "value"),
    )
    def render_episode_detail(
        selected_rows: list[dict[str, Any]] | None,
        _signal: dict[str, Any] | None,
        profile: str | None,
        market_class: str | None,
        interval: str | None,
        horizon: str | None,
        direction: str | None,
        duration: str | None,
        outcome: str | None,
        query: str | None,
    ):
        snapshot = service.get(
            DashboardFilters(
                profile=profile or "all",
                market_class=market_class or "all",
                interval=interval or "month",
                horizon=horizon or "all",
                direction=_normalized_filter(direction, "All directions"),
                duration=duration or "all",
                outcome=_normalized_filter(outcome, "All outcomes"),
                query=query or "",
            )
        )
        selected_id = str(selected_rows[0].get("episode_id")) if selected_rows else ""
        episode = next(
            (item for item in snapshot.episodes if item.episode_id == selected_id),
            snapshot.episodes[0] if snapshot.episodes else None,
        )
        return episode_detail(episode)


def _normalized_filter(value: str | None, all_label: str) -> str:
    if not value or value == all_label:
        return "all"
    return value.lower()


def _state_banner(
    status: str,
    message: str,
    signal: dict[str, Any] | None,
) -> tuple[object, str]:
    signal_status = str((signal or {}).get("status") or "")
    signal_message = str((signal or {}).get("message") or "")
    if signal_status == "error":
        return (
            dmc.Alert(
                signal_message
                or "The latest refresh is unavailable; showing the last complete snapshot.",
                title="Refresh unavailable",
                color="yellow",
                variant="light",
            ),
            "data-state-banner",
        )
    if status == "error":
        return (
            dmc.Alert(
                message or "Analytics data is unavailable. The dashboard will retry automatically.",
                title="Data unavailable",
                color="red",
                variant="light",
            ),
            "data-state-banner",
        )
    if status == "empty":
        return (
            dmc.Alert(
                message or "No analytics data is available for the current selection.",
                title="No matching data",
                color="blue",
                variant="light",
            ),
            "data-state-banner",
        )
    return "", "data-state-banner is-hidden"
