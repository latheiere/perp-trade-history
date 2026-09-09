from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from perp_trade_history.config import AppConfig, VenueSettings
from perp_trade_history.storage import DataStore


@dataclass(slots=True)
class NormalizedRecord:
    """One source record and the normalized rows derived from it."""

    source_id: str
    table_rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)


@dataclass(slots=True)
class SourceBatch:
    venue: str
    source: str
    requested_start_ms: int
    covered_through_ms: int
    backfill_complete: bool
    raw_records: list[dict[str, Any]] = field(default_factory=list)
    table_rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    error_category: str = ""
    retryable: bool = False
    skipped: bool = False
    skip_reason: str = ""
    terminal_exclusions: list[dict[str, Any]] = field(default_factory=list)
    coverage_dataset: str = ""
    coverage_status: str = ""
    coverage_scope: str = "account"
    coverage_limitation: str = ""
    acquisition: str = "rest"

    def add_row(self, table: str, row: dict[str, str]) -> None:
        self.table_rows.setdefault(table, []).append(row)

    @property
    def record_count(self) -> int:
        return len(self.raw_records)

    @property
    def oldest_ms(self) -> int:
        timestamps = self._timestamps()
        return min(timestamps) if timestamps else 0

    @property
    def newest_ms(self) -> int:
        timestamps = self._timestamps()
        return max(timestamps) if timestamps else 0

    def _timestamps(self) -> list[int]:
        candidates = (
            "event_time_ms",
            "created_at_ms",
            "updated_at_ms",
            "opened_at_ms",
            "closed_at_ms",
            "observed_at_ms",
        )
        values: list[int] = []
        for rows in self.table_rows.values():
            for row in rows:
                for candidate in candidates:
                    value = row.get(candidate)
                    if value:
                        values.append(int(value))
                        break
        return values


class VenueAdapter(ABC):
    name: str

    def __init__(self, config: AppConfig, venue: VenueSettings, store: DataStore):
        self.config = config
        self.venue = venue
        self.store = store

    @abstractmethod
    def collect(self, *, end_ms: int, full: bool) -> list[SourceBatch]:
        """Collect independent source batches without mutating durable storage."""

    def finalize_cashflows(self) -> None:
        """Resolve overlapping sources after all venue batches have been persisted."""
        return None

    @staticmethod
    def position_lifetime_ids(
        store: DataStore, executions: list[dict[str, str]]
    ) -> dict[str, str]:
        """Map execution record IDs to native position lifetimes when available."""
        return {}

    def start_for(
        self,
        source: str,
        *,
        full: bool,
        retention_start_ms: int | None = None,
    ) -> int:
        return self.store.state.start_for(
            venue=self.name,
            source=source,
            initial_start_ms=self.config.collection.initial_start_ms,
            overlap_ms=self.config.collection.overlap_ms,
            full=full,
            retention_start_ms=retention_start_ms,
        )

    def failure_batch(
        self,
        *,
        source: str,
        start_ms: int,
        end_ms: int,
        error: Exception,
    ) -> SourceBatch:
        return SourceBatch(
            venue=self.name,
            source=source,
            requested_start_ms=start_ms,
            covered_through_ms=end_ms,
            backfill_complete=False,
            error=str(error),
            error_category=getattr(error, "category", "collection"),
            retryable=bool(getattr(error, "retryable", False)),
        )

    def circuit_skip_batch(
        self, *, source: str, start_ms: int, end_ms: int
    ) -> SourceBatch | None:
        if not getattr(getattr(self, "http", None), "circuit_open", False):
            return None
        return SourceBatch(
            venue=self.name,
            source=source,
            requested_start_ms=start_ms,
            covered_through_ms=end_ms,
            backfill_complete=False,
            skipped=True,
            skip_reason="venue_transport_circuit_open",
            error_category="transport_circuit_open",
            retryable=True,
        )
