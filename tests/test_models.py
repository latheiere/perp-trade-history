from decimal import Decimal

import pytest

from perp_trade_history.models import (
    decimal_text,
    infer_settlement_currency,
    stable_id,
    timestamp_fields,
    to_milliseconds,
)


def test_timestamp_conversion_accepts_seconds_milliseconds_and_iso() -> None:
    expected = 1_700_000_000_123
    assert to_milliseconds("1700000000.123") == expected
    assert to_milliseconds(expected) == expected
    assert to_milliseconds("2023-11-14T22:13:20.123Z") == expected
    assert timestamp_fields(expected) == ("2023-11-14T22:13:20.123Z", str(expected))


def test_decimal_text_is_lossless_and_rejects_non_finite_values() -> None:
    assert decimal_text(Decimal("12.340000")) == "12.34"
    assert decimal_text("-0.000") == "0"
    assert decimal_text(None) == ""
    with pytest.raises(ValueError, match="non-finite"):
        decimal_text("NaN")


def test_stable_identity_is_human_readable_and_unambiguous() -> None:
    assert stable_id("venue", "account one", "record:1") == "venue:account%20one:record%3A1"


def test_settlement_currency_inference_handles_common_contract_notation() -> None:
    assert infer_settlement_currency("ASSET_USDT") == "USDT"
    assert infer_settlement_currency("ASSET/USD:USD") == "USD"
    assert infer_settlement_currency("UNKNOWN", "eur") == "EUR"
