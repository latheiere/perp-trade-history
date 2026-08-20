from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from perp_trade_history.adapters.binance import (
    ARCHIVE_WINDOW_MS,
    BINANCE_ARCHIVE_ENDPOINTS,
    BINANCE_ARCHIVE_RETENTION_MS,
    BinanceAdapter,
    canonicalize_binance_archive_row,
)
from perp_trade_history.config import AppConfig
from perp_trade_history.errors import ApiError
from perp_trade_history.models import format_datetime, parse_datetime, stable_id, utc_now_iso
from perp_trade_history.storage import DataStore, _atomic_text, _exclusive_lock
from perp_trade_history.sync import SourceResult, SyncEngine

ARCHIVE_KINDS = tuple(BINANCE_ARCHIVE_ENDPOINTS)
ARCHIVE_MONTHLY_LIMITS = {"orders": 10, "trades": 5, "income": 5}
ARCHIVE_REPORT_KINDS = ("trades", "income")
ARCHIVE_POLL_DELAY = timedelta(minutes=10)


@dataclass(slots=True)
class ArchiveAction:
    kind: str
    action: str
    status: str
    start_ms: int = 0
    end_ms: int = 0
    download_id: str = ""
    records: int = 0
    archive_file: str = ""
    source_csv: str = ""
    sha256: str = ""
    normalized: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    next_eligible_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BinanceArchiveCoordinator:
    """Manage asynchronous account-history exports without persisting signed URLs."""

    def __init__(
        self,
        config: AppConfig,
        store: DataStore | None = None,
        *,
        adapter: BinanceAdapter | None = None,
    ):
        venue = config.venues["binance"]
        if not venue.enabled:
            raise ValueError("Binance must be enabled for archive operations")
        self.config = config
        self.store = store or DataStore(config.data_dir)
        self.adapter = adapter or BinanceAdapter(config, venue, self.store)
        self.engine = SyncEngine(config, self.store)
        self.account_id = venue.account_id
        self.state_path = self.store.root / "state" / "binance-archives.json"
        self.lock_path = self.state_path.with_suffix(f"{self.state_path.suffix}.lock")
        self.artifact_root = self.store.root / "raw" / "binance" / "archive-files"

    def status(self) -> dict[str, Any]:
        payload = self._load_state()
        return {
            "schema_version": payload["schema_version"],
            "updated_at": payload.get("updated_at", ""),
            "next_kind_index": payload.get("next_kind_index", 0),
            "targets": payload.get("targets", {}),
            "jobs": [payload["jobs"][key] for key in sorted(payload["jobs"])],
            "coverage": {
                kind: self._kind_coverage(payload, kind) for kind in ARCHIVE_KINDS
            },
            "backfill": self._backfill_status(payload),
        }

    def backfill(
        self,
        *,
        end_ms: int,
        now: datetime | None = None,
    ) -> tuple[list[ArchiveAction], dict[str, Any]]:
        """Advance an operator-initiated, year-first historical archive fill."""
        now_utc = (now or datetime.now(tz=UTC)).astimezone(UTC)
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            self._ensure_backfill(state, end_ms=end_ms)
            self._refresh_backfill_state(state, now=now_utc)
            self._save_state(state)

        actions = self.poll(now=now_utc, minimum_age=ARCHIVE_POLL_DELAY)

        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            self._refresh_backfill_state(state, now=now_utc)
            if self._pending_jobs(state):
                self._save_state(state)
                return actions, self._backfill_status(state, now=now_utc)

            backfill = state["backfill"]
            next_request_text = str(backfill.get("next_request_at") or "")
            quota_kinds = {
                str(kind) for kind in backfill.get("quota_exhausted_kinds", [])
            }
            if (
                next_request_text
                and now_utc < parse_datetime(next_request_text)
                and quota_kinds.intersection(ARCHIVE_REPORT_KINDS)
            ):
                self._save_state(state)
                return actions, self._backfill_status(state, now=now_utc)
            if next_request_text and now_utc >= parse_datetime(next_request_text):
                backfill["next_request_at"] = ""
                backfill["quota_exhausted_kinds"] = []
                quota_kinds = set()

            critical_blocked = False
            orders_blocked = "orders" in quota_kinds
            for start_ms, window_end_ms in self._backfill_windows(state):
                missing_report = [
                    kind
                    for kind in ARCHIVE_REPORT_KINDS
                    if not self._interval_is_complete(
                        state, kind, start_ms, window_end_ms
                    )
                ]
                for kind in missing_report:
                    action = self._request_backfill_window(
                        state,
                        kind=kind,
                        start_ms=start_ms,
                        end_ms=window_end_ms,
                        now=now_utc,
                    )
                    actions.append(action)
                    if action.status in {"waiting_for_quota", "error"}:
                        critical_blocked = True
                        break
                if critical_blocked:
                    break

                report_ready = all(
                    self._interval_is_complete(state, kind, start_ms, window_end_ms)
                    or self._has_active_window_job(
                        state, kind, start_ms, window_end_ms
                    )
                    for kind in ARCHIVE_REPORT_KINDS
                )
                if (
                    report_ready
                    and not orders_blocked
                    and not self._interval_is_complete(
                        state, "orders", start_ms, window_end_ms
                    )
                    and not self._has_active_window_job(
                        state, "orders", start_ms, window_end_ms
                    )
                ):
                    order_action = self._request_backfill_window(
                        state,
                        kind="orders",
                        start_ms=start_ms,
                        end_ms=window_end_ms,
                        now=now_utc,
                    )
                    actions.append(order_action)
                    orders_blocked = order_action.status in {
                        "waiting_for_quota",
                        "error",
                    }

            self._refresh_backfill_state(state, now=now_utc)
            self._save_state(state)
            return actions, self._backfill_status(state, now=now_utc)

    def request_next(
        self,
        *,
        end_ms: int,
        kinds: tuple[str, ...] = ARCHIVE_KINDS,
        max_requests: int = 1,
        recover_gaps: bool = False,
    ) -> list[ArchiveAction]:
        selected = self._validate_kinds(kinds)
        if max_requests < 1 or max_requests > len(selected):
            raise ValueError("max_requests must be within the selected archive kind count")
        actions: list[ArchiveAction] = []
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            ordered = self._round_robin(selected, int(state.get("next_kind_index") or 0))
            requests = 0
            for kind in ordered:
                if requests >= max_requests:
                    break
                if self._has_pending_job(state, kind):
                    actions.append(
                        ArchiveAction(kind, "request", "skipped", message="export is pending")
                    )
                    continue
                window = self._next_window(
                    state,
                    kind,
                    end_ms=end_ms,
                    recover_gaps=recover_gaps,
                )
                if window is None:
                    actions.append(
                        ArchiveAction(
                            kind,
                            "request",
                            "complete",
                            message="no uncovered archive interval meets the request threshold",
                        )
                    )
                    continue
                if self._monthly_request_count(state, kind) >= ARCHIVE_MONTHLY_LIMITS[kind]:
                    next_eligible_at = self._next_month_at()
                    state["targets"].setdefault(kind, {})[
                        "next_eligible_at"
                    ] = next_eligible_at
                    self._save_state(state)
                    actions.append(
                        ArchiveAction(
                            kind,
                            "request",
                            "waiting_for_quota",
                            message="locally tracked monthly export quota is exhausted",
                            next_eligible_at=next_eligible_at,
                        )
                    )
                    continue
                start_ms, window_end_ms = window
                action = self._request_archive(
                    state,
                    kind=kind,
                    start_ms=start_ms,
                    end_ms=window_end_ms,
                    action="request",
                )
                state["next_kind_index"] = (ARCHIVE_KINDS.index(kind) + 1) % len(
                    ARCHIVE_KINDS
                )
                self._save_state(state)
                requests += 1
                actions.append(action)
        return actions

    def backfill_step(
        self,
        *,
        end_ms: int,
        kinds: tuple[str, ...] = ARCHIVE_KINDS,
        max_requests: int = 1,
    ) -> list[ArchiveAction]:
        """Advance pending initial exports without extending completed targets."""
        actions = self.poll()
        actions.extend(
            self.request_next(
                end_ms=end_ms,
                kinds=kinds,
                max_requests=max_requests,
                recover_gaps=False,
            )
        )
        return actions

    def request_range(
        self,
        *,
        kind: str,
        start_ms: int,
        end_ms: int,
    ) -> ArchiveAction:
        self._validate_kinds((kind,))
        if start_ms > end_ms:
            raise ValueError("archive range start cannot exceed end")
        if end_ms - start_ms > ARCHIVE_WINDOW_MS:
            raise ValueError("archive range cannot exceed one year")
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            if self._has_pending_job(state, kind):
                return ArchiveAction(
                    kind,
                    "request-range",
                    "skipped",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    message="export is pending",
                )
            if not _uncovered_intervals(
                self._complete_intervals(state, kind), start_ms, end_ms
            ):
                return ArchiveAction(
                    kind,
                    "request-range",
                    "skipped",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    message="archive interval is already covered",
                )
            if self._monthly_request_count(state, kind) >= ARCHIVE_MONTHLY_LIMITS[kind]:
                return ArchiveAction(
                    kind,
                    "request-range",
                    "waiting_for_quota",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    message="locally tracked monthly export quota is exhausted",
                    next_eligible_at=self._next_month_at(),
                )
            return self._request_archive(
                state,
                kind=kind,
                start_ms=start_ms,
                end_ms=end_ms,
                action="request-range",
            )

    def _request_archive(
        self,
        state: dict[str, Any],
        *,
        kind: str,
        start_ms: int,
        end_ms: int,
        action: str,
    ) -> ArchiveAction:
        requested_at = utc_now_iso()
        attempt_id = stable_id("attempt", requested_at, len(state["jobs"]))
        job_key = self._job_key(
            kind,
            start_ms,
            end_ms,
            attempt_id=attempt_id,
        )
        job = {
            "job_key": job_key,
            "account_id": self.account_id,
            "kind": kind,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "download_id": "",
            "status": "requesting",
            "requested_at": requested_at,
            "last_polled_at": "",
            "imported_at": "",
            "records": 0,
            "archive_file": "",
            "source_csv": "",
            "sha256": "",
            "complete_range": True,
            "last_error": "",
        }
        state["jobs"][job_key] = job
        # Persist before issuing the high-weight request because a connection failure
        # cannot reveal whether the venue consumed the monthly export allowance.
        self._save_state(state)
        try:
            response = self.adapter.request_archive(
                kind, start_ms=start_ms, end_ms=end_ms
            )
            download_id = str(response["downloadId"])
            if not download_id:
                raise ValueError("archive request returned an empty download id")
        except Exception as exc:
            quota_exhausted = _is_archive_quota_error(exc)
            job["status"] = "quota-exhausted" if quota_exhausted else "request-error"
            job["last_error"] = str(exc)
            if quota_exhausted:
                self._record_quota_wait(
                    state,
                    (kind,),
                    now=parse_datetime(requested_at),
                )
            self._save_state(state)
            return ArchiveAction(
                kind,
                action,
                "waiting_for_quota" if quota_exhausted else "error",
                start_ms=start_ms,
                end_ms=end_ms,
                message=str(exc),
                next_eligible_at=(
                    self._next_month_at(parse_datetime(requested_at))
                    if quota_exhausted
                    else ""
                ),
            )
        job["download_id"] = download_id
        job["status"] = "requested"
        self._save_state(state)
        return ArchiveAction(
            kind,
            action,
            "requested",
            start_ms=start_ms,
            end_ms=end_ms,
            download_id=download_id,
        )

    def poll(
        self,
        *,
        now: datetime | None = None,
        minimum_age: timedelta = timedelta(0),
    ) -> list[ArchiveAction]:
        actions: list[ArchiveAction] = []
        now_utc = (now or datetime.now(tz=UTC)).astimezone(UTC)
        with _exclusive_lock(self.lock_path):
            state = self._load_state()
            for job_key in sorted(state["jobs"]):
                job = state["jobs"][job_key]
                if job.get("status") not in {"requested", "processing", "parse-error"}:
                    continue
                if job.get("status") != "parse-error" and minimum_age:
                    activity_text = str(
                        job.get("last_polled_at") or job.get("requested_at") or ""
                    )
                    if activity_text and now_utc < (
                        parse_datetime(activity_text) + minimum_age
                    ):
                        continue
                kind = str(job["kind"])
                try:
                    if job.get("status") == "parse-error" and job.get("archive_file"):
                        payload = (self.store.root / str(job["archive_file"])).read_bytes()
                        imported = self._import_payload(
                            kind,
                            payload,
                            start_ms=int(job["start_ms"]),
                            end_ms=int(job["end_ms"]),
                            complete_range=True,
                            origin="retained",
                        )
                        job.update(
                            {
                                "status": "imported",
                                "imported_at": utc_now_iso(),
                                "records": imported.records,
                                "source_csv": imported.source_csv,
                                "last_error": "",
                            }
                        )
                        actions.append(imported)
                        continue
                    response = self.adapter.poll_archive(kind, str(job["download_id"]))
                    job["last_polled_at"] = utc_now_iso()
                    status = str(response.get("status") or "processing").lower()
                    if status != "completed":
                        job["status"] = "processing"
                        job["last_error"] = ""
                        actions.append(
                            ArchiveAction(
                                kind,
                                "poll",
                                "processing",
                                start_ms=int(job["start_ms"]),
                                end_ms=int(job["end_ms"]),
                                download_id=str(job["download_id"]),
                            )
                        )
                        continue
                    if self._is_expired(response):
                        job["status"] = "expired"
                        job["last_error"] = "completed export link expired before download"
                        actions.append(
                            ArchiveAction(
                                kind,
                                "poll",
                                "expired",
                                start_ms=int(job["start_ms"]),
                                end_ms=int(job["end_ms"]),
                                message=job["last_error"],
                            )
                        )
                        continue
                    url = str(response.get("url") or "")
                    if not url:
                        raise ValueError("completed archive response did not include a URL")
                    payload = self.adapter.download_archive(url)
                    imported = self._import_payload(
                        kind,
                        payload,
                        start_ms=int(job["start_ms"]),
                        end_ms=int(job["end_ms"]),
                        complete_range=True,
                        origin="download",
                    )
                    job.update(
                        {
                            "status": "imported",
                            "imported_at": utc_now_iso(),
                            "records": imported.records,
                            "archive_file": imported.archive_file,
                            "source_csv": imported.source_csv,
                            "sha256": imported.sha256,
                            "last_error": "",
                        }
                    )
                    actions.append(imported)
                except RetainedArchiveError as exc:
                    job.update(
                        {
                            "status": "parse-error",
                            "archive_file": exc.archive_file,
                            "sha256": exc.sha256,
                            "last_error": str(exc),
                        }
                    )
                    actions.append(
                        ArchiveAction(
                            kind,
                            "local-reparse",
                            "error",
                            start_ms=int(job["start_ms"]),
                            end_ms=int(job["end_ms"]),
                            archive_file=exc.archive_file,
                            sha256=exc.sha256,
                            message=str(exc),
                        )
                    )
                except Exception as exc:
                    job["last_error"] = str(exc)
                    actions.append(
                        ArchiveAction(
                            kind,
                            "poll",
                            "error",
                            start_ms=int(job["start_ms"]),
                            end_ms=int(job["end_ms"]),
                            download_id=str(job.get("download_id") or ""),
                            message=str(exc),
                        )
                    )
                finally:
                    self._save_state(state)
        return actions

    def import_file(
        self,
        *,
        kind: str,
        path: Path,
        start_ms: int | None = None,
        end_ms: int | None = None,
        complete_range: bool = False,
    ) -> ArchiveAction:
        self._validate_kinds((kind,))
        if complete_range and (start_ms is None or end_ms is None):
            raise ValueError("a complete manual range requires both start and end")
        payload = path.read_bytes()
        action = self._import_payload(
            kind,
            payload,
            start_ms=start_ms or 0,
            end_ms=end_ms or 0,
            complete_range=complete_range,
            origin="manual",
        )
        if complete_range:
            with _exclusive_lock(self.lock_path):
                state = self._load_state()
                manual_id = f"manual:{hashlib.sha256(payload).hexdigest()}"
                job_key = self._job_key(
                    kind,
                    int(start_ms),
                    int(end_ms),
                    attempt_id=manual_id,
                )
                previous = state["jobs"].get(job_key, {})
                state["jobs"][job_key] = {
                    "job_key": job_key,
                    "account_id": self.account_id,
                    "kind": kind,
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "download_id": manual_id,
                    "status": "imported",
                    "requested_at": previous.get("requested_at", ""),
                    "last_polled_at": previous.get("last_polled_at", ""),
                    "imported_at": utc_now_iso(),
                    "records": action.records,
                    "archive_file": action.archive_file,
                    "source_csv": action.source_csv,
                    "sha256": action.sha256,
                    "complete_range": True,
                    "last_error": "",
                }
                self._save_state(state)
        return action

    def _import_payload(
        self,
        kind: str,
        payload: bytes,
        *,
        start_ms: int,
        end_ms: int,
        complete_range: bool,
        origin: str,
    ) -> ArchiveAction:
        digest = hashlib.sha256(payload).hexdigest()
        stem = (
            f"{start_ms}-{end_ms}-{digest}"
            if start_ms and end_ms
            else f"manual-{digest}"
        )
        destination = self.artifact_root / kind
        extension = archive_extension(payload)
        archive_path = destination / f"{stem}{extension}"
        source_csv_path = destination / f"{stem}.source.csv"
        _atomic_bytes(archive_path, payload)
        try:
            rows = parse_binance_archive(payload, kind=kind)
            _write_source_csv(source_csv_path, rows)

            collected_at = utc_now_iso()
            batch = self.adapter.normalize_archive_rows(
                kind, rows, collected_at=collected_at
            )
            batch.requested_start_ms = start_ms or batch.oldest_ms
            batch.covered_through_ms = end_ms or batch.newest_ms
            if complete_range and rows and (
                batch.oldest_ms < start_ms or batch.newest_ms > end_ms
            ):
                raise ValueError(
                    "archive records fall outside the asserted complete range"
                )
            batch.backfill_complete = bool(complete_range)
            batch.coverage_dataset = kind
            batch.coverage_status = "complete" if complete_range else "partial"
            batch.coverage_scope = "account"
            batch.coverage_limitation = (
                "exchange export range supplied explicitly"
                if complete_range
                else "manual archive range was inferred from contained records"
            )
            batch.acquisition = (
                "archive" if origin in {"download", "retained"} else "manual-archive"
            )
            normalized: SourceResult = self.engine.persist_batch(batch)
        except Exception as exc:
            raise RetainedArchiveError(
                f"archive import failed after retaining source bytes: {exc}",
                archive_file=str(archive_path.relative_to(self.store.root)),
                sha256=digest,
            ) from exc
        return ArchiveAction(
            kind=kind,
            action="import",
            status="imported",
            start_ms=batch.requested_start_ms,
            end_ms=batch.covered_through_ms,
            records=len(rows),
            archive_file=str(archive_path.relative_to(self.store.root)),
            source_csv=str(source_csv_path.relative_to(self.store.root)),
            sha256=digest,
            normalized=normalized.to_dict(),
        )

    def _ensure_backfill(self, state: dict[str, Any], *, end_ms: int) -> None:
        initial_start_ms = self.config.collection.initial_start_ms
        if end_ms < initial_start_ms:
            raise ValueError("archive backfill end cannot precede collection.initial_start")
        backfill = state.setdefault(
            "backfill",
            {
                "initial_start_ms": initial_start_ms,
                "target_end_ms": end_ms,
                "started_at": utc_now_iso(),
                "completed_at": "",
                "next_poll_at": "",
                "next_request_at": "",
                "quota_exhausted_kinds": [],
            },
        )
        backfill["initial_start_ms"] = initial_start_ms
        backfill.setdefault("target_end_ms", end_ms)
        backfill.setdefault("started_at", utc_now_iso())
        backfill.setdefault("completed_at", "")
        backfill.setdefault("next_poll_at", "")
        backfill.setdefault("next_request_at", "")
        backfill.setdefault("quota_exhausted_kinds", [])
        for kind in ARCHIVE_KINDS:
            target = state["targets"].setdefault(kind, {})
            target["initial_start_ms"] = initial_start_ms
            target["deep_end_ms"] = int(backfill["target_end_ms"])

    def _request_backfill_window(
        self,
        state: dict[str, Any],
        *,
        kind: str,
        start_ms: int,
        end_ms: int,
        now: datetime,
    ) -> ArchiveAction:
        if self._has_active_window_job(state, kind, start_ms, end_ms):
            return ArchiveAction(
                kind,
                "backfill-request",
                "skipped",
                start_ms=start_ms,
                end_ms=end_ms,
                message="matching export is pending",
            )
        if self._monthly_request_count(state, kind) >= ARCHIVE_MONTHLY_LIMITS[kind]:
            self._record_quota_wait(state, (kind,), now=now)
            return ArchiveAction(
                kind,
                "backfill-request",
                "waiting_for_quota",
                start_ms=start_ms,
                end_ms=end_ms,
                message="locally tracked monthly export quota is exhausted",
                next_eligible_at=self._next_month_at(now),
            )
        return self._request_archive(
            state,
            kind=kind,
            start_ms=start_ms,
            end_ms=end_ms,
            action="backfill-request",
        )

    def _record_quota_wait(
        self,
        state: dict[str, Any],
        kinds: tuple[str, ...],
        *,
        now: datetime,
    ) -> None:
        backfill = state.get("backfill")
        if not isinstance(backfill, dict):
            return
        recorded = {
            str(kind) for kind in backfill.get("quota_exhausted_kinds", [])
        }
        recorded.update(kinds)
        backfill["quota_exhausted_kinds"] = sorted(recorded)
        backfill["next_request_at"] = self._next_month_at(now)

    def _refresh_backfill_state(
        self, state: dict[str, Any], *, now: datetime
    ) -> None:
        backfill = state.get("backfill")
        if not isinstance(backfill, dict):
            return
        pending = self._pending_jobs(state)
        next_poll = ""
        for job in pending:
            activity_text = str(
                job.get("last_polled_at") or job.get("requested_at") or ""
            )
            if not activity_text:
                continue
            eligible = parse_datetime(activity_text) + ARCHIVE_POLL_DELAY
            eligible_text = format_datetime(eligible)
            if not next_poll or eligible_text < next_poll:
                next_poll = eligible_text
        backfill["next_poll_at"] = next_poll

        status = self._backfill_status(state, now=now)
        if status["status"] == "complete":
            backfill["completed_at"] = str(backfill.get("completed_at") or format_datetime(now))
            backfill["next_poll_at"] = ""
            backfill["next_request_at"] = ""
            backfill["quota_exhausted_kinds"] = []
        elif backfill.get("completed_at"):
            backfill["completed_at"] = ""

    def _backfill_status(
        self,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        backfill = state.get("backfill")
        if not isinstance(backfill, dict):
            return {
                "status": "not_started",
                "expected_start_at": _format_ms(self.config.collection.initial_start_ms),
                "target_end_at": "",
                "covered_back_to": "",
                "remaining_report_years": 0,
                "remaining_order_years": 0,
                "remaining_files": 0,
                "next_window": {},
                "next_poll_at": "",
                "next_request_at": "",
                "next_action_at": "",
                "completed_at": "",
                "quota": self._quota_status(state),
            }

        windows = self._backfill_windows(state)
        missing_by_window: list[dict[str, Any]] = []
        report_remaining = 0
        order_remaining = 0
        remaining_files = 0
        covered_back_to = ""
        contiguous = True
        for start_ms, end_ms in windows:
            missing = [
                kind
                for kind in ARCHIVE_KINDS
                if not self._interval_is_complete(state, kind, start_ms, end_ms)
            ]
            missing_report = [kind for kind in missing if kind in ARCHIVE_REPORT_KINDS]
            if missing_report:
                report_remaining += 1
            if "orders" in missing:
                order_remaining += 1
            remaining_files += len(missing)
            if missing:
                missing_by_window.append(
                    {
                        "start_at": _format_ms(start_ms),
                        "end_at": _format_ms(end_ms),
                        "missing_kinds": missing,
                    }
                )
            if contiguous and not missing_report:
                covered_back_to = _format_ms(start_ms)
            elif missing_report:
                contiguous = False

        pending = self._pending_jobs(state)
        next_poll_at = str(backfill.get("next_poll_at") or "")
        next_request_at = str(backfill.get("next_request_at") or "")
        now_utc = (now or datetime.now(tz=UTC)).astimezone(UTC)
        waiting_for_quota = bool(
            next_request_at and now_utc < parse_datetime(next_request_at)
        )
        if not missing_by_window:
            status = "complete"
        elif report_remaining == 0:
            status = "report_complete_orders_pending"
        elif pending:
            status = "waiting_for_exports"
        elif waiting_for_quota:
            status = "waiting_for_quota"
        else:
            status = "ready"
        return {
            "status": status,
            "expected_start_at": _format_ms(int(backfill["initial_start_ms"])),
            "target_end_at": _format_ms(int(backfill["target_end_ms"])),
            "covered_back_to": covered_back_to,
            "remaining_report_years": report_remaining,
            "remaining_order_years": order_remaining,
            "remaining_files": remaining_files,
            "next_window": missing_by_window[0] if missing_by_window else {},
            "next_poll_at": next_poll_at,
            "next_request_at": next_request_at,
            "next_action_at": next_poll_at or next_request_at,
            "completed_at": str(backfill.get("completed_at") or ""),
            "quota_exhausted_kinds": list(
                backfill.get("quota_exhausted_kinds", [])
            ),
            "pending_exports": len(pending),
            "quota": self._quota_status(state),
        }

    def _quota_status(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            kind: {
                "used": self._monthly_request_count(state, kind),
                "limit": ARCHIVE_MONTHLY_LIMITS[kind],
                "remaining": max(
                    ARCHIVE_MONTHLY_LIMITS[kind]
                    - self._monthly_request_count(state, kind),
                    0,
                ),
            }
            for kind in ARCHIVE_KINDS
        }

    def _backfill_windows(self, state: dict[str, Any]) -> list[tuple[int, int]]:
        backfill = state.get("backfill")
        if not isinstance(backfill, dict):
            return []
        initial_start_ms = int(backfill["initial_start_ms"])
        cursor_end_ms = int(backfill["target_end_ms"])
        windows: list[tuple[int, int]] = []
        while cursor_end_ms >= initial_start_ms:
            start_ms = max(initial_start_ms, cursor_end_ms - ARCHIVE_WINDOW_MS)
            windows.append((start_ms, cursor_end_ms))
            cursor_end_ms = start_ms - 1
        return windows

    def _interval_is_complete(
        self,
        state: dict[str, Any],
        kind: str,
        start_ms: int,
        end_ms: int,
    ) -> bool:
        return not _uncovered_intervals(
            self._complete_intervals(state, kind), start_ms, end_ms
        )

    def _has_active_window_job(
        self,
        state: dict[str, Any],
        kind: str,
        start_ms: int,
        end_ms: int,
    ) -> bool:
        return any(
            job.get("account_id") == self.account_id
            and job.get("kind") == kind
            and int(job.get("start_ms") or 0) == start_ms
            and int(job.get("end_ms") or 0) == end_ms
            and job.get("status") in {"requested", "processing", "parse-error"}
            for job in state["jobs"].values()
        )

    def _pending_jobs(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            job
            for job in state["jobs"].values()
            if job.get("account_id") == self.account_id
            and job.get("status") in {"requested", "processing", "parse-error"}
        ]

    def _next_window(
        self,
        state: dict[str, Any],
        kind: str,
        *,
        end_ms: int,
        recover_gaps: bool,
    ) -> tuple[int, int] | None:
        archive_end_ms = end_ms - BINANCE_ARCHIVE_RETENTION_MS[kind] - 1
        if archive_end_ms < self.config.collection.initial_start_ms:
            return None
        target = state["targets"].setdefault(
            kind,
            {
                "initial_start_ms": self.config.collection.initial_start_ms,
                "deep_end_ms": archive_end_ms,
            },
        )
        target["initial_start_ms"] = min(
            int(target["initial_start_ms"]), self.config.collection.initial_start_ms
        )
        if recover_gaps:
            target["deep_end_ms"] = max(int(target["deep_end_ms"]), archive_end_ms)
        initial_ms = int(target["initial_start_ms"])
        deep_end_ms = int(target["deep_end_ms"])
        intervals = self._complete_intervals(state, kind)
        gaps = _uncovered_intervals(intervals, initial_ms, deep_end_ms)
        if gaps:
            gap_start, gap_end = gaps[-1]
            return max(gap_start, gap_end - ARCHIVE_WINDOW_MS), gap_end
        return None

    def _kind_coverage(self, state: dict[str, Any], kind: str) -> dict[str, Any]:
        target = state.get("targets", {}).get(kind)
        intervals = self._complete_intervals(state, kind)
        if not target:
            return {
                "status": "not_started",
                "complete_intervals": intervals,
                "gaps": [],
            }
        gaps = _uncovered_intervals(
            intervals,
            int(target["initial_start_ms"]),
            int(target["deep_end_ms"]),
        )
        return {
            "status": "complete" if not gaps else "incomplete",
            "target_start_ms": int(target["initial_start_ms"]),
            "target_end_ms": int(target["deep_end_ms"]),
            "complete_intervals": intervals,
            "gaps": gaps,
        }

    def _complete_intervals(
        self, state: dict[str, Any], kind: str
    ) -> list[tuple[int, int]]:
        intervals = [
            (int(job["start_ms"]), int(job["end_ms"]))
            for job in state["jobs"].values()
            if job.get("account_id") == self.account_id
            and job.get("kind") == kind
            and job.get("status") == "imported"
            and job.get("complete_range") is True
        ]
        return _merge_intervals(intervals)

    def _has_pending_job(self, state: dict[str, Any], kind: str) -> bool:
        return any(
            job.get("account_id") == self.account_id
            and job.get("kind") == kind
            and job.get("status") in {"requested", "processing"}
            for job in state["jobs"].values()
        )

    def _monthly_request_count(self, state: dict[str, Any], kind: str) -> int:
        now = parse_datetime(utc_now_iso())
        count = 0
        for job in state["jobs"].values():
            requested_at = str(job.get("requested_at") or "")
            if (
                job.get("account_id") != self.account_id
                or job.get("kind") != kind
                or not requested_at
                or job.get("status") == "quota-exhausted"
            ):
                continue
            requested = parse_datetime(requested_at)
            if requested.year == now.year and requested.month == now.month:
                count += 1
        return count

    @staticmethod
    def _next_month_at(now: datetime | None = None) -> str:
        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1, tzinfo=UTC)
        else:
            next_month = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
        return next_month.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": 1,
                "updated_at": "",
                "next_kind_index": 0,
                "targets": {},
                "jobs": {},
            }
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported Binance archive state in {self.state_path}")
        payload.setdefault("next_kind_index", 0)
        payload.setdefault("targets", {})
        payload.setdefault("jobs", {})
        return payload

    def _save_state(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = utc_now_iso()
        _atomic_text(self.state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _job_key(
        self,
        kind: str,
        start_ms: int,
        end_ms: int,
        *,
        attempt_id: str,
    ) -> str:
        return stable_id(self.account_id, kind, start_ms, end_ms, attempt_id)

    @staticmethod
    def _validate_kinds(kinds: tuple[str, ...]) -> tuple[str, ...]:
        if not kinds:
            raise ValueError("at least one Binance archive kind is required")
        invalid = set(kinds) - set(ARCHIVE_KINDS)
        if invalid:
            raise ValueError(f"unsupported Binance archive kinds: {sorted(invalid)}")
        return tuple(dict.fromkeys(kinds))

    @staticmethod
    def _round_robin(kinds: tuple[str, ...], index: int) -> tuple[str, ...]:
        by_global_order = sorted(kinds, key=ARCHIVE_KINDS.index)
        eligible = [kind for kind in ARCHIVE_KINDS[index:] if kind in by_global_order]
        eligible.extend(kind for kind in ARCHIVE_KINDS[:index] if kind in by_global_order)
        return tuple(eligible)

    @staticmethod
    def _is_expired(response: dict[str, Any]) -> bool:
        raw = response.get("isExpired")
        explicit = str(raw).strip().lower() in {"true", "1", "yes"}
        legacy = str(response.get("expired") or "").strip().lower()
        if explicit or legacy in {"true", "1", "yes"}:
            return True
        try:
            expiration_ms = int(response.get("expirationTimestamp") or 0)
        except (TypeError, ValueError):
            expiration_ms = 0
        now_ms = int(parse_datetime(utc_now_iso()).timestamp() * 1000)
        return 0 < expiration_ms <= now_ms


def _format_ms(value: int) -> str:
    return format_datetime(datetime.fromtimestamp(value / 1000, tz=UTC))


def _is_archive_quota_error(exc: Exception) -> bool:
    """Recognize the monthly export allowance without conflating HTTP rate limits."""
    if not isinstance(exc, ApiError) or exc.status_code == 429:
        return False
    message = str(exc).lower()
    monthly_limit_markers = (
        "times per month",
        "requests for this month",
        "request limitation",
        "monthly download limit",
        "monthly export limit",
    )
    return any(marker in message for marker in monthly_limit_markers)


class RetainedArchiveError(ValueError):
    """Archive import failed after immutable source bytes were retained."""

    def __init__(self, message: str, *, archive_file: str, sha256: str):
        super().__init__(message)
        self.archive_file = archive_file
        self.sha256 = sha256


def parse_binance_archive(
    payload: bytes, *, kind: str | None = None
) -> list[dict[str, str]]:
    documents: list[tuple[str, bytes]]

    if payload.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            csv_names = [name for name in names if name.lower().endswith(".csv")]
            selected = csv_names or names
            documents = [(name, archive.read(name)) for name in sorted(selected)]
    elif payload.startswith(b"\x1f\x8b"):
        documents = [("archive.csv", gzip.decompress(payload))]
    else:
        documents = [("archive.csv", payload)]

    rows: list[dict[str, str]] = []

    for name, document in documents:
        text = document.decode("utf-8-sig")

        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        except csv.Error:
            reader = csv.DictReader(io.StringIO(text))

        if reader.fieldnames is None:
            continue

        fieldnames = [str(field).strip() for field in reader.fieldnames]

        if kind:
            canonical_header = canonicalize_binance_archive_row(
                kind,
                {field: "present" for field in fieldnames},
            )

            required = {
                "orders": ("orderId", "time"),
                "trades": ("id", "time"),
                "income": ("incomeType", "time"),
            }[kind]

            missing = [
                field for field in required
                if field not in canonical_header
            ]

            if missing:
                raise ValueError(
                    f"Binance {kind} archive member {name!r} lacks required "
                    f"columns {tuple(missing)}; "
                    f"raw columns={fieldnames!r}; "
                    f"canonical columns={list(canonical_header)!r}"
                )

        for row in reader:
            normalized = {
                str(key).strip(): str(value) if value is not None else ""
                for key, value in row.items()
                if key is not None
            }

            if len(documents) > 1:
                normalized["archive_member"] = name

            rows.append(normalized)

    return rows


def archive_extension(payload: bytes) -> str:
    if payload.startswith(b"PK\x03\x04"):
        return ".zip"
    if payload.startswith(b"\x1f\x8b"):
        return ".csv.gz"
    return ".csv"


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start_ms, end_ms in sorted(intervals):
        if start_ms > end_ms:
            raise ValueError("coverage interval start cannot exceed end")
        if not merged or start_ms > merged[-1][1] + 1:
            merged.append((start_ms, end_ms))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end_ms))
    return merged


def _uncovered_intervals(
    intervals: list[tuple[int, int]], start_ms: int, end_ms: int
) -> list[tuple[int, int]]:
    if start_ms > end_ms:
        return []
    gaps: list[tuple[int, int]] = []
    cursor = start_ms
    for covered_start, covered_end in _merge_intervals(intervals):
        if covered_end < start_ms or covered_start > end_ms:
            continue
        bounded_start = max(covered_start, start_ms)
        bounded_end = min(covered_end, end_ms)
        if bounded_start > cursor:
            gaps.append((cursor, bounded_start - 1))
        cursor = max(cursor, bounded_end + 1)
    if cursor <= end_ms:
        gaps.append((cursor, end_ms))
    return gaps


def _write_source_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO(newline="")
    if fieldnames:
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    _atomic_text(path, buffer.getvalue())


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
