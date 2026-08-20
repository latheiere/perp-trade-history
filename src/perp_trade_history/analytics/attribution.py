from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from perp_trade_history.analytics.constants import (
    CAPITAL_MOVEMENT_TYPES,
    TRADING_CASHFLOW_TYPES,
)
from perp_trade_history.analytics.schema import (
    AccountCashflowSummary,
    CashflowAttribution,
    EpisodeCashflowSummary,
    EpisodeExecutionLink,
    EpisodeRow,
)
from perp_trade_history.models import decimal_text


@dataclass(frozen=True, slots=True)
class AttributionResult:
    attributions: tuple[CashflowAttribution, ...]
    episode_summaries: tuple[EpisodeCashflowSummary, ...]
    account_summaries: tuple[AccountCashflowSummary, ...]
    unattributed_primary_cashflows: int
    ambiguous_primary_cashflows: int


def attribute_cashflows(
    *,
    cashflows: Sequence[dict[str, str]],
    executions: Sequence[dict[str, str]],
    episodes: Sequence[EpisodeRow],
    links: Sequence[EpisodeExecutionLink],
    converted: Mapping[str, tuple[str, Decimal]],
) -> AttributionResult:
    execution_links: dict[str, list[EpisodeExecutionLink]] = defaultdict(list)
    for link in links:
        execution_links[link.execution_record_id].append(link)

    trades: dict[tuple[str, ...], list[str]] = defaultdict(list)
    orders: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row in executions:
        if row.get("trade_id"):
            trades[_context_identifier(row, "trade_id")].append(row["record_id"])
        if row.get("order_id"):
            orders[_context_identifier(row, "order_id")].append(row["record_id"])

    episodes_by_context: dict[tuple[str, ...], list[EpisodeRow]] = defaultdict(list)
    for episode in episodes:
        episodes_by_context[
            (
                episode.venue,
                episode.account_id,
                episode.market_type,
                episode.symbol,
            )
        ].append(episode)

    attributions: list[CashflowAttribution] = []
    unattributed = 0
    ambiguous = 0
    for row in sorted(
        cashflows,
        key=lambda item: (
            int(item.get("event_time_ms") or 0),
            item.get("record_id", ""),
        ),
    ):
        amount = _decimal(row.get("amount"))
        reporting_currency, reporting_amount = converted[row["record_id"]]
        role = row.get("reporting_role", "")
        event_type = row.get("event_type", "")
        if role != "primary":
            attributions.append(
                _attribution(
                    row,
                    episode_id="",
                    method="excluded_role",
                    confidence="none",
                    reason="Only primary cashflows contribute to canonical episode performance.",
                    amount=amount,
                    reporting_currency=reporting_currency,
                    reporting_amount=reporting_amount,
                    fraction=Decimal(1),
                )
            )
            continue
        if event_type not in TRADING_CASHFLOW_TYPES:
            method = (
                "excluded_capital_movement"
                if event_type in CAPITAL_MOVEMENT_TYPES
                else "excluded_event_type"
            )
            attributions.append(
                _attribution(
                    row,
                    episode_id="",
                    method=method,
                    confidence="none",
                    reason="The cashflow category is outside episode trading performance.",
                    amount=amount,
                    reporting_currency=reporting_currency,
                    reporting_amount=reporting_amount,
                    fraction=Decimal(1),
                )
            )
            continue

        exact_links: list[EpisodeExecutionLink] = []
        exact_method = ""
        if row.get("trade_id"):
            execution_ids = trades.get(_context_identifier(row, "trade_id"), [])
            if len(execution_ids) == 1:
                exact_links = execution_links.get(execution_ids[0], [])
                exact_method = "exact_trade"
        if not exact_links and row.get("order_id"):
            execution_ids = orders.get(_context_identifier(row, "order_id"), [])
            exact_links = [
                link
                for execution_id in execution_ids
                for link in execution_links.get(execution_id, [])
            ]
            if exact_links:
                exact_method = "exact_order"
        if exact_links:
            selected_links = exact_links
            if event_type == "realized_pnl":
                closing_links = [link for link in exact_links if link.transition == "close"]
                if closing_links:
                    selected_links = closing_links
            allocations = _episode_allocations(selected_links)
            remaining_amount = amount
            remaining_reporting_amount = reporting_amount
            for index, (episode_id, fraction) in enumerate(allocations):
                is_last = index == len(allocations) - 1
                allocated_amount = (
                    remaining_amount if is_last else amount * fraction
                )
                allocated_reporting_amount = (
                    remaining_reporting_amount
                    if is_last
                    else reporting_amount * fraction
                )
                attributions.append(
                    _attribution(
                        row,
                        episode_id=episode_id,
                        method=exact_method,
                        confidence="exact" if len(allocations) == 1 else "derived",
                        reason="The cashflow resolves through execution identity.",
                        amount=allocated_amount,
                        reporting_currency=reporting_currency,
                        reporting_amount=allocated_reporting_amount,
                        fraction=fraction,
                    )
                )
                remaining_amount -= allocated_amount
                remaining_reporting_amount -= allocated_reporting_amount
            continue

        if not row.get("symbol"):
            unattributed += 1
            attributions.append(
                _attribution(
                    row,
                    episode_id="",
                    method="account_scoped",
                    confidence="none",
                    reason="The cashflow has no instrument boundary.",
                    amount=amount,
                    reporting_currency=reporting_currency,
                    reporting_amount=reporting_amount,
                    fraction=Decimal(1),
                )
            )
            continue

        event_time_ms = int(row.get("event_time_ms") or 0)
        candidates = [
            episode
            for episode in episodes_by_context.get(_context(row), [])
            if episode.opened_at_ms <= event_time_ms
            and (episode.closed_at_ms is None or event_time_ms < episode.closed_at_ms)
        ]
        if len(candidates) == 1:
            attributions.append(
                _attribution(
                    row,
                    episode_id=candidates[0].episode_id,
                    method="temporal_unique",
                    confidence="derived",
                    reason="Exactly one episode is active for the instrument at the event time.",
                    amount=amount,
                    reporting_currency=reporting_currency,
                    reporting_amount=reporting_amount,
                    fraction=Decimal(1),
                )
            )
        else:
            unattributed += 1
            if len(candidates) > 1:
                ambiguous += 1
            attributions.append(
                _attribution(
                    row,
                    episode_id="",
                    method="ambiguous_temporal" if candidates else "outside_episode",
                    confidence="ambiguous" if candidates else "none",
                    reason=(
                        "More than one episode is active for the instrument at the event time."
                        if candidates
                        else "No episode is active for the instrument at the event time."
                    ),
                    amount=amount,
                    reporting_currency=reporting_currency,
                    reporting_amount=reporting_amount,
                    fraction=Decimal(1),
                )
            )

    ordered = tuple(
        sorted(
            attributions,
            key=lambda item: (
                item.cashflow_record_id,
                item.episode_id,
                item.method,
            ),
        )
    )
    return AttributionResult(
        attributions=ordered,
        episode_summaries=_episode_summaries(ordered),
        account_summaries=_account_summaries(cashflows, ordered, converted),
        unattributed_primary_cashflows=unattributed,
        ambiguous_primary_cashflows=ambiguous,
    )


def _episode_allocations(
    links: Sequence[EpisodeExecutionLink],
) -> list[tuple[str, Decimal]]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    for link in links:
        quantities[link.episode_id] += Decimal(link.quantity)
    total = sum(quantities.values(), Decimal(0))
    if not total:
        return []
    ordered = sorted(quantities.items())
    allocations: list[tuple[str, Decimal]] = []
    consumed = Decimal(0)
    for index, (episode_id, quantity) in enumerate(ordered):
        fraction = (
            Decimal(1) - consumed
            if index == len(ordered) - 1
            else quantity / total
        )
        allocations.append((episode_id, fraction))
        consumed += fraction
    return allocations


def _attribution(
    row: dict[str, str],
    *,
    episode_id: str,
    method: str,
    confidence: str,
    reason: str,
    amount: Decimal,
    reporting_currency: str,
    reporting_amount: Decimal,
    fraction: Decimal,
) -> CashflowAttribution:
    return CashflowAttribution(
        cashflow_record_id=row["record_id"],
        episode_id=episode_id,
        method=method,
        confidence=confidence,
        reason=reason,
        reporting_role=row.get("reporting_role", ""),
        event_type=row.get("event_type", ""),
        currency=row.get("currency", ""),
        amount=decimal_text(amount),
        reporting_currency=reporting_currency,
        reporting_amount=decimal_text(reporting_amount),
        fraction=decimal_text(fraction),
    )


def _episode_summaries(
    attributions: Sequence[CashflowAttribution],
) -> tuple[EpisodeCashflowSummary, ...]:
    totals: dict[tuple[str, ...], tuple[Decimal, Decimal, set[str]]] = {}
    for item in attributions:
        if not item.episode_id:
            continue
        key = (
            item.episode_id,
            item.event_type,
            item.currency,
            item.reporting_currency,
        )
        amount, reporting_amount, events = totals.get(
            key, (Decimal(0), Decimal(0), set())
        )
        events.add(item.cashflow_record_id)
        totals[key] = (
            amount + Decimal(item.amount),
            reporting_amount + Decimal(item.reporting_amount),
            events,
        )
    return tuple(
        EpisodeCashflowSummary(
            episode_id=key[0],
            event_type=key[1],
            currency=key[2],
            amount=decimal_text(values[0]),
            reporting_currency=key[3],
            reporting_amount=decimal_text(values[1]),
            events=len(values[2]),
        )
        for key, values in sorted(totals.items())
    )


def _account_summaries(
    cashflows: Sequence[dict[str, str]],
    attributions: Sequence[CashflowAttribution],
    converted: Mapping[str, tuple[str, Decimal]],
) -> tuple[AccountCashflowSummary, ...]:
    by_record: dict[str, list[CashflowAttribution]] = defaultdict(list)
    for item in attributions:
        by_record[item.cashflow_record_id].append(item)
    totals: dict[tuple[str, ...], dict[str, Decimal | int]] = {}
    for row in cashflows:
        classification = _classification(row)
        reporting_currency, reporting_amount = converted[row["record_id"]]
        key = (
            row.get("venue", ""),
            row.get("account_id", ""),
            row.get("event_type", ""),
            row.get("reporting_role", ""),
            classification,
            row.get("currency", ""),
            reporting_currency,
        )
        target = totals.setdefault(
            key,
            {
                "amount": Decimal(0),
                "reporting_amount": Decimal(0),
                "attributed": Decimal(0),
                "unattributed": Decimal(0),
                "excluded": Decimal(0),
                "attributed_reporting": Decimal(0),
                "unattributed_reporting": Decimal(0),
                "excluded_reporting": Decimal(0),
                "events": 0,
            },
        )
        amount = _decimal(row.get("amount"))
        target["amount"] += amount
        target["reporting_amount"] += reporting_amount
        target["events"] += 1
        items = by_record[row["record_id"]]
        if classification == "trading_primary":
            attributed_amount = sum(
                (Decimal(item.amount) for item in items if item.episode_id),
                Decimal(0),
            )
            attributed_reporting_amount = sum(
                (
                    Decimal(item.reporting_amount)
                    for item in items
                    if item.episode_id
                ),
                Decimal(0),
            )
            target["attributed"] += attributed_amount
            target["unattributed"] += amount - attributed_amount
            target["attributed_reporting"] += attributed_reporting_amount
            target["unattributed_reporting"] += (
                reporting_amount - attributed_reporting_amount
            )
        else:
            target["excluded"] += amount
            target["excluded_reporting"] += reporting_amount
    return tuple(
        AccountCashflowSummary(
            venue=key[0],
            account_id=key[1],
            event_type=key[2],
            reporting_role=key[3],
            classification=key[4],
            currency=key[5],
            amount=decimal_text(values["amount"]),
            reporting_currency=key[6],
            reporting_amount=decimal_text(values["reporting_amount"]),
            attributed_amount=decimal_text(values["attributed"]),
            unattributed_amount=decimal_text(values["unattributed"]),
            excluded_amount=decimal_text(values["excluded"]),
            attributed_reporting_amount=decimal_text(values["attributed_reporting"]),
            unattributed_reporting_amount=decimal_text(values["unattributed_reporting"]),
            excluded_reporting_amount=decimal_text(values["excluded_reporting"]),
            events=int(values["events"]),
        )
        for key, values in sorted(totals.items())
    )


def _classification(row: dict[str, str]) -> str:
    if row.get("reporting_role") != "primary":
        return "non_authoritative"
    if row.get("event_type") in CAPITAL_MOVEMENT_TYPES:
        return "capital_movement"
    if row.get("event_type") in TRADING_CASHFLOW_TYPES:
        return "trading_primary"
    return "other_primary"


def _context_identifier(row: dict[str, str], identifier: str) -> tuple[str, ...]:
    return (*_context(row), row.get(identifier, ""))


def _context(row: dict[str, str]) -> tuple[str, ...]:
    return (
        row.get("venue", ""),
        row.get("account_id", ""),
        row.get("market_type", ""),
        row.get("symbol", ""),
    )


def _decimal(value: str | None) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal(0)
