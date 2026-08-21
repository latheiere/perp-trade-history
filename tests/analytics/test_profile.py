from pathlib import Path

from perp_trade_history.analytics import build_snapshot
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, utc_ms


def test_capabilities_disable_metrics_without_required_economic_facts(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1",
        time_ms=start,
        action="open_long",
        quantity="2",
        price="10",
        quantity_unit="contract",
    )
    history.add_execution(
        "execution-2",
        time_ms=start + HOUR_MS,
        action="close_long",
        quantity="2",
        price="11",
        quantity_unit="contract",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert snapshot.capabilities.episode_reconstruction
    assert not snapshot.capabilities.notional_metrics
    assert any(flag.code == "missing_contract_economics" for flag in snapshot.quality_flags)


def test_as_of_boundary_excludes_later_facts(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-open", time_ms=start, action="open_long", quantity="1", price="10"
    )
    history.add_execution(
        "execution-close",
        time_ms=start + 2 * HOUR_MS,
        action="close_long",
        quantity="1",
        price="11",
    )
    history.add_cashflow(
        "cashflow-later",
        time_ms=start + 2 * HOUR_MS,
        event_type="realized_pnl",
        amount="1",
        trade_id="trade-execution-close",
    )

    snapshot = build_snapshot(history.write(tmp_path), as_of_ms=start + HOUR_MS)

    assert snapshot.episodes[0].status == "open"
    assert not snapshot.cashflow_attributions
    cashflow_profile = next(
        table for table in snapshot.profile.tables if table.name == "cashflows"
    )
    assert cashflow_profile.rows == 0


def test_snapshot_is_serializable_and_filter_dimensions_are_deduplicated(
    tmp_path: Path,
) -> None:
    snapshot = build_snapshot(multi_history_for_dimensions().write(tmp_path))

    payload = snapshot.as_dict()
    assert payload["episodes"]
    assert snapshot.dimensions.venues == ("venue_a", "venue_b")
    assert snapshot.dimensions.accounts == ("account_a", "account_b")
    assert snapshot.dimensions.symbols == ("CONTRACT_A", "CONTRACT_B")


def multi_history_for_dimensions() -> SyntheticHistory:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1", time_ms=start, action="open_long", quantity="1", price="10"
    )
    history.add_execution(
        "execution-2",
        time_ms=start,
        action="open_short",
        quantity="1",
        price="10",
        venue="venue_b",
        account_id="account_b",
        symbol="CONTRACT_B",
    )
    return history
