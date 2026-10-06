from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from perp_trade_history.models import TABLE_FIELDS, row_for


def demonstration_tables() -> dict[str, list[dict[str, str]]]:
    """Supply deterministic canonical records without a second report algorithm."""
    tables: dict[str, list[dict[str, str]]] = {name: [] for name in TABLE_FIELDS}
    first = datetime(2021, 1, 1, tzinfo=UTC)
    durations = (timedelta(minutes=30), timedelta(hours=12), timedelta(days=3),
                 timedelta(days=12), timedelta(days=45))
    last = first + timedelta(days=239 * 8 + 60)
    gap_start = first + timedelta(days=42 * 8)
    gap_end = gap_start + timedelta(days=1)

    def append(table: str, record_id: str, venue: str, at: datetime, **values: object) -> None:
        common = dict(
            record_id=record_id, venue=venue, account_id="demo-account",
            market_type="perpetual", event_time=at.isoformat(),
            event_time_ms=int(at.timestamp() * 1000), source="demonstration",
            source_id=record_id, collected_at="demonstration",
        )
        tables[table].append(row_for(
            table, **{name: value for name, value in common.items() if name in TABLE_FIELDS[table]},
            **values,
        ))

    for index in range(240):
        venue = "gate" if index % 3 == 0 else "binance"
        opened = first + timedelta(days=index * 8, hours=index % 24)
        closed = opened + durations[index % len(durations)]
        direction = "short" if index % 2 else "long"
        symbol = f"DEMO_CONTRACT_{index % 5}"
        is_open = index >= 228
        flat = index % 37 == 0
        result = Decimal(0) if flat else Decimal((-300, 400, -700, 100, 250)[index % 5])
        if opened.year == 2022 and not flat:
            result = -abs(result)
        entry = Decimal(1000 + index % 11)
        exit_price = entry + result / (10 if direction == "long" else -10)
        events = [("open", opened, entry)]
        if not is_open:
            events.append(("close", closed, exit_price))
        for action, at, price in events:
            key = f"demo-{index}-{action}"
            append("executions", key, venue, at, symbol=symbol,
                   position_action=f"{action}_{direction}", position_side=direction,
                   side="buy" if (direction == "long") == (action == "open") else "sell",
                   quantity="10", quantity_unit="base", base_quantity="10",
                   price=str(price), notional=str(price * 10), settlement_currency="USDT",
                   fee="0" if flat else "1", fee_currency="USDT",
                   trade_id=key, order_id=key, source_updated_at_ms=int(at.timestamp() * 1000))
            append("cashflows", f"fee-{key}", venue, at, symbol=symbol,
                   event_type="commission", amount="0" if flat else "-1", currency="USDT",
                   reporting_role="primary", trade_id=key)
        if not is_open:
            append("cashflows", f"result-{index}", venue, closed, symbol=symbol,
                   event_type="realized_pnl", amount=str(result), currency="USDT",
                   reporting_role="primary", trade_id=f"demo-{index}-close")

    for venue in ("binance", "gate"):
        intervals = [(first, last, "complete")]
        if venue == "gate":
            intervals = [(first, gap_start - timedelta(milliseconds=1), "complete"),
                         (gap_start, gap_end, "partial"),
                         (gap_end + timedelta(milliseconds=1), last, "complete")]
        for index, (start, end, status) in enumerate(intervals):
            append("coverage", f"coverage-{venue}-{index}", venue, start,
                   dataset="trades", acquisition="demonstration", scope="account",
                   start_time_ms=int(start.timestamp() * 1000),
                   end_time_ms=int(end.timestamp() * 1000), status=status,
                   limitation="Demonstration source interval has incomplete coverage."
                   if status == "partial" else "")
        append("account_snapshots", f"snapshot-{venue}", venue, last,
               observed_at_ms=int(last.timestamp() * 1000))
    return tables
