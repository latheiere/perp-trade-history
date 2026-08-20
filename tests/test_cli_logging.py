import json
import logging
from contextlib import contextmanager

from perp_trade_history import cli
from perp_trade_history.errors import CollectorBusyError


def test_operational_logging_routes_regular_and_problem_levels_to_separate_streams(
    capsys,
) -> None:
    cli._configure_logging(verbose=False)
    logger = logging.getLogger("perp_trade_history.test")

    logger.debug("diagnostic detail")
    logger.info("collection started")
    logger.warning("collection delayed")
    logger.error("collection failed")

    captured = capsys.readouterr()
    assert "collection started" in captured.out
    assert "diagnostic detail" not in captured.out
    assert "collection delayed" not in captured.out
    assert "collection delayed" in captured.err
    assert "collection failed" in captured.err
    assert "collection started" not in captured.err
    assert "Z | INFO    | perp_trade_history.test |" in captured.out


def test_human_collection_output_summarizes_sources_without_storage_payloads(
    capsys,
) -> None:
    payload = {
        "ok": False,
        "status": "retry_required",
        "data_dir": "/var/lib/history",
        "sources": [
            {
                "venue": "venue-a",
                "source": "account-events",
                "source_records": 12,
                "error": "",
                "warnings": ["history is retention-limited"],
                "raw": {"inserted": 12, "total": 2000},
                "tables": {"events": {"inserted": 12, "total": 1900}},
            },
            {
                "venue": "venue-b",
                "source": "orders",
                "source_records": 0,
                "error": "request rejected",
                "warnings": [],
                "raw": {},
                "tables": {},
            },
        ],
    }

    cli._emit(payload, as_json=False)

    output = capsys.readouterr().out
    assert "Collection status: retry required" in output
    assert "Sources: 1 succeeded, 1 failed; source records: 12; warnings: 1" in output
    assert "venue-b/orders: request rejected" in output
    assert '"raw"' not in output
    assert '"tables"' not in output


def test_json_collection_output_preserves_complete_source_details(capsys) -> None:
    payload = {
        "ok": True,
        "sources": [
            {
                "venue": "venue-a",
                "source": "events",
                "source_records": 1,
                "raw": {"inserted": 1},
            }
        ],
    }

    cli._emit(payload, as_json=True)

    assert json.loads(capsys.readouterr().out) == payload


def test_archive_output_lists_action_outcomes_without_serializing_internal_fields(
    capsys,
) -> None:
    payload = {
        "ok": False,
        "status": "advanced",
        "actions": [
            {
                "status": "error",
                "kind": "orders",
                "action": "poll",
                "message": "archive schema was not recognized",
                "download_id": "machine-only-value",
            }
        ],
    }

    cli._emit(payload, as_json=False)

    output = capsys.readouterr().out
    assert "Archive status: advanced" in output
    assert "[error] orders/poll: archive schema was not recognized" in output
    assert "machine-only-value" not in output


def test_archive_backfill_output_explains_coverage_and_next_operator_action(capsys) -> None:
    payload = {
        "ok": True,
        "status": "waiting_for_exports",
        "actions": [],
        "backfill": {
            "expected_start_at": "2022-01-01T00:00:00.000Z",
            "target_end_at": "2026-08-20T00:00:00.000Z",
            "covered_back_to": "2025-08-20T00:00:00.000Z",
            "remaining_report_years": 4,
            "remaining_order_years": 4,
            "remaining_files": 12,
            "pending_exports": 3,
            "next_window": {
                "start_at": "2024-08-20T00:00:00.000Z",
                "end_at": "2025-08-19T23:59:59.999Z",
                "missing_kinds": ["trades", "income", "orders"],
            },
            "next_action_at": "2026-08-20T00:10:00.000Z",
        },
        "schedule": {
            "status": "scheduled",
            "run_at": "2026-08-20T00:10:00.000Z",
            "mechanism": "launchd",
        },
    }

    cli._emit(payload, as_json=False)

    output = capsys.readouterr().out
    assert "Archive status: waiting for exports" in output
    assert "Remaining: report_years=4 order_years=4 files=12 pending=3" in output
    assert "Next recommended run: 2026-08-20T00:10:00.000Z" in output
    assert "Rerun schedule: status=scheduled" in output


def test_overlapping_singleton_collection_is_a_successful_warning(
    tmp_path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "[storage]",
                'data_dir = "history"',
                "[venues.mexc]",
                "enabled = true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PERP_TRADE_HISTORY_MEXC_API_KEY", "read-key")
    monkeypatch.setenv("PERP_TRADE_HISTORY_MEXC_API_SECRET", "read-secret")

    @contextmanager
    def busy_collector(_path):
        raise CollectorBusyError("another collection writer is active")
        yield

    monkeypatch.setattr(cli, "_collector_lock", busy_collector)

    exit_code = cli.run(
        [
            "--config",
            str(config_path),
            "weekly-collect",
            "--now",
            "2026-08-17T08:00:00Z",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: skipped" in captured.out
    assert "Reason: another collection writer is active" in captured.out
    assert "Collection skipped: another collection writer is active" in captured.err
