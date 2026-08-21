from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from perp_trade_history.analytics.schema import (
    AnalyticsCapabilities,
    EpisodeRow,
    FilterDimensions,
    NotableChange,
)
from perp_trade_history.dashboard.models import DashboardFilters, PerformancePoint
from perp_trade_history.dashboard.providers import (
    AnalyticsSnapshotProvider,
    SyntheticSnapshotProvider,
    _apply_horizon,
    _cashflow_performance,
    _matches_analytics_episode,
)
from perp_trade_history.storage import DataStore


def _episode() -> EpisodeRow:
    return EpisodeRow(
        episode_id="episode-generic",
        venue="venue-generic",
        account_id="account-generic",
        market_type="perpetual",
        symbol="instrument-generic",
        position_mode="net",
        direction="long",
        status="closed",
        boundary_status="complete",
        opened_at_ms=1_640_995_200_000,
        closed_at_ms=1_640_998_800_000,
        duration_ms=3_600_000,
        duration_bucket="1h_to_1d",
        open_year=2022,
        open_month="2022-01",
        close_year=2022,
        close_month="2022-01",
        quantity_unit="contract",
        opened_quantity="1",
        closed_quantity="1",
        maximum_quantity="1",
        remaining_quantity="0",
        entry_vwap="10",
        exit_vwap="11",
        execution_count=2,
        order_count=2,
        coverage_status="complete",
        quality_flags=(),
    )


def _analytics_snapshot(*, with_cashflow: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        as_of_ms=1_640_998_800_000,
        episodes=(_episode(),),
        execution_links=(),
        cashflow_attributions=(),
        episode_cashflows=(
            (
                SimpleNamespace(
                    episode_id="episode-generic",
                    reporting_currency="Reporting currency",
                    reporting_amount="5",
                ),
            )
            if with_cashflow
            else ()
        ),
        notable_changes=(
            NotableChange(
                comparison="latest_calendar_periods",
                metric="median_duration",
                prior_period="prior",
                current_period="current",
                prior_value="1",
                current_value="2",
                absolute_delta="1",
                relative_change="1",
                unit="milliseconds",
                direction="increased",
                prior_sample_size=6,
                current_sample_size=6,
                threshold="relative",
                summary="Median duration increased beyond the comparison threshold.",
            ),
        ),
        dimensions=FilterDimensions(
            venues=("venue-generic",),
            accounts=("account-generic",),
            market_types=("perpetual",),
            symbols=("instrument-generic",),
            directions=("long",),
            statuses=("closed",),
            duration_buckets=("1h_to_1d",),
            open_years=(2022,),
            close_years=(2022,),
            currencies=(),
            event_types=(),
        ),
        capabilities=AnalyticsCapabilities(
            episode_reconstruction=True,
            exact_episode_boundaries=True,
            exact_execution_cashflow_attribution=False,
            temporal_cashflow_attribution=False,
            notional_metrics=False,
            complete_source_trade_coverage=True,
            complete_reconstructed_episode_boundaries=True,
        ),
        build_report=SimpleNamespace(gaps=()),
    )


def test_synthetic_provider_is_deterministic_and_filterable() -> None:
    provider = SyntheticSnapshotProvider()
    first = provider.load(DashboardFilters())
    second = provider.load(
        DashboardFilters(
            market_class="perpetual derivatives",
            direction="long",
            query="ambiguous",
        )
    )

    assert first == provider.load(DashboardFilters())
    assert second.episodes
    assert all(
        item.direction == "Long" and "Ambiguous Tie Order" in item.tags for item in second.episodes
    )
    assert any(item.confidence == "Low confidence" for item in first.notable_changes)
    assert len(first.episodes) == 240
    assert first.performance[-1].net == 20_514.0
    assert any(item.period < 0 for item in first.performance)
    assert any(item.net_result is not None and item.net_result < 0 for item in first.years)


def test_analytics_provider_uses_file_identity_and_only_exposes_trade_supported_metrics(
    tmp_path, monkeypatch
) -> None:
    store = DataStore(tmp_path)
    provider = AnalyticsSnapshotProvider(store, reporting_currency="Reporting currency")
    monkeypatch.setattr(
        "perp_trade_history.dashboard.providers.build_snapshot",
        lambda store, conversion=None: _analytics_snapshot(),
    )
    first_revision = provider.revision()
    source = store.tables["executions"].path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("generic-source-revision", encoding="utf-8")
    first_file_revision = provider.revision()
    stat = source.stat()
    replacement = source.with_suffix(".replacement")
    replacement.write_text("generic-source-revision", encoding="utf-8")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, source)
    replacement_revision = provider.revision()

    snapshot = provider.load(DashboardFilters())

    assert provider.revision() != first_revision
    assert replacement_revision != first_file_revision
    assert snapshot.episodes[0].outcome == "Unavailable"
    assert snapshot.episodes[0].instrument == "instrument-generic"
    assert snapshot.episodes[0].status == "Closed"
    assert snapshot.episodes[0].net_result is None
    assert snapshot.heatmap.metric_label == "Activity by weekday & hour (episode count)"
    assert sum(sum(row) for row in snapshot.heatmap.values) == 1
    assert all(item.net_average is None for item in snapshot.exposure)
    assert snapshot.notable_changes[0].confidence == "Low confidence"
    assert not hasattr(snapshot, "unsupported_metrics")


def test_analytics_provider_derives_safe_cashflow_patterns(tmp_path, monkeypatch) -> None:
    store = DataStore(tmp_path)
    provider = AnalyticsSnapshotProvider(store, reporting_currency="Reporting currency")
    monkeypatch.setattr(
        "perp_trade_history.dashboard.providers.build_snapshot",
        lambda store, conversion=None: _analytics_snapshot(with_cashflow=True),
    )

    snapshot = provider.load(DashboardFilters(market_class="perpetual"))

    assert "average comparable cashflow" in snapshot.heatmap.metric_label
    observed = [value for row in snapshot.heatmap.values for value in row if value is not None]
    assert observed == [5.0]
    band = next(item for item in snapshot.exposure if item.label == "1h – 1d")
    assert band.long_average == 5.0
    assert band.net_average == 5.0
    assert snapshot.episodes[0].outcome == "Win"
    assert snapshot.episodes[0].net_result == 5.0


def test_analytics_provider_reuses_canonical_snapshot_across_filters(tmp_path, monkeypatch) -> None:
    store = DataStore(tmp_path)
    provider = AnalyticsSnapshotProvider(store, reporting_currency="Reporting currency")
    builds = 0

    def counted_snapshot(store, conversion=None):
        nonlocal builds
        builds += 1
        return _analytics_snapshot(with_cashflow=True)

    monkeypatch.setattr(
        "perp_trade_history.dashboard.providers.build_snapshot",
        counted_snapshot,
    )

    provider.load(DashboardFilters())
    provider.load(DashboardFilters(direction="long"))

    assert builds == 1

    source = store.tables["executions"].path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("new canonical revision", encoding="utf-8")
    provider.load(DashboardFilters(direction="short"))

    assert builds == 2


def test_trade_filters_apply_exact_duration_local_time_and_open_status() -> None:
    episode = replace(
        _episode(),
        status="open",
        closed_at_ms=None,
        boundary_status="right_censored",
        duration_bucket="under_1h",
    )
    zone = ZoneInfo("America/New_York")

    assert _matches_analytics_episode(
        episode,
        DashboardFilters(
            duration="under_1h",
            outcome="open",
            start_date="2021-12-31",
            end_date="2021-12-31",
            weekday=4,
            hour_bucket=18,
        ),
        {episode.episode_id: Decimal("5")},
        zone,
    )
    assert not _matches_analytics_episode(
        episode,
        DashboardFilters(hour_bucket=20),
        {episode.episode_id: Decimal("5")},
        zone,
    )


def test_performance_aggregation_rebuilds_period_rolling_and_drawdown_series() -> None:
    first = _episode()
    second = replace(
        first,
        episode_id="episode-later",
        opened_at_ms=1_643_673_600_000,
        closed_at_ms=1_643_677_200_000,
        open_year=2022,
        open_month="2022-02",
        close_year=2022,
        close_month="2022-02",
    )

    points = _cashflow_performance(
        (first, second),
        {first.episode_id: Decimal("10"), second.episode_id: Decimal("-14")},
        "month",
        ZoneInfo("UTC"),
    )

    assert [point.period for point in points] == [10.0, -14.0]
    assert [point.net for point in points] == [10.0, -4.0]
    assert [point.rolling for point in points] == [10.0, -4.0]
    assert [point.drawdown for point in points] == [0.0, -14.0]


def test_filtered_performance_horizon_starts_at_zero_and_excludes_prior_periods() -> None:
    points = tuple(
        PerformancePoint(
            f"2025-{month:02d}-01T00:00:00",
            float(100 + month),
            100.0 if month == 1 else 1.0,
            102.0,
            0.0,
        )
        for month in range(1, 13)
    ) + (
        PerformancePoint(
            "2026-01-01T00:00:00",
            112.0,
            1.0,
            3.0,
            0.0,
        ),
    )

    visible = _apply_horizon(points, "1y", "month")

    assert visible[0] == PerformancePoint(
        "2025-01-01T00:00:00",
        0.0,
        0.0,
        0.0,
        0.0,
    )
    assert [point.net for point in visible[:3]] == [0.0, 1.0, 2.0]
    assert [point.rolling for point in visible[:4]] == [0.0, 1.0, 2.0, 3.0]
    assert visible[-1].net == 12.0

    sparse = _apply_horizon(
        (
            PerformancePoint("2024-01-01T00:00:00", 5.0, 5.0, 5.0, 0.0),
            PerformancePoint("2025-01-01T00:00:00", 12.0, 7.0, 12.0, 0.0),
            PerformancePoint("2026-01-01T00:00:00", 9.0, -3.0, 9.0, -3.0),
        ),
        "1y",
        "month",
    )

    assert [point.timestamp for point in sparse] == [
        "2025-01-01T00:00:00",
        "2026-01-01T00:00:00",
    ]
    assert [point.net for point in sparse] == [0.0, -3.0]
    assert [point.rolling for point in sparse] == [0.0, -3.0]
