from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from perp_trade_history.models import format_datetime, parse_datetime
from perp_trade_history.storage import _atomic_text, _exclusive_lock


class ArchiveRerunScheduler:
    """Install and track one-shot archive reruns without storing credentials."""

    def __init__(self, data_dir: Path, *, launch_agents_dir: Path | None = None):
        self.data_dir = data_dir
        self.state_path = data_dir / "state" / "binance-archive-rerun.json"
        self.lock_path = self.state_path.with_suffix(f"{self.state_path.suffix}.lock")
        digest = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
        self.label = f"com.perp-trade-history.archive-backfill.{digest}"
        self.launch_agents_dir = launch_agents_dir or (
            Path.home() / "Library" / "LaunchAgents"
        )
        self.plist_path = self.launch_agents_dir / f"{self.label}.plist"

    def status(self) -> dict[str, Any]:
        state = self._load()
        return {
            "status": str(state.get("status") or "not_scheduled"),
            "run_at": str(state.get("run_at") or ""),
            "mechanism": str(state.get("mechanism") or ""),
            "scheduled_at": str(state.get("scheduled_at") or ""),
            "completed_at": str(state.get("completed_at") or ""),
            "message": str(state.get("message") or ""),
            "manual_command": str(state.get("manual_command") or ""),
        }

    def schedule(
        self,
        *,
        run_at: datetime,
        command: list[str],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now_utc = (now or datetime.now(tz=UTC)).astimezone(UTC)
        run_at_utc = run_at.astimezone(UTC)
        if run_at_utc.second or run_at_utc.microsecond:
            run_at_utc = run_at_utc.replace(second=0, microsecond=0) + timedelta(
                minutes=1
            )
        if run_at_utc <= now_utc:
            run_at_utc = (now_utc + timedelta(minutes=1)).replace(
                second=0, microsecond=0
            )
        with _exclusive_lock(self.lock_path):
            self._cancel_loaded(self._load())
            try:
                if platform.system() == "Darwin" and shutil.which("launchctl"):
                    mechanism, external_id = self._schedule_launchd(
                        run_at_utc, command
                    )
                elif shutil.which("at"):
                    mechanism, external_id = self._schedule_at(run_at_utc, command)
                else:
                    raise OSError("no supported one-shot scheduler is available")
            except (OSError, subprocess.SubprocessError) as exc:
                state = {
                    "schema_version": 1,
                    "status": "manual_rerun_required",
                    "run_at": format_datetime(run_at_utc),
                    "mechanism": "",
                    "external_id": "",
                    "scheduled_at": format_datetime(now_utc),
                    "completed_at": "",
                    "message": str(exc),
                    "manual_command": shlex.join(command),
                }
                self._save(state)
                return dict(state)

            state = {
                "schema_version": 1,
                "status": "scheduled",
                "run_at": format_datetime(run_at_utc),
                "mechanism": mechanism,
                "external_id": external_id,
                "scheduled_at": format_datetime(now_utc),
                "completed_at": "",
                "message": "",
                "manual_command": shlex.join(command),
            }
            self._save(state)
            return dict(state)

    def mark_running(self, *, now: datetime | None = None) -> None:
        with _exclusive_lock(self.lock_path):
            state = self._load()
            state["status"] = "running"
            state["started_at"] = format_datetime(now or datetime.now(tz=UTC))
            self._save(state)

    def complete(self, *, success: bool, now: datetime | None = None) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._load()
            mechanism = str(state.get("mechanism") or "")
            external_id = str(state.get("external_id") or "")
            if mechanism == "launchd":
                self.plist_path.unlink(missing_ok=True)
            state.update(
                {
                    "status": "completed" if success else "failed",
                    "completed_at": format_datetime(now or datetime.now(tz=UTC)),
                    "message": "" if success else "scheduled archive rerun failed",
                }
            )
            self._save(state)
            if mechanism == "launchd":
                self._bootout_launchd(external_id or self.label)
            return dict(state)

    def cancel(self) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._load()
            self._cancel_loaded(state)
            state.update(
                {
                    "schema_version": 1,
                    "status": "cancelled",
                    "run_at": "",
                    "mechanism": "",
                    "external_id": "",
                    "completed_at": format_datetime(datetime.now(tz=UTC)),
                    "message": "",
                }
            )
            self._save(state)
            return dict(state)

    def _schedule_launchd(
        self, run_at: datetime, command: list[str]
    ) -> tuple[str, str]:
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        self._bootout_launchd(self.label)
        local_run_at = run_at.astimezone()
        payload = {
            "Label": self.label,
            "ProgramArguments": command,
            "RunAtLoad": False,
            "StartCalendarInterval": {
                "Month": local_run_at.month,
                "Day": local_run_at.day,
                "Hour": local_run_at.hour,
                "Minute": local_run_at.minute,
            },
            "StandardOutPath": str(
                self.data_dir / "state" / "binance-archive-rerun.stdout.log"
            ),
            "StandardErrorPath": str(
                self.data_dir / "state" / "binance-archive-rerun.stderr.log"
            ),
        }
        _atomic_text(
            self.plist_path,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8"),
        )
        domain = f"gui/{os.getuid()}"
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(self.plist_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.plist_path.unlink(missing_ok=True)
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise OSError(f"launchd scheduling failed: {detail}")
        return "launchd", self.label

    @staticmethod
    def _schedule_at(run_at: datetime, command: list[str]) -> tuple[str, str]:
        timestamp = run_at.astimezone().strftime("%Y%m%d%H%M.%S")
        result = subprocess.run(
            ["at", "-t", timestamp],
            input=shlex.join(command) + "\n",
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise OSError(f"at scheduling failed: {detail}")
        match = re.search(r"\bjob\s+(\d+)\b", f"{result.stdout}\n{result.stderr}")
        return "at", match.group(1) if match else ""

    def _cancel_loaded(self, state: dict[str, Any]) -> None:
        mechanism = str(state.get("mechanism") or "")
        external_id = str(state.get("external_id") or "")
        if mechanism == "launchd":
            self.plist_path.unlink(missing_ok=True)
            self._bootout_launchd(external_id or self.label)
        elif mechanism == "at" and external_id and shutil.which("atrm"):
            subprocess.run(
                ["atrm", external_id],
                check=False,
                capture_output=True,
                text=True,
            )

    @staticmethod
    def _bootout_launchd(label: str) -> None:
        if not shutil.which("launchctl"):
            return
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 1, "status": "not_scheduled"}
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported archive rerun state in {self.state_path}")
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = 1
        _atomic_text(self.state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def parse_schedule_delay(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9]\d*)([md])", value.strip().lower())
    if not match:
        raise ValueError("schedule delay must use a positive number followed by m or d")
    amount = int(match.group(1))
    return timedelta(minutes=amount) if match.group(2) == "m" else timedelta(days=amount)


def scheduled_time(value: str) -> datetime:
    return parse_datetime(value)
