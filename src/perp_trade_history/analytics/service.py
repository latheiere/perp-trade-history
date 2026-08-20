from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from typing import Protocol

from perp_trade_history.analytics.attribution import attribute_cashflows
from perp_trade_history.analytics.changes import find_notable_changes
from perp_trade_history.analytics.constants import TABLE_TIME_FIELDS, TRADING_CASHFLOW_TYPES
from perp_trade_history.analytics.episodes import EpisodeBuilder, EpisodeBuildResult
from perp_trade_history.analytics.profile import build_data_profile, profile_quality_flags
from perp_trade_history.analytics.schema import (
    AccountCashflowSummary,
    AnalyticsCapabilities,
    AnalyticsSnapshot,
    DataProfile,
    EpisodeRow,
    FilterDimensions,
    QualityFlag,
)
from perp_trade_history.storage import DataStore


class CashflowConversion(Protocol):
    def prepare(self, rows: list[dict[str, str]]) -> None: ...

    def amount(self, row: dict[str, str]) -> Decimal: ...

    def currency(self, row: dict[str, str]) -> str: ...


def build_snapshot(
    store: DataStore,
    *,
    as_of_ms: int | None = None,
    conversion: CashflowConversion | None = None,
) -> AnalyticsSnapshot:
    """Build a deterministic analytics snapshot without changing canonical storage."""
    loaded = {name: table.read() for name, table in store.tables.items()}
    effective_as_of_ms = as_of_ms if as_of_ms is not None else _latest_fact_time(loaded)
    tables = {
        name: _rows_as_of(name, rows, effective_as_of_ms)
        for name, rows in loaded.items()
    }
    cashflows = tables["cashflows"]
    if conversion:
        conversion.prepare(cashflows)
    converted = {
        row["record_id"]: (
            conversion.currency(row) if conversion else row.get("currency", ""),
            conversion.amount(row)
            if conversion
            else _decimal(row.get("amount")),
        )
        for row in cashflows
    }

    profile = build_data_profile(tables, as_of_ms=effective_as_of_ms)
    episode_result = EpisodeBuilder(
        as_of_ms=effective_as_of_ms,
        coverage_rows=tables["coverage"],
    ).build(tables["executions"])
    attribution = attribute_cashflows(
        cashflows=cashflows,
        executions=tables["executions"],
        episodes=episode_result.episodes,
        links=episode_result.links,
        converted=converted,
    )
    quality_flags = _quality_flags(
        base=profile_quality_flags(tables, profile),
        episode_result=episode_result,
        unattributed=attribution.unattributed_primary_cashflows,
        ambiguous=attribution.ambiguous_primary_cashflows,
    )
    capabilities = _capabilities(
        tables=tables,
        profile=profile,
        episode_result=episode_result,
        attribution_count=len(attribution.attributions),
    )
    return AnalyticsSnapshot(
        as_of_ms=effective_as_of_ms,
        profile=profile,
        capabilities=capabilities,
        quality_flags=quality_flags,
        episodes=episode_result.episodes,
        execution_links=episode_result.links,
        cashflow_attributions=attribution.attributions,
        episode_cashflows=attribution.episode_summaries,
        account_cashflows=attribution.account_summaries,
        notable_changes=find_notable_changes(
            episode_result.episodes, attribution.episode_summaries
        ),
        dimensions=_dimensions(
            episode_result.episodes,
            attribution.account_summaries,
        ),
    )


def _latest_fact_time(tables: dict[str, list[dict[str, str]]]) -> int:
    latest = 0
    for name in ("executions", "cashflows", "account_snapshots"):
        for row in tables.get(name, []):
            for field in TABLE_TIME_FIELDS[name]:
                with suppress(ValueError):
                    latest = max(latest, int(row.get(field) or 0))
    return latest


def _rows_as_of(
    name: str, rows: list[dict[str, str]], as_of_ms: int
) -> list[dict[str, str]]:
    primary_field = TABLE_TIME_FIELDS.get(name, ("",))[0]
    if not primary_field:
        return list(rows)
    selected: list[dict[str, str]] = []
    for row in rows:
        raw = row.get(primary_field, "")
        if not raw:
            selected.append(row)
            continue
        try:
            if int(raw) <= as_of_ms:
                selected.append(row)
        except ValueError:
            selected.append(row)
    return selected


def _capabilities(
    *,
    tables: dict[str, list[dict[str, str]]],
    profile: DataProfile,
    episode_result: EpisodeBuildResult,
    attribution_count: int,
) -> AnalyticsCapabilities:
    diagnostics = episode_result.diagnostics
    valid_executions = len(tables["executions"]) - diagnostics.skipped_invalid_executions
    exact_boundaries = bool(episode_result.episodes) and not (
        diagnostics.left_censored_episodes
        or diagnostics.right_censored_episodes
        or profile.opposing_tied_transition_groups
        or diagnostics.unsupported_action_executions
    )
    notional_metrics = bool(valid_executions) and all(
        _has_notional_basis(row) for row in tables["executions"] if _valid_numeric_execution(row)
    )
    coverage_complete = bool(episode_result.episodes) and all(
        episode.coverage_status == "complete" for episode in episode_result.episodes
    )
    return AnalyticsCapabilities(
        episode_reconstruction=bool(valid_executions and episode_result.episodes),
        exact_episode_boundaries=exact_boundaries,
        exact_execution_cashflow_attribution=(
            bool(profile.exactly_linkable_primary_cashflows) and bool(attribution_count)
        ),
        temporal_cashflow_attribution=bool(
            episode_result.episodes
            and any(
                row.get("reporting_role") == "primary"
                and row.get("event_type") in TRADING_CASHFLOW_TYPES
                and row.get("symbol")
                for row in tables["cashflows"]
            )
        ),
        notional_metrics=notional_metrics,
        capital_return_metrics=False,
        mark_to_market_metrics=False,
        complete_coverage=coverage_complete,
    )


def _quality_flags(
    *,
    base: tuple[QualityFlag, ...],
    episode_result: EpisodeBuildResult,
    unattributed: int,
    ambiguous: int,
) -> tuple[QualityFlag, ...]:
    diagnostics = episode_result.diagnostics
    flags = list(base)
    additions = (
        (
            diagnostics.orphan_close_events,
            "orphan_close_event",
            "warning",
            "A closing execution was observed without prior position inventory.",
        ),
        (
            diagnostics.oversized_close_events,
            "oversized_close_event",
            "warning",
            "A closing execution exceeded the reconstructed position inventory.",
        ),
        (
            diagnostics.mixed_mode_partitions,
            "mixed_position_modes",
            "warning",
            "A partition contains both explicit-side and net-position execution actions.",
        ),
        (
            diagnostics.left_censored_episodes,
            "left_censored_episode",
            "info",
            "An episode begins before the available execution boundary.",
        ),
        (
            diagnostics.right_censored_episodes,
            "right_censored_episode",
            "info",
            "An episode remains open at the analytics boundary.",
        ),
        (
            unattributed,
            "unattributed_primary_cashflow",
            "warning",
            "A primary trading cashflow does not have one defensible episode boundary.",
        ),
        (
            ambiguous,
            "ambiguous_temporal_cashflow",
            "warning",
            "A primary trading cashflow overlaps multiple active episode candidates.",
        ),
    )
    flags.extend(
        QualityFlag(code=code, severity=severity, count=count, detail=detail)
        for count, code, severity, detail in additions
        if count
    )
    combined: dict[tuple[str, str, str], int] = defaultdict(int)
    for flag in flags:
        combined[(flag.code, flag.severity, flag.detail)] += flag.count
    return tuple(
        QualityFlag(code=key[0], severity=key[1], detail=key[2], count=count)
        for key, count in sorted(combined.items())
    )


def _dimensions(
    episode_rows: Sequence[EpisodeRow],
    summary_rows: Sequence[AccountCashflowSummary],
) -> FilterDimensions:
    return FilterDimensions(
        venues=tuple(
            sorted(
                {episode.venue for episode in episode_rows}
                | {summary.venue for summary in summary_rows}
            )
        ),
        accounts=tuple(
            sorted(
                {episode.account_id for episode in episode_rows}
                | {summary.account_id for summary in summary_rows}
            )
        ),
        market_types=tuple(sorted({episode.market_type for episode in episode_rows})),
        symbols=tuple(sorted({episode.symbol for episode in episode_rows})),
        directions=tuple(sorted({episode.direction for episode in episode_rows})),
        statuses=tuple(sorted({episode.status for episode in episode_rows})),
        duration_buckets=tuple(
            bucket
            for bucket in (
                "under_1h",
                "1h_to_1d",
                "1d_to_7d",
                "7d_to_30d",
                "30d_or_more",
            )
            if any(episode.duration_bucket == bucket for episode in episode_rows)
        ),
        open_years=tuple(sorted({episode.open_year for episode in episode_rows})),
        close_years=tuple(
            sorted(
                {
                    episode.close_year
                    for episode in episode_rows
                    if episode.close_year is not None
                }
            )
        ),
        currencies=tuple(sorted({summary.currency for summary in summary_rows})),
        event_types=tuple(sorted({summary.event_type for summary in summary_rows})),
    )


def _has_notional_basis(row: dict[str, str]) -> bool:
    if row.get("notional"):
        return True
    if row.get("base_quantity") and row.get("price"):
        return True
    return bool(row.get("quantity_unit") == "base" and row.get("quantity") and row.get("price"))


def _valid_numeric_execution(row: dict[str, str]) -> bool:
    try:
        return Decimal(row.get("quantity") or "0") > 0 and Decimal(
            row.get("price") or "0"
        ) > 0
    except InvalidOperation:
        return False


def _decimal(value: str | None) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal(0)
