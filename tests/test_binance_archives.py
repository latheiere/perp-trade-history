import gzip
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from perp_trade_history import binance_archives
from perp_trade_history.adapters.binance import (
    BinanceAdapter,
    normalize_binance_trade,
)
from perp_trade_history.binance_archives import (
    BinanceArchiveCoordinator,
    parse_binance_archive,
)
from perp_trade_history.config import AppConfig, load_config
from perp_trade_history.errors import ApiError, CollectionError
from perp_trade_history.storage import DataStore

TRADE_CSV = (
    b"symbol,id,orderId,side,positionSide,price,qty,quoteQty,commission,"
    b"commissionAsset,marginAsset,realizedPnl,maker,time\n"
    b"ASSETQUOTE,7,9,SELL,LONG,10,2,20,0.2,QUOTE,QUOTE,3.5,false,1700000000000\n"
)
EMPTY_TRADE_CSV = TRADE_CSV.splitlines(keepends=True)[0]
FEE_WITH_CURRENCY_TRADE_CSV = (
    b"Uid,Time,Symbol,Side,Price,Quantity,Amount,Fee,Realized "
    b"Profit,Buyer,Maker,Trade ID,Order ID\n"
    b"167857164,2023-01-01 19:53:13,ETHUSDT,SELL,1200.62,2,"
    b"2401.24,0.00352911 BNB,10.24,false,false,2593877352,"
    b"8389765568816714211\n"
)
LEGACY_TIME_HEADERS_TRADE_CSV = (
    b"Uid,Time(UTC),Symbol,Side,Position Side,Price,Quantity,Amount,Fee,"
    b"Realized Profit,Buyer,Maker,Trade Id,Order Id\n"
    b"1,2023-01-01 00:00:00,ASSETQUOTE,BUY,Long,1.5,1,1.5,0.00053 BNB,2.34,true,false,99,88\n"
)
LEGACY_TIME_HEADERS_ORDER_CSV = (
    b"Uid,Time(UTC),Symbol,Type,Side,Position Side,Price,Average Price,Amount,"
    b"Executed Amount,Executed Quote Amount,Stop Price,Status,Client Order Id,"
    b"Time In Force,Activate Price,Price Rate,Update Time,Order No\n"
    b"1,2023-01-01 00:00:00,ASSETQUOTE,LIMIT,BUY,Long,10,11,1,1,"
    b"1,0,NEW,client-order,GTC,0,0.0,2023-01-01 00:00:10,77\n"
)
LEGACY_TIME_HEADERS_INCOME_CSV = (
    b"Date(UTC),type,Amount,Asset,Symbol,Transaction ID\n"
    b"2023-01-01 00:00:00,FUNDING_FEE,-0.8,USDT,ASSETQUOTE,123\n"
)


def _config(tmp_path: Path) -> AppConfig:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
schema_version = 1
[storage]
data_dir = "history"
[collection]
initial_start = "2020-01-01T00:00:00Z"
[venues.binance]
enabled = true
account_id = "primary"
symbols = []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return load_config(config_path, require_credentials=False)


def _zip_csv(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("history.csv", payload)
    return buffer.getvalue()


class FailingHistoryHttp:
    @staticmethod
    def get_json(path: str, *, params, headers):
        raise ApiError(f"GET {path} returned HTTP 400: contract is unavailable")


class TerminalContractHistoryHttp:
    circuit_open = False

    def __init__(self, response_code: int = -4141) -> None:
        self.contracts: list[str] = []
        self.response_code = response_code

    def get_json(self, path: str, *, params=None, headers=None):
        contract = str(dict(params or []).get("symbol") or "")
        self.contracts.append(contract)
        if contract == "CLOSED_CONTRACT":
            raise ApiError(
                f"GET {path} returned HTTP 400: contract is closed",
                status_code=400,
                response_code=self.response_code,
            )
        return []


def test_symbol_scoped_failure_identifies_contract_endpoint_and_utc_window(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapter = BinanceAdapter(
        config,
        config.venues["binance"],
        DataStore(config.data_dir),
        http=FailingHistoryHttp(),  # type: ignore[arg-type]
    )

    with pytest.raises(CollectionError) as caught:
        adapter._collect_symbol_windows(
            source="orders",
            path="/history/orders",
            symbols=["HISTORICALCONTRACT"],
            start_ms=1_700_000_000_000,
            end_ms=1_700_000_001_000,
            collected_at="2026-01-01T00:00:00.000Z",
            normalizer=normalize_binance_trade,
        )

    message = str(caught.value)
    assert "venue=binance source=orders endpoint=/history/orders" in message
    assert "contract='HISTORICALCONTRACT'" in message
    assert "window_start=2023-11-14T22:13:20.000Z" in message
    assert "window_end=2023-11-14T22:13:21.000Z" in message
    assert "error=GET /history/orders returned HTTP 400" in message


@pytest.mark.parametrize(
    ("path", "response_code"),
    [
        ("/fapi/v1/allOrders", -4141),
        ("/fapi/v1/allAlgoOrders", -1121),
    ],
)
def test_endpoint_terminal_contract_is_recorded_without_aborting_other_contracts(
    tmp_path: Path, path: str, response_code: int
) -> None:
    config = _config(tmp_path)
    http = TerminalContractHistoryHttp(response_code)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(
        config,
        config.venues["binance"],
        store,
        http=http,  # type: ignore[arg-type]
    )

    batch = adapter._collect_symbol_windows(
        source="all_orders",
        path=path,
        symbols=["CLOSED_CONTRACT", "TRADING_CONTRACT"],
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_001_000,
        collected_at="2026-01-01T00:00:00.000Z",
        normalizer=normalize_binance_trade,
    )

    assert batch.error == ""
    assert http.contracts == ["CLOSED_CONTRACT", "TRADING_CONTRACT"]
    assert len(batch.terminal_exclusions) == 1
    exclusion = batch.terminal_exclusions[0]
    assert exclusion["contract"] == "CLOSED_CONTRACT"
    assert exclusion["endpoint"] == path
    assert exclusion["response_code"] == response_code


class ContractCatalogHttp:
    circuit_open = False

    @staticmethod
    def get_json(path: str, *, params=None, headers=None):
        assert path == "/fapi/v1/exchangeInfo"
        return {
            "symbols": [
                {
                    "symbol": "TRADING_CONTRACT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
                {
                    "symbol": "SETTLING_CONTRACT",
                    "contractType": "PERPETUAL",
                    "status": "SETTLING",
                },
                {
                    "symbol": "DELIVERY_CONTRACT",
                    "contractType": "CURRENT_QUARTER",
                    "status": "TRADING",
                },
            ]
        }


def test_public_discovery_adds_only_trading_perpetual_contracts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    adapter = BinanceAdapter(
        config,
        config.venues["binance"],
        DataStore(config.data_dir),
        http=ContractCatalogHttp(),  # type: ignore[arg-type]
    )

    discovered, perpetual_count = adapter._discover_symbols()

    assert discovered == {"TRADING_CONTRACT"}
    assert perpetual_count == 2


class FakeArchiveAdapter:
    def __init__(self, real: BinanceAdapter, payload: bytes):
        self.real = real
        self.payload = payload
        self.requests: list[tuple[str, int, int]] = []
        self.polls: list[tuple[str, str]] = []

    def request_archive(self, kind: str, *, start_ms: int, end_ms: int) -> dict[str, Any]:
        self.requests.append((kind, start_ms, end_ms))
        return {"downloadId": f"download-{len(self.requests)}"}

    def poll_archive(self, kind: str, download_id: str) -> dict[str, Any]:
        self.polls.append((kind, download_id))
        return {
            "downloadId": download_id,
            "status": "completed",
            "url": "https://download.example/archive",
            "isExpired": "false",
        }

    def download_archive(self, url: str) -> bytes:
        assert url.startswith("https://")
        return self.payload

    def normalize_archive_rows(
        self, kind: str, rows: list[dict[str, Any]], *, collected_at: str
    ):
        return self.real.normalize_archive_rows(kind, rows, collected_at=collected_at)


class ExpiredArchiveAdapter(FakeArchiveAdapter):
    def poll_archive(self, kind: str, download_id: str) -> dict[str, Any]:
        self.polls.append((kind, download_id))
        return {
            "downloadId": download_id,
            "status": "completed",
            "url": "https://download.example/archive",
            "isExpired": "null",
            "expirationTimestamp": 1,
        }


class FailingRequestArchiveAdapter(FakeArchiveAdapter):
    def request_archive(
        self, kind: str, *, start_ms: int, end_ms: int
    ) -> dict[str, Any]:
        self.requests.append((kind, start_ms, end_ms))
        if len(self.requests) == 1:
            raise ConnectionError("request result is unknown")
        return {"downloadId": f"download-{len(self.requests)}"}


def test_archive_parser_accepts_plain_zip_and_gzip_csv() -> None:
    expected = parse_binance_archive(TRADE_CSV)
    assert expected[0]["id"] == "7"
    assert parse_binance_archive(_zip_csv(TRADE_CSV)) == expected
    assert parse_binance_archive(gzip.compress(TRADE_CSV)) == expected


def test_archive_parser_accepts_legacy_binance_headers_with_time_utc_columns() -> None:
    trade_rows = parse_binance_archive(LEGACY_TIME_HEADERS_TRADE_CSV, kind="trades")
    assert trade_rows[0]["Trade Id"] == "99"
    assert trade_rows[0]["Time(UTC)"] == "2023-01-01 00:00:00"

    order_rows = parse_binance_archive(LEGACY_TIME_HEADERS_ORDER_CSV, kind="orders")
    assert order_rows[0]["Order No"] == "77"
    assert order_rows[0]["Time(UTC)"] == "2023-01-01 00:00:00"

    income_rows = parse_binance_archive(LEGACY_TIME_HEADERS_INCOME_CSV, kind="income")
    assert income_rows[0]["type"] == "FUNDING_FEE"
    assert income_rows[0]["Date(UTC)"] == "2023-01-01 00:00:00"


def test_archive_request_attempt_is_retained_before_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = FailingRequestArchiveAdapter(real, TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=fake)  # type: ignore[arg-type]
    start_ms = 1_700_000_000_000
    end_ms = start_ms + 10_000

    failed = coordinator.request_range(
        kind="trades", start_ms=start_ms, end_ms=end_ms
    )
    retried = coordinator.request_range(
        kind="trades", start_ms=start_ms, end_ms=end_ms
    )

    jobs = coordinator.status()["jobs"]
    assert failed.status == "error"
    assert retried.status == "requested"
    assert sorted(job["status"] for job in jobs) == ["request-error", "requested"]
    assert all(job["requested_at"] for job in jobs)


def test_archive_parse_failure_retries_retained_bytes_without_redownload(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = FakeArchiveAdapter(real, b"unknown,column\n")
    coordinator = BinanceArchiveCoordinator(
        config, store, adapter=fake  # type: ignore[arg-type]
    )
    start_ms = 1_700_000_000_000
    end_ms = start_ms + 10_000
    coordinator.request_range(kind="trades", start_ms=start_ms, end_ms=end_ms)

    first = coordinator.poll()
    job = coordinator.status()["jobs"][0]

    assert first[0].status == "error"
    assert job["status"] == "parse-error"
    assert (store.root / job["archive_file"]).read_bytes() == b"unknown,column\n"
    assert len(fake.polls) == 1

    recovered_rows = parse_binance_archive(TRADE_CSV, kind="trades")
    monkeypatch.setattr(
        binance_archives,
        "parse_binance_archive",
        lambda payload, *, kind=None: recovered_rows,
    )
    second = coordinator.poll()

    assert second[0].status == "imported"
    assert coordinator.status()["jobs"][0]["status"] == "imported"
    assert len(fake.polls) == 1


def test_archive_import_rejects_unrecognized_empty_schema(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    source_path = tmp_path / "invalid.csv"
    source_path.write_bytes(b"unknown,column\n")
    coordinator = BinanceArchiveCoordinator(config, store, adapter=adapter)
    with pytest.raises(ValueError, match="required columns"):
        coordinator.import_file(kind="trades", path=source_path)


def test_complete_archive_range_rejects_records_outside_asserted_bounds(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    source_path = tmp_path / "provided.csv"
    source_path.write_bytes(TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=adapter)

    with pytest.raises(ValueError, match="outside the asserted"):
        coordinator.import_file(
            kind="trades",
            path=source_path,
            start_ms=1_600_000_000_000,
            end_ms=1_600_086_399_999,
            complete_range=True,
        )


def test_archive_trade_fee_currency_and_settlement_currency_are_decoupled(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    rows = adapter.normalize_archive_rows(
        "trades",
        parse_binance_archive(FEE_WITH_CURRENCY_TRADE_CSV),
        collected_at="2026-01-01T00:00:00.000Z",
    ).table_rows
    execution = rows["executions"][0]
    cashflows = rows["cashflows"]
    assert execution["settlement_currency"] == "USDT"
    assert execution["fee_currency"] == "BNB"
    assert execution["notional"] == "2401.24"
    realized = next(row for row in cashflows if row["event_type"] == "realized_pnl")
    commission = next(row for row in cashflows if row["event_type"] == "commission")
    assert realized["currency"] == "USDT"
    assert realized["amount"] == "10.24"
    assert commission["currency"] == "BNB"
    assert commission["amount"] == "-0.00352911"


def test_all_supported_archive_datasets_use_the_rest_normalization_path(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    collected_at = "2026-01-01T00:00:00.000Z"

    orders = adapter.normalize_archive_rows(
        "orders",
        [
            {
                "Symbol": "ASSETQUOTE",
                "Order ID": "21",
                "Client Order ID": "client-21",
                "Original Quantity": "3",
                "Executed Quantity": "1",
                "Price": "12",
                "Side": "BUY",
                "Position Side": "LONG",
                "Status": "PARTIALLY_FILLED",
                "Type": "LIMIT",
                "Time": "1700000000000",
                "Update Time": "1700000001000",
                "Reduce Only": "false",
            }
        ],
        collected_at=collected_at,
    )
    order = orders.table_rows["orders"][0]
    assert order["order_id"] == "21"
    assert order["reduce_only"] == "false"

    income = adapter.normalize_archive_rows(
        "income",
        [
            {
                "Symbol": "ASSETQUOTE",
                "Income Type": "FUNDING_FEE",
                "Amount": "-0.4",
                "Currency": "QUOTE",
                "Transaction ID": "22",
                "Time": "1700000002000",
            }
        ],
        collected_at=collected_at,
    )
    cashflow = income.table_rows["cashflows"][0]
    assert cashflow["event_type"] == "funding"
    assert cashflow["amount"] == "-0.4"


def test_archive_with_legacy_headers_normalizes_via_archive_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=adapter)
    trade_path = tmp_path / "trade_legacy.csv"
    order_path = tmp_path / "order_legacy.csv"
    income_path = tmp_path / "income_legacy.csv"
    trade_path.write_bytes(LEGACY_TIME_HEADERS_TRADE_CSV)
    order_path.write_bytes(LEGACY_TIME_HEADERS_ORDER_CSV)
    income_path.write_bytes(LEGACY_TIME_HEADERS_INCOME_CSV)

    import_trade = coordinator.import_file(
        kind="trades",
        path=trade_path,
        start_ms=1_670_000_000_000,
        end_ms=1_680_000_000_000,
        complete_range=True,
    )
    import_order = coordinator.import_file(
        kind="orders",
        path=order_path,
        start_ms=1_670_000_000_000,
        end_ms=1_680_000_000_000,
        complete_range=True,
    )
    import_income = coordinator.import_file(
        kind="income",
        path=income_path,
        start_ms=1_670_000_000_000,
        end_ms=1_680_000_000_000,
        complete_range=True,
    )

    assert import_trade.records == 1
    assert import_order.records == 1
    assert import_income.records == 1


def test_async_export_lifecycle_retains_source_and_imports_canonical_rows(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = FakeArchiveAdapter(real, _zip_csv(TRADE_CSV))
    coordinator = BinanceArchiveCoordinator(config, store, adapter=fake)  # type: ignore[arg-type]

    end_ms = 1_735_689_600_000
    requested = coordinator.request_next(
        end_ms=end_ms, kinds=("trades",), max_requests=1
    )
    assert requested[0].status == "requested"
    assert fake.requests[0][2] < end_ms

    imported = coordinator.poll()
    assert imported[0].status == "imported"
    assert len(store.tables["executions"].read()) == 1
    assert len(store.tables["cashflows"].read()) == 2
    archive_path = store.root / imported[0].archive_file
    source_csv_path = store.root / imported[0].source_csv
    assert archive_path.read_bytes().startswith(b"PK\x03\x04")
    assert source_csv_path.read_text(encoding="utf-8").startswith("symbol,id,")
    state_text = (store.root / "state" / "binance-archives.json").read_text()
    assert "download.example" not in state_text


def test_expired_export_attempt_is_retained_and_can_be_requested_again(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = ExpiredArchiveAdapter(real, TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=fake)  # type: ignore[arg-type]
    end_ms = 1_735_689_600_000

    first = coordinator.request_next(
        end_ms=end_ms, kinds=("trades",), max_requests=1
    )[0]
    expired = coordinator.poll()[0]
    second = coordinator.request_next(
        end_ms=end_ms, kinds=("trades",), max_requests=1
    )[0]

    assert first.status == "requested"
    assert expired.status == "expired"
    assert second.status == "requested"
    jobs = coordinator.status()["jobs"]
    assert [job["status"] for job in jobs] == ["expired", "requested"]


def test_explicit_gap_request_is_imported_once_and_then_skipped(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = FakeArchiveAdapter(real, TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=fake)  # type: ignore[arg-type]
    start_ms = 1_699_000_000_000
    end_ms = 1_701_000_000_000

    requested = coordinator.request_range(
        kind="trades", start_ms=start_ms, end_ms=end_ms
    )
    imported = coordinator.poll()[0]
    repeated = coordinator.request_range(
        kind="trades", start_ms=start_ms, end_ms=end_ms
    )

    assert requested.status == "requested"
    assert imported.status == "imported"
    assert repeated.status == "skipped"
    assert repeated.message == "archive interval is already covered"


def test_rest_and_archive_records_deduplicate_and_repeated_import_is_idempotent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    rest = normalize_binance_trade(
        {
            "symbol": "ASSETQUOTE",
            "id": 7,
            "orderId": 9,
            "side": "SELL",
            "positionSide": "LONG",
            "price": "10",
            "qty": "2",
            "quoteQty": "20",
            "commission": "0.2",
            "commissionAsset": "QUOTE",
            "marginAsset": "QUOTE",
            "realizedPnl": "3.5",
            "maker": False,
            "time": 1_700_000_000_000,
        },
        account_id="primary",
        source="user_trades",
        collected_at="2026-01-01T00:00:00.000Z",
    )
    for table, rows in rest.table_rows.items():
        store.upsert(table, rows)

    source_path = tmp_path / "provided.csv"
    source_path.write_bytes(TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=adapter)
    first = coordinator.import_file(kind="trades", path=source_path)
    second = coordinator.import_file(kind="trades", path=source_path)

    assert first.normalized["tables"]["executions"]["updated"] == 1
    assert second.normalized["tables"]["executions"]["unchanged"] == 1
    assert len(store.tables["executions"].read()) == 1
    assert len(store.tables["cashflows"].read()) == 2


def test_distinct_source_archives_for_one_range_are_both_retained(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    adapter = BinanceAdapter(config, config.venues["binance"], store)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=adapter)
    source_path = tmp_path / "provided.csv"
    replacement = TRADE_CSV.replace(b",7,9,", b",8,10,")
    start_ms = 1_699_000_000_000
    end_ms = 1_701_000_000_000

    source_path.write_bytes(TRADE_CSV)
    first = coordinator.import_file(
        kind="trades",
        path=source_path,
        start_ms=start_ms,
        end_ms=end_ms,
        complete_range=True,
    )
    source_path.write_bytes(replacement)
    second = coordinator.import_file(
        kind="trades",
        path=source_path,
        start_ms=start_ms,
        end_ms=end_ms,
        complete_range=True,
    )

    assert first.archive_file != second.archive_file
    assert (store.root / first.archive_file).read_bytes() == TRADE_CSV
    assert (store.root / second.archive_file).read_bytes() == replacement


def test_overlapping_complete_ranges_merge_and_next_request_targets_remaining_gap(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = DataStore(config.data_dir)
    real = BinanceAdapter(config, config.venues["binance"], store)
    fake = FakeArchiveAdapter(real, TRADE_CSV)
    coordinator = BinanceArchiveCoordinator(config, store, adapter=fake)  # type: ignore[arg-type]
    source_path = tmp_path / "provided.csv"
    source_path.write_bytes(TRADE_CSV)

    end_ms = 1_735_689_600_000
    first_request = coordinator.request_next(
        end_ms=end_ms, kinds=("trades",), max_requests=1
    )[0]
    coordinator.poll()
    overlap_start = first_request.start_ms - 10_000
    source_path.write_bytes(EMPTY_TRADE_CSV)
    coordinator.import_file(
        kind="trades",
        path=source_path,
        start_ms=overlap_start,
        end_ms=first_request.start_ms + 10_000,
        complete_range=True,
    )

    coverage = coordinator.status()["coverage"]["trades"]
    assert coverage["status"] == "incomplete"
    assert coverage["complete_intervals"][-1] == (
        overlap_start,
        first_request.end_ms,
    )
    next_request = coordinator.request_next(
        end_ms=end_ms, kinds=("trades",), max_requests=1
    )[0]
    assert next_request.status == "requested"
    assert next_request.end_ms == overlap_start - 1
