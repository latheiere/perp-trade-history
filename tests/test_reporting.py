from pathlib import Path
from decimal import Decimal

import pytest

from perp_trade_history.models import row_for
from perp_trade_history.reporting import pnl_report
from perp_trade_history.storage import DataStore


def _flow(
    record_id: str,
    *,
    time: str,
    time_ms: str,
    currency: str,
    amount: str,
    role: str = "primary",
    venue: str = "venue",
) -> dict[str, str]:
    return row_for(
        "cashflows",
        record_id=record_id,
        venue=venue,
        account_id="primary",
        market_type="perpetual",
        event_time=time,
        event_time_ms=time_ms,
        event_type="funding",
        symbol="ASSET_QUOTE",
        currency=currency,
        amount=amount,
        reporting_role=role,
        source="ledger",
        source_id=record_id,
        collected_at=time,
    )


def _custom_flow(
    record_id: str,
    *,
    time: str,
    time_ms: str,
    currency: str,
    amount: str,
    event_type: str,
    event_subtype: str | None = None,
    role: str = "primary",
    venue: str = "venue",
) -> dict[str, str]:
    row = _flow(
        record_id,
        time=time,
        time_ms=time_ms,
        currency=currency,
        amount=amount,
        role=role,
        venue=venue,
    )
    row["event_type"] = event_type
    if event_subtype is not None:
        row["event_subtype"] = event_subtype
    return row


def _coverage(
    record_id: str,
    *,
    dataset: str,
    status: str,
    start_ms: str,
    end_ms: str,
) -> dict[str, str]:
    return row_for(
        "coverage",
        record_id=record_id,
        venue="binance",
        account_id="primary",
        market_type="perpetual",
        dataset=dataset,
        source=f"source-{record_id}",
        acquisition="archive" if status == "complete" else "rest",
        scope="account",
        start_time="2026-01-01T00:00:00.000Z",
        start_time_ms=start_ms,
        end_time="2026-01-31T23:59:59.999Z",
        end_time_ms=end_ms,
        status=status,
        limitation="test coverage boundary",
        collected_at="2026-02-01T00:00:00.000Z",
    )


def test_report_groups_primary_cashflows_without_supplemental_double_count(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    store.upsert(
        "cashflows",
        [
            _flow(
                "one",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE",
                amount="1.25",
            ),
            _flow(
                "two",
                time="2026-01-20T00:00:00.000Z",
                time_ms="1768867200000",
                currency="QUOTE",
                amount="-0.25",
            ),
            _flow(
                "summary",
                time="2026-01-20T00:00:00.000Z",
                time_ms="1768867200000",
                currency="QUOTE",
                amount="9",
                role="supplemental",
            ),
        ],
    )
    rows = pnl_report(
        store,
        period="month",
        group_by=["venue", "event_type", "currency"],
    )
    assert rows == [
        {
            "period_start": "2026-01-01T00:00:00Z",
            "venue": "venue",
            "event_type": "funding",
            "currency": "QUOTE",
            "amount": "1",
            "events": 2,
        }
    ]


def test_report_refuses_to_mix_multiple_currencies(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    store.upsert(
        "cashflows",
        [
            _flow(
                "one",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE_A",
                amount="1",
            ),
            _flow(
                "two",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE_B",
                amount="1",
            ),
        ],
    )
    with pytest.raises(ValueError, match="currency must be grouped"):
        pnl_report(store, period="day", group_by=["venue"])


def test_report_excludes_non_pnl_cash_movements_unless_requested(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    funding = _flow(
        "funding",
        time="2026-01-02T00:00:00.000Z",
        time_ms="1767312000000",
        currency="QUOTE",
        amount="1",
    )
    transfer = {
        **_flow(
            "transfer",
            time="2026-01-02T00:00:00.000Z",
            time_ms="1767312000000",
            currency="QUOTE",
            amount="10",
        ),
        "event_type": "transfer",
    }
    store.upsert("cashflows", [funding, transfer])

    pnl_only = pnl_report(store, period="day", group_by=["currency"])
    with_movements = pnl_report(
        store,
        period="day",
        group_by=["currency"],
        include_non_pnl=True,
    )

    assert pnl_only[0]["amount"] == "1"
    assert with_movements[0]["amount"] == "11"


def test_report_excludes_unknown_event_types_from_pnl_total_by_default(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    store.upsert(
        "cashflows",
        [
            _custom_flow(
                "funding",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE",
                amount="1",
                event_type="funding",
            ),
            _custom_flow(
                "reward",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE",
                amount="2",
                event_type="reward",
            ),
            {
                **_custom_flow(
                    "other",
                    time="2026-01-02T00:00:00.000Z",
                    time_ms="1767312000000",
                    currency="QUOTE",
                    amount="50",
                    event_type="other",
                    event_subtype="BFUSD_REWARD",
                ),
                "event_type": "other",
            },
            _custom_flow(
                "transfer",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE",
                amount="10",
                event_type="transfer",
            ),
        ]
    )

    pnl_only = pnl_report(
        store,
        period="day",
        group_by=["event_type", "currency"],
    )
    assert len(pnl_only) == 2
    assert {row["event_type"] for row in pnl_only} == {"funding", "reward"}
    total = Decimal(pnl_only[0]["amount"]) + Decimal(pnl_only[1]["amount"])
    assert total == Decimal("3")

    with_non_pnl = pnl_report(
        store,
        period="day",
        group_by=["event_type", "currency"],
        include_non_pnl=True,
    )
    assert {row["event_type"] for row in with_non_pnl} == {
        "funding",
        "reward",
        "other",
        "transfer",
    }


def test_report_marks_period_incomplete_until_required_datasets_cover_it(
    tmp_path: Path,
) -> None:
    store = DataStore(tmp_path)
    store.upsert(
        "cashflows",
        [
            _flow(
                "flow",
                time="2026-01-02T00:00:00.000Z",
                time_ms="1767312000000",
                currency="QUOTE",
                amount="1",
                venue="binance",
            )
        ],
    )
    month_start = "1767225600000"
    month_end = "1769903999999"
    store.upsert(
        "coverage",
        [
            _coverage(
                "income",
                dataset="income",
                status="complete",
                start_ms=month_start,
                end_ms=month_end,
            ),
            _coverage(
                "trades-partial",
                dataset="trades",
                status="partial",
                start_ms=month_start,
                end_ms=month_end,
            ),
        ],
    )

    incomplete = pnl_report(
        store,
        period="month",
        group_by=["venue", "event_type", "currency"],
    )
    assert incomplete[0]["coverage_status"] == "incomplete"

    store.upsert(
        "coverage",
        [
            _coverage(
                "trades-archive",
                dataset="trades",
                status="complete",
                start_ms=month_start,
                end_ms=month_end,
            )
        ],
    )
    complete = pnl_report(
        store,
        period="month",
        group_by=["venue", "event_type", "currency"],
    )
    assert complete[0]["coverage_status"] == "complete"
