from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from perp_trade_history.config import load_config
from perp_trade_history.models import parse_datetime
from perp_trade_history.reporting import REPORT_PERIODS, pnl_report
from perp_trade_history.storage import DataStore

COMPONENTS = ("realized_pnl", "funding", "commission", "rebate", "reward", "settlement")
# Include explicitly-classified Binance income outcomes to avoid hiding them under OTHER.
COMPONENTS_EXTENDED = COMPONENTS + ("bonus", "premium", "insurance")
STATUS_RANK = {"complete": 0, "unknown": 1, "incomplete": 2}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print a cross-venue perpetual-account PnL report from normalized history."
    )
    parser.add_argument("--config", help="TOML configuration path")
    parser.add_argument("--secrets", help="Optional environment-style credential file")
    parser.add_argument("--period", choices=sorted(REPORT_PERIODS), default="none")
    parser.add_argument("--start", help="Inclusive UTC ISO start")
    parser.add_argument("--end", help="Inclusive UTC ISO end")
    parser.add_argument("--venue", action="append", choices=["binance", "gate", "mexc"])
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--include-supplemental", action="store_true")
    parser.add_argument("--include-non-pnl", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = load_config(args.config, secrets_path=args.secrets, require_credentials=False)
    selected_venues = set(args.venue) if args.venue else {
        venue.name for venue in config.enabled_venues
    }
    rows = pnl_report(
        DataStore(config.data_dir),
        period=args.period,
        group_by=["venue", "symbol", "event_type", "currency"],
        include_supplemental=args.include_supplemental,
        include_non_pnl=args.include_non_pnl,
        start_ms=_time_ms(args.start),
        end_ms=_time_ms(args.end),
        venues=selected_venues,
        symbols=set(args.symbol) if args.symbol else None,
    )
    print(render_text_report(rows, venues=selected_venues, period=args.period))


def render_text_report(
    rows: list[dict[str, Any]], *, venues: set[str], period: str
) -> str:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("period_start") or "all"),
            str(row.get("venue") or ""),
            str(row.get("symbol") or "(account)"),
            str(row.get("currency") or "(unspecified)"),
        )
        target = grouped.setdefault(
            key,
            {
                "amounts": defaultdict(Decimal),
                "events": 0,
                "coverage": "complete",
                "other_event_breakdown": [],
            },
        )
        event_type = str(row.get("event_type") or "other")
        component = event_type if event_type in COMPONENTS_EXTENDED else "other"
        target["amounts"][component] += Decimal(str(row.get("amount") or "0"))
        target["events"] += int(row.get("events") or 0)
        if event_type == "other" and row.get("other_event_breakdown"):
            target["other_event_breakdown"].append(row["other_event_breakdown"])
        elif event_type not in COMPONENTS_EXTENDED:
            target["other_event_breakdown"].append(
                json.dumps(
                    [
                        {
                            "event_subtype": event_type,
                            "events": int(row.get("events") or 0),
                            "amount": str(row.get("amount") or "0"),
                        }
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        status = str(row.get("coverage_status") or "unknown")
        if STATUS_RANK.get(status, 1) > STATUS_RANK.get(target["coverage"], 1):
            target["coverage"] = status

    headers = [
        "PERIOD",
        "VENUE",
        "SYMBOL",
        "CURRENCY",
        "REALIZED",
        "FUNDING",
        "COMMISSION",
        "REWARD",
        "SETTLEMENT",
        "REBATE",
        "BONUS",
        "PREMIUM",
        "INSURANCE",
        "OTHER",
        "TOTAL",
        "EVENTS",
        "COVERAGE",
    ]
    table_rows: list[list[str]] = []
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    represented: set[str] = set()
    ranked = sorted(
        grouped.items(),
        key=lambda item: (-sum(item[1]["amounts"].values(), Decimal(0)), item[0]),
    )
    for key, value in ranked:
        period_start, venue, symbol, currency = key
        represented.add(venue)
        amounts = value["amounts"]
        total = sum(amounts.values(), Decimal(0))
        totals[(venue, currency)] += total
        table_rows.append(
            [
                period_start,
                venue,
                symbol,
                currency,
                _number(amounts["realized_pnl"]),
                _number(amounts["funding"]),
                _number(amounts["commission"]),
                _number(amounts["reward"]),
                _number(amounts["settlement"]),
                _number(amounts["rebate"]),
                _number(amounts["bonus"]),
                _number(amounts["premium"]),
                _number(amounts["insurance"]),
                _number(amounts["other"]),
                _number(total),
                str(value["events"]),
                str(value["coverage"]),
            ]
        )

    lines = [
        "Perpetual account PnL report",
        f"Period grouping: {period}",
        "Basis: primary signed cashflows; transfers/conversions excluded by default.",
        "Realized PnL represents closed execution outcomes; coverage is never assumed.",
        "",
    ]
    if table_rows:
        lines.extend(_table(headers, table_rows))
    else:
        lines.append("No matching primary PnL cashflows are stored.")

    lines.extend(["", "Venue totals:"])
    if totals:
        total_rows = [
            [venue, currency, _number(amount)]
            for (venue, currency), amount in sorted(totals.items())
        ]
        lines.extend(_table(["VENUE", "CURRENCY", "TOTAL"], total_rows))
    else:
        lines.append("  none")
    missing = sorted(venues - represented)
    if missing:
        lines.extend(["", "No matching primary PnL cashflows: " + ", ".join(missing)])
    lines.extend(_build_other_breakdown_section(grouped))
    return "\n".join(lines)


def _build_other_breakdown_section(
    grouped: dict[tuple[str, str, str, str], dict[str, Any]]
) -> list[str]:
    rows: list[tuple[str, Decimal, int]] = []
    for value in grouped.values():
        raw_entries = value.get("other_event_breakdown") or value.get("other_breakdown")
        if not raw_entries:
            continue
        if isinstance(raw_entries, str):
            raw_entries = [raw_entries]
        for raw_entry in raw_entries:
            try:
                entries = json.loads(raw_entry)
            except (TypeError, json.JSONDecodeError):
                continue
            for entry in entries:
                subtype = str(entry.get("event_subtype") or "(missing)")
                amount = Decimal(str(entry.get("amount") or "0"))
                events = int(entry.get("events") or 0)
                if not subtype:
                    continue
                rows.append((subtype, amount, events))

    if not rows:
        return []

    merged: dict[str, tuple[Decimal, int]] = {}
    for subtype, amount, events in rows:
        total_amount, total_events = merged.get(subtype, (Decimal(0), 0))
        merged[subtype] = (
            total_amount + amount,
            total_events + events,
        )
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
