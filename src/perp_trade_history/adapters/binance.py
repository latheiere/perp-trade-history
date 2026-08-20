from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from perp_trade_history.adapters.base import NormalizedRecord, SourceBatch, VenueAdapter
from perp_trade_history.errors import (
    ApiError,
    ClockSkewError,
    CollectionError,
    TerminalContractError,
)
from perp_trade_history.http import ReadOnlyHttp, binance_signed_query
from perp_trade_history.models import (
    bool_text,
    boolean_value,
    canonical_json,
    decimal_text,
    infer_settlement_currency,
    negative_magnitude,
    parse_datetime,
    row_for,
    source_identity,
    stable_id,
    timestamp_fields,
    utc_now_iso,
)
from perp_trade_history.storage import raw_record

DAY_MS = 24 * 60 * 60 * 1000
SEVEN_DAY_WINDOW_MS = 7 * DAY_MS - 1
TRADE_RETENTION_MS = 183 * DAY_MS
ORDER_RETENTION_MS = 90 * DAY_MS
ORDER_REST_RETENTION_MS = 89 * DAY_MS
INCOME_RETENTION_MS = 90 * DAY_MS
FORCE_ORDER_RETENTION_MS = 7 * DAY_MS
ARCHIVE_WINDOW_MS = 365 * DAY_MS - 1
TERMINAL_CONTRACT_RECHECK_MS = 7 * DAY_MS

LOGGER = logging.getLogger(__name__)

BINANCE_ARCHIVE_ENDPOINTS = {
    "orders": ("/fapi/v1/order/asyn", "/fapi/v1/order/asyn/id"),
    "trades": ("/fapi/v1/trade/asyn", "/fapi/v1/trade/asyn/id"),
    "income": ("/fapi/v1/income/asyn", "/fapi/v1/income/asyn/id"),
}

BINANCE_ARCHIVE_RETENTION_MS = {
    "orders": ORDER_RETENTION_MS,
    "trades": TRADE_RETENTION_MS,
    "income": INCOME_RETENTION_MS,
}

BINANCE_ARCHIVE_COLUMNS = {
    "orders": {
        "symbol": "symbol",
        "orderid": "orderId",
        "orderno": "orderId",
        "timeutc": "time",
        "clientorderid": "clientOrderId",
        "price": "price",
        "origqty": "origQty",
        "originalquantity": "origQty",
        "amount": "origQty",
        "executedqty": "executedQty",
        "executedquantity": "executedQty",
        "executedamount": "executedQty",
        "cumqty": "cumQty",
        "cumquote": "cumQuote",
        "executedquoteamount": "cumQuote",
        "avgprice": "avgPrice",
        "status": "status",
        "timeinforce": "timeInForce",
        "type": "type",
        "origtype": "origType",
        "side": "side",
        "positionside": "positionSide",
        "stopprice": "stopPrice",
        "triggerprice": "triggerPrice",
        "workingtype": "workingType",
        "reduceonly": "reduceOnly",
        "closeposition": "closePosition",
        "time": "time",
        "createtime": "time",
        "updatetime": "updateTime",
        "activateprice": "activatePrice",
        "activationprice": "activatePrice",
        "callbackrate": "callbackRate",
    },
    "trades": {
        "symbol": "symbol",
        "id": "id",
        "tradeid": "id",
        "timeutc": "time",
        "orderid": "orderId",
        "side": "side",
        "positionside": "positionSide",
        "price": "price",
        "qty": "qty",
        "quantity": "qty",
        "baseqty": "baseQty",
        "amount": "quoteQty",
        "quoteqty": "quoteQty",
        "commission": "commission",
        "fee": "commission",
        "commissionasset": "commissionAsset",
        "feeasset": "commissionAsset",
        "marginasset": "marginAsset",
        "realizedpnl": "realizedPnl",
        "realizedprofit": "realizedPnl",
        "maker": "maker",
        "buyer": "buyer",
        "time": "time",
    },
    "income": {
        "symbol": "symbol",
        "incometype": "incomeType",
        "type": "incomeType",
        "income": "income",
        "amount": "income",
        "asset": "asset",
        "dateutc": "time",
        "currency": "asset",
        "info": "info",
        "time": "time",
        "tranid": "tranId",
        "transactionid": "tranId",
        "tradeid": "tradeId",
    },
}

BINANCE_COVERAGE = {
    "income": (
        "income",
        "complete",
        "account",
        "REST history is bounded by the venue rolling-retention window",
    ),
    "user_trades": (
        "trades",
        "partial",
        "configured-and-discovered-contracts",
        "REST history is symbol-scoped and bounded by rolling retention",
    ),
    "all_orders": (
        "orders",
        "partial",
        "configured-and-discovered-contracts",
        "REST history is symbol-scoped and bounded by rolling retention",
    ),
    "algo_orders": (
        "conditional_orders",
        "partial",
        "configured-and-discovered-contracts",
        "REST history is symbol-scoped and bounded by rolling retention",
    ),
    "order_amendments": (
        "order_events",
        "partial",
        "known-recent-orders",
        "REST history is retained for three months and requires one query per known order",
    ),
    "force_orders": (
        "liquidation_orders",
        "complete",
        "account",
        "REST history is bounded by a short rolling-retention window",
    ),
    "position_risk": (
        "positions",
        "point",
        "account",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
    "account_snapshot": (
        "account_snapshots",
        "point",
        "account",
        "current-state snapshot; it does not establish historical interval coverage",
    ),
}

BINANCE_INCOME_TYPES = {
    "TRANSFER": "transfer",
    "WELCOME_BONUS": "bonus",
    "REALIZED_PNL": "realized_pnl",
    "FUNDING_FEE": "funding",
    "COMMISSION": "commission",
    "INSURANCE_CLEAR": "insurance",
    "REFERRAL_KICKBACK": "rebate",
    "COMMISSION_REBATE": "rebate",
    "API_REBATE": "rebate",
    "CONTEST_REWARD": "reward",
    "CROSS_COLLATERAL_TRANSFER": "transfer",
    "OPTIONS_PREMIUM_FEE": "premium",
    "OPTIONS_SETTLE_PROFIT": "settlement",
    "INTERNAL_TRANSFER": "transfer",
    "AUTO_EXCHANGE": "conversion",
    "DELIVERED_SETTELMENT": "settlement",
    "COIN_SWAP_DEPOSIT": "transfer",
    "COIN_SWAP_WITHDRAW": "transfer",
    "POSITION_LIMIT_INCREASE_FEE": "commission",
    "BFUSD_REWARD": "reward",
}


class BinanceAdapter(VenueAdapter):
    name = "binance"

    def __init__(self, *args: Any, http: ReadOnlyHttp | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.http = http or ReadOnlyHttp(
            self.venue.base_url,
            timeout_seconds=self.config.collection.request_timeout_seconds,
            retries=self.config.collection.request_retries,
            minimum_interval_seconds=0.15,
        )
        self._clock_offset_ms = 0

    def collect(self, *, end_ms: int, full: bool) -> list[SourceBatch]:
        collected_at = utc_now_iso()
        batches: list[SourceBatch] = []
        regular_orders: SourceBatch | None = None

        income_source = "income"
        income_retention = end_ms - INCOME_RETENTION_MS
        income_start = self.start_for(
            income_source, full=full, retention_start_ms=income_retention
        )
        income = self._attempt(
            income_source,
            income_start,
            end_ms,
            True,
            lambda: self._collect_income(income_start, end_ms, collected_at),
        )
        batches.append(income)

        configured_symbols = {
            str(value).upper() for value in self.venue.options.get("symbols", [])
        }
        historical_symbols = self.store.historical_symbols(
            self.name, self.venue.account_id
        )
        income_symbols = {
            row["symbol"]
            for rows in income.table_rows.values()
            for row in rows
            if row.get("symbol")
        }
        symbols = configured_symbols | historical_symbols | income_symbols
        discovered_count = 0
        discovered_added = 0
        discovery_limit = int(
            self.venue.options.get("max_public_discovery_symbols", 100)
        )
        if self.venue.options.get("discover_active_symbols", True):
            try:
                discovered, public_perpetual_count = self._discover_symbols()
                discovered_count = len(discovered)
                if len(discovered) <= discovery_limit:
                    previous_count = len(symbols)
                    symbols.update(discovered)
                    discovered_added = len(symbols) - previous_count
                else:
                    income.warnings.append(
                        "public contract discovery result was rejected because "
                        f"eligible_contracts={len(discovered)} exceeded "
                        f"safety_limit={discovery_limit}; discovered_contracts_added=0 and "
                        "configured or account-history candidates were retained; "
                        f"catalog_perpetual_contracts={public_perpetual_count}"
                    )
            except Exception as exc:
                income.warnings.append(f"active symbol discovery failed: {exc}")
        LOGGER.info(
            "Contract scope prepared: venue=%s candidates=%d configured_candidates=%d "
            "historical_candidates=%d current_income_candidates=%d "
            "public_discovery_enabled=%s public_discovered=%d public_added=%d "
            "public_safety_limit=%d",
            self.name,
            len(symbols),
            len(configured_symbols),
            len(historical_symbols),
            len(income_symbols),
            self.venue.options.get("discover_active_symbols", True),
            discovered_count,
            discovered_added,
            discovery_limit,
        )

        definitions = (
            (
                "user_trades",
                "/fapi/v1/userTrades",
                end_ms - TRADE_RETENTION_MS,
                normalize_binance_trade,
            ),
            (
                "all_orders",
                "/fapi/v1/allOrders",
                end_ms - ORDER_REST_RETENTION_MS,
                normalize_binance_order,
            ),
            (
                "algo_orders",
                "/fapi/v1/allAlgoOrders",
                end_ms - ORDER_REST_RETENTION_MS,
                normalize_binance_algo_order,
            ),
        )
        for source, path, retention_start, normalizer in definitions:
            start_ms = self.start_for(
                source, full=full, retention_start_ms=retention_start
            )
            batch = self._attempt(
                source,
                start_ms,
                end_ms,
                True,
                lambda source=source, path=path, normalizer=normalizer,
                start_ms=start_ms: self._collect_symbol_windows(
                    source=source,
                    path=path,
                    symbols=sorted(symbols),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    collected_at=collected_at,
                    normalizer=normalizer,
                ),
            )
            if not symbols and not batch.error:
                batch.warnings.append("no contracts were available for symbol-scoped history")
            batches.append(batch)
            if source == "all_orders":
                regular_orders = batch

        amendment_source = "order_amendments"
        amendment_start = self.start_for(
            amendment_source,
            full=full,
            retention_start_ms=end_ms - ORDER_REST_RETENTION_MS,
        )
        candidates = self._order_amendment_candidates(regular_orders, end_ms=end_ms)
        amendments = self._attempt(
            amendment_source,
            amendment_start,
            end_ms,
            True,
            lambda: self._collect_order_amendments(
                candidates=candidates,
                start_ms=amendment_start,
                end_ms=end_ms,
                collected_at=collected_at,
            ),
        )
        if not candidates and not amendments.error:
            amendments.warnings.append(
                "no known recent orders were available for amendment discovery"
            )
        batches.append(amendments)

        force_source = "force_orders"
        force_start = self.start_for(
            force_source,
            full=full,
            retention_start_ms=end_ms - FORCE_ORDER_RETENTION_MS,
        )
        batches.append(
            self._attempt(
                force_source,
                force_start,
                end_ms,
                True,
                lambda: self._collect_symbol_windows(
                    source=force_source,
                    path="/fapi/v1/forceOrders",
                    symbols=[None],
                    start_ms=force_start,
                    end_ms=end_ms,
                    collected_at=collected_at,
                    normalizer=normalize_binance_order,
                    request_limit=100,
                ),
            )
        )

        position_source = "position_risk"
        position_start = self.start_for(position_source, full=full)
        batches.append(
            self._attempt(
                position_source,
                position_start,
                end_ms,
                True,
                lambda: self._collect_list(
                    source=position_source,
                    path="/fapi/v2/positionRisk",
                    params={},
                    collected_at=collected_at,
                    normalizer=normalize_binance_position,
                ),
            )
        )

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
        return batches

    def _order_amendment_candidates(
        self, current: SourceBatch | None, *, end_ms: int
    ) -> list[tuple[str, str]]:
        candidates: set[tuple[str, str]] = set()
        if current:
            for raw in current.raw_records:
                payload = raw.get("payload")
                if not isinstance(payload, dict):
                    continue
                symbol = str(payload.get("symbol") or "")
                order_id = str(payload.get("orderId") or "")
                if symbol and order_id:
                    candidates.add((symbol, order_id))
        for row in self.store.tables["orders"].read():
            if row.get("venue") != self.name or row.get("account_id") != self.venue.account_id:
                continue
            if row.get("source") not in {"all_orders", "archive_orders"}:
                continue
            latest_ms = int(row.get("updated_at_ms") or row.get("created_at_ms") or 0)
            active = row.get("status") in {"new", "partially_filled", "pending"}
            if latest_ms and latest_ms < end_ms - ORDER_RETENTION_MS and not active:
                continue
            symbol = str(row.get("symbol") or "")
            order_id = str(row.get("order_id") or "")
            if symbol and order_id:
                candidates.add((symbol, order_id))
        return sorted(candidates)

    def _collect_order_amendments(
        self,
        *,
        candidates: list[tuple[str, str]],
        start_ms: int,
        end_ms: int,
        collected_at: str,
    ) -> SourceBatch:
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        limit = 100
        if len(candidates) > self.config.collection.max_pages_per_query:
            raise RuntimeError("order amendment queries exceeded max_pages_per_query")
        for symbol, order_id in candidates:
            try:
                payload = self._get_signed(
                    "/fapi/v1/orderAmendment",
                    {
                        "symbol": symbol,
                        "orderId": order_id,
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": limit,
                    },
                )
            except Exception as exc:
                warnings.append(
                    f"order amendment history was unavailable for {symbol}: {exc}"
                )
                continue
            if not isinstance(payload, list):
                warnings.append(
                    f"order amendment history returned a non-list response for {symbol}"
                )
                continue
            order_rows = [row for row in payload if isinstance(row, dict)]
            rows.extend(order_rows)
            if len(order_rows) >= limit:
                warnings.append(
                    f"order amendment history reached the endpoint limit for {symbol}"
                )
        batch = self._normalize_rows(
            "order_amendments",
            _dedupe_binance_rows(rows, ("symbol", "orderId", "amendmentId")),
            collected_at,
            normalize_binance_order_amendment,
        )
        batch.warnings.extend(warnings)
        return batch

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
        metadata = BINANCE_COVERAGE.get(batch.source)
        if not metadata:
            return
        (
            batch.coverage_dataset,
            batch.coverage_status,
            batch.coverage_scope,
            batch.coverage_limitation,
        ) = metadata

    def _collect_income(
        self, start_ms: int, end_ms: int, collected_at: str
    ) -> SourceBatch:
        limit = min(max(self.config.collection.page_limit, 100), 1000)
        rows: list[dict[str, Any]] = []
        for page in range(1, self.config.collection.max_pages_per_query + 1):
            payload = self._get_signed(
                "/fapi/v1/income",
                {"startTime": start_ms, "endTime": end_ms, "page": page, "limit": limit},
            )
            if not isinstance(payload, list):
                raise RuntimeError("income returned a non-list response")
            page_rows = [row for row in payload if isinstance(row, dict)]
            rows.extend(page_rows)
            if len(page_rows) < limit:
                break
        else:
            raise RuntimeError("income exceeded max_pages_per_query")
        return self._normalize_rows(
            "income", _dedupe_binance_rows(rows, ("incomeType", "tranId")), collected_at,
            normalize_binance_income
        )

    def _collect_symbol_windows(
        self,
        *,
        source: str,
        path: str,
        symbols: list[str | None],
        start_ms: int,
        end_ms: int,
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        request_limit: int = 1000,
    ) -> SourceBatch:
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        terminal_exclusions: list[dict[str, Any]] = []
        request_budget = self.config.collection.max_pages_per_query
        request_count = 0
        for symbol in symbols:
            if symbol:
                blocked = self.store.contract_eligibility.blocked(
                    venue=self.name,
                    endpoint=path,
                    contract=symbol,
                    now_ms=end_ms,
                )
                if blocked:
                    terminal_exclusions.append(blocked)
                    continue
            cursor = start_ms
            while cursor <= end_ms:
                window_end = min(cursor + SEVEN_DAY_WINDOW_MS, end_ms)
                remaining_budget = request_budget - request_count
                if remaining_budget <= 0:
                    raise RuntimeError(f"{source} exceeded max_pages_per_query")
                try:
                    fetched, calls, saturated = self._fetch_dense_range(
                        path=path,
                        symbol=symbol,
                        start_ms=cursor,
                        end_ms=window_end,
                        request_budget=remaining_budget,
                        limit=request_limit,
                    )
                except TerminalContractError as exc:
                    if not symbol:
                        raise
                    terminal_exclusions.append(
                        self.store.contract_eligibility.record_terminal(
                            venue=self.name,
                            endpoint=path,
                            contract=symbol,
                            reason=str(exc),
                            response_code=exc.response_code,
                            observed_at_ms=end_ms,
                            recheck_after_ms=TERMINAL_CONTRACT_RECHECK_MS,
                        )
                    )
                    break
                except Exception as exc:
                    window_start = timestamp_fields(cursor)[0]
                    window_stop = timestamp_fields(window_end)[0]
                    contract = repr(symbol) if symbol else "<account-wide>"
                    raise CollectionError(
                        "history request failed: "
                        f"venue={self.name} source={source} endpoint={path} "
                        f"contract={contract} window_start={window_start} "
                        f"window_end={window_stop} error={exc}",
                        category=getattr(exc, "category", "collection"),
                        retryable=bool(getattr(exc, "retryable", False)),
                    ) from exc
                request_count += calls
                rows.extend(fetched)
                if saturated:
                    warnings.append(
                        f"{source} reached the per-request limit within one millisecond "
                        f"for {symbol or 'all contracts'}"
                    )
                if window_end >= end_ms:
                    break
                cursor = window_end + 1
        batch = self._normalize_rows(
            source,
            _dedupe_binance_rows(rows, ("symbol", "id", "orderId", "algoId")),
            collected_at,
            normalizer,
        )
        batch.warnings.extend(warnings)
        batch.terminal_exclusions.extend(terminal_exclusions)
        if terminal_exclusions:
            batch.warnings.append(
                "endpoint-specific terminal contract exclusions were applied: "
                f"endpoint={path} excluded_contracts={len(terminal_exclusions)} "
                f"queried_contracts={len(symbols) - len(terminal_exclusions)}"
            )
        return batch

    def _fetch_dense_range(
        self,
        *,
        path: str,
        symbol: str | None,
        start_ms: int,
        end_ms: int,
        request_budget: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        if request_budget <= 0:
            raise RuntimeError("history request budget exhausted")
        params: dict[str, Any] = {
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        if symbol:
            params["symbol"] = symbol
        payload = self._get_signed(
            path,
            params,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"{path} returned a non-list response")
        rows = [row for row in payload if isinstance(row, dict)]
        if len(rows) < limit:
            return rows, 1, False
        if start_ms >= end_ms:
            return rows, 1, True
        midpoint = start_ms + (end_ms - start_ms) // 2
        left, left_calls, left_saturated = self._fetch_dense_range(
            path=path,
            symbol=symbol,
            start_ms=start_ms,
            end_ms=midpoint,
            request_budget=request_budget - 1,
            limit=limit,
        )
        right_budget = request_budget - 1 - left_calls
        right, right_calls, right_saturated = self._fetch_dense_range(
            path=path,
            symbol=symbol,
            start_ms=midpoint + 1,
            end_ms=end_ms,
            request_budget=right_budget,
            limit=limit,
        )
        return left + right, 1 + left_calls + right_calls, left_saturated or right_saturated

    def _collect_list(
        self,
        *,
        source: str,
        path: str,
        params: dict[str, Any],
        collected_at: str,
        normalizer: Callable[..., NormalizedRecord],
        row_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> SourceBatch:
        payload = self._get_signed(path, params)
        if not isinstance(payload, list):
            raise RuntimeError(f"{source} returned a non-list response")
        rows = [row for row in payload if isinstance(row, dict)]
        if row_filter:
            rows = [row for row in rows if row_filter(row)]
        return self._normalize_rows(source, rows, collected_at, normalizer)

    def _collect_account_snapshot(self, *, source: str, collected_at: str) -> SourceBatch:
        payload = self._get_signed("/fapi/v2/account", {})
        if not isinstance(payload, dict):
            raise RuntimeError("account snapshot returned a non-object response")
        assets = [row for row in payload.get("assets", []) if isinstance(row, dict)]
        batch = SourceBatch(self.name, source, 0, 0, False)
        source_id = str(int(parse_datetime(collected_at).timestamp() * 1000))
        raw = raw_record(
            venue=self.name,
            account_id=self.venue.account_id,
            source=source,
            source_id=source_id,
            payload=payload,
            collected_at=collected_at,
        )
        batch.raw_records.append(raw)
        observed_at, observed_ms = timestamp_fields(collected_at)
        metrics = (
            "walletBalance",
            "unrealizedProfit",
            "marginBalance",
            "maintMargin",
            "initialMargin",
            "positionInitialMargin",
            "openOrderInitialMargin",
            "maxWithdrawAmount",
            "crossWalletBalance",
            "crossUnPnl",
            "availableBalance",
        )
        for asset in assets:
            currency = str(asset.get("asset") or "")
            for metric in metrics:
                if asset.get(metric) in (None, ""):
                    continue
                metric_source_id = stable_id(source_id, currency, metric)
                row = row_for(
                    "account_snapshots",
                    record_id=stable_id(
                        "binance", self.venue.account_id, "snapshot", metric_source_id
                    ),
                    venue="binance",
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
                )
                batch.add_row("account_snapshots", row)
        return batch

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

    def _discover_symbols(self) -> tuple[set[str], int]:
        payload = self.http.get_json("/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict):
            raise RuntimeError("exchangeInfo returned a non-object response")
        symbols = set()
        perpetual_count = 0
        for row in payload.get("symbols", []):
            if not isinstance(row, dict):
                continue
            if row.get("contractType") != "PERPETUAL" or not row.get("symbol"):
                continue
            perpetual_count += 1
            if row.get("status") == "TRADING":
                symbols.add(str(row["symbol"]))
        return symbols, perpetual_count

    def request_archive(self, kind: str, *, start_ms: int, end_ms: int) -> dict[str, Any]:
        try:
            path = BINANCE_ARCHIVE_ENDPOINTS[kind][0]
        except KeyError as exc:
            raise ValueError(f"unsupported Binance archive kind: {kind}") from exc
        payload = self._get_signed(path, {"startTime": start_ms, "endTime": end_ms})
        if not isinstance(payload, dict) or not payload.get("downloadId"):
            raise RuntimeError(f"Binance {kind} archive request returned no download id")
        return payload

    def poll_archive(self, kind: str, download_id: str) -> dict[str, Any]:
        try:
            path = BINANCE_ARCHIVE_ENDPOINTS[kind][1]
        except KeyError as exc:
            raise ValueError(f"unsupported Binance archive kind: {kind}") from exc
        payload = self._get_signed(path, {"downloadId": download_id})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Binance {kind} archive status returned a non-object response")
        return payload

    def download_archive(self, url: str) -> bytes:
        if url.startswith("//"):
            url = f"https:{url}"
        elif "://" not in url:
            url = f"https://{url.lstrip('/')}"
        return self.http.get_bytes_url(url)

    def normalize_archive_rows(
        self,
        kind: str,
        rows: Iterable[dict[str, Any]],
        *,
        collected_at: str,
    ) -> SourceBatch:
        definitions = {
            "orders": normalize_binance_order,
            "trades": normalize_binance_trade,
            "income": normalize_binance_income,
        }
        try:
            normalizer = definitions[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported Binance archive kind: {kind}") from exc
        canonical_rows = [
            canonicalize_binance_archive_row(kind, row)
            for row in rows
            if any(value not in (None, "") for value in row.values())
        ]
        required = {
            "orders": ("orderId", "time"),
            "trades": ("id", "time"),
            "income": ("incomeType", "time"),
        }[kind]
        for index, row in enumerate(canonical_rows, start=1):
            if any(row.get(field) in (None, "") for field in required):
                raise ValueError(
                    f"Binance {kind} archive row {index} lacks required columns {required}"
                )
        return self._normalize_rows(
            f"archive_{kind}", canonical_rows, collected_at, normalizer
        )

    def _get_signed(self, path: str, params: dict[str, Any]) -> Any:
        for clock_attempt in range(2):
            signed = binance_signed_query(
                params,
                api_secret=self.venue.api_secret,
                timestamp_ms=int(time.time() * 1000) + self._clock_offset_ms,
            )
            try:
                return self.http.get_json(
                    path,
                    params=signed,
                    headers={"X-MBX-APIKEY": self.venue.api_key},
                )
            except ApiError as exc:
                if str(exc.response_code) in {"-4141", "-1121"}:
                    raise TerminalContractError(
                        str(exc),
                        status_code=exc.status_code,
                        response_code=exc.response_code,
                    ) from exc
                if str(exc.response_code) != "-1021" or clock_attempt:
                    raise
                self._synchronize_clock()
        raise AssertionError("unreachable")

    def _synchronize_clock(self) -> None:
        started_ms = int(time.time() * 1000)
        payload = self.http.get_json("/fapi/v1/time")
        finished_ms = int(time.time() * 1000)
        if not isinstance(payload, dict) or not payload.get("serverTime"):
            raise ClockSkewError("Binance server time response was invalid")
        midpoint_ms = started_ms + (finished_ms - started_ms) // 2
        self._clock_offset_ms = int(payload["serverTime"]) - midpoint_ms
        LOGGER.warning(
            "Signed-request clock corrected: venue=%s offset_ms=%d",
            self.name,
            self._clock_offset_ms,
        )


def normalize_binance_income(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    income_type = str(payload.get("incomeType") or "OTHER")
    transaction_id = source_identity(payload, ("tranId",))
    source_id = stable_id(income_type, transaction_id)
    event_at, event_ms = timestamp_fields(payload.get("time"))
    event_type = BINANCE_INCOME_TYPES.get(income_type, "other")
    role = "informational" if event_type in {"realized_pnl", "commission"} else "primary"
    row = row_for(
        "cashflows",
        record_id=stable_id("binance", account_id, "cashflow", "income", source_id),
        venue="binance",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        event_type=event_type,
        event_subtype=income_type,
        symbol=payload.get("symbol"),
        currency=payload.get("asset"),
        amount=decimal_text(payload.get("income"), default="0"),
        trade_id=payload.get("tradeId"),
        reporting_role=role,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
        notes=str(payload.get("info") or ""),
    )
    return NormalizedRecord(source_id, {"cashflows": [row]})


def normalize_binance_trade(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    symbol = str(payload.get("symbol") or "")
    trade_id = source_identity(payload, ("id",))
    source_id = stable_id(symbol, trade_id)
    event_at, event_ms = timestamp_fields(payload.get("time"))
    settlement_currency = str(
        payload.get("marginAsset") or infer_settlement_currency(symbol)
    )
    side = str(payload.get("side") or "").lower()
    position_side = str(payload.get("positionSide") or "").lower()
    action = _binance_position_action(side, position_side)
    fee_currency = str(payload.get("commissionAsset") or payload.get("marginAsset") or "")
    row = row_for(
        "executions",
        record_id=stable_id("binance", account_id, "execution", source_id),
        venue="binance",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        symbol=symbol,
        side=side,
        position_action=action,
        position_side=position_side,
        liquidity="maker" if boolean_value(payload.get("maker")) else "taker",
        quantity=decimal_text(payload.get("qty")),
        quantity_unit="base",
        base_quantity=decimal_text(payload.get("baseQty") or payload.get("qty")),
        price=decimal_text(payload.get("price")),
        notional=decimal_text(payload.get("quoteQty")),
        settlement_currency=settlement_currency,
        realized_pnl=decimal_text(payload.get("realizedPnl"), default="0"),
        fee=negative_magnitude(payload.get("commission")),
        fee_currency=fee_currency,
        order_id=payload.get("orderId"),
        trade_id=trade_id,
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )
    cashflows: list[dict[str, str]] = []
    realized = Decimal(decimal_text(payload.get("realizedPnl"), default="0"))
    if realized:
        cashflows.append(
            _binance_trade_cashflow(
                account_id=account_id,
                source=source,
                source_id=source_id,
                event_at=event_at,
                event_ms=event_ms,
                event_type="realized_pnl",
                amount=decimal_text(realized),
                currency=settlement_currency,
                symbol=symbol,
                order_id=payload.get("orderId"),
                trade_id=trade_id,
                collected_at=collected_at,
            )
        )
    commission = Decimal(decimal_text(payload.get("commission"), default="0"))
    if commission:
        cashflows.append(
            _binance_trade_cashflow(
                account_id=account_id,
                source=source,
                source_id=source_id,
                event_at=event_at,
                event_ms=event_ms,
                event_type="commission",
                amount=negative_magnitude(commission),
                currency=fee_currency,
                symbol=symbol,
                order_id=payload.get("orderId"),
                trade_id=trade_id,
                collected_at=collected_at,
            )
        )
    return NormalizedRecord(
        source_id,
        {"executions": [row], "cashflows": cashflows},
    )


def normalize_binance_order(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    return _normalize_binance_order_like(
        payload,
        account_id=account_id,
        source=source,
        collected_at=collected_at,
        identifier_field="orderId",
        quantity_field="origQty",
        status_field="status",
        client_id_field="clientOrderId",
        type_field="type",
    )


def normalize_binance_algo_order(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    return _normalize_binance_order_like(
        payload,
        account_id=account_id,
        source=source,
        collected_at=collected_at,
        identifier_field="algoId",
        quantity_field="quantity",
        status_field="algoStatus",
        client_id_field="clientAlgoId",
        type_field="orderType",
    )


def normalize_binance_order_amendment(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    symbol = str(payload.get("symbol") or "")
    order_id = str(payload.get("orderId") or "")
    amendment_id = source_identity(payload, ("amendmentId",))
    source_id = stable_id(symbol, order_id, amendment_id)
    event_at, event_ms = timestamp_fields(payload.get("time"))
    amendment = payload.get("amendment")
    amendment = amendment if isinstance(amendment, dict) else {}
    sequence = amendment.get("count")
    modify_id = amendment.get("modifyId")
    rows: list[dict[str, str]] = []
    for field, value in amendment.items():
        if not isinstance(value, dict):
            continue
        if value.get("before") in (None, "") and value.get("after") in (None, ""):
            continue
        rows.append(
            row_for(
                "order_events",
                record_id=stable_id(
                    "binance",
                    account_id,
                    "order-event",
                    source_id,
                    field,
                ),
                venue="binance",
                account_id=account_id,
                market_type="perpetual",
                event_time=event_at,
                event_time_ms=event_ms,
                event_type="amendment",
                event_subtype="field_change",
                symbol=symbol,
                order_id=order_id,
                client_order_id=payload.get("clientOrderId"),
                amendment_id=amendment_id,
                sequence=sequence,
                field=field,
                before_value=value.get("before"),
                after_value=value.get("after"),
                source=source,
                source_id=source_id,
                source_updated_at=event_at,
                source_updated_at_ms=event_ms,
                collected_at=collected_at,
                notes=stable_id("modify", modify_id) if modify_id not in (None, "") else "",
            )
        )
    if not rows:
        rows.append(
            row_for(
                "order_events",
                record_id=stable_id(
                    "binance", account_id, "order-event", source_id, "summary"
                ),
                venue="binance",
                account_id=account_id,
                market_type="perpetual",
                event_time=event_at,
                event_time_ms=event_ms,
                event_type="amendment",
                event_subtype="summary",
                symbol=symbol,
                order_id=order_id,
                client_order_id=payload.get("clientOrderId"),
                amendment_id=amendment_id,
                sequence=sequence,
                source=source,
                source_id=source_id,
                source_updated_at=event_at,
                source_updated_at_ms=event_ms,
                collected_at=collected_at,
                notes=canonical_json(amendment),
            )
        )
    return NormalizedRecord(source_id, {"order_events": rows})


def normalize_binance_position(
    payload: dict[str, Any], *, account_id: str, source: str, collected_at: str
) -> NormalizedRecord:
    symbol = str(payload.get("symbol") or "")
    position_side = str(payload.get("positionSide") or "both").lower()
    source_id = stable_id(symbol, position_side)
    updated_at, updated_ms = timestamp_fields(payload.get("updateTime"))
    amount = Decimal(decimal_text(payload.get("positionAmt"), default="0"))
    if position_side == "both":
        position_side = "long" if amount > 0 else "short" if amount < 0 else "both"
    row = row_for(
        "positions",
        record_id=stable_id("binance", account_id, "position", source_id),
        venue="binance",
        account_id=account_id,
        market_type="perpetual",
        symbol=symbol,
        status="open" if amount else "closed",
        position_side=position_side,
        quantity=decimal_text(abs(amount)),
        quantity_unit="base",
        open_price=decimal_text(payload.get("entryPrice")),
        settlement_currency=payload.get("marginAsset"),
        leverage=decimal_text(payload.get("leverage")),
        margin_mode=(
            payload.get("marginType")
            or (
                "isolated"
                if payload.get("isolatedMargin") not in (None, "0", "0.0")
                else "cross"
            )
        ),
        liquidation_price=decimal_text(payload.get("liquidationPrice")),
        position_id=source_id,
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"positions": [row]})


def _normalize_binance_order_like(
    payload: dict[str, Any],
    *,
    account_id: str,
    source: str,
    collected_at: str,
    identifier_field: str,
    quantity_field: str,
    status_field: str,
    client_id_field: str,
    type_field: str,
) -> NormalizedRecord:
    symbol = str(payload.get("symbol") or "")
    identifier = source_identity(payload, (identifier_field,))
    source_id = stable_id(symbol, identifier)
    created_at, created_ms = timestamp_fields(payload.get("time") or payload.get("createTime"))
    updated_at, updated_ms = timestamp_fields(
        payload.get("updateTime") or payload.get("triggerTime") or payload.get("time")
    )
    side = str(payload.get("side") or "").lower()
    position_side = str(payload.get("positionSide") or "").lower()
    reduce_only = boolean_value(payload.get("reduceOnly")) or boolean_value(
        payload.get("closePosition")
    )
    action = "close" if reduce_only else _binance_position_action(side, position_side)
    record_kind = "algo-order" if identifier_field == "algoId" else "order"
    row = row_for(
        "orders",
        record_id=stable_id("binance", account_id, record_kind, source_id),
        venue="binance",
        account_id=account_id,
        market_type="perpetual",
        created_at=created_at,
        created_at_ms=created_ms,
        updated_at=updated_at,
        updated_at_ms=updated_ms,
        symbol=symbol,
        side=side,
        position_action=action,
        position_side=position_side,
        order_type=str(payload.get(type_field) or payload.get("algoType") or "").lower(),
        time_in_force=str(payload.get("timeInForce") or "").lower(),
        status=str(payload.get(status_field) or "").lower(),
        quantity=decimal_text(payload.get(quantity_field)),
        quantity_unit="base",
        filled_quantity=decimal_text(payload.get("executedQty"), default="0"),
        price=decimal_text(payload.get("price")),
        trigger_price=decimal_text(payload.get("triggerPrice") or payload.get("stopPrice")),
        trigger_type=payload.get("workingType"),
        activation_price=decimal_text(payload.get("activatePrice")),
        callback_rate=decimal_text(payload.get("priceRate") or payload.get("callbackRate")),
        average_price=decimal_text(payload.get("avgPrice") or payload.get("actualPrice")),
        reduce_only=bool_text(payload.get("reduceOnly")),
        close_only=bool_text(payload.get("closePosition")),
        client_order_id=payload.get(client_id_field),
        order_id=identifier,
        linked_order_id=(
            payload.get("actualOrderId") or payload.get("orderId")
            if identifier_field == "algoId"
            else ""
        ),
        failure_reason=payload.get("algoStatusMsg"),
        source=source,
        source_id=source_id,
        source_updated_at=updated_at,
        source_updated_at_ms=updated_ms,
        collected_at=collected_at,
    )
    return NormalizedRecord(source_id, {"orders": [row]})


def _binance_trade_cashflow(
    *,
    account_id: str,
    source: str,
    source_id: str,
    event_at: str,
    event_ms: str,
    event_type: str,
    amount: str,
    currency: str,
    symbol: str,
    order_id: Any,
    trade_id: Any,
    collected_at: str,
) -> dict[str, str]:
    return row_for(
        "cashflows",
        record_id=stable_id("binance", account_id, "cashflow", event_type, source_id),
        venue="binance",
        account_id=account_id,
        market_type="perpetual",
        event_time=event_at,
        event_time_ms=event_ms,
        event_type=event_type,
        event_subtype="fill",
        symbol=symbol,
        currency=currency,
        amount=amount,
        order_id=order_id,
        trade_id=trade_id,
        reporting_role="primary",
        source=source,
        source_id=source_id,
        source_updated_at=event_at,
        source_updated_at_ms=event_ms,
        collected_at=collected_at,
    )


def _binance_position_action(side: str, position_side: str) -> str:
    side = side.lower()
    position_side = position_side.lower()
    if position_side == "long":
        return "open_long" if side == "buy" else "close_long"
    if position_side == "short":
        return "open_short" if side == "sell" else "close_short"
    return "buy" if side == "buy" else "sell" if side == "sell" else ""


def _dedupe_binance_rows(
    rows: Iterable[dict[str, Any]], identity_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = [
            str(row.get(field))
            for field in identity_fields
            if row.get(field) not in (None, "")
        ]
        identity = stable_id(*values) if values else source_identity(row, ())
        by_id[identity] = row
    return list(by_id.values())

def canonicalize_binance_archive_row(
    kind: str, row: dict[str, Any]
) -> dict[str, Any]:
    try:
        columns = BINANCE_ARCHIVE_COLUMNS[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported Binance archive kind: {kind}") from exc

    canonical: dict[str, Any] = {}

    for original, value in row.items():
        key = "".join(
            character for character in str(original).lower()
            if character.isalnum()
        )
        target = columns.get(key)
        if not target:
            continue

        if kind == "trades" and key == "fee":
            parts = str(value or "").strip().split(maxsplit=1)
            canonical["commission"] = parts[0] if parts else ""
            if len(parts) > 1:
                canonical["commissionAsset"] = parts[1]
            continue

        canonical[target] = value

    return canonical
