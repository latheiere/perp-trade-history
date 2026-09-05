from decimal import Decimal
from pathlib import Path

import pytest

from perp_trade_history.adapters.mexc import (
    MexcAdapter,
    normalize_mexc_deal,
    normalize_mexc_funding,
    normalize_mexc_position,
    resolve_mexc_cashflows,
)
from perp_trade_history.cli import build_parser as cli_parser
from perp_trade_history.reporting import select_cashflows
from perp_trade_history.storage import DataStore, raw_record
from perp_trade_history.text_report import build_parser as text_parser

START = 1_700_000_000_000
COLLECTED = "2026-01-01T00:00:00.000Z"


def _save(store, normalizer, source, payload, account="primary"):
    result = normalizer(payload, account_id=account, source=source, collected_at=COLLECTED)
    raw = raw_record(
        venue="mexc", account_id=account, source=source, source_id=result.source_id,
        payload=payload, collected_at=COLLECTED,
    )
    store.raw.upsert("mexc", source, [raw])
    for table, rows in result.table_rows.items():
        for row in rows:
            row["raw_ref"] = raw["raw_id"]
        store.upsert(table, rows)


def _position(**updates):
    return {
        "positionId": 1, "symbol": "ASSET_USDT", "positionType": 1, "state": 3,
        "createTime": START, "updateTime": START + 100_000,
        "realised": "7", "closeProfitLoss": "10", "holdFee": "-2", "fee": "-1",
        **updates,
    }


def _deal(**updates):
    return {
        "id": 2, "orderId": 3, "symbol": "ASSET_USDT", "side": 4,
        "timestamp": START + 50_000, "profit": "4", "fee": "0.4",
        **updates,
    }


def _total(store):
    return sum((Decimal(row["amount"]) for row in select_cashflows(store)), Decimal(0))


def test_position_total_replaces_partial_details_without_losing_uncovered_history(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    _save(store, normalize_mexc_deal, "order_deals", _deal(id=4, timestamp=START - 1))
    _save(store, normalize_mexc_position, "history_positions", _position())
    for identity, side in ((5, 1), (6, 2)):
        _save(store, normalize_mexc_funding, "funding_records", {
            "id": identity, "symbol": "ASSET_USDT", "positionType": side,
            "settleTime": START + 50_000, "funding": "-0.5",
        })
    resolve_mexc_cashflows(store, "primary")
    # Net position result + uncovered fill - opposite-side funding.
    assert _total(store) == Decimal("10.1")
    rows = store.tables["cashflows"].read()
    assert sum(row["reporting_role"] == "informational" for row in rows) == 3
    resolve_mexc_cashflows(store, "primary")
    assert store.tables["cashflows"].read() == rows
    # Recollection must not promote overlapping observations permanently.
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    resolve_mexc_cashflows(store, "primary")
    assert _total(store) == Decimal("10.1")


def test_summary_revision_can_zero_all_components_and_keep_same_identity(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_position, "history_positions", _position())
    identifiers = {r["record_id"] for r in store.tables["cashflows"].read()}
    _save(store, normalize_mexc_position, "history_positions", _position(
        realised="0", closeProfitLoss="0", holdFee="0", fee="0",
    ))
    resolve_mexc_cashflows(store, "primary")
    assert {r["record_id"] for r in store.tables["cashflows"].read()} == identifiers
    assert _total(store) == 0


def test_position_net_preserves_deductions_and_rounding(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_position, "history_positions", _position(realised="6.8"))
    amounts = {r["event_type"]: r["amount"] for r in select_cashflows(store)}
    assert amounts == {"realized_pnl": "10", "funding": "-2", "commission": "-1.2"}
    assert _total(store) == Decimal("6.8")


def test_summary_arriving_after_detail_and_stale_snapshot_bounds(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_deal, "order_deals", _deal(positionId=1))
    resolve_mexc_cashflows(store, "primary")
    assert _total(store) == Decimal("3.6")
    _save(store, normalize_mexc_position, "open_positions", _position(
        state=1, updateTime=START + 40_000,
    ))
    resolve_mexc_cashflows(store, "primary")
    assert _total(store) == Decimal("10.6")
    _save(store, normalize_mexc_position, "history_positions", _position())
    resolve_mexc_cashflows(store, "primary")
    assert _total(store) == Decimal("7")


def test_order_position_identity_prevents_temporal_false_match(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_position, "history_positions", _position())
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    store.raw.upsert("mexc", "history_orders", [raw_record(
        venue="mexc", account_id="primary", source="history_orders", source_id="3",
        payload={"orderId": 3, "positionId": 99}, collected_at=COLLECTED,
    )])
    resolve_mexc_cashflows(store, "primary")
    assert _total(store) == Decimal("10.6")


def test_account_scope_and_missing_summary_amount_preserve_details(tmp_path: Path):
    store = DataStore(tmp_path)
    _save(store, normalize_mexc_position, "history_positions", _position(), account="other")
    _save(store, normalize_mexc_position, "history_positions", _position(realised=None))
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    resolve_mexc_cashflows(store, "primary")
    assert all(r["reporting_role"] == "primary" for r in select_cashflows(store))
    assert _total(store) == Decimal("10.6")


def test_ambiguous_position_membership_is_not_silently_deduplicated(tmp_path: Path):
    store = DataStore(tmp_path)
    for identity in (1, 2):
        _save(store, normalize_mexc_position, "history_positions", _position(positionId=identity))
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    with pytest.raises(ValueError, match="Ambiguous MEXC position membership"):
        resolve_mexc_cashflows(store, "primary")


@pytest.mark.parametrize("flag", ["--include-supplemental", "--supplemental-venue"])
def test_report_interfaces_reject_removed_accounting_overrides(flag):
    for parser, prefix in ((text_parser(), []), (cli_parser(), ["report"])):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([*prefix, flag])
        assert exc.value.code == 2


def test_adapter_finalization_uses_retained_history(tmp_path: Path):
    from types import SimpleNamespace

    store = DataStore(tmp_path)
    _save(store, normalize_mexc_position, "history_positions", _position())
    _save(store, normalize_mexc_deal, "order_deals", _deal())
    adapter = MexcAdapter.__new__(MexcAdapter)
    adapter.store = store
    adapter.venue = SimpleNamespace(account_id="primary")
    adapter.finalize_cashflows()
    assert _total(store) == Decimal("7")
