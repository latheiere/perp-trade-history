from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from perp_trade_history.config import load_config
from perp_trade_history.conversion import ConversionError, StoredCashflowConversion
from perp_trade_history.models import compact_base_symbol, parse_datetime
from perp_trade_history.reporting import REPORT_PERIODS, pnl_report, position_trade_counts
from perp_trade_history.storage import DataStore

BASE_COMPONENTS = ("realized_pnl", "funding", "commission")
EXTRA_COMPONENTS = ("reward", "settlement", "rebate", "bonus", "premium", "insurance", "other")
COMPONENTS = frozenset(BASE_COMPONENTS + EXTRA_COMPONENTS)
STATUS_RANK = {"complete": 0, "unknown": 1, "incomplete": 2}
ZERO = Decimal(0)

ReportView = Literal["venue", "symbol", "contract"]
VIEW_LABELS: dict[ReportView, str] = {
    "venue": "base symbol within each venue",
    "symbol": "base symbol across selected venues",
    "contract": "full contract symbol within each venue",
}


@dataclass
class _ReportRow:
    amounts: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    events: int = 0
    trades: int = 0
    coverage: str = "complete"

    def include_coverage(self, status: str) -> None:
        if STATUS_RANK.get(status, 1) > STATUS_RANK[self.coverage]:
            self.coverage = status if status in STATUS_RANK else "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print perpetual account PnL and trade counts from stored history."
    )
    display = parser.add_argument_group("Display")
    mode = display.add_mutually_exclusive_group()
    mode.add_argument(
        "--view",
        choices=VIEW_LABELS,
        help=f"venue: {VIEW_LABELS['venue']} (default); "
        f"symbol: {VIEW_LABELS['symbol']}, hiding venue names; "
        f"contract: {VIEW_LABELS['contract']}, with a currency column",
    )
    mode.add_argument(
        "--compact", dest="view", action="store_const", const="venue", help=argparse.SUPPRESS,
    )
    mode.add_argument(
        "--verbose", dest="view", action="store_const", const="contract", help=argparse.SUPPRESS,
    )
    parser.set_defaults(view="venue")
    display.add_argument(
        "--period", choices=sorted(REPORT_PERIODS), default="none",
        help="Split rows by UTC calendar period (default: no time buckets)",
    )
    display.add_argument(
        "--extra-columns",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show or hide REWARD through OTHER; TOTAL includes their amounts either way",
    )
    filters = parser.add_argument_group("Filters")
    filters.add_argument("--start", help="First included time (ISO 8601, UTC by default)")
    filters.add_argument("--end", help="Last included time (ISO 8601, UTC by default)")
    filters.add_argument(
        "--venue", action="append", choices=["binance", "gate", "mexc"],
        help="Select a venue before grouping; repeat to select several (default: enabled venues)",
    )
    filters.add_argument(
        "--symbol", action="append",
        help="Match an exact contract symbol before grouping; repeat to select several",
    )
    filters.add_argument(
        "--include-non-pnl", action="store_true",
        help="Include transfers, conversions, and other non-PnL cashflows in TOTAL",
    )
    parser.add_argument("--config", help="Path to the TOML configuration")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config, require_credentials=False)
    selected_venues = set(args.venue) if args.venue else {
        venue.name for venue in config.enabled_venues
    }
    filters = dict(
        period=args.period,
        start_ms=_time_ms(args.start),
        end_ms=_time_ms(args.end),
        venues=selected_venues,
        symbols=set(args.symbol) if args.symbol else None,
    )
    store = DataStore(config.data_dir)
    try:
        rows = pnl_report(
            store,
            group_by=["venue", "symbol", "event_type"],
            include_non_pnl=args.include_non_pnl,
            conversion=StoredCashflowConversion(
                store,
                target_currency=config.reporting.target_currency,
                method=config.reporting.conversion_method,
                fixed_rates=config.reporting.fixed_rates,
            ),
            **filters,
        )
    except ConversionError as exc:
        parser.error(str(exc))
    print(
        render_text_report(
            rows,
            venues=selected_venues,
            period=args.period,
            view=args.view,
            target_currency=config.reporting.target_currency,
            conversion_method=config.reporting.conversion_method,
            trade_counts=position_trade_counts(store, **filters),
            extra_columns=args.extra_columns,
            include_non_pnl=args.include_non_pnl,
        )
    )


def render_text_report(
    rows: list[dict[str, Any]],
    *,
    venues: set[str],
    period: str,
    view: ReportView = "venue",
    target_currency: str = "USDT",
    conversion_method: str = "previous_day.close",
    trade_counts: dict[tuple[str, str, str], int] | None = None,
    extra_columns: bool = True,
    include_non_pnl: bool = False,
) -> str:
    """Render cashflows already converted to the reporting currency."""
    merge_venues = view == "symbol"
    full_contract = view == "contract"
    grouped: dict[tuple[str, str, str], _ReportRow] = defaultdict(_ReportRow)
    cashflow_groups: set[tuple[str, str, str]] = set()
    represented: set[str] = set()
    other_breakdown: dict[str, tuple[Decimal, int]] = defaultdict(lambda: (ZERO, 0))
    for row in rows:
        bucket = str(row.get("period_start") or "all")
        venue = str(row.get("venue") or "")
        symbol = _display_symbol(str(row.get("symbol") or ""), full_contract=full_contract)
        represented.add(venue)
        if merge_venues:
            cashflow_groups.add((bucket, venue, symbol))
        target = grouped[bucket, "" if merge_venues else venue, symbol]
        event_type = str(row.get("event_type") or "other")
        component = event_type if event_type in COMPONENTS else "other"
        amount = Decimal(str(row.get("amount") or "0"))
        events = int(row.get("events") or 0)
        target.amounts[component] += amount
        target.events += events
        target.include_coverage(str(row.get("coverage_status") or "unknown"))
        if component == "other":
            raw_breakdown = row.get("other_event_breakdown") if event_type == "other" else None
            entries = (
                _other_entries(raw_breakdown) if raw_breakdown else ((event_type, amount, events),)
            )
            for subtype, subtype_amount, subtype_events in entries:
                total_amount, total_events = other_breakdown[subtype]
                other_breakdown[subtype] = (
                    total_amount + subtype_amount, total_events + subtype_events
                )

    for (bucket, venue, symbol), count in (trade_counts or {}).items():
        display_symbol = _display_symbol(symbol, full_contract=full_contract)
        represented.add(venue)
        source_key = (bucket, venue, display_symbol)
        key = (bucket, "" if merge_venues else venue, display_symbol)
        # Trade-only venue rows contribute unknown coverage even in a combined row.
        if key not in grouped or (merge_venues and source_key not in cashflow_groups):
            grouped[key].include_coverage("unknown")
        grouped[key].trades += count

    def dimensions(bucket: str, venue: str, symbol: str, currency: str) -> list[str]:
        result = [bucket, symbol] if merge_venues else [bucket, venue, symbol]
        if full_contract:
            result.append(currency)
        return result

    columns = BASE_COMPONENTS + EXTRA_COMPONENTS if extra_columns else BASE_COMPONENTS
    headers = dimensions("PERIOD", "VENUE", "SYMBOL", "CURRENCY")
    headers.extend("REALIZED" if name == "realized_pnl" else name.upper() for name in columns)
    headers.extend(["TOTAL", "TRADES", "EVENTS", "COVERAGE"])
    table_rows: list[list[str]] = []
    totals: dict[str, Decimal] = defaultdict(Decimal)
    ranked = [(key, row, sum(row.amounts.values(), ZERO)) for key, row in grouped.items()]
    ranked.sort(key=lambda item: (-item[2], item[0]))
    for (bucket, venue, symbol), row, total in ranked:
        totals[venue] += total
        table_row = dimensions(bucket, venue, symbol, target_currency)
        table_row.extend(_number(row.amounts.get(name, ZERO)) for name in columns)
        table_row.extend([_number(total), str(row.trades), str(row.events), row.coverage])
        table_rows.append(table_row)

    time_grouping = "no time buckets" if period == "none" else f"UTC {period} buckets"
    cashflow_scope = (
        "includes non-PnL cashflows" if include_non_pnl else "PnL cashflows only"
    )
    price = conversion_method.replace("_", " ").replace(".", " ")
    lines = [
        "Perpetual account PnL report",
        f"Rows: {VIEW_LABELS[view]}; {time_grouping}.",
        f"Amounts in {target_currency} ({price} conversion); {cashflow_scope}.",
        "TRADES counts observed position cycles, including open and boundary-truncated cycles. "
        "Adds and partial reductions stay within one trade.",
        "Position summaries use source update time. COVERAGE describes available history.",
    ]
    if not extra_columns:
        lines.append("TOTAL includes cashflows from hidden columns.")
    lines.append("")
    if table_rows:
        lines.extend(_table(headers, table_rows))
        if merge_venues:
            lines.extend(["", f"Total ({target_currency}): {_number(totals[''])}"])
        else:
            lines.extend(["", "Venue totals:"])
            total_rows = [[venue, _number(amount)] for venue, amount in sorted(totals.items())]
            lines.extend(_table(["VENUE", f"TOTAL ({target_currency})"], total_rows))
        if missing := venues - represented:
            missing_label = (
                f"{len(missing)} of {len(venues)} selected venues"
                if merge_venues else ", ".join(sorted(missing))
            )
            lines.extend(["", f"No matching cashflows or trades: {missing_label}"])
    else:
        lines.append("No matching cashflows or trades are stored.")
    lines.extend(_build_other_breakdown_section(other_breakdown))
    return "\n".join(lines)


def _display_symbol(symbol: str, *, full_contract: bool) -> str:
    if symbol.casefold() == "(account)":
        return "(account)"
    return (symbol if full_contract else compact_base_symbol(symbol)) or "(account)"


def _other_entries(raw: str) -> Iterator[tuple[str, Decimal, int]]:
    try:
        entries = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return
    for entry in entries:
        yield (
            str(entry.get("event_subtype") or "(missing)"),
            Decimal(str(entry.get("amount") or "0")),
            int(entry.get("events") or 0),
        )


def _build_other_breakdown_section(
    merged: dict[str, tuple[Decimal, int]],
) -> list[str]:
    if not merged:
        return []
    rendered_rows: list[list[str]] = [
        [subtype, _number(amount), str(events)]
        for subtype, (amount, events) in sorted(
            merged.items(),
            key=lambda item: (abs(item[1][0]) * -1, item[0]),
        )
    ]

    return [
        "",
        "OTHER breakdown by original subtype:",
        *_table(["SUBTYPE", "AMOUNT", "EVENTS"], rendered_rows),
    ]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    rendered = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))]
    rendered.append("  ".join("-" * width for width in widths))
    rendered.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
    return rendered


def _number(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "0" if rounded == 0 else format(rounded, ".2f")


def _time_ms(value: str | None) -> int | None:
    return int(parse_datetime(value).timestamp() * 1000) if value else None


if __name__ == "__main__":
    main()
