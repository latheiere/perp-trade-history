from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from perp_trade_history.models import format_datetime, parse_datetime
from perp_trade_history.storage import _atomic_text, _exclusive_lock


@dataclass(frozen=True, slots=True)
class WeeklyDecision:
    due: bool
    due_at: str
    next_due_at: str
    last_success_at: str = ""
    status: str = "due"
    retry_venues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WeeklyRestSchedule:
    """Persist a UTC weekly gate for an interval-based local supervisor."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")

    def decision(self, now: datetime) -> WeeklyDecision:
        now_utc = now.astimezone(UTC)
        due_at = _latest_monday_eight(now_utc)
        state = self.load()
        last_due_text = str(state.get("last_success_due_at") or "")
        last_due = parse_datetime(last_due_text) if last_due_text else None
        due = last_due is None or last_due < due_at
        status = "due" if due else "not_due"
        retry_venues: tuple[str, ...] = ()
        next_due = due_at if due else due_at + timedelta(days=7)
        retry_due_text = str(state.get("retry_due_at") or "")
        next_retry_text = str(state.get("next_retry_at") or "")
        if due and retry_due_text == format_datetime(due_at) and next_retry_text:
            next_retry = parse_datetime(next_retry_text)
            retry_venues = tuple(
                sorted(str(value) for value in state.get("retry_venues", []))
            )
            if now_utc < next_retry:
                due = False
                status = "retry_wait"
                next_due = next_retry
        return WeeklyDecision(
            due=due,
            due_at=format_datetime(due_at),
            next_due_at=format_datetime(next_due),
            last_success_at=str(state.get("last_success_at") or ""),
            status=status,
            retry_venues=retry_venues,
        )

    def record_attempt(
        self,
        *,
        now: datetime,
        decision: WeeklyDecision,
        success: bool,
        error_count: int,
        retryable_error_count: int = 0,
        retry_venues: set[str] | None = None,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self.load()
            state.update(
                {
                    "schema_version": 1,
                    "last_attempt_at": format_datetime(now),
                    "last_error_count": error_count,
                    "last_retryable_error_count": retryable_error_count,
                }
            )
            if success:
                state.update(
                    {
                        "last_success_at": format_datetime(now),
                        "last_success_due_at": decision.due_at,
                        "consecutive_failures": 0,
                        "next_retry_at": "",
                        "retry_due_at": "",
                        "retry_venues": [],
                    }
                )
            else:
                failures = int(state.get("consecutive_failures") or 0) + 1
                delays = (
                    timedelta(minutes=15),
                    timedelta(hours=1),
                    timedelta(hours=6),
                    timedelta(hours=24),
                )
                delay = delays[min(failures - 1, len(delays) - 1)]
                state.update(
                    {
                        "consecutive_failures": failures,
                        "next_retry_at": format_datetime(now + delay),
                        "retry_due_at": decision.due_at,
                        "retry_venues": sorted(retry_venues or set()),
                    }
                )
            _atomic_text(self.path, json.dumps(state, indent=2, sort_keys=True) + "\n")
            return state

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": 1,
                "last_attempt_at": "",
                "last_success_at": "",
                "last_success_due_at": "",
                "last_error_count": 0,
                "last_retryable_error_count": 0,
                "consecutive_failures": 0,
                "next_retry_at": "",
                "retry_due_at": "",
                "retry_venues": [],
            }
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported weekly schedule state in {self.path}")
        return payload


def _latest_monday_eight(now: datetime) -> datetime:
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    return monday if now >= monday else monday - timedelta(days=7)
