from decimal import Decimal

import pytest

from perp_trade_history.analytics import build_snapshot_from_tables
from perp_trade_history.dashboard.models import DashboardFilters
from perp_trade_history.dashboard.providers import _adapt_analytics
from perp_trade_history.models import TABLE_FIELDS, row_for
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, utc_ms

START = utc_ms(2025, 1, 1)


def _position(identity="lifetime", *, opened=START, closed=START + 2 * HOUR_MS, **kwargs):
    return row_for(
        "positions",
        record_id=kwargs.pop("record_id", identity),
        position_id=identity,
        venue=kwargs.pop("venue", "venue_a"),
        account_id=kwargs.pop("account_id", "account_a"),
        market_type=kwargs.pop("market_type", "perpetual"),
        symbol=kwargs.pop("symbol", "CONTRACT_A"),
        position_side=kwargs.pop("position_side", "long"),
        status="closed" if closed else "open",
        opened_at_ms=opened or "",
        closed_at_ms=closed or "",
        source_updated_at_ms=closed or START + 2 * HOUR_MS,
        quantity="2",
        quantity_unit="base",
        settlement_currency="CURRENCY_A",
        open_price="10",
        close_price="12" if closed else "",
        **kwargs,
    )


def _snapshot(history):
    tables = {name: history.rows.get(name, []) for name in TABLE_FIELDS}
    return build_snapshot_from_tables(tables), tables


def _dashboard(history, filters=None):
    analytics, tables = _snapshot(history)
    return _adapt_analytics(
        analytics,
        filters or DashboardFilters(),
        revision="fixture",
        reporting_currency="CURRENCY_A",
        execution_rows=tables["executions"],
        cashflow_rows=tables["cashflows"],
    )


@pytest.mark.parametrize("venue", ["mexc", "gate", "binance", "venue_a"])
def test_historical_positions_without_fills_are_searchable_for_supported_venues(venue):
    history = SyntheticHistory(rows={"positions": [_position(venue=venue)]})
    snapshot = _dashboard(history, DashboardFilters(profile=venue, query="contract_a"))

    assert len(snapshot.episodes) == 1
    row = snapshot.episodes[0]
    assert row.status == "Closed"
    assert row.direction == "Long"
    assert row.duration == "2h 0m"
    assert row.entry == "10" and row.exit == "12"
    assert row.net_result is None and row.outcome == "Unavailable"
    assert row.executions == ()
    assert row.tags == ("No flags",)
    assert snapshot.kpis[1].value == "1"
    assert snapshot.date_start == "2025-01-01"
    assert _dashboard(history, DashboardFilters(query="missing")).episodes == ()


def test_position_cashflows_at_the_closing_boundary_are_attributed_once():
    history = SyntheticHistory(rows={"positions": [_position()]})
    for event_type, amount in (("realized_pnl", "4"), ("commission", "-1"), ("funding", "-0.5")):
        row = history.add_cashflow(
            event_type,
            time_ms=START + 2 * HOUR_MS,
            event_type=event_type,
            amount=amount,
        )
        row.update(position_id="lifetime", event_subtype="position_summary")
    informational = history.add_cashflow(
        "detail",
        time_ms=START + HOUR_MS,
        event_type="realized_pnl",
        amount="4",
        role="informational",
    )
    informational["position_id"] = "lifetime"

    analytics, _ = _snapshot(history)
    snapshot = _dashboard(history)
    assert snapshot.episodes[0].net_result == 2.5
    assert len(snapshot.episodes[0].cashflows) == 3
    total = sum(Decimal(row.reporting_amount) for row in analytics.episode_cashflows)
    assert total == Decimal("2.5")
    assert all(
        row.method == "exact_position" for row in analytics.cashflow_attributions if row.episode_id
    )


@pytest.mark.parametrize("complete_fills", [False, True])
def test_summary_and_execution_history_share_one_stable_row(complete_fills):
    history = SyntheticHistory(rows={"positions": [_position()]})
    original, _ = _snapshot(history)
    if complete_fills:
        history.add_execution("open", time_ms=START, action="open_long", quantity="2", price="10")
    history.add_execution(
        "close",
        time_ms=START + 2 * HOUR_MS,
        action="close_long",
        quantity="2",
        price="12",
    )
    result, _ = _snapshot(history)

    assert len(result.episodes) == 1
    assert result.episodes[0].episode_id == original.episodes[0].episode_id
    assert result.episodes[0].opened_at_ms == START
    assert result.episodes[0].execution_count == (2 if complete_fills else 1)
    assert {link.episode_id for link in result.execution_links} == {result.episodes[0].episode_id}


def test_repeated_sources_and_partial_close_fragments_do_not_duplicate_positions():
    first = _position()
    repeated = {**first, "record_id": "other-source", "open_price": "", "source": "import"}
    history = SyntheticHistory(rows={"positions": [first, repeated]})
    for number in (1, 2):
        history.add_execution(
            f"close-{number}",
            time_ms=START + number * HOUR_MS,
            action="close_long",
            quantity="1",
            price="12",
        )
    result, _ = _snapshot(history)
    assert len(result.episodes) == 1
    assert result.episodes[0].execution_count == 2
    assert result.episodes[0].entry_vwap == "10"
    reversed_history = SyntheticHistory(
        rows={key: list(reversed(rows)) for key, rows in history.rows.items()}
    )
    assert _snapshot(reversed_history)[0] == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("venue", "venue_b"),
        ("account_id", "account_b"),
        ("market_type", "inverse_perpetual"),
        ("symbol", "CONTRACT_B"),
        ("position_side", "short"),
        ("settlement_currency", "CURRENCY_B"),
    ],
)
def test_position_membership_preserves_context_boundaries(field, value):
    history = SyntheticHistory(rows={"positions": [_position(), {**_position(), field: value}]})
    result, _ = _snapshot(history)
    assert len(result.episodes) == 2
    assert len({row.episode_id for row in result.episodes}) == 2


def test_adjacent_lifetimes_and_opposing_positions_remain_distinct():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position("first", closed=START + HOUR_MS),
                _position("second", opened=START + HOUR_MS),
                _position("opposite", position_side="short"),
            ]
        }
    )
    for identity, hour, action in (
        ("a", 0, "open_long"),
        ("b", 1, "close_long"),
        ("c", 1, "open_long"),
        ("d", 2, "close_long"),
    ):
        history.add_execution(
            identity, time_ms=START + hour * HOUR_MS, action=action, quantity="2", price="10"
        )
    result, _ = _snapshot(history)
    assert len(result.episodes) == 3
    assert sorted(row.execution_count for row in result.episodes) == [0, 2, 2]


def test_snapshot_without_lifecycle_dates_retains_unknown_fields_and_ignores_zero_positions():
    observation = _position(opened=None, closed=None)
    observation.update(open_price="", close_price="")
    history = SyntheticHistory(
        rows={
            "positions": [
                observation,
                {**observation, "record_id": "later-observation"},
                {
                    **observation,
                    "record_id": "empty",
                    "symbol": "CONTRACT_B",
                    "status": "closed",
                    "quantity": "0",
                },
            ]
        }
    )
    snapshot = _dashboard(history)
    assert len(snapshot.episodes) == 1
    row = snapshot.episodes[0]
    assert row.duration == "Unavailable" and row.entry == "Unavailable"
    assert row.net_result is None and row.status == "Open"
    assert snapshot.date_start == "2025-01-01"
    assert _dashboard(history, DashboardFilters(weekday=2)).episodes == ()


def test_close_only_position_does_not_invent_an_opening_or_duration():
    history = SyntheticHistory(rows={"positions": [_position(opened=None)]})
    analytics, _ = _snapshot(history)
    snapshot = _dashboard(history)
    assert len(analytics.episodes) == 1
    assert analytics.episodes[0].opened_at_ms is None
    assert analytics.episodes[0].duration_ms is None
    assert snapshot.episodes[0].duration == "Unavailable"
    assert snapshot.kpis[1].value == "1"
    assert snapshot.kpis[3].value == "Unavailable"


def test_snapshots_within_known_lifetimes_do_not_add_rows():
    historical = _position()
    observation = _position("snapshot", opened=None, closed=None)
    observation["source_updated_at_ms"] = str(START + HOUR_MS)
    history = SyntheticHistory(rows={"positions": [historical, observation]})
    assert len(_snapshot(history)[0].episodes) == 1


def test_as_of_boundary_clips_future_closures_and_cashflows():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position(realized_pnl="4", funding="0", fees="-1"),
            ]
        }
    )
    row = history.add_cashflow(
        "result", time_ms=START + 2 * HOUR_MS, event_type="realized_pnl", amount="4"
    )
    row["position_id"] = "lifetime"
    _, tables = _snapshot(history)
    result = build_snapshot_from_tables(tables, as_of_ms=START + HOUR_MS)
    assert len(result.episodes) == 1
    assert result.episodes[0].status == "open"
    assert result.episodes[0].closed_at_ms is None
    assert result.episodes[0].exit_vwap == ""
    assert result.episode_cashflows == ()


def test_retained_position_total_replaces_partial_detail_without_changing_cashflow_authority():
    history = SyntheticHistory(rows={"positions": [_position()]})
    summary = history.add_cashflow(
        "position-total",
        time_ms=START + 2 * HOUR_MS,
        event_type="realized_pnl",
        amount="3",
        role="informational",
    )
    summary.update(position_id="lifetime", event_subtype="position_summary")
    partial = history.add_cashflow(
        "partial-profit",
        time_ms=START + HOUR_MS,
        event_type="realized_pnl",
        amount="2",
    )
    partial["position_id"] = "lifetime"
    analytics, _ = _snapshot(history)
    assert _dashboard(history).episodes[0].net_result == 3
    primary = next(row for row in analytics.account_cashflows if row.reporting_role == "primary")
    assert primary.amount == "2"
    assert summary["reporting_role"] == "informational"
    assert len(analytics.episode_cashflows) == 1


def test_reported_financial_components_require_complete_inputs_and_supported_currency():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position(realized_pnl="4", funding="-0.5", fees="-1"),
            ]
        }
    )
    assert _dashboard(history).episodes[0].net_result == 2.5
    history.rows["positions"][0]["funding"] = ""
    assert _dashboard(history).episodes[0].net_result is None
    history.rows["positions"][0].update(funding="-0.5", settlement_currency="CURRENCY_B")
    assert _dashboard(history).episodes[0].net_result is None


def test_position_cashflow_identity_cannot_cross_account_or_direction_boundaries():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position(),
                _position(account_id="account_b"),
            ]
        }
    )
    result = history.add_cashflow(
        "result",
        time_ms=START + 2 * HOUR_MS,
        event_type="realized_pnl",
        amount="4",
    )
    result.update(position_id="lifetime", event_subtype="position_summary")
    analytics, _ = _snapshot(history)
    assert len(analytics.episode_cashflows) == 1
    linked = next(row for row in analytics.cashflow_attributions if row.episode_id)
    assert (
        next(row for row in analytics.episodes if row.episode_id == linked.episode_id).account_id
        == "account_a"
    )
    history.rows["positions"].append(_position(position_side="short"))
    analytics, _ = _snapshot(history)
    assert not analytics.episode_cashflows
    assert not any(row.episode_id for row in analytics.cashflow_attributions)


def test_missing_middle_fills_do_not_collapse_distinct_reported_lifetimes():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position("first", closed=START + HOUR_MS),
                _position("second", opened=START + HOUR_MS),
            ]
        }
    )
    history.add_execution("open", time_ms=START, action="open_long", quantity="2", price="10")
    history.add_execution(
        "close", time_ms=START + 2 * HOUR_MS, action="close_long", quantity="2", price="12"
    )
    result, _ = _snapshot(history)
    assert len(result.episodes) == 2
    assert sorted(row.execution_count for row in result.episodes) == [1, 1]
    assert len(result.execution_links) == 2


def test_overlapping_reported_lifetimes_remain_visible_without_guessing_fill_ownership():
    history = SyntheticHistory(rows={"positions": [_position("first"), _position("second")]})
    history.add_execution("open", time_ms=START, action="open_long", quantity="2", price="10")
    history.add_execution(
        "close", time_ms=START + 2 * HOUR_MS, action="close_long", quantity="2", price="12"
    )
    result, _ = _snapshot(history)
    assert len(result.episodes) == 2
    assert result.execution_links == ()
    assert all(row.execution_count == 0 for row in result.episodes)


def test_unknown_observation_dates_never_display_epoch_dates():
    row = _position(opened=None, closed=None)
    row.update(source_updated_at_ms="", collected_at="")
    snapshot = _dashboard(SyntheticHistory(rows={"positions": [row]}))
    assert len(snapshot.episodes) == 1
    assert snapshot.episodes[0].occurred_at == "Unavailable"
    assert "1970" not in snapshot.refreshed_label


def test_fills_outside_reported_lifetimes_remain_available_after_reconciliation():
    history = SyntheticHistory(
        rows={
            "positions": [
                _position("first", closed=START + HOUR_MS),
                _position("second", opened=START + 2 * HOUR_MS, closed=START + 3 * HOUR_MS),
            ]
        }
    )
    history.add_execution("open", time_ms=START, action="open_long", quantity="2", price="10")
    history.add_execution(
        "close", time_ms=START + 4 * HOUR_MS, action="close_long", quantity="2", price="12"
    )
    result, _ = _snapshot(history)
    assert len(result.episodes) == 3
    assert {link.execution_record_id for link in result.execution_links} == {"open", "close"}
    partial = next(row for row in result.episodes if not row.position_id)
    assert partial.duration_ms is None and partial.opened_at_ms is None
    assert partial.closed_at_ms == START + 4 * HOUR_MS
    assert partial.execution_count == 1
    assert sum(row.execution_count for row in result.episodes) == 2
