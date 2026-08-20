from __future__ import annotations

import csv
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from perp_trade_history.models import TABLE_FIELDS, canonical_json, stable_id, utc_now_iso


@dataclass(slots=True)
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    total: int = 0

    def add(self, other: UpsertStats) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.total = other.total

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _same_except_collection_time(left: dict[str, Any], right: dict[str, Any]) -> bool:
    volatile = {"collected_at"}
    left_stable = {key: value for key, value in left.items() if key not in volatile}
    right_stable = {key: value for key, value in right.items() if key not in volatile}
    return left_stable == right_stable


class CsvTable:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = path
        self.fieldnames = fieldnames
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def read(self) -> list[dict[str, str]]:
        return list(self.iter_read())

    def iter_read(self) -> Iterator[dict[str, str]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != self.fieldnames:
                raise ValueError(
                    f"unexpected CSV schema in {self.path}: {reader.fieldnames!r}; "
                    f"expected {self.fieldnames!r}"
                )
            for row in reader:
                yield dict(row)

    def upsert(self, rows: list[dict[str, str]]) -> UpsertStats:
        stats = UpsertStats()
        if not rows:
            stats.total = len(self.read())
            return stats
        with _exclusive_lock(self.lock_path):
            existing = self.read()
            by_id = {row["record_id"]: row for row in existing}
            for incoming in rows:
                self._validate_row(incoming)
                record_id = incoming["record_id"]
                previous = by_id.get(record_id)
                if previous is None:
                    by_id[record_id] = incoming
                    stats.inserted += 1
                elif _same_except_collection_time(previous, incoming):
                    stats.unchanged += 1
                else:
                    by_id[record_id] = incoming
                    stats.updated += 1
            merged = sorted(by_id.values(), key=self._sort_key)
            self._write(merged)
            stats.total = len(merged)
        return stats
    def _validate_row(self, row: dict[str, str]) -> None:
        if set(row) != set(self.fieldnames):
            missing = sorted(set(self.fieldnames) - set(row))
            extra = sorted(set(row) - set(self.fieldnames))
            raise ValueError(f"invalid row for {self.path.name}: missing={missing}, extra={extra}")
        if not row.get("record_id"):
            raise ValueError(f"record_id is required for {self.path.name}")

    @staticmethod
    def _sort_key(row: dict[str, str]) -> tuple[int, str]:
        timestamp_fields = (
            "event_time_ms",
            "created_at_ms",
            "opened_at_ms",
            "observed_at_ms",
        )
        timestamp = next((row.get(name, "") for name in timestamp_fields if row.get(name)), "")
        return int(timestamp or 0), row["record_id"]

    def _write(self, rows: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
def raw_record(
    *,
    venue: str,
    account_id: str,
    source: str,
    source_id: str,
    payload: Any,
    collected_at: str,
) -> dict[str, Any]:
    raw_id = stable_id(venue, account_id, "raw", source, source_id)
    return {
        "schema_version": 1,
        "raw_id": raw_id,
        "venue": venue,
        "account_id": account_id,
        "source": source,
        "source_id": source_id,
        "collected_at": collected_at,
        "payload": payload,
    }


class RawArchive:
    _SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_.-]+")

    def __init__(self, root: Path):
        self.root = root

    def upsert(self, venue: str, source: str, records: list[dict[str, Any]]) -> UpsertStats:
        path = self._path(venue, source)
        lock_path = path.with_suffix(f"{path.suffix}.lock")
        stats = UpsertStats()
        if not records:
            stats.total = len(self.read(venue, source))
            return stats
        with _exclusive_lock(lock_path):
            existing = self.read(venue, source)
            by_id = {row["raw_id"]: row for row in existing}
            for incoming in records:
                raw_id = str(incoming.get("raw_id", ""))
                if not raw_id:
                    raise ValueError("raw_id is required")
                previous = by_id.get(raw_id)
                if previous is None:
                    by_id[raw_id] = incoming
                    stats.inserted += 1
                elif _same_except_collection_time(previous, incoming):
                    stats.unchanged += 1
                else:
                    by_id[raw_id] = incoming
                    stats.updated += 1
            merged = [by_id[key] for key in sorted(by_id)]
            content = "".join(canonical_json(row) + "\n" for row in merged)
            _atomic_text(path, content)
            stats.total = len(merged)
        return stats

    def read(self, venue: str, source: str) -> list[dict[str, Any]]:
        path = self._path(venue, source)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL in {path}:{line_number}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"non-object JSONL row in {path}:{line_number}")
                rows.append(row)
        return rows

    def _path(self, venue: str, source: str) -> Path:
        safe_venue = self._SAFE_COMPONENT.sub("-", venue).strip("-")
        safe_source = self._SAFE_COMPONENT.sub("-", source).strip("-")
        if not safe_venue or not safe_source:
            raise ValueError("venue and source must contain a safe path component")
        return self.root / safe_venue / f"{safe_source}.jsonl"


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "updated_at": "", "sources": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported sync state in {self.path}")
        payload.setdefault("sources", {})
        return payload

    def source(self, venue: str, source: str) -> dict[str, Any]:
        return dict(self.load().get("sources", {}).get(self._key(venue, source), {}))

    def start_for(
        self,
        *,
        venue: str,
        source: str,
        initial_start_ms: int,
        overlap_ms: int,
        full: bool,
        retention_start_ms: int | None = None,
    ) -> int:
        lower_bound = max(initial_start_ms, retention_start_ms or 0)
        if full:
            return lower_bound
        state = self.source(venue, source)
        covered_through = int(state.get("covered_through_ms") or 0)
        if not state.get("backfill_complete") or not covered_through:
            return lower_bound
        return max(lower_bound, covered_through - overlap_ms)

    def record_success(
        self,
        *,
        venue: str,
        source: str,
        requested_start_ms: int,
        covered_through_ms: int,
        oldest_ms: int,
        newest_ms: int,
        records: int,
        backfill_complete: bool,
    ) -> None:
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            key = self._key(venue, source)
            previous = payload["sources"].get(key, {})
            previous_oldest = int(previous.get("oldest_ms") or 0)
            previous_newest = int(previous.get("newest_ms") or 0)
            payload["sources"][key] = {
                "venue": venue,
                "source": source,
                "last_attempt_at": utc_now_iso(),
                "last_success_at": utc_now_iso(),
                "requested_start_ms": requested_start_ms,
                "covered_through_ms": max(
                    covered_through_ms, int(previous.get("covered_through_ms") or 0)
                ),
                "oldest_ms": self._minimum_nonzero(previous_oldest, oldest_ms),
                "newest_ms": max(previous_newest, newest_ms),
                "last_record_count": records,
                "backfill_complete": bool(
                    previous.get("backfill_complete") or backfill_complete
                ),
                "last_error": "",
            }
            payload["updated_at"] = utc_now_iso()
            _atomic_text(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def record_failure(
        self,
        *,
        venue: str,
        source: str,
        error: str,
        category: str = "collection",
        retryable: bool = False,
    ) -> None:
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            key = self._key(venue, source)
            previous = dict(payload["sources"].get(key, {}))
            previous.update(
                {
                    "venue": venue,
                    "source": source,
                    "last_attempt_at": utc_now_iso(),
                    "last_error": error,
                    "last_error_category": category,
                    "last_error_retryable": retryable,
                }
            )
            payload["sources"][key] = previous
            payload["updated_at"] = utc_now_iso()
            _atomic_text(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _key(venue: str, source: str) -> str:
        return f"{venue}:{source}"

    @staticmethod
    def _minimum_nonzero(left: int, right: int) -> int:
        values = [value for value in (left, right) if value > 0]
        return min(values) if values else 0


class ContractEligibilityStore:
    """Persist endpoint-specific terminal contract exclusions."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "updated_at": "", "contracts": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported contract eligibility state in {self.path}")
        payload.setdefault("contracts", {})
        return payload

    def blocked(
        self, *, venue: str, endpoint: str, contract: str, now_ms: int
    ) -> dict[str, Any] | None:
        item = self.load()["contracts"].get(self._key(venue, endpoint, contract))
        if not isinstance(item, dict) or item.get("status") != "terminal":
            return None
        if int(item.get("next_check_at_ms") or 0) <= now_ms:
            return None
        return dict(item)

    def record_terminal(
        self,
        *,
        venue: str,
        endpoint: str,
        contract: str,
        reason: str,
        response_code: str | int | None,
        observed_at_ms: int,
        recheck_after_ms: int,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            key = self._key(venue, endpoint, contract)
            previous = dict(payload["contracts"].get(key, {}))
            item = {
                "venue": venue,
                "endpoint": endpoint,
                "contract": contract,
                "status": "terminal",
                "reason": reason,
                "response_code": response_code,
                "first_observed_at": previous.get("first_observed_at") or utc_now_iso(),
                "last_observed_at": utc_now_iso(),
                "next_check_at_ms": observed_at_ms + recheck_after_ms,
                "occurrences": int(previous.get("occurrences") or 0) + 1,
            }
            payload["contracts"][key] = item
            payload["updated_at"] = utc_now_iso()
            _atomic_text(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return dict(item)

    @staticmethod
    def _key(venue: str, endpoint: str, contract: str) -> str:
        return f"{venue}:{endpoint}:{contract}"


class CoverageIndex:
    """In-memory coverage intervals reused across every group in one report."""

    def __init__(self, rows: list[dict[str, str]]):
        intervals: dict[
            tuple[str, str, str], dict[str, list[tuple[int, int]]]
        ] = {}
        scopes: dict[tuple[str, str, str], set[str]] = {}
        for row in rows:
            key = (
                str(row.get("venue") or ""),
                str(row.get("account_id") or ""),
                str(row.get("dataset") or ""),
            )
            scope = str(row.get("scope") or "account")
            scopes.setdefault(key, set()).add(scope)
            if (
                row.get("status") != "complete"
                or not row.get("start_time_ms")
                or not row.get("end_time_ms")
            ):
                continue
            intervals.setdefault(key, {}).setdefault(scope, []).append(
                (int(row["start_time_ms"]), int(row["end_time_ms"]))
            )
        self._scopes = scopes
        self._intervals = {
            key: {
                scope: [tuple(interval) for interval in _merge_coverage_intervals(values)]
                for scope, values in by_scope.items()
            }
            for key, by_scope in intervals.items()
        }

    def assessment(
        self,
        *,
        venue: str,
        account_id: str,
        dataset: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        key = (venue, account_id, dataset)
        scopes = self._scopes.get(key)
        if not scopes:
            return {
                "status": "unknown",
                "dataset": dataset,
                "scopes": {},
                "gaps": [[start_ms, end_ms]],
            }
        assessed_scopes = {"account"} if "account" in scopes else scopes
        scope_results: dict[str, Any] = {}
        all_gaps: list[list[int]] = []
        for scope in sorted(assessed_scopes):
            complete_intervals = self._intervals.get(key, {}).get(scope, [])
            gaps = _coverage_gaps(complete_intervals, start_ms, end_ms)
            scope_results[scope] = {
                "complete_intervals": [list(interval) for interval in complete_intervals],
                "gaps": gaps,
            }
            all_gaps.extend(gaps)
        return {
            "status": "complete" if not all_gaps else "incomplete",
            "dataset": dataset,
            "scopes": scope_results,
            "gaps": all_gaps,
        }


class DataStore:
    def __init__(self, root: Path):
        self.root = root
        self.normalized_root = root / "normalized"
        self.raw = RawArchive(root / "raw")
        self.state = StateStore(root / "state" / "sync-state.json")
        self.contract_eligibility = ContractEligibilityStore(
            root / "state" / "contract-eligibility.json"
        )
        self.tables = {
            table: CsvTable(self.normalized_root / f"{table}.csv", fields)
            for table, fields in TABLE_FIELDS.items()
        }

    def upsert(self, table: str, rows: list[dict[str, str]]) -> UpsertStats:
        return self.tables[table].upsert(rows)

    def existing_symbols(self, venue: str, account_id: str) -> set[str]:
        symbols: set[str] = set()
        for table in ("executions", "orders", "positions", "cashflows"):
            for row in self.tables[table].read():
                if (
                    row.get("venue") == venue
                    and row.get("account_id") == account_id
                    and row.get("symbol")
                ):
                    symbols.add(row["symbol"])
        return symbols

    def historical_symbols(self, venue: str, account_id: str) -> set[str]:
        """Return symbols with account activity, excluding closed point snapshots."""
        symbols: set[str] = set()
        for table in ("executions", "orders", "cashflows"):
            for row in self.tables[table].read():
                if (
                    row.get("venue") == venue
                    and row.get("account_id") == account_id
                    and row.get("symbol")
                ):
                    symbols.add(row["symbol"])
        for row in self.tables["positions"].read():
            if (
                row.get("venue") == venue
                and row.get("account_id") == account_id
                and row.get("symbol")
                and row.get("status") == "open"
            ):
                symbols.add(row["symbol"])
        return symbols

    def coverage_assessment(
        self,
        *,
        venue: str,
        account_id: str,
        dataset: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        return self.coverage_index().assessment(
            venue=venue,
            account_id=account_id,
            dataset=dataset,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def coverage_index(self) -> CoverageIndex:
        return CoverageIndex(self.tables["coverage"].read())

    def summary(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.root),
            "tables": {table: len(csv_table.read()) for table, csv_table in self.tables.items()},
            "sync_state": self.state.load(),
        }


def _merge_coverage_intervals(
    intervals: list[tuple[int, int]],
) -> list[list[int]]:
    merged: list[list[int]] = []
    for start_ms, end_ms in sorted(intervals):
        if start_ms > end_ms:
            continue
        if not merged or start_ms > merged[-1][1] + 1:
            merged.append([start_ms, end_ms])
        else:
            merged[-1][1] = max(merged[-1][1], end_ms)
    return merged


def _coverage_gaps(
    intervals: list[tuple[int, int]], start_ms: int, end_ms: int
) -> list[list[int]]:
    if start_ms > end_ms:
        return []
    gaps: list[list[int]] = []
    cursor = start_ms
    for covered_start, covered_end in _merge_coverage_intervals(intervals):
        if covered_end < start_ms or covered_start > end_ms:
            continue
        if covered_start > cursor:
            gaps.append([cursor, min(covered_start - 1, end_ms)])
        cursor = max(cursor, covered_end + 1)
        if cursor > end_ms:
            break
    if cursor <= end_ms:
        gaps.append([cursor, end_ms])
    return gaps
