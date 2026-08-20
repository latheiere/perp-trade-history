import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from perp_trade_history import archive_schedule
from perp_trade_history.archive_schedule import (
    ArchiveRerunScheduler,
    parse_schedule_delay,
)


def test_schedule_delay_accepts_operator_friendly_minute_and_day_values() -> None:
    assert parse_schedule_delay("10m") == timedelta(minutes=10)
    assert parse_schedule_delay("31d") == timedelta(days=31)
    with pytest.raises(ValueError, match="positive number"):
        parse_schedule_delay("10 minutes")


def test_unavailable_scheduler_persists_exact_manual_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    scheduler = ArchiveRerunScheduler(
        tmp_path / "data", launch_agents_dir=tmp_path / "LaunchAgents"
    )
    monkeypatch.setattr(archive_schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(archive_schedule.shutil, "which", lambda _name: None)
    command = ["/opt/history/python", "-m", "perp_trade_history", "archives", "backfill"]

    result = scheduler.schedule(
        run_at=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
        command=command,
        now=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
    )

    assert result["status"] == "manual_rerun_required"
    assert result["run_at"] == "2026-09-20T08:00:00.000Z"
    assert result["manual_command"] == " ".join(command)
    assert scheduler.status()["manual_command"] == " ".join(command)


def test_macos_schedule_installs_replaceable_one_shot_launch_agent(
    tmp_path: Path, monkeypatch
) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    scheduler = ArchiveRerunScheduler(
        tmp_path / "data", launch_agents_dir=launch_agents
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(archive_schedule.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        archive_schedule.shutil,
        "which",
        lambda name: f"/bin/{name}" if name == "launchctl" else None,
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(archive_schedule.subprocess, "run", fake_run)
    command = ["/opt/history/python", "-m", "perp_trade_history", "archives", "backfill"]

    result = scheduler.schedule(
        run_at=datetime.now(tz=UTC) + timedelta(minutes=10),
        command=command,
    )

    assert result["status"] == "scheduled"
    assert result["mechanism"] == "launchd"
    payload = plistlib.loads(scheduler.plist_path.read_bytes())
    assert payload["Label"] == scheduler.label
    assert payload["ProgramArguments"] == command
    assert any(call[:2] == ["launchctl", "bootstrap"] for call in calls)

    completed = scheduler.complete(success=True)

    assert completed["status"] == "completed"
    assert not scheduler.plist_path.exists()
    assert any(call[:2] == ["launchctl", "bootout"] for call in calls)
