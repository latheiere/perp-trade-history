from decimal import Decimal
from pathlib import Path

from perp_trade_history.analytics import build_snapshot
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, utc_ms


class FixedConversion:
    def prepare(self, rows: list[dict[str, str]]) -> None:
        self.prepared = len(rows)

    def amount(self, row: dict[str, str]) -> Decimal:
        return Decimal(row["amount"]) * 2

    def currency(self, row: dict[str, str]) -> str:
        return "REPORTING_CURRENCY"


def test_primary_execution_cashflows_use_exact_linkage_without_double_counting(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1",
        time_ms=start,
        action="open_long",
        quantity="1",
        price="10",
        trade_id="trade-1",
    )
    history.add_execution(
        "execution-2",
        time_ms=start + HOUR_MS,
        action="close_long",
        quantity="1",
        price="12",
        trade_id="trade-2",
    )
    history.add_cashflow(
        "cashflow-primary",
        time_ms=start + HOUR_MS,
        event_type="realized_pnl",
        amount="2",
        trade_id="trade-2",
    )
    history.add_cashflow(
        "cashflow-informational",
        time_ms=start + HOUR_MS,
        event_type="realized_pnl",
        amount="2",
        trade_id="trade-2",
        role="informational",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    primary = next(
        item
        for item in snapshot.cashflow_attributions
        if item.cashflow_record_id == "cashflow-primary"
    )
    duplicate = next(
        item
        for item in snapshot.cashflow_attributions
        if item.cashflow_record_id == "cashflow-informational"
    )
    assert primary.method == "exact_trade"
    assert primary.episode_id
    assert duplicate.method == "excluded_role"
    assert not duplicate.episode_id
    assert snapshot.episode_cashflows[0].amount == "2"


def test_symbol_cashflow_requires_one_active_episode(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-long", time_ms=start, action="open_long", quantity="1", price="10"
    )
    history.add_execution(
        "execution-short", time_ms=start, action="open_short", quantity="1", price="10"
    )
    history.add_cashflow(
        "cashflow-funding",
        time_ms=start + HOUR_MS,
        event_type="funding",
        amount="-1",
    )

    snapshot = build_snapshot(history.write(tmp_path), as_of_ms=start + 2 * HOUR_MS)

    attribution = next(
        item
        for item in snapshot.cashflow_attributions
        if item.cashflow_record_id == "cashflow-funding"
    )
    assert attribution.method == "ambiguous_temporal"
    assert not attribution.episode_id
    summary = next(
        item for item in snapshot.account_cashflows if item.event_type == "funding"
    )
    assert summary.attributed_amount == "0"
    assert summary.unattributed_amount == "-1"


def test_unique_temporal_cashflow_is_attributed_and_conserved(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-open", time_ms=start, action="open_short", quantity="2", price="10"
    )
    history.add_execution(
        "execution-close",
        time_ms=start + 2 * HOUR_MS,
        action="close_short",
        quantity="2",
        price="9",
    )
    history.add_cashflow(
        "cashflow-funding",
        time_ms=start + HOUR_MS,
        event_type="funding",
        amount="0.5",
    )
    history.add_cashflow(
        "cashflow-transfer",
        time_ms=start + HOUR_MS,
        event_type="transfer",
        amount="5",
        symbol="",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    funding = next(
        item
        for item in snapshot.cashflow_attributions
        if item.cashflow_record_id == "cashflow-funding"
    )
    transfer = next(
        item
        for item in snapshot.cashflow_attributions
        if item.cashflow_record_id == "cashflow-transfer"
    )
    assert funding.method == "temporal_unique"
    assert funding.episode_id
    assert transfer.method == "excluded_capital_movement"
    assert not transfer.episode_id
    raw_total = sum(
        (Decimal(item.amount) for item in snapshot.account_cashflows), Decimal(0)
    )
    classified_total = sum(
        (
            Decimal(item.attributed_amount)
            + Decimal(item.unattributed_amount)
            + Decimal(item.excluded_amount)
            for item in snapshot.account_cashflows
        ),
        Decimal(0),
    )
    assert raw_total == classified_total == Decimal("5.5")


def test_conversion_hook_preserves_source_and_reporting_conservation(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-open", time_ms=start, action="open_long", quantity="1", price="10"
    )
    history.add_execution(
        "execution-close",
        time_ms=start + HOUR_MS,
        action="close_long",
        quantity="1",
        price="11",
        trade_id="trade-close",
    )
    history.add_cashflow(
        "cashflow-result",
        time_ms=start + HOUR_MS,
        event_type="realized_pnl",
        amount="3",
        trade_id="trade-close",
    )
    conversion = FixedConversion()

    snapshot = build_snapshot(history.write(tmp_path), conversion=conversion)

    summary = snapshot.account_cashflows[0]
    assert conversion.prepared == 1
    assert summary.amount == "3"
    assert summary.reporting_amount == "6"
    assert summary.attributed_reporting_amount == "6"
    assert summary.unattributed_reporting_amount == "0"
    assert snapshot.episode_cashflows[0].reporting_currency == "REPORTING_CURRENCY"
