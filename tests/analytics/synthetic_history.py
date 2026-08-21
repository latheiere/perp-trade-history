from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from perp_trade_history.models import row_for
from perp_trade_history.storage import DataStore

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


def utc_ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


@dataclass
class SyntheticHistory:
    rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def add_execution(
        self,
        record_id: str,
        *,
        time_ms: int,
        action: str,
        quantity: str,
        price: str,
        symbol: str = "CONTRACT_A",
        side: str | None = None,
        position_side: str | None = None,
        quantity_unit: str = "base",
        venue: str = "venue_a",
        account_id: str = "account_a",
        order_id: str | None = None,
        trade_id: str | None = None,
        notional: str | None = None,
        base_quantity: str | None = None,
    ) -> dict[str, str]:
        direction = (
            "long"
            if action.endswith("long")
            else "short" if action.endswith("short") else ""
        )
        resolved_side = side or (
            "buy"
            if action in {"buy", "open_long", "close_short"}
            else "sell"
        )
        resolved_position_side = position_side
        if resolved_position_side is None:
            resolved_position_side = direction if direction else "both"
        row = row_for(
            "executions",
            record_id=record_id,
            venue=venue,
            account_id=account_id,
            market_type="perpetual",
            event_time=datetime.fromtimestamp(time_ms / 1000, tz=UTC).isoformat(),
            event_time_ms=time_ms,
            symbol=symbol,
            side=resolved_side,
            position_action=action,
            position_side=resolved_position_side,
            liquidity="maker",
            quantity=quantity,
            quantity_unit=quantity_unit,
            base_quantity=(
                base_quantity
                if base_quantity is not None
                else quantity if quantity_unit == "base" else ""
            ),
            price=price,
            notional=(
                notional
                if notional is not None
                else str(float(quantity) * float(price)) if quantity_unit == "base" else ""
            ),
            settlement_currency="CURRENCY_A",
            realized_pnl="0",
            fee="0",
            fee_currency="CURRENCY_A",
            order_id=order_id or f"order-{record_id}",
            trade_id=trade_id or f"trade-{record_id}",
            source="executions",
            source_id=record_id,
            source_updated_at_ms=time_ms,
            collected_at="collection",
        )
        self.rows.setdefault("executions", []).append(row)
        return row

    def add_cashflow(
        self,
        record_id: str,
        *,
        time_ms: int,
        event_type: str,
        amount: str,
        symbol: str = "CONTRACT_A",
        role: str = "primary",
        venue: str = "venue_a",
        account_id: str = "account_a",
        order_id: str = "",
        trade_id: str = "",
        currency: str = "CURRENCY_A",
    ) -> dict[str, str]:
        row = row_for(
            "cashflows",
            record_id=record_id,
            venue=venue,
            account_id=account_id,
            market_type="perpetual",
            event_time=datetime.fromtimestamp(time_ms / 1000, tz=UTC).isoformat(),
            event_time_ms=time_ms,
            event_type=event_type,
            event_subtype="synthetic",
            symbol=symbol,
            currency=currency,
            amount=amount,
            order_id=order_id,
            trade_id=trade_id,
            reporting_role=role,
            source="cashflows",
            source_id=record_id,
            source_updated_at_ms=time_ms,
            collected_at="collection",
        )
        self.rows.setdefault("cashflows", []).append(row)
        return row

    def add_complete_trade_coverage(
        self,
        *,
        start_ms: int,
        end_ms: int,
        venue: str = "venue_a",
        account_id: str = "account_a",
    ) -> None:
        self.rows.setdefault("coverage", []).append(
            row_for(
                "coverage",
                record_id=f"coverage-{venue}-{account_id}",
                venue=venue,
                account_id=account_id,
                market_type="perpetual",
                dataset="trades",
                source="synthetic",
                acquisition="fixture",
                scope="account",
                start_time_ms=start_ms,
                end_time_ms=end_ms,
                status="complete",
                limitation="Synthetic complete execution interval.",
                collected_at="collection",
            )
        )

    def add_trade_coverage(
        self,
        record_id: str,
        *,
        start_ms: int,
        end_ms: int,
        status: str,
        limitation: str = "",
        source: str = "synthetic",
        acquisition: str = "fixture",
        scope: str = "account",
        venue: str = "venue_a",
        account_id: str = "account_a",
        market_type: str = "perpetual",
        collected_at: str = "collection",
    ) -> None:
        self.rows.setdefault("coverage", []).append(
            row_for(
                "coverage",
                record_id=record_id,
                venue=venue,
                account_id=account_id,
                market_type=market_type,
                dataset="trades",
                source=source,
                acquisition=acquisition,
                scope=scope,
                start_time_ms=start_ms,
                end_time_ms=end_ms,
                status=status,
                limitation=limitation,
                collected_at=collected_at,
            )
        )

    def write(self, root: Path) -> DataStore:
        store = DataStore(root)
        for table, rows in self.rows.items():
            store.upsert(table, rows)
        return store


def multi_year_history() -> SyntheticHistory:
    history = SyntheticHistory()
    first_open = utc_ms(2020, 12, 31, 23)
    first_close = utc_ms(2021, 1, 2)
    second_open = utc_ms(2024, 2, 29)
    second_close = utc_ms(2026, 1, 1)
    history.add_execution(
        "execution-1", time_ms=first_open, action="open_long", quantity="2", price="10"
    )
    history.add_execution(
        "execution-2", time_ms=first_close, action="close_long", quantity="2", price="12"
    )
    history.add_execution(
        "execution-3",
        time_ms=second_open,
        action="open_short",
        quantity="3",
        price="15",
        symbol="CONTRACT_B",
    )
    history.add_execution(
        "execution-4",
        time_ms=second_close,
        action="close_short",
        quantity="3",
        price="11",
        symbol="CONTRACT_B",
    )
    history.add_complete_trade_coverage(
        start_ms=utc_ms(2020, 1, 1), end_ms=utc_ms(2026, 12, 31)
    )
    return history
