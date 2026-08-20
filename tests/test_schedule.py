from datetime import UTC, datetime
from pathlib import Path

from perp_trade_history import cli
from perp_trade_history.schedule import WeeklyRestSchedule


def test_weekly_schedule_runs_once_after_monday_utc_boundary(tmp_path) -> None:
    schedule = WeeklyRestSchedule(tmp_path / "weekly.json")
    now = datetime(2026, 8, 17, 8, 1, tzinfo=UTC)

    first = schedule.decision(now)
    schedule.record_attempt(now=now, decision=first, success=True, error_count=0)
    repeated = schedule.decision(datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    following = schedule.decision(datetime(2026, 8, 24, 8, 0, tzinfo=UTC))

    assert first.due is True
    assert first.due_at == "2026-08-17T08:00:00.000Z"
    assert repeated.due is False
    assert repeated.next_due_at == "2026-08-24T08:00:00.000Z"
    assert following.due is True


def test_failed_weekly_collection_honors_persisted_retry_backoff(tmp_path) -> None:
    schedule = WeeklyRestSchedule(tmp_path / "weekly.json")
    now = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    decision = schedule.decision(now)

    schedule.record_attempt(
        now=now,
        decision=decision,
        success=False,
        error_count=1,
        retryable_error_count=1,
        retry_venues={"mexc"},
    )

    waiting = schedule.decision(datetime(2026, 8, 17, 8, 5, tzinfo=UTC))
    retry = schedule.decision(datetime(2026, 8, 17, 8, 15, tzinfo=UTC))

    assert waiting.due is False
    assert waiting.status == "retry_wait"
    assert waiting.next_due_at == "2026-08-17T08:15:00.000Z"
    assert retry.due is True
    assert retry.retry_venues == ("mexc",)


def test_weekly_boundary_is_calculated_after_timezone_normalization(tmp_path) -> None:
    schedule = WeeklyRestSchedule(tmp_path / "weekly.json")
    local_offset = datetime.fromisoformat("2026-08-17T09:00:00+01:00")

    decision = schedule.decision(local_offset)

    assert decision.due_at == "2026-08-17T08:00:00.000Z"


def test_weekly_command_invokes_only_rest_sync_engine(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
schema_version = 1
[storage]
data_dir = "history"
[venues.mexc]
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PERP_TRADE_HISTORY_MEXC_API_KEY", "read-key")
    monkeypatch.setenv("PERP_TRADE_HISTORY_MEXC_API_SECRET", "read-secret")
    calls: list[int] = []

    class _Result:
        errors: list[object] = []

        @staticmethod
        def to_dict():
            return {"ok": True, "sources": []}

    class _SyncEngine:
        def __init__(self, config, store):
            pass

        @staticmethod
        def collect(*, end_ms: int):
            calls.append(end_ms)
            return _Result()

    class _UnexpectedArchiveCoordinator:
        def __init__(self, *args, **kwargs):
            raise AssertionError("weekly collection must not enter archive ingestion")

    monkeypatch.setattr(cli, "SyncEngine", _SyncEngine)
    monkeypatch.setattr(
        cli, "BinanceArchiveCoordinator", _UnexpectedArchiveCoordinator
    )

    exit_code = cli.run(
        [
            "--config",
            str(config_path),
            "weekly-collect",
            "--now",
            "2026-08-17T08:00:00Z",
            "--json",
        ]
    )

    assert exit_code == 0
    expected_end_ms = int(datetime(2026, 8, 17, 8, 0, tzinfo=UTC).timestamp() * 1000)
    assert calls == [expected_end_ms]
    assert '"status": "collected"' in capsys.readouterr().out
