from pathlib import Path
from typing import Any

import pytest
import requests

from perp_trade_history.adapters import binance, mexc
from perp_trade_history.adapters.binance import BinanceAdapter
from perp_trade_history.adapters.mexc import NINETY_DAYS_MS, MexcAdapter
from perp_trade_history.config import load_config
from perp_trade_history.errors import CircuitOpenError, TransportError
from perp_trade_history.http import ReadOnlyHttp
from perp_trade_history.storage import DataStore


def _config(tmp_path: Path, venue: str):
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "[storage]",
                'data_dir = "history"',
                f"[venues.{venue}]",
                "enabled = true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return load_config(path, require_credentials=False)


class _BinanceClockHttp:
    circuit_open = False

    def __init__(self) -> None:
        self.timestamps: list[int] = []

    def get_json(self, path: str, *, params=None, headers=None) -> Any:
        if path == "/fapi/v1/time":
            return {"serverTime": 1_005_000}
        timestamp = int(dict(params or [])["timestamp"])
        self.timestamps.append(timestamp)
        if len(self.timestamps) == 1:
            from perp_trade_history.errors import ApiError

            raise ApiError("timestamp rejected", response_code=-1021)
        return []


def test_binance_clock_skew_refreshes_offset_and_retries_once(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, "binance")
    http = _BinanceClockHttp()
    monkeypatch.setattr(binance.time, "time", lambda: 1000.0)
    adapter = BinanceAdapter(
        config,
        config.venues["binance"],
        DataStore(config.data_dir),
        http=http,  # type: ignore[arg-type]
    )

    assert adapter._get_signed("/fapi/v1/allOrders", {}) == []
    assert http.timestamps == [1_000_000, 1_005_000]


class _MexcClockHttp:
    circuit_open = False

    def __init__(self) -> None:
        self.timestamps: list[int] = []

    def get_json(self, path: str, *, params=None, headers=None) -> Any:
        if path == "/api/v1/contract/ping":
            return {"data": 1_005_000}
        self.timestamps.append(int(headers["Request-Time"]))
        if len(self.timestamps) == 1:
            return {"success": False, "code": 513, "message": "timestamp rejected"}
        return {"success": True, "code": 0, "data": []}


def test_mexc_clock_skew_refreshes_offset_and_retries_once(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, "mexc")
    http = _MexcClockHttp()
    monkeypatch.setattr(mexc.time, "time", lambda: 1000.0)
    adapter = MexcAdapter(
        config,
        config.venues["mexc"],
        DataStore(config.data_dir),
        http=http,  # type: ignore[arg-type]
    )

    payload = adapter._get("/api/v1/private/order/list/history_orders", {}, "orders")

    assert payload["data"] == []
    assert http.timestamps == [1_000_000, 1_005_000]


def test_mexc_window_collection_never_exceeds_reachable_history_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, "mexc")
    adapter = MexcAdapter(
        config,
        config.venues["mexc"],
        DataStore(config.data_dir),
    )
    requested: list[tuple[int, int]] = []

    def paged(path: str, params: dict[str, Any], source: str):
        requested.append((params["start_time"], params["end_time"]))
        return []

    monkeypatch.setattr(adapter, "_paged", paged)
    end_ms = 2_000_000_000_000
    adapter._collect_window_source(
        source="history_orders",
        path="/history",
        start_ms=end_ms - 2 * NINETY_DAYS_MS,
        end_ms=end_ms,
        collected_at="2026-01-01T00:00:00.000Z",
        normalizer=lambda **kwargs: None,  # type: ignore[arg-type]
    )

    assert requested == [(end_ms - NINETY_DAYS_MS, end_ms)]
    assert all(stop - start <= NINETY_DAYS_MS for start, stop in requested)


class _OfflineSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        raise requests.ConnectionError("network unavailable")


def test_transport_outage_opens_circuit_and_skips_following_requests() -> None:
    session = _OfflineSession()
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=session,  # type: ignore[arg-type]
        circuit_cooldown_seconds=60,
    )

    with pytest.raises(TransportError):
        client.get_json("/first")
    with pytest.raises(CircuitOpenError):
        client.get_json("/second")

    assert session.calls == 1
