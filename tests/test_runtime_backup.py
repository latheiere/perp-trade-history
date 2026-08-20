from pathlib import Path

from perp_trade_history.runtime_backup import (
    create_backup,
    restore_backup,
    verify_backup,
)


def test_statecrate_backup_verifies_and_restores_complete_durable_state(
    tmp_path: Path,
) -> None:
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
