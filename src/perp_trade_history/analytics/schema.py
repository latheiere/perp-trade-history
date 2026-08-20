from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldProfile:
    name: str
    populated: int
    total: int

    @property
    def completeness(self) -> float:
        return self.populated / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class TableProfile:
    name: str
    rows: int
    earliest_ms: int | None
    latest_ms: int | None
    distinct_accounts: int
    distinct_instruments: int
    fields: tuple[FieldProfile, ...]


@dataclass(frozen=True, slots=True)
class DataProfile:
    as_of_ms: int
    tables: tuple[TableProfile, ...]
    execution_position_modes: tuple[str, ...]
    tied_execution_groups: int
    opposing_tied_transition_groups: int
    primary_trading_cashflows: int
    exactly_linkable_primary_cashflows: int


@dataclass(frozen=True, slots=True)
class AnalyticsCapabilities:
    episode_reconstruction: bool
    exact_episode_boundaries: bool
    exact_execution_cashflow_attribution: bool
    temporal_cashflow_attribution: bool
    notional_metrics: bool
    capital_return_metrics: bool
    mark_to_market_metrics: bool
    complete_coverage: bool


@dataclass(frozen=True, slots=True)
class QualityFlag:
    code: str
    severity: str
    count: int
    detail: str


@dataclass(frozen=True, slots=True)
class EpisodeExecutionLink:
    episode_id: str
    execution_record_id: str
    transition: str
    quantity: str


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    episode_id: str
    venue: str
    account_id: str
    market_type: str
    symbol: str
    position_mode: str
    direction: str
    status: str
    boundary_status: str
    opened_at_ms: int
    closed_at_ms: int | None
    duration_ms: int
    duration_bucket: str
    open_year: int
    open_month: str
    close_year: int | None
    close_month: str
    quantity_unit: str
    opened_quantity: str
    closed_quantity: str
    maximum_quantity: str
    remaining_quantity: str
    entry_vwap: str
    exit_vwap: str
    execution_count: int
    order_count: int
    coverage_status: str
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CashflowAttribution:
    cashflow_record_id: str
    episode_id: str
    method: str
    confidence: str
    reason: str
    reporting_role: str
    event_type: str
    currency: str
    amount: str
    reporting_currency: str
    reporting_amount: str
    fraction: str


@dataclass(frozen=True, slots=True)
class EpisodeCashflowSummary:
    episode_id: str
    event_type: str
    currency: str
    amount: str
    reporting_currency: str
    reporting_amount: str
    events: int


@dataclass(frozen=True, slots=True)
class AccountCashflowSummary:
    venue: str
    account_id: str
    event_type: str
    reporting_role: str
    classification: str
    currency: str
    amount: str
    reporting_currency: str
    reporting_amount: str
    attributed_amount: str
    unattributed_amount: str
    excluded_amount: str
    attributed_reporting_amount: str
    unattributed_reporting_amount: str
    excluded_reporting_amount: str
    events: int


@dataclass(frozen=True, slots=True)
class FilterDimensions:
    venues: tuple[str, ...]
    accounts: tuple[str, ...]
    market_types: tuple[str, ...]
    symbols: tuple[str, ...]
    directions: tuple[str, ...]
    statuses: tuple[str, ...]
    duration_buckets: tuple[str, ...]
    open_years: tuple[int, ...]
    close_years: tuple[int, ...]
    currencies: tuple[str, ...]
    event_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotableChange:
    comparison: str
    metric: str
    prior_period: str
    current_period: str
    prior_value: str
    current_value: str
    absolute_delta: str
    relative_change: str
    unit: str
    direction: str
    prior_sample_size: int
    current_sample_size: int
    threshold: str
    summary: str


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    as_of_ms: int
    profile: DataProfile
    capabilities: AnalyticsCapabilities
    quality_flags: tuple[QualityFlag, ...]
    episodes: tuple[EpisodeRow, ...]
    execution_links: tuple[EpisodeExecutionLink, ...]
    cashflow_attributions: tuple[CashflowAttribution, ...]
    episode_cashflows: tuple[EpisodeCashflowSummary, ...]
    account_cashflows: tuple[AccountCashflowSummary, ...]
    notable_changes: tuple[NotableChange, ...]
    dimensions: FilterDimensions

    def as_dict(self) -> dict[str, Any]:
        """Return a recursively serializable representation for external consumers."""
        return asdict(self)
