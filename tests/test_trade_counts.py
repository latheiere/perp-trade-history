from pathlib import Path

from perp_trade_history.models import row_for
from perp_trade_history.reporting import position_trade_counts
from perp_trade_history.storage import raw_record
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, utc_ms

START = utc_ms(2025, 1, 1)


def _execution(history, identity, hour, action, quantity="1", **kwargs):
    return history.add_execution(
        identity, time_ms=START + hour * HOUR_MS, action=action,
        quantity=quantity, price="10", **kwargs,
    )


def _position(identity, opened, closed, **kwargs):
    return row_for(
        "positions", record_id=identity, position_id=kwargs.pop("position_id", identity),
        venue="venue_a", account_id="account_a", market_type="perpetual",
        symbol="CONTRACT_A", position_side="long", status="closed" if closed else "open",
        opened_at_ms=opened, closed_at_ms=closed,
        source_updated_at_ms=closed or opened,
        **kwargs,
    )


def test_adds_partial_reductions_and_reopening_count_position_cycles(tmp_path: Path):
    history = SyntheticHistory()
    for index, (action, quantity) in enumerate([
        ("open_long", "2"), ("open_long", "3"), ("close_long", "1"),
        ("close_long", "4"), ("open_long", "1"), ("close_long", "1"),
    ]):
        _execution(history, str(index), index, action, quantity)
    assert position_trade_counts(history.write(tmp_path), period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 2,
    }


def test_open_positions_and_repeated_boundary_closes_each_count_once(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "partial-close-a", 0, "close_long", "2")
    _execution(history, "partial-close-b", 1, "close_long", "1")
    _execution(history, "partial-close-c", 2, "close_long", "3")
    _execution(history, "open", 0, "open_short", "5", symbol="CONTRACT_B")
    _execution(history, "add", 1, "open_short", "2", symbol="CONTRACT_B")
    _execution(history, "reduce", 2, "close_short", "1", symbol="CONTRACT_B")
    assert position_trade_counts(history.write(tmp_path), period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 1,
        ("all", "venue_a", "CONTRACT_B"): 1,
    }


def test_net_reversal_counts_closed_cycle_and_new_open_cycle(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "open", 0, "buy", "2")
    _execution(history, "reverse", 1, "sell", "5")
    assert position_trade_counts(history.write(tmp_path), period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 2,
    }


def test_summary_and_partial_execution_history_are_one_trade(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "close-a", 1, "close_long")
    _execution(history, "close-b", 2, "close_long")
    store = history.write(tmp_path)
    store.upsert("positions", [
        _position("summary", START, START + 2 * HOUR_MS),
        _position("same-position-source", START, START + 2 * HOUR_MS, position_id="summary"),
        _position("older-summary", START - 2 * HOUR_MS, START - HOUR_MS),
    ])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 2,
    }


def test_adjacent_summary_and_execution_cycles_remain_separate(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "open", 1, "open_long")
    _execution(history, "close", 2, "close_long")
    store = history.write(tmp_path)
    store.upsert("positions", [_position("summary", START, START + HOUR_MS)])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 2,
    }


def test_summaries_respect_account_and_direction_boundaries(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "long-open", 0, "open_long", account_id="account_b")
    _execution(history, "long-close", 1, "close_long", account_id="account_b")
    _execution(history, "short-open", 0, "open_short")
    _execution(history, "short-close", 1, "close_short")
    store = history.write(tmp_path)
    store.upsert("positions", [_position("summary", START, START + HOUR_MS)])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 3,
    }


def test_window_preserves_opening_history_and_counts_ongoing_boundary_trade(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "open", 0, "open_long", "2")
    _execution(history, "reduce", 2, "close_long")
    _execution(history, "close", 4, "close_long")
    store = history.write(tmp_path)
    assert position_trade_counts(
        store, period="hour", start_ms=START + HOUR_MS, end_ms=START + 3 * HOUR_MS,
    ) == {("2025-01-01T01:00:00Z", "venue_a", "CONTRACT_A"): 1}
    assert position_trade_counts(
        store, period="hour", start_ms=START + HOUR_MS,
    ) == {("2025-01-01T04:00:00Z", "venue_a", "CONTRACT_A"): 1}
    assert position_trade_counts(store, period="none", start_ms=START + 5 * HOUR_MS) == {}


def test_closed_summary_beyond_window_counts_as_ongoing_in_window(tmp_path: Path):
    store = SyntheticHistory().write(tmp_path)
    store.upsert("positions", [_position("summary", START, START + 4 * HOUR_MS)])
    assert position_trade_counts(
        store, period="hour", start_ms=START + HOUR_MS, end_ms=START + 3 * HOUR_MS,
    ) == {("2025-01-01T01:00:00Z", "venue_a", "CONTRACT_A"): 1}


def test_empty_position_snapshots_do_not_create_trades(tmp_path: Path):
    store = SyntheticHistory().write(tmp_path)
    store.upsert("positions", [
        row_for("positions", record_id="empty", venue="venue_a", status="closed"),
    ])
    assert position_trade_counts(store, period="none") == {}


def test_open_position_summary_without_fills_counts_once(tmp_path: Path):
    history = SyntheticHistory()
    history.add_cashflow("component", time_ms=START, event_type="funding", amount="-1")
    history.rows["cashflows"][0].update(event_subtype="position_summary", position_id="summary")
    store = history.write(tmp_path)
    store.upsert("positions", [_position("summary", START, 0)])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 1,
    }


def test_nonzero_snapshots_without_fills_count_once_even_without_open_time(tmp_path: Path):
    store = SyntheticHistory().write(tmp_path)
    store.upsert("positions", [
        _position("snapshot-a", 0, 0, quantity="2", collected_at="2025-01-01T00:00:00Z"),
        _position("snapshot-b", START + HOUR_MS, 0, quantity="1"),
    ])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 1,
    }
    assert position_trade_counts(store, period="none", end_ms=START - HOUR_MS) == {}


def test_snapshots_within_execution_cycle_do_not_add_trades(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "open", 0, "open_long", "2")
    _execution(history, "close", 2, "close_long", "2")
    store = history.write(tmp_path)
    store.upsert("positions", [_position("snapshot", START + HOUR_MS, 0, quantity="2")])
    assert position_trade_counts(store, period="none") == {
        ("all", "venue_a", "CONTRACT_A"): 1,
    }


def test_venue_and_symbol_filters_do_not_change_position_identity(tmp_path: Path):
    history = SyntheticHistory()
    for venue in ("venue_a", "venue_b"):
        _execution(history, venue, 0, "open_long", venue=venue)
    store = history.write(tmp_path)
    assert position_trade_counts(store, period="none", venues={"venue_b"}) == {
        ("all", "venue_b", "CONTRACT_A"): 1,
    }
    assert position_trade_counts(store, period="none", symbols={"CONTRACT_B"}) == {}


def test_native_position_identity_keeps_incomplete_add_reduce_cycles_together(tmp_path: Path):
    history = SyntheticHistory()
    for index, action in enumerate(["close_short", "open_short", "close_short", "close_short"]):
        _execution(history, str(index), index, action, venue="mexc", order_id=str(index))
    store = history.write(tmp_path)
    store.raw.upsert("mexc", "history_orders", [
        raw_record(
            venue="mexc", account_id="account_a", source="history_orders", source_id=str(index),
            payload={"orderId": index, "positionId": 9}, collected_at="collection",
        ) for index in range(4)
    ])
    assert position_trade_counts(store, period="none") == {("all", "mexc", "CONTRACT_A"): 1}


def test_native_position_identity_and_summary_do_not_duplicate_trade(tmp_path: Path):
    history = SyntheticHistory()
    _execution(history, "fill", 1, "close_long", venue="mexc", order_id="order")
    store = history.write(tmp_path)
    summary = _position("summary", START, START + 2 * HOUR_MS, position_id="9")
    summary["venue"] = "mexc"
    store.upsert("positions", [summary])
    store.raw.upsert("mexc", "history_orders", [raw_record(
        venue="mexc", account_id="account_a", source="history_orders", source_id="order",
        payload={"orderId": "order", "positionId": 9}, collected_at="collection",
    )])
    assert position_trade_counts(store, period="none") == {("all", "mexc", "CONTRACT_A"): 1}


def test_same_order_and_position_ids_in_different_accounts_remain_separate(tmp_path: Path):
    history = SyntheticHistory()
    for account in ("account_a", "account_b"):
        _execution(history, account, 0, "open_long", venue="mexc", account_id=account, order_id="1")
    store = history.write(tmp_path)
    store.raw.upsert("mexc", "history_orders", [
        raw_record(
            venue="mexc", account_id=account, source="history_orders", source_id="1",
            payload={"orderId": 1, "positionId": 9}, collected_at="collection",
        ) for account in ("account_a", "account_b")
    ])
    assert position_trade_counts(store, period="none") == {("all", "mexc", "CONTRACT_A"): 2}
