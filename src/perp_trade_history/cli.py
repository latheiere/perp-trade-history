from __future__ import annotations

import argparse
import csv
import fcntl
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from perp_trade_history.archive_schedule import (
    ArchiveRerunScheduler,
    parse_schedule_delay,
    scheduled_time,
)
from perp_trade_history.binance_archives import (
    ARCHIVE_KINDS,
    ARCHIVE_MONTHLY_LIMITS,
    ArchiveAction,
    BinanceArchiveCoordinator,
)
from perp_trade_history.config import AppConfig, load_config
from perp_trade_history.conversion import StoredCashflowConversion
from perp_trade_history.errors import CollectorBusyError, PerpTradeHistoryError
from perp_trade_history.models import parse_datetime, utc_now_iso
from perp_trade_history.reporting import REPORT_FIELDS, REPORT_PERIODS, pnl_report
from perp_trade_history.schedule import WeeklyRestSchedule
from perp_trade_history.storage import DataStore
from perp_trade_history.sync import SyncEngine, run_conversion_pass

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perp-trade-history",
        description=(
            "Collect and store normalized perpetual-account history through read-only APIs."
        ),
    )
    parser.add_argument("--config", help="TOML configuration path")
    parser.add_argument("--secrets", help="Environment-style credential file path")
    parser.add_argument("--verbose", action="store_true", help="Enable diagnostic logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check-config", help="Validate configuration without network access"
    )
    check.add_argument("--json", action="store_true", help="Emit JSON")

    collect = subparsers.add_parser("collect", help="Collect all enabled history sources")
    collect.add_argument("--venue", action="append", choices=["binance", "gate", "mexc"])
    collect.add_argument("--full", action="store_true", help="Revisit full available history")
    collect.add_argument("--end", help="Inclusive UTC ISO collection end; defaults to now")
    collect.add_argument("--json", action="store_true", help="Emit JSON")

    recalculate = subparsers.add_parser(
        "recalculate-conversions",
        help="Rebuild every stored cashflow conversion with the configured basis",
    )
    recalculate.add_argument(
        "--venue", action="append", choices=["binance", "gate", "mexc"]
    )
    recalculate.add_argument("--json", action="store_true", help="Emit JSON")

    weekly = subparsers.add_parser(
        "weekly-collect",
        help="Run REST ingestion once for each Monday 08:00 UTC schedule window",
    )
    weekly.add_argument(
        "--now",
        help="UTC ISO scheduling instant; defaults to the current time",
    )
    weekly.add_argument("--json", action="store_true", help="Emit JSON")

    archives = subparsers.add_parser(
        "archives", help="Manage asynchronous historical exports and manual archive imports"
    )
    archive_commands = archives.add_subparsers(dest="archive_command", required=True)
    archive_backfill = archive_commands.add_parser(
        "backfill",
        help="Advance the operator-initiated Binance historical archive fill",
    )
    archive_backfill.add_argument(
        "--end", help="Inclusive UTC target end fixed on the first run; defaults to now"
    )
    scheduling = archive_backfill.add_mutually_exclusive_group()
    scheduling.add_argument(
        "--schedule-next",
        action="store_true",
        help="Schedule one rerun at the next persisted poll or quota date",
    )
    scheduling.add_argument(
        "--schedule-in",
        metavar="DELAY",
        help="Schedule one rerun after a delay such as 10m or 31d",
    )
    archive_backfill.add_argument(
        "--scheduled", action="store_true", help=argparse.SUPPRESS
    )
    archive_backfill.add_argument("--json", action="store_true", help="Emit JSON")
    archive_request = archive_commands.add_parser(
        "request-next", help="Request the next detected Binance history gap"
    )
    archive_request.add_argument("--kind", action="append", choices=ARCHIVE_KINDS)
    archive_request.add_argument("--max-requests", type=int, default=1)
    archive_request.add_argument("--end", help="Inclusive UTC ISO target end; defaults to now")
    archive_request.add_argument(
        "--recover-gaps",
        action="store_true",
        help="Extend a completed initial target to a newly unreachable REST gap",
    )
    archive_request.add_argument("--json", action="store_true", help="Emit JSON")
    archive_range = archive_commands.add_parser(
        "request-range",
        help="Request one exact Binance export range for an explicitly unreachable gap",
    )
    archive_range.add_argument("--kind", required=True, choices=ARCHIVE_KINDS)
    archive_range.add_argument("--start", required=True, help="Inclusive UTC ISO range start")
    archive_range.add_argument("--end", required=True, help="Inclusive UTC ISO range end")
    archive_range.add_argument("--json", action="store_true", help="Emit JSON")
    archive_step = archive_commands.add_parser(
        "backfill-step",
        help="Poll pending initial exports and request at most one next deep-history gap",
    )
    archive_step.add_argument("--kind", action="append", choices=ARCHIVE_KINDS)
    archive_step.add_argument("--max-requests", type=int, default=1)
    archive_step.add_argument("--end", help="Inclusive UTC ISO target end; defaults to now")
    archive_step.add_argument("--json", action="store_true", help="Emit JSON")
    archive_poll = archive_commands.add_parser(
        "poll", help="Poll, download, retain, and import completed Binance exports"
    )
    archive_poll.add_argument("--json", action="store_true", help="Emit JSON")
    archive_status = archive_commands.add_parser(
        "status", help="Show Binance export jobs, merged coverage, and detected gaps"
    )
    archive_status.add_argument("--json", action="store_true", help="Emit JSON")
    archive_schedule = archive_commands.add_parser(
        "schedule", help="Manage an explicitly requested one-shot archive rerun"
    )
    archive_schedule_commands = archive_schedule.add_subparsers(
        dest="archive_schedule_command", required=True
    )
    archive_schedule_cancel = archive_schedule_commands.add_parser(
        "cancel", help="Cancel the pending one-shot archive rerun"
    )
    archive_schedule_cancel.add_argument("--json", action="store_true", help="Emit JSON")
    archive_import = archive_commands.add_parser(
        "import", help="Import an exchange-provided Binance CSV or compressed archive"
    )
    archive_import.add_argument("--kind", required=True, choices=ARCHIVE_KINDS)
    archive_import.add_argument("--file", required=True, help="CSV, ZIP, or gzip archive path")
    archive_import.add_argument("--start", help="Inclusive UTC ISO export range start")
    archive_import.add_argument("--end", help="Inclusive UTC ISO export range end")
    archive_import.add_argument(
        "--complete-range",
        action="store_true",
        help="Treat the explicit start/end interval as complete export coverage",
    )
    archive_import.add_argument("--json", action="store_true", help="Emit JSON")

    status = subparsers.add_parser("status", help="Show local record counts and source coverage")
    status.add_argument("--json", action="store_true", help="Emit JSON")

    doctor = subparsers.add_parser(
        "doctor", help="Diagnose collection schedule, failures, coverage, archives, and lock state"
    )
    doctor.add_argument("--json", action="store_true", help="Emit JSON")

    backup = subparsers.add_parser(
        "backup", help="Create and self-verify a signed Statecrate data backup"
    )
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--json", action="store_true", help="Emit JSON")

    verify_backup = subparsers.add_parser(
        "verify-backup", help="Verify a signed Statecrate data backup"
    )
    verify_backup.add_argument("archive", type=Path)
    verify_backup.add_argument("--verification-key", type=Path)
    verify_backup.add_argument("--json", action="store_true", help="Emit JSON")

    restore_backup = subparsers.add_parser(
        "restore-backup", help="Verify and restore a Statecrate backup into a staging directory"
    )
    restore_backup.add_argument("archive", type=Path)
    restore_backup.add_argument("--target", type=Path, required=True)
    restore_backup.add_argument("--verification-key", type=Path)
    restore_backup.add_argument("--replace", action="store_true")
    restore_backup.add_argument("--json", action="store_true", help="Emit JSON")

    report = subparsers.add_parser("report", help="Aggregate primary signed cash flows")
    report.add_argument("--period", choices=sorted(REPORT_PERIODS), default="month")
    report.add_argument(
        "--group-by",
        default="venue,symbol,event_type,currency",
        help=f"Comma-separated fields from: {','.join(sorted(REPORT_FIELDS))}",
    )
    report.add_argument("--include-supplemental", action="store_true")
    report.add_argument(
        "--include-non-pnl",
        action="store_true",
        help="Include transfers and account conversions in cash-flow totals",
    )
    report.add_argument("--start", help="Inclusive UTC ISO start")
    report.add_argument("--end", help="Inclusive UTC ISO end")
    report.add_argument("--venue", action="append", choices=["binance", "gate", "mexc"])
    report.add_argument("--symbol", action="append")
    report.add_argument("--output", help="CSV output path; stdout by default")
    report.add_argument("--json", action="store_true", help="Emit JSON instead of CSV")
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(verbose=args.verbose)
    try:
        if args.command == "check-config":
            config = _load(args, require_credentials=True)
            _check_secret_permissions(config)
            payload = _config_summary(config)
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "collect":
            config = _load(args, require_credentials=True)
            _check_secret_permissions(config)
            end_ms = (
                _time_arg(args.end) if args.end else int(datetime.now(tz=UTC).timestamp() * 1000)
            )
            store = DataStore(config.data_dir)
            with _collector_lock(store.root / "state" / "collector.lock"):
                result = SyncEngine(config, store).collect(
                    end_ms=end_ms,
                    full=args.full,
                    venues=set(args.venue) if args.venue else None,
                )
            _emit(result.to_dict(), as_json=args.json)
            return 1 if result.errors else 0
        if args.command == "weekly-collect":
            config = _load(args, require_credentials=True)
            _check_secret_permissions(config)
            now = parse_datetime(args.now) if args.now else datetime.now(tz=UTC)
            store = DataStore(config.data_dir)
            schedule = WeeklyRestSchedule(store.root / "state" / "weekly-rest-ingestion.json")
            with _collector_lock(store.root / "state" / "collector.lock"):
                decision = schedule.decision(now)
                if not decision.due:
                    _emit(
                        {
                            "ok": True,
                            "status": decision.status,
                            "schedule": decision.to_dict(),
                        },
                        as_json=args.json,
                    )
                    return 0
                collect_options: dict[str, Any] = {
                    "end_ms": int(now.timestamp() * 1000)
                }
                if decision.retry_venues:
                    collect_options["venues"] = set(decision.retry_venues)
                result = SyncEngine(config, store).collect(**collect_options)
                retryable_errors = [
                    error
                    for error in result.errors
                    if getattr(error, "retryable", False)
                ]
                error_venues = {
                    str(error.venue)
                    for error in result.errors
                    if getattr(error, "venue", "")
                }
                schedule.record_attempt(
                    now=now,
                    decision=decision,
                    success=not result.errors,
                    error_count=len(result.errors),
                    retryable_error_count=len(retryable_errors),
                    retry_venues=error_venues,
                )
            payload = result.to_dict()
            payload["status"] = "collected" if not result.errors else "retry_required"
            payload["schedule"] = schedule.decision(now).to_dict()
            _emit(payload, as_json=args.json)
            return 1 if result.errors else 0
        if args.command == "archives":
            require_credentials = args.archive_command in {
                "backfill",
                "request-next",
                "request-range",
                "backfill-step",
                "poll",
            }
            config = _load(args, require_credentials=require_credentials)
            if require_credentials:
                _check_secret_permissions(config)
            store = DataStore(config.data_dir)
            coordinator = BinanceArchiveCoordinator(config, store)
            rerun_scheduler = ArchiveRerunScheduler(store.root)
            if args.archive_command == "status":
                archive_status_payload = coordinator.status()
                archive_status_payload["schedule"] = rerun_scheduler.status()
                _emit(archive_status_payload, as_json=args.json)
                return 0
            if args.archive_command == "schedule":
                schedule_payload = rerun_scheduler.cancel()
                _emit(
                    {"ok": True, "status": "cancelled", "schedule": schedule_payload},
                    as_json=args.json,
                )
                return 0
            if args.archive_command == "backfill" and args.scheduled:
                rerun_scheduler.mark_running()
            with _collector_lock(store.root / "state" / "collector.lock"):
                backfill_status: dict[str, Any] = {}
                if args.archive_command == "backfill":
                    now = datetime.now(tz=UTC)
                    end_ms = _time_arg(args.end) if args.end else int(now.timestamp() * 1000)
                    actions, backfill_status = coordinator.backfill(
                        end_ms=end_ms,
                        now=now,
                    )
                elif args.archive_command in {"request-next", "backfill-step"}:
                    end_ms = (
                        _time_arg(args.end)
                        if args.end
                        else int(datetime.now(tz=UTC).timestamp() * 1000)
                    )
                    if args.archive_command == "request-next":
                        actions = coordinator.request_next(
                            end_ms=end_ms,
                            kinds=tuple(args.kind or ARCHIVE_KINDS),
                            max_requests=args.max_requests,
                            recover_gaps=args.recover_gaps,
                        )
                    else:
                        actions = coordinator.backfill_step(
                            end_ms=end_ms,
                            kinds=tuple(args.kind or ARCHIVE_KINDS),
                            max_requests=args.max_requests,
                        )
                elif args.archive_command == "request-range":
                    actions = [
                        coordinator.request_range(
                            kind=args.kind,
                            start_ms=_time_arg(args.start),
                            end_ms=_time_arg(args.end),
                        )
                    ]
                elif args.archive_command == "poll":
                    actions = coordinator.poll()
                elif args.archive_command == "import":
                    action = coordinator.import_file(
                        kind=args.kind,
                        path=Path(args.file),
                        start_ms=_time_arg(args.start) if args.start else None,
                        end_ms=_time_arg(args.end) if args.end else None,
                        complete_range=args.complete_range,
                    )
                    actions = [action]
                else:
                    parser.error(f"unknown archive command: {args.archive_command}")
                conversion_sources = (
                    run_conversion_pass(config, store, venues={"binance"})
                    if args.archive_command in {"backfill", "backfill-step", "poll", "import"}
                    else []
                )
            schedule_payload = rerun_scheduler.status()
            if args.archive_command == "backfill":
                run_at: datetime | None = None
                if args.schedule_in:
                    run_at = datetime.now(tz=UTC) + parse_schedule_delay(args.schedule_in)
                elif args.schedule_next and backfill_status.get("next_action_at"):
                    run_at = scheduled_time(str(backfill_status["next_action_at"]))
                if run_at is not None:
                    schedule_payload = rerun_scheduler.schedule(
                        run_at=run_at,
                        command=_archive_backfill_command(config),
                    )
            payload = {
                "ok": not any(action.status == "error" for action in actions)
                and not any(source.error for source in conversion_sources),
                "actions": [action.to_dict() for action in actions],
                "conversion_sources": [
                    source.to_dict() for source in conversion_sources
                ],
            }
            if args.archive_command == "backfill":
                payload["status"] = backfill_status["status"]
                payload["backfill"] = backfill_status
                payload["schedule"] = schedule_payload
                if args.schedule_next and not backfill_status.get("next_action_at"):
                    payload["schedule"] = {
                        **schedule_payload,
                        "message": "no rerun is needed for the current backfill state",
                    }
            _log_archive_actions(actions)
            _emit(payload, as_json=args.json)
            if args.archive_command == "backfill" and args.scheduled:
                rerun_scheduler.complete(success=bool(payload["ok"]))
            return 1 if not payload["ok"] else 0
        if args.command == "status":
            config = _load(args, require_credentials=False)
            _emit(DataStore(config.data_dir).summary(), as_json=args.json)
            return 0
        if args.command == "recalculate-conversions":
            config = _load(args, require_credentials=False)
            store = DataStore(config.data_dir)
            with _collector_lock(store.root / "state" / "collector.lock"):
                sources = run_conversion_pass(
                    config,
                    store,
                    venues=set(args.venue) if args.venue else None,
                    recalculate=True,
                )
            payload = {
                "ok": not any(source.error for source in sources),
                "sources": [source.to_dict() for source in sources],
            }
            _emit(payload, as_json=args.json)
            return 1 if not payload["ok"] else 0
        if args.command == "doctor":
            config = _load(args, require_credentials=False)
            _emit(_doctor(config), as_json=args.json)
            return 0
        if args.command == "backup":
            from perp_trade_history.runtime_backup import create_backup

            config = _load(args, require_credentials=False)
            store = DataStore(config.data_dir)
            with _collector_lock(store.root / "state" / "collector.lock"):
                payload = create_backup(output=args.output, data_dir=store.root)
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "verify-backup":
            from perp_trade_history.runtime_backup import verify_backup

            payload = verify_backup(
                archive=args.archive,
                verification_key=args.verification_key,
            )
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "restore-backup":
            from perp_trade_history.runtime_backup import restore_backup

            payload = restore_backup(
                archive=args.archive,
                target=args.target,
                verification_key=args.verification_key,
                replace=args.replace,
            )
            _emit(payload, as_json=args.json)
            return 0
        if args.command == "report":
            config = _load(args, require_credentials=False)
            group_by = [field.strip() for field in args.group_by.split(",") if field.strip()]
            store = DataStore(config.data_dir)
            rows = pnl_report(
                store,
                period=args.period,
                group_by=group_by,
                include_supplemental=args.include_supplemental,
                include_non_pnl=args.include_non_pnl,
                start_ms=_time_arg(args.start) if args.start else None,
                end_ms=_time_arg(args.end) if args.end else None,
                venues=set(args.venue) if args.venue else None,
                symbols=set(args.symbol) if args.symbol else None,
                conversion=StoredCashflowConversion(
                    store,
                    target_currency=config.reporting.target_currency,
                    method=config.reporting.conversion_method,
                    fixed_rates=config.reporting.fixed_rates,
                ),
            )
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                _write_csv(rows, Path(args.output) if args.output else None)
            return 0
        parser.error(f"unknown command: {args.command}")
    except CollectorBusyError as exc:
        LOGGER.warning("Collection skipped: %s", exc)
        _emit(
            {"ok": True, "status": "skipped", "message": str(exc)},
            as_json=getattr(args, "json", False),
        )
        return 0
    except (PerpTradeHistoryError, OSError, ValueError) as exc:
        LOGGER.error("Command failed: %s", exc)
        return 2
    return 2


def main() -> None:
    raise SystemExit(run())


def _load(args: argparse.Namespace, *, require_credentials: bool) -> AppConfig:
    return load_config(
        args.config,
        secrets_path=args.secrets,
        require_credentials=require_credentials,
    )


def _config_summary(config: AppConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "checked_at": utc_now_iso(),
        "config": str(config.path),
        "secrets": str(config.secrets_path) if config.secrets_path else "process environment",
        "data_dir": str(config.data_dir),
        "reporting": {
            "target_currency": config.reporting.target_currency,
            "conversion_method": config.reporting.conversion_method,
            "fixed_rates": {
                asset: str(rate)
                for asset, rate in sorted(config.reporting.fixed_rates.items())
            },
        },
        "enabled_venues": [venue.name for venue in config.enabled_venues],
        "read_only_http": True,
    }


def _doctor(config: AppConfig) -> dict[str, Any]:
    store = DataStore(config.data_dir)
    source_state = store.state.load()
    schedule = WeeklyRestSchedule(
        store.root / "state" / "weekly-rest-ingestion.json"
    )
    schedule_state = schedule.load()
    last_success_at = str(schedule_state.get("last_success_at") or "")
    all_failures = [
        {
            "venue": item.get("venue", ""),
            "source": item.get("source", ""),
            "category": item.get("last_error_category", "unclassified"),
            "retryable": item.get("last_error_retryable", True),
            "error": item.get("last_error", ""),
            "last_attempt_at": item.get("last_attempt_at", ""),
        }
        for item in source_state.get("sources", {}).values()
        if isinstance(item, dict) and item.get("last_error")
    ]
    failures = [
        failure
        for failure in all_failures
        if not last_success_at
        or str(failure.get("last_attempt_at") or "") > last_success_at
    ]
    eligibility = store.contract_eligibility.load()
    lock = _lock_diagnostics(store.root / "state" / "collector.lock")
    archive: dict[str, Any] = {"status": "disabled"}
    if config.venues["binance"].enabled:
        coordinator = BinanceArchiveCoordinator(config, store)
        archive_status = coordinator.status()
        jobs = archive_status["jobs"]
        archive = {
            "backfill": archive_status["backfill"],
            "rerun_schedule": ArchiveRerunScheduler(store.root).status(),
            "coverage": archive_status["coverage"],
            "job_statuses": dict(
                sorted(Counter(str(job.get("status") or "unknown") for job in jobs).items())
            ),
            "parse_failures": [
                {
                    "kind": job.get("kind", ""),
                    "archive_file": job.get("archive_file", ""),
                    "error": job.get("last_error", ""),
                }
                for job in jobs
                if job.get("status") == "parse-error"
            ],
            "quota": {
                kind: {
                    "used": coordinator._monthly_request_count(
                        coordinator._load_state(), kind
                    ),
                    "limit": ARCHIVE_MONTHLY_LIMITS[kind],
                    "next_eligible_at": coordinator._next_month_at(),
                }
                for kind in ARCHIVE_KINDS
            },
        }
    return {
        "ok": not failures,
        "checked_at": utc_now_iso(),
        "data_dir": str(store.root),
        "last_weekly_success_at": schedule_state.get("last_success_at", ""),
        "weekly_schedule": schedule.decision(datetime.now(tz=UTC)).to_dict(),
        "source_failures": failures,
        "stale_source_failure_count": len(all_failures) - len(failures),
        "terminal_exclusions": list(
            eligibility.get("contracts", {}).values()
        ),
        "archive": archive,
        "lock": lock,
    }


def _lock_diagnostics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"active": False, "metadata": {}}
    with path.open("r", encoding="utf-8") as handle:
        text = handle.read().strip()
        try:
            metadata = json.loads(text) if text else {}
        except json.JSONDecodeError:
            metadata = {"unparsed": text}
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"active": True, "metadata": metadata}
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {"active": False, "metadata": metadata}


def _check_secret_permissions(config: AppConfig) -> None:
    if not config.secrets_path:
        return
    mode = config.secrets_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"credential file must not be accessible by group or others: {config.secrets_path}"
        )


def _time_arg(value: str) -> int:
    return int(parse_datetime(value).timestamp() * 1000)


def _archive_backfill_command(config: AppConfig) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "perp_trade_history",
        "--config",
        str(config.path),
    ]
    if config.secrets_path:
        command.extend(["--secrets", str(config.secrets_path)])
    command.extend(["archives", "backfill", "--scheduled"])
    return command


class _BelowWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def _configure_logging(*, verbose: bool) -> None:
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime

    standard_handler = logging.StreamHandler(sys.stdout)
    standard_handler.addFilter(_BelowWarningFilter())
    standard_handler.setFormatter(formatter)

    problem_handler = logging.StreamHandler(sys.stderr)
    problem_handler.setLevel(logging.WARNING)
    problem_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[standard_handler, problem_handler],
        force=True,
    )


def _log_archive_actions(actions: list[ArchiveAction]) -> None:
    errors = 0
    warnings = 0
    for action in actions:
        if action.status == "error":
            errors += 1
            LOGGER.error(
                "Archive action failed: dataset=%s operation=%s error=%s",
                action.kind,
                action.action,
                action.message or "unknown error",
            )
        elif action.status == "expired":
            warnings += 1
            LOGGER.warning(
                "Archive action needs attention: dataset=%s operation=%s status=%s message=%s",
                action.kind,
                action.action,
                action.status,
                action.message or "no detail",
            )
        else:
            LOGGER.debug(
                "Archive action completed: dataset=%s operation=%s status=%s records=%d",
                action.kind,
                action.action,
                action.status,
                action.records,
            )
    LOGGER.info(
        "Archive step completed: actions=%d failed=%d warnings=%d",
        len(actions),
        errors,
        warnings,
    )


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload.get("status") in {"disabled", "not_due", "retry_wait", "skipped"}:
        _emit_inactive_summary(payload)
        return
    if "weekly_schedule" in payload and "source_failures" in payload:
        _emit_doctor_summary(payload)
        return
    if isinstance(payload.get("backfill"), dict) and isinstance(
        payload.get("jobs"), list
    ):
        _emit_archive_status(payload)
        return
    sources = payload.get("sources")
    if isinstance(sources, list) and all(isinstance(source, dict) for source in sources):
        _emit_collection_summary(payload, sources)
        return
    actions = payload.get("actions")
    if isinstance(actions, list) and all(isinstance(action, dict) for action in actions):
        _emit_archive_summary(payload, actions)
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}={json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}={value}")


def _emit_inactive_summary(payload: dict[str, Any]) -> None:
    print(f"Status: {str(payload['status']).replace('_', ' ')}")
    if payload.get("message"):
        print(f"Reason: {payload['message']}")
    schedule = payload.get("schedule")
    if isinstance(schedule, dict):
        print(
            "Schedule: "
            f"due={schedule.get('due')} "
            f"last_success={schedule.get('last_success_at') or 'never'} "
            f"next_due={schedule.get('next_due_at') or 'unknown'}"
        )


def _emit_doctor_summary(payload: dict[str, Any]) -> None:
    failures = payload.get("source_failures", [])
    exclusions = payload.get("terminal_exclusions", [])
    print(f"Doctor status: {'healthy' if payload.get('ok') else 'needs attention'}")
    print(f"Data: {payload.get('data_dir', 'unknown')}")
    schedule = payload.get("weekly_schedule", {})
    print(
        "Weekly REST: "
        f"last_success={payload.get('last_weekly_success_at') or 'never'} "
        f"status={schedule.get('status', 'unknown')} "
        f"next={schedule.get('next_due_at', 'unknown')}"
    )
    print(
        f"Failures: active={len(failures)} "
        f"stale={payload.get('stale_source_failure_count', 0)}"
    )
    for failure in failures:
        print(
            f"  - {failure.get('venue', 'unknown')}/{failure.get('source', 'unknown')} "
            f"[{failure.get('category', 'unclassified')}; "
            f"retryable={failure.get('retryable', False)}]: "
            f"{failure.get('error', 'unknown error')}"
        )
    print(f"Terminal contract exclusions: {len(exclusions)}")
    for exclusion in exclusions:
        print(
            f"  - {exclusion.get('venue', 'unknown')} "
            f"{exclusion.get('endpoint', 'unknown')} "
            f"contract={exclusion.get('contract', 'unknown')} "
            f"next_check_at_ms={exclusion.get('next_check_at_ms', 0)}"
        )
    archive = payload.get("archive", {})
    if archive.get("status") == "disabled":
        print("Archives: disabled")
    else:
        print(
            "Archives: "
            f"jobs={json.dumps(archive.get('job_statuses', {}), sort_keys=True)} "
            f"parse_failures={len(archive.get('parse_failures', []))} "
            f"backfill={archive.get('backfill', {}).get('status', 'not_started')}"
        )
        archive_backfill = archive.get("backfill", {})
        if archive_backfill.get("next_action_at"):
            print(f"  next_archive_run={archive_backfill['next_action_at']}")
        for kind, quota in sorted(archive.get("quota", {}).items()):
            print(
                f"  {kind}: quota={quota.get('used', 0)}/{quota.get('limit', 0)} "
                f"next_eligible={quota.get('next_eligible_at', 'unknown')}"
            )
    lock = payload.get("lock", {})
    print(
        f"Collector lock: active={lock.get('active', False)} "
        f"metadata={json.dumps(lock.get('metadata', {}), sort_keys=True)}"
    )


def _emit_collection_summary(payload: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    failures = [source for source in sources if source.get("error")]
    warnings = [
        (source, str(warning)) for source in sources for warning in source.get("warnings", [])
    ]
    status = str(
        payload.get("status") or ("completed" if payload.get("ok", not failures) else "failed")
    ).replace("_", " ")
    records = sum(int(source.get("source_records") or 0) for source in sources)
    succeeded = len(sources) - len(failures)

    print(f"Collection status: {status}")
    print(
        f"Sources: {succeeded} succeeded, {len(failures)} failed; "
        f"source records: {records}; warnings: {len(warnings)}"
    )
    for venue in sorted({str(source.get("venue") or "unknown") for source in sources}):
        venue_sources = [source for source in sources if source.get("venue") == venue]
        venue_failures = sum(bool(source.get("error")) for source in venue_sources)
        venue_records = sum(int(source.get("source_records") or 0) for source in venue_sources)
        print(
            f"  {venue}: sources={len(venue_sources)} failed={venue_failures} "
            f"records={venue_records}"
        )
    if failures:
        print("Failures:")
        for source in failures:
            print(
                f"  - {source.get('venue', 'unknown')}/{source.get('source', 'unknown')}: "
                f"{source.get('error')}"
            )
    if warnings:
        print("Warnings:")
        for source, warning in warnings:
            print(
                f"  - {source.get('venue', 'unknown')}/{source.get('source', 'unknown')}: {warning}"
            )
    schedule = payload.get("schedule")
    if isinstance(schedule, dict):
        print(
            "Schedule: "
            f"due={schedule.get('due')} "
            f"last_success={schedule.get('last_success_at') or 'never'} "
            f"next_due={schedule.get('next_due_at') or 'unknown'}"
        )
    if payload.get("data_dir"):
        print(f"Data directory: {payload['data_dir']}")


def _emit_archive_summary(payload: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    status = str(
        payload.get("status") or ("completed" if payload.get("ok", True) else "failed")
    ).replace("_", " ")
    counts: dict[str, int] = {}
    for action in actions:
        action_status = str(action.get("status") or "unknown")
        counts[action_status] = counts.get(action_status, 0) + 1
    count_text = ", ".join(
        f"{count} {action_status}" for action_status, count in sorted(counts.items())
    )
    print(f"Archive status: {status}")
    backfill = payload.get("backfill")
    if isinstance(backfill, dict):
        print(
            "Coverage: "
            f"target={backfill.get('expected_start_at') or 'unknown'}.."
            f"{backfill.get('target_end_at') or 'unknown'} "
            f"covered_back_to={backfill.get('covered_back_to') or 'not yet'}"
        )
        print(
            "Remaining: "
            f"report_years={backfill.get('remaining_report_years', 0)} "
            f"order_years={backfill.get('remaining_order_years', 0)} "
            f"files={backfill.get('remaining_files', 0)} "
            f"pending={backfill.get('pending_exports', 0)}"
        )
        next_window = backfill.get("next_window")
        if isinstance(next_window, dict) and next_window:
            print(
                "Next window: "
                f"{next_window.get('start_at')}..{next_window.get('end_at')} "
                f"datasets={','.join(next_window.get('missing_kinds', []))}"
            )
        if backfill.get("next_action_at"):
            print(f"Next recommended run: {backfill['next_action_at']}")
    print(f"Actions: {len(actions)}" + (f" ({count_text})" if count_text else ""))
    for action in actions:
        detail = (
            f"  - [{action.get('status', 'unknown')}] "
            f"{action.get('kind', 'unknown')}/{action.get('action', 'unknown')}"
        )
        if action.get("records"):
            detail += f" records={action['records']}"
        if action.get("message"):
            detail += f": {action['message']}"
        print(detail)
    schedule = payload.get("schedule")
    if isinstance(schedule, dict):
        print(
            "Rerun schedule: "
            f"status={schedule.get('status', 'not_scheduled')} "
            f"at={schedule.get('run_at') or 'none'} "
            f"mechanism={schedule.get('mechanism') or 'manual'}"
        )
        if schedule.get("message"):
            print(f"Schedule note: {schedule['message']}")
        if schedule.get("status") == "manual_rerun_required" and schedule.get(
            "manual_command"
        ):
            print(f"Run manually: {schedule['manual_command']}")


def _emit_archive_status(payload: dict[str, Any]) -> None:
    backfill = payload["backfill"]
    jobs = payload.get("jobs", [])
    job_counts = dict(
        sorted(Counter(str(job.get("status") or "unknown") for job in jobs).items())
    )
    print(f"Archive backfill: {str(backfill.get('status') or 'unknown').replace('_', ' ')}")
    print(
        "Coverage: "
        f"target={backfill.get('expected_start_at') or 'unknown'}.."
        f"{backfill.get('target_end_at') or 'not started'} "
        f"covered_back_to={backfill.get('covered_back_to') or 'not yet'}"
    )
    print(
        "Remaining: "
        f"report_years={backfill.get('remaining_report_years', 0)} "
        f"order_years={backfill.get('remaining_order_years', 0)} "
        f"files={backfill.get('remaining_files', 0)} "
        f"pending={backfill.get('pending_exports', 0)}"
    )
    next_window = backfill.get("next_window")
    if isinstance(next_window, dict) and next_window:
        print(
            "Next window: "
            f"{next_window.get('start_at')}..{next_window.get('end_at')} "
            f"datasets={','.join(next_window.get('missing_kinds', []))}"
        )
    if backfill.get("next_action_at"):
        print(f"Next recommended run: {backfill['next_action_at']}")
    print(f"Jobs: {json.dumps(job_counts, sort_keys=True)}")
    for kind, quota in sorted(backfill.get("quota", {}).items()):
        print(
            f"  {kind}: quota={quota.get('used', 0)}/{quota.get('limit', 0)} "
            f"remaining={quota.get('remaining', 0)}"
        )
    schedule = payload.get("schedule", {})
    print(
        "Rerun schedule: "
        f"status={schedule.get('status', 'not_scheduled')} "
        f"at={schedule.get('run_at') or 'none'} "
        f"mechanism={schedule.get('mechanism') or 'manual'}"
    )
    if schedule.get("message"):
        print(f"Schedule note: {schedule['message']}")
    if schedule.get("status") == "manual_rerun_required" and schedule.get(
        "manual_command"
    ):
        print(f"Run manually: {schedule['manual_command']}")


def _write_csv(rows: list[dict[str, Any]], output: Path | None) -> None:
    fieldnames = list(rows[0]) if rows else ["amount", "events"]
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        print(output)
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)


@contextmanager
def _collector_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            metadata = handle.read().strip()
            detail = f": {metadata}" if metadata else ""
            raise CollectorBusyError(
                f"another collection writer is active{detail}"
            ) from exc
        acquired = {
            "status": "active",
            "owner": os.environ.get("TRADIER_DEV_OWNER") or "perp-trade-history",
            "workload": next(
                (value for value in sys.argv[1:] if not value.startswith("-")),
                "unknown",
            ),
            "pid": os.getpid(),
            "started_at": utc_now_iso(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(acquired, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            acquired.update({"status": "released", "released_at": utc_now_iso()})
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(acquired, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
