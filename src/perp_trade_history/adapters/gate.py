from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from perp_trade_history.adapters.base import NormalizedRecord, SourceBatch, VenueAdapter
from perp_trade_history.http import ReadOnlyHttp, gate_headers
from perp_trade_history.models import (
    bool_text,
    boolean_value,
    content_id,
    decimal_text,
    negative_magnitude,
    row_for,
    source_identity,
    stable_id,
    timestamp_fields,
    utc_now_iso,
)
from perp_trade_history.storage import raw_record

DAY_MS = 24 * 60 * 60 * 1000
GATE_REST_RETENTION_MS = 180 * DAY_MS
GATE_ACCOUNT_BOOK_WINDOW_MS = 30 * DAY_MS - 1000
GATE_HISTORY_WINDOW_MS = 180 * DAY_MS - 1000

GATE_ACCOUNT_TYPES = {
    "dnw": "transfer",
    "pnl": "realized_pnl",
    "fee": "commission",
    "refr": "rebate",
    "fund": "funding",
    "point_dnw": "transfer",
    "point_fee": "commission",
    "point_refr": "rebate",
    "bonus_offset": "bonus_adjustment",
}
GATE_ACCOUNT_METRICS = (
    "total",
    "available",
    "position_margin",
    "order_margin",
    "unrealised_pnl",
    "point",
    "bonus",
)

GATE_COVERAGE = {
    "account_book": (
        "income",
        "complete",
        "REST history is bounded by rolling retention and 30-day request windows",
    ),
    "orders": (
        "orders",
        "complete",
        "REST history is bounded by the venue rolling-retention window",
    ),
    "trades": (
        "trades",
        "complete",
        "REST history is bounded by the venue rolling-retention window",
    ),
    "position_close": (
        "position_summaries",
        "complete",
        "time-range position-close endpoint",
    ),
    "liquidations": (
        "liquidation_events",
        "complete",
        "time-range liquidation endpoint",
    ),
    "auto_deleverages": (
        "auto_deleveraging_events",
        "complete",
        "time-range auto-deleveraging endpoint",
    ),
    "open_positions": (
        "positions",
        "point",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "account_snapshot": (
        "account_snapshots",
        "point",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "position_history": (
        "positions",
        "partial",
        "history is queried for contracts discovered from other account datasets",
    ),
    "price_orders": (
        "conditional_orders",
        "partial",
        "advanced-order history availability is controlled by the venue endpoint",
    ),
    "trail_orders": (
        "conditional_orders",
        "partial",
        "advanced-order history availability is controlled by the venue endpoint",
    ),
    "chase_orders": (
        "conditional_orders",
        "partial",
        "advanced-order history availability is controlled by the venue endpoint",
    ),
}


class GateAdapter(VenueAdapter):
    name = "gate"

    def __init__(self, *args: Any, http: ReadOnlyHttp | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.http = http or ReadOnlyHttp(
            self.venue.base_url,
            timeout_seconds=self.config.collection.request_timeout_seconds,
            retries=self.config.collection.request_retries,
            minimum_interval_seconds=0.05,
        )

    def collect(self, *, end_ms: int, full: bool) -> list[SourceBatch]:
        collected_at = utc_now_iso()
        batches: list[SourceBatch] = []
        settlements = [str(value).lower() for value in self.venue.options.get(
            "settlements", ["usdt", "btc"]
        )]
        for settlement in settlements:
            definitions = (
                ("account_book", "account_book", "time", normalize_gate_account_book),
                ("orders", "orders_timerange", "create_time", normalize_gate_order),
                ("trades", "my_trades_timerange", "create_time", normalize_gate_trade),
                ("position_close", "position_close", "time", normalize_gate_position_close),
                ("liquidations", "liquidates", "time", normalize_gate_liquidation),
                ("auto_deleverages", "auto_deleverages", "time", normalize_gate_adl),
            )
            settlement_batches: list[SourceBatch] = []
            for short_name, endpoint, time_field, normalizer in definitions:
                source = f"{settlement}_{short_name}"
                start_ms = self.start_for(
                    source,
                    full=full,
                    retention_start_ms=end_ms - GATE_REST_RETENTION_MS,
                )
                window_ms = (
                    GATE_ACCOUNT_BOOK_WINDOW_MS
                    if short_name == "account_book"
                    else GATE_HISTORY_WINDOW_MS
                )
                batch = self._attempt(
                    source,
                    start_ms,
                    end_ms,
                    start_ms <= self.config.collection.initial_start_ms,
                    lambda source=source, endpoint=endpoint, time_field=time_field,
                    normalizer=normalizer, start_ms=start_ms,
                    settlement=settlement, window_ms=window_ms: self._collect_windowed(
                        source=source,
                        path=f"/api/v4/futures/{settlement}/{endpoint}",
                        time_field=time_field,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        collected_at=collected_at,
                        normalizer=normalizer,
                        settlement=settlement,
                        window_ms=window_ms,
                    ),
                )
                batches.append(batch)
                settlement_batches.append(batch)

            current_source = f"{settlement}_open_positions"
            current_start = self.start_for(current_source, full=full)
            current = self._attempt(
                current_source,
                current_start,
                end_ms,
                True,
                lambda current_source=current_source,
                settlement=settlement: self._collect_list(
                    source=current_source,
                    path=f"/api/v4/futures/{settlement}/positions",
                    params=[],
                    collected_at=collected_at,
                    normalizer=normalize_gate_position,
                    settlement=settlement,
                ),
            )
            batches.append(current)

            account_source = f"{settlement}_account_snapshot"
            account_start = self.start_for(account_source, full=full)
            batches.append(
                self._attempt(
                    account_source,
                    account_start,
                    end_ms,
                    True,
                    lambda account_source=account_source,
                    settlement=settlement: self._collect_account_snapshot(
                        source=account_source,
                        settlement=settlement,
                        collected_at=collected_at,
                    ),
                )
            )

            symbols = self.store.historical_symbols(self.name, self.venue.account_id)
            for batch in settlement_batches:
                for rows in batch.table_rows.values():
                    symbols.update(row["symbol"] for row in rows if row.get("symbol"))
            for rows in current.table_rows.values():
                symbols.update(
                    row["symbol"]
                    for row in rows
                    if row.get("symbol") and row.get("status") == "open"
                )
            symbols = {
                symbol for symbol in symbols if _gate_settlement_for_symbol(symbol) == settlement
            }
            history_source = f"{settlement}_position_history"
            history_start = self.start_for(
                history_source,
                full=full,
                retention_start_ms=end_ms - GATE_REST_RETENTION_MS,
            )
            history_symbols = tuple(sorted(symbols))
            history = self._attempt(
                history_source,
                history_start,
                end_ms,
                history_start <= self.config.collection.initial_start_ms,
                lambda history_source=history_source,
                settlement=settlement,
                symbols=history_symbols,
                history_start=history_start: self._collect_position_history(
                    source=history_source,
                    settlement=settlement,
                    symbols=symbols,
                    start_ms=history_start,
                    end_ms=end_ms,
                    collected_at=collected_at,
                ),
            )
            if not symbols and not history.error:
                history.warnings.append(
                    "no historical contracts were available for position discovery"
                )
            batches.append(history)

            advanced_definitions = (
                (
                    "price_orders",
                    f"/api/v4/futures/{settlement}/price_orders",
                    "list",
                    normalize_gate_price_order,
                ),
                (
                    "trail_orders",
                    f"/api/v4/futures/{settlement}/autoorder/v1/trail/list",
                    "wrapped",
                    normalize_gate_advanced_order,
                ),
                (
                    "chase_orders",
                    f"/api/v4/futures/{settlement}/autoorder/v1/chase/list",
                    "wrapped",
                    normalize_gate_advanced_order,
                ),
            )
            for short_name, path, response_kind, normalizer in advanced_definitions:
                source = f"{settlement}_{short_name}"
                start_ms = self.start_for(source, full=full)
                batches.append(
                    self._attempt(
                        source,
                        start_ms,
                        end_ms,
                        True,
                        lambda source=source, path=path, response_kind=response_kind,
                        normalizer=normalizer, start_ms=start_ms,
                        settlement=settlement: self._collect_advanced_orders(
                            source=source,
                            path=path,
                            response_kind=response_kind,
                            start_ms=start_ms,
                            end_ms=end_ms,
                            collected_at=collected_at,
                            normalizer=normalizer,
                            settlement=settlement,
                        ),
                    )
                )
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
        except Exception as exc:
            batch = self.failure_batch(
                source=source, start_ms=start_ms, end_ms=end_ms, error=exc
            )
            self._apply_coverage(batch)
            return batch

    @staticmethod
    def _apply_coverage(batch: SourceBatch) -> None:
        settlement, separator, short_name = batch.source.partition("_")
        if not separator:
            return
        metadata = GATE_COVERAGE.get(short_name)
        if not metadata:
            return
        batch.coverage_dataset = metadata[0]
        batch.coverage_status = metadata[1]
        batch.coverage_scope = f"settlement:{settlement}"
        batch.coverage_limitation = metadata[2]

    def _collect_backward(
        self,
        *,
        source: str,
        path: str,
        time_field: str,
        start_ms: int,
        end_ms: int,
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        settlement: str,
        extra_params: list[tuple[str, Any]] | None = None,
    ) -> SourceBatch:
        limit = min(self.config.collection.page_limit, 100)
        start_seconds = start_ms // 1000
        all_rows: list[dict[str, Any]] = []
        page_budget = self.config.collection.max_pages_per_query
        end_seconds = end_ms // 1000
        for page in range(page_budget):
            params = list(extra_params or []) + [
                ("from", start_seconds),
                ("to", end_seconds),
                ("limit", limit),
                ("offset", page * limit),
            ]
            payload = self._get(path, params)
            if not isinstance(payload, list):
                raise RuntimeError(f"{source} returned a non-list response")
            page_rows = [row for row in payload if isinstance(row, dict)]
            if page_rows and not any(
                row.get(time_field) not in (None, "") for row in page_rows
            ):
                raise RuntimeError(f"{source} rows did not contain {time_field}")
            all_rows.extend(page_rows)
            if len(page_rows) < limit:
                break
        else:
            raise RuntimeError(f"{source} exceeded max_pages_per_query")
        return self._normalize_rows(
            source,
            _dedupe_gate_rows(all_rows),
            collected_at,
            normalizer,
            settlement,
        )

    def _collect_windowed(
        self,
        *,
        source: str,
        path: str,
        time_field: str,
        start_ms: int,
        end_ms: int,
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        settlement: str,
        window_ms: int,
        extra_params: list[tuple[str, Any]] | None = None,
    ) -> SourceBatch:
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(cursor + window_ms, end_ms)
            batch = self._collect_backward(
                source=source,
                path=path,
                time_field=time_field,
                start_ms=cursor,
                end_ms=window_end,
                collected_at=collected_at,
                normalizer=normalizer,
                settlement=settlement,
                extra_params=extra_params,
            )
            rows.extend(record["payload"] for record in batch.raw_records)
            if window_end >= end_ms:
                break
            cursor = window_end + 1
        return self._normalize_rows(
            source,
            _dedupe_gate_rows(rows),
            collected_at,
            normalizer,
            settlement,
        )

    def _collect_list(
        self,
        *,
        source: str,
        path: str,
        params: list[tuple[str, Any]],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        settlement: str,
    ) -> SourceBatch:
        payload = self._get(path, params)
        if not isinstance(payload, list):
            raise RuntimeError(f"{source} returned a non-list response")
        return self._normalize_rows(
            source,
            [row for row in payload if isinstance(row, dict)],
            collected_at,
            normalizer,
            settlement,
        )

    def _collect_account_snapshot(
        self, *, source: str, settlement: str, collected_at: str
    ) -> SourceBatch:
        payload = self._get(f"/api/v4/futures/{settlement}/accounts", [])
        if not isinstance(payload, dict):
            raise RuntimeError(f"{source} returned a non-object response")
        source_id = str(int(time.time() * 1000))
        raw = raw_record(
            venue=self.name,
            account_id=self.venue.account_id,
            source=source,
            source_id=source_id,
            payload=payload,
            collected_at=collected_at,
        )
        observed_at, observed_ms = timestamp_fields(collected_at)
        batch = SourceBatch(self.name, source, 0, 0, False, raw_records=[raw])
        currency = str(payload.get("currency") or settlement).upper()
        for metric in GATE_ACCOUNT_METRICS:
            if payload.get(metric) in (None, ""):
                continue
            metric_id = stable_id(source_id, currency, metric)
            batch.add_row(
                "account_snapshots",
                row_for(
                    "account_snapshots",
                    record_id=stable_id(
                        "gate", self.venue.account_id, "snapshot", metric_id
                    ),
                    venue="gate",
                    account_id=self.venue.account_id,
                    market_type="perpetual",
                    observed_at=observed_at,
                    observed_at_ms=observed_ms,
                    currency=currency,
                    metric=metric,
                    value=decimal_text(payload.get(metric), default="0"),
                    source=source,
                    source_id=metric_id,
                    collected_at=collected_at,
                    raw_ref=raw["raw_id"],
                ),
            )
        return batch

    def _collect_advanced_orders(
        self,
        *,
        source: str,
        path: str,
        response_kind: str,
        start_ms: int,
        end_ms: int,
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        settlement: str,
    ) -> SourceBatch:
        rows: list[dict[str, Any]] = []
        if response_kind == "list":
            for status in ("open", "finished"):
                rows.extend(
                    self._collect_offset_pages(
                        source=source,
                        path=path,
                        base_params=[("status", status)],
                        response_kind=response_kind,
                    )
                )
        else:
            common = [("sort_by", 1), ("hide_cancel", "false")]
            rows.extend(
                self._collect_offset_pages(
                    source=source,
                    path=path,
                    base_params=common + [("is_finished", "false")],
                    response_kind=response_kind,
                )
            )
            rows.extend(
                self._collect_offset_pages(
                    source=source,
                    path=path,
                    base_params=common
                    + [
                        ("is_finished", "true"),
                        ("start_at", start_ms // 1000),
                        ("end_at", end_ms // 1000),
                    ],
                    response_kind=response_kind,
                )
            )
        return self._normalize_rows(
            source,
            _dedupe_gate_rows(rows),
            collected_at,
            normalizer,
            settlement,
        )

    def _collect_offset_pages(
        self,
        *,
        source: str,
        path: str,
        base_params: list[tuple[str, Any]],
        response_kind: str,
    ) -> list[dict[str, Any]]:
        limit = min(self.config.collection.page_limit, 100)
        rows: list[dict[str, Any]] = []
        for page in range(self.config.collection.max_pages_per_query):
            if response_kind == "wrapped":
                pagination = [("page_num", page + 1), ("page_size", limit)]
            else:
                pagination = [("limit", limit), ("offset", page * limit)]
            payload = self._get(path, base_params + pagination)
            if response_kind == "wrapped":
                page_rows = _gate_wrapped_rows(payload)
            elif isinstance(payload, list):
                page_rows = [row for row in payload if isinstance(row, dict)]
            else:
                raise RuntimeError(f"{source} returned a non-list response")
            rows.extend(page_rows)
            if len(page_rows) < limit:
                break
        else:
            raise RuntimeError(f"{source} exceeded max_pages_per_query")
        return rows

    def _collect_position_history(
        self,
        *,
        source: str,
        settlement: str,
        symbols: list[str],
        start_ms: int,
        end_ms: int,
        collected_at: str,
    ) -> SourceBatch:
        combined: list[dict[str, Any]] = []
        for symbol in symbols:
            batch = self._collect_windowed(
                source=source,
                path=f"/api/v4/futures/{settlement}/positions_timerange",
                time_field="time",
                start_ms=start_ms,
                end_ms=end_ms,
                collected_at=collected_at,
                normalizer=normalize_gate_position,
                settlement=settlement,
                window_ms=GATE_HISTORY_WINDOW_MS,
                extra_params=[("contract", symbol)],
            )
            combined.extend(record["payload"] for record in batch.raw_records)
        return self._normalize_rows(
            source,
            _dedupe_gate_rows(combined),
            collected_at,
            normalize_gate_position,
            settlement,
        )

    def _normalize_rows(
        self,
        source: str,
        rows: Iterable[dict[str, Any]],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        settlement: str,
    ) -> SourceBatch:
        batch = SourceBatch(self.name, source, 0, 0, False)
        for payload in rows:
            normalized = normalizer(
                payload,
                account_id=self.venue.account_id,
                source=source,
                collected_at=collected_at,
                settlement=settlement,
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

    def _get(self, path: str, params: list[tuple[str, Any]]) -> Any:
        timestamp_seconds = int(time.time())
        request_params = [
            (key, "true" if value is True else "false" if value is False else str(value))
            for key, value in params
        ]
        headers = gate_headers(
            method="GET",
            api_path=path,
            query=request_params,
            api_key=self.venue.api_key,
            api_secret=self.venue.api_secret,
            timestamp_seconds=timestamp_seconds,
        )
        return self.http.get_json(path, params=request_params, headers=headers)


def normalize_gate_account_book(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id",))
    event_at, event_ms = timestamp_fields(payload.get("time"))
    raw_type = str(payload.get("type") or "other")
    event_type = GATE_ACCOUNT_TYPES.get(raw_type, "other")
    row = row_for(
        "cashflows",
        record_id=stable_id("gate", account_id, "cashflow", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        event_type=event_type,
        event_subtype=raw_type,
        symbol=payload.get("contract"),
        currency=settlement.upper(),
        amount=decimal_text(payload.get("change"), default="0"),
        trade_id=payload.get("trade_id"),
        reporting_role="primary",
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
        notes=str(payload.get("text") or ""),
    )
    return NormalizedRecord(source_id, {"cashflows": [row]})


def normalize_gate_trade(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = source_identity(payload, ("trade_id", "id"))
    event_at, event_ms = timestamp_fields(payload.get("create_time"))
    size = Decimal(decimal_text(payload.get("size"), default="0"))
    close_size = Decimal(decimal_text(payload.get("close_size"), default="0"))
    side, action, position_side = _gate_trade_direction(size, close_size)
    row = row_for(
        "executions",
        record_id=stable_id("gate", account_id, "execution", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        symbol=payload.get("contract"),
        side=side,
        position_action=action,
        position_side=position_side,
        liquidity=payload.get("role"),
        quantity=decimal_text(abs(size)),
        quantity_unit="contract",
        price=decimal_text(payload.get("price")),
        notional=decimal_text(payload.get("trade_value")),
        settlement_currency=settlement.upper(),
        fee=negative_magnitude(payload.get("fee")),
        fee_currency=settlement.upper(),
        order_id=payload.get("order_id"),
        trade_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"executions": [row]})


def normalize_gate_order(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id_string", "id"))
    created_at, created_ms = timestamp_fields(payload.get("create_time"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("finish_time") or payload.get("create_time")
    )
    size = Decimal(decimal_text(payload.get("size"), default="0"))
    left = Decimal(decimal_text(payload.get("left"), default="0"))
    side = "buy" if size > 0 else "sell" if size < 0 else ""
    is_close = boolean_value(payload.get("is_close"))
    is_reduce_only = boolean_value(payload.get("is_reduce_only"))
    position_action = "close" if is_close or is_reduce_only else "open"
    status = str(payload.get("status") or "")
    finish_as = str(payload.get("finish_as") or "")
    if status == "finished" and finish_as:
        status = finish_as
    row = row_for(
        "orders",
        record_id=stable_id("gate", account_id, "order", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=payload.get("contract"),
        side=side,
        position_action=position_action,
        order_type=(
            "market"
            if Decimal(decimal_text(payload.get("price"), default="0")) == 0
            else str(payload.get("order_type") or "limit")
        ),
        time_in_force=payload.get("tif"),
        status=status,
        quantity=decimal_text(abs(size)),
        quantity_unit="contract",
        filled_quantity=decimal_text(abs(size) - abs(left)),
        price=decimal_text(payload.get("price")),
        trigger_price=decimal_text(
            payload.get("tpsl_tp_trigger_price")
            or payload.get("tpsl_sl_trigger_price")
        ),
        average_price=decimal_text(payload.get("fill_price")),
        reduce_only=bool_text(payload.get("is_reduce_only")),
        close_only=bool_text(payload.get("is_close")),
        margin_mode=payload.get("pos_margin_mode"),
        client_order_id=payload.get("text"),
        order_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def normalize_gate_price_order(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    initial = payload.get("initial") if isinstance(payload.get("initial"), dict) else {}
    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
    source_id = source_identity(payload, ("id_string", "id"))
    created_at, created_ms = timestamp_fields(payload.get("create_time"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("finish_time") or payload.get("create_time")
    )
    quantity = Decimal(
        decimal_text(initial.get("size") or initial.get("amount"), default="0")
    )
    is_close = boolean_value(initial.get("close") or initial.get("is_close"))
    is_reduce_only = boolean_value(
        initial.get("reduce_only") or initial.get("is_reduce_only")
    )
    side, action, position_side = _gate_advanced_direction(
        quantity,
        initial.get("auto_size"),
        is_close or is_reduce_only,
        "",
    )
    trigger_parts = (
        trigger.get("strategy_type"),
        trigger.get("price_type"),
        trigger.get("rule"),
    )
    trigger_type = (
        stable_id(*trigger_parts)
        if any(value not in (None, "") for value in trigger_parts)
        else ""
    )
    row = row_for(
        "orders",
        record_id=stable_id(
            "gate", account_id, "price-order", settlement, source_id
        ),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=initial.get("contract"),
        side=side,
        position_action=action,
        position_side=position_side,
        order_type=str(payload.get("order_type") or "price_triggered").lower(),
        time_in_force=initial.get("tif"),
        status=payload.get("status"),
        quantity=decimal_text(abs(quantity)),
        quantity_unit="contract",
        price=decimal_text(initial.get("price")),
        trigger_price=decimal_text(trigger.get("price")),
        trigger_type=trigger_type,
        reduce_only=bool_text(initial.get("reduce_only") or initial.get("is_reduce_only")),
        close_only=bool_text(initial.get("close") or initial.get("is_close")),
        margin_mode=payload.get("pos_margin_mode"),
        client_order_id=initial.get("text"),
        order_id=source_id,
        linked_order_id=payload.get("me_order_id") or payload.get("trade_id"),
        failure_reason=payload.get("reason") or payload.get("finish_as"),
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def normalize_gate_advanced_order(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = source_identity(payload, ("id",))
    created_value = (
        payload.get("created_at_precise")
        or payload.get("create_time_precise")
        or payload.get("created_at")
        or payload.get("create_time")
    )
    updated_value = (
        payload.get("finished_at_precise")
        or payload.get("finish_time_precise")
        or payload.get("finished_at")
        or payload.get("finish_time")
        or payload.get("updated_at")
        or created_value
    )
    created_at, created_ms = timestamp_fields(created_value)
    updated_at, updated_ms = timestamp_fields(updated_value)
    quantity = Decimal(decimal_text(payload.get("amount"), default="0"))
    side, action, position_side = _gate_advanced_direction(
        quantity,
        "",
        boolean_value(payload.get("reduce_only")),
        str(payload.get("side_label") or payload.get("position_side_output") or ""),
    )
    order_kind = "trail-order" if "trail" in source else "chase-order"
    row = row_for(
        "orders",
        record_id=stable_id("gate", account_id, order_kind, settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=payload.get("contract"),
        side=side,
        position_action=action,
        position_side=position_side,
        order_type="trailing_stop" if "trail" in source else "chase_limit",
        status=payload.get("status") or payload.get("status_code"),
        quantity=decimal_text(abs(quantity)),
        quantity_unit="contract",
        filled_quantity=decimal_text(payload.get("fill_amount"), default="0"),
        price=decimal_text(
            payload.get("price_limit") or payload.get("suborder_price")
        ),
        trigger_price=decimal_text(payload.get("trigger_price")),
        trigger_type=payload.get("price_type"),
        activation_price=decimal_text(payload.get("activation_price")),
        callback_rate=decimal_text(
            payload.get("price_offset") or payload.get("price_gap_value")
        ),
        average_price=decimal_text(payload.get("average_fill_price")),
        reduce_only=bool_text(payload.get("reduce_only")),
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=payload.get("pos_margin_mode"),
        client_order_id=payload.get("text"),
        order_id=source_id,
        linked_order_id=payload.get("suborder_id"),
        failure_reason=(
            payload.get("error_label")
            or payload.get("reason")
            or payload.get("suborder_finish_as")
        ),
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def normalize_gate_position_close(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = _gate_composite_id(payload, ("time", "contract", "side", "first_open_time"))
    opened_at, opened_ms = timestamp_fields(payload.get("first_open_time"))
    closed_at, closed_ms = timestamp_fields(payload.get("time"))
    side = str(payload.get("side") or "")
    open_price = payload.get("long_price") if side == "long" else payload.get("short_price")
    close_price = payload.get("short_price") if side == "long" else payload.get("long_price")
    position = row_for(
        "positions",
        record_id=stable_id("gate", account_id, "position-close", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        opened_at=opened_at,
        opened_at_ms=opened_ms,
        closed_at=closed_at,
        closed_at_ms=closed_ms,
        symbol=payload.get("contract"),
        status="closed",
        position_side=side,
        quantity=decimal_text(payload.get("accum_size")),
        quantity_unit="contract",
        max_quantity=decimal_text(payload.get("max_size")),
        open_price=decimal_text(open_price),
        close_price=decimal_text(close_price),
        realized_pnl=decimal_text(payload.get("pnl_pnl"), default="0"),
        funding=decimal_text(payload.get("pnl_fund"), default="0"),
        fees=decimal_text(payload.get("pnl_fee"), default="0"),
        settlement_currency=settlement.upper(),
        position_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=closed_at,
        source_updated_at_ms=closed_ms,
        collected_at=collected_at,
    )
    summary = row_for(
        "cashflows",
        record_id=stable_id(
            "gate", account_id, "cashflow", "position-summary", settlement, source_id
        ),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        event_time=closed_at,
        event_time_ms=closed_ms,
        event_type="realized_pnl",
        event_subtype="position_summary",
        symbol=payload.get("contract"),
        currency=settlement.upper(),
        amount=decimal_text(payload.get("pnl"), default="0"),
        position_id=source_id,
        reporting_role="informational",
        source=source,
        source_id=source_id,
        source_updated_at=closed_at,
        source_updated_at_ms=closed_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(
        source_id,
        {"positions": [position], "cashflows": [summary]},
    )


def normalize_gate_position(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    symbol = str(payload.get("contract") or "")
    side = str(payload.get("mode") or payload.get("side") or "")
    size = Decimal(decimal_text(payload.get("size"), default="0"))
    if side not in {"long", "short"}:
        side = "long" if size > 0 else "short" if size < 0 else ""
    source_id = source_identity(payload, ("id",))
    event_value = payload.get("time") or payload.get("update_time")
    event_at, event_ms = timestamp_fields(event_value)
    if source_id.startswith("content:"):
        if "position_history" in source and event_ms:
            source_id = stable_id(symbol, side or "net", event_ms)
        else:
            source_id = stable_id(symbol, side or "net")
    row = row_for(
        "positions",
        record_id=stable_id("gate", account_id, "position", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        opened_at=event_at,
        opened_at_ms=event_ms,
        symbol=symbol,
        status="open" if size else "closed",
        position_side=side,
        quantity=decimal_text(abs(size)),
        quantity_unit="contract",
        open_price=decimal_text(payload.get("entry_price")),
        realized_pnl=decimal_text(
            payload.get("realised_pnl") or payload.get("realized_pnl"),
            default="0",
        ),
        settlement_currency=settlement.upper(),
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=payload.get("pos_margin_mode") or payload.get("cross_leverage_limit"),
        liquidation_price=decimal_text(payload.get("liq_price")),
        position_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"positions": [row]})


def normalize_gate_liquidation(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    source_id = source_identity(payload, ("order_id", "id"))
    event_at, event_ms = timestamp_fields(payload.get("time"))
    size = Decimal(decimal_text(payload.get("size"), default="0"))
    position = row_for(
        "positions",
        record_id=stable_id("gate", account_id, "liquidation", settlement, source_id),
        venue="gate",
        account_id=account_id,
        market_type="perpetual",
        closed_at=event_at,
        closed_at_ms=event_ms,
        symbol=payload.get("contract"),
        status="liquidated",
        position_side="long" if size > 0 else "short" if size < 0 else "",
        quantity=decimal_text(abs(size)),
        quantity_unit="contract",
        open_price=decimal_text(payload.get("entry_price")),
        close_price=decimal_text(payload.get("fill_price")),
        settlement_currency=settlement.upper(),
        leverage=decimal_text(payload.get("leverage")),
        liquidation_price=decimal_text(payload.get("liq_price")),
        position_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"positions": [position]})


def normalize_gate_adl(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    settlement: str,
) -> NormalizedRecord:
    normalized = normalize_gate_liquidation(
        payload,
        account_id=account_id,
        source=source,
        collected_at=collected_at,
        settlement=settlement,
    )
    for row in normalized.table_rows.get("positions", []):
        row["status"] = "auto_deleveraged"
        row["record_id"] = row["record_id"].replace(":liquidation:", ":auto-deleverage:")
    return normalized


def _gate_trade_direction(size: Decimal, close_size: Decimal) -> tuple[str, str, str]:
    if size > 0:
        if close_size > 0 and size <= close_size:
            return "buy", "close_short", "short"
        if close_size > 0:
            return "buy", "reverse_short_to_long", "mixed"
        return "buy", "open_long", "long"
    if size < 0:
        if close_size < 0 and size >= close_size:
            return "sell", "close_long", "long"
        if close_size < 0:
            return "sell", "reverse_long_to_short", "mixed"
        return "sell", "open_short", "short"
    return "", "", ""


def _gate_advanced_direction(
    quantity: Decimal,
    auto_size: Any,
    reduce_only: bool,
    side_label: str,
) -> tuple[str, str, str]:
    label = str(side_label or auto_size or "").strip().lower().replace(" ", "_")
    known = {
        "open_long": ("buy", "open_long", "long"),
        "open_short": ("sell", "open_short", "short"),
        "close_long": ("sell", "close_long", "long"),
        "close_short": ("buy", "close_short", "short"),
        "long": ("buy", "close_short" if reduce_only else "open_long", "long"),
        "short": ("sell", "close_long" if reduce_only else "open_short", "short"),
    }
    if label in known:
        return known[label]
    if quantity > 0:
        return "buy", "close_short" if reduce_only else "open_long", (
            "short" if reduce_only else "long"
        )
    if quantity < 0:
        return "sell", "close_long" if reduce_only else "open_short", (
            "long" if reduce_only else "short"
        )
    return "", "close" if reduce_only else "", ""


def _gate_composite_id(payload: dict[str, Any], fields: tuple[str, ...]) -> str:
    values = [payload.get(field) for field in fields]
    if all(value not in (None, "") for value in values):
        return stable_id(*values)
    return content_id("content", payload)


def _gate_settlement_for_symbol(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith("_USDT"):
        return "usdt"
    if upper.endswith("_USD"):
        return "btc"
    return ""


def _dedupe_gate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = source_identity(row, ("id", "trade_id", "order_id", "id_string"))
        if identity.startswith("content:"):
            identity = content_id("content", row)
        by_id[identity] = row
    return list(by_id.values())


def _gate_wrapped_rows(payload: Any) -> list[dict[str, Any]]:
    candidate = payload
    if isinstance(candidate, dict) and isinstance(candidate.get("data"), dict):
        candidate = candidate["data"]
    if isinstance(candidate, dict):
        candidate = candidate.get("orders", [])
    if not isinstance(candidate, list):
        return []
    return [row for row in candidate if isinstance(row, dict)]
