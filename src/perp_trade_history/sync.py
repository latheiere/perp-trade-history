from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from perp_trade_history.adapters.base import SourceBatch, VenueAdapter
from perp_trade_history.adapters.binance import BinanceAdapter
from perp_trade_history.adapters.gate import GateAdapter
from perp_trade_history.adapters.mexc import MexcAdapter
from perp_trade_history.config import AppConfig
from perp_trade_history.conversion import CashflowConversionPass
from perp_trade_history.models import row_for, stable_id, timestamp_fields, utc_now_iso
from perp_trade_history.storage import DataStore

LOGGER = logging.getLogger(__name__)

ADAPTERS: dict[str, type[VenueAdapter]] = {
    "binance": BinanceAdapter,
    "gate": GateAdapter,
    "mexc": MexcAdapter,
}


def run_conversion_pass(
    config: AppConfig,
    store: DataStore,
    *,
    venues: set[str] | None = None,
    recalculate: bool = False,
) -> list[SourceResult]:
    """Run the shared conversion pass used by automatic and manual workflows."""
    sources: list[SourceResult] = []
    for conversion in CashflowConversionPass(store, config).run(
        venues=venues, recalculate=recalculate
    ):
        source = SourceResult(
            venue=conversion.venue,
            source="cashflow_conversion",
            source_records=conversion.converted,
            error=conversion.error,
            error_category="conversion" if conversion.error else "",
            retryable=bool(conversion.error)
            and "configuration changed" not in conversion.error,
        )
        source.tables.update(conversion.tables)
        sources.append(source)
        if conversion.error:
            LOGGER.error(
                "Cashflow conversion failed: venue=%s error=%s",
                conversion.venue,
                conversion.error,
            )
    return sources


@dataclass(slots=True)
class SourceResult:
    venue: str
    source: str
    source_records: int
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    raw: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_category: str = ""
    retryable: bool = False
    skipped: bool = False
    skip_reason: str = ""
    terminal_exclusions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SyncResult:
    data_dir: str
    sources: list[SourceResult] = field(default_factory=list)

    @property
    def errors(self) -> list[SourceResult]:
        return [source for source in self.sources if source.error]

    @property
    def retryable_errors(self) -> list[SourceResult]:
        return [source for source in self.errors if source.retryable]

    @property
    def error_venues(self) -> set[str]:
        return {source.venue for source in self.errors}

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "ok": not self.errors,
            "sources": [source.to_dict() for source in self.sources],
        }


class SyncEngine:
    def __init__(self, config: AppConfig, store: DataStore | None = None):
        self.config = config
        self.store = store or DataStore(config.data_dir)

    def collect(
        self, *, end_ms: int, full: bool = False, venues: set[str] | None = None
    ) -> SyncResult:
        started = time.monotonic()
        selected = [
            venue for venue in self.config.enabled_venues if venues is None or venue.name in venues
        ]
        if venues:
            enabled_names = {venue.name for venue in self.config.enabled_venues}
            unavailable = venues - enabled_names
            if unavailable:
                raise ValueError(f"venues are not enabled: {sorted(unavailable)}")

        LOGGER.info(
            "Collection started: venues=%d full_history=%s end_ms=%d",
            len(selected),
            full,
            end_ms,
        )
        result = SyncResult(data_dir=str(self.store.root))
        for venue in selected:
            adapter = ADAPTERS[venue.name](self.config, venue, self.store)
            venue_started = time.monotonic()
            source_start = len(result.sources)
            LOGGER.info("Venue collection started: venue=%s", venue.name)
            for batch in adapter.collect(end_ms=end_ms, full=full):
                result.sources.append(self.persist_batch(batch))
            venue_sources = result.sources[source_start:]
            LOGGER.info(
                "Venue collection completed: venue=%s sources=%d failed=%d "
                "warnings=%d records=%d duration_seconds=%.1f",
                venue.name,
                len(venue_sources),
                sum(bool(source.error) for source in venue_sources),
                sum(len(source.warnings) for source in venue_sources),
                sum(source.source_records for source in venue_sources),
                time.monotonic() - venue_started,
            )
        result.sources.extend(
            run_conversion_pass(
                self.config,
                self.store,
                venues={venue.name for venue in selected},
            )
        )
        LOGGER.info(
            "Collection completed: sources=%d failed=%d warnings=%d records=%d "
            "duration_seconds=%.1f",
            len(result.sources),
            len(result.errors),
            sum(len(source.warnings) for source in result.sources),
            sum(source.source_records for source in result.sources),
            time.monotonic() - started,
        )
        return result

    def persist_batch(self, batch: SourceBatch) -> SourceResult:
        source_result = SourceResult(
            venue=batch.venue,
            source=batch.source,
            source_records=batch.record_count,
            warnings=list(batch.warnings),
            error=batch.error,
            error_category=batch.error_category,
            retryable=batch.retryable,
            skipped=batch.skipped,
            skip_reason=batch.skip_reason,
            terminal_exclusions=list(batch.terminal_exclusions),
        )
        for warning in batch.warnings:
            LOGGER.warning(
                "Source collection warning: venue=%s source=%s warning=%s",
                batch.venue,
                batch.source,
                warning,
            )
        if batch.error:
            source_result.coverage = self._persist_coverage(batch, "failed")
            self.store.state.record_failure(
                venue=batch.venue,
                source=batch.source,
                error=batch.error,
                category=batch.error_category or "collection",
                retryable=batch.retryable,
            )
            LOGGER.error(
                "Source collection failed: venue=%s source=%s category=%s "
                "retryable=%s error=%s",
                batch.venue,
                batch.source,
                batch.error_category or "collection",
                batch.retryable,
                batch.error,
            )
            return source_result

        if batch.skipped:
            LOGGER.info(
                "Source collection skipped: venue=%s source=%s reason=%s",
                batch.venue,
                batch.source,
                batch.skip_reason,
            )
            return source_result

        raw_stats = self.store.raw.upsert(batch.venue, batch.source, batch.raw_records)
        source_result.raw = raw_stats.to_dict()
        for table, rows in sorted(batch.table_rows.items()):
            stats = self.store.upsert(table, rows)
            source_result.tables[table] = stats.to_dict()
        source_result.coverage = self._persist_coverage(batch, batch.coverage_status)
        self.store.state.record_success(
            venue=batch.venue,
            source=batch.source,
            requested_start_ms=batch.requested_start_ms,
            covered_through_ms=batch.covered_through_ms,
            oldest_ms=batch.oldest_ms,
            newest_ms=batch.newest_ms,
            records=batch.record_count,
            backfill_complete=batch.backfill_complete,
        )
        LOGGER.debug(
            "Source collection completed: venue=%s source=%s records=%d tables=%d",
            batch.venue,
            batch.source,
            batch.record_count,
            len(source_result.tables),
        )
        return source_result

    def _persist_coverage(self, batch: SourceBatch, status: str) -> dict[str, Any]:
        if not batch.coverage_dataset or not status:
            return {}
        account_id = self.config.venues[batch.venue].account_id
        if status == "point":
            start_ms = batch.covered_through_ms
            end_ms = batch.covered_through_ms
        else:
            start_ms = batch.requested_start_ms
            end_ms = batch.covered_through_ms
        start_time, start_text = timestamp_fields(start_ms)
        end_time, end_text = timestamp_fields(end_ms)
        record_id = stable_id(
            batch.venue,
            account_id,
            "coverage",
            batch.coverage_dataset,
            batch.source,
            batch.acquisition,
            batch.coverage_scope,
            start_ms,
            end_ms,
            "failure" if status == "failed" else "coverage",
        )
        row = row_for(
            "coverage",
            record_id=record_id,
            venue=batch.venue,
            account_id=account_id,
            market_type="perpetual",
            dataset=batch.coverage_dataset,
            source=batch.source,
            acquisition=batch.acquisition,
            scope=batch.coverage_scope,
            start_time=start_time,
            start_time_ms=start_text,
            end_time=end_time,
            end_time_ms=end_text,
            status=status,
            limitation=batch.coverage_limitation,
            collected_at=utc_now_iso(),
        )
        return self.store.upsert("coverage", [row]).to_dict()
