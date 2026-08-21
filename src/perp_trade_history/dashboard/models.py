from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Tone = Literal["positive", "negative", "neutral", "info"]


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    profile: str = "all"
    market_class: str = "all"
    interval: str = "month"
    horizon: str = "all"
    start_date: str = ""
    end_date: str = ""
    timezone: str = "UTC"
    performance_view: str = "cumulative"
    weekday: int | None = None
    hour_bucket: int | None = None
    direction: str = "all"
    duration: str = "all"
    outcome: str = "all"
    query: str = ""


@dataclass(frozen=True, slots=True)
class FilterOption:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Kpi:
    label: str
    value: str
    change: str
    note: str
    tone: Tone = "neutral"


@dataclass(frozen=True, slots=True)
class PerformancePoint:
    timestamp: str
    net: float
    period: float
    rolling: float
    drawdown: float


@dataclass(frozen=True, slots=True)
class Regime:
    label: str
    start: str
    end: str
    tone: Tone = "neutral"


@dataclass(frozen=True, slots=True)
class NotableChange:
    rank: int
    title: str
    summary: str
    episodes: int
    confidence: str
    tone: Tone


@dataclass(frozen=True, slots=True)
class HeatmapData:
    hours: tuple[str, ...]
    weekdays: tuple[str, ...]
    values: tuple[tuple[float | None, ...], ...]
    metric_label: str = "Performance by weekday & hour"
    unit: str = "reporting amount"
    diverging: bool = True


@dataclass(frozen=True, slots=True)
class ExposureBand:
    label: str
    filter_value: str
    long_count: int
    long_average: float | None
    short_count: int
    short_average: float | None
    net_average: float | None


@dataclass(frozen=True, slots=True)
class YearSummary:
    label: str
    episodes: int
    net_result: float | None
    change: float | None
    win_rate: float | None
    distribution: tuple[int, ...]
    average_hold: str
    result_label: str = "Net result"


@dataclass(frozen=True, slots=True)
class ExecutionDetail:
    occurred_at: str
    transition: str
    side: str
    price: str
    quantity: str
    quantity_unit: str
    notional: str
    fee: str
    fee_currency: str
    order_id: str


@dataclass(frozen=True, slots=True)
class CashflowDetail:
    occurred_at: str
    event_type: str
    amount: str
    currency: str
    reporting_amount: str
    reporting_currency: str
    method: str


@dataclass(frozen=True, slots=True)
class QualityIndicator:
    label: str
    value: str
    tone: Tone = "positive"


@dataclass(frozen=True, slots=True)
class Episode:
    episode_id: str
    occurred_at: str
    profile: str
    market_class: str
    instrument: str
    status: str
    direction: str
    duration: str
    duration_bucket: str
    entry: str
    exit: str
    outcome: str
    net_result: float | None
    result_currency: str
    tags: tuple[str, ...]
    executions: tuple[ExecutionDetail, ...]
    cashflows: tuple[CashflowDetail, ...]
    quality: tuple[QualityIndicator, ...]
    confidence: str
    samples: int


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    percent: float
    complete: int
    total: int
    date_range: str
    boundary_complete: int = 0
    open_episodes: int = 0
    left_censored: int = 0


@dataclass(frozen=True, slots=True)
class CoverageGap:
    profile: str
    market_class: str
    start: str
    end: str
    status: str
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class BuildSummary:
    timestamp: str
    build_id: str
    source_data: str
    next_build: str
    status: str


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    revision: str
    refreshed_label: str
    timezone: str
    date_range: str
    currency: str
    status: Literal["ready", "empty", "error"] = "ready"
    message: str = ""
    profiles: tuple[FilterOption, ...] = ()
    market_classes: tuple[FilterOption, ...] = ()
    timezone_options: tuple[FilterOption, ...] = ()
    date_start: str = ""
    date_end: str = ""
    kpis: tuple[Kpi, ...] = ()
    performance: tuple[PerformancePoint, ...] = ()
    regimes: tuple[Regime, ...] = ()
    notable_changes: tuple[NotableChange, ...] = ()
    heatmap: HeatmapData = field(
        default_factory=lambda: HeatmapData(hours=(), weekdays=(), values=())
    )
    exposure: tuple[ExposureBand, ...] = ()
    exposure_metric_label: str = "Average result"
    exposure_net_label: str = "Net result"
    years: tuple[YearSummary, ...] = ()
    episodes: tuple[Episode, ...] = ()
    coverage: CoverageSummary = field(
        default_factory=lambda: CoverageSummary(0.0, 0, 0, "No available range")
    )
    coverage_gaps: tuple[CoverageGap, ...] = ()
    build: BuildSummary = field(
        default_factory=lambda: BuildSummary("Unavailable", "—", "—", "—", "Unavailable")
    )


def empty_snapshot(
    *, revision: str = "empty", message: str = "No analytics data is available yet"
) -> DashboardSnapshot:
    return DashboardSnapshot(
        revision=revision,
        refreshed_label="Waiting for data",
        timezone="UTC",
        date_range="No available range",
        currency="Reporting currency",
        status="empty",
        message=message,
    )
