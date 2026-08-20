from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from perp_trade_history.analytics.constants import EXPLICIT_ACTIONS, NET_ACTIONS


@dataclass(frozen=True, slots=True)
class TiedTransitionProfile:
    tied_groups: int
    opposing_groups: int
    ambiguous_execution_ids: frozenset[str]


def execution_mode(row: dict[str, str]) -> str:
    action = row.get("position_action", "")
    if action in EXPLICIT_ACTIONS:
        return "explicit_side"
    if action in NET_ACTIONS:
        return "net"
    return "unsupported"


def explicit_direction(action: str) -> str:
    if action.endswith("long"):
        return "long"
    if action.endswith("short"):
        return "short"
    return ""


def transition_delta(row: dict[str, str]) -> Decimal:
    try:
        quantity = Decimal(row.get("quantity") or "0")
    except InvalidOperation:
        return Decimal(0)
    action = row.get("position_action", "")
    if action.startswith("open_") or action == "buy":
        return quantity
    if action.startswith("close_") or action == "sell":
        return -quantity
    return Decimal(0)


def deterministic_execution_order(
    rows: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("event_time_ms") or 0),
            row.get("record_id", ""),
        ),
    )


def profile_tied_transitions(rows: Iterable[dict[str, str]]) -> TiedTransitionProfile:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        mode = execution_mode(row)
        direction = (
            explicit_direction(row.get("position_action", ""))
            if mode == "explicit_side"
            else "net"
        )
        key = (
            row.get("venue", ""),
            row.get("account_id", ""),
            row.get("market_type", ""),
            row.get("symbol", ""),
            mode,
            direction,
            row.get("event_time_ms", ""),
        )
        groups[key].append(row)

    tied_groups = 0
    opposing_groups = 0
    ambiguous_ids: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        tied_groups += 1
        signs = {delta.compare(Decimal(0)) for delta in map(transition_delta, group)}
        if Decimal(-1) in signs and Decimal(1) in signs:
            opposing_groups += 1
            ambiguous_ids.update(row.get("record_id", "") for row in group)
    return TiedTransitionProfile(
        tied_groups=tied_groups,
        opposing_groups=opposing_groups,
        ambiguous_execution_ids=frozenset(ambiguous_ids),
    )
