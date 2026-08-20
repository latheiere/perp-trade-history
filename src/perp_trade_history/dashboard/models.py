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
    benchmark: float | None
    drawdown: float | None


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
    metric_label: str = "Performance by weekday & hour (average R per episode)"
    unit: str = "R"
    diverging: bool = True


@dataclass(frozen=True, slots=True)
class ExposureBand:
    label: str
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
class AttributionItem:
    label: str
    value: float


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
    direction: str
    duration: str
    duration_bucket: str
    entry: str
    exit: str
    r_multiple: float | None
    outcome: str
    mae: float | None
    mfe: float | None
    tags: tuple[str, ...]
    timeline_labels: tuple[str, ...]
    timeline_values: tuple[float, ...]
    attribution: tuple[AttributionItem, ...]
    quality: tuple[QualityIndicator, ...]
    confidence: str
    samples: int


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    percent: float
    complete: int
    total: int
    date_range: str


@dataclass(frozen=True, slots=True)
class UnsupportedMetric:
    label: str
    percent: float | None = None


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
    kpis: tuple[Kpi, ...] = ()
    performance: tuple[PerformancePoint, ...] = ()
    regimes: tuple[Regime, ...] = ()
    notable_changes: tuple[NotableChange, ...] = ()
    heatmap: HeatmapData = field(
        default_factory=lambda: HeatmapData(hours=(), weekdays=(), values=())
    )
    exposure: tuple[ExposureBand, ...] = ()
    exposure_metric_label: str = "Avg R"
    exposure_net_label: str = "Net R"
    years: tuple[YearSummary, ...] = ()
    episodes: tuple[Episode, ...] = ()
    coverage: CoverageSummary = field(
        default_factory=lambda: CoverageSummary(0.0, 0, 0, "No available range")
    )
    unsupported_metrics: tuple[UnsupportedMetric, ...] = ()
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
