from pathlib import Path

from perp_trade_history.models import row_for
from perp_trade_history.storage import DataStore, raw_record


def _cashflow(record_id: str, amount: str, collected_at: str) -> dict[str, str]:
    return row_for(
        "cashflows",
        record_id=record_id,
        venue="venue",
        account_id="primary",
        market_type="perpetual",
        event_time="2026-01-01T00:00:00.000Z",
        event_time_ms="1767225600000",
        event_type="funding",
        event_subtype="funding_settlement",
        currency="QUOTE",
        amount=amount,
        reporting_role="primary",
        source="funding",
        source_id=record_id,
        collected_at=collected_at,
    )


def test_csv_upsert_extends_history_and_overwrites_updated_source_rows(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    first = store.upsert(
        "cashflows",
        [_cashflow("one", "1", "2026-01-01T00:00:00.000Z")],
    )
    assert first.to_dict() == {"inserted": 1, "updated": 0, "unchanged": 0, "total": 1}

    second = store.upsert(
        "cashflows",
        [
            _cashflow("one", "1", "2026-01-02T00:00:00.000Z"),
            _cashflow("two", "2", "2026-01-02T00:00:00.000Z"),
        ],
    )
    assert second.inserted == 1
    assert second.unchanged == 1
    assert len(store.tables["cashflows"].read()) == 2

    third = store.upsert(
        "cashflows",
        [_cashflow("one", "1.5", "2026-01-03T00:00:00.000Z")],
    )
    assert third.updated == 1
    by_id = {row["record_id"]: row for row in store.tables["cashflows"].read()}
    assert by_id["one"]["amount"] == "1.5"
    assert "two" in by_id


def test_raw_archive_is_idempotent_and_preserves_unmentioned_rows(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    one = raw_record(
        venue="venue",
        account_id="primary",
        source="fills",
        source_id="one",
        payload={"value": 1},
        collected_at="first",
    )
    two = raw_record(
        venue="venue",
        account_id="primary",
        source="fills",
        source_id="two",
        payload={"value": 2},
        collected_at="first",
    )
    store.raw.upsert("venue", "fills", [one, two])
    one_new = {**one, "payload": {"value": 3}, "collected_at": "second"}
    stats = store.raw.upsert("venue", "fills", [one_new])
    assert stats.updated == 1
    rows = {row["source_id"]: row for row in store.raw.read("venue", "fills")}
    assert rows["one"]["payload"] == {"value": 3}
    assert rows["two"]["payload"] == {"value": 2}


def test_historical_symbols_exclude_closed_point_snapshots(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    closed = row_for(
        "positions",
        record_id="closed-position",
        venue="venue",
        account_id="primary",
        market_type="perpetual",
        symbol="CLOSEDQUOTE",
        status="closed",
        source="position_snapshot",
        source_id="closed-position",
        collected_at="2026-01-01T00:00:00.000Z",
    )
    opened = {
        **closed,
        "record_id": "open-position",
        "symbol": "OPENQUOTE",
        "status": "open",
    }
    store.upsert("positions", [closed, opened])
    store.upsert(
        "cashflows",
        [
            {
                **_cashflow("activity", "1", "2026-01-01T00:00:00.000Z"),
                "symbol": "HISTORYQUOTE",
            }
        ],
    )

    assert store.historical_symbols("venue", "primary") == {"OPENQUOTE", "HISTORYQUOTE"}


def test_sync_state_uses_overlap_after_completed_backfill(tmp_path: Path) -> None:
    state = DataStore(tmp_path).state
    initial = 1_000
    assert (
        state.start_for(
            venue="venue",
            source="fills",
            initial_start_ms=initial,
            overlap_ms=100,
            full=False,
        )
        == initial
    )
    state.record_success(
        venue="venue",
        source="fills",
        requested_start_ms=initial,
        covered_through_ms=10_000,
        oldest_ms=2_000,
        newest_ms=9_000,
        records=2,
        backfill_complete=True,
    )
    assert (
        state.start_for(
            venue="venue",
            source="fills",
            initial_start_ms=initial,
            overlap_ms=100,
            full=False,
        )
        == 9_900
    )
