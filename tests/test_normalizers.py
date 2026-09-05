from perp_trade_history.adapters.binance import (
    normalize_binance_income,
    normalize_binance_order_amendment,
    normalize_binance_trade,
)
from perp_trade_history.adapters.gate import (
    normalize_gate_account_book,
    normalize_gate_position,
    normalize_gate_position_close,
    normalize_gate_trade,
)
from perp_trade_history.adapters.mexc import (
    normalize_mexc_deal,
    normalize_mexc_funding,
    normalize_mexc_position,
    normalize_mexc_stop_order,
    normalize_mexc_trigger_order,
)

COLLECTED_AT = "2026-01-02T00:00:00.000Z"


def test_binance_fill_produces_execution_and_separate_primary_cashflows() -> None:
    normalized = normalize_binance_trade(
        {
            "id": 7,
            "orderId": 9,
            "symbol": "ASSETUSDT",
            "time": 1_700_000_000_000,
            "side": "SELL",
            "positionSide": "LONG",
            "maker": False,
            "qty": "2",
            "baseQty": "2",
            "price": "10",
            "quoteQty": "20",
            "marginAsset": "USDT",
            "realizedPnl": "3.5",
            "commission": "0.2",
            "commissionAsset": "USDT",
        },
        account_id="primary",
        source="user_trades",
        collected_at=COLLECTED_AT,
    )
    execution = normalized.table_rows["executions"][0]
    assert execution["position_action"] == "close_long"
    assert execution["notional"] == "20"
    by_type = {
        row["event_type"]: row for row in normalized.table_rows["cashflows"]
    }
    assert by_type["realized_pnl"]["amount"] == "3.5"
    assert by_type["commission"]["amount"] == "-0.2"
    assert all(row["reporting_role"] == "primary" for row in by_type.values())


def test_binance_archive_fill_infers_longer_and_delivery_quote_suffixes() -> None:
    currencies: list[str] = []
    for index, symbol in enumerate(("ASSETBUSD", "ASSETUSDT_230331"), start=1):
        normalized = normalize_binance_trade(
            {
                "id": index,
                "symbol": symbol,
                "time": 1_700_000_000_000,
                "realizedPnl": "1",
                "commission": "0",
            },
            account_id="primary",
            source="archive_trades",
            collected_at=COLLECTED_AT,
        )
        currencies.append(normalized.table_rows["cashflows"][0]["currency"])
    assert currencies == ["BUSD", "USDT"]


def test_binance_income_keeps_fill_derived_types_informational() -> None:
    normalized = normalize_binance_income(
        {
            "incomeType": "COMMISSION",
            "tranId": 4,
            "time": 1_700_000_000_000,
            "symbol": "ASSETUSDT",
            "asset": "USDT",
            "income": "-0.1",
        },
        account_id="primary",
        source="income",
        collected_at=COLLECTED_AT,
    )
    assert normalized.table_rows["cashflows"][0]["reporting_role"] == "informational"


def test_binance_order_amendment_preserves_each_field_change() -> None:
    normalized = normalize_binance_order_amendment(
        {
            "amendmentId": 41,
            "symbol": "ASSETQUOTE",
            "orderId": 42,
            "clientOrderId": "client-order",
            "time": 1_700_000_000_000,
            "amendment": {
                "price": {"before": "10", "after": "11"},
                "origQty": {"before": "2", "after": "3"},
                "count": 2,
            },
        },
        account_id="primary",
        source="order_amendments",
        collected_at=COLLECTED_AT,
    )
    rows = normalized.table_rows["order_events"]
    assert {row["field"] for row in rows} == {"price", "origQty"}
    assert {row["sequence"] for row in rows} == {"2"}


def test_gate_account_book_is_primary_and_fill_is_execution_only() -> None:
    book = normalize_gate_account_book(
        {
            "id": "8",
            "time": "1700000000.5",
            "change": "-0.3",
            "type": "fund",
            "contract": "ASSET_USDT",
        },
        account_id="primary",
        source="usdt_account_book",
        collected_at=COLLECTED_AT,
        settlement="usdt",
    )
    assert book.table_rows["cashflows"][0]["event_type"] == "funding"
    assert book.table_rows["cashflows"][0]["reporting_role"] == "primary"

    trade = normalize_gate_trade(
        {
            "trade_id": "11",
            "create_time": "1700000001.25",
            "contract": "ASSET_USDT",
            "order_id": "12",
            "size": "-5",
            "close_size": "-5",
            "price": "20",
            "fee": "0.1",
            "role": "maker",
        },
        account_id="primary",
        source="usdt_trades",
        collected_at=COLLECTED_AT,
        settlement="usdt",
    )
    assert trade.table_rows["executions"][0]["position_action"] == "close_long"
    assert "cashflows" not in trade.table_rows


def test_gate_position_summary_is_supplemental() -> None:
    normalized = normalize_gate_position_close(
        {
            "time": 1_700_000_010,
            "first_open_time": 1_700_000_000,
            "contract": "ASSET_USDT",
            "side": "long",
            "pnl": "1.2",
            "pnl_pnl": "1.5",
            "pnl_fund": "-0.1",
            "pnl_fee": "-0.2",
            "max_size": "10",
            "accum_size": "10",
            "long_price": "10",
            "short_price": "11",
        },
        account_id="primary",
        source="usdt_position_close",
        collected_at=COLLECTED_AT,
        settlement="usdt",
    )
    assert normalized.table_rows["positions"][0]["open_price"] == "10"
    assert normalized.table_rows["cashflows"][0]["reporting_role"] == "supplemental"


def test_mexc_fill_and_funding_normalize_sign_and_position_action() -> None:
    deal = normalize_mexc_deal(
        {
            "id": 15,
            "orderId": 16,
            "symbol": "ASSET_USDT",
            "side": 2,
            "vol": "4",
            "price": "5",
            "fee": "0.04",
            "feeCurrency": "USDT",
            "profit": "2",
            "isTaker": True,
            "timestamp": 1_700_000_000_000,
        },
        account_id="primary",
        source="order_deals",
        collected_at=COLLECTED_AT,
    )
    assert deal.table_rows["executions"][0]["position_action"] == "close_short"
    assert {row["amount"] for row in deal.table_rows["cashflows"]} == {
        "2",
        "-0.04",
    }

    funding = normalize_mexc_funding(
        {
            "id": 17,
            "symbol": "ASSET_USDT",
            "funding": "-0.5",
            "rate": "0.001",
            "settleTime": 1_700_000_000_000,
        },
        account_id="primary",
        source="funding_records",
        collected_at=COLLECTED_AT,
    )
    assert funding.table_rows["cashflows"][0]["amount"] == "-0.5"


def test_mexc_fill_parses_string_boolean_liquidity() -> None:
    deal = normalize_mexc_deal(
        {
            "id": 19,
            "symbol": "ASSET_QUOTE",
            "side": 1,
            "vol": "1",
            "price": "5",
            "isTaker": "false",
            "timestamp": 1_700_000_000_000,
        },
        account_id="primary",
        source="order_deals",
        collected_at=COLLECTED_AT,
    )
    assert deal.table_rows["executions"][0]["liquidity"] == "maker"


def test_mexc_position_summary_preserves_legacy_pnl_as_supplemental() -> None:
    normalized = normalize_mexc_position(
        {
            "positionId": 18,
            "symbol": "ASSET_USDT",
            "positionType": 1,
            "state": 3,
            "closeVol": "3",
            "openAvgPrice": "10",
            "closeAvgPrice": "12",
            "realised": "5",
            "holdFee": "-0.2",
            "createTime": 1_700_000_000_000,
            "updateTime": 1_700_000_100_000,
        },
        account_id="primary",
        source="history_positions",
        collected_at=COLLECTED_AT,
    )
    assert normalized.table_rows["positions"][0]["status"] == "closed"
    assert {
        row["reporting_role"] for row in normalized.table_rows["cashflows"]
    } == {"supplemental"}


def test_gate_historical_position_snapshots_keep_distinct_source_identities() -> None:
    common = {
        "contract": "ASSET_QUOTE",
        "mode": "long",
        "size": "2",
        "entry_price": "10",
    }
    first = normalize_gate_position(
        {**common, "time": 1_700_000_000},
        account_id="primary",
        source="usdt_position_history",
        collected_at=COLLECTED_AT,
        settlement="usdt",
    )
    second = normalize_gate_position(
        {**common, "time": 1_700_000_100},
        account_id="primary",
        source="usdt_position_history",
        collected_at=COLLECTED_AT,
        settlement="usdt",
    )
    assert first.source_id != second.source_id
    assert (
        first.table_rows["positions"][0]["record_id"]
        != second.table_rows["positions"][0]["record_id"]
    )


def test_mexc_conditional_orders_preserve_trigger_and_linkage() -> None:
    trigger = normalize_mexc_trigger_order(
        {
            "id": 30,
            "orderId": 31,
            "symbol": "ASSET_QUOTE",
            "side": 4,
            "state": 3,
            "orderType": 1,
            "vol": "2",
            "triggerPrice": "12",
            "triggerType": 1,
            "trend": 2,
            "createTime": 1_700_000_000_000,
        },
        account_id="primary",
        source="trigger_orders",
        collected_at=COLLECTED_AT,
    ).table_rows["orders"][0]
    assert trigger["trigger_price"] == "12"
    assert trigger["linked_order_id"] == "31"

    stop = normalize_mexc_stop_order(
        {
            "id": 32,
            "placeOrderId": 33,
            "symbol": "ASSET_QUOTE",
            "positionType": 1,
            "state": 1,
            "vol": "2",
            "takeProfitPrice": "14",
            "stopLossPrice": "8",
            "createTime": 1_700_000_000_000,
        },
        account_id="primary",
        source="stop_orders",
        collected_at=COLLECTED_AT,
    ).table_rows["orders"]
    assert {row["order_type"] for row in stop} == {"take_profit", "stop_loss"}
    assert all(row["linked_order_id"] == "33" for row in stop)
