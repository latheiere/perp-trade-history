from pathlib import Path

from perp_trade_history.analytics import build_snapshot
from tests.analytics.synthetic_history import DAY_MS, HOUR_MS, SyntheticHistory, utc_ms


def test_latest_comparable_periods_emit_deterministic_thresholded_changes(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    _add_period(history, year=2023, episodes=5, duration_ms=HOUR_MS, outcome="1")
    _add_period(history, year=2025, episodes=10, duration_ms=2 * DAY_MS, outcome="-1")

    snapshot = build_snapshot(history.write(tmp_path))

    by_metric = {change.metric: change for change in snapshot.notable_changes}
    assert set(by_metric) == {
        "average_net_cashflow",
        "closed_episode_count",
        "median_duration",
        "positive_outcome_rate",
    }
    assert by_metric["closed_episode_count"].prior_period == "2023"
    assert by_metric["closed_episode_count"].current_period == "2025"
    assert by_metric["closed_episode_count"].relative_change == "1"
    assert by_metric["positive_outcome_rate"].direction == "decreased"
    assert by_metric["average_net_cashflow"].unit == "CURRENCY_A"


def test_period_comparison_suppresses_claims_below_minimum_sample(tmp_path: Path) -> None:
    history = SyntheticHistory()
    _add_period(history, year=2023, episodes=4, duration_ms=HOUR_MS, outcome="1")
    _add_period(history, year=2024, episodes=10, duration_ms=2 * DAY_MS, outcome="-1")

    snapshot = build_snapshot(history.write(tmp_path))

    assert snapshot.notable_changes == ()


def _add_period(
    history: SyntheticHistory,
    *,
    year: int,
    episodes: int,
    duration_ms: int,
    outcome: str,
) -> None:
    period_start = utc_ms(year, 1, 1)
    for index in range(episodes):
        opened_at = period_start + index * 3 * DAY_MS
        closed_at = opened_at + duration_ms
        symbol = f"CONTRACT_{year}_{index}"
        open_id = f"execution-open-{year}-{index}"
        close_id = f"execution-close-{year}-{index}"
        trade_id = f"trade-close-{year}-{index}"
        history.add_execution(
            open_id,
            time_ms=opened_at,
            action="open_long",
            quantity="1",
            price="10",
            symbol=symbol,
        )
        history.add_execution(
            close_id,
            time_ms=closed_at,
            action="close_long",
            quantity="1",
            price="11",
            symbol=symbol,
            trade_id=trade_id,
        )
        history.add_cashflow(
            f"cashflow-{year}-{index}",
            time_ms=closed_at,
            event_type="realized_pnl",
            amount=outcome,
            symbol=symbol,
            trade_id=trade_id,
        )
