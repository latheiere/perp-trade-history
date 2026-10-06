from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from perp_trade_history.analytics import AnalyticsSnapshot, build_snapshot_from_tables
from perp_trade_history.analytics.schema import CoverageGap as AnalyticsCoverageGap
from perp_trade_history.analytics.schema import EpisodeRow
from perp_trade_history.conversion import StoredCashflowConversion
from perp_trade_history.dashboard.demo import demonstration_tables
from perp_trade_history.dashboard.models import (
    BuildSummary,
    CashflowDetail,
    CoverageGap,
    CoverageSummary,
    DashboardFilters,
    DashboardSnapshot,
    Episode,
    ExecutionDetail,
    ExposureBand,
    FilterOption,
    HeatmapData,
    Kpi,
    NotableChange,
    PerformancePoint,
    QualityIndicator,
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
        self._cache_lock = RLock()
        self._cached_revision = ""
        self._cached_analytics: AnalyticsSnapshot | None = None
        self._cached_execution_rows: list[dict[str, str]] = []
        self._cached_cashflow_rows: list[dict[str, str]] = []

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
        revision = self.revision()
        with self._cache_lock:
            if self._cached_analytics is None or revision != self._cached_revision:
                tables = {name: table.read() for name, table in self._store.tables.items()}
                analytics = build_snapshot_from_tables(
                    tables,
                    conversion=self._conversion,  # type: ignore[arg-type]
                )
                execution_rows = tables["executions"]
                cashflow_rows = tables["cashflows"]
                self._cached_revision = revision
                self._cached_analytics = analytics
                self._cached_execution_rows = execution_rows
                self._cached_cashflow_rows = cashflow_rows
            analytics = self._cached_analytics
            execution_rows = self._cached_execution_rows
            cashflow_rows = self._cached_cashflow_rows
        assert analytics is not None
        return _adapt_analytics(
            analytics,
            filters,
            revision=revision,
            reporting_currency=self._reporting_currency,
            execution_rows=execution_rows,
            cashflow_rows=cashflow_rows,
        )


    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cached_revision = ""
            self._cached_analytics = None
            self._cached_execution_rows = []
            self._cached_cashflow_rows = []
            if isinstance(self._conversion, StoredCashflowConversion):
                self._conversion.clear()


def _adapt_analytics(
    analytics: AnalyticsSnapshot,
    filters: DashboardFilters,
    *,
    revision: str,
    reporting_currency: str,
    execution_rows: list[dict[str, str]] | None = None,
    cashflow_rows: list[dict[str, str]] | None = None,
) -> DashboardSnapshot:
    zone = _timezone(filters.timezone)
    amounts = _episode_amounts(analytics, reporting_currency)
    selected = tuple(
        sorted(
            (
                row
                for row in analytics.episodes
                if _matches_analytics_episode(row, filters, amounts, zone)
            ),
            key=lambda row: (row.opened_at_ms, row.episode_id),
            reverse=True,
        )
    )
    closed = tuple(row for row in selected if row.status == "closed")
    complete = sum(row.coverage_status == "complete" for row in selected)
    boundary_complete = sum(row.boundary_status == "complete" for row in selected)
    open_episodes = sum(row.status == "open" for row in selected)
    left_censored = sum(
        row.boundary_status in {"left_censored", "both_censored"} for row in selected
    )
    date_range = _analytics_date_range(selected, zone)
    date_start, date_end = _analytics_date_bounds(analytics.episodes, zone)
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
    performance = _cashflow_performance(closed, amounts, filters.interval, zone)
    years = _analytics_years(closed, amounts, zone)
    unfiltered = _is_baseline_filter(filters, date_start, date_end)
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
        timezone=zone.key,
        date_range=date_range,
        currency=reporting_currency,
        status="ready" if selected else "empty",
        message="" if selected else "No reconstructed episodes match the current selection.",
        profiles=profiles,
        market_classes=market_classes,
        timezone_options=_timezone_options(zone.key),
        date_start=date_start,
        date_end=date_end,
        kpis=_analytics_kpis(selected, closed, amounts, reporting_currency),
        performance=_apply_horizon(performance, filters.horizon, filters.interval),
        notable_changes=notable,
        heatmap=_analytics_heatmap(selected, amounts, reporting_currency, zone),
        exposure=_analytics_exposure(selected, amounts),
        exposure_metric_label=f"Avg {reporting_currency}",
        exposure_net_label=f"Avg {reporting_currency}",
        years=years,
        episodes=_analytics_episodes(
            selected,
            amounts,
            analytics,
            execution_rows or [],
            cashflow_rows or [],
            reporting_currency,
            zone,
        ),
        coverage=CoverageSummary(
            coverage_percent,
            complete,
            len(selected),
            date_range,
            boundary_complete,
            open_episodes,
            left_censored,
        ),
        coverage_gaps=_dashboard_coverage_gaps(analytics, filters, zone),
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
    zone: ZoneInfo,
) -> bool:
    if filters.profile != "all" and row.venue.lower() != filters.profile:
        return False
    if filters.market_class != "all" and row.market_type.lower() != filters.market_class:
        return False
    if filters.direction != "all" and row.direction.lower() != filters.direction:
        return False
    if filters.duration != "all" and row.duration_bucket != filters.duration:
        return False
    opened = datetime.fromtimestamp(row.opened_at_ms / 1000, tz=UTC).astimezone(zone)
    if filters.start_date and opened.date() < _parse_date(filters.start_date):
        return False
    if filters.end_date and opened.date() > _parse_date(filters.end_date):
        return False
    if filters.weekday is not None and opened.weekday() != filters.weekday:
        return False
    if filters.hour_bucket is not None and (opened.hour // 2) * 2 != filters.hour_bucket:
        return False
    amount = amounts.get(row.episode_id)
    outcome = (
        "open"
        if row.status == "open"
        else "unavailable"
        if amount is None
        else "win"
        if amount > 0
        else "loss"
        if amount < 0
        else "flat"
    )
    if filters.outcome != "all" and outcome != filters.outcome:
        return False
    query = filters.query.strip().lower()
    searchable = " ".join(
        (
            row.episode_id,
            row.symbol,
            row.market_type,
            row.direction,
            row.status,
            *row.quality_flags,
        )
    ).lower()
    return not query or query in searchable


def _analytics_kpis(
    selected: tuple[EpisodeRow, ...],
    closed: tuple[EpisodeRow, ...],
    amounts: dict[str, Decimal],
    reporting_currency: str,
) -> tuple[Kpi, ...]:
    attributed = [amounts[row.episode_id] for row in closed if row.episode_id in amounts]
    total = sum(attributed, Decimal(0))
    durations = sorted(row.duration_ms for row in closed)
    median_duration = durations[len(durations) // 2] if durations else None
    wins = sum(amount > 0 for amount in attributed)
    win_rate = wins / len(attributed) * 100 if attributed else None
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
            "Win rate",
            f"{win_rate:.1f}%" if win_rate is not None else "Unavailable",
            f"{wins:,} wins" if win_rate is not None else "No comparable outcomes",
            f"{len(attributed):,} closed episodes" if attributed else "",
            "positive" if win_rate is not None and win_rate >= 50 else "neutral",
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
    closed: tuple[EpisodeRow, ...],
    amounts: dict[str, Decimal],
    interval: str,
    zone: ZoneInfo,
) -> tuple[PerformancePoint, ...]:
    periods: dict[date, Decimal] = defaultdict(Decimal)
    for row in closed:
        if row.closed_at_ms is not None and row.episode_id in amounts:
            closed_at = datetime.fromtimestamp(row.closed_at_ms / 1000, tz=UTC).astimezone(zone)
            periods[_period_start(closed_at.date(), interval)] += amounts[row.episode_id]
    running = Decimal(0)
    peak = Decimal(0)
    recent: list[Decimal] = []
    points: list[PerformancePoint] = []
    for period_start, amount in sorted(periods.items()):
        running += amount
        peak = max(peak, running)
        recent.append(amount)
        rolling = sum(recent[-3:], Decimal(0))
        points.append(
            PerformancePoint(
                f"{period_start.isoformat()}T00:00:00",
                float(running),
                float(amount),
                float(rolling),
                float(running - peak),
            )
        )
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
    zone: ZoneInfo,
) -> HeatmapData:
    if not rows:
        return HeatmapData(hours=(), weekdays=(), values=())
    hours = tuple(f"{hour:02d}" for hour in range(0, 24, 2))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    comparable = any(row.episode_id in amounts for row in rows)
    cells: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        opened = datetime.fromtimestamp(row.opened_at_ms / 1000, tz=UTC).astimezone(zone)
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
        bucket,
        len(long_rows),
        _average(long_values),
        len(short_rows),
        _average(short_values),
        _average(net_values),
    )


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _analytics_years(
    rows: tuple[EpisodeRow, ...], amounts: dict[str, Decimal], zone: ZoneInfo
) -> tuple[YearSummary, ...]:
    grouped: dict[int, list[EpisodeRow]] = defaultdict(list)
    for row in rows:
        if row.closed_at_ms is not None:
            close_year = (
                datetime.fromtimestamp(row.closed_at_ms / 1000, tz=UTC).astimezone(zone).year
            )
            grouped[close_year].append(row)
    cards: list[YearSummary] = []
    for year, episodes in sorted(grouped.items()):
        comparable = [amounts[row.episode_id] for row in episodes if row.episode_id in amounts]
        total = sum(comparable, Decimal(0)) if comparable else None
        win_rate = (
            sum(value > 0 for value in comparable) / len(comparable) * 100 if comparable else None
        )
        distribution = tuple(
            sum(
                lower <= float(value) < upper
                for value in comparable
            )
            for lower, upper in (
                (float("-inf"), -100.0),
                (-100.0, -25.0),
                (-25.0, 0.0),
                (0.0, 25.0),
                (25.0, 100.0),
                (100.0, float("inf")),
            )
        )
        average_ms = sum(row.duration_ms for row in episodes) // len(episodes)
        cards.append(
            YearSummary(
                str(year),
                len(episodes),
                float(total) if total is not None else None,
                None,
                win_rate,
                distribution,
                _duration(average_ms),
                "Net cashflow",
            )
        )
    return tuple(cards)


def _analytics_episodes(
    rows: tuple[EpisodeRow, ...],
    amounts: dict[str, Decimal],
    analytics: AnalyticsSnapshot,
    execution_rows: list[dict[str, str]],
    cashflow_rows: list[dict[str, str]],
    reporting_currency: str,
    zone: ZoneInfo,
) -> tuple[Episode, ...]:
    executions_by_id = {row.get("record_id", ""): row for row in execution_rows}
    execution_details: dict[str, list[ExecutionDetail]] = defaultdict(list)
    for link in getattr(analytics, "execution_links", ()):
        source = executions_by_id.get(link.execution_record_id)
        if not source:
            continue
        execution_details[link.episode_id].append(
            ExecutionDetail(
                occurred_at=_row_timestamp(source, "event_time_ms", zone),
                transition=link.transition.title(),
                side=str(source.get("side") or "").title(),
                price=_display_decimal(str(source.get("price") or "")),
                quantity=_display_decimal(str(link.quantity or source.get("quantity") or "")),
                quantity_unit=str(source.get("quantity_unit") or ""),
                notional=_execution_notional(source),
                fee=_display_decimal(str(source.get("fee") or "")),
                fee_currency=str(source.get("fee_currency") or ""),
                order_id=str(source.get("order_id") or ""),
            )
        )
    cashflows_by_id = {row.get("record_id", ""): row for row in cashflow_rows}
    cashflow_details: dict[str, list[CashflowDetail]] = defaultdict(list)
    for item in getattr(analytics, "cashflow_attributions", ()):
        source = cashflows_by_id.get(item.cashflow_record_id, {})
        cashflow_details[item.episode_id].append(
            CashflowDetail(
                occurred_at=_row_timestamp(source, "event_time_ms", zone),
                event_type=item.event_type.replace("_", " ").title(),
                amount=item.amount,
                currency=item.currency,
                reporting_amount=item.reporting_amount,
                reporting_currency=item.reporting_currency,
                method=item.method.replace("_", " ").title(),
            )
        )
    return tuple(
        _analytics_episode(
            row,
            amounts.get(row.episode_id),
            reporting_currency,
            tuple(execution_details.get(row.episode_id, ())),
            tuple(cashflow_details.get(row.episode_id, ())),
            zone,
        )
        for row in rows
    )


def _analytics_episode(
    row: EpisodeRow,
    amount: Decimal | None,
    reporting_currency: str,
    executions: tuple[ExecutionDetail, ...],
    cashflows: tuple[CashflowDetail, ...],
    zone: ZoneInfo,
) -> Episode:
    occurred = _format_timestamp(row.opened_at_ms, include_time=True, zone=zone)
    outcome = (
        "Open"
        if row.status == "open"
        else "Unavailable"
        if amount is None
        else "Win"
        if amount > 0
        else "Loss"
        if amount < 0
        else "Flat"
    )
    exit_value = _display_decimal(row.exit_vwap)
    if row.status == "open":
        exit_value = f"Partial @ {exit_value}" if row.exit_vwap else "Open"
    elif not row.exit_vwap:
        exit_value = "Missing exit"
    return Episode(
        episode_id=row.episode_id,
        occurred_at=occurred,
        profile=row.venue.replace("_", " ").title(),
        market_class=row.market_type.replace("_", " ").title(),
        instrument=row.symbol,
        status=row.status.title(),
        direction=row.direction.title(),
        duration=_duration(row.duration_ms),
        duration_bucket=row.duration_bucket,
        entry=_display_decimal(row.entry_vwap),
        exit=exit_value,
        outcome=outcome,
        net_result=float(amount) if amount is not None else None,
        result_currency=reporting_currency if amount is not None else "",
        tags=tuple(flag.replace("_", " ").title() for flag in row.quality_flags) or ("No flags",),
        executions=executions,
        cashflows=cashflows,
        quality=(
            QualityIndicator("Boundary status", row.boundary_status.title()),
            QualityIndicator("Source coverage", row.coverage_status.title()),
            QualityIndicator("Execution count", str(row.execution_count), "info"),
            QualityIndicator("Order count", str(row.order_count), "info"),
        ),
        confidence=(
            "High"
            if row.boundary_status == "complete" and row.coverage_status == "complete"
            else "Low"
        ),
        samples=row.execution_count,
    )


def _analytics_date_range(rows: tuple[EpisodeRow, ...], zone: ZoneInfo) -> str:
    if not rows:
        return "No available range"
    start = min(row.opened_at_ms for row in rows)
    end = max(row.closed_at_ms or row.opened_at_ms for row in rows)
    return f"{_format_timestamp(start, zone=zone)} – {_format_timestamp(end, zone=zone)}"


def _format_timestamp(
    value: int,
    *,
    include_time: bool = False,
    month: bool = False,
    zone: ZoneInfo | None = None,
) -> str:
    target_zone = zone or ZoneInfo("UTC")
    parsed = datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(target_zone)
    if month:
        return parsed.strftime("%Y-%m-01T00:00:00Z")
    return parsed.strftime(
        f"%b %d, %Y %H:%M {parsed.tzname() or target_zone.key}"
        if include_time
        else "%b %d, %Y"
    )


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


def _execution_notional(row: dict[str, str]) -> str:
    if row.get("notional"):
        return _display_decimal(row["notional"])
    try:
        base_quantity = Decimal(row.get("base_quantity") or "")
        price = Decimal(row.get("price") or "")
    except InvalidOperation:
        return "Unavailable"
    return _display_decimal(str(base_quantity * price))


def _row_timestamp(row: dict[str, str], field: str, zone: ZoneInfo) -> str:
    try:
        value = int(row.get(field) or 0)
    except ValueError:
        value = 0
    return _format_timestamp(value, include_time=True, zone=zone) if value else "Unavailable"


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _timezone_options(selected: str) -> tuple[FilterOption, ...]:
    preferred = (
        "UTC",
        "America/New_York",
        "America/Chicago",
        "Europe/London",
        "Europe/Berlin",
        "Asia/Bangkok",
        "Asia/Singapore",
        "Asia/Tokyo",
        "Australia/Sydney",
    )
    available = available_timezones()
    values = tuple(value for value in preferred if value in available or value == "UTC")
    if selected not in values:
        values = (*values, selected)
    return tuple(FilterOption(value.replace("_", " "), value) for value in values)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return date.min


def _analytics_date_bounds(
    rows: tuple[EpisodeRow, ...], zone: ZoneInfo
) -> tuple[str, str]:
    if not rows:
        return "", ""
    start = datetime.fromtimestamp(min(row.opened_at_ms for row in rows) / 1000, tz=UTC)
    end_ms = max(row.closed_at_ms or row.opened_at_ms for row in rows)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    return start.astimezone(zone).date().isoformat(), end.astimezone(zone).date().isoformat()


def _period_start(value: date, interval: str) -> date:
    if interval == "year":
        return date(value.year, 1, 1)
    if interval == "quarter":
        return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)
    return date(value.year, value.month, 1)


def _is_baseline_filter(
    filters: DashboardFilters, full_start_date: str, full_end_date: str
) -> bool:
    return all(
        (
            filters.profile == "all",
            filters.market_class == "all",
            not filters.start_date or filters.start_date == full_start_date,
            not filters.end_date or filters.end_date == full_end_date,
            filters.weekday is None,
            filters.hour_bucket is None,
            filters.direction == "all",
            filters.duration == "all",
            filters.outcome == "all",
            not filters.query,
        )
    )


def _dashboard_coverage_gaps(
    analytics: AnalyticsSnapshot, filters: DashboardFilters, zone: ZoneInfo
) -> tuple[CoverageGap, ...]:
    report = getattr(analytics, "build_report", None)
    gaps = getattr(report, "gaps", ())
    selected = (
        item
        for item in gaps
        if (filters.profile == "all" or str(getattr(item, "venue", "")).lower() == filters.profile)
        and (
            filters.market_class == "all"
            or str(getattr(item, "market_type", "")).lower() == filters.market_class
        )
        and _gap_overlaps_dates(item, filters, zone)
    )
    return tuple(
        CoverageGap(
            profile=" · ".join(
                part
                for part in (
                    str(getattr(item, "venue", "")).replace("_", " ").title(),
                    str(getattr(item, "account_id", "")).replace("_", " ").title(),
                )
                if part
            ),
            market_class=str(getattr(item, "market_type", "")).replace("_", " ").title(),
            start=_format_timestamp(
                int(getattr(item, "start_ms", 0)), include_time=True, zone=zone
            ),
            end=_format_timestamp(int(getattr(item, "end_ms", 0)), include_time=True, zone=zone),
            status=str(getattr(item, "status", "incomplete")).replace("_", " ").title(),
            reason=str(getattr(item, "reason", "") or "No source reason was recorded."),
            source=" · ".join(
                part
                for part in (
                    str(getattr(item, "source", "")).replace("_", " "),
                    str(getattr(item, "acquisition", "")).upper(),
                )
                if part
            )
            or "Recorded collection coverage",
        )
        for item in selected
    )


def _gap_overlaps_dates(
    item: AnalyticsCoverageGap, filters: DashboardFilters, zone: ZoneInfo
) -> bool:
    try:
        start = datetime.fromtimestamp(int(item.start_ms) / 1000, tz=UTC)
        end = datetime.fromtimestamp(int(item.end_ms) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return True
    start_date = start.astimezone(zone).date()
    end_date = end.astimezone(zone).date()
    return (
        (not filters.start_date or end_date >= _parse_date(filters.start_date))
        and (not filters.end_date or start_date <= _parse_date(filters.end_date))
    )


class SyntheticSnapshotProvider:
    """Demonstration records reconstructed by the production analytics pipeline."""

    def __init__(self, *, revision: str = "synthetic-v2") -> None:
        self._revision = revision
        self._tables = demonstration_tables()
        self._analytics = build_snapshot_from_tables(self._tables)

    def revision(self) -> str:
        return self._revision

    def load(self, filters: DashboardFilters) -> DashboardSnapshot:
        return _adapt_analytics(
            self._analytics,
            filters,
            revision=self._revision,
            reporting_currency="USDT",
            execution_rows=self._tables["executions"],
            cashflow_rows=self._tables["cashflows"],
        )


def _apply_horizon(
    points: tuple[PerformancePoint, ...], horizon: str, _interval: str
) -> tuple[PerformancePoint, ...]:
    years = {"1y": 1, "2y": 2, "3y": 3}.get(horizon)
    if not years:
        return points
    if not points:
        return ()
    latest_period = date.fromisoformat(points[-1].timestamp[:10])
    boundary = date(latest_period.year - years, latest_period.month, 1)
    visible = tuple(
        point
        for point in points
        if date.fromisoformat(point.timestamp[:10]) > boundary
    )
    running = Decimal(0)
    peak = Decimal(0)
    recent: list[Decimal] = []
    rebased = [
        PerformancePoint(
            f"{boundary.isoformat()}T00:00:00",
            0.0,
            0.0,
            0.0,
            0.0,
        )
    ]
    for point in visible:
        amount = Decimal(str(point.period))
        running += amount
        peak = max(peak, running)
        recent.append(amount)
        rebased.append(
            PerformancePoint(
                point.timestamp,
                float(running),
                point.period,
                float(sum(recent[-3:], Decimal(0))),
                float(running - peak),
            )
        )
    return tuple(rebased)
