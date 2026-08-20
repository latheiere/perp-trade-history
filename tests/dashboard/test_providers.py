from __future__ import annotations

import os
from types import SimpleNamespace

from perp_trade_history.analytics.schema import (
    AnalyticsCapabilities,
    EpisodeRow,
    FilterDimensions,
    NotableChange,
)
from perp_trade_history.dashboard.models import DashboardFilters
from perp_trade_history.dashboard.providers import (
    AnalyticsSnapshotProvider,
    SyntheticSnapshotProvider,
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
            capital_return_metrics=False,
            mark_to_market_metrics=False,
            complete_coverage=True,
        ),
    )


def test_synthetic_provider_is_deterministic_and_filterable() -> None:
    provider = SyntheticSnapshotProvider()
    first = provider.load(DashboardFilters())
    second = provider.load(
        DashboardFilters(
            market_class="perpetual derivatives",
            direction="long",
            query="trend",
        )
    )

    assert first == provider.load(DashboardFilters())
    assert second.episodes
    assert all(item.direction == "Long" and "Trend" in item.tags for item in second.episodes)
    assert any(item.confidence == "Low confidence" for item in first.notable_changes)


def test_analytics_provider_uses_file_identity_and_never_fabricates_unsupported_metrics(
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
    assert snapshot.episodes[0].r_multiple is None
    assert snapshot.episodes[0].mae is None
    assert snapshot.episodes[0].mfe is None
    assert snapshot.episodes[0].outcome == "Unavailable"
    assert snapshot.heatmap.metric_label == "Activity by weekday & hour (episode count)"
    assert sum(sum(row) for row in snapshot.heatmap.values) == 1
    assert all(item.net_average is None for item in snapshot.exposure)
    assert snapshot.notable_changes[0].confidence == "Low confidence"
    unavailable = {item.label for item in snapshot.unsupported_metrics}
    assert {"R multiple", "MAE and MFE", "Capital-return performance"} <= unavailable


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
