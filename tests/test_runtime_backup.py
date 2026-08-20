from pathlib import Path

import pytest

from perp_trade_history import runtime_backup
from perp_trade_history.errors import OptionalDependencyError
from perp_trade_history.runtime_backup import (
    create_backup,
    restore_backup,
    verify_backup,
)


def test_statecrate_backup_verifies_and_restores_complete_durable_state(
    tmp_path: Path,
) -> None:
    pytest.importorskip("statecrate")
    data_dir = tmp_path / "data"
    (data_dir / "normalized").mkdir(parents=True)
    (data_dir / "normalized" / "orders.csv").write_text(
        "record_id\nrecord-1\n", encoding="utf-8"
    )
    archive = tmp_path / "archives" / "history.tar.gz"

    created = create_backup(output=archive, data_dir=data_dir)
    verified = verify_backup(archive=archive)
    restored = restore_backup(
        archive=archive,
        target=tmp_path / "restored",
    )

    assert created["ok"] is True
    assert verified["entries_checked"] >= 2
    assert restored["entries_restored"] == verified["entries_checked"]
    assert (
        tmp_path
        / "restored"
        / "trade-history"
        / "normalized"
        / "orders.csv"
    ).read_text(encoding="utf-8") == "record_id\nrecord-1\n"


def test_backup_surface_reports_optional_dependency_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = __import__

    def without_statecrate(name: str, *args: object, **kwargs: object) -> object:
        if name == "statecrate":
            error = ModuleNotFoundError("No module named 'statecrate'")
            error.name = "statecrate"
            raise error
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", without_statecrate)

    with pytest.raises(OptionalDependencyError, match=r"\[backup\]"):
        runtime_backup._statecrate()
