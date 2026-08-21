from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from perp_trade_history.analytics.schema import (
    AnalyticsBuildReport,
    CoverageGap,
    EpisodeRow,
)
from perp_trade_history.storage import CoverageIndex

_Profile = tuple[str, str, str]


def build_analytics_report(
    *,
    episodes: Sequence[EpisodeRow],
    coverage_rows: Sequence[dict[str, str]],
    reconstructed_boundaries_complete: bool | None = None,
) -> AnalyticsBuildReport:
    """Describe source gaps separately from reconstructed episode boundaries."""
    rows_by_profile: dict[_Profile, list[dict[str, str]]] = defaultdict(list)
    for row in coverage_rows:
        if row.get("dataset") != "trades":
            continue
        rows_by_profile[_profile_from_row(row)].append(row)

    indexes = {profile: CoverageIndex(rows) for profile, rows in rows_by_profile.items()}
    empty_index = CoverageIndex([])
    statuses: list[str] = []
    gaps: list[CoverageGap] = []
    for episode in episodes:
        profile = (episode.venue, episode.account_id, episode.market_type)
        profile_rows = rows_by_profile.get(profile, [])
        assessment = indexes.get(profile, empty_index).assessment(
            venue=episode.venue,
            account_id=episode.account_id,
            dataset="trades",
            start_ms=episode.opened_at_ms,
            end_ms=episode.opened_at_ms + episode.duration_ms,
        )
        statuses.append(str(assessment["status"]))
        scope_results = assessment["scopes"]
        if scope_results:
            for scope, result in scope_results.items():
                for start_ms, end_ms in result["gaps"]:
                    gaps.extend(
                        _describe_gap(
                            profile=profile,
                            scope=str(scope),
                            start_ms=int(start_ms),
                            end_ms=int(end_ms),
                            rows=profile_rows,
                        )
                    )
        else:
            for start_ms, end_ms in assessment["gaps"]:
                gaps.extend(
                    _describe_gap(
                        profile=profile,
                        scope="",
                        start_ms=int(start_ms),
                        end_ms=int(end_ms),
                        rows=profile_rows,
                    )
                )

    return AnalyticsBuildReport(
        source_trade_coverage=_combined_coverage_status(statuses),
        reconstructed_episode_boundaries=_boundary_status(
            episodes, reconstructed_boundaries_complete
        ),
        gaps=_merge_equivalent_gaps(gaps),
    )


def _describe_gap(
    *,
    profile: _Profile,
    scope: str,
    start_ms: int,
    end_ms: int,
    rows: Sequence[dict[str, str]],
) -> list[CoverageGap]:
    candidates = [
        (row, interval)
        for row in rows
        if (not scope or str(row.get("scope") or "account") == scope)
        and row.get("status") != "complete"
        and (interval := _row_interval(row)) is not None
        and interval[0] <= end_ms
        and interval[1] >= start_ms
    ]
    boundaries = {start_ms, end_ms + 1}
    for _, (row_start, row_end) in candidates:
        boundaries.add(max(start_ms, row_start))
        boundaries.add(min(end_ms, row_end) + 1)
    ordered = sorted(boundaries)

    described: list[CoverageGap] = []
    for segment_start, next_start in zip(ordered, ordered[1:], strict=False):
        segment_end = next_start - 1
        matching = [
            (row, interval)
            for row, interval in candidates
            if interval[0] <= segment_start <= interval[1]
        ]
        row = _narrowest_metadata(matching)
        described.append(
            CoverageGap(
                venue=profile[0],
                account_id=profile[1],
                market_type=profile[2],
                dataset="trades",
                scope=scope,
                start_ms=segment_start,
                end_ms=segment_end,
                status=str(row.get("status") or "unknown") if row else "unknown",
                reason=(str(row.get("limitation") or "") or None) if row else None,
                source=str(row.get("source") or "") if row else "",
                acquisition=str(row.get("acquisition") or "") if row else "",
            )
        )
    return described


def _narrowest_metadata(
    candidates: Sequence[tuple[dict[str, str], tuple[int, int]]],
) -> dict[str, str] | None:
    if not candidates:
        return None
    with_reason = [item for item in candidates if item[0].get("limitation")]
    eligible = with_reason or list(candidates)
    narrowest_span = min(end - start for _, (start, end) in eligible)
    narrowest = [row for row, (start, end) in eligible if end - start == narrowest_span]
    return max(
        narrowest,
        key=lambda row: (
            str(row.get("collected_at") or ""),
            str(row.get("record_id") or ""),
        ),
    )


def _row_interval(row: dict[str, str]) -> tuple[int, int] | None:
    try:
        start_ms = int(row.get("start_time_ms") or "")
        end_ms = int(row.get("end_time_ms") or "")
    except ValueError:
        return None
    if start_ms > end_ms:
        return None
    return start_ms, end_ms


def _profile_from_row(row: dict[str, str]) -> _Profile:
    return (
        str(row.get("venue") or ""),
        str(row.get("account_id") or ""),
        str(row.get("market_type") or ""),
    )


def _combined_coverage_status(statuses: Sequence[str]) -> str:
    if not statuses:
        return "unknown"
    if all(status == "complete" for status in statuses):
        return "complete"
    if any(status == "incomplete" for status in statuses):
        return "incomplete"
    return "unknown"


def _boundary_status(episodes: Sequence[EpisodeRow], complete: bool | None) -> str:
    if not episodes:
        return "unknown"
    if complete is not None:
        return "complete" if complete else "incomplete"
    return (
        "complete"
        if all(episode.boundary_status == "complete" for episode in episodes)
        else "incomplete"
    )


def _merge_equivalent_gaps(gaps: Sequence[CoverageGap]) -> tuple[CoverageGap, ...]:
    ordered = sorted(
        gaps,
        key=lambda gap: (
            gap.venue,
            gap.account_id,
            gap.market_type,
            gap.dataset,
            gap.scope,
            gap.start_ms,
            gap.end_ms,
            gap.status,
            gap.reason or "",
            gap.source,
            gap.acquisition,
        ),
    )
    merged: list[CoverageGap] = []
    for gap in ordered:
        if merged and _equivalent(merged[-1], gap) and gap.start_ms <= merged[-1].end_ms + 1:
            merged[-1] = replace(merged[-1], end_ms=max(merged[-1].end_ms, gap.end_ms))
        else:
            merged.append(gap)
    return tuple(merged)


def _equivalent(left: CoverageGap, right: CoverageGap) -> bool:
    return (
        left.venue,
        left.account_id,
        left.market_type,
        left.dataset,
        left.scope,
        left.status,
        left.reason,
        left.source,
        left.acquisition,
    ) == (
        right.venue,
        right.account_id,
        right.market_type,
        right.dataset,
        right.scope,
        right.status,
        right.reason,
        right.source,
        right.acquisition,
    )
