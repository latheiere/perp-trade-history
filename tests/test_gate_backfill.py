from pathlib import Path
from typing import Any

from perp_trade_history.adapters.gate import (
    DAY_MS,
    GATE_ACCOUNT_BOOK_WINDOW_MS,
    GateAdapter,
    normalize_gate_account_book,
)
from perp_trade_history.config import load_config
from perp_trade_history.storage import DataStore


class RecordingHttp:
    def __init__(self) -> None:
        self.requests: list[list[tuple[str, str]]] = []

    def get_json(
        self,
        path: str,
        *,
        params: list[tuple[str, str]],
        headers: dict[str, str],
    ) -> Any:
        self.requests.append(params)
        return []


def _adapter(tmp_path: Path, http: RecordingHttp) -> GateAdapter:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
schema_version = 1
[storage]
data_dir = "history"
[collection]
initial_start = "2020-01-01T00:00:00Z"
page_limit = 100
max_pages_per_query = 100
[venues.gate]
enabled = true
account_id = "primary"
settlements = ["usdt"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path, require_credentials=False)
    return GateAdapter(config, config.venues["gate"], DataStore(config.data_dir), http=http)


def test_account_book_backfill_uses_venue_request_windows(tmp_path: Path) -> None:
    http = RecordingHttp()
    adapter = _adapter(tmp_path, http)
    end_ms = 95 * DAY_MS

    batch = adapter._collect_windowed(
        source="usdt_account_book",
        path="/api/v4/futures/usdt/account_book",
        time_field="time",
        start_ms=0,
        end_ms=end_ms,
        collected_at="2026-01-01T00:00:00.000Z",
        normalizer=normalize_gate_account_book,
        settlement="usdt",
        window_ms=GATE_ACCOUNT_BOOK_WINDOW_MS,
    )

    assert not batch.error
    assert len(http.requests) == 4
    for params in http.requests:
        values = dict(params)
        assert int(values["to"]) - int(values["from"]) <= 30 * 24 * 60 * 60
