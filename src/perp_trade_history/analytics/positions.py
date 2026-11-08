from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from perp_trade_history.analytics.constants import TRADING_CASHFLOW_TYPES
from perp_trade_history.analytics.episodes import (
    EpisodeBuildResult,
    _duration_bucket,
)
from perp_trade_history.analytics.schema import (
    EpisodeCashflowSummary,
    EpisodeExecutionLink,
    EpisodeRow,
)
from perp_trade_history.models import content_id, decimal_text, parse_datetime
from perp_trade_history.storage import CoverageIndex


def reconcile_positions(
    result: EpisodeBuildResult,
    *,
    positions: Sequence[dict[str, str]],
    executions: Sequence[dict[str, str]],
    coverage: Sequence[dict[str, str]],
    as_of_ms: int,
) -> EpisodeBuildResult:
    """Combine retained position lifetimes and fills without manufacturing executions."""
    if not positions:
        return result
    lifetimes: dict[tuple[str, ...], dict[str, str]] = {}
    observations: dict[tuple[str, ...], dict[str, str]] = {}
    for row in sorted(positions, key=_revision):
        if not row.get("symbol") or not row.get("venue"):
            continue
        opened, closed = _time(row, "opened_at_ms"), _time(row, "closed_at_ms")
        if opened and closed and closed < opened:
            continue
        observed = _observed(row)
        if (opened or closed or observed or 0) > as_of_ms:
            continue
        if closed:
            identity = (*_context(row), row.get("position_id") or row.get("record_id", ""))
            previous = lifetimes.get(identity, {})
            lifetimes[identity] = {**previous, **{k: v for k, v in row.items() if v != ""}}
        elif row.get("status") == "open" and _positive(row.get("quantity")):
            observations[(*_context(row), row.get("position_id", ""))] = row

    by_context: dict[tuple[str, ...], list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for identity, row in lifetimes.items():
        by_context[_context(row)[:4]].append((content_id("position-episode", identity), row))
    if not lifetimes and not observations:
        return result

    assignments: dict[str, str] = {}
    matched: dict[str, list[EpisodeRow]] = defaultdict(list)
    removed: set[str] = set()
    split_links: dict[EpisodeExecutionLink, str] = {}
    unlinked: set[EpisodeExecutionLink] = set()
    partial: dict[str, EpisodeRow] = {}
    original_links: dict[str, list[EpisodeExecutionLink]] = defaultdict(list)
    for link in result.links:
        original_links[link.episode_id].append(link)
    execution_by_id = {row["record_id"]: row for row in executions}
    currencies: dict[str, set[str]] = defaultdict(set)
    for link in result.links:
        currency = execution_by_id[link.execution_record_id].get("settlement_currency", "")
        if currency:
            currencies[link.episode_id].add(currency)
    for episode in result.episodes:
        context = (episode.venue, episode.account_id, episode.market_type, episode.symbol)
        candidates = [
            (identity, row)
            for identity, row in by_context.get(context, ())
            if (not row.get("position_side") or row["position_side"] == episode.direction)
            and (
                not row.get("settlement_currency")
                or not currencies[episode.episode_id]
                or currencies[episode.episode_id] == {row["settlement_currency"]}
            )
            and _overlaps(episode, row, as_of_ms)
        ]
        if len(candidates) == 1:
            identity, _row = candidates[0]
            assignments[episode.episode_id] = identity
            matched[identity].append(episode)
        elif candidates:
            remaining = []
            for link in original_links[episode.episode_id]:
                event = _time(execution_by_id[link.execution_record_id], "event_time_ms") or 0
                owners = [
                    identity
                    for identity, row in candidates
                    if _contains(row, event, link.transition, as_of_ms)
                ]
                if len(owners) == 1:
                    split_links[link] = owners[0]
                    matched[owners[0]].append(replace(episode, boundary_status="left_censored"))
                elif owners:
                    # Retain both proven lifetimes without guessing which owns
                    # an execution whose native position identity is unavailable.
                    unlinked.add(link)
                else:
                    remaining.append(link)
            if not remaining:
                removed.add(episode.episode_id)
            else:
                observed = max(
                    _time(execution_by_id[link.execution_record_id], "event_time_ms") or 0
                    for link in remaining
                )
                closed = episode.closed_at_ms if episode.closed_at_ms == observed else None
                partial[episode.episode_id] = replace(
                    episode,
                    opened_at_ms=None,
                    closed_at_ms=closed,
                    status="closed" if closed else "open",
                    duration_ms=None,
                    duration_bucket="",
                    open_year=None,
                    open_month="",
                    entry_vwap="",
                    exit_vwap="",
                    opened_quantity="",
                    closed_quantity="",
                    maximum_quantity="",
                    remaining_quantity="",
                    close_year=episode.close_year if closed else None,
                    close_month=episode.close_month if closed else "",
                    observed_at_ms=observed or None,
                    boundary_status="left_censored" if closed else "both_censored",
                    execution_count=len({link.execution_record_id for link in remaining}),
                    order_count=len(
                        {
                            execution_by_id[link.execution_record_id].get("order_id")
                            for link in remaining
                        }
                        - {"", None}
                    ),
                )

    links = tuple(
        replace(
            link,
            episode_id=split_links.get(
                link,
                assignments.get(link.episode_id, link.episode_id),
            ),
        )
        for link in result.links
        if link not in unlinked
    )
    links_by_episode: dict[str, list[EpisodeExecutionLink]] = defaultdict(list)
    for link in links:
        links_by_episode[link.episode_id].append(link)
    index = CoverageIndex(list(coverage))
    episodes = [
        partial.get(row.episode_id, row)
        for row in result.episodes
        if row.episode_id not in assignments and row.episode_id not in removed
    ]
    for values in by_context.values():
        for identity, row in values:
            episodes.append(
                _position_episode(
                    row,
                    identity,
                    matched[identity],
                    links_by_episode[identity],
                    execution_by_id,
                    index,
                    as_of_ms,
                )
            )

    # A nonzero observation proves exposure, but does not prove its opening or
    # closing boundary. Repeated observations must not create additional cycles.
    episode_contexts: dict[tuple[str, ...], list[EpisodeRow]] = defaultdict(list)
    for episode in episodes:
        episode_contexts[
            (
                episode.venue,
                episode.account_id,
                episode.market_type,
                episode.symbol,
                episode.direction,
            )
        ].append(episode)
    for context, row in observations.items():
        observed = _observed(row)
        known = episode_contexts.get(context[:5], ())
        if observed and any(
            (episode.opened_at_ms or episode.observed_at_ms or 0)
            <= observed
            <= (episode.closed_at_ms or as_of_ms)
            for episode in known
        ):
            continue
        if not observed and known:
            continue
        identity = content_id("position-observation", context)
        episodes.append(_position_episode(row, identity, (), (), execution_by_id, index, as_of_ms))
    ordered = tuple(sorted(episodes, key=lambda row: (row.sort_time_ms, row.episode_id)))
    return replace(
        result,
        episodes=ordered,
        links=links,
        diagnostics=replace(
            result.diagnostics,
            left_censored_episodes=sum(
                row.boundary_status in {"left_censored", "both_censored"} for row in ordered
            ),
            right_censored_episodes=sum(row.status == "open" for row in ordered),
        ),
    )


def position_cashflow_totals(
    episodes: Sequence[EpisodeRow],
    cashflows: Sequence[dict[str, str]],
    converted: dict[str, tuple[str, Decimal]],
    positions: Sequence[dict[str, str]],
    as_of_ms: int,
) -> tuple[EpisodeCashflowSummary, ...]:
    """Use retained position totals once without changing account cashflow authority."""
    by_position: dict[tuple[str, ...], list[EpisodeRow]] = defaultdict(list)
    for episode in episodes:
        if episode.position_id:
            by_position[
                (
                    episode.venue,
                    episode.account_id,
                    episode.market_type,
                    episode.symbol,
                    episode.settlement_currency,
                    episode.position_id,
                )
            ].append(episode)
    if not by_position:
        return ()
    components: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in cashflows:
        if (
            row.get("event_subtype") != "position_summary"
            or not row.get("position_id")
            or row.get("event_type") not in TRADING_CASHFLOW_TYPES
        ):
            continue
        context = tuple(
            row.get(key, "")
            for key in (
                "venue",
                "account_id",
                "market_type",
                "symbol",
                "currency",
                "position_id",
            )
        )
        matches = by_position.get(context, ())
        if len(matches) == 1:
            key = (matches[0].episode_id, row.get("event_type", ""), row.get("currency", ""))
            previous = components.get(key)
            if previous is None or _revision(row) > _revision(previous):
                components[key] = row
    totals = [
        EpisodeCashflowSummary(
            episode_id=key[0],
            event_type=key[1],
            currency=key[2],
            amount=row["amount"],
            reporting_currency=converted[row["record_id"]][0],
            reporting_amount=decimal_text(converted[row["record_id"]][1]),
            events=1,
        )
        for key, row in sorted(components.items())
    ]
    reported = {row.episode_id for row in totals}
    for row in reversed(sorted(positions, key=_revision)):
        matches = by_position.get(
            (*_context(row)[:4], row.get("settlement_currency", ""), row.get("position_id", "")), ()
        )
        if len(matches) != 1 or matches[0].episode_id in reported:
            continue
        if (_observed(row) or 0) > as_of_ms:
            continue
        values = [row.get(key) for key in ("realized_pnl", "funding", "fees")]
        if not all(value not in (None, "") for value in values):
            continue
        try:
            amounts = [Decimal(value) for value in values]
        except InvalidOperation:
            continue
        if not all(value.is_finite() for value in amounts) or not row.get("settlement_currency"):
            continue
        amount = decimal_text(sum(amounts, Decimal(0)))
        totals.append(
            EpisodeCashflowSummary(
                episode_id=matches[0].episode_id,
                event_type="position_total",
                currency=row["settlement_currency"],
                amount=amount,
                reporting_currency=row["settlement_currency"],
                reporting_amount=amount,
                events=0,
            )
        )
        reported.add(matches[0].episode_id)
    return tuple(totals)


def _position_episode(
    row: dict[str, str],
    identity: str,
    matched: Sequence[EpisodeRow],
    links: Sequence[EpisodeExecutionLink],
    executions: dict[str, dict[str, str]],
    coverage: CoverageIndex,
    as_of_ms: int,
) -> EpisodeRow:
    closed = _time(row, "closed_at_ms")
    if closed and closed > as_of_ms:
        closed = None
    opened = _time(row, "opened_at_ms")
    duration = max(0, (closed or as_of_ms) - opened) if opened else None
    opening = datetime.fromtimestamp(opened / 1000, tz=UTC) if opened else None
    closing = datetime.fromtimestamp(closed / 1000, tz=UTC) if closed else None
    boundary = (
        ("complete" if opened else "left_censored")
        if closed
        else ("right_censored" if opened else "both_censored")
    )
    complete = next((item for item in matched if item.boundary_status == "complete"), None)
    execution_ids = {link.execution_record_id for link in links}
    orders = {executions[key].get("order_id") for key in execution_ids} - {"", None}
    status = "closed" if closed else "open"
    return EpisodeRow(
        episode_id=identity,
        venue=row.get("venue", ""),
        account_id=row.get("account_id", ""),
        market_type=row.get("market_type", ""),
        symbol=row.get("symbol", ""),
        position_mode=complete.position_mode if complete else "",
        direction=row.get("position_side") or (matched[0].direction if matched else ""),
        status=status,
        boundary_status=boundary,
        opened_at_ms=opened,
        closed_at_ms=closed,
        duration_ms=duration,
        duration_bucket=_duration_bucket(duration) if duration is not None else "",
        open_year=opening.year if opening else None,
        open_month=opening.strftime("%Y-%m") if opening else "",
        close_year=closing.year if closing else None,
        close_month=closing.strftime("%Y-%m") if closing else "",
        quantity_unit=row.get("quantity_unit", ""),
        opened_quantity=complete.opened_quantity if complete else "",
        closed_quantity=row.get("quantity", "") if closed else "",
        maximum_quantity=row.get("max_quantity", ""),
        remaining_quantity="0" if closed else row.get("quantity", ""),
        entry_vwap=(
            row["open_price"]
            if _positive(row.get("open_price"))
            else complete.entry_vwap
            if complete
            else ""
        ),
        exit_vwap=(
            row["close_price"]
            if _positive(row.get("close_price"))
            else complete.exit_vwap
            if complete
            else ""
        )
        if closed
        else "",
        execution_count=len(execution_ids),
        order_count=len(orders),
        coverage_status=str(
            coverage.assessment(
                venue=row.get("venue", ""),
                account_id=row.get("account_id", ""),
                dataset="trades",
                start_ms=opened,
                end_ms=closed or as_of_ms,
            )["status"]
        )
        if opened
        else "unknown",
        quality_flags=tuple(
            sorted(
                {
                    flag
                    for episode in matched
                    for flag in episode.quality_flags
                    if flag not in {"left_censored", "right_censored"}
                }
            )
        ),
        position_id=row.get("position_id", ""),
        settlement_currency=row.get("settlement_currency", ""),
        observed_at_ms=_observed(row),
    )


def _context(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        row.get(key, "")
        for key in (
            "venue",
            "account_id",
            "market_type",
            "symbol",
            "position_side",
            "settlement_currency",
        )
    )


def _time(row: dict[str, str], key: str) -> int | None:
    try:
        value = int(row.get(key) or 0)
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def _observed(row: dict[str, str]) -> int | None:
    observed = _time(row, "source_updated_at_ms")
    if observed:
        return observed
    try:
        return int(parse_datetime(row["collected_at"]).timestamp() * 1000)
    except (ValueError, KeyError, TypeError):
        return _time(row, "closed_at_ms") or _time(row, "opened_at_ms")


def _revision(row: dict[str, str]) -> tuple[int, str, str]:
    return (_observed(row) or 0, row.get("collected_at", ""), row.get("record_id", ""))


def _positive(value: str | None) -> bool:
    try:
        return Decimal(value or 0).is_finite() and Decimal(value or 0) > 0
    except InvalidOperation:
        return False


def _overlaps(episode: EpisodeRow, row: dict[str, str], as_of_ms: int) -> bool:
    opened, closed = _time(row, "opened_at_ms"), _time(row, "closed_at_ms")
    if not opened:
        return episode.closed_at_ms == closed
    end = min(closed or as_of_ms, as_of_ms)
    start = episode.opened_at_ms or episode.sort_time_ms
    episode_end = episode.closed_at_ms or as_of_ms
    if start == episode_end:
        return opened < start <= end or opened == end == start
    return start < end and episode_end > opened


def _contains(row: dict[str, str], event: int, transition: str, as_of_ms: int) -> bool:
    opened = _time(row, "opened_at_ms")
    closed = min(_time(row, "closed_at_ms") or as_of_ms, as_of_ms)
    if opened is None:
        return transition == "close" and event == closed
    if opened == closed:
        return event == closed
    return opened <= event < closed if transition == "open" else opened < event <= closed
