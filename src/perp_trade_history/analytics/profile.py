from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import Decimal, InvalidOperation

from perp_trade_history.analytics.constants import (
    KNOWN_ACTIONS,
    TABLE_TIME_FIELDS,
    TRADING_CASHFLOW_TYPES,
)
from perp_trade_history.analytics.ordering import execution_mode, profile_tied_transitions
from perp_trade_history.analytics.schema import (
    DataProfile,
    FieldProfile,
    QualityFlag,
    TableProfile,
)
from perp_trade_history.models import TABLE_FIELDS


def build_data_profile(
    tables: Mapping[str, Sequence[dict[str, str]]], *, as_of_ms: int
) -> DataProfile:
    executions = tables.get("executions", ())
    cashflows = tables.get("cashflows", ())
    tied = profile_tied_transitions(executions)
    trade_keys = Counter(
        _context_key(row, "trade_id") for row in executions if row.get("trade_id")
    )
    order_keys = Counter(
        _context_key(row, "order_id") for row in executions if row.get("order_id")
    )
    primary = [
        row
        for row in cashflows
        if row.get("reporting_role") == "primary"
        and row.get("event_type") in TRADING_CASHFLOW_TYPES
    ]
    exactly_linkable = 0
    for row in primary:
        trade_key = _context_key(row, "trade_id") if row.get("trade_id") else None
        order_key = _context_key(row, "order_id") if row.get("order_id") else None
        if (trade_key and trade_keys[trade_key] == 1) or (
            order_key and order_keys[order_key] > 0
        ):
            exactly_linkable += 1

    return DataProfile(
        as_of_ms=as_of_ms,
        tables=tuple(
            _table_profile(name, tables.get(name, ()))
            for name in sorted(TABLE_FIELDS)
        ),
        execution_position_modes=tuple(
            sorted({execution_mode(row) for row in executions})
        ),
        tied_execution_groups=tied.tied_groups,
        opposing_tied_transition_groups=tied.opposing_groups,
        primary_trading_cashflows=len(primary),
        exactly_linkable_primary_cashflows=exactly_linkable,
    )


def profile_quality_flags(
    tables: Mapping[str, Sequence[dict[str, str]]], profile: DataProfile
) -> tuple[QualityFlag, ...]:
    executions = tables.get("executions", ())
    positions = tables.get("positions", ())
    coverage = tables.get("coverage", ())
    flags: list[QualityFlag] = []

    unknown_actions = sum(
        row.get("position_action") not in KNOWN_ACTIONS for row in executions
    )
    _append_flag(
        flags,
        unknown_actions,
        "unsupported_execution_action",
        "error",
        "Execution rows with an unsupported position transition were excluded.",
    )
    invalid_executions = sum(not _valid_execution(row) for row in executions)
    _append_flag(
        flags,
        invalid_executions,
        "invalid_execution_fact",
        "error",
        "Execution rows with an invalid timestamp, quantity, or price were excluded.",
    )
    missing_contract_economics = sum(
        row.get("quantity_unit") == "contract"
        and not row.get("contract_size")
        and not row.get("base_quantity")
        and not row.get("notional")
        for row in executions
    )
    _append_flag(
        flags,
        missing_contract_economics,
        "missing_contract_economics",
        "warning",
        "Contract-denominated executions cannot support notional-based metrics.",
    )
    _append_flag(
        flags,
        profile.opposing_tied_transition_groups,
        "opposing_tied_transitions",
        "warning",
        "Opposing position transitions share a timestamp without a semantic source sequence.",
    )
    incomplete_coverage = sum(
        row.get("status") in {"partial", "failed"} for row in coverage
    )
    _append_flag(
        flags,
        incomplete_coverage,
        "incomplete_coverage",
        "warning",
        "Stored collection intervals include incomplete historical coverage.",
    )
    sparse_boundaries = sum(
        not row.get("opened_at_ms") or (
            row.get("status") != "open" and not row.get("closed_at_ms")
        )
        for row in positions
    )
    _append_flag(
        flags,
        sparse_boundaries,
        "sparse_position_boundaries",
        "info",
        "Position summaries without complete boundaries are used only for supporting evidence.",
    )
    return tuple(sorted(flags, key=lambda flag: (flag.severity, flag.code)))


def _table_profile(name: str, rows: Sequence[dict[str, str]]) -> TableProfile:
    timestamps: list[int] = []
    for row in rows:
        for field in TABLE_TIME_FIELDS.get(name, ()):
            value = row.get(field)
            if value:
                with suppress(ValueError):
                    timestamps.append(int(value))
    fields = TABLE_FIELDS[name]
    return TableProfile(
        name=name,
        rows=len(rows),
        earliest_ms=min(timestamps) if timestamps else None,
        latest_ms=max(timestamps) if timestamps else None,
        distinct_accounts=len({row.get("account_id", "") for row in rows if row.get("account_id")}),
        distinct_instruments=len({row.get("symbol", "") for row in rows if row.get("symbol")}),
        fields=tuple(
            FieldProfile(
                name=field,
                populated=sum(bool(row.get(field)) for row in rows),
                total=len(rows),
            )
            for field in fields
        ),
    )


def _context_key(row: dict[str, str], identifier: str) -> tuple[str, ...]:
    return (
        row.get("venue", ""),
        row.get("account_id", ""),
        row.get("market_type", ""),
        row.get("symbol", ""),
        row.get(identifier, ""),
    )


def _valid_execution(row: dict[str, str]) -> bool:
    try:
        return (
            int(row.get("event_time_ms") or 0) > 0
            and Decimal(row.get("quantity") or "0") > 0
            and Decimal(row.get("price") or "0") > 0
        )
    except (InvalidOperation, ValueError):
        return False


def _append_flag(
    flags: list[QualityFlag], count: int, code: str, severity: str, detail: str
) -> None:
    if count:
        flags.append(QualityFlag(code=code, severity=severity, count=count, detail=detail))
