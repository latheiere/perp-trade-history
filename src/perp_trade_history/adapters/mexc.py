from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from perp_trade_history.adapters.base import NormalizedRecord, SourceBatch, VenueAdapter
from perp_trade_history.errors import ApiError, ClockSkewError
from perp_trade_history.http import ReadOnlyHttp, ensure_success_payload, mexc_headers
from perp_trade_history.models import (
    bool_text,
    boolean_value,
    decimal_text,
    infer_settlement_currency,
    negative_magnitude,
    row_for,
    source_identity,
    stable_id,
    timestamp_fields,
    utc_now_iso,
)
from perp_trade_history.storage import DataStore, raw_record

MEXC_SIDE = {
    1: ("buy", "open_long", "long"),
    2: ("buy", "close_short", "short"),
    3: ("sell", "open_short", "short"),
    4: ("sell", "close_long", "long"),
}
MEXC_ORDER_STATUS = {
    1: "pending",
    2: "partially_filled",
    3: "filled",
    4: "canceled",
    5: "rejected",
}
MEXC_ORDER_TYPE = {
    1: "limit",
    2: "post_only",
    3: "immediate_or_cancel",
    4: "fill_or_kill",
    5: "market",
    6: "market_to_limit",
}
MEXC_MARGIN_MODE = {1: "isolated", 2: "cross"}
MEXC_TRIGGER_STATUS = {
    1: "untriggered",
    2: "canceled",
    3: "executed",
    4: "invalid",
    5: "execution_failed",
}
MEXC_ACCOUNT_METRICS = (
    "positionMargin",
    "availableBalance",
    "cashBalance",
    "frozenBalance",
    "equity",
    "unrealized",
    "bonus",
)
NINETY_DAYS_MS = 89 * 24 * 60 * 60 * 1000

MEXC_COVERAGE = {
    "account_snapshot": (
        "account_snapshots",
        "point",
        "account",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "history_positions": (
        "position_summaries",
        "partial",
        "account",
        "page history availability is controlled by venue retention",
    ),
    "funding_records": (
        "income",
        "partial",
        "account:funding",
        "page history availability is controlled by venue retention",
    ),
    "transfer_records": (
        "income",
        "partial",
        "account:transfers",
        "page history availability is controlled by venue retention",
    ),
    "history_orders": (
        "orders",
        "complete",
        "account",
        "history is collected through bounded time windows",
    ),
    "open_orders": (
        "orders",
        "point",
        "account",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "trigger_orders": (
        "conditional_orders",
        "complete",
        "account",
        "history is collected through bounded time windows",
    ),
    "stop_orders": (
        "conditional_orders",
        "complete",
        "account",
        "history is collected through bounded time windows",
    ),
    "open_positions": (
        "positions",
        "point",
        "account",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "order_deals": (
        "trades",
        "partial",
        "configured-and-discovered-contracts",
        "fill history is symbol-scoped and depends on historical symbol discovery",
    ),
}


class MexcAdapter(VenueAdapter):
    name = "mexc"

    def finalize_cashflows(self) -> None:
        resolve_mexc_cashflows(self.store, self.venue.account_id)

    def __init__(self, *args: Any, http: ReadOnlyHttp | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.http = http or ReadOnlyHttp(
            self.venue.base_url,
            timeout_seconds=self.config.collection.request_timeout_seconds,
            retries=self.config.collection.request_retries,
            minimum_interval_seconds=0.11,
        )
        self._clock_offset_ms = 0

    def collect(self, *, end_ms: int, full: bool) -> list[SourceBatch]:
        collected_at = utc_now_iso()
        batches: list[SourceBatch] = []

        account_source = "account_snapshot"
        account_start = self.start_for(account_source, full=full)
        batches.append(
            self._attempt(
                account_source,
                account_start,
                end_ms,
                True,
                lambda: self._collect_account_snapshot(
                    source=account_source,
                    collected_at=collected_at,
                ),
            )
        )

        position_source = "history_positions"
        position_start = self.start_for(position_source, full=full)
        positions = self._attempt(
            position_source,
            position_start,
            end_ms,
            True,
            lambda: self._collect_page_source(
                source=position_source,
                path="/api/v1/private/position/list/history_positions",
                params={},
                collected_at=collected_at,
                normalizer=normalize_mexc_position,
            ),
        )
        batches.append(positions)

        funding_source = "funding_records"
        funding_start = self.start_for(funding_source, full=full)
        batches.append(
            self._attempt(
                funding_source,
                funding_start,
                end_ms,
                True,
                lambda: self._collect_page_source(
                    source=funding_source,
                    path="/api/v1/private/position/funding_records",
                    params={},
                    collected_at=collected_at,
                    normalizer=normalize_mexc_funding,
                ),
            )
        )

        transfer_source = "transfer_records"
        transfer_start = self.start_for(transfer_source, full=full)
        batches.append(
            self._attempt(
                transfer_source,
                transfer_start,
                end_ms,
                True,
                lambda: self._collect_page_source(
                    source=transfer_source,
                    path="/api/v1/private/account/transfer_record",
                    params={},
                    collected_at=collected_at,
                    normalizer=normalize_mexc_transfer,
                ),
            )
        )

        order_source = "history_orders"
        order_start = self.start_for(order_source, full=full)
        orders = self._attempt(
            order_source,
            order_start,
            end_ms,
            order_start <= self.config.collection.initial_start_ms,
            lambda: self._collect_window_source(
                source=order_source,
                path="/api/v1/private/order/list/history_orders",
                start_ms=order_start,
                end_ms=end_ms,
                collected_at=collected_at,
                normalizer=normalize_mexc_order,
            ),
        )
        batches.append(orders)

        open_order_source = "open_orders"
        open_order_start = self.start_for(open_order_source, full=full)
        batches.append(
            self._attempt(
                open_order_source,
                open_order_start,
                end_ms,
                True,
                lambda: self._collect_page_source(
                    source=open_order_source,
                    path="/api/v1/private/order/list/open_orders",
                    params={},
                    collected_at=collected_at,
                    normalizer=normalize_mexc_order,
                ),
            )
        )

        conditional_definitions = (
            (
                "trigger_orders",
                "/api/v1/private/planorder/list/orders",
                normalize_mexc_trigger_order,
            ),
            (
                "stop_orders",
                "/api/v1/private/stoporder/list/orders",
                normalize_mexc_stop_order,
            ),
        )
        for source, path, normalizer in conditional_definitions:
            start_ms = self.start_for(source, full=full)
            batches.append(
                self._attempt(
                    source,
                    start_ms,
                    end_ms,
                    start_ms <= self.config.collection.initial_start_ms,
                    lambda source=source, path=path, normalizer=normalizer,
                    start_ms=start_ms: self._collect_window_source(
                        source=source,
                        path=path,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        collected_at=collected_at,
                        normalizer=normalizer,
                    ),
                )
            )

        open_source = "open_positions"
        open_start = self.start_for(open_source, full=full)
        batches.append(
            self._attempt(
                open_source,
                open_start,
                end_ms,
                True,
                lambda: self._collect_list_source(
                    source=open_source,
                    path="/api/v1/private/position/open_positions",
                    params={},
                    collected_at=collected_at,
                    normalizer=normalize_mexc_position,
                ),
            )
        )

        symbols = set(str(value) for value in self.venue.options.get("symbols", []))
        symbols.update(self.store.existing_symbols(self.name, self.venue.account_id))
        for batch in (positions, orders):
            for rows in batch.table_rows.values():
                symbols.update(row["symbol"] for row in rows if row.get("symbol"))

        deal_source = "order_deals"
        deal_start = self.start_for(deal_source, full=full)
        deals = self._attempt(
            deal_source,
            deal_start,
            end_ms,
            deal_start <= self.config.collection.initial_start_ms,
            lambda: self._collect_deals(
                symbols=sorted(symbols),
                start_ms=deal_start,
                end_ms=end_ms,
                collected_at=collected_at,
            ),
        )
        if not symbols and not deals.error:
            deals.warnings.append("no historical symbols were available for fill discovery")
        batches.append(deals)
        return batches

    def _attempt(
        self,
        source: str,
        start_ms: int,
        end_ms: int,
        backfill_complete: bool,
        operation: Callable[[], SourceBatch],
    ) -> SourceBatch:
        skipped = self.circuit_skip_batch(
            source=source, start_ms=start_ms, end_ms=end_ms
        )
        if skipped:
            self._apply_coverage(skipped)
            return skipped
        try:
            batch = operation()
            batch.requested_start_ms = start_ms
            batch.covered_through_ms = end_ms
            batch.backfill_complete = backfill_complete
            self._apply_coverage(batch)
            return batch
        except Exception as exc:  # independent sources must not suppress one another
            batch = self.failure_batch(
                source=source, start_ms=start_ms, end_ms=end_ms, error=exc
            )
            self._apply_coverage(batch)
            return batch

    @staticmethod
    def _apply_coverage(batch: SourceBatch) -> None:
        metadata = MEXC_COVERAGE.get(batch.source)
        if not metadata:
            return
        (
            batch.coverage_dataset,
            batch.coverage_status,
            batch.coverage_scope,
            batch.coverage_limitation,
        ) = metadata

    def _collect_page_source(
        self,
        *,
        source: str,
        path: str,
        params: dict[str, Any],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
    ) -> SourceBatch:
        rows = self._paged(path, params, source)
        return self._normalize_rows(source, rows, collected_at, normalizer)

    def _collect_list_source(
        self,
        *,
        source: str,
        path: str,
        params: dict[str, Any],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
    ) -> SourceBatch:
        payload = self._get(path, params, source)
        rows = _extract_rows(payload)
        return self._normalize_rows(source, rows, collected_at, normalizer)

    def _collect_account_snapshot(
        self, *, source: str, collected_at: str
    ) -> SourceBatch:
        payload = self._get("/api/v1/private/account/assets", {}, source)
        assets = _extract_rows(payload)
        source_id = str(int(time.time() * 1000))
        raw = raw_record(
            venue=self.name,
            account_id=self.venue.account_id,
            source=source,
            source_id=source_id,
            payload=assets,
            collected_at=collected_at,
        )
        observed_at, observed_ms = timestamp_fields(collected_at)
        batch = SourceBatch(self.name, source, 0, 0, False, raw_records=[raw])
        for asset in assets:
            currency = str(asset.get("currency") or "")
            for metric in MEXC_ACCOUNT_METRICS:
                if asset.get(metric) in (None, ""):
                    continue
                metric_source_id = stable_id(source_id, currency, metric)
                batch.add_row(
                    "account_snapshots",
                    row_for(
                        "account_snapshots",
                        record_id=stable_id(
                            "mexc",
                            self.venue.account_id,
                            "snapshot",
                            metric_source_id,
                        ),
                        venue="mexc",
                        account_id=self.venue.account_id,
                        market_type="perpetual",
                        observed_at=observed_at,
                        observed_at_ms=observed_ms,
                        currency=currency,
                        metric=metric,
                        value=decimal_text(asset.get(metric), default="0"),
                        source=source,
                        source_id=metric_source_id,
                        collected_at=collected_at,
                        raw_ref=raw["raw_id"],
                    ),
                )
        return batch

    def _collect_window_source(
        self,
        *,
        source: str,
        path: str,
        start_ms: int,
        end_ms: int,
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        extra_params: dict[str, Any] | None = None,
    ) -> SourceBatch:
        all_rows: list[dict[str, Any]] = []
        # Private contract history rejects intervals older than its rolling
        # reachable window even when each requested slice is short enough.
        cursor = max(start_ms, end_ms - NINETY_DAYS_MS)
        while cursor <= end_ms:
            window_end = min(cursor + NINETY_DAYS_MS, end_ms)
            params = {
                **(extra_params or {}),
                "start_time": cursor,
                "end_time": window_end,
            }
            all_rows.extend(self._paged(path, params, source))
            if window_end >= end_ms:
                break
            cursor = window_end + 1
        return self._normalize_rows(source, _dedupe_rows(all_rows), collected_at, normalizer)

    def _collect_deals(
        self, *, symbols: list[str], start_ms: int, end_ms: int, collected_at: str
    ) -> SourceBatch:
        all_rows: list[dict[str, Any]] = []
        for symbol in symbols:
            batch = self._collect_window_source(
                source="order_deals",
                path="/api/v1/private/order/list/order_deals",
                start_ms=start_ms,
                end_ms=end_ms,
                collected_at=collected_at,
                normalizer=normalize_mexc_deal,
                extra_params={"symbol": symbol},
            )
            all_rows.extend(record["payload"] for record in batch.raw_records)
        return self._normalize_rows(
            "order_deals", _dedupe_rows(all_rows), collected_at, normalize_mexc_deal
        )

    def _normalize_rows(
        self,
        source: str,
        rows: Iterable[dict[str, Any]],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
    ) -> SourceBatch:
        batch = SourceBatch(self.name, source, 0, 0, False)
        for payload in rows:
            normalized = normalizer(
                payload,
                account_id=self.venue.account_id,
                source=source,
                collected_at=collected_at,
            )
            source_id = normalized.source_id
            raw = raw_record(
                venue=self.name,
                account_id=self.venue.account_id,
                source=source,
                source_id=source_id,
                payload=payload,
                collected_at=collected_at,
            )
            batch.raw_records.append(raw)
            for table, table_rows in normalized.table_rows.items():
                for row in table_rows:
                    row["raw_ref"] = raw["raw_id"]
                    batch.add_row(table, row)
        return batch

    def _paged(
        self, path: str, base_params: dict[str, Any], source: str
    ) -> list[dict[str, Any]]:
        limit = min(self.config.collection.page_limit, 100)
        rows: list[dict[str, Any]] = []
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(1, self.config.collection.max_pages_per_query + 1):
            params = {**base_params, "page_num": page, "page_size": limit}
            payload = self._get(path, params, source)
            page_rows, current_page, total_page = _extract_page(payload, page)
            page_identity = tuple(
                source_identity(
                    row,
                    ("id", "tradeId", "orderId", "positionId", "txid"),
                )
                for row in page_rows
            )
            if page_identity and page_identity in seen_pages:
                raise RuntimeError(f"{source} pagination repeated a prior page")
            seen_pages.add(page_identity)
            rows.extend(page_rows)
            if total_page is not None and current_page >= total_page:
                break
            if len(page_rows) < limit:
                break
        else:
            raise RuntimeError(f"{source} exceeded max_pages_per_query")
        return _dedupe_rows(rows)

    def _get(self, path: str, params: dict[str, Any], source: str) -> Any:
        for clock_attempt in range(2):
            timestamp_ms = int(time.time() * 1000) + self._clock_offset_ms
            headers, request_params = mexc_headers(
                params=params,
                api_key=self.venue.api_key,
                api_secret=self.venue.api_secret,
                timestamp_ms=timestamp_ms,
            )
            try:
                payload = self.http.get_json(
                    path, params=request_params, headers=headers
                )
                return ensure_success_payload(
                    payload, venue=self.name, source=source
                )
            except ApiError as exc:
                if str(exc.response_code) != "513" or clock_attempt:
                    raise
                self._synchronize_clock()
        raise AssertionError("unreachable")

    def _synchronize_clock(self) -> None:
        started_ms = int(time.time() * 1000)
        payload = self.http.get_json("/api/v1/contract/ping")
        finished_ms = int(time.time() * 1000)
        server_time: Any = payload
        if isinstance(payload, dict):
            server_time = payload.get("data", payload.get("serverTime"))
        if isinstance(server_time, dict):
            server_time = server_time.get("serverTime", server_time.get("time"))
        try:
            server_ms = int(server_time)
        except (TypeError, ValueError) as exc:
            raise ClockSkewError("MEXC server time response was invalid") from exc
        midpoint_ms = started_ms + (finished_ms - started_ms) // 2
        self._clock_offset_ms = server_ms - midpoint_ms


def normalize_mexc_deal(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id", "tradeId"))
    event_at, event_ms = timestamp_fields(payload.get("timestamp"))
    side, action, position_side = MEXC_SIDE.get(int(payload.get("side") or 0), ("", "", ""))
    symbol = str(payload.get("symbol") or "")
    fee_currency = str(payload.get("feeCurrency") or infer_settlement_currency(symbol))
    execution = row_for(
        "executions",
        record_id=stable_id("mexc", account_id, "execution", source_id),
        venue="mexc",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        symbol=symbol,
        side=side,
        position_action=action,
        position_side=position_side,
        liquidity="taker" if boolean_value(payload.get("isTaker")) else "maker",
        quantity=decimal_text(payload.get("vol")),
        quantity_unit="contract",
        price=decimal_text(payload.get("price")),
        settlement_currency=fee_currency,
        realized_pnl=decimal_text(payload.get("profit"), default="0"),
        fee=negative_magnitude(payload.get("fee")),
        fee_currency=fee_currency,
        order_id=payload.get("orderId"),
        trade_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )
    cashflows: list[dict[str, str]] = []
    profit = decimal_text(payload.get("profit"), default="0")
    if Decimal(profit):
        cashflows.append(
            _mexc_cashflow(
                account_id=account_id,
                source=source,
                source_id=source_id,
                event_time=event_at,
                event_time_ms=event_ms,
                event_type="realized_pnl",
                event_subtype="fill_profit",
                symbol=symbol,
                currency=fee_currency,
                amount=profit,
                order_id=payload.get("orderId"),
                trade_id=source_id,
                position_id=payload.get("positionId"),
                collected_at=collected_at,
            )
        )
    fee = decimal_text(payload.get("fee"), default="0")
    if Decimal(fee):
        cashflows.append(
            _mexc_cashflow(
                account_id=account_id,
                source=source,
                source_id=source_id,
                event_time=event_at,
                event_time_ms=event_ms,
                event_type="commission",
                event_subtype="fill_fee",
                symbol=symbol,
                currency=fee_currency,
                amount=negative_magnitude(fee),
                order_id=payload.get("orderId"),
                trade_id=source_id,
                position_id=payload.get("positionId"),
                collected_at=collected_at,
            )
        )
    return NormalizedRecord(
        source_id,
        {"executions": [execution], "cashflows": cashflows},
    )


def normalize_mexc_order(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("orderId", "id"))
    created_at, created_ms = timestamp_fields(payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("createTime")
    )
    side, action, position_side = MEXC_SIDE.get(int(payload.get("side") or 0), ("", "", ""))
    state = int(payload.get("state") or 0)
    order_type = int(payload.get("orderType") or 0)
    row = row_for(
        "orders",
        record_id=stable_id("mexc", account_id, "order", source_id),
        venue="mexc",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=payload.get("symbol"),
        side=side,
        position_action=action,
        position_side=position_side,
        order_type=MEXC_ORDER_TYPE.get(order_type, str(order_type)),
        time_in_force=_mexc_time_in_force(order_type),
        status=MEXC_ORDER_STATUS.get(state, str(state)),
        quantity=decimal_text(payload.get("vol")),
        quantity_unit="contract",
        filled_quantity=decimal_text(payload.get("dealVol"), default="0"),
        price=decimal_text(payload.get("price")),
        trigger_price=decimal_text(
            payload.get("stopLossPrice") or payload.get("takeProfitPrice")
        ),
        average_price=decimal_text(payload.get("dealAvgPrice")),
        reduce_only=bool_text(payload.get("reduceOnly")),
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=MEXC_MARGIN_MODE.get(int(payload.get("openType") or 0), ""),
        client_order_id=payload.get("externalOid"),
        order_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def normalize_mexc_trigger_order(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id",))
    created_at, created_ms = timestamp_fields(payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("createTime")
    )
    side, action, position_side = MEXC_SIDE.get(
        int(payload.get("side") or 0), ("", "", "")
    )
    state = int(payload.get("state") or 0)
    order_type = int(payload.get("orderType") or 0)
    row = row_for(
        "orders",
        record_id=stable_id("mexc", account_id, "trigger-order", source_id),
        venue="mexc",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=payload.get("symbol"),
        side=side,
        position_action=action,
        position_side=position_side,
        order_type=f"trigger_{MEXC_ORDER_TYPE.get(order_type, str(order_type))}",
        time_in_force=_mexc_time_in_force(order_type),
        status=MEXC_TRIGGER_STATUS.get(state, str(state)),
        quantity=decimal_text(payload.get("vol")),
        quantity_unit="contract",
        price=decimal_text(payload.get("price")),
        trigger_price=decimal_text(payload.get("triggerPrice")),
        trigger_type=stable_id(payload.get("triggerType"), payload.get("trend")),
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=MEXC_MARGIN_MODE.get(int(payload.get("openType") or 0), ""),
        order_id=source_id,
        linked_order_id=payload.get("orderId"),
        failure_reason=(
            str(payload.get("errorCode"))
            if payload.get("errorCode") not in (None, "", 0, "0")
            else ""
        ),
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def normalize_mexc_stop_order(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id",))
    created_at, created_ms = timestamp_fields(payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("createTime")
    )
    position_side = {1: "long", 2: "short"}.get(
        int(payload.get("positionType") or 0), ""
    )
    side = "sell" if position_side == "long" else "buy" if position_side == "short" else ""
    action = f"close_{position_side}" if position_side else "close"
    state = int(payload.get("state") or 0)
    triggers = (
        ("take_profit", payload.get("takeProfitPrice")),
        ("stop_loss", payload.get("stopLossPrice")),
    )
    rows: list[dict[str, str]] = []
    for leg, trigger_price in triggers:
        if trigger_price in (None, "", 0, "0", "0.0"):
            continue
        leg_source_id = stable_id(source_id, leg)
        rows.append(
            row_for(
                "orders",
                record_id=stable_id(
                    "mexc", account_id, "stop-order", leg_source_id
                ),
                venue="mexc",
                account_id=account_id,
                market_type="perpetual",
                created_at=created_at,
                created_at_ms=created_ms,
                updated_at=updated_at,
                updated_at_ms=updated_ms,
                symbol=payload.get("symbol"),
                side=side,
                position_action=action,
                position_side=position_side,
                order_type=leg,
                status=MEXC_TRIGGER_STATUS.get(state, str(state)),
                quantity=decimal_text(payload.get("vol")),
                quantity_unit="contract",
                filled_quantity=decimal_text(payload.get("realityVol"), default="0"),
                trigger_price=decimal_text(trigger_price),
                trigger_type=leg,
                close_only="true",
                order_id=source_id,
                linked_order_id=(
                    payload.get("placeOrderId") or payload.get("orderId")
                ),
                failure_reason=(
                    str(payload.get("errorCode"))
                    if payload.get("errorCode") not in (None, "", 0, "0")
                    else ""
                ),
                source=source,
                source_id=leg_source_id,
                source_updated_at=updated_at,
                source_updated_at_ms=updated_ms,
                collected_at=collected_at,
            )
        )
    if not rows:
        rows.append(
            row_for(
                "orders",
                record_id=stable_id("mexc", account_id, "stop-order", source_id),
                venue="mexc",
                account_id=account_id,
                market_type="perpetual",
                created_at=created_at,
                created_at_ms=created_ms,
                updated_at=updated_at,
                updated_at_ms=updated_ms,
                symbol=payload.get("symbol"),
                side=side,
                position_action=action,
                position_side=position_side,
                order_type="position_stop",
                status=MEXC_TRIGGER_STATUS.get(state, str(state)),
                quantity=decimal_text(payload.get("vol")),
                quantity_unit="contract",
                filled_quantity=decimal_text(payload.get("realityVol"), default="0"),
                close_only="true",
                order_id=source_id,
                linked_order_id=(
                    payload.get("placeOrderId") or payload.get("orderId")
                ),
                source=source,
                source_id=source_id,
                source_updated_at=updated_at,
                source_updated_at_ms=updated_ms,
                collected_at=collected_at,
            )
        )
    return NormalizedRecord(source_id, {"orders": rows})


def normalize_mexc_funding(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id",))
    event_at, event_ms = timestamp_fields(payload.get("settleTime"))
    symbol = str(payload.get("symbol") or "")
    currency = infer_settlement_currency(symbol)
    row = _mexc_cashflow(
        account_id=account_id,
        source=source,
        source_id=source_id,
        event_time=event_at,
        event_time_ms=event_ms,
        event_type="funding",
        event_subtype="funding_settlement",
        symbol=symbol,
        currency=currency,
        amount=decimal_text(payload.get("funding"), default="0"),
        position_id=payload.get("positionId"),
        collected_at=collected_at,
        notes=f"rate={decimal_text(payload.get('rate'))}",
    )
    return NormalizedRecord(source_id, {"cashflows": [row]})


def normalize_mexc_transfer(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id", "txid"))
    event_at, event_ms = timestamp_fields(payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("createTime")
    )
    amount = Decimal(decimal_text(payload.get("amount"), default="0"))
    amount = -abs(amount) if str(payload.get("type") or "").upper() == "OUT" else abs(amount)
    state = str(payload.get("state") or "").upper()
    row = _mexc_cashflow(
        account_id=account_id,
        source=source,
        source_id=source_id,
        event_time=event_at,
        event_time_ms=event_ms,
        event_type="transfer",
        event_subtype=f"{payload.get('type', '')}:{state}".strip(":"),
        currency=str(payload.get("currency") or ""),
        amount=decimal_text(amount),
        collected_at=collected_at,
        reporting_role="primary" if state == "SUCCESS" else "informational",
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        notes=f"txid={payload.get('txid', '')}",
    )
    return NormalizedRecord(source_id, {"cashflows": [row]})


def normalize_mexc_position(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    source_id = source_identity(
        payload,
        ("positionId", "id", "position_id"),
    )
    opened_at, opened_ms = timestamp_fields(payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("createTime")
    )
    state = int(payload.get("state") or 0)
    closed_at, closed_ms = (updated_at, updated_ms) if state == 3 else ("", "")
    symbol = str(payload.get("symbol") or "")
    settlement = infer_settlement_currency(symbol)
    # realised is the net position result, including funding and paid fees.
    # Preserve that total; totalFee can include fees paid with deductions.
    net = Decimal(decimal_text(payload.get("realised"), default="0"))
    funding = Decimal(decimal_text(payload.get("holdFee"), default="0"))
    fee = Decimal(decimal_text(payload.get("fee"), default="0"))
    gross = Decimal(decimal_text(payload.get("closeProfitLoss"), default=str(net - funding - fee)))
    commission = net - gross - funding
    position_side = {1: "long", 2: "short"}.get(int(payload.get("positionType") or 0), "")
    quantity = payload.get("closeVol") if state == 3 else payload.get("holdVol")
    row = row_for(
        "positions",
        record_id=stable_id("mexc", account_id, "position", source_id),
        venue="mexc",
        account_id=account_id,
        market_type="perpetual",
        opened_at=opened_at,
        opened_at_ms=opened_ms,
        closed_at=closed_at,
        closed_at_ms=closed_ms,
        symbol=symbol,
        status="closed" if state == 3 else "open",
        position_side=position_side,
        quantity=decimal_text(quantity, default="0"),
        quantity_unit="contract",
        max_quantity=decimal_text(payload.get("closeVol"), default="0"),
        open_price=decimal_text(payload.get("openAvgPrice") or payload.get("holdAvgPrice")),
        close_price=decimal_text(payload.get("closeAvgPrice")),
        realized_pnl=decimal_text(gross),
        funding=decimal_text(funding),
        fees=decimal_text(commission),
        settlement_currency=settlement,
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=MEXC_MARGIN_MODE.get(int(payload.get("openType") or 0), ""),
        liquidation_price=decimal_text(payload.get("liquidatePrice")),
        position_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    cashflows: list[dict[str, str]] = []
    # Keep zero components so later source revisions can overwrite nonzero amounts.
    components = (
        ("realized_pnl", f"position:{source_id}", gross),
        ("funding", f"position-funding:{source_id}", funding),
        ("commission", f"position-fee:{source_id}", commission),
    ) if payload.get("realised") not in (None, "") else ()
    for event_type, component_id, amount in components:
        cashflows.append(
            _mexc_cashflow(
                account_id=account_id,
                source=source,
                source_id=component_id,
                event_time=updated_at,
                event_time_ms=updated_ms,
                event_type=event_type,
                event_subtype="position_summary",
                symbol=symbol,
                currency=settlement,
                amount=decimal_text(amount),
                position_id=source_id,
                collected_at=collected_at,
                notes="Position total component, dated at source update; not settlement timing.",
            )
        )
    return NormalizedRecord(
        source_id,
        {"positions": [row], "cashflows": cashflows},
    )


def resolve_mexc_cashflows(store: DataStore, account_id: str) -> None:
    """Count retained position totals once, keeping uncovered detail authoritative."""
    cashflows = [
        row for row in store.tables["cashflows"].read()
        if row["venue"] == "mexc" and row["account_id"] == account_id
    ]
    summary_components = {
        (row["position_id"], row["event_type"])
        for row in cashflows
        if row["event_subtype"] == "position_summary" and row["reporting_role"] == "primary"
    }
    positions = {
        row["position_id"]: row for row in store.tables["positions"].read()
        if row["venue"] == "mexc" and row["account_id"] == account_id
    }
    order_positions: dict[str, str] = {}
    for source in ("history_orders", "open_orders"):
        for raw in store.raw.read("mexc", source):
            if raw["account_id"] == account_id and raw["payload"].get("positionId"):
                order_positions[str(raw["payload"]["orderId"])] = str(
                    raw["payload"]["positionId"]
                )
    funding_sides = {
        raw["raw_id"]: {1: "long", 2: "short"}.get(
            int(raw["payload"].get("positionType") or 0), ""
        )
        for raw in store.raw.read("mexc", "funding_records")
        if raw["account_id"] == account_id
    }
    execution_sides = {
        row["trade_id"]: row["position_side"]
        for row in store.tables["executions"].read()
        if row["venue"] == "mexc" and row["account_id"] == account_id
    }
    changed = []
    for row in cashflows:
        if row["event_subtype"] not in {"fill_profit", "fill_fee", "funding_settlement"}:
            continue
        timestamp = int(row["event_time_ms"] or 0)
        position_id = row["position_id"] or order_positions.get(row["order_id"], "")
        candidates = []
        for position in positions.values():
            if (position["position_id"], row["event_type"]) not in summary_components:
                continue
            if position["symbol"] != row["symbol"]:
                continue
            if position["settlement_currency"] != row["currency"]:
                continue
            if not (
                int(position["opened_at_ms"] or 0) <= timestamp
                <= int(position["source_updated_at_ms"] or 0)
            ):
                continue
            if position_id:
                matches = position["position_id"] == position_id
            else:
                # Responses can omit positionId; contract, side and lifetime
                # must identify exactly one position. Never match on amount alone.
                side = funding_sides.get(row["raw_ref"], "") or execution_sides.get(
                    row["trade_id"], ""
                )
                matches = bool(side) and side == position["position_side"]
            if matches:
                candidates.append(position)
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous MEXC position membership: {row['record_id']}")
        updated = dict(row)
        updated["reporting_role"] = "informational" if candidates else "primary"
        if candidates:
            updated["position_id"] = candidates[0]["position_id"]
        if updated != row:
            changed.append(updated)
    store.upsert("cashflows", changed)


def _mexc_cashflow(
    *,
    account_id: str,
    source: str,
    source_id: str,
    event_time: str,
    event_time_ms: str,
    event_type: str,
    event_subtype: str,
    currency: str,
    amount: str,
    collected_at: str,
    symbol: str = "",
    order_id: Any = "",
    trade_id: Any = "",
    position_id: Any = "",
    reporting_role: str = "primary",
    source_updated_at: str = "",
    source_updated_at_ms: str = "",
    notes: str = "",
) -> dict[str, str]:
    return row_for(
        "cashflows",
        record_id=stable_id("mexc", account_id, "cashflow", event_type, source_id),
        venue="mexc",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_time,
        event_time_ms=event_time_ms,
        event_type=event_type,
        event_subtype=event_subtype,
        symbol=symbol,
        currency=currency,
        amount=amount,
        order_id=order_id,
        trade_id=trade_id,
        position_id=position_id,
        reporting_role=reporting_role,
        source=source,
        source_id=source_id,
        source_updated_at=source_updated_at or event_time,
        source_updated_at_ms=source_updated_at_ms or event_time_ms,
        collected_at=collected_at,
        notes=notes,
    )


def _extract_page(
    payload: Any, requested_page: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("resultList"), list):
        rows = [row for row in data["resultList"] if isinstance(row, dict)]
        current = int(data.get("currentPage") or requested_page)
        total = int(data.get("totalPage") or current)
        return rows, current, total
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], requested_page, None
    if isinstance(data, dict):
        return [data], requested_page, requested_page
    return [], requested_page, requested_page


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    rows, _, _ = _extract_page(payload, 1)
    return rows


def _dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = source_identity(
            row,
            ("id", "tradeId", "orderId", "positionId", "txid"),
        )
        by_id[identity] = row
    return list(by_id.values())


def _mexc_time_in_force(order_type: int) -> str:
    return {2: "post_only", 3: "ioc", 4: "fok"}.get(order_type, "")
