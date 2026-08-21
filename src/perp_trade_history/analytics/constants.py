from __future__ import annotations

TRADING_CASHFLOW_TYPES = frozenset(
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

CAPITAL_MOVEMENT_TYPES = frozenset({"transfer", "conversion"})

EXPLICIT_ACTIONS = frozenset(
    {"open_long", "close_long", "open_short", "close_short"}
)
NET_ACTIONS = frozenset({"buy", "sell"})
KNOWN_ACTIONS = EXPLICIT_ACTIONS | NET_ACTIONS

TABLE_TIME_FIELDS: dict[str, tuple[str, ...]] = {
    "cashflows": ("event_time_ms",),
    "executions": ("event_time_ms",),
    "orders": ("created_at_ms", "updated_at_ms"),
    "order_events": ("event_time_ms",),
    "positions": ("opened_at_ms", "closed_at_ms", "source_updated_at_ms"),
    "account_snapshots": ("observed_at_ms",),
    "coverage": ("start_time_ms", "end_time_ms"),
    "conversion_rates": ("open_time_ms", "close_time_ms"),
}
