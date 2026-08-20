from decimal import Decimal
from pathlib import Path

from perp_trade_history.analytics import build_snapshot
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, multi_year_history, utc_ms


def test_explicit_side_episodes_scale_in_and_out_with_weighted_prices(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1", time_ms=start, action="open_long", quantity="2", price="10"
    )
    history.add_execution(
        "execution-2",
        time_ms=start + HOUR_MS,
        action="open_long",
        quantity="1",
        price="16",
    )
    history.add_execution(
        "execution-3",
        time_ms=start + 2 * HOUR_MS,
        action="close_long",
        quantity="1",
        price="13",
    )
    history.add_execution(
        "execution-4",
        time_ms=start + 3 * HOUR_MS,
        action="close_long",
        quantity="2",
        price="19",
    )
    history.add_complete_trade_coverage(start_ms=start, end_ms=start + 4 * HOUR_MS)

    snapshot = build_snapshot(history.write(tmp_path))

    assert len(snapshot.episodes) == 1
    episode = snapshot.episodes[0]
    assert episode.status == "closed"
    assert episode.boundary_status == "complete"
    assert episode.entry_vwap == "12"
    assert episode.exit_vwap == "17"
    assert episode.maximum_quantity == "3"
    assert episode.remaining_quantity == "0"
    assert episode.execution_count == 4
    assert episode.coverage_status == "complete"


def test_net_position_flip_splits_execution_quantity_without_loss(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1", time_ms=start, action="buy", quantity="2", price="10"
    )
    history.add_execution(
        "execution-2", time_ms=start + HOUR_MS, action="sell", quantity="5", price="11"
    )
    history.add_execution(
        "execution-3", time_ms=start + 2 * HOUR_MS, action="buy", quantity="3", price="9"
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert [(episode.direction, episode.status) for episode in snapshot.episodes] == [
        ("long", "closed"),
        ("short", "closed"),
    ]
    flip_links = [
        link for link in snapshot.execution_links if link.execution_record_id == "execution-2"
    ]
    assert {link.transition for link in flip_links} == {"open", "close"}
    assert sum((Decimal(link.quantity) for link in flip_links), Decimal(0)) == Decimal("5")


def test_missing_history_boundaries_are_explicitly_censored(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    history.add_execution(
        "execution-1", time_ms=start, action="close_long", quantity="2", price="10"
    )
    history.add_execution(
        "execution-2",
        time_ms=start + HOUR_MS,
        action="open_short",
        quantity="1",
        price="11",
        symbol="CONTRACT_B",
    )

    snapshot = build_snapshot(history.write(tmp_path), as_of_ms=start + 2 * HOUR_MS)

    assert {episode.boundary_status for episode in snapshot.episodes} == {
        "left_censored",
        "right_censored",
    }
    assert any(flag.code == "left_censored_episode" for flag in snapshot.quality_flags)
    assert any(flag.code == "right_censored_episode" for flag in snapshot.quality_flags)


def test_multi_year_history_exposes_period_and_duration_dimensions(tmp_path: Path) -> None:
    snapshot = build_snapshot(multi_year_history().write(tmp_path))

    assert snapshot.dimensions.open_years == (2020, 2024)
    assert snapshot.dimensions.close_years == (2021, 2026)
    assert snapshot.dimensions.directions == ("long", "short")
    assert "30d_or_more" in snapshot.dimensions.duration_buckets


def test_opposing_tied_transitions_are_deterministic_and_diagnosed(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2022, 1, 1)
    first = history.add_execution(
        "execution-b", time_ms=start, action="open_long", quantity="1", price="10"
    )
    second = history.add_execution(
        "execution-a", time_ms=start, action="close_long", quantity="1", price="11"
    )
    forward = build_snapshot(history.write(tmp_path / "forward"))
    reversed_history = SyntheticHistory(rows={"executions": [second, first]})
    reverse = build_snapshot(reversed_history.write(tmp_path / "reverse"))

    assert forward.episodes == reverse.episodes
    assert forward.execution_links == reverse.execution_links
    assert forward.profile.opposing_tied_transition_groups == 1
    assert any(flag.code == "opposing_tied_transitions" for flag in forward.quality_flags)
