from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol

from perp_trade_history.analytics import AnalyticsSnapshot, build_snapshot
from perp_trade_history.analytics.schema import EpisodeRow
from perp_trade_history.dashboard.models import (
    AttributionItem,
    BuildSummary,
    CoverageSummary,
    DashboardFilters,
    DashboardSnapshot,
    Episode,
    ExposureBand,
    FilterOption,
    HeatmapData,
    Kpi,
    NotableChange,
    PerformancePoint,
    QualityIndicator,
    Regime,
    UnsupportedMetric,
    YearSummary,
)
from perp_trade_history.storage import DataStore


class SnapshotProvider(Protocol):
    """Supply a cheap revision signal and a filtered immutable dashboard snapshot."""

    def revision(self) -> str: ...

    def load(self, filters: DashboardFilters) -> DashboardSnapshot: ...


class AnalyticsSnapshotProvider:
    """Adapt canonical read-only storage analytics for the dashboard."""

    def __init__(
        self,
        store: DataStore,
        *,
        conversion: object | None = None,
        reporting_currency: str = "Reporting currency",
    ) -> None:
        self._store = store
        self._conversion = conversion
        self._reporting_currency = reporting_currency

    def revision(self) -> str:
        identities: list[tuple[str, int, int, int]] = []
        for name, table in sorted(self._store.tables.items()):
            try:
                stat = table.path.stat()
            except FileNotFoundError:
                identities.append((name, 0, 0, 0))
            else:
                identities.append((name, stat.st_ino, stat.st_mtime_ns, stat.st_size))
        return sha256(repr(identities).encode()).hexdigest()[:20]

    def load(self, filters: DashboardFilters) -> DashboardSnapshot:
        analytics = build_snapshot(
            self._store,
            conversion=self._conversion,  # type: ignore[arg-type]
        )
        return _adapt_analytics(
            analytics,
            filters,
            revision=self.revision(),
            reporting_currency=self._reporting_currency,
        )


def _adapt_analytics(
    analytics: AnalyticsSnapshot,
    filters: DashboardFilters,
    *,
    revision: str,
    reporting_currency: str,
) -> DashboardSnapshot:
    amounts = _episode_amounts(analytics, reporting_currency)
    selected = tuple(
        sorted(
            (
                row
                for row in analytics.episodes
                if _matches_analytics_episode(row, filters, amounts)
            ),
            key=lambda row: (row.opened_at_ms, row.episode_id),
            reverse=True,
        )
    )
    closed = tuple(row for row in selected if row.status == "closed")
    complete = sum(row.coverage_status == "complete" for row in selected)
    date_range = _analytics_date_range(selected)
    profiles = (
        FilterOption("All profiles", "all"),
        *(
            FilterOption(value.replace("_", " ").title(), value.lower())
            for value in analytics.dimensions.venues
        ),
    )
    market_classes = (
        FilterOption("All market classes", "all"),
        *(
            FilterOption(value.replace("_", " ").title(), value.lower())
            for value in analytics.dimensions.market_types
        ),
    )
    performance = _cashflow_performance(closed, amounts)
    years = _analytics_years(closed, amounts)
    unfiltered = filters == DashboardFilters(interval=filters.interval, horizon=filters.horizon)
    notable = (
        tuple(
            _analytics_notable(index, item)
            for index, item in enumerate(analytics.notable_changes[:3], 1)
        )
        if unfiltered
        else ()
    )
    coverage_percent = (complete / len(selected) * 100) if selected else 0.0
    refreshed = _format_timestamp(analytics.as_of_ms, include_time=True)
    snapshot = DashboardSnapshot(
        revision=revision,
        refreshed_label=f"Source data through {refreshed}",
        timezone="UTC",
        date_range=date_range,
        currency=reporting_currency,
        status="ready" if selected else "empty",
        message="" if selected else "No reconstructed episodes match the current selection.",
        profiles=profiles,
        market_classes=market_classes,
        kpis=_analytics_kpis(selected, closed, complete, amounts, reporting_currency),
        performance=_sample_performance(
            _apply_horizon(performance, filters.horizon), filters.interval
        ),
        notable_changes=notable,
        heatmap=_analytics_heatmap(selected, amounts, reporting_currency),
        exposure=_analytics_exposure(selected, amounts),
        exposure_metric_label=f"Avg {reporting_currency}",
        exposure_net_label=f"Avg {reporting_currency}",
        years=years,
        episodes=tuple(_analytics_episode(row, amounts.get(row.episode_id)) for row in selected),
        coverage=CoverageSummary(
            coverage_percent,
            complete,
            len(selected),
            date_range,
        ),
        unsupported_metrics=_unsupported_metrics(analytics),
        build=BuildSummary(
            timestamp=refreshed,
            build_id=revision[:12],
            source_data=refreshed,
            next_build="Automatic on source change",
            status="Ready",
        ),
    )
    return snapshot


def _episode_amounts(analytics: AnalyticsSnapshot, reporting_currency: str) -> dict[str, Decimal]:
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    currencies: dict[str, set[str]] = defaultdict(set)
    for item in analytics.episode_cashflows:
        currencies[item.episode_id].add(item.reporting_currency)
        if item.reporting_currency == reporting_currency:
            amounts[item.episode_id] += _decimal(item.reporting_amount)
    return {
        episode_id: amount
        for episode_id, amount in amounts.items()
        if currencies[episode_id] == {reporting_currency}
    }


def _matches_analytics_episode(
    row: EpisodeRow,
    filters: DashboardFilters,
    amounts: dict[str, Decimal],
) -> bool:
    if filters.profile != "all" and row.venue.lower() != filters.profile:
        return False
    if filters.market_class != "all" and row.market_type.lower() != filters.market_class:
        return False
    if filters.direction != "all" and row.direction.lower() != filters.direction:
        return False
    mapped_duration = {
        "short": {"under_1h"},
        "medium": {"1h_to_1d"},
        "long": {"1d_to_7d", "7d_to_30d", "30d_or_more"},
    }
    if filters.duration != "all" and row.duration_bucket not in mapped_duration[filters.duration]:
        return False
    amount = amounts.get(row.episode_id)
    outcome = "unavailable" if amount is None else "win" if amount > 0 else "loss"
    if filters.outcome != "all" and outcome != filters.outcome:
        return False
    query = filters.query.strip().lower()
    searchable = " ".join(
        (row.episode_id, row.market_type, row.direction, row.status, *row.quality_flags)
    ).lower()
    return not query or query in searchable


def _analytics_kpis(
    selected: tuple[EpisodeRow, ...],
    closed: tuple[EpisodeRow, ...],
    complete: int,
    amounts: dict[str, Decimal],
    reporting_currency: str,
) -> tuple[Kpi, ...]:
    attributed = [amounts[row.episode_id] for row in closed if row.episode_id in amounts]
    total = sum(attributed, Decimal(0))
    durations = sorted(row.duration_ms for row in closed)
    median_duration = durations[len(durations) // 2] if durations else None
    boundary_rate = (complete / len(selected) * 100) if selected else 0.0
    return (
        Kpi(
            "Attributed cashflow",
            _currency(total, reporting_currency) if attributed else "Unavailable",
            f"{len(attributed):,} episodes",
            "Comparable reporting amounts",
            "positive" if total >= 0 and attributed else "neutral",
        ),
        Kpi("Closed episodes", f"{len(closed):,}", "Observed", "Reconstructed", "info"),
        Kpi(
            "Complete boundaries",
            f"{boundary_rate:.1f}%",
            f"{complete:,} episodes",
            "Coverage status",
            "positive" if boundary_rate == 100 else "info",
        ),
        Kpi(
            "Median duration",
            _duration(median_duration) if median_duration is not None else "Unavailable",
            "Closed episodes",
            "Observed holding time",
            "info",
        ),
    )


def _cashflow_performance(
    closed: tuple[EpisodeRow, ...], amounts: dict[str, Decimal]
) -> tuple[PerformancePoint, ...]:
    monthly: dict[str, Decimal] = defaultdict(Decimal)
    for row in closed:
        if row.closed_at_ms is not None and row.episode_id in amounts:
            monthly[_format_timestamp(row.closed_at_ms, month=True)] += amounts[row.episode_id]
    running = Decimal(0)
    points: list[PerformancePoint] = []
    for timestamp, amount in sorted(monthly.items()):
        running += amount
        points.append(PerformancePoint(timestamp, float(running), None, None))
    return tuple(points)


def _analytics_notable(index: int, item: object) -> NotableChange:
    prior = int(item.prior_sample_size)
    current = int(item.current_sample_size)
    minimum = min(prior, current)
    confidence = (
        "High confidence"
        if minimum >= 30
        else "Medium confidence"
        if minimum >= 10
        else "Low confidence"
    )
    direction = str(item.direction)
    tone = (
        "positive"
        if direction == "increased"
        else "negative"
        if direction == "decreased"
        else "neutral"
    )
    title = str(item.metric).replace("_", " ").title()
    return NotableChange(
        index,
        title,
        str(item.summary),
        current,
        confidence,
        tone,
    )


def _analytics_heatmap(
    rows: tuple[EpisodeRow, ...],
    amounts: dict[str, Decimal],
    reporting_currency: str,
) -> HeatmapData:
    if not rows:
        return HeatmapData(hours=(), weekdays=(), values=())
    hours = tuple(f"{hour:02d}" for hour in range(0, 24, 2))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    comparable = any(row.episode_id in amounts for row in rows)
    cells: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        opened = datetime.fromtimestamp(row.opened_at_ms / 1000, tz=UTC)
        hour_index = opened.hour // 2
        if comparable:
            if row.episode_id in amounts:
                cells[(opened.weekday(), hour_index)].append(float(amounts[row.episode_id]))
        else:
            cells[(opened.weekday(), hour_index)].append(1.0)
    values = tuple(
        tuple(
            (
                _average(cells.get((weekday_index, hour_index), []))
                if comparable
                else float(len(cells.get((weekday_index, hour_index), [])))
            )
            for hour_index in range(len(hours))
        )
        for weekday_index in range(len(weekdays))
    )
    if comparable:
        return HeatmapData(
            hours,
            weekdays,
            values,
            f"Performance by weekday & hour (average comparable cashflow, {reporting_currency})",
            reporting_currency,
            True,
        )
    return HeatmapData(
        hours,
        weekdays,
        values,
        "Activity by weekday & hour (episode count)",
        "episodes",
        False,
    )


def _analytics_exposure(
    rows: tuple[EpisodeRow, ...], amounts: dict[str, Decimal]
) -> tuple[ExposureBand, ...]:
    labels = (
        ("Under 1h", "under_1h"),
        ("1h – 1d", "1h_to_1d"),
        ("1d – 7d", "1d_to_7d"),
        ("7d – 30d", "7d_to_30d"),
        ("30d+", "30d_or_more"),
    )
    return tuple(_exposure_band(label, bucket, rows, amounts) for label, bucket in labels)


def _exposure_band(
    label: str,
    bucket: str,
    rows: tuple[EpisodeRow, ...],
    amounts: dict[str, Decimal],
) -> ExposureBand:
    matching = [row for row in rows if row.duration_bucket == bucket]
    long_rows = [row for row in matching if row.direction.lower() == "long"]
    short_rows = [row for row in matching if row.direction.lower() == "short"]
    long_values = [float(amounts[row.episode_id]) for row in long_rows if row.episode_id in amounts]
    short_values = [
        float(amounts[row.episode_id]) for row in short_rows if row.episode_id in amounts
    ]
    net_values = [float(amounts[row.episode_id]) for row in matching if row.episode_id in amounts]
    return ExposureBand(
        label,
        len(long_rows),
        _average(long_values),
        len(short_rows),
        _average(short_values),
        _average(net_values),
    )


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _analytics_years(
    rows: tuple[EpisodeRow, ...], amounts: dict[str, Decimal]
) -> tuple[YearSummary, ...]:
    grouped: dict[int, list[EpisodeRow]] = defaultdict(list)
    for row in rows:
        if row.close_year is not None:
            grouped[row.close_year].append(row)
    cards: list[YearSummary] = []
    for year, episodes in sorted(grouped.items()):
        comparable = [amounts[row.episode_id] for row in episodes if row.episode_id in amounts]
        total = sum(comparable, Decimal(0)) if comparable else None
        win_rate = (
            sum(value > 0 for value in comparable) / len(comparable) * 100 if comparable else None
        )
        average_ms = sum(row.duration_ms for row in episodes) // len(episodes)
        cards.append(
            YearSummary(
                str(year),
                len(episodes),
                float(total) if total is not None else None,
                None,
                win_rate,
                (),
                _duration(average_ms),
                "Net cashflow",
            )
        )
    return tuple(cards)


def _analytics_episode(row: EpisodeRow, amount: Decimal | None) -> Episode:
    occurred = _format_timestamp(row.opened_at_ms, include_time=True)
    outcome = "Unavailable" if amount is None else "Win" if amount > 0 else "Loss"
    return Episode(
        episode_id=row.episode_id,
        occurred_at=occurred,
        profile=row.venue.replace("_", " ").title(),
        market_class=row.market_type.replace("_", " ").title(),
        direction=row.direction.title(),
        duration=_duration(row.duration_ms),
        duration_bucket=(
            "short"
            if row.duration_bucket == "under_1h"
            else "medium"
            if row.duration_bucket == "1h_to_1d"
            else "long"
        ),
        entry=_display_decimal(row.entry_vwap),
        exit=_display_decimal(row.exit_vwap),
        r_multiple=None,
        outcome=outcome,
        mae=None,
        mfe=None,
        tags=tuple(flag.replace("_", " ").title() for flag in row.quality_flags) or ("No flags",),
        timeline_labels=(),
        timeline_values=(),
        attribution=(),
        quality=(
            QualityIndicator("Boundary status", row.boundary_status.title()),
            QualityIndicator("Coverage status", row.coverage_status.title()),
            QualityIndicator("Execution count", str(row.execution_count), "info"),
            QualityIndicator("Order count", str(row.order_count), "info"),
        ),
        confidence="High" if row.boundary_status == "complete" else "Low",
        samples=row.execution_count,
    )


def _analytics_date_range(rows: tuple[EpisodeRow, ...]) -> str:
    if not rows:
        return "No available range"
    start = min(row.opened_at_ms for row in rows)
    end = max(row.closed_at_ms or row.opened_at_ms for row in rows)
    return f"{_format_timestamp(start)} – {_format_timestamp(end)}"


def _format_timestamp(value: int, *, include_time: bool = False, month: bool = False) -> str:
    parsed = datetime.fromtimestamp(value / 1000, tz=UTC)
    if month:
        return parsed.strftime("%Y-%m-01T00:00:00Z")
    return parsed.strftime("%b %d, %Y %H:%M UTC" if include_time else "%b %d, %Y")


def _duration(value: int) -> str:
    minutes = value // 60_000
    days, remainder = divmod(minutes, 24 * 60)
    hours, remaining_minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {remaining_minutes}m"
    return f"{remaining_minutes}m"


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(0)


def _currency(value: Decimal, currency: str) -> str:
    return f"{value:+,.2f} {currency}"


def _display_decimal(value: str) -> str:
    if not value:
        return "Unavailable"
    parsed = _decimal(value)
    decimals = 8 if abs(parsed) >= 1 else 12
    rendered = f"{parsed:,.{decimals}f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _unsupported_metrics(analytics: AnalyticsSnapshot) -> tuple[UnsupportedMetric, ...]:
    capabilities = analytics.capabilities
    labels = ["R multiple", "MAE and MFE", "Benchmark comparison"]
    if not capabilities.capital_return_metrics:
        labels.append("Capital-return performance")
    if not capabilities.mark_to_market_metrics:
        labels.append("Mark-to-market excursion")
    if not capabilities.notional_metrics:
        labels.append("Notional exposure")
    return tuple(UnsupportedMetric(label) for label in labels)


class SyntheticSnapshotProvider:
    """Deterministic local provider used by tests and visual development."""

    def __init__(self, *, revision: str = "synthetic-v1") -> None:
        self._revision = revision

    def revision(self) -> str:
        return self._revision

    def load(self, filters: DashboardFilters) -> DashboardSnapshot:
        snapshot = _synthetic_snapshot(self._revision)
        episodes = tuple(
            episode for episode in snapshot.episodes if _matches_episode(episode, filters)
        )
        performance = _apply_horizon(snapshot.performance, filters.horizon)
        return DashboardSnapshot(
            revision=snapshot.revision,
            refreshed_label=snapshot.refreshed_label,
            timezone=snapshot.timezone,
            date_range=snapshot.date_range,
            currency=snapshot.currency,
            status=snapshot.status,
            message=snapshot.message,
            profiles=snapshot.profiles,
            market_classes=snapshot.market_classes,
            kpis=snapshot.kpis,
            performance=_sample_performance(performance, filters.interval),
            regimes=snapshot.regimes,
            notable_changes=snapshot.notable_changes,
            heatmap=snapshot.heatmap,
            exposure=snapshot.exposure,
            exposure_metric_label=snapshot.exposure_metric_label,
            exposure_net_label=snapshot.exposure_net_label,
            years=snapshot.years,
            episodes=episodes,
            coverage=snapshot.coverage,
            unsupported_metrics=snapshot.unsupported_metrics,
            build=snapshot.build,
        )


def _matches_episode(episode: Episode, filters: DashboardFilters) -> bool:
    if filters.profile != "all" and episode.profile.lower() != filters.profile:
        return False
    if filters.market_class != "all" and episode.market_class.lower() != filters.market_class:
        return False
    if filters.direction != "all" and episode.direction.lower() != filters.direction:
        return False
    if filters.duration != "all" and episode.duration_bucket != filters.duration:
        return False
    if filters.outcome != "all" and episode.outcome.lower() != filters.outcome:
        return False
    query = filters.query.strip().lower()
    searchable = " ".join(
        (episode.episode_id, episode.profile, episode.direction, *episode.tags)
    ).lower()
    return not query or query in searchable


def _apply_horizon(
    points: tuple[PerformancePoint, ...], horizon: str
) -> tuple[PerformancePoint, ...]:
    months = {"1y": 12, "2y": 24, "3y": 36}.get(horizon)
    return points[-months:] if months else points


def _sample_performance(
    points: tuple[PerformancePoint, ...], interval: str
) -> tuple[PerformancePoint, ...]:
    step = {"month": 1, "quarter": 3, "year": 12}.get(interval, 1)
    if step == 1 or len(points) < 2:
        return points
    sampled = list(points[::step])
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return tuple(sampled)


def _synthetic_snapshot(revision: str) -> DashboardSnapshot:
    performance = tuple(
        PerformancePoint(
            timestamp=f"{year:04d}-{month:02d}-01T00:00:00Z",
            net=float(index * 3850 + ((index % 7) - 3) * 2100),
            benchmark=float(index * 2450 + ((index % 9) - 4) * 1200),
            drawdown=float(-4 - (index % 11) * 1.35 - (9 if 13 <= index <= 18 else 0)),
        )
        for index, (year, month) in enumerate(
            (month_index // 12, month_index % 12 + 1)
            for month_index in range(2021 * 12, 2026 * 12 + 8)
        )
    )
    heat_values = tuple(
        tuple(round((((row * 5 + column * 3) % 17) - 8) / 8, 2) for column in range(12))
        for row in range(7)
    )
    episodes = tuple(_episode(index) for index in range(1, 19))
    years = (
        YearSummary("2021", 212, 28743, 18.6, 87.3, (1, 2, 4, 6, 8, 5, 3, 7, 11, 8, 4), "2h 18m"),
        YearSummary("2022", 314, -18952, -11.3, 78.1, (2, 4, 8, 10, 7, 5, 3, 4, 6, 3, 1), "1h 47m"),
        YearSummary("2023", 326, 36128, 20.4, 85.4, (1, 2, 3, 5, 7, 8, 11, 8, 5, 3, 2), "2h 06m"),
        YearSummary("2024", 342, 54321, 26.1, 91.2, (1, 2, 3, 4, 7, 10, 12, 9, 5, 3, 2), "2h 32m"),
        YearSummary("2025", 336, 59874, 25.7, 93.6, (1, 1, 2, 4, 6, 9, 13, 10, 7, 4, 2), "2h 41m"),
        YearSummary(
            "2026 YTD", 312, 18628, 12.7, 94.9, (0, 1, 2, 3, 5, 8, 14, 9, 6, 3, 1), "2h 55m"
        ),
    )
    return DashboardSnapshot(
        revision=revision,
        refreshed_label="Refreshed 2m ago",
        timezone="America/New_York",
        date_range="Jan 1, 2021 – Aug 21, 2026",
        currency="Reporting currency",
        profiles=(
            FilterOption("All profiles", "all"),
            FilterOption("Discretionary", "discretionary"),
            FilterOption("Systematic", "systematic"),
        ),
        market_classes=(
            FilterOption("All market classes", "all"),
            FilterOption("Perpetual derivatives", "perpetual derivatives"),
        ),
        kpis=(
            Kpi("Net result", "+$268,742.31", "+68.74%", "Total return", "positive"),
            Kpi("CAGR", "17.32%", "vs. 8.64%", "Benchmark", "info"),
            Kpi("Closed episodes", "1,842", "92.3%", "Win rate", "info"),
            Kpi("Max drawdown", "-21.83%", "Mar 10 – Apr 6", "Peak decline", "negative"),
        ),
        performance=performance,
        regimes=(
            Regime("Rising market", "2021-01-01", "2021-11-30", "positive"),
            Regime("Risk-off", "2021-12-01", "2022-06-30", "negative"),
            Regime("Recovery", "2022-07-01", "2023-12-31", "positive"),
            Regime("Expansion", "2024-01-01", "2025-07-31", "positive"),
            Regime("Normalization", "2025-08-01", "2026-08-31", "info"),
        ),
        notable_changes=(
            NotableChange(
                1,
                "Drawdown profile improved",
                "Peak decline narrowed across comparable expansion windows.",
                612,
                "High confidence",
                "positive",
            ),
            NotableChange(
                2,
                "Win rate increased",
                "Outcome consistency improved while sample depth remained broad.",
                1104,
                "High confidence",
                "positive",
            ),
            NotableChange(
                3,
                "Overnight exposure increased",
                "Long-horizon exposure grew across the latest evaluation window.",
                453,
                "Low confidence",
                "negative",
            ),
        ),
        heatmap=HeatmapData(
            hours=tuple(f"{hour:02d}" for hour in range(0, 24, 2)),
            weekdays=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
            values=heat_values,
        ),
        exposure=tuple(
            ExposureBand(label, long_count, long_average, short_count, short_average, net)
            for label, long_count, long_average, short_count, short_average, net in (
                ("0 – 15m", 326, 0.18, 298, -0.03, 0.15),
                ("15m – 1h", 412, 0.32, 389, 0.06, 0.26),
                ("1h – 4h", 501, 0.46, 472, 0.15, 0.31),
                ("4h – 1d", 304, 0.41, 276, 0.08, 0.32),
                ("1d – 3d", 176, 0.63, 148, 0.12, 0.51),
                ("3d+", 123, 0.74, 94, 0.18, 0.56),
            )
        ),
        years=years,
        episodes=episodes,
        coverage=CoverageSummary(98.7, 1816, 1842, "Jan 1, 2021 – Aug 21, 2026"),
        unsupported_metrics=(
            UnsupportedMetric("Slippage", 4.3),
            UnsupportedMetric("Commission detail", 2.1),
            UnsupportedMetric("Latency", 1.8),
            UnsupportedMetric("Market depth", 8.6),
        ),
        build=BuildSummary(
            timestamp="Aug 21, 2026 08:14",
            build_id="2026.08.21.0814",
            source_data="Aug 21, 2026",
            next_build="Aug 22, 2026 06:00",
            status="Success",
        ),
    )


def _episode(index: int) -> Episode:
    positive = index % 3 != 2
    direction = "Long" if index % 2 else "Short"
    profile = "Discretionary" if index % 3 else "Systematic"
    r_multiple = round((0.17 + (index % 7) * 0.29) * (1 if positive else -1), 2)
    hours = (index * 3) % 24
    day = 21 - index // 3
    duration_bucket, duration = (
        ("short", f"{20 + index * 3}m")
        if index % 4 == 1
        else ("medium", f"{1 + index % 5}h {index * 7 % 60}m")
        if index % 4 in {0, 2}
        else ("long", f"1d {index % 8}h")
    )
    timeline = (0.0, -0.18, 0.12, 0.35, 0.62, 0.78, 0.91, r_multiple)
    return Episode(
        episode_id=f"episode-{index:03d}",
        occurred_at=f"Aug {day:02d}, 2026 {hours:02d}:{(index * 11) % 60:02d}",
        profile=profile,
        market_class="Perpetual derivatives",
        direction=direction,
        duration=duration,
        duration_bucket=duration_bucket,
        entry="—",
        exit="—",
        r_multiple=r_multiple,
        outcome="Win" if positive else "Loss",
        mae=round(-0.04 - (index % 5) * 0.07, 2),
        mfe=round(abs(r_multiple) + 0.38 + (index % 4) * 0.17, 2),
        tags=("Breakout", "Trend") if index % 2 else ("Mean reversion",),
        timeline_labels=("09:42", "10:00", "10:30", "11:00", "11:30", "12:00", "12:15", "12:18"),
        timeline_values=timeline,
        attribution=(
            AttributionItem("Entry edge", round(abs(r_multiple) * 0.58, 2)),
            AttributionItem("Trade management", round(abs(r_multiple) * 0.34, 2)),
            AttributionItem("Costs", -0.16),
        ),
        quality=(
            QualityIndicator("Setup quality", "High"),
            QualityIndicator("Execution quality", "High"),
            QualityIndicator("Process adherence", "94%"),
            QualityIndicator("Data completeness", "Complete"),
        ),
        confidence="High" if index % 4 else "Medium",
        samples=380 + index * 4,
    )
