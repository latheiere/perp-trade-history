from pathlib import Path

from perp_trade_history.analytics import build_snapshot
from tests.analytics.synthetic_history import HOUR_MS, SyntheticHistory, utc_ms


def _closed_episode(history: SyntheticHistory, start_ms: int, end_ms: int) -> None:
    history.add_execution(
        "execution-open",
        time_ms=start_ms,
        action="open_long",
        quantity="1",
        price="10",
    )
    history.add_execution(
        "execution-close",
        time_ms=end_ms,
        action="close_long",
        quantity="1",
        price="11",
    )


def test_build_report_exposes_exact_gaps_with_recorded_metadata(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2024, 1, 1)
    end = start + 6 * HOUR_MS
    _closed_episode(history, start, end)
    history.add_trade_coverage(
        "complete-before",
        start_ms=start,
        end_ms=start + HOUR_MS - 1,
        status="complete",
    )
    history.add_trade_coverage(
        "partial-window",
        start_ms=start + HOUR_MS,
        end_ms=start + 3 * HOUR_MS - 1,
        status="partial",
        limitation="Recorded response window was incomplete.",
        source="bounded-response",
        acquisition="api",
    )
    history.add_trade_coverage(
        "failed-window",
        start_ms=start + 3 * HOUR_MS,
        end_ms=start + 4 * HOUR_MS - 1,
        status="failed",
        limitation="Recorded collection request failed.",
        source="bounded-response",
        acquisition="api",
    )
    history.add_trade_coverage(
        "complete-after",
        start_ms=start + 4 * HOUR_MS,
        end_ms=end,
        status="complete",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert snapshot.build_report.source_trade_coverage == "incomplete"
    assert snapshot.build_report.reconstructed_episode_boundaries == "complete"
    assert [
        (
            gap.start_ms,
            gap.end_ms,
            gap.status,
            gap.reason,
            gap.source,
            gap.acquisition,
        )
        for gap in snapshot.build_report.gaps
    ] == [
        (
            start + HOUR_MS,
            start + 3 * HOUR_MS - 1,
            "partial",
            "Recorded response window was incomplete.",
            "bounded-response",
            "api",
        ),
        (
            start + 3 * HOUR_MS,
            start + 4 * HOUR_MS - 1,
            "failed",
            "Recorded collection request failed.",
            "bounded-response",
            "api",
        ),
    ]
    assert all(gap.market_type == "perpetual" for gap in snapshot.build_report.gaps)
    assert all(gap.scope == "account" for gap in snapshot.build_report.gaps)
    assert all(gap.venue == "venue_a" for gap in snapshot.build_report.gaps)
    assert all(gap.account_id == "account_a" for gap in snapshot.build_report.gaps)
    assert all(gap.dataset == "trades" for gap in snapshot.build_report.gaps)


def test_equivalent_adjacent_gap_metadata_is_merged_without_inventing_reason(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2024, 1, 1)
    end = start + 4 * HOUR_MS
    _closed_episode(history, start, end)
    history.add_trade_coverage(
        "partial-one",
        start_ms=start,
        end_ms=start + 2 * HOUR_MS - 1,
        status="partial",
        source="bounded-response",
    )
    history.add_trade_coverage(
        "partial-two",
        start_ms=start + 2 * HOUR_MS,
        end_ms=end,
        status="partial",
        source="bounded-response",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert len(snapshot.build_report.gaps) == 1
    gap = snapshot.build_report.gaps[0]
    assert (gap.start_ms, gap.end_ms, gap.status) == (start, end, "partial")
    assert gap.reason is None


def test_unknown_gap_does_not_borrow_metadata_from_another_market_scope(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2024, 1, 1)
    end = start + HOUR_MS
    _closed_episode(history, start, end)
    history.add_trade_coverage(
        "other-market",
        start_ms=start,
        end_ms=end,
        status="partial",
        limitation="A different market collection was incomplete.",
        market_type="delivery",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert snapshot.build_report.source_trade_coverage == "unknown"
    assert len(snapshot.build_report.gaps) == 1
    gap = snapshot.build_report.gaps[0]
    assert gap.scope == ""
    assert gap.status == "unknown"
    assert gap.reason is None
    assert gap.source == ""


def test_account_scope_takes_precedence_over_narrower_complete_scopes(
    tmp_path: Path,
) -> None:
    history = SyntheticHistory()
    start = utc_ms(2024, 1, 1)
    end = start + HOUR_MS
    _closed_episode(history, start, end)
    history.add_trade_coverage(
        "account-partial",
        start_ms=start,
        end_ms=end,
        status="partial",
        limitation="Account-level collection was incomplete.",
    )
    history.add_trade_coverage(
        "narrower-complete",
        start_ms=start,
        end_ms=end,
        status="complete",
        scope="settlement:quote",
    )

    snapshot = build_snapshot(history.write(tmp_path))

    assert len(snapshot.build_report.gaps) == 1
    gap = snapshot.build_report.gaps[0]
    assert gap.scope == "account"
    assert gap.status == "partial"


def test_source_coverage_and_reconstructed_boundaries_are_independent(
    tmp_path: Path,
) -> None:
    start = utc_ms(2024, 1, 1)
    history = SyntheticHistory()
    history.add_execution(
        "execution-open",
        time_ms=start,
        action="open_long",
        quantity="1",
        price="10",
    )
    history.add_complete_trade_coverage(start_ms=start, end_ms=start + HOUR_MS)

    snapshot = build_snapshot(history.write(tmp_path), as_of_ms=start + HOUR_MS)

    assert snapshot.build_report.source_trade_coverage == "complete"
    assert snapshot.build_report.reconstructed_episode_boundaries == "incomplete"
    assert snapshot.build_report.gaps == ()
    assert snapshot.capabilities.complete_source_trade_coverage
    assert not snapshot.capabilities.complete_reconstructed_episode_boundaries


def test_serialized_snapshot_preserves_build_report_fields(tmp_path: Path) -> None:
    history = SyntheticHistory()
    start = utc_ms(2024, 1, 1)
    _closed_episode(history, start, start + HOUR_MS)

    report = build_snapshot(history.write(tmp_path)).as_dict()["build_report"]

    assert report["source_trade_coverage"] == "unknown"
    assert report["reconstructed_episode_boundaries"] == "complete"
    assert report["gaps"][0]["start_ms"] == start
    assert report["gaps"][0]["end_ms"] == start + HOUR_MS
    assert report["gaps"][0]["reason"] is None
