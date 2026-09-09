from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from perp_trade_history.adapters import ADAPTERS
from perp_trade_history.analytics.episodes import EpisodeBuilder
from perp_trade_history.models import (
    canonical_settlement_currency,
    decimal_text,
    parse_datetime,
)
from perp_trade_history.storage import CoverageIndex, DataStore

REPORT_FIELDS = frozenset({"venue", "account_id", "symbol", "event_type", "currency"})
REPORT_PERIODS = frozenset({"none", "hour", "day", "week", "month", "year"})
PNL_EVENT_TYPES = frozenset(
    {
        "realized_pnl",
        "funding",
        "commission",
        "rebate",
        "reward",
        "settlement",
        "insurance",
        "premium",
        "bonus",
    }
)
PNL_REQUIRED_DATASETS = {
    "binance": ("trades", "income"),
    "gate": ("income",),
    "mexc": ("trades", "income"),
}
NON_PNL_EVENT_TYPES = frozenset({"transfer", "conversion"})


def position_trade_counts(
    store: DataStore,
    *,
    period: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    venues: set[str] | None = None,
    symbols: set[str] | None = None,
) -> dict[tuple[str, str, str], int]:
    """Count observed position cycles, including open and boundary-truncated cycles."""
    if period not in REPORT_PERIODS:
        raise ValueError(f"unsupported period: {period}")

    def selected(row: dict[str, str]) -> bool:
        return (not venues or row["venue"] in venues) and (
            not symbols or row["symbol"] in symbols
        )

    def position_time(row: dict[str, str]) -> int:
        timestamp = int(row["source_updated_at_ms"] or row["closed_at_ms"]
                        or row["opened_at_ms"] or 0)
        if not timestamp and row["collected_at"]:
            timestamp = int(parse_datetime(row["collected_at"]).timestamp() * 1000)
        return timestamp

    executions = [row for row in store.tables["executions"].iter_read() if selected(row)]
    positions = [row for row in store.tables["positions"].iter_read() if selected(row)]
    as_of = end_ms if end_ms is not None else max(
        [int(row["event_time_ms"] or 0) for row in executions]
        + [position_time(row) for row in positions],
        default=0,
    )
    summary_ids = {
        (row["venue"], row["account_id"], row["symbol"], row["position_id"])
        for row in store.tables["cashflows"].iter_read()
        if row["event_subtype"] == "position_summary"
    }
    trades: list[tuple[tuple[str, ...], int, int | None]] = []

    # Complete venue summaries retain lifetimes even when individual fills expired.
    # Zero-position snapshots without an opening and closing time are not trades.
    summaries: dict[tuple[str, ...], list[tuple[int, int]]] = defaultdict(list)
    snapshots: list[tuple[tuple[str, ...], int]] = []
    seen: set[tuple[str, ...]] = set()
    for row in positions:
        opened = int(row["opened_at_ms"] or 0)
        closed = int(row["closed_at_ms"] or 0)
        context = (
            row["venue"], row["account_id"], row["market_type"],
            row["symbol"], row["position_side"],
        )
        summary_id = (row["venue"], row["account_id"], row["symbol"], row["position_id"])
        is_open_summary = row["status"] == "open" and summary_id in summary_ids
        if not (closed > 0 or is_open_summary):
            if row["status"] == "open" and Decimal(row["quantity"] or "0") > 0:
                snapshots.append((context, position_time(row)))
            continue
        opened = opened or closed
        if opened <= 0 or opened > as_of or (closed and closed < opened):
            continue
        identity = (*context, row["position_id"] or row["record_id"])
        if identity in seen:
            continue
        seen.add(identity)
        summaries[context].append((opened, closed or as_of))
        trades.append((context, opened, closed if 0 < closed <= as_of else None))

    # Native lifetime IDs keep partial history together even when missing fills
    # would otherwise make execution reconstruction split a single position.
    native_ids: dict[str, str] = {}
    for venue, adapter in ADAPTERS.items():
        rows = [row for row in executions if row["venue"] == venue]
        if rows:
            native_ids.update(adapter.position_lifetime_ids(store, rows))
    native_opened: dict[tuple[str, ...], int] = {}
    for row in executions:
        native_id = native_ids.get(row["record_id"])
        timestamp = int(row["event_time_ms"] or 0)
        if not native_id or not 0 < timestamp <= as_of:
            continue
        context = (
            row["venue"], row["account_id"], row["market_type"],
            row["symbol"], row["position_side"],
        )
        identity = (*context, native_id)
        if identity not in seen:
            native_opened[identity] = min(native_opened.get(identity, timestamp), timestamp)
    trades.extend((identity[:-1], opened, None) for identity, opened in native_opened.items())

    # Read the opening history before filtering, so a window does not split a trade.
    episodes = EpisodeBuilder(as_of_ms=as_of).build(
        row for row in executions if row["record_id"] not in native_ids
    ).episodes
    boundary_runs: dict[tuple[str, ...], int] = {}
    for episode in episodes:
        closed = episode.closed_at_ms
        context = (
            episode.venue, episode.account_id, episode.market_type,
            episode.symbol, episode.direction,
        )
        # A summary and the executions within that position describe the same trade.
        # Adjacent lifetimes meeting at one timestamp remain separate trades.
        covered = any(
            (episode.opened_at_ms < end and (closed or as_of) > start)
            or (episode.opened_at_ms == closed and start < closed <= end)
            or (start == end and episode.opened_at_ms <= start <= (closed or as_of))
            for start, end in summaries.get(context, ())
        )
        if covered:
            boundary_runs.pop(context, None)
            continue
        left_censored = episode.boundary_status in {"left_censored", "both_censored"}
        if left_censored and context in boundary_runs:
            index = boundary_runs[context]
            # Successive partial closes without an observed opening are one
            # boundary-truncated position, not one trade per reduction.
            trades[index] = (context, trades[index][1], closed)
        else:
            if left_censored:
                boundary_runs[context] = len(trades)
            else:
                boundary_runs.pop(context, None)
            trades.append((context, episode.opened_at_ms, closed))

    # A nonzero snapshot is evidence of a trade even if both execution boundaries
    # are missing. Repeated snapshots within a known cycle do not add trades.
    for context, observed in sorted(snapshots, key=lambda item: item[1]):
        if 0 < observed <= as_of and not any(
            context == trade_context and opened <= observed <= (closed or as_of)
            for trade_context, opened, closed in trades
        ):
            trades.append((context, observed, None))

    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for context, opened, closed in trades:
        if start_ms is not None and closed is not None and closed < start_ms:
            continue
        # Closed cycles belong to their close period. Ongoing cycles belong to
        # their first observed activity, clipped to the requested window start.
        counted_at = closed if closed is not None else max(opened, start_ms or 0)
        if counted_at > as_of:
            continue
        bucket = "all" if period == "none" else _period_start_ms(counted_at, period)
        counts[(bucket, context[0], context[3])] += 1
    return dict(counts)


class ReportConversion(Protocol):
    def prepare(self, rows: list[dict[str, str]]) -> None: ...

    def amount(self, row: dict[str, str]) -> Decimal: ...

    def currency(self, row: dict[str, str]) -> str: ...


def pnl_report(
    store: DataStore,
    *,
    period: str,
    group_by: list[str],
    include_non_pnl: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
    venues: set[str] | None = None,
    symbols: set[str] | None = None,
    conversion: ReportConversion | None = None,
) -> list[dict[str, Any]]:
    if period not in REPORT_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    unknown = set(group_by) - REPORT_FIELDS
    if unknown:
        raise ValueError(f"unsupported group fields: {sorted(unknown)}")

    selected = select_cashflows(
        store,
        include_non_pnl=include_non_pnl,
        start_ms=start_ms,
        end_ms=end_ms,
        venues=venues,
        symbols=symbols,
    )

    if conversion:
        conversion.prepare(selected)

    currencies = {_report_currency(row, conversion) for row in selected}
    if len(currencies) > 1 and "currency" not in group_by:
        raise ValueError("currency must be grouped when multiple settlement currencies are present")

    totals: dict[tuple[str, ...], tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))
    contexts: dict[tuple[str, ...], set[tuple[str, str]]] = defaultdict(set)
    other_breakdowns: dict[tuple[str, ...], dict[str, tuple[Decimal, int]]] = defaultdict(
        dict
    )
    observed_bounds: dict[tuple[str, ...], tuple[int, int]] = {}
    for row in selected:
        timestamp_ms = int(row.get("event_time_ms") or 0)
        period_start = _period_start_ms(timestamp_ms, period)
        key_parts = [period_start] if period != "none" else []
        key_parts.extend(
            _report_currency(row, conversion) if field == "currency" else row.get(field, "")
            for field in group_by
        )
        key = tuple(key_parts)
        amount, count = totals[key]
        report_amount = (
            conversion.amount(row)
            if conversion
            else Decimal(row.get("amount") or "0")
        )
        totals[key] = amount + report_amount, count + 1
        contexts[key].add((row.get("venue", ""), row.get("account_id", "")))
        if row.get("event_type") == "other":
            breakdown = other_breakdowns.setdefault(key, {})
            subtype = str(row.get("event_subtype") or "(missing)")
            other_amount, other_count = breakdown.get(subtype, (Decimal(0), 0))
            breakdown[subtype] = (
                other_amount + report_amount,
                other_count + 1,
            )
        previous_bounds = observed_bounds.get(key, (timestamp_ms, timestamp_ms))
        observed_bounds[key] = (
            min(previous_bounds[0], timestamp_ms),
            max(previous_bounds[1], timestamp_ms),
        )

    coverage_index = (
        store.coverage_index()
        if any(
            venue in PNL_REQUIRED_DATASETS
            for group_contexts in contexts.values()
            for venue, _account_id in group_contexts
        )
        else None
    )

    output: list[dict[str, Any]] = []
    for key, (amount, count) in sorted(totals.items()):
        index = 0
        report_row: dict[str, Any] = {}
        if period != "none":
            report_row["period_start"] = key[index]
            index += 1
        for field in group_by:
            report_row[field] = key[index]
            index += 1
        report_row["amount"] = decimal_text(amount, default="0")
        report_row["events"] = count
        coverage_bounds = _coverage_bounds(
            period=period,
            period_start=report_row.get("period_start", ""),
            observed=observed_bounds[key],
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
        )
        coverage = _report_coverage(
            coverage_index,
            contexts=contexts[key],
            start_ms=coverage_bounds[0],
            end_ms=coverage_bounds[1],
        )
        if coverage:
            report_row["coverage_status"] = coverage["status"]
            report_row["coverage_gaps"] = json.dumps(
                coverage["details"], separators=(",", ":"), sort_keys=True
            )
        if key in other_breakdowns:
            report_row["other_event_breakdown"] = json.dumps(
                [
                    {
                        "event_subtype": subtype,
                        "events": count,
                        "amount": decimal_text(amount),
                    }
                    for subtype, (amount, count) in sorted(
                        other_breakdowns[key].items(),
                        key=lambda item: (
                            abs(Decimal(item[1][0])) * -1,
                            item[0],
                        ),
                    )
                ],
                separators=(",", ":"),
                sort_keys=True,
            )
        output.append(report_row)
    return output


def select_cashflows(
    store: DataStore,
    *,
    include_non_pnl: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
    venues: set[str] | None = None,
    symbols: set[str] | None = None,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for row in store.tables["cashflows"].iter_read():
        if row.get("reporting_role") != "primary":
            continue
        event_type = str(row.get("event_type") or "")
        if not include_non_pnl and (
            event_type in NON_PNL_EVENT_TYPES
            or event_type not in PNL_EVENT_TYPES
        ):
            continue
        timestamp_ms = int(row.get("event_time_ms") or 0)
        if start_ms is not None and timestamp_ms < start_ms:
            continue
        if end_ms is not None and timestamp_ms > end_ms:
            continue
        if venues and row.get("venue") not in venues:
            continue
        if symbols and row.get("symbol") not in symbols:
            continue
        selected.append(row)
    return selected


def _report_currency(
    row: dict[str, str], conversion: ReportConversion | None
) -> str:
    if conversion:
        return conversion.currency(row)
    return canonical_settlement_currency(row.get("currency"), row.get("symbol"))


def _period_start_ms(value: int, period: str) -> str:
    instant = datetime.fromtimestamp(value / 1000, tz=UTC)
    if period == "none":
        return ""
    if period == "hour":
        start = instant.replace(minute=0, second=0, microsecond=0)
    elif period == "day":
        start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        day = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day.fromordinal(day.toordinal() - day.weekday()).replace(tzinfo=UTC)
    elif period == "month":
        start = instant.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        start = instant.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unsupported period: {period}")
    return start.isoformat(timespec="seconds").replace("+00:00", "Z")


def _coverage_bounds(
    *,
    period: str,
    period_start: str,
    observed: tuple[int, int],
    requested_start_ms: int | None,
    requested_end_ms: int | None,
) -> tuple[int, int]:
    if period == "none":
        start_ms = requested_start_ms if requested_start_ms is not None else observed[0]
        end_ms = requested_end_ms if requested_end_ms is not None else observed[1]
        return start_ms, end_ms
    else:
        start = parse_datetime(period_start)
        end = _next_period_start(start, period) - timedelta(milliseconds=1)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
    if requested_start_ms is not None:
        start_ms = max(start_ms, requested_start_ms)
    if requested_end_ms is not None:
        end_ms = min(end_ms, requested_end_ms)
    return start_ms, end_ms


def _next_period_start(start: datetime, period: str) -> datetime:
    if period == "hour":
        return start + timedelta(hours=1)
    if period == "day":
        return start + timedelta(days=1)
    if period == "week":
        return start + timedelta(days=7)
    if period == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    if period == "year":
        return start.replace(year=start.year + 1)
    raise ValueError(f"unsupported coverage period: {period}")


def _report_coverage(
    coverage_index: CoverageIndex | None,
    *,
    contexts: set[tuple[str, str]],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any] | None:
    relevant = [context for context in sorted(contexts) if context[0] in PNL_REQUIRED_DATASETS]
    if not relevant or coverage_index is None:
        return None
    details: list[dict[str, Any]] = []
    statuses: list[str] = []
    for venue, account_id in relevant:
        for dataset in PNL_REQUIRED_DATASETS[venue]:
            assessment = coverage_index.assessment(
                venue=venue,
                account_id=account_id,
                dataset=dataset,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            statuses.append(str(assessment["status"]))
            details.append(
                {
                    "venue": venue,
                    "account_id": account_id,
                    "dataset": dataset,
                    "status": assessment["status"],
                    "gaps": assessment["gaps"],
                }
            )
    if all(status == "complete" for status in statuses):
        status = "complete"
    elif any(status == "incomplete" for status in statuses):
        status = "incomplete"
    else:
        status = "unknown"
    return {"status": status, "details": details}
