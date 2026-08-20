from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from perp_trade_history.config import AppConfig
from perp_trade_history.http import ReadOnlyHttp
from perp_trade_history.models import (
    canonical_settlement_currency,
    decimal_text,
    row_for,
    stable_id,
    timestamp_fields,
    utc_now_iso,
)
from perp_trade_history.storage import DataStore

DAY_MS = 24 * 60 * 60 * 1000


class ConversionError(ValueError):
    """Collected cashflows cannot use the configured reporting conversion."""


@dataclass(frozen=True, slots=True)
class DailyRate:
    venue: str
    base_currency: str
    quote_currency: str
    price_date: date
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    close: Decimal
    complete: bool
    source: str


class DailyRateAdapter(ABC):
    venue: str
    maximum_days_per_request = 1000

    def __init__(self, http: ReadOnlyHttp):
        self.http = http

    def fetch(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        rates: list[DailyRate] = []
        for window in _date_windows(dates, self.maximum_days_per_request):
            rates.extend(
                self._fetch_window(
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    dates=set(window),
                )
            )
        return rates

    @abstractmethod
    def _fetch_window(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        """Fetch one bounded UTC daily-rate window."""


class BinanceDailyRateAdapter(DailyRateAdapter):
    venue = "binance"

    def _fetch_window(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        start_ms, end_ms = _window_milliseconds(dates)
        payload = self.http.get_json(
            "/api/v3/klines",
            params={
                "symbol": f"{base_currency}{quote_currency}",
                "interval": "1d",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": self.maximum_days_per_request,
            },
        )
        return _array_rates(
            payload,
            venue=self.venue,
            base_currency=base_currency,
            quote_currency=quote_currency,
            dates=dates,
            timestamp_multiplier=1,
            open_index=1,
            close_index=4,
            close_time_index=6,
            source="/api/v3/klines",
        )


class GateDailyRateAdapter(DailyRateAdapter):
    venue = "gate"

    def _fetch_window(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        start_ms, end_ms = _window_milliseconds(dates)
        payload = self.http.get_json(
            "/api/v4/spot/candlesticks",
            params={
                "currency_pair": f"{base_currency}_{quote_currency}",
                "from": start_ms // 1000,
                "to": end_ms // 1000,
                "interval": "1d",
            },
        )
        return _array_rates(
            payload,
            venue=self.venue,
            base_currency=base_currency,
            quote_currency=quote_currency,
            dates=dates,
            timestamp_multiplier=1000,
            open_index=5,
            close_index=2,
            close_time_index=None,
            source="/api/v4/spot/candlesticks",
        )


class MexcDailyRateAdapter(DailyRateAdapter):
    venue = "mexc"

    def _fetch_window(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        start_ms, end_ms = _window_milliseconds(dates)
        payload = self.http.get_json(
            "/api/v3/klines",
            params={
                "symbol": f"{base_currency}{quote_currency}",
                "interval": "1d",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": self.maximum_days_per_request,
            },
        )
        return _array_rates(
            payload,
            venue=self.venue,
            base_currency=base_currency,
            quote_currency=quote_currency,
            dates=dates,
            timestamp_multiplier=1,
            open_index=1,
            close_index=4,
            close_time_index=6,
            source="/api/v3/klines",
        )


@dataclass(slots=True)
class ConversionResult:
    venue: str
    cashflows: int
    converted: int
    rates_fetched: int
    tables: dict[str, dict[str, int]]
    error: str = ""


class CashflowConversionPass:
    """Run the shared post-collection or manual cashflow conversion pass."""

    def __init__(
        self,
        store: DataStore,
        config: AppConfig,
        *,
        adapters: dict[str, DailyRateAdapter] | None = None,
    ):
        self.store = store
        self.target_currency = config.reporting.target_currency
        self.method = config.reporting.conversion_method
        self.fixed_rates = config.reporting.fixed_rates
        self.spec = conversion_spec(
            self.target_currency, self.method, self.fixed_rates
        )
        self.adapters = adapters or _configured_adapters(config)

    def run(
        self,
        *,
        venues: set[str] | None = None,
        recalculate: bool = False,
        today: date | None = None,
    ) -> list[ConversionResult]:
        current_date = today or datetime.now(tz=UTC).date()
        rows_by_venue: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.store.tables["cashflows"].iter_read():
            venue = str(row.get("venue") or "")
            if venues is None or venue in venues:
                rows_by_venue[venue].append(row)

        saved_rates = _load_rates(self.store)
        saved_conversions = _load_conversions(self.store)
        results: list[ConversionResult] = []
        for venue, rows in sorted(rows_by_venue.items()):
            if not recalculate and any(
                saved_conversions.get(row["record_id"], {}).get("conversion_spec")
                and saved_conversions[row["record_id"]]["conversion_spec"] != self.spec
                for row in rows
            ):
                results.append(
                    ConversionResult(
                        venue=venue,
                        cashflows=len(rows),
                        converted=0,
                        rates_fetched=0,
                        tables={},
                        error=(
                            "reporting conversion configuration changed; run "
                            "recalculate-conversions before collecting or reporting"
                        ),
                    )
                )
                continue

            pending = (
                rows
                if recalculate
                else [
                    row
                    for row in rows
                    if not _conversion_matches(
                        saved_conversions.get(row["record_id"]),
                        row,
                        self.spec,
                    )
                ]
            )
            requirements, errors = _required_rates(
                pending,
                target_currency=self.target_currency,
                method=self.method,
                today=current_date,
                fixed_rates=self.fixed_rates,
            )
            fetched: list[DailyRate] = []
            adapter = self.adapters.get(venue)
            for (base_currency, quote_currency), dates in sorted(requirements.items()):
                missing = {
                    value
                    for value in dates
                    if not _rate_available(
                        saved_rates.get(
                            (venue, base_currency, quote_currency, value)
                        ),
                        self.method,
                    )
                }
                if not missing:
                    continue
                fixed_rate = self.fixed_rates.get(base_currency)
                if fixed_rate is not None:
                    fetched.extend(
                        _fixed_daily_rates(
                            venue=venue,
                            base_currency=base_currency,
                            quote_currency=quote_currency,
                            dates=missing,
                            rate=fixed_rate,
                        )
                    )
                    continue
                if adapter is None:
                    errors.append(f"venue={venue} daily_rate_adapter=unavailable")
                    continue
                try:
                    fetched.extend(
                        adapter.fetch(
                            base_currency=base_currency,
                            quote_currency=quote_currency,
                            dates=missing,
                        )
                    )
                except Exception as exc:
                    errors.append(
                        f"venue={venue} pair={base_currency}/{quote_currency} request={exc}"
                    )

            tables: dict[str, dict[str, int]] = {}
            rate_stats = _persist_rates(self.store, fetched)
            if rate_stats:
                tables["conversion_rates"] = rate_stats
            for rate in fetched:
                saved_rates[
                    (rate.venue, rate.base_currency, rate.quote_currency, rate.price_date)
                ] = rate

            converted: list[dict[str, str]] = []
            for row in pending:
                try:
                    converted.append(
                        _conversion_row(
                            row,
                            target_currency=self.target_currency,
                            method=self.method,
                            spec=self.spec,
                            rates=saved_rates,
                            today=current_date,
                            fixed_rates=self.fixed_rates,
                        )
                    )
                except ConversionError as exc:
                    errors.append(str(exc))
            if converted:
                tables["cashflow_conversions"] = self.store.upsert(
                    "cashflow_conversions", converted
                ).to_dict()
            results.append(
                ConversionResult(
                    venue=venue,
                    cashflows=len(rows),
                    converted=len(converted),
                    rates_fetched=len(fetched),
                    tables=tables,
                    error=_error_summary(errors),
                )
            )
        return results


class StoredCashflowConversion:
    """Read only conversion values persisted by the shared conversion pass."""

    def __init__(
        self,
        store: DataStore,
        *,
        target_currency: str,
        method: str,
        fixed_rates: dict[str, Decimal] | None = None,
    ):
        self.store = store
        self.target_currency = target_currency
        self.spec = conversion_spec(target_currency, method, fixed_rates or {})
        self._amounts: dict[str, Decimal] = {}

    def prepare(self, rows: Iterable[dict[str, str]]) -> None:
        conversions = _load_conversions(self.store)
        missing = False
        stale = False
        for row in rows:
            conversion = conversions.get(row["record_id"])
            if conversion and conversion.get("conversion_spec") != self.spec:
                stale = True
            elif not _conversion_matches(conversion, row, self.spec):
                missing = True
            else:
                self._amounts[row["record_id"]] = Decimal(
                    conversion["converted_amount"]
                )
        if stale:
            raise ConversionError(
                "stored conversions do not match the configured reporting currency and "
                "pricing method; run recalculate-conversions"
            )
        if missing:
            raise ConversionError(
                "cashflows are missing stored converted values; run collection or "
                "recalculate-conversions"
            )

    def amount(self, row: dict[str, str]) -> Decimal:
        return self._amounts[row["record_id"]]

    def currency(self, row: dict[str, str]) -> str:
        return self.target_currency


def conversion_spec(
    target_currency: str,
    method: str,
    fixed_rates: dict[str, Decimal] | None = None,
) -> str:
    base = f"{target_currency.upper()}|{method}"
    rates = fixed_rates or {}
    if not rates:
        return base
    rendered = ",".join(
        f"{asset}={decimal_text(rate)}" for asset, rate in sorted(rates.items())
    )
    return f"{base}|fixed:{rendered}"


def _required_rates(
    rows: Iterable[dict[str, str]],
    *,
    target_currency: str,
    method: str,
    today: date,
    fixed_rates: dict[str, Decimal],
) -> tuple[dict[tuple[str, str], set[date]], list[str]]:
    required: dict[tuple[str, str], set[date]] = defaultdict(set)
    errors: list[str] = []
    day_selector, price_field = method.split(".", 1)
    for row in rows:
        base_currency = canonical_settlement_currency(
            row.get("currency"), row.get("symbol")
        )
        if not base_currency:
            errors.append(
                "cashflow settlement currency could not be inferred: "
                f"record_id={row.get('record_id', '')}"
            )
            continue
        if base_currency == target_currency:
            continue
        event_date = datetime.fromtimestamp(
            int(row.get("event_time_ms") or 0) / 1000, tz=UTC
        ).date()
        price_date = event_date - timedelta(days=day_selector == "previous_day")
        if (
            price_field == "close"
            and price_date >= today
            and base_currency not in fixed_rates
        ):
            errors.append(
                "configured daily close is not complete at collection time: "
                f"date={price_date.isoformat()}"
            )
            continue
        required[(base_currency, target_currency)].add(price_date)
    return required, errors


def _conversion_row(
    row: dict[str, str],
    *,
    target_currency: str,
    method: str,
    spec: str,
    rates: dict[tuple[str, str, str, date], DailyRate],
    today: date,
    fixed_rates: dict[str, Decimal],
) -> dict[str, str]:
    amount = Decimal(str(row.get("amount") or "0"))
    base_currency = canonical_settlement_currency(row.get("currency"), row.get("symbol"))
    if not base_currency:
        raise ConversionError(
            "cashflow settlement currency could not be inferred: "
            f"record_id={row.get('record_id', '')}"
        )
    if base_currency == target_currency:
        converted_amount = amount
    else:
        day_selector, price_field = method.split(".", 1)
        event_date = datetime.fromtimestamp(
            int(row.get("event_time_ms") or 0) / 1000, tz=UTC
        ).date()
        price_date = event_date - timedelta(days=day_selector == "previous_day")
        if (
            price_field == "close"
            and price_date >= today
            and base_currency not in fixed_rates
        ):
            raise ConversionError(
                "configured daily close is not complete at collection time: "
                f"date={price_date.isoformat()}"
            )
        key = (str(row.get("venue") or ""), base_currency, target_currency, price_date)
        rate = rates.get(key)
        if not _rate_available(rate, method):
            raise ConversionError(
                "daily conversion rate was not collected: "
                f"venue={key[0]} "
                f"pair={base_currency}/{target_currency} date={price_date.isoformat()}"
            )
        converted_amount = amount * getattr(rate, price_field)
    return row_for(
        "cashflow_conversions",
        record_id=row["record_id"],
        venue=row.get("venue", ""),
        cashflow_record_id=row["record_id"],
        cashflow_fingerprint=_cashflow_fingerprint(row),
        converted_amount=decimal_text(converted_amount, default="0"),
        conversion_spec=spec,
        converted_at=utc_now_iso(),
    )


def _load_conversions(store: DataStore) -> dict[str, dict[str, str]]:
    return {
        row["cashflow_record_id"]: row
        for row in store.tables["cashflow_conversions"].iter_read()
    }


def _cashflow_fingerprint(row: dict[str, str]) -> str:
    return stable_id(
        row.get("venue", ""),
        row.get("symbol", ""),
        row.get("currency", ""),
        row.get("amount", ""),
        row.get("event_time_ms", ""),
    )


def _conversion_matches(
    conversion: dict[str, str] | None,
    cashflow: dict[str, str],
    spec: str,
) -> bool:
    return bool(
        conversion
        and conversion.get("converted_amount")
        and conversion.get("conversion_spec") == spec
        and conversion.get("cashflow_fingerprint") == _cashflow_fingerprint(cashflow)
    )


def _load_rates(
    store: DataStore,
) -> dict[tuple[str, str, str, date], DailyRate]:
    rates: dict[tuple[str, str, str, date], DailyRate] = {}
    for row in store.tables["conversion_rates"].iter_read():
        try:
            price_date = date.fromisoformat(row["price_date"])
            rate = DailyRate(
                venue=row["venue"],
                base_currency=row["base_currency"],
                quote_currency=row["quote_currency"],
                price_date=price_date,
                open_time_ms=int(row["open_time_ms"]),
                close_time_ms=int(row["close_time_ms"]),
                open=Decimal(row["open"]),
                close=Decimal(row["close"]),
                complete=row["complete"] == "true",
                source=row["source"],
            )
        except (InvalidOperation, KeyError, ValueError) as exc:
            raise ConversionError(
                f"invalid persisted conversion rate: record_id={row.get('record_id', '')}"
            ) from exc
        rates[(rate.venue, rate.base_currency, rate.quote_currency, price_date)] = rate
    return rates


def _persist_rates(store: DataStore, rates: list[DailyRate]) -> dict[str, int]:
    if not rates:
        return {}
    collected_at = utc_now_iso()
    rows: list[dict[str, str]] = []
    for rate in rates:
        open_time, open_time_ms = timestamp_fields(rate.open_time_ms)
        close_time, close_time_ms = timestamp_fields(rate.close_time_ms)
        rows.append(
            row_for(
                "conversion_rates",
                record_id=stable_id(
                    rate.venue,
                    rate.base_currency,
                    rate.quote_currency,
                    rate.price_date.isoformat(),
                ),
                venue=rate.venue,
                base_currency=rate.base_currency,
                quote_currency=rate.quote_currency,
                price_date=rate.price_date.isoformat(),
                open_time=open_time,
                open_time_ms=open_time_ms,
                close_time=close_time,
                close_time_ms=close_time_ms,
                open=decimal_text(rate.open),
                close=decimal_text(rate.close),
                complete="true" if rate.complete else "false",
                source=rate.source,
                collected_at=collected_at,
            )
        )
    return store.upsert("conversion_rates", rows).to_dict()


def _fixed_daily_rates(
    *,
    venue: str,
    base_currency: str,
    quote_currency: str,
    dates: set[date],
    rate: Decimal,
) -> list[DailyRate]:
    output: list[DailyRate] = []
    for price_date in sorted(dates):
        open_time_ms = int(
            datetime.combine(price_date, datetime.min.time(), tzinfo=UTC).timestamp()
            * 1000
        )
        output.append(
            DailyRate(
                venue=venue,
                base_currency=base_currency,
                quote_currency=quote_currency,
                price_date=price_date,
                open_time_ms=open_time_ms,
                close_time_ms=open_time_ms + DAY_MS - 1,
                open=rate,
                close=rate,
                complete=True,
                source="config:fixed_rate",
            )
        )
    return output


def _configured_adapters(config: AppConfig) -> dict[str, DailyRateAdapter]:
    settings = {
        "binance": (BinanceDailyRateAdapter, "https://api.binance.com"),
        "gate": (GateDailyRateAdapter, "https://api.gateio.ws"),
        "mexc": (MexcDailyRateAdapter, "https://api.mexc.com"),
    }
    adapters: dict[str, DailyRateAdapter] = {}
    for venue, (adapter_type, default_base_url) in settings.items():
        venue_settings = config.venues[venue]
        base_url = str(venue_settings.options.get("spot_base_url") or default_base_url)
        adapters[venue] = adapter_type(
            ReadOnlyHttp(
                base_url,
                timeout_seconds=config.collection.request_timeout_seconds,
                retries=config.collection.request_retries,
                minimum_interval_seconds=0.05,
            )
        )
    return adapters


def _date_windows(values: set[date], maximum_days: int) -> list[list[date]]:
    windows: list[list[date]] = []
    current: list[date] = []
    for value in sorted(values):
        if current and (value - current[0]).days >= maximum_days:
            windows.append(current)
            current = []
        current.append(value)
    if current:
        windows.append(current)
    return windows


def _window_milliseconds(dates: set[date]) -> tuple[int, int]:
    start = datetime.combine(min(dates), datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(max(dates) + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def _array_rates(
    payload: Any,
    *,
    venue: str,
    base_currency: str,
    quote_currency: str,
    dates: set[date],
    timestamp_multiplier: int,
    open_index: int,
    close_index: int,
    close_time_index: int | None,
    source: str,
) -> list[DailyRate]:
    if not isinstance(payload, list):
        raise ValueError("daily spot-rate endpoint returned a non-list response")
    output: list[DailyRate] = []
    for item in payload:
        if not isinstance(item, list):
            continue
        required_index = max(open_index, close_index, close_time_index or 0)
        if len(item) <= required_index:
            continue
        try:
            open_time_ms = int(Decimal(str(item[0]))) * timestamp_multiplier
            open_price = Decimal(str(item[open_index]))
            close_price = Decimal(str(item[close_index]))
            close_time_ms = (
                int(Decimal(str(item[close_time_index]))) * timestamp_multiplier
                if close_time_index is not None
                else open_time_ms + DAY_MS - 1
            )
        except (InvalidOperation, TypeError, ValueError):
            continue
        price_date = datetime.fromtimestamp(open_time_ms / 1000, tz=UTC).date()
        if price_date not in dates:
            continue
        if any(
            not value.is_finite() or value <= 0 for value in (open_price, close_price)
        ):
            continue
        output.append(
            DailyRate(
                venue=venue,
                base_currency=base_currency,
                quote_currency=quote_currency,
                price_date=price_date,
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
                open=open_price,
                close=close_price,
                complete=price_date < datetime.now(tz=UTC).date(),
                source=source,
            )
        )
    return output


def _rate_available(rate: DailyRate | None, method: str) -> bool:
    if rate is None:
        return False
    return not method.endswith(".close") or rate.complete


def _error_summary(errors: Iterable[str], *, limit: int = 12) -> str:
    unique = list(dict.fromkeys(errors))
    rendered = "; ".join(unique[:limit])
    if len(unique) > limit:
        rendered += f"; additional_errors={len(unique) - limit}"
    return rendered
