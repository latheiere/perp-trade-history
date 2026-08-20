from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from perp_trade_history.models import decimal_text, parse_datetime
from perp_trade_history.storage import DataStore

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


def pnl_report(
    store: DataStore,
    *,
    period: str,
    group_by: list[str],
    include_supplemental: bool = False,
    include_non_pnl: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
    venues: set[str] | None = None,
    symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    if period not in REPORT_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    unknown = set(group_by) - REPORT_FIELDS
    if unknown:
        raise ValueError(f"unsupported group fields: {sorted(unknown)}")

    rows = store.tables["cashflows"].read()
    selected: list[dict[str, str]] = []
    allowed_roles = {"primary", "supplemental"} if include_supplemental else {"primary"}
    for row in rows:
        if row.get("reporting_role") not in allowed_roles:
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

    currencies = {row.get("currency", "") for row in selected}
    if len(currencies) > 1 and "currency" not in group_by:
        raise ValueError("currency must be grouped when multiple settlement currencies are present")

    totals: dict[tuple[str, ...], tuple[Decimal, int]] = defaultdict(lambda: (Decimal(0), 0))
    contexts: dict[tuple[str, ...], set[tuple[str, str]]] = defaultdict(set)
    other_breakdowns: dict[tuple[str, ...], dict[str, tuple[Decimal, int]]] = defaultdict(
        dict
    )
    observed_bounds: dict[tuple[str, ...], tuple[int, int]] = {}
    for row in selected:
        period_start = _period_start(row["event_time"], period)
        key_parts = [period_start] if period != "none" else []
        key_parts.extend(row.get(field, "") for field in group_by)
        key = tuple(key_parts)
        amount, count = totals[key]
        totals[key] = amount + Decimal(row.get("amount") or "0"), count + 1
        contexts[key].add((row.get("venue", ""), row.get("account_id", "")))
        timestamp_ms = int(row.get("event_time_ms") or 0)
        if row.get("event_type") == "other":
            breakdown = other_breakdowns.setdefault(key, {})
            subtype = str(row.get("event_subtype") or "(missing)")
            other_amount, other_count = breakdown.get(subtype, (Decimal(0), 0))
            breakdown[subtype] = (
                other_amount + Decimal(row.get("amount") or "0"),
                other_count + 1,
            )
        previous_bounds = observed_bounds.get(key, (timestamp_ms, timestamp_ms))
        observed_bounds[key] = (
            min(previous_bounds[0], timestamp_ms),
            max(previous_bounds[1], timestamp_ms),
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
            store,
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


def _period_start(value: str, period: str) -> str:
    instant = parse_datetime(value).astimezone(UTC)
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
    store: DataStore,
    *,
    contexts: set[tuple[str, str]],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any] | None:
    relevant = [context for context in sorted(contexts) if context[0] in PNL_REQUIRED_DATASETS]
    if not relevant:
        return None
    details: list[dict[str, Any]] = []
    statuses: list[str] = []
    for venue, account_id in relevant:
        for dataset in PNL_REQUIRED_DATASETS[venue]:
            assessment = store.coverage_assessment(
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
