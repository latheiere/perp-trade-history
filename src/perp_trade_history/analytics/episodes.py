from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from perp_trade_history.analytics.constants import EXPLICIT_ACTIONS, NET_ACTIONS
from perp_trade_history.analytics.ordering import (
    deterministic_execution_order,
    execution_mode,
    explicit_direction,
    profile_tied_transitions,
)
from perp_trade_history.analytics.schema import EpisodeExecutionLink, EpisodeRow
from perp_trade_history.models import content_id, decimal_text
from perp_trade_history.storage import CoverageIndex


@dataclass(frozen=True, slots=True)
class EpisodeBuildDiagnostics:
    skipped_invalid_executions: int
    unsupported_action_executions: int
    orphan_close_events: int
    oversized_close_events: int
    mixed_mode_partitions: int
    left_censored_episodes: int
    right_censored_episodes: int


@dataclass(frozen=True, slots=True)
class EpisodeBuildResult:
    episodes: tuple[EpisodeRow, ...]
    links: tuple[EpisodeExecutionLink, ...]
    diagnostics: EpisodeBuildDiagnostics


@dataclass(slots=True)
class _EpisodeState:
    episode_id: str
    venue: str
    account_id: str
    market_type: str
    symbol: str
    position_mode: str
    direction: str
    opened_at_ms: int
    quantity_unit: str
    left_censored: bool = False
    closed_at_ms: int | None = None
    current_quantity: Decimal = Decimal(0)
    opened_quantity: Decimal = Decimal(0)
    closed_quantity: Decimal = Decimal(0)
    maximum_quantity: Decimal = Decimal(0)
    entry_value: Decimal = Decimal(0)
    exit_value: Decimal = Decimal(0)
    entry_quantity: Decimal = Decimal(0)
    exit_quantity: Decimal = Decimal(0)
    execution_ids: set[str] = field(default_factory=set)
    order_ids: set[str] = field(default_factory=set)
    quality_flags: set[str] = field(default_factory=set)


class EpisodeBuilder:
    """Reconstruct position episodes from normalized execution facts."""

    def __init__(
        self,
        *,
        as_of_ms: int,
        coverage_rows: Iterable[dict[str, str]] = (),
    ):
        self.as_of_ms = as_of_ms
        self.coverage_index = CoverageIndex(list(coverage_rows))
        self._episodes: list[_EpisodeState] = []
        self._links: list[EpisodeExecutionLink] = []
        self._explicit_active: dict[tuple[str, ...], _EpisodeState] = {}
        self._net_active: dict[tuple[str, ...], _EpisodeState] = {}
        self._skipped_invalid = 0
        self._unsupported = 0
        self._orphan_closes = 0
        self._oversized_closes = 0

    def build(self, rows: Iterable[dict[str, str]]) -> EpisodeBuildResult:
        selected = [row for row in rows if _at_or_before(row, self.as_of_ms)]
        tied = profile_tied_transitions(selected)
        mode_partitions: dict[tuple[str, ...], set[str]] = {}
        for row in selected:
            partition = (
                row.get("venue", ""),
                row.get("account_id", ""),
                row.get("market_type", ""),
                row.get("symbol", ""),
            )
            mode_partitions.setdefault(partition, set()).add(execution_mode(row))

        for row in deterministic_execution_order(selected):
            if not _valid_execution(row):
                self._skipped_invalid += 1
                continue
            action = row.get("position_action", "")
            if action in EXPLICIT_ACTIONS:
                self._consume_explicit(
                    row,
                    row.get("record_id", "") in tied.ambiguous_execution_ids,
                )
            elif action in NET_ACTIONS:
                self._consume_net(row, row.get("record_id", "") in tied.ambiguous_execution_ids)
            else:
                self._unsupported += 1

        episode_rows = tuple(
            sorted(
                (self._finish(state) for state in self._episodes),
                key=lambda episode: (episode.opened_at_ms, episode.episode_id),
            )
        )
        links = tuple(
            sorted(
                self._links,
                key=lambda link: (
                    link.execution_record_id,
                    link.episode_id,
                    link.transition,
                ),
            )
        )
        return EpisodeBuildResult(
            episodes=episode_rows,
            links=links,
            diagnostics=EpisodeBuildDiagnostics(
                skipped_invalid_executions=self._skipped_invalid,
                unsupported_action_executions=self._unsupported,
                orphan_close_events=self._orphan_closes,
                oversized_close_events=self._oversized_closes,
                mixed_mode_partitions=sum(
                    len(modes - {"unsupported"}) > 1 for modes in mode_partitions.values()
                ),
                left_censored_episodes=sum(
                    episode.boundary_status in {"left_censored", "both_censored"}
                    for episode in episode_rows
                ),
                right_censored_episodes=sum(
                    episode.boundary_status in {"right_censored", "both_censored"}
                    for episode in episode_rows
                ),
            ),
        )

    def _consume_explicit(self, row: dict[str, str], ambiguous_tie: bool) -> None:
        action = row["position_action"]
        direction = explicit_direction(action)
        key = _execution_context(row) + ("explicit_side", direction)
        quantity = Decimal(row["quantity"])
        if action.startswith("open_"):
            state = self._explicit_active.get(key)
            if state is None:
                state = self._new_state(row, "explicit_side", direction)
                self._explicit_active[key] = state
            self._open(state, row, quantity, ambiguous_tie)
            return

        state = self._explicit_active.get(key)
        if state is None:
            self._orphan_closes += 1
            state = self._new_state(
                row, "explicit_side", direction, left_censored=True
            )
            state.current_quantity = quantity
            state.opened_quantity = quantity
            state.maximum_quantity = quantity
            self._explicit_active[key] = state
        if quantity > state.current_quantity:
            self._oversized_closes += 1
        closing = min(quantity, state.current_quantity)
        self._close(state, row, closing, ambiguous_tie)
        remainder = quantity - closing
        if state.current_quantity == 0:
            self._explicit_active.pop(key, None)
        if remainder:
            inferred = self._new_state(
                row, "explicit_side", direction, left_censored=True
            )
            inferred.current_quantity = remainder
            inferred.opened_quantity = remainder
            inferred.maximum_quantity = remainder
            self._close(inferred, row, remainder, ambiguous_tie)

    def _consume_net(self, row: dict[str, str], ambiguous_tie: bool) -> None:
        key = _execution_context(row) + ("net",)
        quantity = Decimal(row["quantity"])
        delta = quantity if row["position_action"] == "buy" else -quantity
        state = self._net_active.get(key)
        direction = "long" if delta > 0 else "short"
        if state is None:
            state = self._new_state(row, "net", direction)
            self._net_active[key] = state
            self._open(state, row, abs(delta), ambiguous_tie)
            return

        same_direction = (state.direction == "long" and delta > 0) or (
            state.direction == "short" and delta < 0
        )
        if same_direction:
            self._open(state, row, abs(delta), ambiguous_tie)
            return

        closing = min(state.current_quantity, abs(delta))
        self._close(state, row, closing, ambiguous_tie)
        remainder = abs(delta) - closing
        if state.current_quantity == 0:
            self._net_active.pop(key, None)
        if remainder:
            next_state = self._new_state(row, "net", direction)
            self._net_active[key] = next_state
            self._open(next_state, row, remainder, ambiguous_tie)

    def _new_state(
        self,
        row: dict[str, str],
        position_mode: str,
        direction: str,
        *,
        left_censored: bool = False,
    ) -> _EpisodeState:
        identity = (
            *_execution_context(row),
            position_mode,
            direction,
            row.get("record_id", ""),
        )
        state = _EpisodeState(
            episode_id=content_id("episode", identity),
            venue=row.get("venue", ""),
            account_id=row.get("account_id", ""),
            market_type=row.get("market_type", ""),
            symbol=row.get("symbol", ""),
            position_mode=position_mode,
            direction=direction,
            opened_at_ms=int(row["event_time_ms"]),
            quantity_unit=row.get("quantity_unit", ""),
            left_censored=left_censored,
        )
        if left_censored:
            state.quality_flags.add("left_censored")
        self._episodes.append(state)
        return state

    def _open(
        self,
        state: _EpisodeState,
        row: dict[str, str],
        quantity: Decimal,
        ambiguous_tie: bool,
    ) -> None:
        self._observe(state, row, ambiguous_tie)
        price = Decimal(row["price"])
        state.current_quantity += quantity
        state.opened_quantity += quantity
        state.entry_quantity += quantity
        state.entry_value += quantity * price
        state.maximum_quantity = max(state.maximum_quantity, state.current_quantity)
        self._links.append(
            EpisodeExecutionLink(
                episode_id=state.episode_id,
                execution_record_id=row["record_id"],
                transition="open",
                quantity=decimal_text(quantity),
            )
        )

    def _close(
        self,
        state: _EpisodeState,
        row: dict[str, str],
        quantity: Decimal,
        ambiguous_tie: bool,
    ) -> None:
        if not quantity:
            return
        self._observe(state, row, ambiguous_tie)
        price = Decimal(row["price"])
        state.current_quantity -= quantity
        state.closed_quantity += quantity
        state.exit_quantity += quantity
        state.exit_value += quantity * price
        self._links.append(
            EpisodeExecutionLink(
                episode_id=state.episode_id,
                execution_record_id=row["record_id"],
                transition="close",
                quantity=decimal_text(quantity),
            )
        )
        if state.current_quantity == 0:
            state.closed_at_ms = int(row["event_time_ms"])

    @staticmethod
    def _observe(
        state: _EpisodeState, row: dict[str, str], ambiguous_tie: bool
    ) -> None:
        state.execution_ids.add(row["record_id"])
        if row.get("order_id"):
            state.order_ids.add(row["order_id"])
        if row.get("quantity_unit") != state.quantity_unit:
            state.quality_flags.add("mixed_quantity_units")
        if ambiguous_tie:
            state.quality_flags.add("ambiguous_tie_order")

    def _finish(self, state: _EpisodeState) -> EpisodeRow:
        is_open = state.closed_at_ms is None
        if state.left_censored and is_open:
            boundary_status = "both_censored"
        elif state.left_censored:
            boundary_status = "left_censored"
        elif is_open:
            boundary_status = "right_censored"
        else:
            boundary_status = "complete"
        end_ms = state.closed_at_ms if state.closed_at_ms is not None else self.as_of_ms
        coverage = self.coverage_index.assessment(
            venue=state.venue,
            account_id=state.account_id,
            dataset="trades",
            start_ms=state.opened_at_ms,
            end_ms=end_ms,
        )["status"]
        opened = _instant(state.opened_at_ms)
        closed = _instant(state.closed_at_ms) if state.closed_at_ms is not None else None
        flags = set(state.quality_flags)
        if is_open:
            flags.add("right_censored")
        return EpisodeRow(
            episode_id=state.episode_id,
            venue=state.venue,
            account_id=state.account_id,
            market_type=state.market_type,
            symbol=state.symbol,
            position_mode=state.position_mode,
            direction=state.direction,
            status="open" if is_open else "closed",
            boundary_status=boundary_status,
            opened_at_ms=state.opened_at_ms,
            closed_at_ms=state.closed_at_ms,
            duration_ms=max(0, end_ms - state.opened_at_ms),
            duration_bucket=_duration_bucket(max(0, end_ms - state.opened_at_ms)),
            open_year=opened.year,
            open_month=f"{opened.year:04d}-{opened.month:02d}",
            close_year=closed.year if closed else None,
            close_month=f"{closed.year:04d}-{closed.month:02d}" if closed else "",
            quantity_unit=state.quantity_unit,
            opened_quantity=decimal_text(state.opened_quantity),
            closed_quantity=decimal_text(state.closed_quantity),
            maximum_quantity=decimal_text(state.maximum_quantity),
            remaining_quantity=decimal_text(state.current_quantity),
            entry_vwap=(
                decimal_text(state.entry_value / state.entry_quantity)
                if state.entry_quantity and not state.left_censored
                else ""
            ),
            exit_vwap=(
                decimal_text(state.exit_value / state.exit_quantity)
                if state.exit_quantity
                else ""
            ),
            execution_count=len(state.execution_ids),
            order_count=len(state.order_ids),
            coverage_status=str(coverage),
            quality_flags=tuple(sorted(flags)),
        )


def _execution_context(row: dict[str, str]) -> tuple[str, ...]:
    return (
        row.get("venue", ""),
        row.get("account_id", ""),
        row.get("market_type", ""),
        row.get("symbol", ""),
    )


def _valid_execution(row: dict[str, str]) -> bool:
    try:
        return (
            int(row.get("event_time_ms") or 0) > 0
            and Decimal(row.get("quantity") or "0") > 0
            and Decimal(row.get("price") or "0") > 0
            and bool(row.get("record_id"))
        )
    except (InvalidOperation, ValueError):
        return False


def _at_or_before(row: dict[str, str], as_of_ms: int) -> bool:
    try:
        return int(row.get("event_time_ms") or 0) <= as_of_ms
    except ValueError:
        return True


def _instant(value: int | None) -> datetime:
    return datetime.fromtimestamp((value or 0) / 1000, tz=UTC)


def _duration_bucket(duration_ms: int) -> str:
    hour = 60 * 60 * 1000
    day = 24 * hour
    if duration_ms < hour:
        return "under_1h"
    if duration_ms < day:
        return "1h_to_1d"
    if duration_ms < 7 * day:
        return "1d_to_7d"
    if duration_ms < 30 * day:
        return "7d_to_30d"
    return "30d_or_more"
