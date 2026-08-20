from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = "1"

COMMON_FIELDS = [
    "schema_version",
    "record_id",
    "venue",
    "account_id",
    "market_type",
]

CASHFLOW_FIELDS = COMMON_FIELDS + [
    "event_time",
    "event_time_ms",
    "event_type",
    "event_subtype",
    "symbol",
    "currency",
    "amount",
    "order_id",
    "trade_id",
    "position_id",
    "reporting_role",
    "source",
    "source_id",
    "source_updated_at",
    "source_updated_at_ms",
    "collected_at",
    "raw_ref",
    "notes",
]

EXECUTION_FIELDS = COMMON_FIELDS + [
    "event_time",
    "event_time_ms",
    "symbol",
    "side",
    "position_action",
    "position_side",
    "liquidity",
    "quantity",
    "quantity_unit",
    "contract_size",
    "base_quantity",
    "price",
    "notional",
    "settlement_currency",
    "realized_pnl",
    "fee",
    "fee_currency",
    "order_id",
    "trade_id",
    "source",
    "source_id",
    "source_updated_at",
    "source_updated_at_ms",
    "collected_at",
    "raw_ref",
]

ORDER_FIELDS = COMMON_FIELDS + [
    "created_at",
    "created_at_ms",
    "updated_at",
    "updated_at_ms",
    "symbol",
    "side",
    "position_action",
    "position_side",
    "order_type",
    "time_in_force",
    "status",
    "quantity",
    "quantity_unit",
    "filled_quantity",
    "price",
    "trigger_price",
    "trigger_type",
    "activation_price",
    "callback_rate",
    "average_price",
    "reduce_only",
    "close_only",
    "leverage",
    "margin_mode",
    "client_order_id",
    "order_id",
    "linked_order_id",
    "failure_reason",
    "source",
    "source_id",
    "source_updated_at",
    "source_updated_at_ms",
    "collected_at",
    "raw_ref",
]

ORDER_EVENT_FIELDS = COMMON_FIELDS + [
    "event_time",
    "event_time_ms",
    "event_type",
    "event_subtype",
    "symbol",
    "order_id",
    "client_order_id",
    "amendment_id",
    "sequence",
    "field",
    "before_value",
    "after_value",
    "source",
    "source_id",
    "source_updated_at",
    "source_updated_at_ms",
    "collected_at",
    "raw_ref",
    "notes",
]

POSITION_FIELDS = COMMON_FIELDS + [
    "opened_at",
    "opened_at_ms",
    "closed_at",
    "closed_at_ms",
    "symbol",
    "status",
    "position_side",
    "quantity",
    "quantity_unit",
    "max_quantity",
    "open_price",
    "close_price",
    "realized_pnl",
    "funding",
    "fees",
    "settlement_currency",
    "leverage",
    "margin_mode",
    "liquidation_price",
    "position_id",
    "source",
    "source_id",
    "source_updated_at",
    "source_updated_at_ms",
    "collected_at",
    "raw_ref",
]

SNAPSHOT_FIELDS = COMMON_FIELDS + [
    "observed_at",
    "observed_at_ms",
    "currency",
    "metric",
    "value",
    "source",
    "source_id",
    "collected_at",
    "raw_ref",
]

COVERAGE_FIELDS = [
    "schema_version",
    "record_id",
    "venue",
    "account_id",
    "market_type",
    "dataset",
    "source",
    "acquisition",
    "scope",
    "start_time",
    "start_time_ms",
    "end_time",
    "end_time_ms",
    "status",
    "limitation",
    "collected_at",
]

CONVERSION_RATE_FIELDS = [
    "schema_version",
    "record_id",
    "venue",
    "base_currency",
    "quote_currency",
    "price_date",
    "open_time",
    "open_time_ms",
    "close_time",
    "close_time_ms",
    "open",
    "close",
    "complete",
    "source",
    "collected_at",
]

CASHFLOW_CONVERSION_FIELDS = [
    "schema_version",
    "record_id",
    "venue",
    "cashflow_record_id",
    "cashflow_fingerprint",
    "converted_amount",
    "conversion_spec",
    "converted_at",
]

TABLE_FIELDS: dict[str, list[str]] = {
    "cashflows": CASHFLOW_FIELDS,
    "executions": EXECUTION_FIELDS,
    "orders": ORDER_FIELDS,
    "order_events": ORDER_EVENT_FIELDS,
    "positions": POSITION_FIELDS,
    "account_snapshots": SNAPSHOT_FIELDS,
    "coverage": COVERAGE_FIELDS,
    "conversion_rates": CONVERSION_RATE_FIELDS,
    "cashflow_conversions": CASHFLOW_CONVERSION_FIELDS,
}


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_now_iso() -> str:
    return format_datetime(utc_now())


def format_datetime(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_milliseconds(value: Any) -> int:
    """Convert seconds, milliseconds, or an ISO timestamp to Unix milliseconds."""
    if value in (None, ""):
        return 0
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    if isinstance(value, str) and any(character in value for character in "-:TZ"):
        return int(parse_datetime(value).timestamp() * 1000)
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if number.copy_abs() < Decimal("100000000000"):
        number *= 1000
    return int(number)


def timestamp_fields(value: Any) -> tuple[str, str]:
    milliseconds = to_milliseconds(value)
    if not milliseconds:
        return "", ""
    instant = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    return format_datetime(instant), str(milliseconds)


def decimal_text(value: Any, *, default: str = "") -> str:
    if value in (None, ""):
        return default
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"", "-0"}:
        return "0"
    return normalized


def negative_magnitude(value: Any) -> str:
    text = decimal_text(value, default="0")
    return decimal_text(-abs(Decimal(text)))


def stable_id(*parts: Any) -> str:
    encoded = [quote(str(part if part not in (None, "") else "_"), safe="-_.~") for part in parts]
    return ":".join(encoded)


def content_id(prefix: str, payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return stable_id(prefix, digest)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bool_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return "true" if boolean_value(value) else "false"


def boolean_value(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return bool(value)


def infer_settlement_currency(symbol: str | None, fallback: str = "") -> str:
    if not symbol:
        return fallback.upper()
    normalized = symbol.replace("/", "_").replace(":", "_").upper()
    pieces = [piece for piece in normalized.split("_") if piece]
    currencies = (
        "BUSD",
        "USDT",
        "USDC",
        "USD",
        "BTC",
        "ETH",
        "EUR",
    )
    for piece in reversed(pieces):
        if piece in currencies:
            return piece
        for candidate in currencies:
            if piece.endswith(candidate):
                return candidate
    return fallback.upper()


def canonical_settlement_currency(currency: str | None, symbol: str | None) -> str:
    """Repair absent or prematurely suffix-matched settlement currencies."""
    reported = str(currency or "").upper()
    inferred = infer_settlement_currency(symbol)
    if not reported:
        return inferred
    if reported == "USD" and inferred not in {"", "USD"} and inferred.endswith("USD"):
        return inferred
    return reported


def compact_base_symbol(symbol: str | None) -> str:
    """Return the base asset from common perpetual and delivery contract notation."""
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return ""
    normalized = normalized.replace("/", "_").replace(":", "_").replace("-", "_")
    pieces = [piece for piece in normalized.split("_") if piece]
    while len(pieces) > 1 and (pieces[-1].isdigit() or pieces[-1] in {"PERP", "PERPETUAL"}):
        pieces.pop()
    joined = "_".join(pieces)
    quotes = (
        "BUSD",
        "USDT",
        "USDC",
        "USD",
        "BTC",
        "ETH",
        "EUR",
    )
    for quote_currency in quotes:
        for suffix in (f"_{quote_currency}", quote_currency):
            if joined.endswith(suffix) and len(joined) > len(suffix):
                return joined[: -len(suffix)].rstrip("_")
    return joined


def row_for(table: str, **values: Any) -> dict[str, str]:
    fields = TABLE_FIELDS[table]
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"unknown {table} fields: {sorted(unknown)}")
    row = {field: "" for field in fields}
    row.update({key: str(value) if value is not None else "" for key, value in values.items()})
    row["schema_version"] = SCHEMA_VERSION
    return row


def source_identity(row: dict[str, Any], candidates: Iterable[str]) -> str:
    for candidate in candidates:
        value = row.get(candidate)
        if value not in (None, ""):
            return str(value)
    return content_id("content", row)
