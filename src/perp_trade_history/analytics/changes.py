from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal

from perp_trade_history.analytics.schema import (
    EpisodeCashflowSummary,
    EpisodeRow,
    NotableChange,
)
from perp_trade_history.models import decimal_text

MIN_PERIOD_SAMPLE = 5
RELATIVE_CHANGE_THRESHOLD = Decimal("0.25")
WIN_RATE_CHANGE_THRESHOLD = Decimal("0.10")


def find_notable_changes(
    episodes: Sequence[EpisodeRow],
    episode_cashflows: Sequence[EpisodeCashflowSummary],
) -> tuple[NotableChange, ...]:
    """Compare the latest two sufficiently observed calendar periods deterministically."""
    eligible = [
        episode
        for episode in episodes
        if episode.status == "closed"
        and episode.boundary_status == "complete"
        and episode.close_year is not None
    ]
    by_year: dict[int, list[EpisodeRow]] = defaultdict(list)
    for episode in eligible:
        by_year[int(episode.close_year)].append(episode)
    years = sorted(by_year)
    if len(years) < 2:
        return ()
    prior_year, current_year = years[-2:]
    prior = by_year[prior_year]
    current = by_year[current_year]
    if len(prior) < MIN_PERIOD_SAMPLE or len(current) < MIN_PERIOD_SAMPLE:
        return ()

    changes: list[NotableChange] = []
    _append_relative_change(
        changes,
        metric="closed_episode_count",
        prior_period=str(prior_year),
        current_period=str(current_year),
        prior_value=Decimal(len(prior)),
        current_value=Decimal(len(current)),
        unit="episodes",
        prior_sample=len(prior),
        current_sample=len(current),
        summary_subject="Closed episode frequency",
    )
    _append_relative_change(
        changes,
        metric="median_duration",
        prior_period=str(prior_year),
        current_period=str(current_year),
        prior_value=_median(Decimal(episode.duration_ms) for episode in prior),
        current_value=_median(Decimal(episode.duration_ms) for episode in current),
        unit="milliseconds",
        prior_sample=len(prior),
        current_sample=len(current),
        summary_subject="Median closed-episode duration",
    )
    changes.extend(
        _outcome_changes(
            prior_year=prior_year,
            current_year=current_year,
            prior=prior,
            current=current,
            episode_cashflows=episode_cashflows,
        )
    )
    return tuple(sorted(changes, key=lambda change: (change.metric, change.unit)))


def _outcome_changes(
    *,
    prior_year: int,
    current_year: int,
    prior: Sequence[EpisodeRow],
    current: Sequence[EpisodeRow],
    episode_cashflows: Sequence[EpisodeCashflowSummary],
) -> list[NotableChange]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    currencies: dict[str, set[str]] = defaultdict(set)
    for row in episode_cashflows:
        totals[(row.episode_id, row.reporting_currency)] += Decimal(row.reporting_amount)
        currencies[row.episode_id].add(row.reporting_currency)
    comparable_currencies = {
        currency
        for episode in (*prior, *current)
        if len(currencies.get(episode.episode_id, set())) == 1
        for currency in currencies[episode.episode_id]
    }
    if len(comparable_currencies) != 1:
        return []
    currency = next(iter(comparable_currencies))
    prior_values = [
        totals[(episode.episode_id, currency)]
        for episode in prior
        if currencies.get(episode.episode_id) == {currency}
    ]
    current_values = [
        totals[(episode.episode_id, currency)]
        for episode in current
        if currencies.get(episode.episode_id) == {currency}
    ]
    if len(prior_values) < MIN_PERIOD_SAMPLE or len(current_values) < MIN_PERIOD_SAMPLE:
        return []

    changes: list[NotableChange] = []
    _append_relative_change(
        changes,
        metric="average_net_cashflow",
        prior_period=str(prior_year),
        current_period=str(current_year),
        prior_value=sum(prior_values, Decimal(0)) / len(prior_values),
        current_value=sum(current_values, Decimal(0)) / len(current_values),
        unit=currency,
        prior_sample=len(prior_values),
        current_sample=len(current_values),
        summary_subject="Average attributed net cashflow",
    )
    prior_win_rate = Decimal(sum(value > 0 for value in prior_values)) / len(prior_values)
    current_win_rate = Decimal(sum(value > 0 for value in current_values)) / len(current_values)
    delta = current_win_rate - prior_win_rate
    if abs(delta) >= WIN_RATE_CHANGE_THRESHOLD:
        changes.append(
            NotableChange(
                comparison="latest_calendar_periods",
                metric="positive_outcome_rate",
                prior_period=str(prior_year),
                current_period=str(current_year),
                prior_value=decimal_text(prior_win_rate),
                current_value=decimal_text(current_win_rate),
                absolute_delta=decimal_text(delta),
                relative_change="",
                unit="ratio",
                direction=_direction(delta),
                prior_sample_size=len(prior_values),
                current_sample_size=len(current_values),
                threshold=f"absolute>={decimal_text(WIN_RATE_CHANGE_THRESHOLD)}",
                summary=(
                    "Positive closed-episode outcome rate "
                    f"{_direction(delta)} beyond the comparison threshold."
                ),
            )
        )
    return changes


def _append_relative_change(
    changes: list[NotableChange],
    *,
    metric: str,
    prior_period: str,
    current_period: str,
    prior_value: Decimal,
    current_value: Decimal,
    unit: str,
    prior_sample: int,
    current_sample: int,
    summary_subject: str,
) -> None:
    if prior_value == 0:
        return
    delta = current_value - prior_value
    relative = delta / abs(prior_value)
    if abs(relative) < RELATIVE_CHANGE_THRESHOLD:
        return
    direction = _direction(delta)
    changes.append(
        NotableChange(
            comparison="latest_calendar_periods",
            metric=metric,
            prior_period=prior_period,
            current_period=current_period,
            prior_value=decimal_text(prior_value),
            current_value=decimal_text(current_value),
            absolute_delta=decimal_text(delta),
            relative_change=decimal_text(relative),
            unit=unit,
            direction=direction,
            prior_sample_size=prior_sample,
            current_sample_size=current_sample,
            threshold=f"relative>={decimal_text(RELATIVE_CHANGE_THRESHOLD)}",
            summary=f"{summary_subject} {direction} beyond the comparison threshold.",
        )
    )


def _median(values: Iterable[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _direction(delta: Decimal) -> str:
    if delta > 0:
        return "increased"
    if delta < 0:
        return "decreased"
    return "unchanged"
