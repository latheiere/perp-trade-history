from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from perp_trade_history.conversion import (
    DAY_MS,
    BinanceDailyRateAdapter,
    CashflowConversionPass,
    ConversionError,
    DailyRate,
    DailyRateAdapter,
    GateDailyRateAdapter,
    MexcDailyRateAdapter,
    StoredCashflowConversion,
)
from perp_trade_history.models import row_for
from perp_trade_history.storage import DataStore


class StubDailyRateAdapter(DailyRateAdapter):
    venue = "venue"

    def __init__(self, rates: dict[date, tuple[Decimal, Decimal]]):
        self.rates = rates
        self.calls: list[set[date]] = []

    def _fetch_window(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        dates: set[date],
    ) -> list[DailyRate]:
        self.calls.append(dates)
        output: list[DailyRate] = []
        for price_date in sorted(dates):
            prices = self.rates.get(price_date)
            if prices is None:
                continue
            open_ms = int(
                datetime.combine(price_date, datetime.min.time(), tzinfo=UTC).timestamp()
                * 1000
            )
            output.append(
                DailyRate(
                    venue=self.venue,
                    base_currency=base_currency,
                    quote_currency=quote_currency,
                    price_date=price_date,
                    open_time_ms=open_ms,
                    close_time_ms=open_ms + DAY_MS - 1,
                    open=prices[0],
                    close=prices[1],
                    complete=True,
                    source="stub/daily",
                )
            )
        return output


class FakeHttp:
    def __init__(self, payload: Any):
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, path: str, *, params: dict[str, Any]) -> Any:
        self.calls.append((path, params))
        return self.payload


def _config(
    *,
    target: str = "USDT",
    method: str = "previous_day.close",
    fixed_rates: dict[str, Decimal] | None = None,
) -> Any:
    return SimpleNamespace(
        reporting=SimpleNamespace(
            target_currency=target,
            conversion_method=method,
            fixed_rates=fixed_rates or {},
        )
    )


def _flow(record_id: str, *, currency: str, amount: str) -> dict[str, str]:
    return row_for(
        "cashflows",
        record_id=record_id,
        venue="venue",
        account_id="primary",
        market_type="perpetual",
        event_time="2023-01-02T12:00:00.000Z",
        event_time_ms="1672660800000",
        event_type="funding",
        symbol=f"ASSET{currency}",
        currency=currency,
        amount=amount,
        reporting_role="primary",
        source="ledger",
        source_id=record_id,
        collected_at="2023-01-03T00:00:00.000Z",
    )


def test_shared_pass_persists_rates_once_and_converted_amounts(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    price_date = date(2023, 1, 1)
    adapter = StubDailyRateAdapter({price_date: (Decimal("2"), Decimal("2.5"))})
    store.upsert(
        "cashflows",
        [
            _flow("one", currency="QUOTE", amount="2"),
            _flow("two", currency="QUOTE", amount="-1"),
        ],
    )
    conversion_pass = CashflowConversionPass(
        store,
        _config(),
        adapters={"venue": adapter},
    )

    results = conversion_pass.run(today=date(2023, 1, 3))

    assert results[0].converted == 2
    assert results[0].rates_fetched == 1
    assert adapter.calls == [{price_date}]
    assert len(store.tables["conversion_rates"].read()) == 1
    rows = store.tables["cashflow_conversions"].read()
    assert [row["converted_amount"] for row in rows] == ["5", "-2.5"]
    assert {row["conversion_spec"] for row in rows} == {"USDT|previous_day.close"}

    second = conversion_pass.run(today=date(2023, 1, 3))
    assert second[0].converted == 0
    assert adapter.calls == [{price_date}]

    store.upsert("cashflows", [_flow("one", currency="QUOTE", amount="4")])
    refreshed = conversion_pass.run(today=date(2023, 1, 3))
    assert refreshed[0].converted == 1
    by_id = {
        row["cashflow_record_id"]: row
        for row in store.tables["cashflow_conversions"].read()
    }
    assert by_id["one"]["converted_amount"] == "10"


def test_current_day_open_is_available_during_collection(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    price_date = date(2023, 1, 2)
    adapter = StubDailyRateAdapter({price_date: (Decimal("2"), Decimal("9"))})
    store.upsert("cashflows", [_flow("one", currency="QUOTE", amount="3")])

    result = CashflowConversionPass(
        store,
        _config(method="current_day.open"),
        adapters={"venue": adapter},
    ).run(today=price_date)

    assert not result[0].error
    assert store.tables["cashflow_conversions"].read()[0]["converted_amount"] == "6"


def test_incomplete_current_day_close_fails_only_conversion_pass(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    price_date = date(2023, 1, 2)
    adapter = StubDailyRateAdapter({price_date: (Decimal("2"), Decimal("9"))})
    store.upsert("cashflows", [_flow("one", currency="QUOTE", amount="3")])

    result = CashflowConversionPass(
        store,
        _config(method="current_day.close"),
        adapters={"venue": adapter},
    ).run(today=price_date)

    assert "not complete" in result[0].error
    assert store.tables["cashflow_conversions"].read() == []


def test_fixed_fallback_materializes_daily_rate_and_supports_current_close(
    tmp_path: Path,
) -> None:
    store = DataStore(tmp_path)
    price_date = date(2023, 1, 2)
    adapter = StubDailyRateAdapter({})
    store.upsert("cashflows", [_flow("one", currency="WRAPPED", amount="3")])

    result = CashflowConversionPass(
        store,
        _config(
            method="current_day.close",
            fixed_rates={"WRAPPED": Decimal("1")},
        ),
        adapters={"venue": adapter},
    ).run(today=price_date)

    assert not result[0].error
    assert adapter.calls == []
    conversion = store.tables["cashflow_conversions"].read()[0]
    assert conversion["converted_amount"] == "3"
    assert conversion["conversion_spec"].endswith("fixed:WRAPPED=1")
    rate = store.tables["conversion_rates"].read()[0]
    assert rate["open"] == rate["close"] == "1"
    assert rate["source"] == "config:fixed_rate"


def test_configuration_change_requires_explicit_recalculation(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    first_date = date(2023, 1, 1)
    second_date = date(2023, 1, 2)
    adapter = StubDailyRateAdapter(
        {
            first_date: (Decimal("2"), Decimal("2.5")),
            second_date: (Decimal("3"), Decimal("3.5")),
        }
    )
    store.upsert("cashflows", [_flow("one", currency="QUOTE", amount="2")])
    CashflowConversionPass(
        store, _config(), adapters={"venue": adapter}
    ).run(today=date(2023, 1, 3))
    changed = CashflowConversionPass(
        store,
        _config(method="current_day.open"),
        adapters={"venue": adapter},
    )

    automatic = changed.run(today=date(2023, 1, 3))
    assert "configuration changed" in automatic[0].error
    with pytest.raises(ConversionError, match="recalculate-conversions"):
        StoredCashflowConversion(
            store,
            target_currency="USDT", method="current_day.open"
        ).prepare(store.tables["cashflows"].read())

    manual = changed.run(recalculate=True, today=date(2023, 1, 3))
    assert not manual[0].error
    assert store.tables["cashflow_conversions"].read()[0]["converted_amount"] == "6"


@pytest.mark.parametrize(
    ("adapter_type", "payload", "expected_path"),
    [
        (
            BinanceDailyRateAdapter,
            [[1672617600000, "1.9", "3", "1", "2.1", "10", 1672703999999]],
            "/api/v3/klines",
        ),
        (
            GateDailyRateAdapter,
            [["1672617600", "10", "2.1", "3", "1", "1.9"]],
            "/api/v4/spot/candlesticks",
        ),
        (
            MexcDailyRateAdapter,
            [[1672617600000, "1.9", "3", "1", "2.1", "10", 1672704000000]],
            "/api/v3/klines",
        ),
    ],
)
def test_venue_rate_adapters_parse_public_daily_candles(
    adapter_type: type[DailyRateAdapter], payload: Any, expected_path: str
) -> None:
    http = FakeHttp(payload)
    adapter = adapter_type(http)  # type: ignore[arg-type]

    rates = adapter.fetch(
        base_currency="BASE",
        quote_currency="USDT",
        dates={date(2023, 1, 2)},
    )

    assert len(rates) == 1
    assert rates[0].open == Decimal("1.9")
    assert rates[0].close == Decimal("2.1")
    assert http.calls[0][0] == expected_path
